# harness-bench Web Evaluation Execution Control Plane 设计方案

> 项目：`Abeautifulsnow/harness-bench`  
> 文档类型：增量设计 / PRD 补充  
> 主题：通过 Web 页面受控发起、取消、观察自研 Agent 评测任务  
> 日期：2026-10-02  
> 状态：已按代码库事实核对修订（见 §0），作为 V1 实施契约

---

# 0. 修订记录（事实核对）

本文档初稿基于对仓库的逐项核对修订后入库。核对结论：

- ✅ 承重事实全部属实：PRD §73 确实要求 `Run Benchmark`；API 层现状确为 GET-only
  （9 个 router / 33 个端点）；Web 确为只读控制台（`web/README.md` 原文）；Runner 确有
  `repeat` / `no_judge` / `strict_protocol` / `judge_concurrency` 等参数；`/health` 上报
  观测面能力表 + 实际模型属实；SSE 适配器（`adapters/sse.py`）属实；baseline 可比性
  守卫（Spec §4.3）属实；文中所列"新增"概念当前代码中均不存在，定位准确。
- ✏️ §12 订正：初稿把 `deterministic` / `release` 写成"已有 Profile"，实际
  `evals/profiles/` 下为 `smoke` / `nightly` / `strict` / `chatbot-plain` /
  `chatbot-judge` / `chatbot-strict`。已改为真实名单。
- ✏️ §24 订正：初稿的"已有的 `agent_concurrency`"在代码中不存在；实际字段是
  `RunConfig.concurrency`（CLI `--concurrency`，agent 侧并发）与 profile 内的
  `judge_concurrency`。新 API 以 `agent_concurrency` 命名暴露、映射到
  `RunConfig.concurrency`，映射关系已写明。
- ✏️ `judge_concurrency` 从 V1 API 请求模型中移除（初稿 §13/§16.1/§46 包含它）：
  Runner 不接受运行时覆盖，它由 Git 中的 profile 声明——放开它违反 §12
  "Web 不允许临时修改 profile 参数"的原则。
- ✏️ 示例名澄清：`chatbot-core`、`ai-chatbot-staging`、`deterministic` 等为示意名；
  实际资产为 `evals/benchmarks/ai-chatbot-core.yaml`、`database-core.yaml` 等。
  文中示例已尽量换成真实资产名，仍为示意的一律是 AgentConnectionProfile（PR4 新增）。
- ✏️ §48.1 的 `unhealthy agent` 用例边界澄清：`POST /api/eval-runs` 为快速 202，
  **不做**提交时健康拦截；健康检查是 /health 端点 + New Evaluation 页面的前置把关动作。

---

# 1. 背景

`harness-bench` 当前已经具备较完整的 Agent Evaluation & Regression 能力，包括：

- Benchmark / Dataset / Suite；
- 外部自研 Agent 接入；
- HTTP + SSE；
- Multi-turn；
- Trace；
- Native Evaluator；
- Harness Evaluator；
- Semantic Judge；
- Security Evaluator；
- Regression；
- Stability；
- Quality Gate；
- Failure Intelligence；
- Human Review；
- Web Analysis Console。

当前 CLI 已经能够通过：

```bash
agent-eval benchmark run <benchmark>
```

对自研 Agent 发起完整评测。

当前 Web 也已经具备：

```text
Dashboard
Benchmark
Case
Suite
Run
Trace
Experiment
Regression
Failure
Quality
Review
Security
Cost
Trends
```

但当前 Web 被明确实现为：

> **Read-only Console**

API 层也只暴露 GET 接口。

因此当前实际链路为：

```text
CLI
 ↓
Runner
 ↓
Agent
 ↓
Evaluation
 ↓
Result
 ↓
Web 查看
```

而不是：

```text
Web
 ↓
Runner
 ↓
Agent
 ↓
Evaluation
 ↓
Web 实时查看
```

---

# 2. 与原 PRD 的关系

原 PRD `docs/agent-evaluation-regression-platform-engineering-prd-v2.md` 的：

```text
§73 Benchmark UI
```

明确要求：

```text
支持：

- Search
- Tag Filter
- Version Switch
- Run Benchmark
```

因此：

> **“通过 Web 发起 Benchmark”不是新增产品方向，而是原 PRD 已存在、当前尚未完整实现的能力。**

但原 PRD 只定义了：

```text
Run Benchmark
```

这一产品动作，并没有完整定义背后的：

```text
异步任务模型
任务状态机
执行 API
取消语义
并发控制
实时进度
Agent Connection Profile
Judge Secret
权限模型
审计
SSRF 防护
```

因此本设计文档的目的不是推翻现有 PRD，而是：

> **补全 §73 `Run Benchmark` 所缺失的工程设计。**

---

# 3. 当前实现差距

当前状态：

| 能力 | 当前实现 |
|---|---|
| Web 查看 Benchmark | ✅ |
| Web 搜索 Benchmark | ✅ |
| Web 查看历史 Run | ✅ |
| Web 查看 Trace | ✅ |
| Web 查看 Regression | ✅ |
| Web 查看 Failure | ✅ |
| Web 查看 Quality Gate | ✅ |
| CLI 启动 Benchmark | ✅ |
| Web 启动 Benchmark | ❌ |
| Web Cancel Run | ❌ |
| Web 查看实时进度 | ❌ |
| Web 选择 Agent Connection | ❌ |
| Web 选择 Profile | ❌ |
| Web 设置 repeat | ❌ |
| Web 设置 concurrency | ❌ |
| Web 设置 no-judge | ❌ |
| Web 设置 strict-protocol | ❌ |
| 异步 Eval Job API | ❌ |

当前 Web README 中的：

