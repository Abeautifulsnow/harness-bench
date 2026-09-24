# PRD §43 Evaluator Plugin SDK

## 问题

PRD §43 定义了公开的扩展契约：

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

并要求"新增 Evaluator 不得修改 Runner 主流程"（PRD §109.4 同样要求
"新增 Evaluator 不得修改主 Runner"）。

当前状态：**`EvaluatorPlugin` 与 `EvaluationContext` 都不存在**（全仓 grep 为 0）。
现有的是内部实现：

- `METRIC_REGISTRY: dict[str, MetricDef]`（`evaluators/registry.py:35`）——
  Metric ID 到 provider / fallback / threshold 的静态表
- `MetricDef.provider ∈ {native, deepeval, harness}` —— 但 `harness` 这个 provider
  **在 registry 里没有任何一条注册**，是个预留位
- native 侧是 `native.py` 里一组 `_check_*` 函数，不是插件对象
- DeepEval 侧经 `deepeval_adapter.py` 隔离

也就是说：平台内部评测是插件化的（挂载点、metric id、fallback 链都分离），
但**对外没有扩展点**。第三方（或未来的 harness-specific evaluator）要加评测，
只能改 `METRIC_REGISTRY` 与 `native.py` —— 正是 PRD §43 想避免的。

## 范围

新增公开 SDK 层，**不重写**现有 native / deepeval 实现。

### 1. 定义 SDK 契约

- `EvaluatorPlugin`（ABC）：`name` 属性 + `async def evaluate(context) -> MetricResult`
- `EvaluationContext`：承载插件需要的观测面。**这个类是设计重心**——
  它决定了插件能看到什么，也决定了平台后续不能随意改哪些结构。
  现有 `native.EvalScope` 是它的天然原型（`run_status` / `final_output` /
  `tool_calls` / `latency_ms` / `tokens` / spans），
  但 `EvalScope` 是 native 内部类型，直接暴露会把内部结构冻成公开契约。
  需要判断：**是抽出 `EvaluationContext` 并让 `EvalScope` 成为它的实现，
  还是另建一层**。倾向后者需要更强的理由，倾向前者要评估对现有测试的影响。
- `MetricResult`：PRD §45 已有 `MetricResultModel`，直接复用，不要另造一个。

### 2. 注册与发现

`MetricDef.provider` 已有 `harness` 这个值却无注册项。SDK 落地时：

- 定义 harness 插件如何注册进 `METRIC_REGISTRY`（entry point？显式注册函数？）
- 插件的 `MetricDef` 如何声明 default_threshold 与 fallback
- **不可用时的降级语义**要与现有约定一致：`registry.py` 已有
  `MetricUnavailableError` 与"provider 非 native 且 no_judge → (None, None)、
  调用方记 skipped"的规则，插件要走同一条路径，不能自创一套

### 3. 与 runner 的解耦证明

PRD §109.4 是硬要求：**新增 Evaluator 不得修改主 Runner**。
所以本任务的验收里要有一个"加一个插件、`runner.py` 零改动"的证明——
最好以测试形式固定下来（比如断言插件的 metric 出现在结果里，
而测试本身不 patch runner）。

### 4. 顺序依赖

**建议本任务排在 `harness-evaluators` 之后或同时进行**：
14 个 harness evaluator 是 SDK 的第一批真实用户，
先写 SDK 再写插件容易设计出没人用得上的抽象；
先写 2~3 个插件再抽 SDK，抽象会更准。
如果选择先做本任务，至少要拿一个真实 evaluator 当验证用例。

## 交付物

| 文件 | 改动 |
| --- | --- |
| `src/agent_eval/evaluators/plugin.py`（新） | `EvaluatorPlugin` / `EvaluationContext` 契约 |
| `src/agent_eval/evaluators/registry.py` | harness provider 的注册与解析路径 |
| `src/agent_eval/evaluators/native.py` | `EvalScope` 与 `EvaluationContext` 的关系落地 |
| `tests/test_evaluator_plugin.py`（新） | 契约测试 + "runner 零改动"证明 |
| `.trellis/spec/backend/quality-guidelines.md` | 插件编写约定 |
| `docs/agent-eval-engineering-spec-v2.1.md` | SDK 章节（新增，归 Spec V2.3） |

## 验收

```python
# 一个仓库外的插件，仅依赖公开 SDK
class MyPlugin(EvaluatorPlugin):
    name = "harness.skill_priority"
    async def evaluate(self, context: EvaluationContext) -> MetricResult:
        ...
```

注册后能在 run 结果里看到 `harness.skill_priority` 的 MetricResult，
且 **`runner/runner.py` 未发生任何修改**（用 `git diff` 或测试断言证明）。

## 风险

`EvaluationContext` 是"一旦发布就难改"的公开形状。设计时优先只暴露
**已经是稳定契约的部分**（PRD §8 的 Event Protocol、PRD §10 的 Span Model、
`ToolCallRecord`），避免把内部临时结构塞进去。
