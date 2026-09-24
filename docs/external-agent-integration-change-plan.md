# 外部 Agent 平台接入变更计划

> 场景：用 harness-bench 评测自研 Agent 平台 `ai-chatbot`（Sime）。
> 状态：待评审。本文只列**要改什么**与**为什么改**，不含实现细节。
> 基线：`main` @ `9baed6a`。
>
> **验证状态**：harness-bench 侧的全部行号与行为均已对照源码核实；
> ai-chatbot 侧的事实（SSE chunk 载荷、`proxy.ts` 的 402 行为、`X-Conversation-Id`、
> 审批续跑契约）来自**源码阅读（含 Vercel AI SDK v6 的 `node_modules` 源码），
> 零实跑验证**。落地顺序的第 0 步（冒烟脚本）就是为关闭这个缺口而设，
> 在它跑通之前，B2 映射表应视为"高置信推断"而非事实。

---

## 0. 结论摘要

harness-bench 的接入形态（HTTP + SSE，PRD §6.1）与 ai-chatbot 的 `POST /api/chat`
天然吻合，Runner / 基线 / Gate / 报告 / 插件 SDK **零改动可用**。真正要改的是五类：

| 类别 | 规模 | 阻断性 |
| --- | --- | --- |
| A. harness-bench 底层缺口（观测面可用性、性能三结局、模型钉住、环境透传） | 小，但必须先做 | **A2/A3 阻断 M1**（假绿问题）；**A1 阻断 M2**（环境类断言，对 M1 的只读轨迹类不阻断） |
| B. 协议转译 shim（新增组件，落在接入侧） | 中，主要成本 | **阻断**：不做则完全接不上 |
| C. ai-chatbot 专用评测集 + profile/gate/suite | 大，且需迭代 | 非阻断，但没有它评测无意义 |
| D. 文档变更 | 小 | 非阻断，但条款不复盘则下次接入重犯 |
| E. 边界强制机制（把"框架通用"从约定升级为机制） | 小 | 非阻断，但决定这套代码是通用框架还是第一个接入方的定制件 |

A/B/C 是"能不能测"，D 是"知识沉淀"，**E 是"这套代码以后还值不值钱"**。
A 与 E 都动框架代码，但方向相反：A 补框架缺的能力，E 防框架被接入方渗透。
两者必须同批落地 —— 只做 A 不做 E，等于给框架开了通用的口子却没装门。

**阻断性按里程碑拆分**（修订：A1 原被标为整体阻断，实际只阻断环境类断言）：

- **M1（只读轨迹 + 安全类）**的真正阻断是 **A2 + A3 + E1** —— 那些断言不读文件系统，
  但会被假绿污染（`harness.retry` 恒 pass、`max_tokens` 0 假绿、词汇笔误无告警）。
  M1 期间用 shim 级约定顶住隔离（每会话独立临时 projectDir）。
- **A1（workdir 透传）阻断 M2**（`file_state` / `database_state` / `sql_result`），
  不阻断 M1。

一个反直觉的结论：**最大的风险不是"接不上"，而是"接上了却不报错"**。
ai-chatbot 有三处观测面（重试、上下文压缩、MCP/命令的分类）在转译时如果被偷懒
合并或忽略，对应指标会静默变成恒 pass —— 这正是本项目 Spec §12.1.1 与 §19.1.1
已经记录过的缺陷类别，而 AI 平台接入会把它放大到全量指标上。

### 0.1 通用 vs 定制：各类变更的归属

评审时的第一个问题是"这是不是把框架做成定制件了"。按**落点**分：

| 类别 | 落点 | 性质 |
| --- | --- | --- |
| A1 workdir 透传 | harness-bench | 通用能力补齐（fixture 隔离本就存在，只是链路断在 `SessionContext`） |
| A2 观测面 → skipped | harness-bench | 通用能力补齐（复用已有的 `metric_capability_snapshot` 机制） |
| A3 token 三键 + 性能三结局 | harness-bench + shim | **代码变更**（`native.performance` 需纳入三结局，数据模型可能要动）；纯"契约澄清"的说法在评审中被推翻 |
| A4 模型钉住 | harness-bench + shim | 通用能力补齐（复用 `RegressionComparison.INVALID` 的既有语义） |
| B 转译 shim | **ai-chatbot 测试侧** | 接入适配层，不进框架 |
| C 数据集 / profile / gate | `evals/` YAML | 纯数据 |
| D 文档 | 文档 | 条款 |
| E 边界机制 | harness-bench | 通用机制（防渗透，与具体平台无关） |

**判断 A/E 是否属于"通用"的标准**：换一个 SUT 之后这个改动还成立吗？

- A1 成立：任何进程外 SUT 都需要知道自己的沙箱在哪。`fixtures/` 里
  `FilesystemFixture` / `SQLiteFixture` 已经在为每个 iteration 准备隔离 workdir，
  生命周期 `prepare → 执行 → cleanup` 已完整运行，缺的只是"把这个 handle 交给被测方"。
  且 `SessionContext.extra` 已是不透明 dict —— 通用形状是现成的，只是无人赋值。
- A2 成立：`RunMetadata.metric_capability_snapshot: dict[str, bool]` 已存在，
  `DeepEvalCapabilityAdapter.probe()` 已在填它，`registry.resolve_metric()` 已按它降级。
  A2 只是把**同一机制**从"judge 能力"扩到"观测面能力"。
- A3 成立：`tokens` 未知时 `max_tokens` 判 `skipped` 对任何不上报用量的 SUT 都成立。
- A4 成立：模型漂移使基线失效，是所有外部 SUT 的共性问题，不是 ai-chatbot 特有。
- E 成立：校验的是 PRD §8 固定词汇表，与被测方是谁无关。

**必须承认的定制成本**：B 类 shim 是每个异构 SUT 都要有一份的适配层。
它对应 PRD §6.3 那 6 个未实现 adapter 名字背后的同一件事，
只是用"HTTP + 转译"而非"写一个 ACP adapter"来实现。这份成本不该伪装成零。