```text
“覆盖 PRD §72–§78 的全部导航面”
```

与 PRD §73 中的：

```text
Run Benchmark
```

存在实现偏差。

---

# 4. 产品目标

本功能的目标是让用户无需进入 CLI，即可通过 Web 完成一次标准化 Agent Evaluation。

目标流程：

```text
Web
 ↓
选择 Agent
 ↓
选择 Benchmark
 ↓
选择 Suite / Profile
 ↓
设置运行参数
 ↓
启动 Evaluation
 ↓
实时观察进度
 ↓
查看结果
 ↓
Regression
 ↓
Quality Gate
```

---

# 5. 非目标

V1 不将 Web 改造成完整评测资产编辑后台。

以下能力不属于本阶段：

```text
在线编辑 Dataset
在线编辑 Case
在线编辑 Suite
在线修改 Gate
在线修改 Security Rule
在线编写 Evaluator
在线修改 Benchmark YAML
在线修改 Judge Prompt
```

这些内容继续维持：

```text
Git
+
YAML
+
CLI
```

作为事实源。

---

# 6. 核心设计原则

## 6.1 Definition Plane 与 Execution Plane 分离

建议明确划分三个 Plane：

```text
┌──────────────────────────────┐
│ Definition Plane             │
│                              │
│ Dataset                      │
│ Case                         │
│ Benchmark                    │
│ Suite                        │
│ Profile                      │
│ Gate                         │
│                              │
│ Git / YAML                   │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│ Execution Plane              │
│                              │
│ Web                          │
│ API                          │
│ CLI                          │
│                              │
│ Run                          │
│ Cancel                       │
│ Progress                     │
└──────────────┬───────────────┘
               │
               ▼
             Runner
               │
               ▼
┌──────────────────────────────┐
│ Analysis Plane               │
│                              │
│ Run Detail                   │
│ Trace                        │
│ Regression                   │
│ Failure                      │
│ Security                     │
│ Quality Gate                 │
└──────────────────────────────┘
```

---

# 7. V1 用户流程

## 7.1 New Evaluation

新增页面：

```text
/evaluations/new
```

页面结构建议：

```text
New Evaluation

Agent
  ai-chatbot-staging

Benchmark
  ai-chatbot-core

Suite
  core

Profile
  smoke

Repeat
  3

Agent Concurrency
  4

Judge
  Disabled

Strict Protocol
  Enabled

Tags
  smoke, regression

[ Run Evaluation ]
```

---

# 8. Agent Connection Profile

Web 不应默认允许用户直接输入任意 URL。

不推荐：

```text
Agent Endpoint:
http://任意地址
```

因为这会产生：

```text
SSRF
内网探测
Cloud Metadata Access
Credential Exposure
任意服务调用
```

建议引入：

```text
AgentConnectionProfile
```

例如：

```yaml
id: ai-chatbot-staging
display_name: AI Chatbot Staging

endpoint: http://ai-chatbot-shim:8802

auth:
  type: bearer
  secret_ref: ai-chatbot-staging-token

enabled: true
```

Web 只展示：

```text
AI Chatbot Dev
AI Chatbot Staging
Agent V2
```

而不是暴露真实 Secret。

---

# 9. Health Check

选择 Agent 后，Web 可以执行：

```text
Health Check
```

读取现有 Agent 接入协议中的：

```text
/health
```

展示：

```text
Agent Status        Healthy
Model               qwen3.5-32b
Protocol            Compatible

Observation Surface
────────────────────────
tool.call           ✅
tool.result         ✅
mcp.call            ✅
retry               ❌
context.compaction  ❌
usage.input         ✅
usage.output        ❌
```

这样用户在启动评测之前就知道：

> 哪些 Metric 会被真实评估，哪些会因为 Observation Surface 不足而 `skipped`。

---

# 10. Benchmark Selection

Benchmark 继续来自仓库现有：

```text
evals/
```

Web 不修改 Benchmark。

只允许：

```text
选择
查看
执行
```

建议展示：

```text
Benchmark
Version
Case Count
Last Run
Last Pass Rate
Last Regression
Required Suite
Owner
```

---

# 11. Suite Selection

允许选择已定义 Suite：

```text
core
regression
security
golden
```

但应遵守 Benchmark / Gate 已定义约束。

例如 Release Benchmark 如果规定：

```text
golden
regression
security
core
```

Web 不应允许用户通过取消勾选来绕过 Release Gate。

---

# 12. Profile Selection

允许选择已有 Profile。当前 `evals/profiles/` 下实际存在：

```text
smoke
nightly
strict
chatbot-plain
chatbot-judge
chatbot-strict
```

Profile 继续来源于 Git。

Web 不允许临时修改：

```text
threshold
blocking
fallback
metric params
judge rules
```

---

# 13. 可配置运行参数

V1 推荐允许用户调整：

```text
repeat
agent_concurrency   # 新 API 参数名，映射到 RunConfig.concurrency（CLI --concurrency）
tag
no_judge
strict_protocol
save_trace
baseline_policy
baseline_run
```

注意：`judge_concurrency` **不在** V1 可调参数内——它由 Git 中的 profile 声明
（`MetricProfile.judge_concurrency`），放开运行时覆盖既违反 §12 的 Profile 原则，
也超出 Runner 现有入参。

但所有参数必须受服务器端限制。

例如：

```text
repeat <= 10
agent_concurrency <= 16
```

不能只依赖浏览器校验。

---

# 14. 为什么不能使用同步 HTTP

不推荐：

```text
POST /api/run
 ↓
await runner.run()
 ↓
HTTP 等 20 分钟
```

Agent Evaluation 天然属于长任务。

包括：

