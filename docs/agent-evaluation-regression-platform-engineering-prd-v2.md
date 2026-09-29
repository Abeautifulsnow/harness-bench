# Agent Evaluation & Regression Platform PRD

> 文档版本：V2.0.1（2026-09-23 复核同步修订，记录见 Spec V2.1.1 §10.1）  
> 状态：Engineering Ready  
> 面向对象：Agent Platform / Harness / AI Infra 团队  
> 核心语言：Python 3.12  
> 通用评测引擎：DeepEval  
> Agent 接入协议：HTTP / SSE（V1），预留 ACP / CLI / stdio  
> 默认存储：DuckDB + JSONL  
> UI：V1 CLI + HTML，V1.5 React + TypeScript  
> 产品目标：构建覆盖 Benchmark、Experiment、Evaluation、Regression、Failure Intelligence、Security 与 Quality Gate 的 Agent 工程质量平台

---

# 1. 产品定义

## 1.1 产品名称

**Agent Evaluation & Regression Platform**

内部简称：`Agent Eval Platform`  
CLI 名称：`agent-eval`

## 1.2 一句话定位

Agent Eval Platform 是一套面向自研 Agent 平台研发全过程的：

```text
Benchmark
+
Experiment
+
Evaluation
+
Regression
+
Failure Intelligence
+
Quality Management
```

平台。

它不是简单的 LLM 回答评分器，而是：

> **pytest + benchmark + experiment lab + regression dashboard for Agent Harness**

## 1.3 核心问题

系统持续回答：

> **“这次 Agent 的代码、模型、Prompt、Tool、Skill、MCP、Memory 或 Harness 修改后，到底变好了什么，又退化了什么？”**

除最终结果外，还需回答：

- Task 是否真正完成
- Tool 是否选择正确
- Tool Arguments 是否正确
- 执行步骤是否冗余
- 是否发生循环调用
- Error Recovery 是否合理
- MCP / Tool 权限是否被遵循
- Skill 路由是否正确
- SubAgent 调度是否正确
- Context Compression 是否丢失关键事实
- Memory 是否错误干扰当前任务
- Token / Cost / Latency 是否回归
- 稳定性是否下降
- 哪些失败是新 Regression
- 哪些旧问题被修复
- 哪种模型 / Prompt / Harness 配置最适合当前 Benchmark

---

# 2. 项目目标

平台需要覆盖完整 Agent 研发闭环：

```text
开发
 ↓
本地 Eval
 ↓
PR Smoke Eval
 ↓
Experiment
 ↓
Regression Compare
 ↓
CI Quality Gate
 ↓
Release Eval
 ↓
Production Trace
 ↓
Failure Promotion
 ↓
Regression Dataset
```

核心理念：

```text
问题发生一次
→ 变成 Eval Case
→ 永久进入 Regression Suite
→ 防止再次出现
```

---

# 3. 产品范围

## 3.1 V2 一级模块

```text
1. Benchmarks
2. Experiments
3. Runs
4. Evaluations
5. Regression
6. Failure Analysis
7. Quality & Security
8. Administration
```

## 3.2 非目标

首个可用版本暂不要求：

- Kubernetes 分布式执行集群
- 多租户 SaaS
- 在线计费系统
- RLHF 数据平台
- 自动 Agent 训练
- 大规模 Human Annotation Platform
- 强依赖 ACP
- 自研 LLM Judge 框架替代 DeepEval
- **代管被测平台的容器化与数据播种**（外部接入修订，2026-09-29）：平台提供
  fixture workdir 透传与清理（§7.2 的 `metadata.workdir`），不为外部 SUT 构建
  隔离环境、不重置其数据库、不管理其部署形态。被测方的基础设施准备是接入方
  （转译 shim / 运维）的义务——边界不说清会变成无限责任。

---

# 4. DeepEval 的产品定位

DeepEval 在本平台中作为：

> **通用 Evaluation SDK / Metric Engine**

而非整个产品。

平台自身负责：

- Agent Adapter
- Benchmark / Suite / Case
- Dataset Versioning
- Environment / Fixture
- Raw Trace / Span Tree
- Harness-specific Evaluation
- Experiment Matrix
- Baseline / Candidate
- Regression
- Failure Classification / Clustering
- Human Review
- Security / Red Team
- Quality Gate
- CI / Release Gate
- Historical Results
- Report / Dashboard

DeepEval 负责通用语义评测，例如：

- Task Completion
- Step Efficiency
- Tool Correctness
- Argument Correctness
- Plan Quality
- Plan Adherence
- Answer Relevancy
- GEval
- RAG Metrics
- Conversation Metrics

系统不得把自身数据模型与 DeepEval SDK 类型直接绑定。

---

# 5. 总体架构

```text
                         ┌─────────────────────┐
                         │    CLI / Web UI     │
                         └──────────┬──────────┘
                                    │
                         ┌──────────▼──────────┐
                         │ Eval Orchestrator   │
                         └──────────┬──────────┘
                                    │
             ┌──────────────────────┼──────────────────────┐
             ▼                      ▼                      ▼
      Benchmark Engine       Experiment Engine       Scheduler
             │                      │                      │
             └──────────────────────┼──────────────────────┘
                                    ▼
                             Case Execution
                                    │
                             Agent Adapter
                                    │
                           HTTP / SSE / ACP
                                    │
                                    ▼
                              Agent Runtime
                                    │
                                    ▼
                               Raw Events
                                    │
                              Trace Builder
                                    │
                      Event Log + Span Tree
                                    │
             ┌──────────────────────┼──────────────────────┐
             ▼                      ▼                      ▼
      Native Evaluator        DeepEval Engine      Harness Evaluator
             │                      │                      │
             └──────────────────────┼──────────────────────┘
                                    ▼
                              Result Aggregator
                                    │
              ┌─────────────────────┼──────────────────────┐
              ▼                     ▼                      ▼
       Regression Engine     Failure Analyzer       Quality Gate
              │                     │                      │
              └─────────────────────┼──────────────────────┘
                                    ▼
                         DuckDB / JSONL / Artifact
```

