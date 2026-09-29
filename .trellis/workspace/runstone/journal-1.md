# Journal - runstone (Part 1)

> AI development session journal
> Started: 2026-09-23

---



## Session 1: P0 核心执行链路实现

**Date**: 2026-09-23
**Task**: P0 核心执行链路实现
**Branch**: `main`

### Summary

实现 agent-eval P0：models/loading/adapters(SSE+HTTP+Fake+mock)/trace/evaluators(native+registry+deepeval)/runner/storage/reports/CLI，60 tests 全绿，ruff 清零，fake://与真实 mock server 双链路 e2e 通过；确立 uv_build 后端绕开本机桥接 bug

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `wip` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 2: P0 实现 review 修复 + 双批次提交

**Date**: 2026-09-23
**Task**: P0 实现 review 修复 + 双批次提交
**Branch**: `main`

### Summary

review-workflow 全流程：11 项 findings 修复（turn 级断言参与终判、expected.final 加载期报错、exit_code/max_cost fail-fast、endpoint exit 3、judge 移出 agent 槽位、docs 排除 ruff format、fixture 路径防护等），72 测试全绿，f8225e0(P0 实现 77 文件) + 83be168(脚手架) 已本地提交；push 因内网 GitLab 不可达挂起，待网络恢复后 git push origin main

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `83be168` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 3: 缺口任务清单：9 项遗留建为 Trellis 任务

**Date**: 2026-09-24
**Task**: 缺口任务清单：9 项遗留建为 Trellis 任务
**Branch**: `main`

### Summary

P0–P5 交付后复核 PRD/Spec 验收条款，识别 9 处未完成并建为任务（含 prd + spec 上下文），产出 ROADMAP.md；同时补正 4 个过期任务状态

### Main Changes

复核 PRD（V2.0.1）与 Spec（V2.2）的验收条款后，把 9 处未完成登记为 Trellis 任务，
每项都有 prd.md + implement/check 的 spec 上下文：

| 任务 | 优先级 | 性质 |
| --- | --- | --- |
| `09-24-release-gate-suites` | P0 | 代码 + 数据：gate 的 suites 字段未被校验、三个套件文件不存在 |
| `09-24-security-cases` | P0 | 数据 + 疑似引擎缺陷：安全用例数为 0，门禁空转 |
| `09-24-mvp-case-expansion` | P1 | 数据：§103 要求 20~30 cases，实际 6 个，缺 Skill/MCP/Context |
| `09-24-harness-evaluators` | P1 | 代码：§44 的 14 个 evaluator 全缺 |
| `09-24-evaluator-plugin-sdk` | P2 | 代码（契约）：§43 的公开扩展点不存在 |
| `09-24-semantic-trace-diff` | P2 | 代码：只有 structural diff |
| `09-24-case-artifacts` | P2 | 代码 + 契约：缺 snapshot，case 级产物为空目录 |
| `09-24-assertion-extensions` | P2 | 代码：12 个词汇表键只实现 3 个 |
| `09-24-case-scheduler` | P3 | 纯重构：PRD §85 的形状，功能已在 |

关键判断：
- **两条 P0 是"门禁失真"而非"功能缺失"** —— 不会报错，只会让 Gate 在该拦时放行。
  `release.yaml` 写了 `suites: [golden, regression, security, core]` 却无人校验；
  安全评测引擎完整但零条 case 用它，`security.max_failures: 0` 平凡通过。
- **`security-cases` 附带一处疑似引擎缺陷**：`runner` 调 `evaluate_security` 时
  只传 `tool_calls` 与 `final_output`，`mcp_names` 恒为 None，
  `security.forbidden_mcp` 可能永不触发。要求在任务内**实测**后下结论，不预设。
- **`assertion-extensions` 的 `pytest`/`build`/`lint` 被显式排除**：它们本质是
  "在 fixture 里执行代码"，而 V1 只在 local 执行、无隔离，需先有 PRD §88 的沙箱。
- **`case-scheduler` 标 P3 且可永远不做**：纯重构，动全绿的执行核心，收益不明确。

同时补正 4 个过期任务状态：`00-bootstrap-guidelines`、`09-23-docs-review-spec-v2-1`、
`09-23-p0-core-execution` 的工作早已完成却仍是 in_progress；
`09-25-p5-web-platform` 已提交（dcf61ea + b9a9ec1）却仍是 in_progress。