```text
几十 Cases
repeat
LLM 调用
Tool 调用
Judge
Retry
Fixture
Regression
Report
```

因此必须采用异步 Job 模型。

---

# 15. Eval Job Model

新增：

```text
EvalRunJob
```

建议状态：

```text
QUEUED
PREPARING
RUNNING
EVALUATING
FINALIZING
SUCCEEDED
FAILED
CANCELLING
CANCELLED
```

简化状态图：

```text
QUEUED
   │
   ▼
PREPARING
   │
   ▼
RUNNING
   │
   ▼
EVALUATING
   │
   ▼
FINALIZING
   │
   ├───────────────┐
   ▼               ▼
SUCCEEDED         FAILED
```

取消：

```text
QUEUED
  ↓
CANCELLED
```

或者：

```text
RUNNING
  ↓
CANCELLING
  ↓
CANCELLED
```

---

# 16. REST API

## 16.1 创建 Evaluation

```http
POST /api/eval-runs
```

Request：

```json
{
  "agent_profile": "ai-chatbot-staging",
  "benchmark": "ai-chatbot-core",
  "suite": ["core"],
  "profile": "chatbot-strict",
  "repeat": 3,
  "agent_concurrency": 4,
  "no_judge": true,
  "strict_protocol": true,
  "tags": ["smoke"]
}
```

Response：

```http
202 Accepted
```

```json
{
  "job_id": "job_01J...",
  "run_id": null,
  "status": "queued"
}
```

---

## 16.2 查看任务

```http
GET /api/eval-runs/{job_id}
```

Response：

```json
{
  "job_id": "job_01J...",
  "run_id": "run_01J...",
  "status": "running",
  "progress": {
    "total_trials": 48,
    "completed_trials": 31,
    "passed": 24,
    "failed": 4,
    "skipped": 2,
    "running": 1
  }
}
```

---

# 17. Cancel API

```http
POST /api/eval-runs/{job_id}/cancel
```

返回：

```json
{
  "status": "cancelling"
}
```

取消必须最终向现有 Runner 传递 cancellation。

不能只把数据库状态写成：

```text
cancelled
```

而实际上后台任务仍继续调用 Agent / Judge。

---

# 18. 实时进度

推荐使用：

```text
SSE
```

接口：

```http
GET /api/eval-runs/{job_id}/events
```

事件：

```text
job.started
run.created
case.started
iteration.started
turn.started
tool.started
case.completed
evaluation.completed
job.completed
job.failed
job.cancelled
```

V1 不要求把所有 Agent Trace Event 原样通过 Web SSE 推送。

首先只推送：

```text
Run-level Progress
```

即可。

---

# 19. 页面实时状态示例

```text
Evaluation

Benchmark
ai-chatbot-core

Agent
AI Chatbot Staging

Model
qwen3.5-32b

Profile
chatbot-strict

Progress
31 / 48

██████████████████░░░░░░ 65%

PASS       24
FAIL        4
SKIPPED     2
RUNNING     1

Current
case: approval_required_03
iteration: 2 / 3
turn: 4

Elapsed
02:18

[ Cancel Evaluation ]
```

---

# 20. Existing Runner 必须继续是唯一执行核心

Web 不应重新实现一套 Runner。

正确关系：

```text
CLI ────────────────┐
                    │
Web API ────────────┼──→ EvalRunService
                    │
Future Scheduler ───┘
                         │
                         ▼
                    Existing Runner
```

这样：

```text
CLI Run
Web Run
CI Run
Scheduled Run
```

共享完全相同的：

```text
Trace
Evaluator
Fixture
Regression
Gate
Artifact
Failure semantics
```

---

# 21. 推荐服务层

不要让 FastAPI Router 直接操作 Runner。

推荐：

```text
api/
  routers/
    eval_runs.py

services/
  eval_run_service.py

jobs/
  repository.py
  worker.py

runner/
  runner.py
```

关系：

```text
Router
 ↓
EvalRunService
 ↓
JobManager
 ↓
Runner
```

---

# 22. Job Repository

V1 可以先实现：

```text
Local Job Repository
```

例如：

```text
.agent-eval/jobs/
```

或 DuckDB。

必须保存：

```text
job_id
run_id
status
requested_by
request
created_at
started_at
finished_at
error
cancel_requested
```

---

# 23. Worker 模型

V1 不一定需要 Celery / Dramatiq。

可以使用：

```text
asyncio task
+
bounded semaphore
```

因为当前项目本身还是 Local-first。

但服务层必须预留：

```text
JobExecutor
```

接口。

例如：

```python
class JobExecutor(Protocol):
    async def submit(self, job): ...
    async def cancel(self, job_id): ...
```

未来可以替换为：

```text
LocalAsyncExecutor
DramatiqExecutor
RemoteWorkerExecutor
```

---

# 24. 并发控制

必须至少有两层并发限制：

```text
Global Evaluation Job Concurrency
```

例如：

```text
max_running_jobs = 2
```

以及已有的：

```text
agent_concurrency        # RunConfig.concurrency（CLI --concurrency），agent 侧并发
judge_concurrency        # MetricProfile.judge_concurrency，Git 内 profile 声明
```

否则多个 Web 用户同时点击 Run 时，会绕过单个 Runner 内部并发限制。

---

# 25. Queue

超过 `max_running_jobs` 的任务进入：

```text
QUEUED
```

页面显示：

```text
Queue Position
3
```

而不是直接启动。

---

# 26. Authentication / Authorization

如果 harness-bench 后续进入多人环境，至少建议以下角色：

```text
viewer
operator
admin
```

### viewer

允许：

```text
GET
```

### operator

允许：

```text
Run Evaluation
Cancel 自己的 Evaluation
```

