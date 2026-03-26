# Vespa System Testing: Harness Engineering Plan

## 目标

基于 Vespa 系统测试框架，构建自动化 harness engineering 体系，提升搜索引擎开发的自动化水平。

---

## 第一阶段：精选 System Test 子集 + 本地快速反馈

### 问题

795 个系统测试全跑不现实（每个测试需部署完整 Vespa 集群），需要建立分层测试策略。

### 方案

#### 1.1 测试分层

```
┌─────────────────────────────────────────┐
│  Level 1: Schema & Config Validation    │  秒级  ← 每次commit
│  (vespa-lint, schema编译, services.xml) │
├─────────────────────────────────────────┤
│  Level 2: 核心功能 System Test 子集     │  分钟级 ← 每次PR
│  (feeding, basic search, ranking)       │
├─────────────────────────────────────────┤
│  Level 3: 完整 System Test             │  小时级 ← nightly/release
│  (全部795个测试)                        │
├─────────────────────────────────────────┤
│  Level 4: Staging Test                  │  升级验证 ← 版本升级前
│  (旧版本→新版本的upgrade path)          │
└─────────────────────────────────────────┘
```

#### 1.2 精选 Level 2 核心测试子集

从现有 tests/ 中挑选对搜索引擎开发最关键的测试：

```
# 必选：基础功能
tests/search/basicsearch/          # 基础搜索
tests/search/feedandget/           # 灌数据和获取
tests/search/ranking/              # 排序
tests/search/queryprofiles/        # 查询配置

# 必选：数据一致性
tests/vds/visitorsaliency/         # 数据访问
tests/config/deploy/               # 部署验证

# 按需：你的业务场景
tests/search/nearest_neighbor/     # 如果用向量搜索
tests/search/struct_and_map_types/ # 如果用复杂类型
tests/container/                   # 如果有自定义container组件
```

#### 1.3 轻量化运行脚本

创建一个精简的测试运行入口，绕过 Docker Swarm 全量编排：

```bash
# 只跑核心子集，单节点模式
bin/run-tests-on-swarm.sh \
  --file tests/search/basicsearch/basicsearch.rb \
  --consoleoutput \
  --nodelimit 1
```

---

## 第二阶段：多 Agent Harness 架构

基于 Anthropic 博客的 Planner-Generator-Evaluator 模式，适配搜索引擎开发场景。

### 2.1 架构设计

```
┌──────────────┐     spec.md      ┌──────────────┐
│              │ ───────────────→  │              │
│   Planner    │                   │  Generator   │
│   Agent      │  ← feedback ───  │  Agent       │
│              │                   │              │
└──────────────┘                   └──────┬───────┘
                                          │
                                    code changes
                                          │
                                          ▼
                                   ┌──────────────┐
                                   │  Evaluator   │
                                   │  Agent       │
                                   │              │
                                   │ - Schema验证  │
                                   │ - ST子集运行  │
                                   │ - 性能基准    │
                                   └──────────────┘
```

### 2.2 三个 Agent 的职责

#### Planner Agent
- 输入：用户简短需求（如"给商品搜索加同义词扩展"）
- 输出：详细 spec，包括：
  - 需要修改的 schema (.sd) 文件
  - services.xml 变更
  - 预期的搜索行为变化
  - 需要验证的测试场景
- 工具：读 codebase、读 Vespa 文档

#### Generator Agent
- 输入：Planner 的 spec
- 输出：代码变更（schema、配置、Java 组件）
- 工作模式：分 sprint 迭代
  - Sprint 1: Schema 变更 + 基础验证
  - Sprint 2: 排序/查询逻辑
  - Sprint 3: 性能调优
- 自检：`mvn verify -DskipTests`（编译通过）

#### Evaluator Agent（关键创新点）
- **不能自我评估**，必须独立运行
- 评估手段：
  1. Schema 编译检查（秒级）
  2. 核心 System Test 子集（分钟级）
  3. 自定义验收查询（灌测试数据 → 执行查询 → 验证结果）
  4. 性能基准对比（如果涉及排序/索引变更）
- 输出：通过/失败 + 具体失败原因
- 关键：用 hard threshold，不给 Agent "说服自己没问题" 的机会

### 2.3 Sprint Contract（核心机制）

每个 sprint 开始前，Generator 和 Evaluator 协商验收标准：

```yaml
sprint: 1
goal: "添加同义词扩展到商品搜索"
acceptance_criteria:
  - schema_compiles: true
  - system_test_pass:
      - tests/search/basicsearch/basicsearch.rb
  - custom_query_test:
      query: "手机"
      expected_hits_include: ["mobile phone", "cellphone"]
      min_recall: 0.8
```