另产出 `.trellis/tasks/ROADMAP.md`：优先级视图、依赖图、排序理由，
以及 PRD 自己标注为"未来/后续"的三项（§52 Trace Replay、§88 docker/remote、
§89 Postgres/Git fixture）为何不计入缺口。

### Git Commits

| Hash | Message |
|------|---------|
| `2e645a5` | chore(trellis): P0–P5 交付后的缺口任务清单（9 项） |

### Testing

- [OK] `uv run pytest -q` → 103 passed（本批为文档/任务登记，代码零改动）
- [OK] `uv run ruff check .` → All checks passed
- [OK] `uv run ruff format --check .` → 108 files already formatted
- [OK] 9 个新任务的 `task.py validate` 全部通过；`task.py list` 正常（14 个任务）
- [OK] 4 个被修改的 task.json 逐个 json.loads 校验通过

### Status

[OK] **Completed**

### Next Steps

- 批次一（门禁）：`release-gate-suites` → `security-cases`
- 批次二（体量）：`harness-evaluators`（先 2~3 个）→ `evaluator-plugin-sdk` → `mvp-case-expansion`
- 批次三（扩展面）：`semantic-trace-diff` / `case-artifacts` / `assertion-extensions`
- 可选：`case-scheduler`


## Session 4: 批次一~三：8 个缺口任务全部落地（门禁/体量/扩展面）

**Date**: 2026-09-28
**Task**: 批次一~三：8 个缺口任务全部落地（门禁/体量/扩展面）
**Branch**: `main`

### Summary

修掉三处门禁失真（Release Gate 必跑套件未校验 / 安全用例为 0 掩盖 MCP 透传缺陷 / native.status 恒 pass 稀释分母）；落定三处语义（观测不足的第三类结局 skipped、git diff 裁决不做改文件快照、归一化规则表封闭且每条配对反例）；case 级产物按 PRD §90 落地（cleanup 前采集、能力表不造占位、索引只挂宿主、越界查名一律 404）。369 tests 全绿。

### Main Changes

### Main Changes

批次一~三全部落地：8 个缺口任务、6 次功能提交 + 2 次记账提交。每一项都按 ROADMAP
的批次顺序推进，且都是"先想清楚拿什么判定，再动手"。

| 批次 | 任务 | commit |
| --- | --- | --- |
| 一（门禁） | `release-gate-suites` | `ea162a3` |
| 一（门禁） | `security-cases` | `38f3ad8` |
| 二（体量） | `harness-evaluators` + `evaluator-plugin-sdk` | `9baed6a` |
| 二（体量） | `mvp-case-expansion` | `2604520` |
| 三（扩展面） | `assertion-extensions` | `c612c74` |
| 三（扩展面） | `semantic-trace-diff` | `c1c5707` |
| 三（扩展面） | `case-artifacts` | `2714f76` |

`case-scheduler`（P3）按 ROADMAP 的裁决不做：纯重构，功能已在，风险是动全绿的
执行核心。

**三条"门禁失真"性质的缺陷被修掉**（它们不会报错，只会让 Gate 在该拦时放行）：

1. `release.yaml` 写了 `suites: [golden, regression, security, core]` 而
   `evaluate_gate()` 从不读它 → 只跑 smoke 的 run 也能拿 Release PASS。现在按
   PRD §108 强制校验必跑套件覆盖。
2. 安全评测引擎完整但零条 case 用它 → `security.max_failures: 0` 平凡通过。
   补齐用例时**实测**出 `evaluate_security` 的 `mcp_names` 恒为 None 的透传缺陷，
   `security.forbidden_mcp` 永不触发——这是"断言存在但观测面断了"的典型，单元
   测试全绿也照样漏。
3. `native.status` 对任何含扩展键的断言都产出 → 每个有扩展断言的 case 多一条
   永远 pass 的指标、稀释 Gate 分母。改为按 `_needs_status_group` 条件产出。

**三处语义决定，都写进了契约文档与规范**：

- **观测不足有第三类结局**（`assertion-extensions`，Spec §19.1）：旧实现里
  "看不到"只能塞进 PASS 或 FAIL 两类错答案。新增 `ObservationUnavailable`
  承载 `skipped`（blocking=False），与"声明形状非法"的 `UnsupportedAssertionError`
  （判 `error`）严格分开：前者要用例作者改 fixture/协议，后者要改用例。
  `max_cost` / `exit_code` 遵守 `null ≠ 0`（PRD §59）。