### admin

允许：

```text
管理 Agent Connection
管理 Secret Reference
Cancel 任意 Job
```

V1 若仍为单机内部工具，可以暂不完整实现 RBAC。

但 API 设计不要假设：

```text
所有用户永远都是管理员
```

---

# 27. Secret 管理

以下信息禁止下发到浏览器：

```text
Agent API Key
Judge API Key
Internal MCP Token
Database Credential
SSH Credential
```

浏览器只能看到：

```text
secret_ref
```

或者脱敏状态：

```text
Configured
Missing
Invalid
```

---

# 28. SSRF 防护

如果未来允许动态输入 Agent Endpoint，必须至少进行：

```text
scheme allowlist
hostname allowlist
IP validation
redirect validation
metadata IP block
private address policy
DNS rebinding protection
```

V1 最安全的方案仍然是：

> 只允许管理员预注册 Agent Connection Profile。

---

# 29. Judge 配置

Web 允许选择：

```text
Judge Profile
```

例如：

```text
OpenAI Judge
Internal Judge
No Judge
```

但不把：

```text
API Key
Endpoint Credential
```

发送给浏览器。

服务器端 Profile：

```yaml
id: openai-default

provider: openai
model: ...

secret_ref: OPENAI_API_KEY
```

---

# 30. Quality Gate

Web 发起 Evaluation 后，运行完成应自动执行既有 Gate 逻辑。

结果展示：

```text
Quality Gate

Task Success           PASS
Tool Call Regression   PASS
Token Regression       WARNING
Latency Regression     FAIL
Security               PASS

Overall
FAIL
```

Web 不重新计算 Gate。

必须读取后端产生的事实结果。

---

# 31. Baseline

Baseline Policy 必须继续由后端控制。

允许选择：

```text
auto
explicit baseline
none
```

但必须保留现有：

```text
suite comparability
SUT comparability
dataset comparability
```

保护。

---

# 32. Failure Semantics

Web 必须区分：

```text
AGENT_FAILURE
EVALUATION_FAILURE
INFRA_FAILURE
```

例如：

```text
Agent Failed
```

与：

```text
Judge Provider Timeout
```

不能显示成同一种红色 Case Failure。

---

# 33. Run 与 Job 的关系

必须区分：

```text
Job
```

与：

```text
Run
```

Job 表示：

> “用户请求执行一次 Evaluation”

Run 表示：

> “Harness 实际产生的一次 Evaluation Run”

例如：

```text
job_123
  ↓
PREPARING
```

此时可能还没有：

```text
run_id
```

创建 Runner 后：

```text
job_123
  ↓
run_456
```

这样 Job 生命周期不会污染已有 Run 模型。

---

# 34. 任务失败

例如 Agent Endpoint 无法连接：

```text
Job
FAILED
```

原因：

```text
INFRA_FAILURE
```

如果 Run 已创建，应记录：

```text
run_id
```

如果还没有创建，则：

```text
run_id = null
```

---

# 35. Refresh / Browser Close

Evaluation 不能依赖浏览器生命周期。

用户关闭页面：

```text
Evaluation 继续运行
```

重新进入：

```text
/evaluations/{job_id}
```

应继续看到真实状态。

因此：

> Web SSE 只负责订阅，不负责承载任务本身。

---

# 36. 防止重复提交

点击：

```text
Run Evaluation
```

时前端立即 disable。

服务端推荐支持：

```text
idempotency_key
```

避免浏览器重试产生两个相同 Eval。

---

# 37. Audit

至少记录：

```text
requested_by
requested_at
agent_profile
benchmark
suite
profile
parameters
git_commit
dataset_version
agent_version
model
judge_model
```

保证之后能回答：

> 这次评测到底是谁、在什么版本、用什么配置发起的？

---

# 38. Web 路由建议

新增：

```text
/evaluations
/evaluations/new
/evaluations/:jobId
```

其中：

```text
/evaluations
```

展示：

```text
Job
Benchmark
Agent
Status
Progress
Requested By
Started
Duration
Run
```

---

# 39. Benchmark 页面改动

当前 Benchmark Detail 增加：

```text
[ Run Benchmark ]
```

点击进入：

```text
/evaluations/new?benchmark=ai-chatbot-core
```

而不是立即直接执行。

这样用户仍有机会确认：

```text
Agent
Profile
Repeat
Judge
```

---

# 40. Run Detail 与 Evaluation Job 联动

Job 成功后：

```text
Evaluation Job
      ↓
    run_id
      ↓
 Run Detail
```

页面提供：

```text
Open Run Detail
```

后续分析完全复用现有：

```text
Run
Trace
Regression
Failure
Quality
```

页面。

---

# 41. API Read-only 原则如何调整

当前 API 有一个明确约束：

> OpenAPI 中只有 GET。

实现本功能以后，这个约束需要重新定义。

不建议继续坚持：

```text
整个 API 绝对 Read-only
```

而应该改成：

> **Definition Data Read-only，Execution Commands Allowed**

也就是：

```text
GET  Benchmark
GET  Dataset
GET  Case
GET  Suite
GET  Gate

POST Eval Run
POST Cancel Eval Run
```

但依然禁止：

```text
POST /api/benchmarks
PUT  /api/gates
DELETE /api/cases
```

---

# 42. 新安全边界

新的 API 契约应固定：

```text
Definition mutation verbs = forbidden
Execution mutation verbs = allowed
```

测试可以改为：

```text
GET /api/benchmarks              ✅
GET /api/runs                    ✅

POST /api/eval-runs              ✅
POST /api/eval-runs/{id}/cancel  ✅

POST /api/benchmarks             ❌
PUT /api/gates                   ❌
DELETE /api/cases                ❌
```

