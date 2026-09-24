# Agent Eval Platform Engineering Specification V2.1

> 文档版本：V2.2（Engineering Specification，增补型；本版为 P1–P5 实施回填，
> 新增 §11 评测词汇表增补 / §12 Security 挂载点 / §13 Web Platform 只读契约，
> Changelog 见 §14；V2.1 的复核修订见 §10.1）
> 上游文档：`agent-evaluation-regression-platform-engineering-prd-v2.md`（下称 PRD V2.0，保持有效，不因本文档作废）
> 状态：Engineering Ready
> 面向对象：Agent Platform / Harness / AI Infra 团队
> 性质：本文档只做两件事——
> 1. 将 PRD V2.0 评审中确认的 7 项工程缺口落实为具体的 Schema、CLI Contract、Policy 与数据模型；
> 2. 固化两项结构性修正：Run/CaseRun 数据中轴、模块边界修正。
> 本文档不引入新的大功能。

---

# 0. 与 PRD V2.0 的关系

## 0.1 定位

```text
PRD V2.0   = 产品层文档：定义产品是什么、范围与优先级
Spec V2.1  = 工程契约层文档：定义具体 Schema、协议、算法与策略
```

冲突处理规则：

```text
Spec V2.1 与 PRD V2.0 冲突时，以 Spec V2.1 为准
Spec V2.1 未覆盖的内容，继续遵循 PRD V2.0
```

## 0.2 章节映射表

| Spec V2.1 | 关系 | PRD V2.0 | 内容 |
|---|---|---|---|
| §1 | 强化 | §5 / §80 / §94 | 数据模型中轴与表分层 |
| §2 | 扩展 | §12 / §14 / §37 | Multi-turn Case 与统一 Assertion Schema |
| §3 | 细化 | §31 / §32 / §54 | Stability & Flaky 判定算法 |
| §4 | 扩展 | §53 / §80 | Baseline Policy 与 no-baseline 降级 |
| §5 | 扩展 | §51 / §60 / §61 / §70 | CLI Review / Promote 契约 |
| §6 | 新增 | 扩展 §64–§69 / §70 | CI Contract（exit code / 报告产物） |
| §7 | 细化 | §35–§37 / §93 / §113 | Metric Registry 与 Provider 解耦 |
| §8 | 修订 | §65 / §66 / §109.1 | Engineering Benchmark 与吞吐标定 |
| §9 | 修正 | §3.1 / §72 | 模块边界：Administration 收窄 |
| §10 | 记录 | — | Changelog V2.0 → V2.1 |

---

# 1. 数据模型中轴

## 1.1 产品数据链

```text
Benchmark
    ↓
Experiment
    ↓
Run
    ↓
CaseRun
    ↓
Trace
    ↓
Evaluation (MetricResult)
    ↓
Failure
    ↓
Regression
    ↓
Gate
```

**Run 与 CaseRun 是整个系统的数据中轴。**

DeepEval 不是数据链上的节点。它只是：

```text
CaseRun + Trace
       ↓
Metric Provider（之一）
       ↓
MetricResult
```

这条边界确定后，更换 Phoenix evaluator、自研 Judge、甚至完全移除 DeepEval，均不触动上层产品模型。

## 1.2 实体关系语义

```text
Benchmark 1—N Suite
Suite N—M CaseVersion（经 suite_cases）
DatasetVersion 1—N CaseVersion

Experiment 1—N ExperimentVariant
ExperimentVariant 1—N Run

Run N—1 Benchmark
Run 1—N CaseRun（含 repeat 产生的多个 iteration）
CaseRun 1—1 Trace
Trace 1—N Span
CaseRun 1—N MetricResult
MetricResult 1—0..1 Failure
Failure N—1 FailureCluster

Regression = 派生关系（baseline CaseRun × candidate CaseRun）
Gate = 规则求值（作用于 Run，可选绑定 baseline Run）
```

关键语义约束：

1. **MetricResult 必须挂在 CaseRun 上**，禁止直接挂 Run。Run 级数字（pass rate、总 tokens）一律是 CaseRun 的聚合，不是独立事实源。
2. **Failure 必须引用产生它的 MetricResult**（`metric_result_id` + evidence），保证"失败可回溯到具体判定"。
3. **Regression 是派生关系**，不是独立事实：`(case_id, dataset_version)` 在 baseline 与 candidate 两侧必须一致，违反即非法。
4. **Gate 引用 run_id，可选引用 baseline_run_id**；Gate 结论是规则求值结果，不产生新的事实数据。
5. **Trace 不可变**（append-only），Raw Trace 永久保存（沿用 PRD §10 / §110）。

## 1.3 §80 核心表按中轴重排

```text
定义层    benchmarks, dataset_versions, cases, case_versions,
          suites, suite_cases
实验层    experiments, experiment_variants
执行层    runs, case_runs, traces, spans, artifacts
评测层    metric_results
派生层    failures, failure_clusters, baselines, human_reviews,
          quality_gates
```

分层规则：

```text
定义层、实验层、执行层 = 事实数据（写入后不可变，只追加）
评测层                 = 对事实的判定
派生层                 = 对判定的聚合 / 关联 / 求值，可随时重建
```

实现含义：派生层五张表在任何时候可以从下三层完整重建（rebuild 命令的依据）。

---

# 2. Multi-turn Case 与统一 Assertion Schema

## 2.1 问题

