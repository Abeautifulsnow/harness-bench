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

## 收尾批次（2026-09-29）

按建议顺序收掉三个剩余缺口：

| 项 | 处置 | 关键裁决 |
| --- | --- | --- |
| 发现的 2（baseline 不看套件组成） | ✅ 修复 | main-latest 候选要求 `suites_covered` 与当前 run **全等**（不是覆盖——超集的均值同样不可比）；显式/release pin 是人的决定，解析期不拦，但 `compare_runs` 对**所有**模式加比较期守卫：组成不全等 → `valid=False` + 原因可见（§4.3 的"禁止静默跨集合比较"）。实测：此前 smoke run 曾拿 security run 当基线，修复后解析到同为 smoke 的 run |
| §19 Challenge Set | ✅ 落地 | 七类各一条（`challenge.*`），断言全部落在真实观测面（tool_arguments / sql_result / subagent span / compaction 计数），`tests/test_challenge_set.py` 用"拿掉行为标记必须变红"证明不是假覆盖；`challenge` 套件**不进任何 gate**（PRD §19 原文，测试钉住）。mock agent 加法式扩展：逐调用错误粒度（`tool_error_calls`，全错表达不了"自愈"）与多 SubAgent（`subagents`） |
| §40 Nightly Profile | ✅ 落地 | 六个 judge metric 全开、**不带 native fallback**——夜间跑要的是语义全量信号，降级成确定性规则等于白跑，judge 不可用按 §6.1 记 exit 2。GEval（§42 custom.*）未实现故缺位，profile 内注释记账，实现后补 |
| 发现的 1（run 级 diff 无噪声下限） | ✅ 修复 | `MetricDiff` 的方向只在变化幅度超出与 case 级性能回归**同源**的阈值时给出；wall-clock 类加绝对判据（两侧均值 < 1ms 判 unchanged——基线近 0 时相对阈值分母失义，实测 flake 0.4↔0ms）。`test_api` 的 latency 例外随之摘除，摘除即回归断言 |
| 发现的 4（报告不带产物指针） | ✅ 修复 | `CaseAggregate.artifacts` 指针（iteration/name/kind/path/bytes，不塞内容）进 report.json / REST Cases 行 / summary.md「现场」小节 / report.html「现场」列；测试断言指针与落盘文件可互解。能力表仍只有 Web——指针回答"现场在哪"，能力表回答"什么本来就采不到"（Spec §21.1 已回写） |
| §92 按策略跳过 judge | ✅ 落地 | `--judge-skip-policy skip_blocked`：case 已被阻断判死时跳过其**非阻断** judge（保守双条件：blocking 的 judge 参与判定不跳；判定未定不跳），跳过留痕为 skipped metric result（§19.1.1 独立结局）。缺省 none，行为不变 |

---

## §22.12 两项建议项收口（2026-09-29）

Spec §22.12 末尾记的两处"未修建议项"一起收掉。口径写在 **Spec §25**。

| 项 | 处置 | 关键裁决 |
| --- | --- | --- |
| `/api/suites` 每次请求全量装载 dataset | ✅ 收口 | 两项改动：`_read_yaml` 换 libyaml（`CSafeLoader`，缺失回退 `SafeLoader`，语义与 `safe_load` 逐字相同，有逐文件等价断言 + `!!python/object` 仍报错的护栏）；同一请求里同一份事实只装载一次（`GET /api/suites` 曾把每个 dataset 装两遍——suites 计数一遍、安全套件 tag 计数又一遍）。**不引缓存**：Spec §13 的契约是"定义层直接读 evals/ 文件树"，TTL 会让刚改完的定义在页面不更新；真要加，键必须是内容哈希或 `(mtime,size)` |
| junit `skipped` 只有单测覆盖 | ✅ 收口 | 推迟理由"补 fixture 要动套件组成"**实测不成立**：临时 evals 树即可构造（仓库既有范式，示例数据集组成零改动）。两条端到端路径（声明侧全 skipped / `max_cost` 无定价 → `ObservationUnavailable`）都用真实 `Runner` 跑并断言落盘的 `junit.xml`。**顺带暴露**：全 skipped 的 run 与全量验证通过同形（verdict 都是 pass）。处置取保守选项——加 run 级 `UNJUDGED` warning 指名道姓列 case，**不改 gate 规则 / exit code / verdict**（那是判定口径变更，独立增量拍板）。warning 与 junit 计数共用 `case_status_for_junit`，同源 |

实测（示例数据集 40 条 case，中位数）：

```text
                       前        后
load_dataset          ~62ms     ~24ms
GET /api/suites       249ms     73ms    （解析 88 次 → 47 次，重复装载 41 次 → 0）
GET /api/cases        100ms     53ms
```