**框架与接入方的分界线**：`AgentAdapter` 四方法 + PRD §8 事件词汇表。
**只要 ai-chatbot 特有的东西全部在 shim 里归一化成 §8 词汇，框架就不感知它。**
但这条线今天没有强制机制（见 §5 E 类），所以从约定升级为机制是本次的一部分。

---

## 1. A 类：harness-bench 侧代码变更

### A1. `SessionContext.extra` 是死字段，fixture 环境从未到达被测系统 【阻断 M2，不阻断 M1】

**改什么**

- `src/agent_eval/adapters/base.py:26` — `SessionContext.extra` 已声明但**全仓库无一处赋值**，
  也无一处被消费。
- `src/agent_eval/adapters/http_adapter.py:61-68` — 构造 `metadata` 时只放
  `eval_run_id / case_id / variant_id / iteration` 四个键，`extra` 被丢弃。
- `src/agent_eval/runner/runner.py:567-572` — `_open_session()` 构造 `SessionContext` 时不传 `extra`；
  而 `runner.py:484` 为每个 case 准备好的 fixture 工作目录（`fixtures` 已按 iteration 隔离）
  就此断链。

**为什么（场景）**

ai-chatbot 的 `bash` / `read_file` / `write_file` 是在宿主机上真执行的，作用域由
`SIACT_PROJECT_DIR` / 会话的 `projectDir` 决定。于是：

1. **环境轴不存在。** `database_state` / `file_state` / `sql_result` 三个已实现的断言
   （Spec §19.2/§19.3/§19.5）判的是"fixture 库/workdir 变成了什么样"。
   fixture 造了沙箱而 SUT 看不见它，agent 的操作落在别处，这三条断言要么观测不到
   （正确结局是 `skipped`），要么观测到一个与 agent 无关的空目录（那是**假信号**）。
2. **case 之间互相污染。** 没有 workdir 透传，所有 case 只能共用同一个 projectDir。
   并发 `--concurrency 4` 下四个 agent 同时往一个目录写，报告的失败无法归因，
   重跑不复现 —— 这类不确定性与 Spec §3.1 的稳定性判定直接冲突。
3. **红队用例会真的伤到宿主。** "禁止删除文件"这类用例里，agent 拿到的路径若不是
   一次性沙箱，`rm` 打中的就是真实工程目录。这不是功能问题，是安全问题。

**修订一：阻断范围收窄。** M1 的断言（输出文本 / 工具选择 / 工具参数 / 安全硬门 /
子 agent 路由 / skill 加载）不读文件系统，A1 对它们不构成阻断。M1 期间的隔离用
shim 级约定顶住（每个会话分配独立临时 `projectDir`，迭代后清理）——
那是权宜而非修复，权宜不解决 fixture 与 agent 看到同一目录的问题，
所以 M2 之前 A1 必须落地。

**修订二：同位性前提（原文缺失）。** workdir 透传传的是**路径字符串**，
隐含前提是 harness 与 SUT 共享文件系统（或共享挂载点）。本机同 Windows 成立；
但 ai-chatbot 的部署形态之一是 Docker（`Dockerfile.agent`），容器内路径与宿主路径
对不上时 A1 **静默失效**——SUT 拿到一个它无法访问的路径，行为退化为"环境轴不存在"，
且没有任何报错。因此 A1 的 health 阶段必须包含 workdir 可达性探测：
`create_session` 携带 workdir 后，SUT 应回执"可写"与否，不可写按 `InfraError`
处理（exit 2），把"配置错"与"agent 失败"分开。

**修订三：断言面污染。** ai-chatbot 会往 `projectDir` 写自己的产物
（`outputs/<conversationId>/`）。`file_state` 若断言"目录里恰好是这些文件"，
agent 产物会掺进来。用例要么把断言范围限定在 fixture 子集，要么 shim 在比对前
过滤已知产物目录 —— 这必须写进 C 类用例作者指南，否则 `file_state` 会无端 FLAKY。

**验收**

- `mock_server.py` 记录 `create_session` 的 metadata，新增用例断言其中含 workdir；
- 端到端：FakeAgent 全链路用例中，agent 侧能读到 workdir 并往里写文件，
  随后 `file_state` 断言能判 pass/fail（而非 skipped）；
- workdir 不可达（模拟容器路径）时，run 以 `InfraError` 收场而非假绿。

**注意**：workdir 属**会话级**而非轮级。`AgentRequest`（`base.py:34`）只有
`message` / `stream`，不建议为它加逐轮环境参数 —— 那会把 PRD §7.3 的协议面扩大一圈，
而收益为零。SESSION 级足够表达"这个 case 的沙箱在哪"。

---

### A2. 观测面不可用 ≠ 指标通过：`harness.*` 插件缺"能力协商" 【阻断 M1】

**改什么**

- `src/agent_eval/evaluators/harness.py` — `RetryEvaluator`（`harness.retry`）与
  `ContextCompactionEvaluator`（`harness.context_compaction`）只能从"声明侧参数是否缺省"
  判断 skipped，无法从"观测侧是否具备"判断。
- `src/agent_eval/models/profile.py` / `evaluators/registry.py` — 需要一条让 SUT
  声明自身事件能力、或让 profile 显式排除不受支持指标的通道。
- 落痕面：`RunMetadata.metric_degradations`（已有）应扩展到这类"观测面缺失"。

**修订：能力声明必须在 health 阶段，且 runner 阶段顺序要为此调整。**
初版方案写的是"能力声明随 `create_session` 上报"，这与代码时序不兼容：
`_resolve_profiles`（`runner.py:230`，**metric 降级决策在这里发生**）先于
`health_check`（`:247`），更远先于 per-case 的 `create_session`（`:263` TaskGroup 内）。
声明到了 `create_session` 才出现，`resolve_metric` 早已跑完，降级无从发生。

正确落点是 **`health_check` 阶段**（与 `DeepEvalCapabilityAdapter.probe()` 的时机同构，
那是框架已有的第二次能力探测），为此 runner 的顺序调整为
**health（含能力探测）→ `_resolve_profiles`**。这个调整本来就该做：
现状 health 失败发生在基线解析（`:245`）之后，一次注定失败的 run 还会先解析基线。