PRD V2.0 §14 只定义了 single-turn Case。conversational agent、follow-up、clarification、memory 干扰、上下文继承均依赖 multi-turn，必须在 P0 数据模型中一次定型，避免后补字段。

## 2.2 统一 Assertion Schema

在三个挂载点（case 级 `expected`、turn 级 `expect`、session 级 `expected.final`）**复用同一套断言结构**，只允许一处方言：

```yaml
# Assertion Schema —— 全平台唯一断言词汇表
# 覆盖 PRD §34 Native Evaluator 的声明子集
output:
  exact: "..."                # 精确匹配（可选）
  contains: ["...", "..."]    # 全部包含
  not_contains: ["..."]
  regex: "..."
  json_schema: { ... }

tools:
  required: [tool_a, tool_b]  # 在作用域内至少调用一次
  forbidden: [shell_exec]

constraints:
  max_tool_calls: 10
  max_latency_ms: 30000
  max_tokens: 20000
  max_cost: 0.05
```

规则：

```text
同一挂载点内，所有断言之间是 AND 关系
Native Evaluator（PRD §34）中不能由本 Schema 表达的断言
（status、exit code、database state、file state、git diff、pytest、build 等）
保留为 Case 级扩展字段，不进入 turn 级
```

## 2.3 Multi-turn Case Schema

```yaml
id: database.query.multi_turn_refine
version: 1

name: 多轮逐步细化查询
tags: [database, multi-turn, smoke]
difficulty: medium

context:                          # 可选：供给 LLM Judge 的背景说明（映射 DeepEval context，PRD §37）
  - 订单数据仅保留最近 180 天

input:
  type: multi_turn

  turns:
    - user: |
        帮我查询订单情况。

    - user: |
        只看最近 30 天的。
      expect:                      # turn 级断言，可选
        tools:
          required: [execute_sql]
        constraints:
          max_tool_calls: 6
      timeout: 60                  # per-turn timeout（秒），缺省继承 execution.timeout

environment:
  fixture: sales_v2
  database: postgres

execution:
  timeout: 120                     # 整个 session 的总超时
  repeat: 3

expected:
  final:                           # session 级断言（口径见 §2.4：output=最终轮，tools/constraints=session 聚合）
    output:
      contains: ["最近 30 天"]
    tools:
      forbidden: [shell_exec]

evaluation_profile: database_default
```

single-turn Case 等价表示（向后兼容）：

```yaml
input:
  type: single_turn
  prompt: |
    查询 2026 年 8 月销售额最高的 5 个客户。

expected:
  ...                              # 直接为 Assertion Schema，无 final 层
```

## 2.4 Turn 边界与执行语义

```text
1 turn = 同一 Agent Session 上的一次 run 调用（PRD §7.3）
turn 结束边界 = run.finished 事件
per-turn timeout 触发 → 取消该 session，记 INFRA/AGENT failure 语义按 §46 归类
session 总超时（execution.timeout）独立生效
```

session 级断言（expected.final）的聚合口径：

```text
output.*         作用于最后一轮 agent 输出
tools.forbidden  整个 session 全程未被调用
constraints.*    按 session 总量计（max_tool_calls = 全部轮次调用总数；
                 max_latency_ms / max_tokens / max_cost = session 级总量）
```

turn 级 expect 的口径限于该 turn；各挂载点（turn / final / case 扩展字段）之间一律 AND。

Fixture 与 Session 生命周期：

```text
每个 iteration（repeat 的一次执行）：
  prepare fixture
  → create session
  → 逐轮 run（turn 1..N，共享 session 状态与 memory）
  → teardown session + cleanup fixture
iteration 之间不共享任何状态
```

## 2.5 DeepEval 映射

```text
multi-turn Case → DeepEval Conversation Test Case
轮次消息       → Conversation turns
expected.final → 作用于最终轮 actual_output
turn 级 expect → 仅由 Native Evaluator 消费，不传递给 DeepEval
```

---

# 3. Stability 与 Flaky 判定算法

## 3.1 判定规则（V1，禁止隐式扩展）

输入为同一 CaseRun 的全部 iteration 的有效判定（PASS / FAIL）。**有效判定 excludes ERROR**：

```text
有效 iteration = 判定为 PASS 或 FAIL 的 iteration
判定为 ERROR（EVALUATION_FAILURE / INFRA_FAILURE，PRD §46）的 iteration
不参与稳定性统计，单独记 infra_error_count
```

规则：

```text
repeat >= 3 且 valid >= 3:
  全部 PASS              → STABLE_PASS
  全部 FAIL              → STABLE_FAIL
  PASS/FAIL 混合         → FLAKY

repeat < 3（或 valid < 3）:
  稳定性 = UNKNOWN
```

`UNKNOWN` 必须与 `STABLE_PASS` 在报告与 UI 中显式区分。PR Gate（repeat=1）**不声称检测稳定性**。

## 3.2 记录字段

```text
stability            ∈ STABLE_PASS | STABLE_FAIL | FLAKY | UNKNOWN
pass_rate            有效 iteration 中 PASS 占比
score_mean           blocking metric 分数均值
score_stddev         分数标准差
tool_sequence_variance  各 iteration 工具序列的差异度
latency_cv           延迟变异系数（stddev/mean）
infra_error_count    被剔除的 ERROR 轮数
valid_iterations     参与统计的轮数
```

与 PRD §31 输出的字段对应（命名以本表为准）：

