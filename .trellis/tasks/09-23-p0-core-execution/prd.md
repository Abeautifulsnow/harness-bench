# P0 核心执行链路实现（Agent → Trace → Eval → Result）

## Goal

按 PRD V2.0.1 §96（P0 清单）+ Spec V2.1.1 已定契约，实现平台最小可运行核心：
HTTP/SSE Agent 接入 → Trace 构建 → Native 评测（+ DeepEval Adapter 骨架）→ 结果落盘与 JSON 报告，
以 `agent-eval benchmark run smoke` 完整跑通为验收（PRD §104，judge 步骤可用 mock/降级）。

## Requirements（对应文档条款）

* **数据模型**：Case（Spec §2.2/§2.3 统一断言 Schema，single/multi-turn + 可选 context）、
  Dataset 版本化（§13，id@version+hash）、Suite/Benchmark 定义、Run/CaseRun/TraceSpan/MetricResult（§10/§45/§80–§83）。
* **接入层**：AgentAdapter ABC（§6.2）、HttpAgentAdapter（HTTP+SSE，§7/§8）、SSE Parser、
  事件协议 27 类事件（§8）、进程内 FakeAgentAdapter（测试）+ 线程化 mock agent server（本地 demo）。
* **Trace**：TraceBuilder 事件流 → Span Tree（§9/§10），Raw Trace 永久落盘（JSONL append-only）。
* **Fixture**：FixtureProvider ABC（§89），P0 实现 Filesystem/SQLite，每 iteration prepare/cleanup（Spec §2.4）。
* **Runner**：asyncio 调度（§85）、agent/judge 并发分离（§86）、repeat、per-turn/总超时（Spec §2.4）、
  增量写入（§109.2）、失败语义 AGENT/EVALUATION/INFRA（§46）、重试区分（§87）。
* **评测**：Native Evaluator 全量断言（Spec §2.2 词汇表：output.*/tools.*/constraints.* + status 扩展）；
  Metric Registry（Spec §7.1 命名空间 + 点号 fallback 语法）+ capability probe + fail-fast/fallback（§7.4）；
  DeepEvalAdapter：probe()/convert()（Spec §7.3），deepeval 未安装时按降级链走，六个 Agent Metrics 映射留桩。
* **稳定性**：Spec §3 判定（STABLE_PASS/STABLE_FAIL/FLAKY/UNKNOWN，ERROR 轮剔除），repeat<3 → UNKNOWN。
* **Baseline**：--baseline-policy/--baseline-run 记录进 Run Metadata（Spec §4.5）；P0 无 baseline 存储
  → 解析结果 NO_BASELINE，回归记 UNDETERMINED（Spec §4.3 降级语义，不阻塞）。
* **CLI**：benchmark list/run、case list/show、run list/show、gate（重放 verdict）；
  `--run-id-file`（Spec §6.4）、`--no-judge`（§92）；exit code 0–3（Spec §6.1，P0 生效）。
* **报告**：report.json + summary.md（§6.2 的 P0 子集；junit/html 为 P1）。
* **存储**：JSONL + 本地目录（§79 V1），DuckDB 留 P1。

## 技术决策（ADR-lite）

**Context**: 仓库名 harness-bench，uv + Python 3.14.5 已就绪；文档写 Python 3.12；DeepEval 未安装。
**Decision**:
1. 包名 `agent_eval`（src layout），CLI 入口 `agent-eval`，项目名保持 harness-bench；
   requires-python >=3.12（3.14 环境开发测试，满足文档基线）。
2. 依赖按 PRD §95 P0 子集：pydantic/pyyaml/httpx/typer/rich/jinja2 + jsonschema（output.json_schema 用）；
   dev：pytest/pytest-asyncio/ruff。deepeval 为 optional extra `[judge]`，probe 不可用时走 fallback/no-judge。
3. 失败归类裁决：Agent 超时（per-turn/总）归 AGENT_FAILURE（Agent 行为问题）；
   endpoint 不可达/连接错误归 INFRA_FAILURE；Judge/provider 异常归 EVALUATION_FAILURE（§46）。