能力声明的内容是一张"观测面 → 是否具备"表，由 adapter 返回
（扩展 `HealthStatus` 或新增返回值），落 `metric_capability_snapshot`。
每个 `harness.*` 插件需要声明自己依赖的观测面
（如 `RetryEvaluator.required_events = ("retry",)`），
`resolve_metric` 据此统一降级 —— 插件自己不感知 SUT 是谁。

**为什么（场景）**

`evals/profiles/smoke.yaml` 里 `harness.retry` 的 `params.max_retries: 0`，
语义是"任何重试都算超标"。但 ai-chatbot 的 provider 重试只写日志
（`packages/core/src/foundation/model/retry-middleware.ts`），**从不发 `retry` 事件**。
结果是：观测到 0 次重试 → `0 <= 0` → **pass**。

报告上"没有重试"与"根本看不到重试"长得完全一样。同理：

| 指标 | ai-chatbot 观测面 | 现状下的判决 |
| --- | --- | --- |
| `harness.retry` | 无 `retry` 事件 | 恒 pass（假信号） |
| `harness.context_compaction` | 只有 `lastCompactionFreed` 一个数字 | 恒 pass（假信号） |
| `harness.mcp_permission` | 有（`mcp__*` 工具名），但需 shim 拆出 `mcp.call` | 合并则恒 pass |
| `harness.subagent_routing` | 有（`data-sub-open`/`data-sub-done`） | 靠谱 |
| `harness.skill_load` | 有（`use_skill` 工具调用） | 靠谱 |

这不是新问题：`native.ObservationUnavailable` 与 Spec §19.1.1 已经把
"满足 / 不满足 / 观测不到" 三结局定死了。**缺口在于这条规则目前只覆盖扩展断言键，
没有覆盖 `harness.*` 插件。** 本次接入要求把 §19.1.1 从"扩展键规则"升级为
"所有 metric 的通则"。

**验收**

- 一条用例：SUT 不发 `retry` 事件时，`harness.retry` 产出 `verdict=skipped`
  且 `blocking=False`、`metadata.skipped_reason="observation_unavailable"`，
  **不是** pass；
- `metric_degradations` 里留痕，报告与 Web UI 都能看出"这条没测"；
- 反向用例：SUT 发了 `retry` 事件且超过 `max_retries` → FAIL（防止修成恒 skipped）；
- **时序用例**：能力声明出现在 health 阶段、`_resolve_profiles` 消费它 ——
  用一个"health 声明无 `retry` 观测面"的 mock adapter，断言降级发生在
  启动期而不是任何 session 创建之后；
- `FakeAgentAdapter` / `dev/mock_server.py` 声明全量观测面，既有 207 个测试
  **不因本改动变红**（它们的行为不应改变，只多一张能力表）。

---

### A3. Token 缺失会让 `native.performance` 恒 pass —— 三结局必须覆盖性能组 【阻断 M1】

**修订说明**：初版把这条定性为"契约澄清 + shim 义务"，评审推翻了这一定性 ——
框架的数据模型本身就无法表达"token 未知"，这是代码变更。

**改什么**

- `src/agent_eval/models/results.py` — `TurnResult.tokens` / `CaseRunResult.token_count`
  均为 `int`（缺省 0），`native.py` 的 `EvalScope.tokens: int = 0` 同。
  框架**无法表达"用量未知"**，缺失一律坍缩为 0。
- `src/agent_eval/evaluators/native.py:176-177` —
  `if limits.max_tokens is not None and scope.tokens > limits.max_tokens`：
  usage 缺失时 `scope.tokens == 0`，`0 > limit` 为假 → 不追加 problem → **pass**。
  这与 `harness.retry` 的 `0 <= 0`（`harness.py:49`）是**同一种病，发生在 native 侧**。
- 修法二选一，评审定夺（倾向前者，改动面小且语义正确）：
  1. **判定侧**：`EvalScope` 增加"用量已观测"标志（或 `tokens: int | None`），
     usage 全缺时 `native.performance` 组对 `max_tokens` 产出
     `verdict=skipped`（`ObservationUnavailable`，复用 Spec §19.1.1 的既有机制）；
  2. **数据侧**：`tokens` 全链路改 `int | None`，报告/聚合/Web UI 同步
     （`quality-guidelines.md` 第 7 条的 None≠0 约束到处都要守，改动面大）。

**为什么（场景）**

ai-chatbot 的流上唯一的用量数据是 `data-context-usage`，它只带
`actualInputTokens` / `cachedTokens`，**没有 output token**。若 shim 原样透传：

- `max_tokens` 约束恒 pass（上面的 0 假绿路径）；
- `tokens.max_regression_percent` 只对输入侧敏感；
- 趋势图上"输出成本降为零"的假象，正是 `quality-guidelines.md` 第 7 条警告的方向。

**修订：output token 的补齐路径有一个架构代价，初版低估了。**
"主循环走 `cost` 表"意味着 shim 需要**直连 ai-chatbot 的数据库**——
这打破了"shim 只依赖 HTTP 面"的干净边界，且 `DB_DRIVER=postgres` 时还要多一份连接配置。
三个选项，按推荐排序：

1. **接受单侧 + skipped**（推荐，M1 就够用）：input/cache 来自流，output 缺失，
   按 A3 判定侧方案落 `skipped`；token 回归阈值只校 input 侧，报告注明口径。
2. **shim 读 `cost` 表**：数据最准，但引入 DB 依赖与轮询时机问题
   （`cost` 在会话 dispose 时落库，run 结束时不一定已写）。
3. **text-delta 累计估算**：无额外依赖但不是真实值，会把"估算"伪装成"测量"，
   违反本仓库"没有观测来源就不得评测"的底线，**不建议**。

**验收**

- usage 全缺的 case：`max_tokens` 约束产出 `skipped` 而非 pass；
- usage 半缺（有 input 无 output）：按选定方案处置并在 reason 里写清缺哪一侧；
- 若启用成本阈值，先决定 `evals/pricing.yaml` 与 ai-chatbot `cost` 表谁是事实源。

---

### A4. 模型钉不住，基线比对会被静默污染 【阻断回归主张，M3 前】

**为什么（场景）** —— 初版完全遗漏，评审补入。

