# 缺口任务清单（P0–P5 交付后的遗留）

> 生成于 2026-09-24。基线：`main` @ `24e3f1a`，103 pytest 通过、ruff 干净、
> 前端 typecheck/build 通过。这里列的是**已知未完成**，不是待办灵感。

单项细节见各自的 `task.json` 与 `prd.md` 目录。判定依据是 PRD（V2.0.1）与
Spec（V2.2）的验收条款，不是"感觉还差点"。

---

## 进度（2026-09-28 更新）

| 批次 | 任务 | 状态 | commit |
| --- | --- | --- | --- |
| 一（门禁） | `release-gate-suites` | ✅ | `ea162a3` |
| 一（门禁） | `security-cases` | ✅ | `38f3ad8` |
| 二（体量） | `harness-evaluators` + `evaluator-plugin-sdk` | ✅ | `9baed6a` |
| 二（体量） | `mvp-case-expansion` | ✅ | `2604520` |
| 三（扩展面） | `assertion-extensions` | ✅ | `c612c74` |
| 三（扩展面） | `semantic-trace-diff` | ✅ | `c1c5707` |
| 三（扩展面） | `case-artifacts` | ✅ | 本轮 |
| 可选 | `case-scheduler` | 不做（纯重构，见下） | — |

批次三的 4 项全部落地。`case-artifacts` 的口径写在 **Spec §21**：
采集时机（cleanup 之前的 `finally`）、能力表（`UNAVAILABLE_KINDS`，
采不到的观测面不造空文件占位）、索引只有一份（`CaseRunResult.artifacts`，
`id` 就是 `case_run_id`，不建第二份映射）、只读暴露（请求 name 必须与索引
全等才解析路径，越界一律 404）。

`semantic-trace-diff` 的第三项交付（`git diff`）的裁决与依据写在
**Spec §20.4**：**不实现**，改用已落地的 `file_state` 文件快照比对。
`case-artifacts` 复用同一份快照产出文件级产物，因此不引 git 依赖。

---

## 全量审计与修复（2026-09-28）

对 PRD（V2.0.1）与 Spec（V2.2/V2.3）做了一轮**实现 vs 契约**的全量核对，
不新增功能、只修不一致。三项批次（judge 链路 / 门禁保真 / 静默丢弃）全部落地，
契约回填写在 **Spec §22**（Errata V2.3 → V2.3.1）。

修前状态：405 pytest 全绿、ruff 干净、前端 typecheck 通过，但下列缺陷在**真实
运行**中成立——即"绿灯掩盖的问题"，是本轮最值得记的一点。

| 批次 | 缺陷 | 契约 | 处置 |
| --- | --- | --- | --- |
| 一 | 多轮用 `ConversationTestCase`（SDK 实为 `ConversationalTestCase`）→ 多轮 judge 全 ERROR | §2.5 §7.3 | 统一只构造 `LLMTestCase`，多轮走 `context` |
| 一 | `tools_called` 传 dict，SDK 要求 `ToolCall` 对象 | §2.5 | 构造 `ToolCall`；入参取 `attributes["arguments"]` |
| 一 | `probe()` 只查类名存在，探测不出上面两条 | §7.3 | 升级为真构造一次 `LLMTestCase`+`ToolCall` |
| 一 | `judge_model` 解析进配置但从不传 SDK | PRD §91 | `evaluate(model=)` + Runner 按需透传 |
| 二 | `tool_arguments` 只判第一次调用 | §11.2 | 全出现次数 + `call #N` + 连续 score + JSON 序列化 + 脱敏 |
| 二 | `forbidden_paths` 用子串包含（`/var/etc/passwd` 命中 `/etc/passwd`） | §12.1 | 带边界的路径前缀匹配 |
| 二 | main-latest 候选集不看分支 | §4.2 | 加 main 分支过滤，未知分支按不匹配处理 |
| 二 | 必跑套件未覆盖归 exit 1（应归 2） | §6.1 | `suites.coverage` fail → exit 2 |
| 二 | junit `skipped` 分支不可达（恒 0） | §6.3 §19.1.1 | `evaluated_metrics` / `is_unjudged` 接通 |
| 三 | `custom.*` 与拼错 `native.*` 静默丢弃（什么都不跑且不报错） | §7.4 PRD §42 | 指名道姓的 `MetricUnavailableError` → exit 3 |
| 三 | `hard_failure_categories` 被解析但从不求值 | PRD §47/§48 | 新增求值规则，走 taxonomy 同一词汇表 |
| 三 | `agent_version` 无任何赋值路径（永远 `None`） | PRD §91 | `RunConfig` 字段 + `--agent-version` |
| — | 套件 case 数取 `len(case_ids)`，tag-only 套件恒显示 0 | PRD §103 | 走与执行链同源的 `select_suite_cases()` |