- **`git diff` 裁决不做**（`semantic-trace-diff`，Spec §20.4）：实测 fixture
  workdir 位于平台仓库工作树内部，`git diff` 报的是平台自己的 17 个源码文件、
  而 agent 新建的文件不可见——最该看到的一类恰好漏掉。改用 `file_state` 快照
  比对，理由与实测证据写死，避免下次重做这个判断。
- **归一化规则表是封闭的**（Spec §20.1）：每条规则配一个**不该被归一化掉**的
  对照用例。漏判比误报危险——误报让人多点一次确认，漏判让真实回归静默通过。

**`case-artifacts` 的四条主线**（PRD §90，口径在 Spec §21 新章节）：采集时机在
cleanup 之前的 `finally`（cleanup 会删 workspace 与库文件）；能力表
`UNAVAILABLE_KINDS` 如实列出采不到的五类并各给原因（含 `logs`——进程日志走
stdout 从未落盘，是结构缺口不是漏做），采不到的绝不造空文件占位；索引只有一份
（`CaseRunResult.artifacts`，`id` 就是 `case_run_id`，不建第二份映射；`case_id`/
`iteration` 由宿主给出，冗余一份会让索引与文件位置各说各话）；采集失败一律记账
不改判定（provider `snapshot()` 是对外扩展点，不该有能力把一次跑完的执行改判成
ERROR）。

### 实现中发现的问题（已记入 ROADMAP「执行中的发现」）

1. **run-level metric diff 没有噪声下限**：`latency_ms` 是 wall-clock，调度抖动
   就让均值从 0 变 0.4，于是打出一行 `regressed`。仅影响呈现层（Gate 不消费
   run-level diff，case 级走有阈值的性能判定，这个不对称像遗漏）。当下处置是
   不拿更弱的断言盖住问题，显式列为例外并写明原因。
2. **baseline 解析不看跑了哪些 suite**：`--suite smoke`（3 条）会跟最近一次 PASS
   的 `--suite golden`（24 条）比平均工具调用数。只在按 suite 分批跑时出现。
3. **case 级产物的能力缺口**（本轮新增）：`logs`/`screenshots` 的缺口与 PRD 的
   "未来"项（§88 remote 环境、日志落盘策略）绑定，不是忘了实现。

### 一处结构性坑（值得单独记）

case 产物的"原文"端点最初写成 `.../artifacts/{name}/raw`。当产物名为
`files/raw`（agent 在工作区根写出一个叫 `raw` 的文件）时，`/artifacts/files/raw`
会被路由解读成"`files` 的原文"，于是**下载得到、预览 404 说产物不存在**——
半通状态只在浏览器里才会被发现。改为平级前缀 `.../artifact-raw/{name}`，歧义在
结构上就不存在，并加了直接覆盖这个产物名的用例。

### Verification

- [OK] `uv run pytest -q` → **369 passed**（本轮 305 → 369）
- [OK] `uv run ruff check .` → All checks passed
- [OK] `uv run ruff format --check .` → 121 files already formatted
- [OK] `cd web && npm run typecheck && npm run build` → 通过
- [OK] 真实端到端：`benchmark run --tag smoke --no-judge` 跑通 →
  `artifacts/<case>/iter1/artifacts/database.sql`（678B）+ 索引登记
  `raw.trace.jsonl`；`report.json` 5KB 未嵌入产物内容
- [OK] 反例实测红：`../../../../etc/passwd` 等 5 种写法 → 404；
  索引 `path` 越界（`../outside.txt`，目标文件真实存在）→ 404

### Status

[OK] **Completed**（批次一~三全部完成，仅 `case-scheduler` 按裁决不做）

### Next Steps

- 可选：`case-scheduler`（P3，纯重构，可一直不做）
- ROADMAP「执行中的发现」三条待立项：run-level diff 噪声下限、baseline 的
  suite 覆盖约束、case 产物 logs/screenshots 能力缺口
- 每次提交都收到 Mimosa 提示"git commit 前没有得到完整扫描结论
  （project_model/python_ast_unavailable）"——**不要据此宣称项目安全**，
  需要时重跑完整审计


### Git Commits

| Hash | Message |
|------|---------|
| `ea162a3` | (see git log) |
| `38f3ad8` | (see git log) |
| `9baed6a` | (see git log) |
| `2604520` | (see git log) |
| `c612c74` | (see git log) |
| `c1c5707` | (see git log) |
| `2714f76` | (see git log) |
| `fa5f79b` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 5: case-artifacts 评审修复：记账显式化、无界读与漏判消除

