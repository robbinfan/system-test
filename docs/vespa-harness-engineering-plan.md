# Vespa Harness Engineering: 实施路径

## 现状

- CI/CD 集成了 ST，但只跑 2 个（太慢）
- 有线上 skill 能力：k8s 服务发现、schema 拉取、document visit、metrics、logs、search、feed
- 目标：从"跑 2 个固定 ST" 演进到 "上下文感知的自动化 harness"

---

## 实施路径总览

```
里程碑 0          里程碑 1              里程碑 2              里程碑 3
CI 跑 2 个 ST  →  智能选 5-15 个 ST  →  线上数据补测试  →  多 Agent Harness
(现状)            (1-2 周)              (2-4 周)             (4-8 周)
                   ↑ 已实现               ↑ 已实现框架
                   select-tests.rb        generate-tests-from-prod.rb
```

每个里程碑独立可用，不依赖后续里程碑。

---

## 里程碑 1：智能测试筛选（已实现工具，需集成 CI）

### 已完成

- `bin/select-tests.rb` — 基于 git diff 的 6 层打分测试筛选器
- 737 个测试的特性指纹索引（tensor, hnsw, ranking 等 30+ 特性）

### 待做：集成到 GitLab CI

**现状**：GitLab CI 触发 → build docker → 跑 2 个硬编码 ST