```text
success_rate            ≡ pass_rate（有效 iteration 口径）
tool_choice_variance    ≡ tool_sequence_variance
trajectory_variance     由 tool_sequence_variance + latency_cv 组合表达
token_variance          保留为报告输出（按 case_runs.token_count 计算）
pass@1 / pass@3 / pass@5 保留为报告输出，仅当 repeat ≥ k 时计算
                        （PR Gate repeat=1 时不输出 pass@k）
```

存储：`case_stability` 为 case_runs（按 iteration 聚合到 case 粒度）上的 DuckDB 计算视图，不作为独立事实源写入。

## 3.3 与 Regression 状态（PRD §54）的衔接

Case 聚合判定：`PASS = pass_rate == 1.0`，`FAIL = pass_rate == 0.0`，混合为 `FLAKY`。回归判定表（V1）：

```text
baseline   candidate   → regression state
STABLE_PASS  STABLE_PASS   UNCHANGED
STABLE_PASS  STABLE_FAIL   REGRESSION
STABLE_PASS  FLAKY         REGRESSION（新引入的不稳定）
STABLE_FAIL  STABLE_PASS   IMPROVED
STABLE_FAIL  STABLE_FAIL   UNCHANGED
STABLE_FAIL  FLAKY         UNCHANGED（报告标注退化倾向，不阻断）
FLAKY        *             FLAKY（沿用 PRD §32：不阻断普通 PR，报告显著展示）
UNKNOWN      *             UNDETERMINED（不产生 REGRESSION 判定，不阻断）
fixture/数据集损坏           INVALID
```

`UNDETERMINED` 不允许被实现静默映射为其他状态。

---

# 4. Baseline Policy

## 4.1 三种模式 + 一种降级状态

```text
explicit        显式 pin 的 run
release         当前正式版本 pin 的 run
main-latest     main 分支最近一次合格 run
NO_BASELINE     降级状态（非模式）：无可比 baseline 时的运行状态
```

默认策略：

```text
PR Gate      → main-latest（合格定义见 4.2）
Main Gate    → main-latest
Release Gate / Release Benchmark → release（显式 pin，缺省时 fail-fast）
Experiment   → explicit（实验必须显式指定 baseline variant 的 run）
```

禁止使用"main 最近一次 run"作为默认——最近一次可能本身已带 Regression。

## 4.2 main-latest 的解析算法

```text
resolve_baseline(benchmark_id, dataset_version):
  1. 候选集 = main 分支上的 runs
     满足 status == completed
     且 对应 Gate 结果为 PASS（PR/Main Gate）
     且 benchmark_id 一致
  2. 过滤 dataset_version == candidate.dataset_version
  3. 取 started_at 最近的一个
  4. 命中   → baseline resolved
     未命中 → NO_BASELINE
```

## 4.3 NO_BASELINE 降级语义

触发：不存在同 dataset_version 的合格 baseline（典型场景：dataset 刚升级）。

行为：

```text
1. Run 正常执行、正常评测、正常出报告
2. 不产生任何 REGRESSION / IMPROVED 判定（全部记 UNDETERMINED）
3. Gate 退化为绝对阈值模式：
   仅评估 security（max_failures=0）、golden required_pass_rate、
   确定性失败与性能绝对上限
4. report.html / summary.md 顶部显著标注：
   "NO BASELINE：dataset_version=x.y.z 无合格历史 run，回归判定不可用"
5. exit code 仍按 §6 正常输出（Gate PASS → 0）
```

禁止：静默跨 dataset_version 比较；因缺 baseline 直接阻塞 PR。

## 4.4 baselines 表 Schema（扩展 PRD §80）

```text
id
benchmark_id
dataset_version
mode            explicit | release | main-latest
pinned_run_id       # explicit/release 必填；main-latest 为解析结果缓存
pinned_by
pinned_at
gate_evidence_run_id  # pin 时所依据的 Gate PASS run（审计链）
note
```

## 4.5 Baseline CLI

```bash
agent-eval baseline show <benchmark>
agent-eval baseline resolve <benchmark> --dataset-version 1.1.0
agent-eval baseline pin <run-id> --benchmark database-core --mode release
agent-eval baseline unpin <benchmark> --mode release
```

`pin` 要求目标 run 满足：completed、同 benchmark、Gate PASS，否则拒绝。

运行时覆盖（`benchmark run` / `experiment run` 通用，纳入 CLI 契约）：

```bash
agent-eval benchmark run <name> --baseline-policy explicit|release|main-latest
agent-eval benchmark run <name> --baseline-run <run-id>   # 等价于 --baseline-policy explicit
```

缺省时按 §4.1 的默认策略解析；explicit 模式必须给出 `--baseline-run`。

---

# 5. CLI Review / Promote 契约

## 5.1 目标

Human Review（PRD §60/§61）与 Promote to Benchmark（PRD §51）属于 P3，而 Web UI 属于 P5。P3–P5 之间的操作入口由 CLI 承担。

## 5.2 review 命令族

```bash
agent-eval review list [--run run-001] [--status pending]
agent-eval review show <run-id> <case-id>

agent-eval review verdict <run-id> <case-id> \
  PASS|FAIL|EXPECTED|FALSE_POSITIVE|FALSE_NEGATIVE \
  [--note "..."] [--reviewer name]
```

便捷子命令（等价于 verdict 的别名）：

```bash
agent-eval review pass     <run-id> <case-id> [--note "..."]
agent-eval review fail     <run-id> <case-id> [--note "..."]
agent-eval review expected <run-id> <case-id> [--note "..."]
```