**为什么能长期全绿**（详见 Spec §22.11）：`conftest.py` 的 autouse fixture
`deepeval_absent` 把 `probe` 猴补成 `{}`，judge 路径在整套测试里都不执行；
唯一覆盖 judge 的用例用假模块，`LLMTestCase = lambda **kw: {...}` 把类型约束
抹平。补的护栏是**真 SDK 构造**（`importorskip("deepeval")`、无 mock）。

明确**不在**本轮范围（见下方"PRD 自己标注为未来/后续的"与"执行中的发现"）：
`case-scheduler`、§19 Challenge Set、§40 Nightly Profile、呈现层缺口（发现 4）。

上述 commit（ebf408c）随后经一轮独立评审，4 项发现（1 阻塞 / 3 应修）已修复，
裁决与依据写在 **Spec §22.12**——其中 #I01 是修"假阳性"时引入的"漏判"
（目录式声明的 `forbidden_paths` 静默失效），安全规则的两个失败方向必须一起看。

---

## 优先级视图

| 任务 | 优先级 | 性质 | 阻塞了什么 |
| --- | --- | --- | --- |
| [`release-gate-suites`](./09-24-release-gate-suites/prd.md) | P0 | 代码 + 数据 | PRD §108 的 Release Gate 验收无法真正验证 |
| [`security-cases`](./09-24-security-cases/prd.md) | P0 | 数据 + 疑似引擎缺陷 | 安全 Hard Gate 空转；可能暴露 MCP 规则失效 |
| [`mvp-case-expansion`](./09-24-mvp-case-expansion/prd.md) | P1 | 数据 | PRD §103 用例量与 Skill/MCP/Context 覆盖 |
| [`harness-evaluators`](./09-24-harness-evaluators/prd.md) | P1 | 代码 | PRD §44 的 14 个 evaluator；Skill/MCP/Context 断言能力 |
| [`evaluator-plugin-sdk`](./09-24-evaluator-plugin-sdk/prd.md) | P2 | 代码（契约） | PRD §43/§109.4 的对外扩展点 |
| [`semantic-trace-diff`](./09-24-semantic-trace-diff/prd.md) | P2 | 代码 | PRD §57 的 semantic / SQL AST diff |
| [`case-artifacts`](./09-24-case-artifacts/prd.md) | P2 | 代码 + 契约 | PRD §89 snapshot + §90 case 级产物 |
| [`assertion-extensions`](./09-24-assertion-extensions/prd.md) | P2 | 代码 | 9 个已声明未求值的断言扩展键 |
| [`case-scheduler`](./09-24-case-scheduler/prd.md) | P3 | 纯重构 | 无。PRD §85 的形状，不是功能 |

---

## 依赖图

```text
release-gate-suites ──┐
                      ├──> security-cases ──────┐
                      │        │                │
                      │        └─(暴露引擎缺陷)─┤
                      │                         │
mvp-case-expansion ───┴──────────────────────┐  │
        │                                     │  │
        └──> harness-evaluators <──> evaluator-plugin-sdk
                     │
                     └──> assertion-extensions

case-artifacts ──> assertion-extensions（database_state / file_state）
semantic-trace-diff ──> case-artifacts（git diff 采集 → 裁决改为文件快照，Spec §20.4）

case-scheduler（独立，可一直不做）
```

`harness-evaluators` 与 `evaluator-plugin-sdk` 是**互为先后**的关系：
写 2~3 个真实 evaluator 再抽 SDK，抽象会比反过来准确；先定 SDK 则至少要
拿一个真实 evaluator 当验证用例。不要试图一次成型。