**Date**: 2026-09-28
**Task**: case-artifacts 评审修复：记账显式化、无界读与漏判消除
**Branch**: `main`

### Summary

(Add summary)

### Main Changes

## 审查与修复：case-artifacts（review-workflow 全流程）

对 2714f76（PRD §90 case 级产物）执行 review-workflow：基线 c1c5707..5a20cb7、
22 文件、lint/secret 扫描、双层审查、全量回归。结论：无阻塞项，5 项缺陷 +
1 项文档漂移，全部确认后由用户授权修复（f9d4748）。

### 修复的问题（均有实测复现或直接代码证据）

1. **#I01 漏判**：`_digest` 只摘前 1MiB，2MiB 文件在 offset 1.5MB 就地改 100 字节
   被静默判"无变更"（实测复现）。改为全文件流式 sha256（`hashlib.file_digest`），
   摘要不设上限——漏判比误报危险。
2. **#I02 记账缺口**：sqlite 库被删 / dump 失败时 `snapshot()` 返回 `[]`，与
   "无产物"不可区分，而能力表标 database ✅（实测复现）。新增
   `SnapshotUnavailable` 信号，runner 按前缀区分 `snapshot unavailable:`
   （指向 agent 对环境做了什么）与 `fixture snapshot failed:`（指向 provider）；
   base 契约从"不抛异常"窄化为"不得让 run 失败"。
3. **#C01 无界读**：预览端点先 `read_bytes()` 整文件再按 512KiB 截断——trace
   产物不受 fixture 上限约束，整读等于上限名存实亡。改为只读 cap+1 字节，
   `bytes` 用 `stat` 报真实大小，读失败回 404；用 monkeypatch 锁住"不整读"。
4. **#C03 漏账**：内容省略记账原来挂在"最后一条内容产物"的 note 上，前 MAX 个
   全部读失败时完全消失。改挂到必然存在的 `files.changes.txt` 上。
5. **#C02 URL 双份构造**：前端预览用 name 自拼 URL，与 api-types 里"别自己拼"
   的注释矛盾。改为直接用服务端 `CaseArtifactRow.url`（`api.get` 以 /api 为根，
   服务端 URL origin-rooted，折算一次前缀）。
6. **#I03 文档漂移**：Spec §21.1 声称"报告与 Web 直接引用"能力表，实际只有 Web
   （真实 report.json 中 artifact 出现 0 次）。措辞收窄为 Web，缺口记入
   ROADMAP「发现的 4」——是否给 report 加产物指针留给独立增量决策。

### 验证

- `uv run pytest -q` → 375 passed（+6 回归锁）
- `ruff check` / `format --check` → 全绿（121 文件）
- web `typecheck` + `build` → 绿
- 提交 f9d4748；Mimosa 提示未获完整扫描结论，不宣称项目安全

### 未做（记录在案）

- `CaseArtifactsPanel` 145 行（⚠️ 建议拆 4 个子组件）——非阻塞，留给前端增量。


### Git Commits

| Hash | Message |
|------|---------|
| `f9d4748` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 6: 全量契约审计与修复：judge 链路 / 门禁保真 / 静默丢弃

**Date**: 2026-09-28
**Task**: PRD 与 Spec 的实现一致性审计 + 三批次修复
**Branch**: `main`

### Summary

对 PRD（V2.0.1）与 Spec（V2.2/V2.3）做了实现 vs 契约的全量核对（覆盖此前
从未审计的 PRD 前五章、§113，以及 Spec 契约层）。结论：**12 项不一致，全部在
405 pytest 全绿、ruff 干净、前端 typecheck 通过的状态下成立**。用户授权按
judge 链路 → 门禁保真 → 静默丢弃 的顺序修复。

### 为什么全绿掩盖了它们（最值得记的一点）

1. `tests/conftest.py` 的 autouse fixture `deepeval_absent` 把 `probe` 猴补成
   `{}` —— `agent.*` 在**整套测试里都不参与判定**，judge 路径从未被执行。
2. 唯一覆盖 judge 的用例用假 `deepeval` 模块，`LLMTestCase = lambda **kw: {...}`
   把 SDK 的类型约束抹平，错形状也"构造成功"。
3. 其余多数是"配置字段被解析但无消费点"或"分支条件不可达"，测试按**实现的行为**
   写断言，而不是按**契约的要求**。