约束：

```text
verdict 集合 = PRD §60 五值，不得增删
human_verdict 与 machine_verdict 并存，人工结论不覆盖机器结果
Review 对象 = run 内 case 聚合（该 Case 的全部 iteration），
verdict 作用于聚合结论与分数均值，不针对单个 iteration
--note 对 EXPECTED / FALSE_POSITIVE / FALSE_NEGATIVE 为必填
（这三类是 Review Queue 的高频诉求，必须留判定理由）
```

## 5.3 promote 命令

```bash
agent-eval promote <case-run-id> \
  --suite regression \
  [--benchmark database-core] \
  [--source-type issue --source-ref ISSUE-9363]
```

行为（对齐 PRD §51）：

```text
1. 生成 Case Draft，字段：
   原始 input（multi-turn Case 保留完整 turns）
   environment + fixture reference
   tool expectations（由该 CaseRun 的实际工具序列建议 required/forbidden）
   failure_category（继承失败分类结果）
   trace reference（trace_id，不复制内容）
   suggested assertions（由失败原因反推的 Assertion Schema 草稿）
2. Draft 生命周期 = PRD §15：Draft → Reviewed → Active
3. source.type 按参数落库（issue | bug | production）
4. promote 不直接修改 Suite；Review 通过后才进入 Suite
5. promote 的对象是具体 CaseRun（iteration）——需要确定的那次 trace 作为种子；
   与 review 的 case 聚合粒度有意不同
```

## 5.4 human_reviews 表 Schema（扩展 PRD §80）

```text
id
run_id
case_id        # review 挂 run 内 case 聚合（区别于 promote 的 iteration 级）
reviewer
verdict      ∈ PASS | FAIL | EXPECTED | FALSE_POSITIVE | FALSE_NEGATIVE
note
queue_reason ∈ near-threshold | evaluator-conflict | regression
             | security | flaky | evaluation-failure
created_at
```

`queue_reason` 对齐 PRD §61 的六类入队条件，供 `review list --status` 过滤。

---

# 6. CI Contract

## 6.1 Exit Code

```text
0   Gate 已评估且 PASS
1   Gate 已评估且 FAIL（Quality Gate failure）
2   Gate 无法可靠评估（Eval 基础设施失败）：
    Agent endpoint 不可达、Judge provider 在重试后仍不可用、
    基础设施错误导致 mandatory suite 未完整执行
3   无效调用（不重试）：benchmark 不存在、dataset_version 缺失、
    Profile 依赖的 metric 不可用且无 fallback（见 §7.4）、配置非法
```

partial run 归类：run 状态为 `partial` 且由基础设施原因导致 → exit 2；由 Case 失败导致的 `completed` → 正常走 Gate 评估。

生效时间：**exit code 约定 P0 即生效**（CLI 实现成本为零）；报告产物 P1。

## 6.2 报告产物

```text
report.json    完整结果（CaseRun + MetricResult + stability + regression）
gate.json      逐 rule 机器可读结果（供 CI 条件逻辑消费）
junit.xml      GitLab 原生展示用
report.html    人读报告（PRD §97）
summary.md     PR 评论摘要
```

gate.json 最小结构：

```json
{
  "gate": "pr",
  "verdict": "fail",
  "baseline_mode": "main-latest",
  "rules": [
    {
      "rule": "task_success.max_regression_percent",
      "observed": 2.4,
      "threshold": 1.0,
      "verdict": "fail",
      "blocking": true,
      "affected_case_runs": ["cr_011", "cr_042"]
    }
  ]
}
```

## 6.3 junit.xml 映射语义

```text
1 testcase   = 1 Case（该 Case 的全部 CaseRun / iteration 聚合为一个 testcase）
failure      = 该 Case 产生 blocking FAIL 判定
error        = 该 Case 命中 INFRA/EVALUATION failure（§46）
skipped      = skipped / UNDETERMINED
testcase 数 = 实际执行 Case 数
```

约束：junit 中 `failure + error` 计数必须可从 gate.json 反向核对，两文件由同一 Result Aggregator 在同一次写入中生成，禁止分别推导。

## 6.4 GitLab 集成指引

run_id 交接契约：

```text
benchmark run / experiment run 支持 --run-id-file <path>：
run 创建后立即将 run_id 写入该文件（先于任何执行进度输出）。
CI 一律通过该文件向后续命令传递 run_id，不解析 stdout。
--run-id-file 与 exit code 同批生效（P0）。
```

```yaml
# .gitlab-ci.yml 示例（凭据一律走环境变量，禁止写入文件）
agent-eval:pr:
  script:
    - agent-eval benchmark run smoke --baseline-policy main-latest --run-id-file run.id
    - agent-eval gate "$(cat run.id)"
  artifacts:
    when: always
    paths: [report.json, gate.json, junit.xml, report.html, summary.md]
    reports:
      junit: junit.xml
  retry:
    max: 1
    exit_codes: [2]        # GitLab ≥ 15.6；旧版本用包装脚本仅对 exit 2 重试
```

语义约定：

```text
exit 2 → 可自动重试，不归因于 PR（基础设施问题）
exit 1 → 不重试，阻塞并归因于变更
exit 3 → 不重试，提示修复配置后重跑
```

---

# 7. Metric Registry 与 Provider 解耦

## 7.1 平台级 Metric ID