这比“整个 API 必须只有 GET”更符合项目长期形态。

---

# 43. V1 UI 范围

V1 页面只需要实现四块。

## 43.1 New Evaluation

负责：

```text
配置
Health Check
Run
```

## 43.2 Evaluation List

负责：

```text
Running
Queued
History
```

## 43.3 Evaluation Detail

负责：

```text
Progress
Cancel
Error
跳转 Run
```

## 43.4 Benchmark Detail

新增：

```text
Run Benchmark
```

---

# 44. V1 后端范围

新增：

```text
AgentConnectionProfile
EvalRunJob
EvalRunService
JobRepository
LocalJobExecutor
```

API：

```text
POST /api/eval-runs
GET  /api/eval-runs
GET  /api/eval-runs/{id}
POST /api/eval-runs/{id}/cancel
GET  /api/eval-runs/{id}/events
GET  /api/agent-connections
GET  /api/agent-connections/{id}/health
```

---

# 45. 与现有 CLI 的关系

CLI 不应该被废弃。

目标是：

```text
CLI
Web
CI
```

成为三种调用入口。

所有入口最终调用：

```text
EvalRunService
```

或者共享更底层的 Runner Configuration Model。

---

# 46. 推荐的内部请求模型

建议定义统一：

```python
class EvalRunRequest(BaseModel):
    agent_profile: str
    benchmark: str
    suite: list[str] | None = None
    profile: str | None = None

    repeat: int | None = None

    # 映射到 RunConfig.concurrency（agent 侧并发）；None = Runner 缺省（4）。
    agent_concurrency: int | None = None

    no_judge: bool = False
    strict_protocol: bool = False
    save_trace: bool = True

    tags: list[str] = []

    baseline_policy: str | None = None
    baseline_run: str | None = None
```

注意 `judge_concurrency` 不在请求模型内：judge 并发由 profile 声明（§12/§13）。

CLI 与 Web 尽可能共享这一模型。

避免：

```text
CLI 参数有一套语义
Web API 又有另一套语义
```

---

# 47. 推荐代码结构

建议新增：

```text
src/agent_eval/
├── api/
│   └── routers/
│       ├── eval_runs.py
│       └── agent_connections.py
│
├── execution/
│   ├── models.py
│   ├── service.py
│   ├── repository.py
│   ├── executor.py
│   └── events.py
│
└── runner/
    └── runner.py
```

Web：

```text
web/src/
├── pages/
│   ├── EvaluationList.tsx
│   ├── EvaluationNew.tsx
│   └── EvaluationDetail.tsx
│
└── lib/
    └── evaluation-api.ts
```

---

# 48. 测试要求

## 48.1 API

至少覆盖：

```text
create job
get job
cancel queued
cancel running
invalid benchmark
invalid profile
disabled agent connection
max concurrency
duplicate request
```

注：`unhealthy agent` 不在 submit 时拦截（提交必须快速返回 202，健康探测的
超时等待不能发生在创建路径上）；由 `/api/agent-connections/{id}/health` 端点与
New Evaluation 页面的 Health Check 步骤前置把关（§9）。

## 48.2 Security

必须测试：

```text
浏览器不能取得 Secret
任意 URL 不可执行
禁用 Agent Profile 不可运行
非法 benchmark 不可注入路径
不能修改 Gate
不能修改 Dataset
```

## 48.3 Runner Integration

必须证明：

```text
Web Run
```

与：

```text
CLI Run
```

走相同 Runner。

推荐测试：

```text
same request
→ same evaluation semantics
→ same metric set
→ same gate behavior
```

---

# 49. 前端 E2E

新增 Playwright 后建议至少覆盖：

```text
打开 Benchmark
 ↓
Run Benchmark
 ↓
选择 Agent
 ↓
Health Check
 ↓
启动
 ↓
显示 Running
 ↓
完成
 ↓
打开 Run Detail
```

以及：

```text
Run
 ↓
Cancel
 ↓
Cancelled
```

---

# 50. V1 验收标准

## 启动

- Web 可以选择已注册 Agent；
- Web 可以选择 Benchmark；
- Web 可以选择 Profile；
- Web 可以设置 repeat；
- Web 可以设置 concurrency；
- Web 可以设置 Judge；
- Web 可以启动 Evaluation。

## 执行

- HTTP 返回 `202`；
- Evaluation 后台独立运行；
- 浏览器关闭后任务不中断；
- 同时运行 Job 数量受服务器限制；
- Runner 仍是唯一评测核心。

## 状态

- 可以看到 queued；
- 可以看到 running；
- 可以看到 progress；
- 可以看到 failed；
- 可以看到 cancelled；
- 可以看到 succeeded。

## Cancel

- queued 可立即取消；
- running 可以请求取消；
- Runner 收到真实 cancellation；
- 取消后不能继续产生 Agent 调用。

## 安全

- Web 看不到 Secret；
- 用户不能任意访问 URL；
- Definition 数据不可通过 Web 修改；
- Gate 不可通过 Web 临时绕过；
- Security Hard Gate 不可修改。

## 结果

- 完成后产生正常 `run_id`；
- Run Detail 与 CLI Run 完全兼容；
- Regression 正常；
- Quality Gate 正常；
- Failure Intelligence 正常；
- Trace 正常。

---

# 51. V1 不建议实现的能力

第一版不要一次性加入：

```text
Web Dataset Editor
Web Case Editor
Web Gate Editor
Web Evaluator Editor
Web Prompt Editor
Remote Distributed Worker
Cron Scheduler
Production Online Eval
Complex RBAC
Multi-tenant
```

这些都可以后续增量建设。

---

# 52. 后续 V2