回归比对的价值前提是"两次 run 之间只有被测变更在变"。但 ai-chatbot 的模型配置
在它**自己的数据库**（`model_config` 表）里，`POST /api/chat` 可用 `modelConfigId` /
`modelName` 覆盖。而 harness 侧的 `RunMetadata.agent_model` 只是个**无人校验的标签**
（CLI `--model` 填的）。于是：两次 run 之间有人改了 ai-chatbot 的默认模型配置，
基线就是苹果比橘子——token、延迟、通过率全变，报告归因为"回归"，
实际是"换了个模型在跑"。这直接打在"回归平台"的核心主张上，且**不会有任何告警**。

**改什么**

- **SUT 侧（shim 义务）**：`run` 请求显式携带 `modelConfigId` / `modelName`
  （不依赖平台默认值），并在能力/health 上报"实际生效模型"；
- **框架侧（通用机制）**：`health_check` 或 `run.started` 携带实际模型标识，
  写入 `RunMetadata.agent_model`；**基线比对时若两侧 `agent_model` 不一致，
  `RegressionComparison` 判 `INVALID`**——复用其对 dataset 版本不一致的既有语义
  （Spec §3.3），不发明新机制。

**验收**

- 两次 run 标签不一致时，`compare_runs` 产出 `INVALID` 且 `invalid_reason`
  写明"agent model mismatch"，gate 对应规则落 `undetermined` 而非 FAIL/PASS；
- shim 发出的每个 `run` 请求都带显式模型字段（不靠平台默认值）。

---

## 2. B 类：协议转译 shim（新增组件）

shim 是本次接入的主体工作量。它不进 ai-chatbot 的生产代码路径，应是独立的
测试侧组件（建议置于 ai-chatbot 的 `tests/` 或独立目录，不参与其构建产物）。

### B1. 端点映射

| harness-bench 期望 | ai-chatbot 现状 | shim 处置 |
| --- | --- | --- |
| `GET /health` → `{"status":"ok"}` | **无** | shim 自建，且必须真检查 license、模型配置、workdir 可达性（A1 修订二/A4），并**在此上报观测面能力表与实际生效模型**（A2 修订/A4：能力声明在 health 阶段） |
| `POST /api/agent/sessions` → `{"session_id":...}` | 无此端点，但 `conversationId` 由调用方自选 | shim 分配 id 并接受 metadata（含 A1 的 workdir，回执可达性） |
| `POST .../{id}/run` → SSE 事件流 | `POST /api/chat`（UIMessage stream） | **核心转译**，见 B2；请求显式携带 `modelConfigId`/`modelName`（A4，不依赖平台默认值） |
| `POST .../{id}/cancel` | `POST /api/chat/{conversationId}/stop` | 直连 |

`health_check` 不能只探活：ai-chatbot 的 `proxy.ts` 让所有 `/api/*` 过 license 校验，
无有效 serial 一律 402。若 health 报 ok 而 run 报 402，就会被 `http_adapter.py:88-90`
归为 `InfraError` → exit 2，运维看到的是"基础设施故障"而不是"许可证过期"。
health 必须把这两件事的区别暴露出来。

### B2. 事件映射表（转译契约）

| PRD §8 事件 | ai-chatbot SSE 来源 | 必须注意 |
| --- | --- | --- |
| `run.started` / `run.finished` | `start` chunk / `finish` chunk + `[DONE]` | 缺 `run.finished` 会被 `runner.py:809-812` 判 agent 失败，必须可靠发出 |
| `tool.call` / `tool.result` | `tool-input-available` / `tool-output-available` | `input` 已是解析后的对象，直接作 `data.arguments`；**参数类与安全类断言全靠它** |
| `tool.result`(错误) | `tool-output-error` / `tool-output-denied` | `denied` 的载荷只有 `toolCallId`，**没有原因**，reason 需自行补 |
| `mcp.call` / `mcp.result` | `toolName` 形如 `mcp__<server>__<tool>` | **必须拆成独立事件**，不得折叠进 `tool.call` |
| `command.started` / `command.finished` | `bash` 工具调用 | 退出码在 output 对象的 `exitCode` 键里，需提到 `data.exit_code`（Spec §19.4 的唯一观测来源） |
| `subagent.started` / `finished` | `data-sub-open` / `data-sub-done` | 按 `id === toolCallId` 关联；含 `tokenUsage` / `status` |
| `skill.loaded` | `use_skill` 工具调用 | 也可走 `skill.loaded` 显式事件 |
| `model.response.data.usage` | `data-context-usage` | 只有输入侧，见 A3 |
| `error` | `error` chunk 或 `finishReason:"error"` | 注意 `errorText` 可能被自愈逻辑抑制为空串 |
| `retry` | **不可观测** | 见 A2，应落 skipped |
| `context.compaction.*` | **不可观测** | 见 A2，应落 skipped |

**为什么把 MCP/command 单列**：`security/evaluator.py:152-172` 明确要求
`tool_names` / `mcp_names` / `command_calls` 三路**逐条透传**，
注释里写了原话 —— `forbidden_mcp` 曾因 runner 不传 `mcp_names` 而恒 pass。
ai-chatbot 的 MCP 调用在流上就是普通工具调用，只要 shim 图省事不分流，
红队用例会**全部变绿**，而 `security.max_failures: 0` 会因此平凡通过
（ROADMAP 里 P0 任务 `security-cases` 要解决的正是同类问题）。

### B3. 人机审批策略（必须显式且可追溯）

ai-chatbot 遇到 `behavior:'ask'` 的工具或 `ask_user_question` 时，发一个
`tool-approval-request` **然后 SSE 正常结束**（`finish` + `[DONE]`），
等待完全在客户端，服务端不阻塞、无超时。要续跑必须**重新 POST** 一条
`state:'approval-responded'` 的 assistant 消息。

harness-bench 的协议里没有"等人"这个概念，`run.finished` 一到就结算。因此 shim 必须
定一个自动策略（预置 `allow` 权限规则 / 自动批准 / 自动拒绝），并且：
**策略必须写进 `SessionContext` 的 metadata 或 run 元数据，在报告里可追溯。**
否则同一份用例在不同策略下测出来的是两个不同的系统，跨 run 比对失去意义。

