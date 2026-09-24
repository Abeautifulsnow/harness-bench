# P1 — 回归平台实现

契约来源：PRD V2.0.1 §53–§59 / §64–§70 / §79–§83 / §97 / §105；Spec V2.1.1 §1.3 / §3 / §4 / §6。

## 交付范围（PRD §97 全部 12 项）

| # | PRD §97 项 | 落地位置 |
|---|---|---|
| 1 | Dataset Version | `evals/datasets/<id>/dataset.yaml` version+hash → `dataset_versions` 投影；CLI `dataset list/show` |
| 2 | Baseline | `storage/baseline_store.py`（§4.2 解析算法 + §4.4 表）+ `baseline` CLI（§4.5） |
| 3 | Candidate | 候选 run 由 `--baseline-policy/--baseline-run` 显式绑定并写入 Run Metadata |
| 4 | Regression Compare | `regression/compare.py`，PRD §105 的 12 项 diff + §53 四层级 + §55 PERFORMANCE_REGRESSION |
| 5 | Trace Diff | `regression/trace_diff.py`，PRD §56 十项 |
| 6 | Metric Diff | `compare.py` 内 metric-level 段 |
| 7 | DuckDB | `storage/analytics.py`（§79 V1 存储；§1.3 五层表；派生层可 `storage rebuild` 重建） |
| 8 | StepEfficiency | 注册 `native.step_ratio` 并作为 `agent.step_efficiency` 官方 fallback（Spec §7.1 示例） |
| 9 | ArgumentCorrectness | 新增扩展键 `tool_arguments` + 原生 `native.argument_checks`（`agent.argument_correctness` fallback） |
| 10 | Repeat | 既有 repeat 打通到 compare（每 case 多 iteration 聚合口径 Spec §3.3） |
| 11 | Flaky Detection | Spec §3.1 判定 + compare 的 flaky 段 + report 显著展示（PRD §32） |
| 12 | HTML Report | `reports/html.py`（Jinja2）+ `reports/junit.py` + `gate.json`（Spec §6.2/§6.3） |

## 决策（ADR-lite）

1. **事实源与派生源分离。** 事实源：`evals/` 定义树 + `.agent-eval/runs/<run_id>/` 产物（JSON/JSONL）。
   作者态（baselines、human_reviews）落在 `.agent-eval/state/*.jsonl`（append-only）。
   DuckDB 只做投影/分析，任何时刻可由 `agent-eval storage rebuild` 重建（Spec §1.3 分层规则）。
2. **Baseline 解析严格按 §4.2**：completed + Gate PASS + 同 benchmark + 同 dataset_version + started_at 最近；
   未命中 → `NO_BASELINE` 降级（§4.3），回归判定全部 UNDETERMINED 且 Gate 退化为绝对阈值模式。
   禁止跨 dataset_version 比较（§1.2-3）：比较时若两侧 dataset_version 不一致 → `INVALID`，不产出 REGRESSION。
3. **两处新评测词汇**（写入 Spec 增补章节 V2.2，避免"实现悄悄扩展契约"）：
   - `native.step_ratio`：`min(1, baseline_steps/actual_steps)`，`baseline_steps` 取
     `len(tools.required)`（无声明 → 退回 `constraints.max_tool_calls`；两者都无 → `skipped`，不产出假 pass）。
   - `tool_arguments` 扩展 + `native.argument_checks`：逐 required tool 比对已声明参数子集；
     未声明 `tool_arguments` → `skipped`。
4. **Gate 规则引擎在 P1 落地**（PRD §64/§68/§69 的通用求值 + `evals/gates/*.yaml`）。
   P4 只在其上叠加 PR/Main/Release 三套规则集与安全/红队套件，不重复实现求值器。
5. **报告产物同批同源**（Spec §6.3 约束）：`report.json / gate.json / junit.xml / report.html / summary.md`
   由同一次聚合写入，junit 的 failure+error 计数必须可从 gate.json 反向核对。
6. **报告在 real judge 缺失时不得伪造分数**：`agent.*` 无 fallback 且 provider 不可用 → 启动期 exit 3（§7.4）。

## 验收

- `agent-eval baseline pin/resolve/show` 全链路；`agent-eval compare <base> <cand>` 输出 PRD §105 的 12 项。
- `agent-eval benchmark run core` 产出 5 个报告产物；`junit.xml` 与 `gate.json` 计数一致。
- NO_BASELINE 场景：回归判定 UNDETERMINED、报告顶部告警、exit code 不受影响。
- pytest 全绿；ruff check/format 通过。