---

## 为什么按这个顺序

**P0 的两条是"门禁失真"，不是"功能缺失"。** 它们不会让任何功能报错，
只会让 Gate 在该拦住的时候放行——这类缺陷不会自己暴露，
所以优先级高于所有"看得见的功能"。

具体地：

1. `release-gate-suites`：`evals/gates/release.yaml` 里写了
   `suites: [golden, regression, security, core]`，但 `evaluate_gate()`
   从不读它，且 `evals/suites/` 下三个套件文件根本不存在。
   现在一个只跑了 smoke 的 run 也能拿到 Release PASS。
2. `security-cases`：安全评测引擎是完整的（7 条确定性规则、脱敏、
   不可被 judge 覆盖），但**零条 case 用它**。于是
   `security.max_failures: 0` 以"0 个 case、0 个失败"平凡通过。

**P1 是体量与能力。** `mvp-case-expansion` 的 Skill / MCP / Context 三类
在 `harness-evaluators` 落地前**写不出有意义的断言**——这两个任务必须先
想清楚"拿什么断言"，再动手加 case。宁可数量不到 20，也不要造只会
"输出里出现某个词"就算过的假覆盖。

**P2 是扩展面。** 每一项都独立可用，不影响现有验收。

**P3 可以永远不做。** `case-scheduler` 是纯重构：PRD §85 要求的五项控制
（Queue/Concurrency/Timeout/Cancellation/Retry）**功能上都在**，
只是以内联 `Semaphore` + `TaskGroup` 实现。价值在队列可观测性与千级 case 的
调度开销，不在正确性。风险是动了全绿的执行核心，收益不明确。

---

## PRD 自己标注为"未来/后续"的（不列为任务）

- §52 Production Trace Replay —— 原文"未来支持"
- §88 docker / remote environment —— 原文"V1 实现：local"
- §89 PostgresFixture / GitFixture —— 代码显式 `raise ... planned but not implemented`

这三项在 PRD 里就是"以后再说"，不计入缺口。

但注意 §88 与 `assertion-extensions` 的交叉：`pytest` / `build` / `lint`
三个断言键的**本质是在 fixture 里执行代码**，而 V1 只在 local 执行、无任何隔离。
在那三个键之前，PRD §88 的沙箱必须先落地——所以它们被显式排除在
`assertion-extensions` 之外，该任务的 prd 里记了这个判断。

---

## 建议的执行批次

1. **批次一（门禁）**：`release-gate-suites` → `security-cases`
   目标：让 PRD §108 的 Release Gate 验收真的能红也能绿。
2. **批次二（体量）**：`harness-evaluators`（先 2~3 个）→ `evaluator-plugin-sdk`
   → 剩余 harness evaluator → `mvp-case-expansion`
   目标：PRD §44 与 §103 达标，且 Skill/MCP/Context 是**真覆盖**。
3. **批次三（扩展面）**：`semantic-trace-diff` / `case-artifacts` /
   `assertion-extensions` 按依赖顺序推进。
4. **可选**：`case-scheduler`。

---

## 执行中的发现（未立项，先记账）

实现 `assertion-extensions` 时暴露、但不属于该任务表面的问题。记在这里是为了
不让它随一次绿灯消失。

### 发现的 4：report 不携带 case 级产物指针

Spec §21.1 原本写"能力表……报告与 Web 直接引用它"，但实现只在 Web（REST
`/runs/{id}/cases/{case}/artifacts`）消费 `UNAVAILABLE_KINDS`；report.json /
report.html 里既没有 case 级产物索引、没有能力表，也没有指向失败现场的指针
（实测：真实 report.json 中 `artifact` 出现 0 次）。CI 场景下只拿报告的人
看不到"这个 FAIL 的现场在哪"。

- **影响面**：呈现层缺口，不影响 Gate 结论（产物不参与判定）。
- **2026-09-28 review 的处置**：把 Spec §21.1 的措辞收窄为"Web 直接引用它"，
  并注明报告不携带是指账缺口而非承诺——先让文档与实现一致，不把缺口写成功能。