两条新护栏都验证过"可红"：摘掉 warning 构造逻辑 → `assert 0 == 1`；
把 `list_suites` 换回自带装载的旧行为 → 护栏立刻报出重复的 `database-core`。

---

## 外部 Agent 平台接入（2026-09-29 登记，change-plan `docs/external-agent-integration-change-plan.md`）

需求源是评审已过的变更计划（四轮校准）。本次接入把框架侧缺陷一次性补齐，
**框架侧全部落地**（观测面能力协商、性能三结局、词汇校验、边界机制、
workdir 透传、基线守卫、文档条款）；接入侧两块不在本仓：

| 切片 | 内容 | 状态 |
| --- | --- | --- |
| A2+E2 观测面能力 | health 上报 `observation_surface`（事件名 → bool）→ `metric_capability_snapshot`（`event:` 前缀留痕）→ 插件 `required_events` 声明、`run_plugin` 判 skipped（不走 `resolve_metric`——它给的是 fallback/exit 3）；runner 阶段顺序改为 health → `_resolve_profiles` | ✅ 本轮 |
| A3 性能三结局 | `EvalScope` 分量级观测标志（`input_tokens`/`output_tokens: int \| None`）；`max_tokens` 在依赖分量未观测时判 skipped（与 `max_cost` 同向，禁坍缩为 0）；口径进 `RunMetadata.token_usage_scope`（full/partial/None） | ✅ 本轮 |
| A4 模型钉住 | health 自报模型回填 `agent_model`（权威于 CLI 标签）；`compare.py` 两条守卫：`agent_model` 不一致、`token_usage_scope` 不一致 → `INVALID` + `invalid_reason`（Spec §3.3/§4.3） | ✅ 本轮 |
| E1 词汇校验 | 消费 `EVENT_TYPES`：未知事件类型 → `RunMetadata.protocol_violations` → `aggregate.warnings`（默认 warn）；`--strict-protocol` / profile `strict_protocol`（nightly/strict 已开）升级 exit 2；不复用 Gate 的 `strict` 字段，不改 `type` 为 Literal | ✅ 本轮 |
| E3/E4 边界机制 | `tests/test_framework_boundary.py`：专有方言 deny-list 扫 `src/agent_eval/`（基线零命中，防以后变脏）；`SessionContext.extra` 不透明冻结 | ✅ 本轮 |
| A1 workdir 透传 | `_open_session` 经 `SessionContext.extra["workdir"]` 把 fixture 沙箱交给被测方（逐迭代独立）；http_adapter 响应体解析 `workdir_accessible` 回执：False → InfraError（exit 2），缺失 → run 级 warning（未知 ≠ 可达）；E4：extra 不透明，`project_dir` 类具名字段被边界测试禁止 | ✅ 本轮 |
| D 文档条款 | PRD §3.2/§6.2.1/§6.3/§7.1/§7.2/§8、Spec §3.3/§4.3/§6.1/§12.1.1/§12.4/§17.3.1/§19.1.2、quality-guidelines 外部接入三条、README 外部平台最小路径、`docs/external-agent-integration-guide.md`（新增） | ✅ 本轮 |
| B 转译 shim | ai-chatbot 侧测试组件（四端点契约 + §8 词汇归一化 + 审批策略 + 观测面声明），**最大单项工作量，在另一个仓** | ⬜ 接入侧 |
| C 专用评测集 | ai-chatbot 工具面/fixture 形态的 dataset + profile/gate/suite，在 ai-chatbot 仓（`evals/` 数据随本仓走，具体用例依赖 shim 先行） | ⬜ 接入侧 |
| 第 0 步冒烟 | 直连 `POST /api/chat` 的运行时行为验证（chunk 时序 / 402 时点 / 审批收尾 / `id===toolCallId` / usage 时机），shim 写码前的第一优先动作 | ⬜ 接入侧 |

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
- **2026-09-29 已补齐**（独立增量，见上方「收尾批次」）：`CaseAggregate.artifacts`
  指针（iteration/name/kind/path/bytes）进 report.json / REST Cases 行 /
  summary.md「现场」小节 / report.html「现场」列；指针必须与产物落盘可互解
  （测试断言 path 相对 run 目录真实存在）。**能力表仍只有 Web**——指针回答
  "现场在哪"，能力表回答"什么本来就采不到"，两者不是一回事。

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
- **2026-09-29 已修复**（见上方「收尾批次」）：候选要求**全等**（覆盖不充分，
  超集的均值同样不可比）；比较期对所有模式加守卫，组成不全等 → invalid + 原因。
  实测复验：smoke run 现解析到同为 `{smoke: 3}` 的基线。