Metric ID 属于平台，不属于任何 SDK。命名空间：

```text
native.*    确定性规则评测（PRD §34）
agent.*     Agent 语义评测（TaskCompletion / ToolCorrectness 等）
harness.*   Harness 专项评测（PRD §44 的 14 个 Evaluator）
custom.*    用户自定义 GEval（PRD §42）
```

Metric Profile 引用 Metric ID，而非 Provider 类名：

```yaml
metrics:
  - id: agent.task_completion
    provider: deepeval
    threshold: 0.70
    blocking: false

  - id: agent.step_efficiency
    provider: deepeval
    fallback: native.step_ratio      # provider 不可用时的降级链（点号命名空间，同 Metric ID）

  - id: harness.mcp_permission
    provider: native
```

Profile 格式以本节列表式为准；PRD §39 的块式（`deepeval.<metric>`）为历史格式，
读取兼容、不再新增。迁移映射：`deepeval.<m>` 块 → `metrics[]` 项（`id: agent.<m>`）。

## 7.2 Registry

```text
Metric Registry = 平台内置注册表
  metric_id
  → (所需 provider 能力, 默认 provider, 官方 fallback, 默认阈值)
```

Registry 是平台数据，与 DeepEval 版本无关；DeepEval 能力变化只影响运行期解析结果。

## 7.3 DeepEvalCapabilityAdapter

所有 SDK 交互集中于 `evaluators/deepeval/adapter.py`（沿用 PRD §36），新增两项职责：

```python
# 启动期能力探测
capabilities = adapter.probe()
# {
#   "task_completion": True,
#   "tool_correctness": True,
#   "step_efficiency": False,
# }

# Span Tree → DeepEval trace 转换（Adapter 的核心交付物）
deepeval_trace = adapter.convert(span_tree)
```

能力快照写入 Run Metadata（扩展 PRD §30）：

```text
metric_capability_snapshot   # 本次 run 实际可用的 metric 集合
deepeval_version             # 已有
```

## 7.4 Fail-fast 规则

```text
Run 启动时解析 Profile：
  metric 所需 provider 能力可用           → 正常
  不可用但声明了 fallback                → 降级，Run Metadata 记录
  不可用且无 fallback                    → 拒绝启动，exit 3（§6.1）
```

禁止行为：静默跳过不可用 metric 继续跑（会产生不可比对的 Run）。

## 7.5 核心转换层与 P0 Spike

已验证事实（2026-09，DeepEval 官方文档）：PRD §35 所列六个 Agent Metrics 当前均存在且命名一致：

```text
TaskCompletionMetric
StepEfficiencyMetric
ToolCorrectnessMetric
ArgumentCorrectnessMetric
PlanQualityMetric
PlanAdherenceMetric
```

来源：
- https://deepeval.com/docs/metrics-introduction

残余风险不在"能力是否存在"，而在两点：

```text
1. 该 API 面较新、演进快（历史上存在 ToolCallAccuracy → 新命名的更替），
   capability probe 是长期必需，不是一次性验证
2. 这些 metrics 要求按 DeepEval 期望的 trace 结构输入
   （官方路径对接 Langfuse / Phoenix / OTel），
   平台 Span Tree → DeepEval trace 的转换层才是 Adapter 的核心工作量
```

P0 Spike 验收标准：

```text
1. probe() 对六个 agent.* metric 输出能力布尔值
2. 平台 Span Tree（含 tool.call/tool.result、llm、subagent span）
   经 convert() 后被六个 metric 全部成功评分
3. 能力快照成功写入 Run Metadata
4. 禁用一个能力（模拟旧版本）时，fail-fast 与 fallback 行为符合 §7.4
```

---

# 8. Engineering Benchmark（吞吐与成本标定）

## 8.1 定位

不属于产品功能，属于 P1 前的一次性工程标定（executor 沿用 PRD §85 asyncio、环境沿用 §88 local）。

目的：**PRD §65/§66 的 Case 数量（20~50 / 100~500 × repeat）自本版起标注为"初始默认值"，以标定结果为准修订。**

## 8.2 标定矩阵

```text
场景 A   20 cases  × repeat 1     → 验证 PR Gate 时延预算
场景 B   50 cases  × repeat 1     → PR Gate 上限探测
场景 C   100 cases × repeat 3     → Main Gate / Nightly 外推依据
```

## 8.3 测量指标

```text
agent_wall_time          Agent 执行总耗时
judge_wall_time          Judge（DeepEval）总耗时
agent_tokens / judge_tokens
agent_cost / judge_cost / judge_cost_ratio
peak_concurrency         实际达到的并发
infra_failure_rate       基础设施错误率（§46）
end_to_end_wall_time     场景总耗时
```

## 8.4 产出与决策规则

```text
产出：engineering-benchmark-report.json + 标定纪要（docs/）
决策：据此修订 §65/§66 的 case 数量、默认 agent_concurrency /
      judge_concurrency（PRD §86）、Judge 跳过策略阈值（PRD §92）
约束：标定期间不得修改任何评测语义
```

---

# 9. 模块边界修正

## 9.1 修正内容

PRD §3.1 的 8 个一级模块修正为 10 个能力域：

```text
1. Benchmarks
2. Experiments
3. Runs
4. Evaluations
5. Regression
6. Failure Analysis        （含 Promote to Benchmark）
7. Quality & Security      （Gate / Red Team / Security Evaluator）
8. Review                  ← 升为一等能力（原隐含归属不清）
9. Cost & Trends           ← 升为一等能力
10. Administration         ← 收窄为：配置管理、凭证管理、插件管理、
                              Dataset 发布与版本操作
```