V2 可增加：

```text
Scheduled Evaluation
Nightly Job
CI Trigger
Webhook Trigger
Remote Worker
Notification
Evaluation Template
Run Preset
Role Based Access
```

例如：

```text
Nightly
  ↓
chatbot-core
  ↓
repeat=3
  ↓
judge
  ↓
regression
  ↓
quality gate
```

---

# 53. 后续与 Production Evaluation 的关系

未来 Production Trace Ingestion 落地后：

```text
Web Evaluation
```

与：

```text
Production Evaluation
```

应共享同一个 Analysis Plane。

最终：

```text
Manual Web Run
CI Run
Scheduled Run
Production Trace Replay
Online Eval
```

都会落到：

```text
Run
Trace
Evaluation Result
Regression
Failure
Review
Quality
```

---

# 54. 推荐实施顺序

建议分 6 个小 PR。

## PR1：Execution Domain Model

增加：

```text
EvalRunRequest
EvalRunJob
Job Status
Job Repository
```

## PR2：Local Job Executor

增加：

```text
submit
cancel
queue
global concurrency
```

接入现有 Runner。

## PR3：Execution REST API

增加：

```text
POST /api/eval-runs
GET /api/eval-runs
GET /api/eval-runs/{id}
POST /api/eval-runs/{id}/cancel
```

## PR4：Agent Connection Profile

增加：

```text
Agent Registry
Health
Observation Surface
Secret Reference
```

## PR5：Web New Evaluation

增加：

```text
Evaluation List
New Evaluation
Evaluation Detail
Benchmark Run button
```

## PR6：SSE Progress + E2E

增加：

```text
progress event
cancel UI
Playwright
security test
```

---

# 55. 优先级判断

建议优先级：

> **P1**

它没有 Judge Calibration、CI Gate、Production Trace 那么影响评测结论本身的可信度，因此不建议提升到最高 P0。

但它属于：

> **原 PRD 未完整交付项**

并且实现成本明显低于：

```text
Production Evaluation
Remote Sandbox
OTel Integration
```

因此是非常适合作为近期增量的一项。

---

# 56. 最终建议

当前 Web 的 Read-only 设计本身不是错误。

正确演进不是：

```text
Read-only Web
 ↓
Full CRUD Web
```

而应该是：

```text
Read-only Definition
+
Controlled Execution
+
Read-only Analysis
```

也就是：

```text
Definition Plane
      ↓
    Git

Execution Plane
      ↓
 Web / CLI / CI

Analysis Plane
      ↓
     Web
```

这样既能完成原 PRD §73 的：

```text
Run Benchmark
```

又不会破坏当前 harness-bench 最重要的原则：

> **Benchmark、Gate、Security Rule 与评测事实必须可审计、可复现、不可被临时 UI 操作绕过。**

这应当作为 `harness-bench` Web 从“结果查看器”升级为“Agent Evaluation Console”的第一步。

---

# 57. 实施记录（2026-10-02）

V1 已按本文档落地。对应关系：

| 设计 | 实现 |
| --- | --- |
| §15/§22/§33/§46 域模型 | `src/agent_eval/execution/models.py`（JobStatus / EvalRunJob / EvalRunRequest / JobProgress）；`repository.py`（`<data_root>/jobs/*.json`，tmp+replace 原子写） |
| §20/§21 服务层 | `execution/service.py`：EvalRunService（提交校验、幂等、取消、状态机收口），Runner 仍是唯一执行核心 |
| §23/§24/§25 执行机制 | `execution/executor.py`：LocalJobExecutor——独立线程 + 独立 asyncio loop（任务生命周期独立于请求 loop），跨线程 Semaphore 即全局并发，取消经 `run_coroutine_threadsafe` 的 Future 传播进 Runner |
| §8/§9/§27/§28 Agent Connection | `execution/connections.py` + `evals/agents/*.yaml`（管理员预注册；bearer secret_ref 现场解析；GET /agent-connections[+ /health]） |
| §16/§17/§18/§41/§42 REST API | `api/routers/eval_runs.py`：POST /eval-runs（202，幂等重放 200）、GET 列表/详情、POST cancel（202/404/409）、GET events（SSE，终态具名事件收口）；`api/routers/agent_connections.py` |
| §13 服务端限制 | repeat ≤ 10、agent_concurrency ≤ 16（1..cap），submit 期校验 benchmark/profile/suite/connection/secret，400 快速失败 |
| Runner 补丁 | `runner.py`：`RunConfig.agent_headers`（凭证只走内存）、`run(on_progress=…)` run 级进度回调、`CancelledError` 与 KeyboardInterrupt 同一收口（run.json → cancelled） |
| Adapter 补丁 | `open_adapter(endpoint, headers=…)`；HttpAgentAdapter 注入凭证头（不落盘不进日志） |
| §43 Web | `web/src/lib/evaluation-api.ts`（仅有的两个 POST + SSE 订阅）；`pages/Evaluations.tsx` / `EvaluationNew.tsx` / `EvaluationDetail.tsx`；导航"执行 → Evaluation"；Benchmark 列表/详情页 Run Benchmark 按钮（§39：进表单而不是立即执行） |
| §48/§50 测试 | `tests/test_eval_runs_api.py`（契约/校验/生命周期/取消/SSE/Secret 不外泄/executor 单测）；`tests/test_api.py::TestReadOnlyContract` 改为 §42 新边界断言；全量 625 passed |

偏差与边界（有意为之）：

- `judge_concurrency` 不在 V1 API（§12/§13 原则：profile 是 Git 事实源）。
- submit 不做健康拦截（快速 202）；健康是 New Evaluation 页面的前置动作。
- Playwright E2E（§49）与 RBAC（§26）留待后续 PR；Job 关联的审计事实
  （§37 git_commit/dataset_version/model 等）由 run.json 承载，经 run_id 关联。