- **若要补齐**：给 `CaseAggregate`/report.json 加一份 case 级产物指针
  （`name`/`path`/`bytes`/`kind`，不塞内容），report.html 相应加"现场"链接；
  动的是 report schema，应由独立增量做，而不是修文档时顺手加。

### 发现的 3：case 级产物的采集能力缺口

`case-artifacts` 落地的能力表（Spec §21.1）里 ❌ 的五项**不是"没做"，是"采不到"**，
但其中两项的缺口与 PRD 的"未来"项绑定，值得单独记账：

- `logs`：平台**没有 run 级日志文件**——进程日志直接走 stdout，从未落盘。
  于是 PRD §90 的 "logs" 这一类产物在 V1 结构上就取不到（不是忘了实现）。
  要采它得先决定日志的落盘位置与轮转策略，属于运维面变更。
- `screenshots`：依赖 PRD §88 的 remote/浏览器 fixture，V1 不存在该观测面。
- `command_output`：由 trace 事件流覆盖（`command.started/finished`、
  `tool.result`），已可从 Raw Trace 复原，重复落盘只会造成两份事实。

三条都不影响 Gate 结论（产物不参与判定），记账的目的是让"能力缺口"与
"采集失败"在报告里长得不一样——后者会进 `CaseRunResult.artifact_notes`。

### 发现的 1：run-level metric diff 没有噪声下限

`regression/compare.py::_metric_diffs` 对两侧都存在的 metric 一律给方向
（`improved` / `regressed`），只看数值是否相等（阈值 `1e-9`）。而 `latency_ms`
是 wall-clock 时长：进程内 mock 的量级在 0~数 ms，调度抖动就会让均值从 `0`
变成 `0.4`，Run-level Diff（PRD §105）于是打出一行 `latency_ms regressed`。

- **证据**：`tests/test_api.py::test_regression_between_two_runs` 曾间歇失败
  （`{'regressed', 'unchanged'} == {'unchanged'}`）；在外部 CPU 负载下复现，
  同一 mock 连跑时 `run_metrics()["latency_ms"]` 出现 `0.4 → 0`。
- **影响面**：仅呈现层（`cli compare` 的 Run-level Diff 表、`/api/regressions`）。
  gate 不消费它——case 级性能回归走的是 `DEFAULT_PERFORMANCE_THRESHOLDS`（有阈值），
  run 级没有，这个不对称看起来是遗漏而非设计。
- **修法建议**：给 run 级的资源类 metric 也用上相对噪声下限（与 case 级
  `_performance_diff` 同源），而不是无条件给方向。改它要连带改
  `test_compare.py` 的方向语义用例——那是回归引擎的契约，应由
  `09-24-p1-regression-platform` 的后续增量来做，不在 `assertion-extensions` 里顺手改。
- **当下的处置**：`test_api` 那条用例把 `latency_ms` 显式列为 wall-clock 例外并
  写明原因，其余确定性指标仍要求 `unchanged`。这是"不拿更弱的断言盖住问题"，
  不是把问题判成通过。

### 发现的 2：baseline 解析不看"跑了哪些 suite"

`_resolve_main_latest`（`storage/baseline_store.py`）只按
`benchmark_id + dataset_version + Gate PASS + started_at 最新` 选基线，
**不比较 suites_covered**。于是 `--suite smoke`（3 条）会跟最近一次 PASS 的
`--suite golden`（24 条）比 `tool_calls.max_regression_percent`，得到
`25.2% > 20% → FAIL`——两个不同的 case 集合比平均工具调用数。

- **证据**：本机连续 `--suite smoke` 两次，基线都解析到 golden 那次 run
  （失败的 run 被 `_gate_passed` 过滤掉，于是"最近一次 PASS"一直是 golden）。
- **影响面**：只在按 suite 分批跑时出现；`.agent-eval/` 是本地数据，
  CI 一次跑全量时不触发。**不是本次改动引入的**（baseline 逻辑未改动）。
- **修法建议**：基线候选加一条"suites_covered 覆盖当前 run"的约束，
  或在不匹配时降级 `NO_BASELINE` 并给出提示（Spec §4.3 已有降级语义）。