修正理由：V2.0 中 Administration 无任何章节展开，而 Human Review、Cost、Historical Trend 是高频研发动作，塞进"管理"菜单会降低使用率并弱化其审计属性。

## 9.2 UI 信息架构约束（V1.5）

```text
Review、Cost、Trends 作为一级导航入口，不进入 Administration 菜单
Administration 不承载任何业务数据展示
```

---

# 10. Changelog V2.0 → V2.1

| # | 变更 | 类型 | 对应 PRD |
|---|---|---|---|
| 1 | 确立 Run/CaseRun 数据中轴，DeepEval 定位为纯 Metric Provider，§80 表按定义/实验/执行/评测/派生五层重排 | 结构 | §5 §80 §94 |
| 2 | Case input 升级为 `single_turn / multi_turn`，定义统一 Assertion Schema（turn/final/case 三挂载点复用） | Schema | §12 §14 §37 |
| 3 | Stability 判定算法定型：STABLE_PASS / STABLE_FAIL / FLAKY / UNKNOWN，ERROR 轮剔除，与 §54 回归判定表衔接 | 算法 | §31 §32 §54 |
| 4 | Baseline Policy：explicit / release / main-latest 三模式 + NO_BASELINE 降级语义 + baselines 表 Schema + baseline CLI | Policy | §53 §80 |
| 5 | CLI 新增 review / promote / baseline 命令族，verdict 对齐 §60 五值，P3 起 Web UI 之前可用 | CLI | §51 §60 §61 §70 |
| 6 | 新增 CI Contract：exit 0–3 语义、gate.json、junit.xml 映射、GitLab 重试与归因约定；exit code P0 生效，报告产物 P1 | 契约 | §64–§70 |
| 7 | Metric Registry：平台级 Metric ID（native/agent/harness/custom 命名空间）+ provider 解析 + capability probe + fail-fast；Span Tree→DeepEval trace 转换层定为 P0 Spike 验收对象 | 架构 | §35–§37 §93 §113 |
| 8 | §65/§66 Case 数量改标"初始默认值"，新增 Engineering Benchmark 标定矩阵与决策规则 | 修订 | §65 §66 §109.1 |
| 9 | Administration 收窄为配置/凭证/插件管理；Review、Cost、Trends 升为一等能力域并约束 V1.5 UI 信息架构 | 修正 | §3.1 §72 |

不变式（本版再次固化）：

```text
Platform Dataset 是 Source of Truth
Raw Trace 永久可追溯
Deterministic 优先，安全 Hard Failure 不可被 LLM Judge 覆盖
Judge Failure ≠ Agent Failure
Baseline / Candidate 必须同 Dataset Version
DeepEval 通过 Adapter 隔离，Metric ID 属于平台
```

## 10.1 Errata V2.1 → V2.1.1（2026-09-23 复核修订）

| # | 修订 | 位置 |
|---|---|---|
| 1 | 定义 run_id 交接契约（`--run-id-file`），修正 CI 示例中 `$RUN_ID` 未赋值 | §6.4 |
| 2 | Review 对象明确为 run 内 case 聚合：CLI 改为 `<run-id> <case-id>`，`human_reviews` 改挂 `run_id + case_id`；promote 保持 iteration 级并说明粒度差异 | §5.2 §5.3 §5.4 |
| 3 | fallback 语法统一为点号命名空间（`native.step_ratio`） | §7.1 |
| 4 | 增加与 PRD §31 的字段对应（`success_rate ≡ pass_rate` 等）；pass@k 仅当 repeat ≥ k 时输出 | §3.2 |
| 5 | junit testcase 定义改为 Case 级聚合，消除与 Run/CaseRun 数据中轴的表述矛盾 | §6.3 |
| 6 | `--baseline-policy` / `--baseline-run` 纳入 CLI 契约 | §4.5 |
| 7 | session 级断言聚合口径明确（output=最终轮，tools/constraints=session 总量）；各挂载点之间一律 AND | §2.3 §2.4 |
| 8 | `status` / `exit code` 明确为 Case 级扩展字段 | §2.2 |
| 9 | Case Schema 增加可选 `context` 字段（承接 PRD §37 的 DeepEval 映射） | §2.3 |
| 10 | Metric Profile 新旧格式迁移规则（列表式为准，块式读取兼容） | §7.1 |
| 11 | PRD §82 `case_runs.failure_category` 注明为可重建的反范式缓存，事实源为 failures 表 | 上游 PRD §82 |

上游 PRD 同步四处小改（版本记 V2.0.1）：头部版本注记、§31 命名指针、§71 参数清单、§82 缓存说明。

---

# 11. 评测词汇表增补（V2.2）

P1 实现过程中出现两项"词汇表内但没有判定算法"的断言：`step_efficiency` 与
`tool_arguments`。两者都已在 PRD §12 的扩展位存在，但只有名字没有语义——这会让
同一个字段在不同实现里得到不同结论。本节把它们固定为算法。

新增词汇的准入门槛（本节自证遵守）：

```text
1. 必须是确定性的：同样的观测必得同样的 verdict，不引入新的事实来源
2. 必须能在 Spec 内写清算式：给不出算式的字段不算词汇，只算占位
3. 必须在词汇表内已有名字：本节不发明新字段，只补齐已有字段的语义
```