- 实施后 review 修订（2026-10-02）：定义名标识符白名单（防路径注入，§28 的落地）；
  Job 终态不可复活守卫（cancel 竞态收口）；重启时非终态 Job 收口为 failed；
  幂等键在 V2 RBAC 时应按 requested_by 作用域隔离。

---

# 58. V2 增量一（2026-10-03）：Scheduled / Preset / Trigger / Notification / 执行 token 门

§52 路线图的第一批落地。**未做**：Remote Worker（需要分布式队列设计）、完整 RBAC
（用户体系不存在；本批仅落"执行 token 门"作为最小前置）。

## 58.1 Scheduled Evaluation / Nightly Job

- Definition：`evals/schedules/*.yaml`（cron、preset/request 引用、可选 notify），
  随 Git 管理；坏定义装载期即炸（InvalidCallError）。
- Runtime State：`<data_root>/state/schedules/<id>.json`（last_run_at / last_job_id /
  next_run_at / error——调度台账，非事实源，可删可重建）。
- `SchedulerService.tick()` 注入时钟可测；进程内守护线程默认 30s 一 tick，
  仓库有调度定义才启动。
- 语义决策：
  - **首次见到调度只注册不触发**——从 now 起算下一次，绝不部署即连发历史；
  - **错过窗口合并为一次补跑**（coalesce），不回填；
  - **cron 为本地时间**（5 字段、数字、`*/n`、区间、逗号组合；Vixie dom/dow 并集
    规则）。DST 折返日"少跑/多跑一次"是已知边界，接受。

## 58.2 Evaluation Preset / Run Preset

- `evals/presets/*.yaml`：命名的部分参数模板（全部字段可选）。
- 三个消费方：Web New Evaluation 预填表单；Trigger 引用；Schedule 引用。
- 解析规则：显式覆盖 > 预设字段；发起评测时必填字段缺失 → 400（绝不编默认值）。

## 58.3 CI / Webhook Trigger

- `evals/triggers/*.yaml`：`POST /api/triggers/{id}/run`，Bearer token（env var 名
  进仓库、值留在部署环境，§27 约定）。token env 缺失 → **fail-closed 400**；
  token 错误 → 401。CI 应携带 `Idempotency-Key` 防重放。
- 审计：`requested_by = "trigger:<id>"`；执行 token 门（见 58.5）可整体关闭该入口。

## 58.4 Notification（最小形态）

- Job 终态 webhook：`EvalRunJob.notify_webhook`（+ 可选 `notify_token_ref`），
  由 schedule/trigger 提交时给出。
- **尽力而为 + 留痕**：独立守护线程同步 POST（10s 超时），成功/失败追加
  `<data_root>/state/notifications.jsonl`；失败不改写终态、不重试、不阻塞执行线程。

## 58.5 执行 token 门（RBAC 最小前置）

- 设 `AGENT_EVAL_EXEC_TOKEN` 后，全部执行动词（POST /eval-runs、cancel、
  trigger run）要求 `Authorization: Bearer <token>`（恒定时间比较）；
  未设置保持 V1 开放行为。GET 一律不受限。完整 viewer/operator/admin 仍属后续。

## 58.6 验收

- `tests/test_automation.py`：cron 解析边界（含 Vixie 并集规则）、调度注册/触发/
  合并补跑/禁用/坏预设、trigger 鉴权矩阵、执行 token 门、通知内容与留痕。
- API：GET /schedules、GET /eval-presets、GET /triggers、POST /triggers/{id}/run。
- Web：Automation 页（调度台账 + 触发器视图）、New Evaluation 预设填充。

---

# 59. 修订（2026-10-03）：§45 统一入口闭合 + Notification 重试/站内通知

## 59.1 CLI 统一入口（§45 闭合）

- `agent-eval benchmark run` 改经 `EvalRunService.run_sync()`：同一套服务端校验
  （repeat/并发上限、标识符白名单、suite/profile/gate/baseline 校验）+ 同一份
  Job 账本（`<data-dir>/jobs/`），等待终态后按既有约定输出与退出。
- 输出契约与退出码逐字保留：`run <id> status=... gate=... verdict=...`、
  warnings 逐行、Gate FAIL exit 1 / 基础设施 exit 2 / 无效调用 exit 3、
  Ctrl+C → cancel + exit 130、非法 endpoint exit 3（含旧文案关键词）。
- CLI ad-hoc 被测地址（`--agent` / `AGENT_EVAL_AGENT_ENDPOINT`）以
  `connection_override` 进入，不要求注册表里有对应 profile——Web 的注册表
  约束（§8）不变；`request.agent_profile` 仅作请求记录。
- 代价：EvalRunRequest 扩至与 RunConfig 对齐（gate/judge_skip_policy/timeout/
  模型标签/experiment 归属/save_artifacts）——§46"CLI 与 Web 共享同一模型"
  从此是字面事实，Web API 也能设置这些执行调优字段。
- Job 记录新增结果字段：`run_status / gate / gate_verdict / exit_code / warnings`。

## 59.2 Notification 升级（§52）

- **重试 + 退避**：最多 3 次尝试，间隔 2s/8s；只重试暂态失败（网络错误 /
  HTTP 5xx），4xx 首次失败即收口（永久性配置问题，重试只刷屏）。
  每次尝试独立留痕于 `state/notifications.jsonl`（含 attempt 序号）。
