# PRD §85 CaseScheduler 显式化

## 问题

PRD §85：

```text
V1 使用 asyncio
核心：CaseScheduler
控制：Case Queue / Concurrency / Timeout / Cancellation / Retry
```

当前实现**没有 `CaseScheduler` 类**。这些能力分散在 `Runner` 里，以内联方式实现：

| PRD §85 要求 | 现状 | 位置 |
| --- | --- | --- |
| Case Queue | `for case in selected` 直接遍历 | `runner.py:232` |
| Concurrency | `asyncio.Semaphore(cfg.concurrency)` | `runner.py:229` |
| | `asyncio.TaskGroup` 一次性建全部 task | `runner.py:230` |
| Timeout | `asyncio.timeout(total_timeout)` 逐 case / 逐 turn | `runner.py:538`、`666` |
| Cancellation | `KeyboardInterrupt` → `RunStatus.cancelled` | `runner.py:238` |
| | session 级 `adapter.cancel(session)` | `runner.py:550`、`554` |
| Retry | `INFRA_RETRIES = 2`，仅会话建立阶段 | `runner.py:83` |

**功能上这些都在**，`agent_concurrency` 与 `judge_concurrency` 也是分开的
（`runner.py:229` 与 `runner.py:407`，满足 PRD §86）。所以这条不是功能缺失。

## 判断：这个任务要不要做

**倾向"做，但低优先级"**，理由：

- 现在能做是因为**规模尚小**：`selected` 全部一次性 `create_task`，
  100 个 case × repeat 3 = 300 个协程同时挂在 TaskGroup 里。
  `Semaphore` 限制了**并发执行**，但**没有限制排队对象数量**。
  PRD §109.1 要求 100 cases / concurrency=5 本地可跑——这个量级下没问题，
  但 case 数上千时，一次性建全部 task 会带来内存与调度开销，
  且**没有优先级/排队可见性**（PRD §85 的 "Case Queue" 语义）。
- 显式 `CaseScheduler` 的真正价值不是性能，是**可观测性与策略**：
  队列长度、等待时间、哪些 case 在排队、取消时如何回收——
  这些在 Gate 报告和 Web UI 里都是有用的信号，内联实现给不出来。

**但它不阻塞任何 PRD 验收项。** PRD §109.2 的"进程异常退出后已完成 Case 可恢复"
已经是增量写入满足的（`RunStore.save_case_run` 每 case 落盘），
不依赖 scheduler 的形状。

## 范围（若做）

### 1. 抽出 `CaseScheduler`

- 输入：case 列表（展开 repeat 后的 `(case, iteration)` 任务）
- 控制：并发上限、每任务 timeout、取消传播、retry 策略
- 输出：完成结果流 + 队列指标（排队时长 / 执行时长）
- **`Runner` 保留编排职责**，scheduler 只管"把任务排好、限好、取消好"。
  不要把 Gate / 报告 / 基线逻辑搬进去。

### 2. 队列可见性

- PRD §85 的 "Case Queue" 应当能在 run 期间被观测到
  （至少：已完成 / 排队中 / 执行中 的计数）
- Web UI 的 Run Detail 与 `GET /api/runs/{id}/status` 可以考虑暴露该状态
  （现有 `/status` 端点已存在，确认它现在返回什么）

### 3. Retry 策略的形状

PRD §87 要求区分三类 retry，当前只有 `INFRA_RETRIES`：

```text
Agent Retry            → 属于 Trace 数据，Runner 不重试
Infrastructure Retry   → Eval Runner 网络重试（已实现，会话建立阶段）
Judge Retry            → 未实现？
```

**先确认 Judge Retry 的现状**：`_run_judge_metrics()` 里有无重试？
如果没有，这是本任务更值得做的一半——Judge 调用失败会直接让 case 变
`EVALUATION` error，而 PRD §110-4 说"Judge Failure 不等于 Agent Failure"，
一个瞬时网络错误就污染结果是不合适的。

**建议：把"Cancellation 传播与 Judge Retry"从本任务拆出来优先做**，
`scheduler` 的形状改造留到最后。

## 交付物

| 文件 | 改动 |
| --- | --- |
| `src/agent_eval/runner/scheduler.py`（新） | `CaseScheduler` |
| `src/agent_eval/runner/runner.py` | 用 scheduler 替换内联 semaphore/TaskGroup |
| `tests/test_scheduler.py`（新） | 并发上限、timeout、取消、retry 分类 |
| `api/routers/runs.py` | `/status` 暴露队列状态（可选） |

## 验收

- 并发上限被严格遵守（现有测试应继续绿）
- 取消一个 run → 排队中的任务不启动，执行中的被 cancel
- 三类 retry 的行为可分别测试：Agent 自身 retry 记进 trace 且 Runner 不重试；
  infrastructure retry 只在会话建立阶段；judge retry 独立计数
- **重构不得改变 run.json / report.json 的形状**（外部契约不变）

## 风险

这是**纯重构**，收益是可观测性与未来规模，风险是动到了已经全绿的执行核心
（103 个测试里 `test_runner.py` 覆盖的就是这块）。
如果时间有限，这个任务可以一直不做——把它标成 P3 是合理的。
若做，务必先有 `test_runner.py` 的完整绿灯做基线，且保持外部产物形状不变。