**修订：审批的"答案"是测试输入，不只是执行策略。**
`ask_user_question` 的回答会进入对话上下文并**改变 agent 的后续行为**——
答案内容不是 shim 的实现细节，是**用例作者必须设计的东西**。
一条多轮用例如果中途会触发提问，作者必须决定"用户答什么"，
且这个答案要能被 shim 在正确的时机注入（`approval.reason` 字段，
`ask_user_question` 的答案是 `JSON.stringify({answers})`）。
没设计答案的用例跑出来的是"agent 在等一个随机回答"，稳定性无从谈起。
这项要求写进 C 类用例作者指南（见 §3 修订）。

### B4. 并发与副作用

- **并发**：ai-chatbot 有 `CHAT_MAX_CONCURRENCY=5` / 队列 30，超限时发
  `{type:'error', errorText:'服务器繁忙，请稍后重试'}`，这会被判成 agent 失败。
  shim 与 `--concurrency` 都必须压在队列容量之下，且这个上限应记录在 run 元数据里。
- **副作用**：agent 会往 `<projectDir>/outputs/` 写产物与日志。shim 解析轨迹时
  不得把 projectDir 当工作区，且每个 case 的迭代后要清理。

---

## 3. C 类：ai-chatbot 专用评测集

`evals/datasets/database-core/` 的 31 个 case **不可复用**：它们假设的工具名是
`execute_sql` / `shell_exec`，fixture 是 `sales_v2`。ai-chatbot 的真实工具面是
`read_file` / `write_file` / `edit_file` / `bash` / `grep` / `glob` / `ls` /
`web_search` / `use_skill` / `agent` / `mcp__*` / `<connector>_*`。

**新建 dataset 的硬约束**

1. case 里的工具名必须与 ai-chatbot 实际发出的 `toolName` 逐字一致 —— 拼错的后果是
   `tools.required` 恒 FAIL（显性）或 `tools.forbidden` 恒 pass（隐性）。
2. fixture 只能落在已实现能力内（`filesystem` / `sqlite`）；`postgres` / `git` 在
   `get_provider()` 里显式 raise（Spec §18.4）。走 connector 的 SQL 场景需要另行设计。
3. 遵守 Spec §18 的覆盖约束：每维度至少一条**负向** case，且负向必须真的红；
   `golden` 只收正向（否则 `golden.required_pass_rate: 1.0` 永远 FAIL）。
4. profile 需按 A2 的结论裁剪：`harness.retry` / `harness.context_compaction`
   在 shim 能提供观测面之前，应在 profile 里显式排除或标记，不得留着静默 pass。
5. gate 的 `suites` 必须真有对应套件文件（Spec §15.5、ROADMAP P0 `release-gate-suites`
   的教训：YAML 里声明了却无人读的字段就是装饰品）。
6. **（修订）审批答案属于用例设计**：会触发 `ask_user_question` / 审批的多轮用例，
   作者必须声明"用户答什么"（见 B3）。答案进上下文、改变后续行为，
   没设计答案的用例测的是随机性。
7. **（修订）断言风格适配非确定性**：ai-chatbot 跑真实 LLM，输出逐字不可复现。
   用例默认用 `contains` / `regex` / `tools.required` / `tool_arguments` / 步数上限这类
   **结构性断言**；`exact` 只用于固定回显类。`repeat` 处理残余抖动，
   但 repeat 救不了写得含糊的断言。
8. **（修订）超时必须按场景校准**：`execution.timeout` 默认 120s，而 ai-chatbot 的
   子 agent 场景实测可达 5 分钟一单、`/api/chat/generate` 的 `maxDuration` 是 300s。
   含子 agent 的用例显式给 `timeout: 360` 以上，否则测的是"超时"不是"行为"。
9. **（修订）`file_state` 断言要过滤 agent 产物**（见 A1 修订三）：
   ai-chatbot 往 projectDir 写 `outputs/`，断言要么限定 fixture 子集要么过滤该目录。

**首版建议只做确定性部分**（`--no-judge`）：工具选择 / 工具参数 / 输出断言 /
步数效率 / `native.status` / 安全硬门 / 子 agent 路由 / skill 加载。
judge 指标（`agent.task_completion` 阈值 0.70，带 native 兜底）留到第二版，
因为它需要先有稳定的确定性基线才能校准。

**（修订）运行预算先算再跑**：真实 LLM × 30 case × repeat × 多轮，一次全量的
token 成本与墙钟时间在写第一版 dataset 之前就要估出来（ai-chatbot 本地库里有
历史 `cost` 数据可参考：单会话成本从几美分到一美元以上不等）。建议从
10 case × repeat 2 的小集起步校准断言，再扩到全量——直接上全量会先烧掉预算
再发现断言写错。

---

## 4. D 类：文档变更（本文的重点交付）

评审时要看的不是代码 diff，而是**这些文档条款是否被接受**。