**目标**：利用 GitLab 的 [Dynamic Child Pipeline](https://docs.gitlab.com/ee/ci/pipelines/downstream_pipelines.html#dynamic-child-pipelines)，在 build 之后动态生成测试矩阵。

```yaml
# .gitlab-ci.yml
stages:
  - build
  - select-tests
  - system-test

build-docker:
  stage: build
  script:
    - docker build -t vespa-st:${CI_COMMIT_SHORT_SHA} -f docker/Dockerfile.systemtest .
  artifacts:
    paths: [docker-image.tar]

# Stage 1: 分析变更，动态生成测试列表
select-tests:
  stage: select-tests
  script:
    - |
      TESTS=$(ruby bin/select-tests.rb \
        --diff origin/${CI_MERGE_REQUEST_TARGET_BRANCH_NAME}..HEAD \
        --top 15 --format json)

      # 如果没选到任何测试，fallback 到 baseline
      COUNT=$(echo "$TESTS" | ruby -rjson -e 'puts JSON.parse(STDIN.read).size')
      if [ "$COUNT" -eq "0" ]; then
        echo "No relevant tests, using baseline"
        TESTS='[{"file":"search/basicsearch/basic_search.rb"},{"file":"TBD_YOUR_2ND_TEST"}]'
      fi

      # 生成 child pipeline YAML（每个测试一个并行 job）
      ruby -rjson -ryaml -e '
        tests = JSON.parse(ARGV[0])
        pipeline = tests.each_with_index.map { |t, i|
          ["st-#{i}", {
            "stage" => "test",
            "script" => "bin/run-tests-on-swarm.sh -f #{t["file"]} --consoleoutput --nodelimit 1",
            "tags" => ["docker", "vespa-st"],
            "timeout" => "30m",
            "allow_failure" => t.fetch("score", 100) < 50
          }]
        }.to_h
        pipeline["stages"] = ["test"]
        File.write("dynamic-st-pipeline.yml", pipeline.to_yaml)
      ' "$TESTS"
  artifacts:
    paths: [dynamic-st-pipeline.yml]

# Stage 2: 并行跑选出的测试
run-system-tests:
  stage: system-test
  trigger:
    include:
      - artifact: dynamic-st-pipeline.yml
        job: select-tests
    strategy: depend
```

**关键优势**：

- **并行**：GitLab child pipeline 的每个 test job 独立并行，总时间 ≈ 最慢的单个 ST（而非串行 N 个）
- **动态**：每次 MR 根据 diff 自动决定跑哪些，不再硬编码
- **渐进**：低分测试（score < 50）设为 `allow_failure`，不阻塞 pipeline
- **Fallback**：没选到相关测试时仍跑 baseline 2 个，保证最低覆盖
- **可观测**：GitLab UI 上每个 ST 是独立 job，一眼看到哪个挂了

### 与现有 Docker 流程的兼容

你们现在 build docker 就跑 ST，这个不变。变的只是"跑哪些"这个决策从硬编码变成动态的。`run-tests-on-swarm.sh` 的 `-f` 参数已经支持指定单个测试文件，所以不需要改测试执行层。

### 效果

- 从固定 2 个 → 动态 5-15 个，覆盖率大幅提升
- 改 nearest_neighbor schema → 自动选出 5 个 ANN 测试
- 改 container 配置 → 自动选出 search_chains 等测试
- 不相关的变更 → 仍然只跑 2 个 baseline
- **并行执行**：15 个测试并行跑，总时间可能跟以前串行 2 个差不多

### 待确认

1. **你们 CI 跑的那 2 个 ST 的具体文件名** — 设为 fallback baseline
2. **GitLab Runner 配置**：runner 有 docker executor？能起 Docker Swarm？还是用 k8s executor？
3. **可接受的 CI 时长**：目前 2 个 ST 跑多久？并行能接受到多少个？

---

## 里程碑 2：线上感知 + 动态测试生成

### 核心思路

```
                    ┌─────────────────┐
 git diff ──────→   │  select-tests   │ ──→ 现有 ST 子集
                    └────────┬────────┘
                             │
                      coverage gaps?
                             │
                    ┌────────▼────────┐         ┌──────────────┐
                    │ generate-tests  │ ◄───────│  线上 Vespa   │
                    │ from-prod       │         │  k8s cluster  │
                    └────────┬────────┘         │              │
                             │                  │ ① schema 拉取 │
                    ┌────────▼────────┐         │ ② visit 数据  │
                    │ 生成的回归测试    │         │ ③ 查询基线    │
                    │ feed.json       │         │ ④ metrics    │
                    │ expected.json   │         └──────────────┘
                    │ test.rb         │
                    └─────────────────┘
```

### 已完成框架

- `bin/generate-tests-from-prod.rb` — 4 阶段编排器：
  1. **ProdCapture** — 从线上拉 schema、visit 文档、抓查询基线、采集 metrics
  2. **CoverageAnalyzer** — 对比变更特性 vs 已选测试覆盖，找出 gap
  3. **TestGenerator** — 用线上数据生成完整的 ST（.rb + feed.json + services.xml + expected_results.json）
  4. **Orchestrator** — 串联整个流程

### 三类覆盖 Gap → 三种线上数据注入

| Gap 类型 | 触发条件 | 线上数据用法 |
|----------|----------|-------------|
| **Schema Gap** | 改了 .sd，但没有对应 ST | visit 该 schema 的线上文档 → 生成 feed.json + 基础查询测试 |
| **Ranking Gap** | 改了 rank-profile/first-phase | 抓线上 top queries → 记录当前结果作为 baseline → 生成排序回归测试 |
| **Migration Gap** | 改了 struct-field/map/reference | visit 线上真实文档结构 → 验证新 schema 能正确索引旧数据 |

### 待做：对接你们的线上 skill

当前 `ProdCapture` 类用的是标准 Vespa HTTP API：

```ruby
# 现在的实现（直接 HTTP 调用）
capture = ProdCapture.new("http://vespa.prod.svc:8080")
capture.capture_documents("music", sample_size: 50)
capture.capture_query_baselines(queries)
```

**需要你补充的适配层**：

```ruby
# 方案 A：如果你们的 skill 是 CLI 工具
# 替换 ProdCapture 里的 HTTP 调用为 skill 调用
def capture_documents_via_skill(schema_name, sample_size)
  # 你们的 visit skill 命令是什么？类似：
  `vespa-skill visit --schema #{schema_name} --limit #{sample_size} --format json`
end

def download_schema_via_skill(schema_name)
  # 你们的 schema 拉取 skill 命令是什么？类似：
  `vespa-skill schema get #{schema_name}`
end

# 方案 B：如果你们的 skill 是 MCP Server
# 通过 Claude Code / Cursor 的 MCP 协议调用
# 这种方式更适合 harness agent 直接编排
```

### 你需要补充什么

4. **线上 skill 的调用接口**：是 CLI 命令？MCP Server？HTTP API？具体的命令/endpoint 格式是什么？
5. **线上环境的访问方式**：
   - k8s service 地址怎么获取？（kubectl？service discovery？固定地址？）
   - 需要 TLS 证书吗？
   - 有多套环境吗？（dev/staging/prod）
6. **数据安全约束**：线上数据 dump 下来有脱敏需求吗？能直接用于测试吗？
7. **查询日志来源**：有 access log 吗？格式是什么？还是通过 metrics/monitoring 获取 top queries？

---

## 里程碑 3：多 Agent Harness

### 架构

```
┌──────────────────────────────────────────────────────────────┐
│                     Harness Orchestrator                      │
│                                                              │
│  ┌──────────┐    spec    ┌───────────┐   code    ┌────────┐ │
│  │ Planner  │ ─────────→ │ Generator │ ────────→ │Evaluator│ │
│  │ Agent    │            │ Agent     │           │ Agent   │ │
│  │          │ ← reject ─ │           │ ← fail ── │         │ │
│  └──────────┘            └───────────┘           └────┬────┘ │
│       │                       │                       │      │
│   reads:                  writes:                  runs:     │
│   - codebase              - .sd files              - select  │
│   - vespa docs            - services.xml             -tests  │
│   - prod metrics          - java code              - gen     │
│   - issue/ticket          - pom.xml                  -tests  │
│                                                    - prod    │
│                                                      capture │
└──────────────────────────────────────────────────────────────┘
                                │
                    ┌───────────┼───────────┐
                    ▼           ▼           ▼
              线上 Vespa    测试 Vespa    Git/CI
              (只读)        (读写)        (push)
```

### 关键设计决策

**Q: Agent 运行在哪里？**

团队主要用 Cursor，所以 **Cursor Cloud Agent (self-hosted)** 是首选执行层：

| 角色 | 运行环境 | 原因 |
|------|---------|------|
| **Planner** | Cursor 本地 (Composer) | 交互式对齐需求，团队已熟悉 |
| **Generator** | Cursor Cloud Worker | Sprint 迭代可能几小时，需要长运行 |
| **Evaluator** | Cursor Cloud Worker | 跑 ST 慢，独立 worker 不阻塞开发 |

**Cursor Cloud Agent 的关键能力**：
- Worker 跑在你们 k8s 里，代码和数据不离开内网
- 每个 session 独占 worker → 可以跑长时间 ST
- 支持 MCP Server → 直接复用你们的线上 skill（schema/visit/metrics）
- 支持 Subagent → Evaluator 可以 spawn 多个子 agent 并行跑不同 ST
- Helm chart 部署 → 和你们现有 k8s 基础设施一致

**渐进路径**：
1. 先在 Cursor 本地验证 Evaluator prompt（用 Composer 手动跑 select-tests + ST）
2. 确认有效后，部署 1 个 Cloud Worker 试跑
3. 扩展到 3-5 worker 池，支持多 Agent 并行

### Sprint Contract 模板

```yaml
# .harness/sprint-contract.yaml
task: "给商品搜索加同义词扩展"
schema: product
environment:
  prod_endpoint: http://vespa.prod.svc:8080
  test_cluster: docker-swarm

sprints:
  - id: 1
    goal: "Schema 变更 + 基础功能验证"
    acceptance:
      - type: schema_compile
        pass: true
      - type: existing_st
        tests: auto  # 由 select-tests.rb 自动选择
        pass: all
      - type: prod_data_feed
        sample_size: 100
        pass: "all documents indexed without error"

  - id: 2
    goal: "同义词查询验证"
    acceptance:
      - type: prod_query_regression
        queries_from: access_log
        top_n: 20
        tolerance: 0.1  # 允许 10% hitcount 波动
      - type: custom_query
        query: "手机"
        expect_hits_contain: ["mobile phone", "cellphone"]

  - id: 3
    goal: "性能验证"
    acceptance:
      - type: latency_baseline
        p99_max_ms: 50
        source: prod_metrics
      - type: throughput_baseline
        qps_min: 1000
```

### 你需要补充什么

8. **Cursor Cloud Agent 部署**：
   - 你们 k8s 集群有多余资源部署 worker 吗？初始 1-3 个就够
   - 需要走安全审批流程吗？（worker HTTPS 出站到 Cursor Cloud）
9. **编排偏好**：
   - 希望 Agent 全自动（push 触发 → 自动跑完 → 输出 MR review），还是半自动（人在环中确认关键步骤）？
   - 失败后的行为：自动重试修复？还是通知人？

---

## 里程碑 4（远期）：持续学习 + 自优化

### 思路

- **测试选择优化**：记录每次 "选了哪些测试 → 实际哪些 pass/fail"，用反馈调整权重
- **线上基线自动更新**：定期从线上抓 query baseline，发现 baseline drift 自动告警
- **Flaky test 检测**：标记不稳定的 ST，降低其权重
- **成本优化**：分析 token 使用，在 Haiku/Sonnet/Opus 之间动态选模型

---

## 完整工具清单

| 工具 | 状态 | 用途 |
|------|------|------|
| `bin/select-tests.rb` | ✅ 已实现 | 基于 git diff 动态选择 ST |
| `bin/generate-tests-from-prod.rb` | ✅ 框架已实现 | 从线上数据生成回归测试 |
| `bin/run-tests-on-swarm.sh` | ✅ 已有 | Vespa ST 执行器 |
| CI 集成 YAML | ⬜ 待做 | 把 select-tests 接入 CI pipeline |
| 线上 skill 适配层 | ⬜ 待做 | 对接你们的 vespa skill 到 ProdCapture |
| Sprint Contract 解析器 | ⬜ 待做 | 解析 YAML contract，驱动 Agent 流程 |
| Evaluator Agent prompt | ⬜ 待做 | Agent 的 system prompt + tool 定义 |
| Harness Orchestrator | ⬜ 待做 | Planner → Generator → Evaluator 编排 |

---

## 你需要回答的问题（按优先级排序）

### 立即需要（里程碑 1 上线）

1. CI 现在跑的 2 个 ST 的具体文件名是什么？
2. CI 环境是什么？ST 在 CI 里怎么跑起来的？
3. 可接受的 CI 时长上限？

### 短期需要（里程碑 2 落地）

4. 线上 skill 的调用方式和接口格式？
5. k8s 上 Vespa 的服务发现/地址获取方式？
6. 数据脱敏需求？
7. 查询日志获取方式？

### 中期需要（里程碑 3 设计）

8. Agent 运行环境偏好和工具选型？
9. 自动化程度偏好（全自动 vs 人在环中）？