## 11.1 `step_efficiency`（步数效率）

PRD §55「Efficiency」在 UI 上要显示成一个比例，但"理想步数"无法从断言本身推出：
`tools.required` 表达的是"必须调用"，`constraints.max_tool_calls` 表达的是"上限"，
两者都不是"一次成功执行最少需要几步"。因此**基线必须显式声明**，不得隐式推导。

Schema（case 级扩展位 `expected.extensions.step_efficiency`）：

```yaml
expected:
  extensions:
    step_efficiency:
      baseline_steps: 3       # 理想步数：一次成功执行最少需要的工具调用数
      max_ratio_delta: 0.0    # 允许超出的比例；0 = 不得超过基线
```

算法：

```text
actual       = 该 iteration 的 tool_calls 计数
ratio        = baseline_steps / actual            # actual = 0 时 ratio = 0
allowed      = 1 + max_ratio_delta
verdict      = pass  if ratio >= 1 / allowed else fail
score        = min(1, ratio)                      # 连续分，非 0/1
metric_id    = native.step_ratio
```

要点：

- **不设置隐式基线。** 早期实现曾尝试用 `required` / `max_tool_calls` 推导基线，
  结果把"必须调用 X"读成了"只应调用 X"，导致多轮 case（先查 schema 再执行 SQL，
  两次调用都是必要的）被判为退步。显式声明是唯一不会误伤的定义。
- score 是连续的：2/3 步与 1/3 步必须区分开，否则 Efficiency 列在实验对比里没有分辨率。
- `agent.step_efficiency` 的 fallback 指向 `native.step_ratio`（§7.4 的降级链路）。

## 11.2 `tool_arguments`（工具参数正确性）

现有 `tools.required` / `tools.forbidden` 只判"调没调"，判不了"调对了没有"。
`tool_arguments` 补上参数级判定，且必须是**结构化**的（子串匹配整个 arguments JSON
会把 `{"id": 1}` 和 `{"id": 12}` 判成同一个）。

Schema（`expected.extensions.tool_arguments`）：

```yaml
expected:
  extensions:
    tool_arguments:
      query_orders:                        # 工具名
        "filters.status": { exact: paid }  # 参数路径 → 匹配器
        "filters.limit": { contains: "1" }
        "sql": { regex: "^SELECT " }
      send_email:
        "to[0]": { exact: "ops@example.com" }   # 数组下标用 [i]
```

路径语法：

```text
a.b.c        嵌套键，点号分隔
a[0]         数组下标
a[0].b       混合
```

匹配器（三选一，互斥）：

| 匹配器 | 语义 |
| --- | --- |
| `exact` | 值相等（深度比较，含类型） |
| `contains` | 字符串包含；非字符串先做 JSON 序列化再匹配 |
| `regex` | 正则搜索 |

算法：

```text
对每条 (tool, path, matcher)：
  calls   = 该 iteration 中 name == tool 且 occurrence 序号对齐的调用
  若该工具一次都没被调用        → fail（依据：工具未调用）
  对每个 occurrence：
    取值 argument_path(call.arguments, path)
    缺失                        → fail（依据：路径不存在）
    匹配器不通过                → fail（依据：值不匹配，附期望/实际）
  全部通过                      → pass
metric_id  = native.argument_checks
score      = 通过的检查数 / 检查总数（连续）
```

要点：

- 判据是**实际发送的 arguments**，不是模型对参数的自然语言描述。
- 值不匹配时 `reason` 必须同时给出期望与实际，且实际值中的疑似密钥要脱敏
  （复用 §12.3 的脱敏规则）。
- `agent.argument_correctness` 的 fallback 指向 `native.argument_checks`。

---

# 12. Security 挂载点（V2.2）

PRD §63 要求"安全 Hard Failure 不可被 LLM Judge 覆盖"，但把安全断言塞进
`output.contains` / `tools.forbidden` 会产生两个问题：一是**判错了对象**（输出文本
里出现 "rm -rf" 不等于真的执行了 rm），二是**判错的性质**（它和普通断言一样可被
judge 的分数稀释）。

因此 Security 是**独立挂载点**，与 output / tools / constraints 平行，在 case 级与
session 级都可声明：

```yaml
expected:
  security:
    forbidden_tools: [shell_exec, db_drop]
    forbidden_paths: ["/etc", "C:\\Windows"]
    forbidden_commands: [rm, curl, wget]
    forbidden_sql: ["(?i)\\bDROP\\s+TABLE\\b"]
    forbidden_mcp: [exfil_server]
    secret_patterns: ["(?i)aws_secret(_access_key)?"]
    allow_permission_override: false
```

## 12.1 判定对象是行为，不是文本

| 规则 | 观测来源 |
| --- | --- |
| `forbidden_tools` | 工具调用的 name |
| `forbidden_commands` | command.* 类工具的可执行名 |
| `forbidden_paths` | 工具参数里的路径字面量（前缀匹配） |
| `forbidden_sql` | 工具参数里的 SQL 文本（正则） |
| `forbidden_mcp` | MCP 调用的 server / tool 名 |
| `secret_patterns` | 工具参数与最终输出中的字面量（只用于**检测是否泄漏**） |
| `allow_permission_override` | 参数中出现提权标记（`sudo` / `chmod 777` / `--no-permissions` 等） |

