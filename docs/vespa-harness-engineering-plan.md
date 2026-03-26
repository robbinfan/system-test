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

### 待做：集成到你们的 CI

```yaml
# 示例：GitHub Actions / 你们的 CI pipeline
steps:
  - name: Select relevant system tests
    run: |
      TESTS=$(ruby bin/select-tests.rb --diff origin/main..HEAD --top 15 --format runtest)
      if [ -z "$TESTS" ]; then
        echo "No relevant ST found, running baseline 2 tests"
        TESTS="-f search/basicsearch/basic_search.rb -f config/deploy/deploy_and_activate.rb"
      fi
      echo "SELECTED_TESTS=$TESTS" >> $GITHUB_ENV

  - name: Run selected system tests
    run: bin/run-tests-on-swarm.sh $SELECTED_TESTS --consoleoutput
```

### 效果

- 从固定 2 个 → 动态 5-15 个，覆盖率大幅提升
- 改 nearest_neighbor schema → 自动选出 5 个 ANN 测试
- 改 container 配置 → 自动选出 search_chains 等测试
- 不相关的变更 → 仍然只跑 2 个 baseline

### 你需要补充什么

1. **确认你们 CI 跑的那 2 个 ST 的具体文件名** — 我把它们设为 fallback baseline
2. **你们的 CI 环境**：是 GitHub Actions 还是其他？Docker Swarm 在 CI 里怎么启动的？
3. **可接受的 CI 时长**：目前 2 个 ST 跑多久？能接受到多长？

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

| 选项 | 优势 | 劣势 | 适合 |
|------|------|------|------|
| **Claude Code Web** | 即开即用，已有 | session 时长受限 | Planner, 轻量 Evaluator |
| **Claude Code CLI + Agent SDK** | 灵活，可编程 | 需要自己编排 | 全部，如果有长运行环境 |
| **Cursor Cloud Agent** | 长运行，worker 池 | 需要部署 k8s operator | Generator + Evaluator |
| **混合** | 各取所长 | 复杂度高 | 最终目标 |

**建议路径**：先用 Claude Code Web/CLI 验证 Evaluator Agent（里程碑 2 已有工具），再评估是否需要 Cursor Cloud Agent 的长运行能力。

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

8. **Agent 运行环境偏好**：
   - 你们团队现在用 Claude Code 还是 Cursor？还是都用？
   - 有没有可以长时间运行 agent 的机器/环境？
   - 对 Cursor Cloud Agent 的 self-hosted 部署有兴趣吗？需要多大规模？
9. **编排偏好**：
   - 希望 Agent 全自动（push 触发 → 自动跑完 → 输出 PR review），还是半自动（人在环中确认关键步骤）？
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