---

# 6. Agent 接入层

## 6.1 V1 接入协议

当前自研 Agent 客户端已有 HTTP Service，因此 V1 使用：

```text
HTTP + SSE
```

ACP 不是 Eval 的前置条件。

## 6.2 Adapter 接口

```python
class AgentAdapter(ABC):

    @abstractmethod
    async def health_check(self) -> HealthStatus:
        ...

    @abstractmethod
    async def create_session(
        self,
        context: SessionContext,
    ) -> AgentSession:
        ...

    @abstractmethod
    async def run(
        self,
        session: AgentSession,
        request: AgentRequest,
    ) -> AsyncIterator[AgentEvent]:
        ...

    @abstractmethod
    async def cancel(
        self,
        session: AgentSession,
    ) -> None:
        ...
```

### 6.2.1 Adapter 的四项义务（外部接入修订，2026-09-29）

接口只有四个方法，但**每个 adapter 实现都隐含承担四项协议义务**。把它们写明，
否则会被当成"实现细节"各行其是——而它们是"框架保持通用"的唯一保障：

1. **能力声明**：SUT 支持哪些观测面（PRD §8 事件），必须在 `health_check` 阶段
   整份上报（`HealthStatus.observation_surface`，事件名 → bool）。能力是 run 级
   事实——放 `create_session` 意味着每个 case 各报一次、会话失败的 case 干脆
   没有声明。声明表是外部接入侧的义务，框架**不得**从"事件没出现"反推
   "观测不到"（两者在数据上同形）。
2. **事件归一化**：平台方言必须在接入侧转译成 §8 的固定词汇，不得透传。
   未在词汇表内的事件类型按 §8 的校验后果处置（默认 run 级 warning，
   strict 档 exit 2）。
3. **环境回执**：收到 `create_session` 的 `metadata.workdir` 后，SUT 必须
   探测其可达性并经**响应侧字段**（`workdir_accessible`）回执。回执缺失按
   "未知"处理并记 warning——不得默认视为可达：SUT 拿到访问不了的路径会静默
   退化为"环境轴不存在"，那是最难排查的假信号。注意回执只能走响应体：
   `AgentSession.metadata` 是请求回显，SUT 写不进去。
4. **用量口径上报**：SUT 必须让框架知道"哪些用量分量被观测到了"
   （`RunMetadata.token_usage_scope`：full / partial / 未观测）。单侧观测的
   run 与全量观测的 run 之间，token 类数值不可比——口径不明的基线会被
   模型漂移同类的"口径漂移"静默污染（Spec §4.3）。

## 6.3 未来 Adapter

```text
HttpAgentAdapter
ACPAgentAdapter
CLIAgentAdapter
StdioAgentAdapter
PythonAgentAdapter
RemoteAgentAdapter
```

**外部自研平台接入是已定型形态（外部接入修订，2026-09-29）**：上列 6 个名字
是未来工作，不代表"外部平台必须先等其中一个实现"。外部自研平台（自有 HTTP
服务、事件流与本平台 §8 词汇不同构）的定型接入方式是：

```text
HTTP(S) 直连 + 接入侧转译 shim
```

shim 是**测试侧组件**：对上实现本平台的 `/health`、`/api/agent/sessions`、
`/run`、`/cancel` 四端点契约，对下调用被测平台的原生 API，并把平台方言
归一化成 §8 词汇。它不进被测平台的生产代码路径、不进本框架——每个异构 SUT
各有一份 shim，这份成本是真实的，不应伪装成零。指南见
`docs/external-agent-integration-guide.md`。

---

# 7. HTTP Contract

## 7.1 Health

```http
GET /health
```

```json
{
  "status": "ok"
}
```

扩展字段（外部接入修订，2026-09-29；均**可选**，缺省按"未声明"处理）：

```json
{
  "status": "ok",
  "observation_surface": {
    "retry": false,
    "tool.call": true,
    "context.compaction.started": false
  },
  "agent_model": "sut-effective-model-id"
}
```

- `observation_surface`：观测面能力表（PRD §8 事件名 → bool）。`false` = 该事件
  本平台**从不发出**，依赖它的 metric 一律判 skipped（`observation_unavailable`），
  绝不拿"0 次观测"当"满足"。键不在表内 = 未声明 = 框架按"具备"处理。
  这是 run 级事实，必须在 health 阶段整份上报（见 §6.2.1 义务 1）。
- `agent_model`：SUT 自报的**实际生效**模型标识。它权威于 CLI `--model` 标签，
  落入 `RunMetadata.agent_model` 并参与基线守卫：两侧不一致的比较判 INVALID
  （Spec §4.3）——模型漂移不得被静默归因为"回归"。

## 7.2 Create Session

```http
POST /api/agent/sessions
```

```json
{
  "metadata": {
    "eval_run_id": "run_xxx",
    "case_id": "case_xxx",
    "variant_id": "variant_xxx"
  }
}
```

**metadata 允许携带扩展键**（外部接入修订，2026-09-29）。四个平台键之外，
接入方自定义的键（如 `workdir`）原样透传，框架不解释其语义：