| # | 文档 | 改什么 | 为什么（场景） |
| --- | --- | --- | --- |
| D1 | `docs/agent-evaluation-regression-platform-engineering-prd-v2.md` §7.2 | 明确 `metadata` 允许携带扩展键，并定义 `workdir` 的语义 | A1 要求 fixture 环境透传；不改则协议里没有它的位置，实现会各行其是 |
| D2 | 同上 §6.3 | 把"外部自研平台接入"从未来工作提升为**已定型形态**，注明 shim 模式 | 现状只列了 6 个未实现的 adapter 名字，没有说明"外部平台用 HTTP 够用、需自备转译层"，后来者会以为必须写 ACP/CLI adapter |
| D3 | 同上 §3.2 非目标 | 补一条：不代管被测平台的容器化与数据播种 | ai-chatbot 无容器隔离、无环境重置能力，边界不说清会变成无限责任 |
| D4 | `docs/agent-eval-engineering-spec-v2.1.md` §19.1.1 | 把"三结局（满足/不满足/观测不到）"从**扩展断言键**升级为**所有 metric 的通则**，明确 `harness.*` 插件**与 `native.performance`** 同样适用 | A2/A3 的直接依据。不改则 §19.1.1 读起来只约束扩展键，插件作者会认为恒 pass 是可接受的；`max_tokens` 的 0 假绿（`native.py:176`）是同病异侧 |
| D5 | 同上 §17.3 | 补"外部接入侧的观测缺口"小节，登记 retry / compaction 在本次接入中不可观测 | 现有 §17.3 只记录内部 evaluator 的 8 项缺口，外部接入的缺口无处可记 |
| D6 | 同上 §12.1.1 / §12.4 | 把"观测面逐条透传"与"覆盖缺口必须可见"扩写为**外部接入方义务** | 原条款是写给内部 runner 的；接入方（shim 作者）才是新的漏传来源 |
| D7 | 新增 `docs/external-agent-integration-guide.md` | 接入指南：事件映射表（本文 §B2）、审批策略、观测面能力声明、并发上限 | 第一次接入必然踩这些坑，而知识现在只散落在两个仓库的源码注释里，没有可交付给接入方的契约文档 |
| D8 | `.trellis/spec/backend/quality-guidelines.md` | 新增两条硬约束：(1) 外部映射必须逐观测面拆分，MCP/command 不得折叠进 `tool.*`；(2) 未被 SUT 观测能力覆盖的 metric 必须在 profile 显式处置，不得静默 pass | 把 A2/A3/B2 的结论固化成评审时能引用的条款，否则下次接入会原样重犯 |
| D9 | `.trellis/tasks/ROADMAP.md` | 登记本次接入的 7 个切片：观测面能力（A2）、性能三结局（A3）、模型钉住（A4）、**转译 shim（B，最大单项工作量，初版漏登记）**、专用评测集（C）、边界机制（E）、接入指南（D7）；A1（workdir）随 M2 登记即可 | 现有 ROADMAP 九项里没有"外部接入"，不登记则不会被排期；初版切片清单不含 B，等于最大的活不在排期表里 |
| D10 | `README.md` | 补"评价一个外部平台"的最小路径：起 shim → 配 profile → 跑 benchmark → 读报告 | 现有 README 的快速开始只有 `fake://` 与 mock server，没有对外平台的走法 |
| D11 | **ai-chatbot 侧** `README.md` / `AGENTS.md` | 补"如何被外部测试驱动"：本地起服务的 env、license 前置、`POST /api/chat` 的调用契约（含必须自带 `conversationId`） | 该仓目前没有说明如何本地起服务供外部测试，交接成本高 |
| D12 | **ai-chatbot 侧** 新增接入说明 | 公开 `tool-approval-request` 的语义（轮次即结束、需重新 POST） | 该语义现在只在 `docs/zombie-approval-contracts.md` 与源码里，对外部集成方不可见；B3 的策略设计依赖它 |
| D13 | `docs/agent-evaluation-regression-platform-engineering-prd-v2.md` §8 | 明确 `EVENT_TYPES` 是**闭合词汇表**（新增事件类型必须走框架升级，不得由接入方自行扩展），并说明校验后果 | E1 的依据。现在 `EVENT_TYPES` 全仓库零消费（`models/events.py:11`）、`type` 是裸 `str`（`:51`），读文档的人无法知道"自定义事件名"是被允许还是被禁止 |
| D14 | 同上 §6.2 / §7.2 | 写明 adapter 的三项义务：**能力声明**（哪些观测面存在，**在 health 阶段**）、**事件归一化**（平台方言必须转成 §8 词汇，不得透传）、**环境回执**（收到 workdir 后回报可达性） | E2/E5/A1 修订二的依据。这些现在是隐含要求，不写进协议面就会被当成"实现细节"，而它们是"框架保持通用"的唯一保障 |
| D15 | `docs/agent-eval-engineering-spec-v2.1.md` §6.1 | 补 exit 2 的一个触发面：协议词汇违约（未知事件类型）在升级开关打开时的归属 | 现有 §6.1 把 exit 2 定义为"gate 无法可靠求值"。协议违约让观测面失效，正是同一语义；不写清则实现者会把它当 AGENT_FAILURE（exit 1）处理，冤枉被测方 |
| D16 | 同上 §3.3 | 把"基线 INVALID 的触发面"从 dataset 版本不一致**扩展到 `agent_model` 不一致**，写明 `RunMetadata.agent_model` 从此是受校验字段而非自由标签 | A4 的依据。不改则模型漂移静默污染基线，回归平台的核心主张落空 |

---

## 5. E 类：边界强制机制（把约定升级为机制）

**要解决的问题**：框架的通用性目前**没有任何机制保障**，只靠"实现的人记得别乱写"。
下一个接入方、或者半年后的自己，很容易在框架里加一个只为某个平台服务的分支，
而这一切都不会让任何测试变红。

证据是现成的两处：

- `src/agent_eval/models/events.py:11` 定义了 `EVENT_TYPES`（PRD §8 的固定词汇表），
  **全仓库零处消费**（`grep EVENT_TYPES src/ tests/` 只命中定义处）。
- `src/agent_eval/models/events.py:51` 是 `type: str` 而非 `Literal`，
  `adapters/sse.py:16-28` 只校验 JSON 形状与 pydantic 字段，**不校验 `type` 取值**。

后果：shim 若把 `mcp.call` 写成 `mcp_call`（下划线笔误），整条流照常解析，
`mcp.calls` 恒空，`security.forbidden_mcp` 与 `harness.mcp_permission` 恒 pass，
run 全绿、exit 0、报告没有任何异常。**这是一个不会自己暴露的缺陷**，
与 Spec §12.1.1、§19.1.1 记录的是同一类问题。

以下五条机制覆盖五类渗透路径。E1/E2/E3 是新增，E4/E5 是把已有形状冻结下来。

### E1. 消费 `EVENT_TYPES`：未知事件类型必须可见

**机制**：观测到的 `event.type` 若不在 `EVENT_TYPES` 内 →

- **默认**：新增一个 run 级计数（建议名 `protocol_violations`）记录违规类型与次数，
  并写入既有的 `aggregate.warnings`（`reports/aggregate.py:101` 已有该字段，
  CLI `benchmark_cmd.py:91` 与 `report.json` 已消费），**可见但不阻断**；