---

## 第三阶段：Cursor Self-Hosted Cloud Agent 作为执行层

### 3.1 为什么适合

| 需求 | Cursor Cloud Agent 能力 |
|------|------------------------|
| 代码不能离开内网 | Worker 在你的基础设施内运行 |
| 需要运行 Vespa 集群 | Worker 可以访问内网 Docker/K8s |
| 长时间运行（ST 慢） | 每个 session 独占 worker |
| 多 Agent 并行 | K8s operator 支持 worker 池 |
| 可扩展性 | Helm chart + WorkerDeployment |

### 3.2 部署架构

```
┌─ Your Infrastructure ──────────────────────────────┐
│                                                     │
│  ┌─────────────┐   ┌─────────────┐                 │
│  │ Worker Pool  │   │ Vespa Test  │                 │
│  │ (K8s)       │   │ Cluster     │                 │
│  │             │   │ (Docker     │                 │
│  │ - Planner   │──→│  Swarm)     │                 │
│  │ - Generator │   │             │                 │
│  │ - Evaluator │   └─────────────┘                 │
│  └──────┬──────┘                                    │
│         │ HTTPS (outbound only)                     │
└─────────┼───────────────────────────────────────────┘
          │
          ▼
   ┌──────────────┐
   │ Cursor Cloud  │
   │ (Inference)   │
   └──────────────┘
```

### 3.3 实施步骤

```
Step 1: 部署 Cursor Worker
  - Helm chart 安装到现有 K8s 集群
  - 配置 WorkerDeployment（建议初始 3-5 个 worker）
  - 网络策略：worker 可访问 Docker Swarm 网络

Step 2: 配置 MCP Server
  - Vespa CLI MCP：schema验证、部署、查询
  - Docker/K8s MCP：管理测试集群生命周期
  - Git MCP：代码提交、分支管理

Step 3: 定义 Agent Skills
  - /vespa-schema-check：编译验证
  - /vespa-st-quick：跑核心 ST 子集
  - /vespa-st-full：跑全量 ST
  - /vespa-perf-bench：性能基准

Step 4: 编排 Harness
  - Planner → Generator → Evaluator 流水线
  - Sprint contract YAML 模板
  - 失败自动回滚机制
```

### 3.4 与 Claude Code 的对比/互补

| 维度 | Claude Code (CLI/Web) | Cursor Cloud Agent |
|------|----------------------|-------------------|
| 适合场景 | 交互式开发、PR review | 长时间自动化任务 |
| 运行时长 | 受 session 限制 | 长时间运行 |
| 基础设施访问 | 本地终端 | 内网 K8s 集群 |
| 多 Agent | 通过 Agent SDK | 原生 worker pool |
| 建议用法 | Planner + 日常开发 | Generator + Evaluator |

**推荐组合**：Claude Code 做 Planner（交互式对齐需求），Cursor Cloud Agent 做 Generator + Evaluator（长时间运行 ST）。

---

## 快速启动：第一周行动项

1. **[ ] 识别你的核心 ST 子集**
   - 列出你们搜索引擎实际用到的 Vespa 功能
   - 从 tests/ 中映射对应的测试文件
   - 目标：10-20 个核心测试，跑完 < 30 分钟

2. **[ ] 搭建单节点快速测试环境**
   - 基于 `docker/Dockerfile.systemtest` 构建镜像
   - 写一个 `run-core-st.sh` 只跑核心子集
   - 集成到 CI（GitHub Actions 或你们的 CI）

3. **[ ] 原型化 Evaluator Agent**
   - 用 Claude Code 的 Agent SDK 或 Cursor 的 subagent
   - 让 Evaluator 执行：schema 编译 → 核心 ST → 报告
   - 这是 harness engineering 的第一个 "非自我评估" 组件

4. **[ ] 评估 Cursor Cloud Agent**
   - 申请 self-hosted cloud agent 权限
   - 在 staging K8s 部署 1 个 worker
   - 验证 worker 能否访问 Vespa 测试集群

---

## 成本估算

基于 Anthropic 博客的数据点：
- 单次简单任务（单 Agent）：~$9, 20 分钟
- 完整 harness（多 Agent 多 sprint）：~$200, 6 小时
- 你的场景（搜索功能迭代）：预估每个 feature $50-150，取决于 ST 复杂度

关键省钱策略：
- Level 1/2 用便宜模型（Haiku/Sonnet）
- 只在 Evaluator 和关键决策用 Opus
- ST 子集精选，避免全量运行