- `metadata.workdir`（扩展键语义约定）：本 case 的 fixture 沙箱目录。SUT 应把
  它当作本次会话的工作区根——文件操作、产物输出都应落在这个目录内，且
  **不得**越出该目录（红队用例的安全性依赖这一点：agent 拿到的路径若不是
  一次性沙箱，`rm` 打中的就是真实工程目录）。workdir 属**会话级**而非轮级，
  每个 iteration 独立。

**响应体**：

```json
{
  "session_id": "sess_xxx",
  "workdir_accessible": true
}
```

- `session_id`：必填。
- `workdir_accessible`（可选回执，外部接入修订）：SUT 对 `metadata.workdir`
  可达/可写性的探测回执。`false` → 框架按 `InfraError`（exit 2）终止该 case
  ——把"配置错"（如容器路径与宿主不共享）与"agent 失败"分开；缺省/非布尔
  → 按"未知"处理并记 run 级 warning，**不得默认视为可达**。回执走**响应体**：
  `AgentSession.metadata` 是请求载荷的回显，SUT 无法写它。

## 7.3 Run

```http
POST /api/agent/sessions/{session_id}/run
```

```json
{
  "message": "查询销售额最高的 5 个客户",
  "stream": true
}
```

推荐返回 SSE Event Stream。

---

# 8. Agent Event Protocol

统一事件类型：

```text
run.started
run.finished
agent.started
agent.finished
model.request
model.response
tool.call
tool.result
mcp.call
mcp.result
skill.discovered
skill.loaded
retriever.call
retriever.result
subagent.started
subagent.finished
memory.query
memory.result
context.compaction.started
context.compaction.finished
plan.created
plan.updated
file.read
file.write
command.started
command.finished
error
retry
interrupt
cancel
```

**本词汇表是闭合的（外部接入修订，2026-09-29）**：它是框架消费侧的固定契约，
新增事件类型必须走**框架升级**（在此清单登记），不得由接入方自行扩展——
接入方的平台方言必须在转译层归一化为上述词汇（§6.2.1 义务 2）。把平台私有
事件名塞进事件流会让依赖对应观测面的规则静默失效（例如把 `mcp.call` 写成
`mcp_call`，`mcp` span 恒空，MCP 越权规则恒 pass——一个不会自己暴露的缺陷）。

校验后果（事件 `type` 不在清单内）：

```text
默认：产出 run 级计数（RunMetadata.protocol_violations：类型 → 次数），
      并写入 aggregate.warnings——可见但不阻断，不改变任何 verdict 与 exit code
升级：strict_protocol 打开时（CLI --strict-protocol 或 profile 声明），
      判 InfraError → exit 2
```

默认 warn 而非 strict 的理由：词汇表会演进（`context.compaction.*` 就是本项目
自己新加的），默认 strict 会把"框架该升级词汇表"错判成"接入方违约"；升级开关
交给愿意承担红线的团队（收尾档 nightly/strict 常开，PR 档保持 warn）。
框架实现上 `type` 保持 `str` 而非 `Literal`，`EVENT_TYPES` 是**声明式清单**，
新增事件类型 = 改这一处，路径必须保留。

事件最小结构：

```json
{
  "event_id": "evt_xxx",
  "trace_id": "trace_xxx",
  "parent_span_id": "span_xxx",
  "type": "tool.call",
  "timestamp": "2026-09-23T11:00:00+08:00",
  "data": {}
}
```

---

# 9. Trace Builder

Raw Event 不直接用于评测。

必须经过：

```text
TraceBuilder
```

生成：

```text
Event Log
+
Span Tree
+
Derived Metrics
```

示例：

```text
AgentRun
├── ModelCall
├── ToolCall: database_schema
│   └── ToolResult
├── ModelCall
├── ToolCall: execute_sql
│   └── ToolResult
└── FinalOutput
```

---

# 10. Trace Span Model

```python
class TraceSpan(BaseModel):
    id: str
    trace_id: str
    parent_span_id: str | None

    type: str
    name: str

    started_at: datetime
    finished_at: datetime | None

    input: Any | None
    output: Any | None

    attributes: dict[str, Any]

    status: str
    error: str | None
```

Span 类型：

```text
agent
llm
tool
retriever
mcp
skill
subagent
memory
command
workflow
```

Raw Trace 永久保存，不能只保存评分结果。

---

# 11. Benchmarks 模块

Benchmark 定义“测什么”。

模型：

```text
Benchmark
 ├── Dataset Version
 ├── Suite
 │   ├── Case
 │   ├── Case
 │   └── Case
 └── Metric Profile
```

Benchmark 页面需要提供：

- 名称
- 描述
- Owner
- Dataset Version
- Case 数量
- Suite
- Tag 分布
- 最近一次 Run
- 最近 Pass Rate
- 最近 Regression 数量

---

# 12. Dataset

支持：

```text
single-turn
multi-turn
agent-task
coding-task
database-task
security-task
```

平台自己的 Dataset 是 Source of Truth。

DeepEval Dataset 仅作为运行时适配输出。

---

# 13. Dataset Versioning

Dataset 必须版本化：

```text
database-core@1.0.0
database-core@1.1.0
harness-regression@2.3.0
```

每次 Run 强制绑定：

```text
dataset_id
dataset_version
dataset_hash
```

Baseline 与 Candidate 比较时必须使用相同 Dataset Version。

---

# 14. Eval Case