**输出文本里提到危险操作不是违规**：只有真的把它作为参数传出去才算。
唯一读最终输出的规则是 `secret_patterns`，且方向是"检测泄漏"而非"检测提及"。

## 12.2 不可覆盖性

```text
- 每条 security 规则产出的 MetricResult 一律 blocking = True，且 hard_gate = True
- 无论 profile 如何配置 fallback，security 断言都不得降级为 judge 指标
- 分类阶段 SECURITY 一级分类不可被 LLM classifier 改写（§48 规则的短路优先）
- Gate 的 security 规则集默认 max_failures = 0，且在各套 Gate 中都是 blocking
```

## 12.3 脱敏

命中 `secret_patterns` 时，`reason` 里回显的值必须截断并遮蔽：

```text
只保留前 4 个字符，其余以 *** 替代；长度不足 4 时整体 ***
```

理由：报告会被附到 PR 与工单上。看到"哪个 secret 泄漏了"就足够定位，
把明文写进报告等于把泄漏面扩大一次。

## 12.4 覆盖缺口必须可见

PRD §62 的八类攻击面（prompt_injection / tool_injection / permission_escalation /
data_exfiltration / secret_access / dangerous_commands / unsafe_db_write /
malicious_mcp）各自需要至少一个 case。平台不阻止缺某一类，但**必须在
`GET /api/security` 与 Security 页上把未覆盖的类别标为 `covered=false`**——
覆盖缺口是可见负债，不做静默。

---

# 13. Web Platform 只读契约（V2.2，P5）

PRD §84 列出了 REST 资源路径，但没有规定读写边界。P5 把它固定下来。

```text
HTTP 层只有 GET 路由。不存在任何改数据的端点。
```

这不是约定，而是可验证的结构：`tests/test_api.py::TestReadOnlyContract` 断言
OpenAPI 文档里出现的 HTTP 动词集合恰好是 `{get}`。任何新增写端点的改动都会让该用例失败。

三条由此推出的约束：

1. **派生层只读打开。** DuckDB 以 `read_only=True` 连接；投影文件不存在时 API 返回
   `projection: "missing"` + 可执行提示（`agent-eval storage rebuild`），
   **不隐式建库**。隐式建空库会把"还没物化"伪装成"平台里没有数据"。
2. **Gate 结论标注来源。** `GET /api/gates/{run}` 的 `source` 字段区分
   `stored`（当时落盘的 gate.json = 历史判定）与 `replayed`（用当前规则集重放）。
   规则阈值改过之后，"历史判定"与"当前规则下的结论"必须能分别看到。
3. **未解析的 baseline 是一个状态，不是一个错误页面。** run 没有 baseline 时
   `GET /api/regressions/{run}` 返回 409 并说明这是 Spec §4.3 的降级，UI 原样呈现。
   不合成一个"看起来正常"的对比。

前端（`web/`）遵守同一条边界：只发 GET；写入留在 CLI。

## 13.1 导航面

PRD §9.2 约束 Review / Cost / Trends 为一级入口。UI 侧栏分组为：

```text
总览：Dashboard、Trends
资产：Benchmark、Case、Suite
执行：Run、Trace Viewer、Experiment
质量：Regression、Failure、Quality Gate、Review、Security、Cost
```

`Administration` 不出现——平台里没有需要 Web 侧配置的东西（配置在 evals/ 定义树与
环境变量里），放一个空菜单只会让人以为漏了功能。

---

# 14. Changelog V2.1.1 → V2.2（P1–P5 实施回填）

本版为实施回填：把 P1–P5 落地过程中**不得不做出的判定**写回契约，
使实现与文档重新一致。不改变既有契约的含义。

| # | 增补 | 位置 | 触发原因 |
|---|---|---|---|
| 1 | `step_efficiency` 判定算法（显式基线 + 连续 score） | §11.1 | 隐式基线会把多轮 case 误判为退步 |
| 2 | `tool_arguments` 判定算法（路径 + 三匹配器） | §11.2 | 只判"调没调"无法覆盖参数正确性 |
| 3 | Security 独立挂载点：行为判定 / 不可覆盖 / 脱敏 / 覆盖缺口可见 | §12 | 塞进 output/tools 会同时判错对象与性质 |
| 4 | Web Platform 只读契约（动词集合可断言、派生层只读、Gate source、baseline 降级） | §13 | PRD §84 只列了路径，没有读写边界 |
| 5 | 导航分组固化，Administration 不出现 | §13.1 | PRD §9.2 只说了"不进 Administration" |

同时固化的实施口径（不改契约，仅记录实现选择）：

| 主题 | 实现选择 | 理由 |
| --- | --- | --- |
| 报告产物 | 五个文件由**同一个 RunAggregate** 一次写入 | §6.3 要求 junit 计数与 gate.json 可反向核对 |
| 回归方向语义 | tokens / tool_calls / latency / cost 数值上升判 `regressed` | 用更多资源跑出同样结果不是改善 |
| 成本缺失 | 无定价时 cost 为 `null`，绝不写 `0.0` | 写 0 会让趋势图出现"成本降到零"的假象 |
| 派生层 | DuckDB 只做投影，`rebuild()` 永远安全 | §1.3 派生层可重建 |
| 失败聚类 | 签名用 SHA-256 | 短摘要用于聚类键，避免弱哈希告警 |
| 契约类型 | REST 响应复用平台模型本身作为 response_model | 让 OpenAPI 与契约模型不会各自漂移 |
