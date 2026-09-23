# Error Handling

> 错误分类、传播与 CLI 退出码映射。契约：PRD §46 / Spec V2.1.1 §6.1。

---

## 退出码（P0 契约，不可更改）

| code | 语义 | 映射异常 |
|---|---|---|
| 0 | Gate 评估完成且 PASS | `RunOutcome(exit_code=0)` |
| 1 | Gate 评估完成且 FAIL（blocking failure） | blocking metric fail |
| 2 | 基础设施失败，不可归因于变更 | `InfraError` / `EvaluationInfraError` |
| 3 | 无效调用，不重试 | `InvalidCallError`（含 `MetricUnavailableError`） |

## 异常类型（src/agent_eval/errors.py）

- `AgentEvalError`：携带 `exit_code` 的基类；CLI 只捕获这一类 + KeyboardInterrupt(130)。
- `InvalidCallError`：YAML 非法、benchmark/profile 不存在、metric 能力缺失且无 fallback。
- `InfraError`：endpoint 不可达、HTTP 非 2xx、SSE 断裂/负载非法。
- `AgentFailureError`（非 AgentEvalError）：Agent 通过 error 事件上报失败——这是**评测结果**（FAIL/AGENT_FAILURE），不是平台错误。
- `UnsupportedAssertionError`：断言词汇超出 P0 支持面；Runner 启动期 `scan_unsupported_assertions` fail-fast。

## 跨层规则

1. 失败语义三分（PRD §46）：Agent 超时/错误 → `AGENT_FAILURE`；传输层 → `INFRA_FAILURE`；Judge → `EVALUATION_FAILURE`。
2. 单 iteration 异常不得终止整个 Run：`_execute_iteration` 兜底捕获 → 该轮记 ERROR → Run 状态 partial → exit 2。
3. 增量落盘用 tmp 文件 + `Path.replace` 原子替换，避免崩溃留下半写 JSON。
4. 可恢复的读路径（`RunStore.list_runs`）对损坏文件跳过而非抛错；不可恢复的读（`load_run`）抛 `InvalidCallError`。
5. Adapter 层 best-effort 清理（cancel）用 `contextlib.suppress`；结果已定的收尾不允许覆盖主结果。