```yaml
id: database.query.top_customers
version: 3

name: 查询销售额最高客户

tags:
  - database
  - postgres
  - tool-use
  - smoke

difficulty: medium

input:
  prompt: |
    查询 2026 年 8 月销售额最高的 5 个客户。

environment:
  fixture: sales_v2
  database: postgres

execution:
  timeout: 60
  repeat: 3

expected:
  status: success

  tools:
    required:
      - database_schema
      - execute_sql

    forbidden:
      - shell_exec

  constraints:
    max_tool_calls: 10
    max_latency_ms: 30000

evaluation_profile: database_default
```

---

# 15. Case 生命周期

```text
Draft
 ↓
Reviewed
 ↓
Active
 ↓
Deprecated
```

Case 修改会产生新 Version，不覆盖历史版本。

---

# 16. Case 来源

```text
manual
production
issue
bug
synthetic
red_team
benchmark_import
```

示例：

```yaml
source:
  type: issue
  reference: ISSUE-9363
```

---

# 17. Golden Set

Golden Set 是稳定、关键、必须通过的核心 Case。

要求：

- 修改必须 Review
- Release Gate 必须运行
- 默认不允许自动删除
- Hard Failure 默认阻断 Release

---

# 18. Regression Set

历史真实 Bug 修复后进入 Regression Set。

流程：

```text
Bug / Production Failure
        ↓
Promote to Benchmark
        ↓
Case Draft
        ↓
Human Review
        ↓
Regression Suite
```

---

# 19. Challenge Set

用于测试能力上限：

- 长链路任务
- 模糊意图
- 多 Tool 协作
- Tool Error
- 上下文冲突
- 超长上下文
- 多 SubAgent

默认不作为 PR Hard Gate。

---

# 20. Security Set

覆盖：

- MCP 权限
- Tool 权限
- Prompt Injection
- Tool Injection
- 恶意 Tool Description
- 越权操作
- Filesystem Boundary
- Secret Leakage
- Database Write
- Dangerous Command

---

# 21. Suite

预置：

```text
smoke
core
regression
nightly
release
security
red_team
model_benchmark
prompt_benchmark
```

Suite 可以通过 Tag 或显式 Case ID 定义。

---

# 22. Experiments 模块

Experiment 用于回答：

> **哪个 Agent 配置更好？**

一个 Experiment 包含：

```text
Benchmark
+
Variants
+
Metric Profile
+
Comparison Policy
```

---

# 23. Variant

```yaml
name: model-a-prompt-v13-strict

agent:
  version: 0.8.0

model:
  provider: openai-compatible
  name: model-a

prompt:
  version: v13

tools:
  policy: strict

skills:
  version: 4
```

---

# 24. Experiment Matrix

```yaml
matrix:
  model:
    - model-a
    - model-b
    - model-c

  prompt:
    - v12
    - v13

  tool_policy:
    - strict
    - auto
```

自动生成：

```text
3 × 2 × 2 = 12 Variants
```

---

# 25. Experiment 维度

支持比较：

- Agent Version
- Model
- Prompt
- System Prompt
- Planner Prompt
- Tool Description
- Tool Policy
- Tool Set
- Skill Set
- Skill Version
- MCP Set
- Memory Strategy
- Context Strategy
- Compression Strategy
- Reasoning Effort
- Temperature

---

# 26. Model Benchmark

同 Harness、同 Dataset 下比较不同模型：

```text
Task Success
Task Completion
Tool Correctness
Step Efficiency
Latency
Tokens
Cost
Stability
```

---

# 27. Prompt Benchmark

支持：

```text
System Prompt A/B
Planner Prompt A/B
Tool Description A/B
Router Prompt A/B
```

---

# 28. Harness Benchmark

比较：

```text
Harness v1
Harness v2
```

重点观察：

- Agent Loop
- Context
- Memory
- Tool Router
- Skill Loader
- SubAgent
- Retry
- Compression

---

# 29. Runs 模块

Run 状态：

```text
queued
running
completed
partial
failed
cancelled
```

Run 支持：

- 单 Case
- Suite
- Benchmark
- Experiment Variant
- Matrix Experiment

---

# 30. Run Metadata

必须记录：

```text
run_id
benchmark_id
dataset_version
suite
variant
agent_version
git_commit
git_branch
git_dirty
agent_model
agent_model_config
judge_model
judge_model_config
deepeval_version
eval_platform_version
started_at
finished_at
environment
```

---

# 31. Repeat & Stability

支持：

```text
repeat=N
```

输出：

```text
success_rate
pass@1
pass@3
pass@5
variance
trajectory_variance
tool_choice_variance
latency_variance
token_variance
```

命名与判定口径以 Spec V2.1 §3.2 为准：`success_rate ≡ pass_rate`、`tool_choice_variance ≡ tool_sequence_variance`；pass@k 仅当 repeat ≥ k 时输出。

---

# 32. Flaky Detection

例如：

```text
PASS
FAIL
PASS
FAIL
PASS
```

标记为：

```text
FLAKY
```

Flaky Case 默认不应直接阻断普通 PR，但必须在报告中显著展示。

---

# 33. Evaluation 架构

```text
                    Evaluation
                        │
       ┌────────────────┼────────────────┐
       ▼                ▼                ▼
 Native Eval       DeepEval Eval    Harness Eval
```

---

# 34. Native Evaluator

确定性规则优先。

支持：

```text
exact match
contains
regex
json schema
sql result
database state
file state
git diff
pytest
build
lint
exit code
required tool
forbidden tool
tool call limit
permission
latency
token
cost
```

---

# 35. DeepEval Evaluator

用于：

```text
semantic quality
trajectory quality
tool semantic correctness
argument semantic correctness
planning
conversation
RAG
```

封装的 Agent Metrics：

```text
TaskCompletion
StepEfficiency
ToolCorrectness
ArgumentCorrectness
PlanQuality
PlanAdherence
```

---

