# PRD §44 首批 Harness-specific Evaluators

## 问题

PRD §44 列出 14 个首批 harness-specific evaluator，**当前一个都没有**
（`SkillPriorityEvaluator` … `ForkEvaluator` 全仓 grep 为 0）。

```text
SkillPriorityEvaluator        SkillLoadEvaluator
MCPPermissionEvaluator        MCPFallbackEvaluator
ContextCompressionEvaluator   CompressionRetentionEvaluator
MemoryRetrievalEvaluator      MemoryConflictEvaluator
SubAgentRoutingEvaluator      SubAgentRecoveryEvaluator
LoopEvaluator                 RetryEvaluator
InterruptEvaluator            ForkEvaluator
```

这把两件事卡在一起：

1. **PRD §44 未交付**本身。
2. **`mvp-case-expansion` 的 Skill / MCP / Context 三类 case 写不出有意义的断言**
   —— 这三类正是这 14 个 evaluator 覆盖的领域。没有它们，只写 case 不写断言
   就是假覆盖。

`MetricDef.provider` 里已有 `harness` 这个值但无注册项
（`evaluators/registry.py`），说明这个位置是预留好的，只是没填。

## 范围

按"能确定性判定"的原则逐个实现，**不是 14 个一起上**。

### 1. 先分类，再定序

这 14 个 evaluator 的判定难度差异极大，实现前要先判断每个属于哪一类：

- **纯确定性（应当直接做）**：可从 trace / tool_calls / spans 直接判定。
  例如 `RetryEvaluator`（比较 span 里的 retry 计数）、
  `LoopEvaluator`（工具调用序列的重复模式）、
  `SubAgentRoutingEvaluator`（routing 目标是否等于期望）、
  `SkillPriorityEvaluator`（skill 加载顺序）、`ForkEvaluator`（fork 点是否符合声明）。
- **需要新观测**：trace 里当前没有对应数据，要先补采集。
  例如 `ContextCompressionEvaluator` / `CompressionRetentionEvaluator`
  （需要 context 压缩前后的事实）、`MemoryRetrievalEvaluator` / `MemoryConflictEvaluator`
  （需要 memory 读写的观测）。
- **可能无法确定性判定**：需要语义判断的，应当走 DeepEval 而不是 harness provider。

**清单本身是待核实的**：PRD 只给了名字，没给判定口径。
每个 evaluator 的"什么算 pass / 什么算 fail"必须在实现前写清楚，
写不清楚的不要实现——按 PRD §110-3"能程序判断的不交给 LLM Judge"的反面，
判定口径不清的实现只会制造噪音。

### 2. 落地方式取决于 `evaluator-plugin-sdk`

- 若 SDK 已就绪 → 每个 evaluator 是一个 plugin，**不得修改 runner**（PRD §109.4）
- 若 SDK 未就绪 → 先在 native / harness 路径下实现 2~3 个作为 SDK 的验证用例，
  再抽 SDK（见 `evaluator-plugin-sdk` 的顺序依赖讨论）

两个任务的关系是"2~3 个真实插件 ⇄ 抽象"的来回，不要试图一次成型。

### 3. 与 case 的闭环

每实现一个 evaluator，都要有 case 能触发它的两个方向：

- 命中（应该 FAIL）
- 合规（应该 PASS）

只有 FAIL 方向会把"总是判 fail 的 evaluator"当成正确实现。

## 交付物

| 文件 | 改动 |
| --- | --- |
| `src/agent_eval/evaluators/harness/*.py`（新包）或 `native.py` | 逐批实现 evaluator |
| `src/agent_eval/evaluators/registry.py` | `harness.*` metric 注册（default_threshold / fallback） |
| `evals/profiles/*.yaml` | 新 metric 进入对应 profile 的 metrics 列表 |
| `evals/datasets/*/cases/*.yaml` | 能触发每个 evaluator 双向结果的 case |
| `tests/test_harness_evaluators.py`（新） | 每个 evaluator 双向用例 |
| `.trellis/spec/backend/quality-guidelines.md` | harness evaluator 编写约定 |

## 验收

- 每个已实现的 evaluator：命中 → FAIL、合规 → PASS，各有测试。
- 未实现的 evaluator：在 prd/notes 里如实列出**未实现及原因**
  （口径不清 / 缺观测数据），不要以空实现或恒 PASS 占位。
  恒 PASS 的占位比缺失更危险——它会让覆盖统计说谎。
- `harness.*` metric 出现在 run 结果的 `metric_results` 里，
  并在 report.json / REST API 中可见。

## 依赖

- `evaluator-plugin-sdk`：扩展点契约（互为先后，见上文）
- `mvp-case-expansion`：Skill / MCP / Context 的 case 消费这些 evaluator
- `assertion-extensions`：若某些 evaluator 的声明需要新的 assertion 扩展键