### 批次一：judge 链路（PRD §34/§35/§91，Spec §2.5/§7.3）

- 多轮用了 `ConversationTestCase`，SDK 实为 `ConversationalTestCase` →
  多轮 judge metric 全部 `AttributeError`（实测 `case_runs/*.iter1.json`）。
- `tools_called` 传 dict，SDK 要求 `list[ToolCall]`。改为统一只构造 `LLMTestCase`
  （六个 `agent.*` metric 只接受这一种），多轮用 `context` 承接前序轮次。
- 工具入参改取 span 的 `attributes["arguments"]`（由 builder 落盘），不取
  `span.input`——后者 fallback 到整个 event.data，会把工具名塞进 arguments。
- `probe()` 从"查类名存在"升级为**真构造一次** `LLMTestCase`+`ToolCall`：
  只查属性存在正是上面两条硬伤能通过能力探测的原因。
- `judge_model` 此前只进配置、从不传 SDK。现 `evaluate(model=)` + Runner 按需透传。

### 批次二：门禁保真

- `tool_arguments` 只判第一次调用 → 全出现次数 + `call #N` + 连续 score +
  非字符串 JSON 序列化 + 按 §12.3 脱敏（reason 会进 PR 评论）。
- `forbidden_paths` 用子串包含 → 带边界的路径前缀匹配（`/var/etc/passwd`
  不再命中 `/etc/passwd`）。