- **升级**：由**独立的显式开关**决定是否升级为 `InfraError`（exit 2），
  例如 `--strict-protocol` 或 profile 级配置。

**【开放设计问题，交评审定，不预设结论】默认值选 warn 还是 strict？**

- **warn 默认**（初版立场）：SUT 事件协议升级时不至于打断流水线；
  风险是 CI 里不红的东西会烂掉。
- **strict 默认 + 逃逸口**（`--allow-unknown-events`）：闭合词汇表本应硬执行，
  发现笔误当次就拦；风险是框架词汇表升级前，正常演进的新事件类型会让 run 红。
- 两个方向都有正当理由，**本方案不替评审做这个决定**。但无论选哪个，
  "可见"是底线 —— 不存在"既不阻断也不记录"的选项。

**两个必须避开的实现陷阱**

1. **不要复用 Gate 的 `strict` 字段**。该字段已有确定语义
   （"任何 blocking FAIL 都阻断"，Spec §15 与 `release.yaml` 都在用），
   把"协议严格性"也塞进去，会让 Release Gate 的行为取决于一个与门禁无关的维度，
   属于本仓库 quality-guidelines 第 1 条警告的"字段语义被稀释"。
2. **不要把它做成另一条恒 pass 的声明**。如果这个开关默认关、且没人会打开，
   那它就等于不存在 —— 与 `suites` 曾在 YAML 里躺了整个 P4 是同一类问题。
   默认行为必须是"至少可见"，升级开关只是给愿意承担红线的团队用。

**为什么不把 `type` 改成 `Literal`**：PRD §8 会演进（本项目自己就在加
`context.compaction.*`）。Literal 会把"框架该升级词汇表"错判成"接入方违约"，
而后者是 exit 2 —— 一个平台升级事件协议就会让整条流水线红掉。
`EVENT_TYPES` 是**声明式清单**，新增事件类型 = 改这一处，路径必须保留。

**为什么这条是纯通用的**：它校验的是 PRD §8 的固定词汇表，与被测方是谁无关。
它拦的不是"ai-chatbot 特有事件"，而是**任何**接入方的笔误与方言泄漏。

**验收**：一条用例喂入 `type="mcp_call"` 的 SSE 帧，断言 run 产出 warning
且文本里出现违规类型名；另一条断言开启升级开关后同一输入得到 exit 2。

### E2. 观测面能力由接入侧声明，框架不得推断

**机制**：能力声明在 **`health_check` 阶段**由 adapter 上报（不是
`create_session` —— 那晚于 `_resolve_profiles` 的降级决策，见 A2 修订）→ 落
`RunMetadata.metric_capability_snapshot`（`models/run.py:67` 已有该字段）
→ `registry.resolve_metric()` 按它统一降级为 `skipped`。
A2 复用**同一机制**，不新增概念。

**两条必须拒绝的反模式**：

1. **框架里出现被测方标识的分支**：

   ```python
   # 坏 —— 这就是定制化的定义：第二个平台要再加一个分支
   if "ai-chatbot" in endpoint:
       return skipped("该平台不上报 retry")
   ```

   正确形状是判断完全由声明驱动，框架侧对任何 SUT 走同一段代码。

2. **从"事件没出现"反推"观测不到"**：`retry` 事件 0 次与"根本不发 `retry` 事件"
   在数据上同形 —— 这正是 A2 要解决的问题本身。能力必须由接入侧**事先声明**
   （与 `DeepEvalCapabilityAdapter.probe()` 同构），不能从观测数据推断。

### E3. 源码级边界断言（沿用仓库既有先例）

仓库里已经有这个手法的先例：`tests/test_evaluator_plugin.py:301-305` 用
**读 `runner.py` 源码、断言其中不含任何具体插件名**来证明 PRD §109.4 的可扩展性
（注释原话：这个证明必须落在测试里，否则"可扩展"只是文档承诺）。
框架通用性用同一手法守住即可。

**机制**：维护一份**专有方言清单**，测试断言框架源码不含其中任一项。
清单初版（来自本次接入的实测）：

```text
ai-chatbot          平台名
mcp__               工具命名空间分隔符
data-sub- / data-context-usage / data-task    自定义 data part
tool-approval-request / tool-output-denied    UIMessage stream 方言
projectDir / conversationId                   ai-chatbot 的会话字段名
SIACT_ / siact                               其环境变量与目录前缀
```

**扫描范围**：`src/agent_eval/`。`dev/` 与 `tests/` 可豁免
（`dev/mock_server.py` 是测试替身，允许出现具体形状）。
清单本身是通用的 —— 它就是一份"专有方言"登记表，不含平台逻辑。

**判断标准（写进清单的注释里）**：**这个标识在换一个 SUT 之后还成立吗？**
成立 = 通用，不成立 = 越界。这条比清单本身更重要，清单会过时，判断标准不会。

**（修订）诚实声明这条机制能拦什么、拦不住什么。**
E3 是 deny-list，它拦得住**字符串渗透**（平台名、字段名、方言前缀），
拦不住**语义渗透**：例如有人在 `security/evaluator.py` 里写
"工具名含双下划线就拆出 server 名"—— 这是纯 ai-chatbot 语义，
却不含清单里的任何字符串，测试照样绿。语义渗透的下游症状会被 E1 捕获
（拆出来的名字若不是合法 §8 事件/观测面，词汇校验会响），
但"合法地表达了平台专属语义"这种情况只有评审能拦。
所以 E 类的准确定位是：**把"完全靠自觉"升级为"有弱机制兜底 + 评审焦点集中"**，
不是"机制保证"。宣称更强就是误导评审。

### E4. `extra` 保持不透明透传

**机制**：A1 只做转发，键名由接入方定义，框架不解其义。

**必须拒绝的反模式**：把 `SessionContext.extra` 换成具名官方字段
（`project_dir: str`）。一旦官方字段出现，第二个 SUT 的环境概念不同就得再加一个，
`SessionContext` 会退化成各平台字段的并集 —— 这正是 E2 反模式 1 的字段版本。
E3 的清单覆盖 `project_dir` / `projectDir` 这类命名。

### E5. 归一化责任在接入侧（写死，不留解释空间）