# 36. DeepEval Adapter

所有 SDK 依赖集中到：

```text
evaluators/deepeval/adapter.py
```

负责：

```text
Internal EvalCase
+
AgentRun
+
Trace
     ↓
DeepEval TestCase / Trace
     ↓
Metric Execution
     ↓
Platform MetricResult
```

DeepEval 升级时只需要修改 Adapter 层。

---

# 37. DeepEval Test Case Mapping

平台字段到 DeepEval：

```text
input            ← case.input.prompt
actual_output    ← run.final_output
expected_output  ← case.expected.output
context          ← case.context
retrieval_context← trace retriever result
tools_called     ← trace tool calls
expected_tools   ← case expected tools
completion_time  ← run latency
token_cost       ← calculated cost
```

Multi-turn Case 映射到 Conversation Test Case。

---

# 38. Metric Profile

Metric Profile 决定：

- 跑哪些 Evaluator
- Threshold
- Blocking / Non-blocking
- Judge Model
- Timeout
- Retry

---

# 39. Smoke Profile

```yaml
name: smoke

native:
  enabled: true

deepeval:
  task_completion:
    enabled: true
    threshold: 0.70
    blocking: false

  tool_correctness:
    enabled: true
    threshold: 0.80
    blocking: false
```

目标：速度优先。

---

# 40. Nightly Profile

启用：

```text
TaskCompletion
StepEfficiency
ToolCorrectness
ArgumentCorrectness
PlanQuality
PlanAdherence
GEval
```

---

# 41. Release Profile

必须执行：

```text
Golden Set
Regression Set
Security Set
Core Business Set
```

安全和 deterministic failure 作为 Hard Gate。

---

# 42. Custom GEval

```yaml
metric:
  type: geval
  name: database_answer_quality
  criteria: |
    Agent 必须准确解释数据库返回结果。
    不得杜撰查询结果中不存在的数据。
```

支持版本化：

```text
criteria_version
judge_model
```

---

# 43. Evaluator Plugin SDK

```python
class EvaluatorPlugin(ABC):
    name: str

    @abstractmethod
    async def evaluate(
        self,
        context: EvaluationContext,
    ) -> MetricResult:
        ...
```

新增 Evaluator 不得修改 Runner 主流程。

---

# 44. Harness-specific Evaluators

首批：

```text
SkillPriorityEvaluator
SkillLoadEvaluator
MCPPermissionEvaluator
MCPFallbackEvaluator
ContextCompressionEvaluator
CompressionRetentionEvaluator
MemoryRetrievalEvaluator
MemoryConflictEvaluator
SubAgentRoutingEvaluator
SubAgentRecoveryEvaluator
LoopEvaluator
RetryEvaluator
InterruptEvaluator
ForkEvaluator
```

---

# 45. MetricResult

```python
class MetricResult(BaseModel):
    metric: str
    evaluator: str

    score: float | None
    threshold: float | None

    verdict: str
    blocking: bool

    reason: str | None
    metadata: dict
```

Verdict：

```text
pass
fail
warning
error
skipped
```

---

# 46. Failure Semantics

必须区分：

```text
AGENT_FAILURE
EVALUATION_FAILURE
INFRA_FAILURE
```

Judge Provider 超时不能自动等价为 Agent FAIL。

---

# 47. Failure Taxonomy

一级：

```text
AGENT
MODEL
TOOL
MCP
SKILL
MEMORY
CONTEXT
SUBAGENT
ENVIRONMENT
EVALUATOR
SECURITY
```

二级示例：

```text
tool.selection
tool.argument
tool.timeout
tool.result
agent.loop
agent.incomplete
agent.redundant_steps
mcp.permission
mcp.unavailable
skill.not_found
skill.wrong_priority
context.lost_information
memory.conflict
subagent.wrong_route
```

---

# 48. Failure Classification

优先：

```text
Rule-based Classification
```

无法确定时再使用：

```text
LLM Failure Classifier
```

分类必须保存 Reason 和 Evidence。

---

# 49. Failure Clustering

输入：

- Failure Category
- Failure Reason
- Trace Summary
- Tool Sequence
- Error Message
- Metric Reasons

输出：

```text
Cluster A
Tool argument error
37 cases

Cluster B
Repeated schema lookup
24 cases

Cluster C
MCP permission handling
11 cases
```

---

# 50. Failure Cluster Detail

展示：

- Cluster Summary
- Case Count
- Affected Versions
- Common Tool Sequence
- Common Error
- Representative Cases
- First Seen
- Latest Seen
- Regression Ratio

---

# 51. Promote to Benchmark

任何 Case Run 支持：

```text
Promote to Benchmark
```

自动生成 Case Draft：

- 原始 Input
- Environment
- Fixture Reference
- Tool Expectations
- Failure Category
- Trace Reference
- Suggested Assertions

人工 Review 后加入 Regression Suite。

---

# 52. Production Trace Replay

未来支持：

```text
Production Trace
       ↓
Sanitize / Redact
       ↓
Generate Eval Case
       ↓
Replay Candidate
       ↓
Evaluate
```

用途：真实用户问题回归。

---

# 53. Regression 模块

支持：

```text
Baseline vs Candidate
```

比较层级：

```text
case-level
suite-level
metric-level
failure-cluster-level
```

---

# 54. Case Regression State

```text
PASSED
FAILED
REGRESSION
IMPROVED
UNCHANGED
FLAKY
INVALID
```

判定：

```text
Baseline PASS + Candidate FAIL → REGRESSION
Baseline FAIL + Candidate PASS → IMPROVED
```

---

# 55. Performance Regression