- **站内通知**：不另建事件存储——feed 直接从 Job 账本投影终态 Job
  （`GET /api/notifications`），已读标记存 `state/notification_state.json`
  （上限 500，超限丢最旧）。`POST /api/notifications/read` 只改 UI 状态，
  是 §42 边界的第三类合法 POST（非 Definition、非评测事实）。
- Web：侧栏底部通知铃铛（未读计数、最近 12 条、单条点击即已读、全部已读）。

---

# 60. V2 增量二（2026-10-03）：§18 细粒度实时事件 + §53 Production Trace 第一片

## 60.1 §18 细粒度实时事件（case/turn/tool 级）

- Runner 挂 `on_event` sink（实例级、run 期重置）：case.started/completed、
  turn.started/completed、tool/mcp/子代理/压缩/异常活动事件。
- 转发是**白名单标量**：name/tool/status 等小字段进实时流；工具参数与 LLM
  响应等大 payload 留在落盘 trace（走 Trace Viewer），`model.request/response`
  不转发（高频大帧）。
- JobEventHub：跨线程投递（执行线程 loop → 订阅者请求 loop），有界队列 500、
  慢订阅者丢帧——观测面 best-effort，事实源仍是 Job 快照（§35 分工不变）。
- SSE：`/api/eval-runs/{id}/events` 合并两条流——job.* 快照 + `job.activity`
  （具体 type 在 data 里；EventSource 无法通配监听，故统一事件名）。
- Web：Evaluation 详情页运行中显示"实时活动"滚动区（最近 8 条），终态后隐藏。
- 观测面故障绝不影响评测：sink 抛异常即摘除。

## 60.2 §53 Production Trace Ingestion 第一片

已做（摄取 + 只读查看，分析面同构第一步）：

- **OTel 转换器**（`trace/otel.py`）：OTel traces JSON → 平台事件。span 名
  **保真保留**（不映射进 §8 词汇表——生产命名是被观测系统的事实）；
  gen_ai/openinference 语义约定只提取展示用 hints。
- **摄取入口** `POST /api/production/traces`（执行 token 门 + 8 MiB 上限）：
  platform（TraceEvent）与 OTel 双格式自动识别，逐 case 校验。
- **存储** `<data_root>/production/<trace_id>/`（meta.json + events.jsonl，
  append-only 事实源，派生视图可重建）。
- **查看**：GET 列表/详情/`/trace`（Span Tree）。树构建双路径：带 parent 结构
  的事件直构（OTel 天生带树）；平台事件流回落 TraceBuilder。与 Run Trace
  Viewer 共用 SpanNode/SpanTree 组件。Web 新增"生产 Trace"页（总览）。

**未做（后续片）**：Online Eval（对摄取 trace 跑评测——需先定生产 Q/A 与
benchmark dataset 的对齐语义）；Trace Replay 到 benchmark 用例（同一对齐
问题）；production run 投影进 runs/ 分析面（会伪造 evaluation 事实，等
Online Eval 语义定了再做）；OTel Collector/SDK 侧的采集端集成。

---

# 61. V2 增量三（2026-10-03）：§53 Online Eval（参考无关 judge）

## 61.1 对齐语义（本片的核心决策）

Online Eval 评测的是生产 trace 自身的 (input, actual_output) 对，判定标准来自
**EvalPolicy**（Definition Plane 资产 `evals/eval-policies/*.yaml`：judge metric
清单 + 阈值 + judge model + 超时）——**参考无关，不需要 dataset 对齐**。
需要"同题对比"的 Trace Replay（生产行为 vs benchmark case 期望）是独立的
后续特性，不与 Online Eval 混用语义。

## 61.2 pair 来源（优先级递减，绝不编造）

1. 摄取时显式给出的 `input` / `expected_output` / `context`（随 meta 落账）；
2. 事件提取：`run.started.data.input`（兜底 `model.request` 末条 user 文本）
   与 `run.finished.data.output`；tool 序列取自 `tool.call/mcp.call`；
3. 都没有 → 该 case 记为 **unextractable，如实跳过并计数**（422 当整条
   trace 无可评测对）。

## 61.3 执行与结果

- 复用 Runner 的同一条 judge 路径（`DeepEvalCapabilityAdapter.evaluate`，
  含 `a_measure` 修正与 judge model 分离）；适配器经
  `create_app(production_evaluator=…)` 可注入（测试 stub）。
- 每个 (case, metric) 独立收口：score<threshold → fail；SDK 异常/超时 →
  error（reason 原样）；SDK 未安装 → **skipped**（与 Runner 能力探测同语义）。
  单格失败绝不中断整批。
- 结果落 `<trace>/evaluations/<eval_id>.json`（含 rows + summary：per-metric
  pass/fail/error/skipped/mean_score），历史可列。**不产生 Run、不算 Gate、
  不进 Regression**——生产 trace 没有可比基线，伪造这些只会制造假象。
- API：GET /eval-policies、POST /production/traces/{id}/evaluate（token 门）、
  GET …/evaluations。Web：生产详情页评测区（策略选择/运行/结果表/历史）。

## 61.4 OTel 采集端（文档级）

采集侧示例（Collector OTLP→JSON 转换后直接 POST 摄取端点）：

```yaml
# otel-collector-config.yaml（节选）：exporter 把 OTLP 转 JSON 推给平台
exporters:
  otlphttp:
    endpoint: http://agent-eval-host:8000/api/otel-sink   # 由采集侧网关转换
service:
  pipelines:
    traces: { receivers: [otlp], exporters: [otlphttp] }
```

V1 不内置 Collector：摄取端点吃标准 OTel traces JSON，任何能把 OTLP 导出为
JSON 的采集链（Collector + translate_exporter / 自定义脚本 / SDK exporter）
都能对接；token 走 `AGENT_EVAL_EXEC_TOKEN`。