- main-latest 候选集不看分支 → 按 §4.2 过滤 main/master 及 origin/*；
  未知分支按不匹配处理，宁可 NO_BASELINE。
- 必跑套件未覆盖归 exit 1 → 归 2（§6.1 明列，§6.4 的归因约定让归类差距有后果）。
- junit `skipped` 分支恒不可达 → `evaluated_metrics` / `is_unjudged` 接通。
- `SuiteRow.cases` 取 `len(case_ids)`，tag-only 套件恒显示 0 → 走与执行链同源的
  `select_suite_cases()`（已抽为唯一实现）。

### 批次三：静默丢弃

- `custom.*`（PRD §42 合法命名空间）与拼错 `native.*` 被判"可用"，
  随后什么都不跑也不报错 → 指名道姓的 `MetricUnavailableError` → exit 3。
- `hard_failure_categories` 被解析、被回显，求值器从不读取 → 新增求值规则，
  走 taxonomy 同一词汇表。
- `agent_version` 无任何赋值路径（永远 None）→ `RunConfig` 字段 + CLI 选项。

### 验证

- `uv run pytest -q` → **405 passed**（+28 回归锁：`test_gate_fidelity.py` 19、
  `test_baseline_policy.py` 5、`test_registry.py` +4）
- `ruff check` / `format` → 全绿
- web `bun run typecheck` → 绿
- 真实链路复验：`benchmark run database-core --suite smoke --agent fake://` 的
  两个 case 现在只报 `OpenAI API key is not configured`（认证问题），
  不再是 SDK 不兼容——类名与 ToolCall 两条硬伤确认修掉。

### 文档回填

- Spec 新增 **§22 Errata V2.3 → V2.3.1**（12 项 + §22.11 记录"为什么能长期全绿"
  与对应护栏）；§2.5 / §4.2 / §6.1 / §6.3 / §7.4 / §11.2 / §12.1 各加就地修正注。
- ROADMAP 新增「全量审计与修复（2026-09-28）」小节。

### 未做（明确排除，非遗漏）

`case-scheduler`、PRD §19 Challenge Set、§40 Nightly Profile、呈现层缺口
（ROADMAP「发现的 4」：报告不携带 case 级产物指针）。

### 安全扫描

Mimosa deep 扫描 `scan-2026-09-28T07-40-40.862Z-fce672ff8910`：0 findings，
但 `runStatus=inconclusive`（threatModel / findingDiscovery 两阶段
`partial`，165 文件全部解析成功）。**不宣称项目安全**。

### Git Commits

| Hash | Message |
|------|---------|
| (pending) | fix: 全量契约审计修复（judge 链路 / 门禁保真 / 静默丢弃） |

### Testing

- [OK] 405 passed / ruff 干净 / 前端 typecheck 绿

### Status

[OK] **Completed**

### Next Steps

- 呈现层缺口（报告携带 case 级产物指针）如需补齐，应由独立增量做（Spec §21.1）


## Session 7: ebf408c 评审复核与修复（review-workflow）

**Date**: 2026-09-29
**Task**: 对全量审计修复 commit（ebf408c）做独立复核并修复发现项
**Branch**: `main`

### Summary

对 ebf408c（681a341..ebf408c，21 文件）跑 review-workflow：审查者为该 commit
作者，因此核心判断交由独立复核流程承担。结论：4 项问题（1 阻塞 / 3 应修），
全部确认后由用户授权修复。最值得记的是 **#I01 的方向教训**：修"假阳性"时引入了
"漏判"，安全规则的两个失败方向必须一起看。

### 发现与修复（Spec §22.12 有完整记账）

1. **#I01 🔴 阻塞**：`_is_path_prefix` 的边界判定把目录式声明（`~/.ssh/`、
   `/etc/`）尾分隔符后的正常文件名当成"更长字面量"，这类写法永远匹配不到其下
   文件——上一版子串实现是命中的，属本轮引入的**反向漏判**。修法：匹配前剥去
   声明值尾部分隔符；空字面量匹配不到任何东西（把它当"前缀"理解会把每个绝对
   路径判违规，一条配置笔误放大成全员误报）。现有 fixture 无尾斜杠声明，
   影响是潜伏的。
2. **#I02 🟡**：多轮判官参照只读 `expected.output`，`expected.final` 被忽略——
   `database.query.multi_turn_refine` 声明了 `expected.final.output.contains`
   但 judge 拿到 `expected_output=None`，无参照评分。修法：参照优先取
   `expected_final.output`（Spec §2.5 在 judge 侧的落点）。
3. **#C01 🟡**：`_mask` 脱敏只接进 `exact` 分支，同一份含密钥值换 `contains` /
   `regex` 匹配器就把原文写进 reason（实测复现）。修法：三种匹配器同源脱敏。
4. **#C02 🟢**：假 `deepeval` 模块仍是 `lambda **kw`（§22.11 第 2 条的残留），
   对签名漂移免疫。换成显式形参表 + 类型校验的替身；真 SDK 测试仍是最终裁决。

### 明确不修（记账）

- `/api/suites` 计数每次请求全量装载 dataset——case 数上千时再议缓存。
- junit `skipped` 分支仍只有单测覆盖（真实 run 均 skipped=0）——补 fixture 要动
  套件组成，不应顺手做。

### 验证

- 定向：`test_gate_fidelity` / `test_registry` / `test_native_eval` → 97 passed
- 全量：见提交前记录（pytest -q）
- `ruff check` / `format --check` → 全绿
- 秘密扫描（内置正则，diff 新增行）→ 0 命中
- 工作区干净，临时文件（.review-tmp）已清理

### Git Commits

| Hash | Message |
|------|---------|
| `830d191` | (see git log) |

### Testing

- [OK] 全量 pytest + ruff

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 8: 收尾批次——发现 2 修复、§19 Challenge Set、§40 Nightly Profile

**Date**: 2026-09-29
**Task**: 按建议顺序收掉三个剩余缺口
**Branch**: `main`

### 发现 2（判定正确性）：baseline 解析不看套件组成

- main-latest 候选要求 `suites_covered` 与当前 run **全等**——不是"覆盖"：
  超集的均值同样不可比，全等才保证两侧是同一 case 总体。
- `compare_runs` 对**所有**模式（含显式/release pin）加比较期守卫：组成不全等
  → `valid=False` + 指名道姓的原因。解析期拦 main-latest 是因为候选是算出来的；
  pin 是人的决定，不替用户改主意，但比较期照旧明说不可比（§4.3 同一原则）。
- 实测复验：历史 run 里确有现行犯（smoke run 的 baseline 解析到 security run）；
  修复后新 run 解析到同为 `{smoke: 3}` 的基线。Spec §4.2/§4.3 已回填。

### §19 Challenge Set：七类各一条

- 断言全部落在真实观测面：`tool_arguments`（模糊意图/协作/冲突——"参数绑了"
  才算数）、`sql_result`（Tool Error 恢复判**重试调用带回的真实行数据**）、
  `subagent_routing` + 容器内工具（多 SubAgent）、`context_compaction` 恰好 1 次
  + 压缩后约束保留（超长上下文）、`step_efficiency` 基线 4（长链路不绕路）。
- `test_challenge_assertions_can_go_red`：拿掉行为标记后断言必须变红——
  "不是假覆盖"的直接证明。
- `challenge` 套件不进任何 gate（PRD §19 原文），测试钉住三个 gate 的 suites。
- mock agent 两处加法式扩展：`tool_error_calls`（逐调用错误粒度——全错表达不了
  "自愈"；结果载荷只挂成功调用，否则 sql_result 会把失败调用当候选）、
  `subagents`（多容器；单数形式保留兼容）。
- 词汇表没有的**不发明**：顺序敏感约束用"步数=理想值"表达，顺序断言缺口仍在
  §17.3 记账。

### §40 Nightly Profile

- 六个 agent.* 全开（threshold 0.70/0.80），**不带 native fallback**：夜间跑要
  语义全量信号，降级成确定性规则等于白跑；judge 不可用按 §6.1 记 exit 2。
- GEval（§42 custom.*）未实现故缺位——写进去会让所有 nightly run 启动期
  exit 3（§22.7 fail-fast）。profile 内注释记账，实现 PRD §42 后补列。

### 验证

- 全量 pytest：425 passed（+11：发现 2 的 2+4 条、challenge 5 条）
- 真实链路：`--suite challenge --agent fake:// --no-judge` → 7 条全过、
  NO_BASELINE 提示正确（该组成无历史基线）
- ruff check / format 全绿；web `tsc --noEmit` 绿
- Spec 新增 §23（Challenge Set 与 Nightly Profile 的判定口径）；ROADMAP
  「收尾批次」+ 发现 2 关账

### Git Commits

| Hash | Message |
|------|---------|
| `badfb76` | (see git log) |

### Testing

- [OK] 425 passed / ruff / tsc

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 9: 剩余缺口收口——发现 1、发现 4、§92

**Date**: 2026-09-29
**Task**: 修复三个已记账缺口的最后一项可行动集合
**Branch**: `main`

### 修复内容

1. **发现 1**：`MetricDiff` 方向曾逐字比较（1e-9），wall-clock 的 latency_ms 抖动
   就打假方向。现资源类 metric 的方向只在超出与 case 级性能回归**同源**的阈值
   （tool_calls 20% / tokens 25% / latency 20%）时给出；wall-clock 类加绝对判据
   （两侧均值 < 1ms → unchanged），基线近 0 时相对阈值分母失义，实测 flake
   （0.4↔0ms，±100%）只有这个诚实的结论。`test_api` 的 latency 例外摘除——
   摘除本身就是回归断言。方向语义契约按 ROADMAP 预告连带更新（阈值内=unchanged）。
2. **发现 4**：`CaseAggregate.artifacts` 指针（iteration/name/kind/path/bytes）
   进 report.json / REST Cases 行 / summary.md「现场」小节 / report.html「现场」列。
   iteration 必须进指针（摊平后宿主是多个 iteration）；指针 ≠ 能力表
   （Spec §21.1 已回写）。测试断言指针与落盘文件真实互解。
3. **§92**：`--judge-skip-policy skip_blocked`。保守双条件（非阻断才可跳、
   判定已定才可跳），跳过留痕为 skipped metric result（metadata 记 policy），
   发生在 convert 之前——连 trace 都不构造。缺省 none 行为不变。

### 文档

Spec 新增 **§24**（三缺口的判定口径）；ROADMAP「收尾批次」补三行；发现 1/4 关账。

### 验证

- 全量 pytest：**434 passed**（+9：方向语义 3、噪声下限 3、产物指针 1、§92 6 −
  重叠计数）；web `tsc --noEmit` 绿；secret 扫描 0 命中
- ⚠️ **ruff 路径读取异常（环境层，非代码问题）**：本轮中途开始，ruff 0.13/0.16
  对 `runs.py` 及任何**由 python 进程新写入**的文件报 E902（invalid UTF-8），
  而同一字节经 stdin 喂入通过、bun/git/python 读取正常、HEAD 原字节亦复现。
  判定：机器上的过滤器对本会话 python 写入的文件向 ruff 提供了损坏视图。
  **处置**：全部 11 个变更文件用 `ruff check --stdin-filename` 完成等效检查
  （0 违规）；`ruff format`（自身的读写路径）报告全部文件已格式化。
  未宣称"ruff 按路径全绿"；建议环境恢复后（重启/更新 Mimosa）重跑 `ruff check .`。

### Status

[OK] **Completed**

### Next Steps

- 环境恢复后重跑 `ruff check .` 与 Mimosa 完整扫描