即使 Candidate 仍 PASS，也可能发生：

```text
Tool Calls 4 → 10
Tokens 15K → 28K
Latency 5s → 11s
```

此时标记：

```text
PERFORMANCE_REGRESSION
```

---

# 56. Trace Diff

对比：

- Tool Sequence
- Tool Arguments
- Tool Results
- Model Call Count
- SubAgent Calls
- Errors
- Retries
- Token
- Latency
- Final Answer

示例：

```text
Baseline:
schema
execute_sql

Candidate:
schema
schema        + ADDED
execute_sql
execute_sql   + ADDED
```

---

# 57. Semantic Trace Diff

Tool Arguments 支持：

```text
structural diff
semantic diff
```

SQL 建议使用 SQLGlot 增加：

```text
AST Diff
```

文件修改支持：

```text
git diff
```

---

# 58. Historical Trend

按：

```text
commit
version
date
model
prompt
```

展示：

```text
Task Success
Task Completion
Step Efficiency
Tool Correctness
Tool Calls
Tokens
Latency
Cost
Failure Count
```

---

# 59. Cost Evaluation

记录：

- Input Tokens
- Output Tokens
- Cache Tokens
- Agent Model Cost
- Judge Cost
- Tool Cost
- Total Cost

输出：

```text
avg_cost_per_case
cost_per_success
suite_cost
experiment_cost
judge_cost_ratio
```

---

# 60. Human Review

支持人工 Verdict：

```text
PASS
FAIL
EXPECTED
FALSE_POSITIVE
FALSE_NEGATIVE
```

人工结论不能覆盖机器结果，必须并存：

```text
machine_verdict
human_verdict
```

---

# 61. Review Queue

以下情况自动进入：

- DeepEval 分数接近阈值
- Native 与 Semantic Evaluator 冲突
- Regression
- Security Failure
- Flaky Case
- Evaluation Failure

---

# 62. Red Team Suite

覆盖：

```text
Prompt Injection
Tool Injection
Permission Escalation
Data Exfiltration
Secret Access
Dangerous Commands
Unsafe DB Write
Malicious MCP
```

---

# 63. Security Evaluator

安全规则必须优先 deterministic：

```text
Forbidden Tool
Forbidden Path
Forbidden Command
Forbidden SQL
Forbidden MCP
Permission Override
Secret Access
```

任何安全 Hard Failure 默认禁止被 LLM Judge 覆盖。

---

# 64. Quality Gate

支持：

```text
PR Gate
Main Gate
Release Gate
```

---

# 65. PR Gate

建议：

```text
20~50 Cases
repeat=1
```

目标：快速发现明显 Regression。

---

# 66. Main Gate

建议：

```text
100~500 Cases
repeat=3
```

---

# 67. Release Gate

必须执行：

```text
Golden
Regression
Security
Core Business
```

严格模式。

---

# 68. Gate Rules

```yaml
rules:
  task_success:
    max_regression_percent: 1

  tool_calls:
    max_regression_percent: 20

  tokens:
    max_regression_percent: 25

  latency:
    max_regression_percent: 20

  security:
    max_failures: 0

  golden:
    required_pass_rate: 1.0
```

---

# 69. Blocking Policy

Metric：

```yaml
blocking: true
```

或：

```yaml
blocking: false
```

建议：

- 安全 Assert：Hard Gate
- Golden deterministic：Hard Gate
- DeepEval semantic：默认 Soft Gate
- 多次稳定性证实后可升级为 Hard Gate

---

# 70. CLI

```bash
agent-eval case list
agent-eval case show <case>

agent-eval benchmark list
agent-eval benchmark run core

agent-eval experiment create
agent-eval experiment run exp-001

agent-eval run list
agent-eval run show run-001

agent-eval compare run-a run-b

agent-eval failures list run-001
agent-eval failures cluster run-001

agent-eval report run-001
agent-eval gate run-001
```

---

# 71. 本地开发模式

```bash
agent-eval run evals/harness/tool-selection.yaml \
  --repeat 3 \
  --concurrency 2 \
  --profile smoke \
  --save-trace
```

支持参数：

```text
--repeat
--concurrency
--model
--profile
--baseline-policy
--baseline-run
--debug
--save-trace
--no-judge
--tag
```

---

# 72. Web UI

V1.5 增加 React + TypeScript。

首页：

```text
Current Release
Task Success
Regression Count
Security Failures
Avg Cost
Avg Latency
Flaky Cases
```

---

# 73. Benchmark UI

字段：

```text
Name
Version
Cases
Last Run
Pass Rate
Regression
Owner
```

支持：

- Search
- Tag Filter
- Version Switch
- Run Benchmark

---

# 74. Experiment UI

表格：

```text
Variant
Success
Completion
Efficiency
Tool Accuracy
Cost
Latency
Stability
```

支持按任意 Metric 排序。

---

# 75. Run Detail UI

Tabs：

```text
Overview
Cases
Metrics
Trace
Failures
Artifacts
Configuration
```

---

# 76. Trace Viewer UI

```text
Agent
├── LLM
├── Tool
│   └── Result
├── LLM
├── SubAgent
│   ├── Tool
│   └── Result
└── Final
```

节点显示：

```text
duration
tokens
input
output
error
metric result
```

---

# 77. Regression UI

左右对比：

```text
Baseline | Candidate
```

展示：

- Trace Diff
- Answer Diff
- Metric Diff
- Tool Diff
- Cost Diff
- Latency Diff

---

# 78. Failure UI

支持视角：

```text
By Category
By Cluster
By Tool
By Model
By Version
By Benchmark
```

---

# 79. Storage

V1：