以下三件事**全部在 shim 里完成**，框架只认 PRD §8 词汇：

| shim 输入 | shim 输出 |
| --- | --- |
| `toolName` 形如 `mcp__<server>__<tool>` | `mcp.call` / `mcp.result` |
| `data-sub-open` / `data-sub-done` | `subagent.started` / `subagent.finished` |
| `tool-approval-request` + 自动审批策略 | run 级策略元数据（见 B3） |

**必须拒绝的反模式**：把 `ai_chatbot.context_usage` 加进 `EVENT_TYPES`、
再在 `native.py` 里读它。那样 `EVENT_TYPES` 会变成跨平台杂烩，
`mcp__*` 的解析规则也会渗进 `security/evaluator.py`。

### 机制总览

| 机制 | 拦的是什么 | 落点 | 新增? |
| --- | --- | --- | --- |
| E1 消费 `EVENT_TYPES` | 方言泄漏 / 笔误 → 观测面静默失效 | `models/events.py` 消费点 + `aggregate.warnings` + 独立升级开关 | 新增 |
| E2 能力声明来自接入侧 | 框架里长出"平台判断"分支 | 复用 `metric_capability_snapshot` + `resolve_metric` | 新增 |
| E3 源码级边界断言 | 框架代码渗透被测方标识 | 新增测试（仿 `test_evaluator_plugin.py`） | 新增 |
| E4 `extra` 不透明 | 官方字段被平台方言污染 | 冻结 `adapters/base.py:26` 现有形状 | 冻结 |
| E5 归一化在接入侧 | 框架词汇表被平台方言撑大 | 契约条款（D7/D8/D14） | 冻结 |

### 关于 E3 清单的一点说明

清单会过时（平台改名、方言迭代），所以**判断标准比清单重要**：
"这个标识在换一个 SUT 之后还成立吗？" 建议把这句话写进清单文件的注释里，
让后来者知道该往清单里加什么、以及为什么。

反过来，E3 有一条必须守住的边界：**清单里只登记"专有方言"，不登记机制**。
例如 `metadata` 这个键名是 PRD §7.2 定义的通用形状，它在清单里**不该出现** ——
否则会有人把"框架里出现 metadata 这个词"也判成越界，
把一条防渗透的测试变成阻碍正常开发的噪音。

---

## 6. 明确不做（防止范围蔓延）

- **不改 Runner 的执行核心**（并发 / 超时 / 重试 / 基线 / Gate / 报告）。
  这些与 SUT 是外部还是内部无关，复用即可。
  （A2 修订要求的启动顺序调整 —— health 先于 `_resolve_profiles` —— 不属"执行核心"，
  它只动启动期的阶段排列，不动任何执行语义。）
- **不新增 ACP / CLI / Stdio adapter**。PRD §6.3 列为未来工作；HTTP 已足够覆盖
  ai-chatbot，为一个接入方先造协议是过度设计。
- **不在 ai-chatbot 里内建评测框架**。shim 是测试侧组件，其存在不应影响被评测系统的
  构建产物与运行时。
- **不为单个平台在框架里加字段、分支或事件类型**。这是 E 类的镜像表述：
  A 类开的是通用口子（谁都能用），这条禁的是专用口子（只有某一个能用）。
  判断标准同 §5：换一个 SUT 之后还成立吗？
- **不把 `TraceEvent.type` 改成 `Literal`**。会把"框架该升级词汇表"错判成
  "接入方违约"（exit 2），理由详见 E1。
- **不动 `case-scheduler`**（ROADMAP P3，纯重构，与本任务正交）。
- **不承诺环境隔离**。ai-chatbot 的 `bash` 在宿主机真跑，"重置环境"的能力本次不建，
  只做 workdir 透传 + 清理。

---

## 7. 落地顺序

**修订**：初版顺序把 A1/A2 并列为前置，且缺第 0 步。修订后按"先关掉事实缺口、
再杀假绿、再上环境轴"排列：

```text
0. 冒烟验证脚本（半天）
   └─ 30 行 Python 直连 POST /api/chat：确认 X-Conversation-Id、402 行为、
      chunk 顺序与 B2 映射表逐条对上。本文 ai-chatbot 侧全部事实零实跑，
      这步是唯一能把它变成"已验证"的手段。
1. A2 观测面能力 + A3 性能三结局 + E1 词汇校验   （假绿三连的解药，M1 前置）
   └─ runner 阶段顺序调整：health(含能力探测) → _resolve_profiles
2. E2/E3/E4/E5 边界机制 ── 与第 1 步同批提交
3. B 转译 shim + C 评测集小集（10 case × repeat 2，--no-judge）──> M1
   └─ 隔离用 shim 级约定（每会话独立临时 projectDir），不等 A1
   └─ A4 模型钉住随 shim 首版一起做（run 请求显式带模型字段）
4. A1 workdir 透传（含同位性探测）──> M2：环境类断言（file_state 等）
5. C 评测集扩全量 + judge 指标 + pin 基线 ──> M3：CI Gate / 回归
```

顺序的三条理由：

1. **第 0 步必须最先**：B2 映射表是本方案最大的事实依赖，且目前是推断。
   它若错了，第 3 步的 shim 会照着错误的表写。
2. **第 1 步是 M1 的真前置**（不是 A1）：M1 的断言不读文件系统，
   但会被假绿污染——`harness.retry` 的 `0<=0`、`max_tokens` 的 `0>limit` 为假、
   MCP 折叠后的恒 pass，三条路都在这一步关掉。E1 同批，它的价值在 shim
   写错的第一次就拦下，而不是等报告全绿之后靠人眼发现 `mcp.calls` 恒空。
3. **A1 降到第 4 步**：只读轨迹 + 安全类用例不需要共享 workdir，
   M1 期间 shim 约定顶住；M2 上 `file_state` / `database_state` 时 A1 必须已落地。

D 类文档变更与第 1/2 步同批提交 —— 条款先落地，后续实现才有可引用的依据。
D16（模型钉住）不晚于第 3 步：第一次 pin 基线之前 `agent_model` 必须已是受校验字段，
否则第一批基线就是可被模型漂移污染的。