4. 结果目录 `.agent-eval/runs/<run_id>/`（run.json、case_runs/*.json、traces/*.jsonl、report.json、summary.md）。
5. evals/ 示例集：database-core@1.0.0（≥6 cases，含 1 个 multi-turn）、suites smoke/core、profile smoke（native only，
   含一条带 fallback 的 judge metric 以演示 §7.4 降级）。
**Consequences**: P0 可在无 deepeval、无真实 Agent 的机器上全链路自测；P1 接入 DuckDB 与 baseline 存储时
不动模型层。

## Acceptance Criteria

* [x] `uv sync && uv run pytest` 全绿（60 tests：SSE/TraceBuilder/Native/稳定性/Registry/Runner e2e/CLI exit code/http/mock server）
* [x] `uv run agent-eval benchmark run database-core --tag smoke --run-id-file run.id` 对 fake:// exit 0；
      对真实 mock server（HTTP+SSE over TCP）同样 exit 0；run.id 先于执行写入（Spec §6.4）
* [x] blocking FAIL → exit 1；endpoint 不可达 → exit 2；不存在的 benchmark → exit 3；gate 重放一致
* [x] multi-turn case 按 Spec §2.4 生命周期执行（每 iteration 新 session，逐轮 run，session 级断言聚合）
* [x] `uv run ruff check .` 无告警
* [x] Raw Trace JSONL 落盘且 `run show` 可回放摘要；judge 不可用时降级链记录于
      metric_capability_snapshot / metric_degradations（Spec §7.4）

### 实施期补充决策

* 本机 uv 通用 PEP 517 桥接子进程损坏 → pyproject 固定 `uv_build` 原生后端（勿改回 hatchling）。
* `expected.final` 挂载点：Case 模型 validator 拆分为 expected_final，`session_assertions()` 统一取用。
* FakeAgentAdapter 与 dev/mock_server 保持同一套确定性脚本语义（[fail]/[forbidden]/[subagent]/30 天）。

### Review 修复（2026-09-23 review-workflow，全部落地）

| # | 缺陷 | 修复 |
|---|---|---|
| I01 | turn 级断言判定被丢弃，case 恒 PASS | `CaseRunResult.all_metric_results` 聚合三挂载点，`blocking_failed`/report/gate 统一消费 |
| I02 | 单轮 Case 误用 `expected.final` 被静默丢弃 | `_split_final` 对非 multi_turn 直接抛 ValueError → 加载期 exit 3 |
| I03 | `exit_code` 声明恒定假红 | 移出实现面，`unsupported_declarations` 启动期 fail-fast |
| I04 | `max_cost` 声明恒定假绿 | 同上；`EvalScope` 删除无来源的 cost/exit_code 字段 |
| I05 | 非法 endpoint exit 1 + 裸回溯 | `InvalidEndpointError(InvalidCallError)` → exit 3 |
| C01 | Judge 阶段占用 agent 槽位（§86） | 两阶段执行：agent_sem 包住 agent phase，judge 在槽位外；区间重叠回归测试 |
| C02 | `ruff format` 可改写契约文档 | docs 加入 exclude；`ruff format --check` 纳入质量门并清零 |
| C03 | `_execute_iteration` 128 行 | 拆为 agent_phase/open_session/drive_session/evaluate_session/finish_iteration，全部 <80 行 |
| C04 | `f` 单字母变量 | report.py 改名 failure，并在 Blocking Failures 增加 mount/turn 字段 |
| C05 | 工具运行时状态混入提交面 | .gitignore 增加 `.mimosa/`、`.zcode/`（含子目录） |
| S01 | run_store 函数内重复 import | 提升至模块头 |
| S02 | fixture 名路径越界 | `resolve_fixture_dir` 统一边界校验（filesystem/sqlite 共用） |
| S04 | 平台判定混在编排层 | 下沉为 `native.synthesize_platform_verdicts` |
| 报告 | total_cost 伪造 0.0 | 改为 null + summary 显示 "N/A (P0 不计成本)" |

未修（建议，不阻塞）：#S03 `script_queue` 在 `concurrency>1` 下的顺序约束——已由文档与
测试纪律（concurrency=1）覆盖，改为按 (case, iteration) 分配属 P1 增强。

## Out of Scope

* DuckDB 表层与 baseline 存储（P1）、Experiment（P2）、Failure Intelligence/Review（P3）、
  Gate Rules 完整引擎与 Security（P4）、Web UI（P5）
* Postgres/Git Fixture、真实 DeepEval 评分联调（需 `[judge]` extra，留待真实环境）

## Technical Notes

* 契约基线：docs/agent-eval-engineering-spec-v2.1.md（V2.1.1）、docs/agent-evaluation-regression-platform-engineering-prd-v2.md（V2.0.1）
* 工程结构按 PRD §94；事件/SSE 按 §7–§8；断言词汇按 Spec §2.2