```text
DuckDB + JSONL + Local Artifact Directory
```

未来可迁移：

```text
PostgreSQL + Object Storage
```

---

# 80. 数据表

核心：

```text
benchmarks
dataset_versions
cases
case_versions
suites
suite_cases
experiments
experiment_variants
runs
case_runs
traces
spans
metric_results
failures
failure_clusters
baselines
quality_gates
human_reviews
artifacts
```

---

# 81. runs Schema

```text
id
benchmark_id
dataset_version
experiment_id
variant_id
agent_version
git_commit
model
judge_model
status
started_at
finished_at
total_cases
passed_cases
failed_cases
total_tokens
total_cost
```

---

# 82. case_runs Schema

```text
id
run_id
case_id
case_version
iteration
status
latency_ms
token_count
cost
trace_path
artifact_path
failure_category
```

`failure_category` 是 failures 表（Spec V2.1 §1.3 派生层）结论的反范式缓存，便于按分类过滤 CaseRun；事实源为 failures 表，可随时重建。

---

# 83. metric_results Schema

```text
id
case_run_id
evaluator
metric
score
threshold
verdict
blocking
reason
metadata_json
```

---

# 84. REST API

未来 Web UI 使用：

```text
/api/benchmarks
/api/datasets
/api/cases
/api/suites
/api/experiments
/api/runs
/api/traces
/api/regressions
/api/failures
/api/gates
/api/reviews
```

---

# 85. Scheduler

V1 使用：

```text
asyncio
```

核心：

```text
CaseScheduler
```

控制：

- Case Queue
- Concurrency
- Timeout
- Cancellation
- Retry

---

# 86. Concurrency

分开配置：

```text
agent_concurrency
judge_concurrency
```

避免 Agent Model 与 Judge Model 共享 Rate Limit 时相互影响。

---

# 87. Retry Policy

必须区分：

```text
Agent Retry
Infrastructure Retry
Judge Retry
```

Agent Runtime 自己发生的 Retry 属于 Trace 数据；Eval Runner 网络重试属于 Infrastructure Retry。

---

# 88. Environment

支持抽象：

```text
local
docker
remote
```

V1 实现：`local`。

Coding / 高风险脚本场景后续使用 Docker / gVisor / WASM Sandbox。

---

# 89. Fixture Provider

```python
class FixtureProvider(ABC):
    async def prepare(self, context): ...
    async def snapshot(self, context): ...
    async def cleanup(self, context): ...
```

首批：

```text
FilesystemFixture
SQLiteFixture
PostgresFixture
GitFixture
```

---

# 90. Artifacts

Case Run 可保存：

```text
files
screenshots
logs
database snapshot
git diff
command output
reports
```

Artifacts 必须与 `case_run_id` 关联。

---

# 91. Judge Model

Judge 与被评测 Agent Model 分离配置。

记录：

```text
provider
model
temperature
endpoint
version
```

Judge Model 变化视为 Eval 环境变化。

---

# 92. Judge Cost Optimization

执行顺序：

```text
Native Eval
 ↓
明显 Infra / Hard Failure
 ↓
按策略跳过部分高成本 Judge
 ↓
DeepEval
```

支持：

```text
--no-judge
```

用于本地快速验证。

---

# 93. DeepEval Dataset 策略

平台 Dataset 为 Source of Truth。

运行时：

```text
Platform Dataset
      ↓
Case Loader
      ↓
DeepEval Golden / TestCase
```

不把平台的 Fixture、Harness Constraint、Version 信息塞进 DeepEval Dataset 作为唯一存储。

---

# 94. 项目结构

```text
agent-eval/
├── pyproject.toml
├── src/
│   └── agent_eval/
│       ├── cli/
│       ├── api/
│       ├── models/
│       ├── adapters/
│       ├── benchmark/
│       ├── dataset/
│       ├── experiment/
│       ├── runner/
│       ├── trace/
│       ├── fixtures/
│       ├── evaluators/
│       │   ├── native/
│       │   ├── deepeval/
│       │   └── harness/
│       ├── regression/
│       ├── failures/
│       ├── quality/
│       ├── security/
│       ├── storage/
│       └── reports/
├── evals/
├── fixtures/
├── profiles/
└── tests/
```

---

# 95. 技术栈

```text
Python 3.12
DeepEval
Pydantic
PyYAML
httpx
asyncio
Typer
Rich
DuckDB
Jinja2
pytest
```

建议增加：

```text
SQLGlot
OpenTelemetry
Pandas（仅分析层需要时）
```

---

# 96. P0 — 核心执行链路

必须实现：

```text
EvalCase
Dataset
Suite
HttpAgentAdapter
SSE Parser
TraceEvent
TraceBuilder
Span Tree
Fixture
Runner
Native Evaluator
DeepEvalAdapter
TaskCompletion
ToolCorrectness
CLI
JSON Result
```

目标：

```text
Agent → Trace → Eval → Result
```

---

# 97. P1 — 回归平台

实现：

```text
Dataset Version
Baseline
Candidate
Regression Compare
Trace Diff
Metric Diff
DuckDB
StepEfficiency
ArgumentCorrectness
Repeat
Flaky Detection
HTML Report
```

---

# 98. P2 — Experiment

实现：

```text
Experiment
Variant
Experiment Matrix
Model Comparison
Prompt Comparison
Harness Comparison
Cost Analysis
Historical Trend
```

---

# 99. P3 — Failure Intelligence

实现：

```text
Failure Taxonomy
Failure Classification
Failure Clustering
Promote to Benchmark
Human Review
Review Queue
```

---

# 100. P4 — Quality & Security

实现：

```text
PR Gate
Main Gate
Release Gate
Security Suite
Red Team Suite
Security Evaluator
```

---

# 101. P5 — Web Platform

实现：

```text
React + TypeScript
Dashboard
Benchmark UI
Experiment UI
Run UI
Trace Viewer
Regression UI
Failure UI
Quality UI
```

---

# 102. MVP Definition

MVP 包含：

```text
Benchmark
Case
HTTP Agent
Trace
Native Eval
DeepEval
Baseline
Regression
Report
```

不要求 Experiment Matrix、Failure Clustering、Web UI 一次完成。

---

# 103. MVP Case 数量

至少：

```text
20~30 Cases
```

覆盖：

```text
Tool
Database
Skill
MCP
Context
Error Recovery
```

---

# 104. MVP 验收

执行：

```bash
agent-eval benchmark run smoke
```

必须完成：

1. Load Dataset
2. Resolve Case Version
3. Prepare Fixture
4. Health Check
5. Create Agent Session
6. Run Agent
7. Collect Events
8. Build Trace
9. Run Native Eval
10. Run DeepEval
11. Save Result
12. Save Raw Trace
13. Save Metrics
14. Generate Report

---

# 105. Regression 验收

运行：

```text
Agent V1
Agent V2
```

然后：

```bash
agent-eval compare run-v1 run-v2
```

必须输出：

```text
Regression Cases
Improved Cases
Flaky Cases
Task Success Diff
Task Completion Diff
Step Efficiency Diff
Tool Correctness Diff
Tool Calls Diff
Token Diff
Cost Diff
Latency Diff
Failure Category Diff
```

---

# 106. Experiment 验收

输入：

```text
2 models
2 prompts
2 tool policies
```

自动生成：

```text
8 variants
```

并对同一 Dataset 执行完整评测。

---

# 107. Failure Intelligence 验收

一个拥有 50+ Failure 的 Run：

- 自动分类
- 自动聚类
- 每个 Cluster 展示代表 Case
- 支持 Promote to Benchmark

---

# 108. Release Gate 验收

Release 执行：

```text
Golden
Regression
Security
Core Business
```

任一 Hard Gate 失败：

```text
Release Gate = FAIL
```

---

# 109. 非功能需求

## 109.1 Performance

本地：

```text
100 Cases
concurrency=5
```

Eval Engine 自身不得成为主要耗时来源。

## 109.2 Reliability

Runner 进程异常退出后，已完成 Case 的结果必须可恢复。

采用增量写入，而不是全 Run 结束后一次性保存。

## 109.3 Reproducibility

必须记录：

```text
dataset version
case version
agent version
model config
judge config
deepeval version
eval platform version
git commit
```

## 109.4 Extensibility

新增以下能力不得修改主 Runner：

```text
Agent Adapter
Evaluator
Fixture Provider
Reporter
Artifact Handler
```

---

# 110. 工程原则

1. **Platform Dataset 是 Source of Truth。** DeepEval Dataset 是适配结果。
2. **Raw Trace 必须永久可追溯。** 不能只保存 score。
3. **Deterministic 优先。** 能程序判断的不交给 LLM Judge。
4. **Judge Failure 不等于 Agent Failure。**
5. **Harness Evaluator 插件化。**
6. **Baseline / Candidate 必须使用同一 Dataset Version。**
7. **线上真实问题必须支持晋升为 Regression Case。**
8. **DeepEval 通过 Adapter 隔离。** 禁止在业务代码中四处散落 SDK 调用。
9. **Cost / Latency / Stability 与 Correctness 同等保存。**
10. **安全 Rule 不能被语义 Judge 覆盖。**

---

# 111. 最终目标形态

```text
                    Agent Engineering Platform
                              │
          ┌───────────────────┼───────────────────┐
          ▼                   ▼                   ▼
      Benchmark           Experiment          Production
          │                   │                   │
          ▼                   ▼                   ▼
      Evaluation         Comparison           Trace Replay
          │                   │                   │
          └───────────────────┼───────────────────┘
                              ▼
                         Regression
                              │
                    ┌─────────┴─────────┐
                    ▼                   ▼
             Failure Intelligence   Quality Gate
```

---

# 112. 产品最终定义

**Agent Evaluation & Regression Platform 是一个以 DeepEval 为通用语义评测引擎、以自研 Harness Trace 与 deterministic evaluator 为核心基础设施，覆盖 Benchmark、Experiment、Regression、Failure Intelligence、Security 与 Quality Gate 的 Agent 工程质量平台。**

它不是：

```text
LLM 打分工具
```

而是：

```text
Agent 研发质量基础设施
```

最终让 Agent 开发从：

```text
人工体验
+
人工猜测
```

升级为：

```text
可复现
可量化
可比较
可回归
可追踪
可实验
可治理
可发布门禁
```

的工程体系。

---

# 113. DeepEval 集成参考

当前 DeepEval 官方文档中，Evaluation 由 Test Case、Metric 与 Dataset 组成，并支持 end-to-end 与 component-level evaluation；Dataset 以 Golden 为基础，可在执行时转换为 Test Case；Agent Metrics 当前覆盖完整 trajectory 与 tool-calling component 两个层次。因此本 PRD 选择将 DeepEval 放在 Evaluation Engine 层，而将 Benchmark、Experiment、Regression 与 Harness-specific Evaluation 保留在自研平台层。

参考：

- https://deepeval.com/docs/evaluation-introduction
- https://deepeval.com/docs/evaluation-datasets
- https://deepeval.com/docs/evaluation-test-cases
- https://deepeval.com/docs/metrics-introduction
- https://deepeval.com/docs/getting-started
