# 外部 Agent 平台接入变更计划

> 场景：用 harness-bench 评测自研 Agent 平台 `ai-chatbot`（Sime）。
> 状态：**评审已过，可作为需求实施**。本文只列**要改什么**与**为什么改**，不含实现细节。
> 第三轮修订（评审逐条对源码复核后并入）改了四处实质内容与一处计数事实，见 §0 末；
> 第四轮校准（与可行性评估报告交叉验证 + ai-chatbot 侧坐标逐条核实）见 §0 末与附录。
> 基线：`main` @ `0721ab4`（初版成文于 `9baed6a`；第二轮校准于 `d20d28a`，
> 时隔 15 个提交；第三/四轮的 harness 侧行号与计数已直接在 `0721ab4` 上复核，
> ai-chatbot 侧坐标快照见附录 §8）。
>
> **行号约定**：本文行号随提交漂移，`runner.py` 尤其频繁（初版成文至今
> 最多已偏 80 行）。引用**以符号名与行为为准**，行号只是便于当下定位的近似值。
> 若行号与行为不符，信行为。
>
> **验证状态（第四轮校准后）**：harness-bench 侧的全部行为均已对照源码核实；
> ai-chatbot 侧的**静态事实**（端点与响应头、402 门禁、chunk 载荷字段、审批落库
> 契约、工具面、并发与目录约定）已升级为**源码级坐标核实**——坐标逐条列在附录，
> 本轮由两个独立来源交叉确认（可行性评估报告
> `outputs/UUj9KH79JAcCE1ShSil_h/reports/stage1_可行性评估报告.md` + 本仓复核）。
> **但运行时行为仍零实跑**：chunk 的实际顺序、`finish`+`[DONE]` 的收尾时序、
> 402 发生在流前还是流中、审批后流是否干净结束、`data-sub-*` 的 `id` 是否等于
> 父级 `toolCallId`——这些只能跑起来才知道。第 0 步（冒烟脚本）因此仍是第一优先动作；
> 在它跑通之前，B2 映射表的**顺序与时序**部分应视为"高置信推断"，
> 载荷字段部分已是"已核实"。

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
  **（第三轮修订）E1 默认取 warn，"阻断"的成立条件是 M1 验收里那条
  "warnings 不得有协议违规"**——没有它 E1 只是可见性，不构成阻断（见 §5 E1 修订与 §7）。
- **A1（workdir 透传）阻断 M2**（`file_state` / `database_state` / `sql_result`），
  不阻断 M1。

一个反直觉的结论：**最大的风险不是"接不上"，而是"接上了却不报错"**。
ai-chatbot 有三处观测面（重试、上下文压缩、MCP/命令的分类）在转译时如果被偷懒
合并或忽略，对应指标会静默变成恒 pass —— 这正是本项目 Spec §12.1.1、§19.1.1
与 §22.7 已经记录过的缺陷类别（§22.11 专门复盘了"为什么这类缺陷能长期全绿"），
而 AI 平台接入会把它放大到全量指标上。

**第二轮校准（2026-09-29，基线 `d20d28a`）：结论全部仍然成立。**
初版成文后又合入了 15 个提交（`9baed6a` → `d20d28a`，见 §22–§24 的审计修复、
Challenge Set、Nightly Profile、run 级 diff 噪声下限）。逐条复核结果：

| 项 | 复核结论 |
| --- | --- |
| A1/A2/A3/A4 的问题本身 | **未被推翻**，四条都仍在源码里成立 |
| E1/E2/E4/E5 的机制判断 | **未被推翻**；E3 的清单基线当前命中数为 0，测试可立即写 |
| D1–D16 的条款 | 本仓库内的**一条都没落地**；D11/D12 在 ai-chatbot 仓，本次未核对（逐条复核见 §4 表后说明） |
| B/C 类的判断 | 未被推翻；`database-core` case 数增长不影响"不可复用"的结论（**第三轮修订更正计数**：初版成文的 `9baed6a` 下是 **15** 条，不是 31——31 是 `2604520` 时的数，其后 `c612c74` 到 33、`badfb76` 到 40） |
| 第 0 步 | **仍未执行**，故"ai-chatbot 侧零实跑"的前提依然准确 |

校准带来的**四处收敛**（不是结论改变，是证据变强，各写在对应章节）：
A3 的修法不再需要评审定夺、A4 的守卫落点确定为 `compare.py` 且契约依据要引 §3.3 + §4.3、
E3 可以今天就写成绿、A1 落地没有时序障碍。另有一处**新增后果**：
`case-artifacts` 落地后，agent 产物污染面从 `file_state` 扩展到产物索引（见 A1 修订三）。

**第三轮修订（2026-09-29，评审逐条对源码复核后并入）：四处实质修正 + 一处计数更正。**

前两轮是"事实是否仍然成立"的复核，这一轮是"按本文实施会不会撞墙"的复核，
查出的都是**落点与触发条件**的问题，没有一条推翻原有结论：

| # | 原文 | 修正后 | 依据 |
| --- | --- | --- | --- |
| 1 | A2：观测面表落 `metric_capability_snapshot`，由 `resolve_metric` 统一降级 | **`resolve_metric` 不能承担这条降级**：它对"能力为假"的既有语义是 fallback → 否则 `MetricUnavailableError`（exit 3）；且返回 `None` 时调用方 `continue`，metric 直接消失。改为把观测面表送进 `EvaluationContext`、**由插件判定 skipped**，`metric_capability_snapshot` 只留痕 | A2 修订四 |
| 2 | A3：触发条件写"usage **全缺** → skipped"，与自己的修订段（input/cache 有、output 无）矛盾 | 触发条件改为"**该约束依赖的用量分量未观测** → skipped"，并增加用量口径标记——`int \| None` 表达不了"半缺"，而半缺恰是 ai-chatbot 的实况 | A3 修订五 |
| 3 | A1：workdir 回执"不可写按 `InfraError` 处理" | 回执**今天没有通道**：`create_session` 只从响应体取 `session_id`，`AgentSession.metadata` 是请求回显。需新增响应侧字段，一并写进 D1/D14 | A1 修订四 |
| 4 | E1：默认值 warn 还是 strict "交评审定，不预设结论" | 定为 **默认 warn（可见）+ 收尾档（`release`/`nightly`）开 strict**；并补 warn 分支下 M1 的出场条件（warnings 里不得有协议违规），否则"假绿三连的解药"这一说法不成立。计数载体明确到 `RunMetadata` | E1 修订 |
| 5 | §0 表与 §3：`database-core` "初版成文时 31 个" | 初版成文（`9baed6a`）为 **15** 个；31 是 `2604520` 时的数 | §3 |

四处修正的共性：**本文此前把"能力/口径/回执"都当成了直接可用的既有机制，
实际它们各缺一段落点**。修正只改落点与触发条件，不改任何一条结论与里程碑划分。

**第四轮校准（2026-09-29，与可行性评估报告交叉验证 + ai-chatbot 侧坐标核实）：
结论全部仍成立，ai-chatbot 侧静态事实升级为"已核实"。**
本轮把本文与 `outputs/UUj9KH79JAcCE1ShSil_h/reports/stage1_可行性评估报告.md`
交叉比对，并以其附录坐标为线索，对 ai-chatbot 仓逐条做了源码核实
（`git grep` tracked 文件 + 直读；**注意不要用裸 `grep -rn` 扫它的仓**——
遍历 `node_modules` 会静默死掉且 stderr 被吞，产生"零命中"假象）。结果：

| 项 | 复核结论 |
| --- | --- |
| A1–A4 / B / C / D / E 的结论 | 与 stage1 报告互证，**未被推翻**；harness 侧坐标（`benchmark_cmd.py:38`、`native.py:90/93`、`events.py:51`）逐字吻合 |
| ai-chatbot 侧静态事实 | **全部核实**，坐标见附录 §8：端点/响应头、402 门禁、`data-context-usage` 字段、retry 只打日志、`data-sub-*` 事件、`mcp__` 命名、并发 5/队列 30、审批落库契约、`outputs/<conversationId>`、vitest+Playwright、仓内无评测设施痕迹 |
| 运行时行为 | **仍零实跑**——chunk 顺序、`finish`+`[DONE]` 时序、402 时点、审批后流收尾、`id === toolCallId` 关联，第 0 步冒烟仍是唯一手段 |
| stage1 报告自身的三处数字错误 | **不采纳**：`EVENT_TYPES` "27 个"（实际 **30**）、"~46 个源模块"（实际 103 个 `.py` / 80 个非 `__init__`）、"405+ pytest"（当前 441）。这三处不影响本文的任何结论 |
| 两处精度修正 | 已写回正文：B3 的审批**线格式**（`tool-approval-request` 不是第一方形状）、B2 的 denied 原因可从 `approval.reason` 取回 |

### 0.1 通用 vs 定制：各类变更的归属

评审时的第一个问题是"这是不是把框架做成定制件了"。按**落点**分：

| 类别 | 落点 | 性质 |
| --- | --- | --- |
| A1 workdir 透传 | harness-bench | 通用能力补齐（fixture 隔离本就存在，只是链路断在 `SessionContext`） |
| A2 观测面 → skipped | harness-bench | 通用能力补齐（**第三轮修订**：形状复用 `metric_capability_snapshot` 留痕，判定落插件侧，不复用 `resolve_metric`） |
| A3 token 三键 + 性能三结局 | harness-bench + shim | **代码变更**（`native.performance` 需纳入三结局，数据模型可能要动）；纯"契约澄清"的说法在评审中被推翻。**第三轮修订**：触发条件是"依赖的用量分量未观测"，且口径要进 run 元数据并参与基线守卫 |
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
  但**降级动作不能照抄**（第三轮修订，见 A2 修订四）：`resolve_metric` 对"能力为假"
  给的是 fallback 或 exit 3，不是 `skipped`；而且它的键空间是 metric id，
  观测面表的键是事件名。A2 是"同一形状、不同落点"——留痕复用字段，判定落插件侧。
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
- `src/agent_eval/runner/runner.py:593-604`（`_open_session`）— 构造 `SessionContext`
  时不传 `extra`；而 `runner.py:515` 为每个 case 准备好的 fixture 工作目录
  （`fixtures` 已按 iteration 隔离）就此断链。

**落地没有时序障碍（核对时补记）**：`provider.prepare(workdir)`（`runner.py:515`）
发生在 `_open_session`（`:593`）**之前**，所以 handle 早已就绪，"把环境交给被测方"
只需在既有的 `SessionContext` 构造点上多传一个键，不需要调整任何阶段顺序。

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
且没有任何报错。因此 A1 必须包含 workdir 可达性回执：`create_session` 携带 workdir 后，
SUT 回执"可写"与否，不可写按 `InfraError` 处理（exit 2），把"配置错"与"agent 失败"分开。

**修订三：断言面污染 —— 现在多了一条路径。** ai-chatbot 会往 `projectDir` 写自己的产物
（`outputs/<conversationId>/`）。`file_state` 若断言"目录里恰好是这些文件"，
agent 产物会掺进来。用例要么把断言范围限定在 fixture 子集，要么 shim 在比对前
过滤已知产物目录 —— 这必须写进 C 类用例作者指南，否则 `file_state` 会无端 FLAKY。

**（第二轮校准新增）同一个污染面现在也进产物索引。** `case-artifacts`（Spec §21）
已落地：workdir 在 `cleanup` 之前被快照，快照同时产出产物记录。于是 SUT 写进
`projectDir` 的 `outputs/` 不只是"可能掺进 `file_state` 的噪声"，它会作为
**case 级产物被采集并索引**（`CaseRunResult.artifacts`），并随报告指针出现在
report.json / summary.md / report.html。结论不变（不阻断判定——产物不参与求值），
但"过滤已知产物目录"的要求现在同时适用于 `file_state` 与产物采集两处，
用例作者指南里应一并写明，否则失败现场的"现场"里会混进 SUT 自己的日常输出。

**修订四（第三轮）：回执今天没有通道，"多传一个键"只覆盖去程。**
`http_adapter.py:78` 返回的是 `AgentSession(session_id=..., metadata=payload["metadata"])`
——`metadata` 是**适配器自己请求载荷的回显**（`adapters/base.py:29-31`），
不是服务端响应体；响应体现在只被取了 `session_id`。所以：

- **去程**（workdir 递给 SUT）：确实只需在 `_open_session` 的 `SessionContext` 构造点
  多传一个 `extra` 键，不动任何阶段顺序——修订一那句话仍然成立；
- **回程**（可达性回执）：需要一个**新增的响应侧载体**（`create_session` 响应体里的
  一个字段，或 health 里的一条探测结果），并把 `http_adapter.create_session` 的
  解析从"只取 session_id"扩展到读取它。这条要写进 D1 / D14 的协议面，
  否则实现者会以为回执可以塞进 `AgentSession.metadata`——那是回显，SUT 写不进去。

顺带说：`HealthStatus` 现在只有 `ok: bool` 与 `detail: str`（`base.py:14-16`），
A2 修订四要加的观测面表、A4 要加的实际模型标识、这条要加的可达性回执，
三者都需要扩展它（或新增返回形状）。三处应**一次设计**，避免各加一个字段。

**验收**

- `mock_server.py` 记录 `create_session` 的 metadata，新增用例断言其中含 workdir；
- 端到端：FakeAgent 全链路用例中，agent 侧能读到 workdir 并往里写文件，
  随后 `file_state` 断言能判 pass/fail（而非 skipped）；
- workdir 不可达（模拟容器路径）时，run 以 `InfraError` 收场而非假绿 ——
  **这条依赖修订四的回执通道**，没有它测不出来（回执缺失时按"未知"处理并记 warning，
  不能默认视为可达）。

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
- `src/agent_eval/evaluators/plugin.py` — `EvaluationContext` 需要携带"观测面表"，
  插件的 skipped 判定要有依据（见修订四）。
- 落痕面：观测面表进 `RunMetadata.metric_capability_snapshot`（已有字段，
  但键空间要区分，见修订四），降级原因进 `metric_degradations`（已有）。

**修订：能力声明必须在 health 阶段，且 runner 阶段顺序要为此调整。**
初版方案写的是"能力声明随 `create_session` 上报"，这与代码时序不兼容：
`_resolve_profiles`（`runner.py:246`，**metric 降级决策在这里发生**）先于
`_resolve_baseline`（`:261`），再先于 `health_check`（`:263`），
更远先于 per-case 的 `create_session`（`:277` 起的 TaskGroup 内）。
（第三轮修订后，判定搬到插件侧、不再发生在 `_resolve_profiles`，但结论不变：
观测面表是**run 级**事实，必须整份一次拿到、且在 per-case 执行前就到了位。
放 `create_session` 意味着每个 case 各报一次、会话创建失败的 case 干脆没有声明，
报告上会出现"这个 case 没有观测面表"与"这个 case 不支持该观测面"混在一起。
`health_check` 是框架既有的第二次能力探测点，同构且更早。）

正确落点是 **`health_check` 阶段**（与 `DeepEvalCapabilityAdapter.probe()` 的时机同构），
为此 runner 的顺序调整为 **health（含能力探测）→ `_resolve_profiles`**。
这个调整本来就该做：现状 health 失败发生在基线解析（`:261`）之后，
一次注定失败的 run 还会先解析基线。

**修订四（第三轮）：`resolve_metric` 承担不了这条降级，判定必须落在插件侧。**
初版写"能力声明落 `metric_capability_snapshot`，`resolve_metric` 据此统一降级"，
照此实施会撞两堵墙：

1. **`resolve_metric` 对"能力为假"的既有语义不是 skipped。**
   `evaluators/registry.py:169-189`：先查 fallback，有则降级；`harness.*` 既不是
   `custom.*` 也不是 `native.*`，于是落到最后一条 `raise MetricUnavailableError`。
   `harness.retry` 在 `smoke.yaml` / `strict.yaml` 里都没声明 fallback，
   所以"声明无 retry 观测面"的结果是 **exit 3（无效调用）、run 根本起不来** ——
   比它要取代的那个恒 pass 严重得多。Spec §6.1 也确实把 "Profile 依赖的 metric
   不可用且无 fallback" 归在 exit 3。
2. **即便让 `resolve_metric` 返回 `None`，也拿不到本节的验收物。**
   `runner.py:450-455` 对 `effective is None` 的处理是 `continue`：该 metric 从
   profile 里**消失**，不产出任何 `MetricResult`。而验收要的是
   `verdict=skipped` + `blocking=False` + `metadata.skipped_reason="observation_unavailable"`
   —— 那是插件层的判决形状（`native.py:760-770` 的 `ObservationUnavailable` 路径），
   不是 profile 解析层的产物。

因此正确落法是**两段**：

- **留痕段**：观测面表（事件名 → bool）随 health 上报，写进 `RunMetadata`
  与 `metric_capability_snapshot`（报告 / Web UI 的展现链路已经存在）。
  注意键空间与 `probe()` 现在填的不同：后者是 metric id → bool
  （`deepeval_adapter.py:105-120`）。同字段、两类键要在注释里说清，否则报告上
  "两个 False"看不出是 judge 缺失还是观测面缺失。`metric_degradations` 留给降级原因。
- **判定段**：观测面表进 `EvaluationContext`，插件按自己声明的依赖判
  `skipped`。形状就是插件现在已有的 `skipped` 返回（如
  `context.result("skipped", blocking=False, reason=...)`），只是多一个
  `metadata.skipped_reason="observation_unavailable"` 以便与"参数未声明"区分。
  插件自己不感知 SUT 是谁 —— 它只查"我依赖的观测面在不在表里"。

每个 `harness.*` 插件声明自己依赖的观测面
（如 `RetryEvaluator.required_events = ("retry",)`），这张声明表是框架侧的静态事实，
与 SUT 无关；表里查不到声明时按"具备"处理（保持既有测试的行为不变）。

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
- 观测面表在报告里可查（`metric_capability_snapshot` 与 `metric_degradations` 留痕），
  报告与 Web UI 都能看出"这条没测"，且能与"profile 里没配这条"区分开；
- 反向用例：SUT 发了 `retry` 事件且超过 `max_retries` → FAIL（防止修成恒 skipped）；
- **时序用例（按修订四改口径）**：断言观测面表**在 health 阶段采集**、并到达插件的
  `EvaluationContext`。用一个"health 声明无 `retry` 观测面"的 mock adapter，
  断言 run 的 `metric_capability_snapshot` 里有该条目、且插件据此判 skipped；
  **不断言"启动期降级"**——按修订四，判定发生在插件求值期，启动期只负责采集与留痕；
- **不回归**：`FakeAgentAdapter` / `dev/mock_server.py` 声明全量观测面（或干脆不声明，
  声明表查不到时按"具备"处理），既有测试**不因本改动变红**——它们的行为不应改变，
  只多一张能力表。（测试总数随提交漂移，落地时以当时的全绿为准，不要照抄数字。）

---

### A3. Token 缺失会让 `native.performance` 恒 pass —— 三结局必须覆盖性能组 【阻断 M1】

**修订说明**：初版把这条定性为"契约澄清 + shim 义务"，评审推翻了这一定性 ——
框架的数据模型本身就无法表达"token 未知"，这是代码变更。

**改什么**

- `src/agent_eval/models/results.py` — `TurnResult.tokens` / `CaseRunResult.token_count`
  均为 `int`（缺省 0），`native.py` 的 `EvalScope.tokens: int = 0` 同。
  框架**无法表达"用量未知"**，缺失一律坍缩为 0。
- `src/agent_eval/evaluators/native.py:187` —
  `if limits.max_tokens is not None and scope.tokens > limits.max_tokens`：
  usage 缺失时 `scope.tokens == 0`，`0 > limit` 为假 → 不追加 problem → **pass**。
  （第三轮修订补：半缺——有输入无输出——同样坍缩成这个数，
  因为 `tokens` 是一个合成后的整数，落进来之后分不出分量。）
  这与 `harness.retry` 的 `0 <= 0`（`harness.py:49`）是**同一种病，发生在 native 侧**。
- 修法二选一（**第二轮校准：这条不再需要评审定夺**）：同一个函数
  `_check_constraints`（`native.py:168-187`）里，紧邻的 `max_cost` 分支已经给出了
  答案 ——
  1. **判定侧（应当选，与 `max_cost` 完全对齐）**：`EvalScope.cost` 就是
     `float | None = None`，`cost is None` 时抛 `ObservationUnavailable` 判 skipped。
     `tokens` 照做即可：给 `EvalScope` 加一个"用量已观测"标志（或 `tokens: int | None`），
     **该约束依赖的用量分量未观测时**，`native.performance` 组对 `max_tokens` 产出
     `verdict=skipped`（`ObservationUnavailable`，复用 Spec §19.1.1 的既有机制）。
     **同一个函数里 `cost: float | None = None` 与 `tokens: int = 0` 的不对称，
     已经不是设计选择，是遗漏** —— 两者都是"外部数据缺失"，处置必须一致。
  2. **数据侧**：`tokens` 全链路改 `int | None`，报告/聚合/Web UI 同步
     （`quality-guidelines.md` 第 7 条的 None≠0 约束到处都要守，改动面大）。
     选它就要顺手统一 `cost` 的现有习惯，否则同一次 run 里两个用量字段用两种
     表达方式。

**修订五（第三轮）：触发条件写准 —— 是"依赖的分量缺"，不是"全缺"，并且要带口径标记。**
本节原先在三个地方给了互相矛盾的触发条件：修订说明段写"usage 全缺时 skipped"，
output-token 段写"input/cache 来自流、output 缺失"（**半缺**）。而 ai-chatbot 的实况
是后者，所以按"全缺"实施会对它完全不起作用——半缺的 case 照样 `pass`。
两处必须一起改：

1. **触发条件是分量粒度**：`max_tokens` 依赖的是"输出侧用量"（那才是被 `max_tokens`
   约束的东西），`tokens.max_regression_percent` 依赖的是"总量"。哪个分量未观测，
   哪条约束判 skipped，而不是笼统地"usage 缺就跳"。`int | None` 是二值的，
   表达不了"输入有、输出无"，所以判定条件得挂在"分量"上（一个标志位，
   或 `input_tokens` / `output_tokens` 分开落）。
2. **用量口径要进 run 元数据**：文档推荐"接受单侧 + skipped，token 回归阈值只校 input
   侧"。这条一旦落地，`tokens.max_regression_percent` 在"只看到输入"与"看到全部"的
   两次 run 之间就**不可比**——同一阈值下比出来的差值里混着口径变化，
   与 A4 的模型漂移是同一类"苹果比橘子"。所以口径（哪一侧被观测到 / 阈值只校了哪侧）
   必须写进 `RunMetadata`，且**基线比对时两侧口径不一致应走 A4 的 INVALID 语义**，
   而不是静默出一个回归数字。否则第 3 步 pin 下来的第一批基线，口径就是不明的。

这一条不改变"判定侧应当选"的结论，只是把"什么时候判 skipped"和"跳了之后
什么还能比"写清楚——后者原文本该由 A4 的守卫覆盖，但它只覆盖了 `agent_model`。

**（第四轮校准补）子 agent 路径上 output token 有现成观测来源。**
上文"流上唯一用量数据缺 output token"对**主循环**成立（`data-context-usage` 无
output 字段，已核实 `run-session.ts:266-285`）；但 `data-sub-done` 的
`tokenUsage` 是 `{inputTokens, outputTokens, totalTokens}` 全量三键
（`event-broadcaster.ts` 的 `broadcastDone` 载荷，已核实）。也就是说：
shim 若要补 output 侧，**子 agent 的 case 可以先做到**，主循环仍缺、
本节的口径问题也仍然存在——两者不要混为一谈。B2 表已列 `tokenUsage`，
A3 的落地说明里应把"哪里有全量、哪里只有单侧"写清，避免 shim 作者
误以为全链路都缺 output。

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
- usage 半缺（有 input 无 output）：**`max_tokens` 同样产 `skipped`**（它依赖输出侧），
  且 reason 里写清缺哪一侧；只依赖总量/输入侧的约束按选定方案处置并注明口径；
- 用量口径写进 `RunMetadata`，基线比对时两侧口径不一致不产出回归数字
  （与 A4 的 INVALID 语义一致，见修订五）；
- 若启用成本阈值，先决定 `evals/pricing.yaml` 与 ai-chatbot `cost` 表谁是事实源
  （仓库现状：`PricingTable.load(evals_root)`，`reports/cost.py:30-55`）。

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
  `RegressionComparison` 判 `INVALID`**——复用它对 dataset 版本不一致的既有语义
  （Spec §3.3），不发明新机制。
- **（第三轮修订补）同类第二条：用量口径。** A3 修订五要求把"哪一侧用量被观测到"
  写进 run 元数据。它与 `agent_model` 是同一类"两侧不可比"：只校输入侧的 run
  与校全部用量的 run，`tokens.max_regression_percent` 的数值不可比。
  两条守卫应同批落在 `compare.py`，否则 A3 的"接受单侧 + skipped"会把口径漂移
  伪装成回归。

**（第二轮校准）守卫的落点是 `compare.py` 现有三条之后，契约依据要引两个章节而不是一个。**
`regression/compare.py` 现已有三条同形守卫：`dataset_version`、`benchmark_id`、
`suites_covered`（后者是 ROADMAP「发现的 2」的修复），都在扫描 `baseline_meta` 与
`candidate_meta` 之后逐条 `comparison.valid = False` + 写 `invalid_reason`。
`agent_model` 与用量口径是同一段代码里的第四、第五条，写法照抄即可 ——
副作用是它们天然继承"`valid=False` → Gate 对应规则落 `undetermined`
（`quality/gates.py:132-140`）"这条既有链路，不需要另建通路。

因此 D16 只改 Spec §3.3 会漏掉真正的落点：`suites_covered` 那条守卫的规范依据写在
**Spec §4.3**（"禁止：静默跨 dataset_version 比较；静默跨**套件组成**比较"）。
`agent_model` 属于同一类"两侧不可比"，应同时写进 §4.3 的那句禁令，否则下一个人
读 §4.3 只会看到两条禁令，读不出第三条的存在。

**验收**

- 两次 run 标签不一致时，`compare_runs` 产出 `INVALID` 且 `invalid_reason`
  写明"agent model mismatch"，gate 对应规则落 `undetermined` 而非 FAIL/PASS；
- 两次 run 的用量口径不一致时同样 `INVALID`（第三轮修订补，见 A3 修订五）；
- shim 发出的每个 `run` 请求都带显式模型字段（不靠平台默认值）。

---

## 2. B 类：协议转译 shim（新增组件）

shim 是本次接入的主体工作量。它不进 ai-chatbot 的生产代码路径，应是独立的
测试侧组件（建议置于 ai-chatbot 的 `tests/` 或独立目录，不参与其构建产物）。

### B1. 端点映射

| harness-bench 期望 | ai-chatbot 现状 | shim 处置 |
| --- | --- | --- |
| `GET /health` → `{"status":"ok"}` | **无** | shim 自建，且必须真检查 license、模型配置、workdir 可达性（A1 修订二/A4），并**在此上报观测面能力表与实际生效模型**（A2 修订/A4：能力声明在 health 阶段） |
| `POST /api/agent/sessions` → `{"session_id":...}` | 无此端点，但 `conversationId` **必填、值由调用方自选**（缺失直接 400 `Missing conversationId`，`chat-service.ts` 已核实） | shim 分配 id 并接受 metadata（含 A1 的 workdir，回执可达性） |
| `POST .../{id}/run` → SSE 事件流 | `POST /api/chat`（UIMessage stream） | **核心转译**，见 B2；请求显式携带 `modelConfigId`/`modelName`（A4，不依赖平台默认值） |
| `POST .../{id}/cancel` | `POST /api/chat/{conversationId}/stop` | 直连 |

`health_check` 不能只探活：ai-chatbot 的 `proxy.ts` 让 `/api/*` 过 license 校验
（已核实 `proxy.ts:67-71`：无效/过期一律 `402 {error:'LICENSE_INVALID'}`；
**豁免集合**：`/api/license/*`、`/nest`、`/sime`、`/.well-known/*`——激活路径必须能通，
shim 自建的 `/health` 若要免 license 得落在豁免前缀下或持有效 license）。
若 health 报 ok 而 run 报 402，就会被 `http_adapter.py:88-90`
归为 `InfraError` → exit 2，运维看到的是"基础设施故障"而不是"许可证过期"。
health 必须把这两件事的区别暴露出来。

### B2. 事件映射表（转译契约）

| PRD §8 事件 | ai-chatbot SSE 来源 | 必须注意 |
| --- | --- | --- |
| `run.started` / `run.finished` | `start` chunk / `finish` chunk + `[DONE]` | 缺 `run.finished` 会被 `runner.py:892` 判 agent 失败，必须可靠发出 |
| `tool.call` / `tool.result` | `tool-input-available` / `tool-output-available` | `input` 已是解析后的对象，直接作 `data.arguments`；**参数类与安全类断言全靠它** |
| `tool.result`(错误) | `tool-output-error` / `tool-output-denied` | `denied` 的载荷只有 `toolCallId`，part 本身**没有 reason 字段**；但原因可从该 part 的 **`approval.reason`** 取回（`zombie-approval-contracts.md` §1：审批终态只能落在 `approval.reason`，不得带 `output`/`errorText`）——shim 取它即可，不必"自行编" |
| `mcp.call` / `mcp.result` | `toolName` 形如 `mcp__<server>__<tool>` | **必须拆成独立事件**，不得折叠进 `tool.call` |
| `command.started` / `command.finished` | `bash` 工具调用 | 退出码在 output 对象的 `exitCode` 键里，需提到 `data.exit_code`（Spec §19.4 的唯一观测来源） |
| `subagent.started` / `finished` | `data-sub-open` / `data-sub-done` | 含 `tokenUsage` / `status`（done 载荷为全量三键，见 A3 第四轮补）；"按 `id === toolCallId` 关联"是**运行时待验证项**（冒烟清单第 4 条）。另有 `data-sub-async`：父流不会再有 done，终态要查 `subagent_sessions`，shim 不得把它当丢事件 |
| `skill.loaded` | `use_skill` 工具调用 | 也可走 `skill.loaded` 显式事件 |
| `model.response.data.usage` | `data-context-usage` | 只有输入侧，见 A3 |
| `error` | `error` chunk 或 `finishReason:"error"` | 注意 `errorText` 可能被自愈逻辑抑制为空串 |
| `retry` | **不可观测** | 见 A2，应落 skipped |
| `context.compaction.*` | **不可观测** | 见 A2，应落 skipped |

**为什么把 MCP/command 单列**：`security/evaluator.py:209-223` 明确要求
`tool_names` / `mcp_names` / `command_calls` 三路**逐条透传**，
注释里写了原话 —— `forbidden_mcp` 曾因 runner 不传 `mcp_names` 而恒 pass。
ai-chatbot 的 MCP 调用在流上就是普通工具调用，只要 shim 图省事不分流，
红队用例会**全部变绿**，而 `security.max_failures: 0` 会因此平凡通过
（ROADMAP 里 P0 任务 `security-cases` 要解决的正是同类问题）。

**分流不是"顺手的实现细节"，它决定 `mcp` span 是否存在。** MCP 观测面不是
独立的解析器，而是由 `mcp.call` 事件派生的 span（`trace/builder.py:29`：
`"mcp.call": ("mcp.result", "mcp")`），`MCPPermissionEvaluator` 读的正是
`context.mcp_calls`（即 span 集合）。shim 把 `mcp__*` 写成普通 `tool.call`，
`mcp` span 数量恒为 0；此时若 case 声明了 `allowed`，越权集合为空 → 判 pass
（未声明 `allowed` 才退化为 skipped）。**报告上"没有越权"与"根本没看到 MCP 调用"
长得完全一样** —— 与 A2 是同一个病灶。

### B3. 人机审批策略（必须显式且可追溯）

ai-chatbot 遇到 `behavior:'ask'` 的工具或 `ask_user_question` 时，**流挂起、
本轮正常结束**（`finish` + `[DONE]`），等待完全在客户端，服务端不阻塞、无超时；
审批以 **`state:'approval-requested'` 的 tool part** 落库并上流
（`connector/tool-adapter.ts:142` 注释原话："needsApproval 挂起（流暂停 →
approval-requested part 落库 → 前端展示"）。要续跑必须**重新 POST** 一条
`state:'approval-responded'` 的 assistant 消息（`zombie-approval-contracts.md`：
"服务端只按 `body.message` 自己含 `approval-responded` 按 id upsert"）。

**（第四轮校准）线格式修正：`tool-approval-request` 不是第一方形状。**
这个字面串只存在于 AI SDK provider 的 `node_modules` 类型里，
ai-chatbot 第一方代码零命中——初版把它当成了流上 part 的类型名。
shim 转译时应按第一方形状（tool part 的 `state` 字段）识别审批，
**最终以第 0 步冒烟的实测 chunk 为准**。本段的三个实质判断不受影响：
轮次即结束、等待在客户端、续跑重新 POST——三条都已在源码与契约文档中核实。

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

`evals/datasets/database-core/` 的 40 个 case **不可复用**（第三轮修订更正计数：
初版成文的 `9baed6a` 下是 15 条，`2604520` 才到 31，其后 `c612c74` 到 33、
`badfb76` 到 40；增长过程不影响本条判断）：它们假设的工具名是
`execute_sql` / `shell_exec`，fixture 是 `sales_v2`。ai-chatbot 的真实工具面是
`read_file` / `write_file` / `edit_file` / `bash` / `grep` / `glob` / `ls` /
`web_search` / `use_skill` / `agent` / `mcp__*` / `<connector>_*`。

**（第四轮校准）工具名的权威依据是 registry 本身，不是任何文档的清单。**
上表与各处清单都是**子集**：完整工具面以 ai-chatbot 的
`packages/core/src/runtime/tools/index.ts`（静态 registry：含 `save_report`、
`ask_user_question`、`task_create/update/list/get`、cron 系列、`exit_plan_mode`）
+ `plan-mode.ts` 白名单 + `mcp/registry.ts` 与 connector 的动态注册为准。
两份文档（本文与 stage1 报告）各列了不同子集——本文含 `web_search` 不含
`task_*`/`save_report`，stage1 正相反。**写 case 前先从 registry 拉全量名单**，
再按下面第 1 条的"逐字一致"约束核对。

**新建 dataset 的硬约束**

1. case 里的工具名必须与 ai-chatbot 实际发出的 `toolName` 逐字一致 —— 拼错的后果是
   `tools.required` 恒 FAIL（显性）或 `tools.forbidden` 恒 pass（隐性）。
2. fixture 只能落在已实现能力内（`filesystem` / `sqlite`）；`postgres` / `git` 在
   `get_provider()` 里显式 raise（Spec §18.4）。走 connector 的 SQL 场景需要另行设计。
3. 遵守 Spec §18 的覆盖约束：每维度至少一条**负向** case，且负向必须真的红；
   `golden` 只收正向（否则 `golden.required_pass_rate: 1.0` 永远 FAIL）。
4. profile 需按 A2 的结论裁剪：`harness.retry` / `harness.context_compaction`
   在 shim 能提供观测面之前，应在 profile 里显式排除，**并由接入侧声明观测面不存在**
   （两条一起做，缺前者是"静默 pass"，只做后者会让 metric 恒 skipped 而占着覆盖统计）。
   **（第三轮修订）不要用"删掉 profile 条目"代替能力声明**：删了之后报告上看不出
   "本来就不打算测"与"忘了配"的区别，这正是 A2 要留痕的东西。
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
   **同一条过滤也要用在产物采集上**（第二轮校准）：`case-artifacts`（Spec §21）已落地，
   workdir 在 `cleanup` 前被快照并产出 `CaseRunResult.artifacts`，SUT 的日常输出
   会随失败现场一起进报告。产物不参与判定，所以这是"现场不干净"而非"结论错"。

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
| D1 | `docs/agent-evaluation-regression-platform-engineering-prd-v2.md` §7.2 | 明确 `metadata` 允许携带扩展键，并定义 `workdir` 的语义；**（第三轮修订）同时定义 `create_session` 响应的回执字段**（workdir 是否可达/可写） | A1 要求 fixture 环境透传；不改则协议里没有它的位置，实现会各行其是。回执字段必须写进协议面：`AgentSession.metadata` 是请求回显（`base.py:29-31`），SUT 塞不进去（见 A1 修订四） |
| D2 | 同上 §6.3 | 把"外部自研平台接入"从未来工作提升为**已定型形态**，注明 shim 模式 | 现状只列了 6 个未实现的 adapter 名字，没有说明"外部平台用 HTTP 够用、需自备转译层"，后来者会以为必须写 ACP/CLI adapter |
| D3 | 同上 §3.2 非目标 | 补一条：不代管被测平台的容器化与数据播种 | ai-chatbot 无容器隔离、无环境重置能力，边界不说清会变成无限责任 |
| D4 | `docs/agent-eval-engineering-spec-v2.1.md` §19.1.1 | 把"三结局（满足/不满足/观测不到）"从**扩展断言键**升级为**所有 metric 的通则**，明确 `harness.*` 插件**与 `native.performance`** 同样适用 | A2/A3 的直接依据。不改则 §19.1.1 读起来只约束扩展键，插件作者会认为恒 pass 是可接受的；`max_tokens` 的 0 假绿（`native.py:187`）是同病异侧 |
| D5 | 同上 §17.3 | 补"外部接入侧的观测缺口"小节，登记 retry / compaction 在本次接入中不可观测 | 现有 §17.3 只记录内部 evaluator 的 8 项缺口，外部接入的缺口无处可记 |
| D6 | 同上 §12.1.1 / §12.4 | 把"观测面逐条透传"与"覆盖缺口必须可见"扩写为**外部接入方义务** | 原条款是写给内部 runner 的；接入方（shim 作者）才是新的漏传来源 |
| D7 | 新增 `docs/external-agent-integration-guide.md` | 接入指南：事件映射表（本文 §B2）、审批策略、观测面能力声明、并发上限 | 第一次接入必然踩这些坑，而知识现在只散落在两个仓库的源码注释里，没有可交付给接入方的契约文档 |
| D8 | `.trellis/spec/backend/quality-guidelines.md` | 新增三条硬约束：(1) 外部映射必须逐观测面拆分，MCP/command 不得折叠进 `tool.*`；(2) 未被 SUT 观测能力覆盖的 metric 必须在 profile 显式处置并由能力声明留痕，不得静默 pass，**也不得靠删条目掩盖**；(3) **（第三轮修订）`_check_constraints` 一类的"外部数据缺失"判定必须与 `max_cost` 同向**——依赖的分量未观测即 skipped，禁止坍缩为 0 后参与比较 | 把 A2/A3/B2 的结论固化成评审时能引用的条款，否则下次接入会原样重犯。第 3 条是 A3 修订五的直接依据：`tokens: int = 0` 与 `cost: float \| None = None` 的不对称不是设计选择 |
| D9 | `.trellis/tasks/ROADMAP.md` | 登记本次接入的 7 个切片：观测面能力（A2）、性能三结局（A3）、模型钉住（A4）、**转译 shim（B，最大单项工作量，初版漏登记）**、专用评测集（C）、边界机制（E）、接入指南（D7）；A1（workdir）随 M2 登记即可 | 现有 ROADMAP 九项里没有"外部接入"，不登记则不会被排期；初版切片清单不含 B，等于最大的活不在排期表里 |
| D10 | `README.md` | 补"评价一个外部平台"的最小路径：起 shim → 配 profile → 跑 benchmark → 读报告 | 现有 README 的快速开始只有 `fake://` 与 mock server，没有对外平台的走法 |
| D11 | **ai-chatbot 侧** `README.md` / `AGENTS.md` | 补"如何被外部测试驱动"：本地起服务的 env、license 前置、`POST /api/chat` 的调用契约（含必须自带 `conversationId`） | 该仓目前没有说明如何本地起服务供外部测试，交接成本高 |
| D12 | **ai-chatbot 侧** 新增接入说明 | 公开 `tool-approval-request` 的语义（轮次即结束、需重新 POST） | 该语义现在只在 `docs/zombie-approval-contracts.md` 与源码里，对外部集成方不可见；B3 的策略设计依赖它 |
| D13 | `docs/agent-evaluation-regression-platform-engineering-prd-v2.md` §8 | 明确 `EVENT_TYPES` 是**闭合词汇表**（新增事件类型必须走框架升级，不得由接入方自行扩展），并说明校验后果：**默认产出 run 级 warning，`strict` 档升级为 exit 2**（第三轮修订：默认值已定为 warn） | E1 的依据。现在 `EVENT_TYPES` 全仓库零消费（`models/events.py:11`）、`type` 是裸 `str`（`:51`），读文档的人无法知道"自定义事件名"是被允许还是被禁止 |
| D14 | 同上 §6.2 / §7.2 | 写明 adapter 的四项义务：**能力声明**（哪些观测面存在，**在 health 阶段**）、**事件归一化**（平台方言必须转成 §8 词汇，不得透传）、**环境回执**（收到 workdir 后回报可达性，**走响应侧字段**）、**用量口径上报**（第三轮修订：报了什么、没报什么） | E2/E5/A1 修订二/A3 修订五的依据。这些现在是隐含要求，不写进协议面就会被当成"实现细节"，而它们是"框架保持通用"的唯一保障 |
| D15 | `docs/agent-eval-engineering-spec-v2.1.md` §6.1 | 补 exit 2 的一个触发面：协议词汇违约（未知事件类型）在升级开关打开时的归属 | 现有 §6.1 把 exit 2 定义为"gate 无法可靠求值"。协议违约让观测面失效，正是同一语义；不写清则实现者会把它当 AGENT_FAILURE（exit 1）处理，冤枉被测方 |
| D16 | 同上 §3.3 **与 §4.3** | 把"基线 INVALID 的触发面"从 dataset 版本不一致**扩展到 `agent_model` 不一致**（第三轮修订**再加一条：用量口径不一致**），写明 `RunMetadata.agent_model` 从此是受校验字段而非自由标签 | A4 的依据。不改则模型漂移静默污染基线，回归平台的核心主张落空。**（第二轮校准）必须同时改 §4.3**：实现落点 `regression/compare.py` 的三条既有守卫里，`suites_covered` 那条的规范依据写在 §4.3 的"禁止静默跨 dataset_version / 跨套件组成比较"，只改 §3.3 会让第三条禁令在真正的落点处不可见。**（第三轮修订）用量口径同批**：A3 的"接受单侧 + skipped"若不配口径守卫，阈值比出来的差值里混着口径变化，正是 §4.3 要禁的"不可比" |

**第二轮校准：本仓库内的 D 条款一条都没落地（逐条核对，非推定）。** `docs/` 下仍只有
Spec / PRD / 本文三个文件（D7 的接入指南不存在）；PRD §7.2 无 `workdir` 与扩展键
说法（D1）、§6.3 仍是 6 个 adapter 名字（D2）、§3.2 无非目标补充（D3）、
§8 无"闭合词汇表"表述（D13）、§6.2 无 adapter 三项义务（D14）；
Spec §3.3 与 §4.3 均无 `agent_model`（D16）、§17.3 无外部接入小节（D5）、
§12.1.1 / §12.4 仍只写给内部 runner（D6）、§6.1 无协议违约归属（D15）、
§19.1.1 的表述未升级为通则（D4）；
`.trellis/spec/backend/quality-guidelines.md` 里 `外部` / `接入` / `shim` / `SUT`
命中数为 0（D8）；ROADMAP 里搜不到任何"外部接入"登记，仍是原九项（D9）；
README 快速开始仍只有 `fake://` 与 mock server（D10）。
**D11 / D12 在 ai-chatbot 仓，本次未核对**（那是另一个仓库，不在本次复核范围）。
这不是结论失效 —— D 类**本来就是待办清单**，未落地是它的预期状态；
列出是为了让评审能看到"条款待评审"与"条款已被接受并落地"是两件事。

---

## 5. E 类：边界强制机制（把约定升级为机制）

**要解决的问题**：框架的通用性目前**没有任何机制保障**，只靠"实现的人记得别乱写"。
下一个接入方、或者半年后的自己，很容易在框架里加一个只为某个平台服务的分支，
而这一切都不会让任何测试变红。

证据是现成的两处：

- `src/agent_eval/models/events.py:11` 定义了 `EVENT_TYPES`（PRD §8 的固定词汇表，
  **现 30 项**，第四轮校准计数），**全仓库零处消费**（`grep EVENT_TYPES src/ tests/`
  只命中定义处）。
- `src/agent_eval/models/events.py:51` 是 `type: str` 而非 `Literal`，
  `adapters/sse.py:16-28` 只校验 JSON 形状与 pydantic 字段，**不校验 `type` 取值**。

后果：shim 若把 `mcp.call` 写成 `mcp_call`（下划线笔误），整条流照常解析，
`mcp.calls` 恒空，`security.forbidden_mcp` 与 `harness.mcp_permission` 恒 pass，
run 全绿、exit 0、报告没有任何异常。**这是一个不会自己暴露的缺陷**，
与 Spec §12.1.1、§19.1.1 记录的是同一类问题。

**它不是假想：本轮已经抓到过同形的一次。** Spec §12.1.1（V2.3 回填）记的
`forbidden_mcp` 恒 pass，根因就是 runner 漏传 `mcp_names` —— 规则、观测面、
测试都在，唯独"事件 → 观测面"这一段没人校验，于是它静默失效了很久。
E1 要补的正是这一段：**§12.1.1 管的是"观测面 → 规则"的透传义务，
E1 管的是"事件流 → 观测面"的词汇校验**，两者是同一条链上的相邻环节，
缺前者规则是装饰品，缺后者笔误不会被发现。
§22.11 那三类成因（测试把被测路径关掉了）对本条同样适用：
一条"输出里没有 mcp.calls"的报告，读起来与"这个 agent 没调 MCP"完全一样。

以下五条机制覆盖五类渗透路径。E1/E2/E3 是新增，E4/E5 是把已有形状冻结下来。

### E1. 消费 `EVENT_TYPES`：未知事件类型必须可见

**机制**：观测到的 `event.type` 若不在 `EVENT_TYPES` 内 →

- **默认**：新增一个 run 级计数（`protocol_violations`）记录违规类型与次数，
  并写入既有的 `aggregate.warnings`（`reports/aggregate.py:119` 已有该字段，
  落点是 `build_aggregate` 里 `warnings: list[str]` 那段，
  CLI `benchmark_cmd.py:107` 与 `report.json` 已消费），**可见但不阻断**；
- **升级**：由**独立的显式开关**决定是否升级为 `InfraError`（exit 2），
  `--strict-protocol` 或 profile 级配置。

**E1 修订（第三轮）：默认值定为 warn，并补上 warn 分支下的 M1 出场条件。**

初版把 warn/strict 留作评审决定。现按以下理由定为 **默认 warn + 收尾档开 strict**
（仓库现状：gate 有 `evals/gates/{main,pr,release}.yaml`，profile 有
`evals/profiles/{smoke,nightly,strict}.yaml`；开关落在哪一层属实现细节，
但**不要**塞进 gate 的 `strict` 字段，见下"陷阱 1"）：

- 文档自己在"为什么不把 `type` 改成 `Literal`"里说了：`EVENT_TYPES` 会演进
  （`context.compaction.*` 就是本项目自己新加的）。默认 strict 会把"框架该升级词汇表"
  判成"接入方违约"，而后者是 exit 2——一次正常的协议演进就会让流水线红掉；
- strict 的风险是单向的（版本演进期必红），warn 的风险是"CI 里不红的东西会烂掉"，
  后者可以用**档位**消除：收尾档（`release` / `nightly`）开 strict，
  PR 档保持 warn（那里不红是为了不打断）。

**但 warn 默认会削弱本节"假绿三连的解药"这个定位**，所以补一条 M1 出场条件：
**M1 的验收必须包含"`aggregate.warnings` 里没有协议违规"**。
否则"词汇写错 → 报告全绿"依然成立（只是多了一行没人看的 warning），
E1 就不算 M1 的阻断项。开发期（第 3 步 shim 首版）建议直接以 strict 跑，
把笔误在第一次就拦下来；定稿基线再回到所在档位的默认值。

**计数的载体（第三轮修订补）**：`aggregate.warnings` 只是**呈现**，
`RunAggregate` 是 run 结束后从结果拼出来的，违规计数要在执行期累加、
再活到聚合阶段，因此需要一个落点（`RunMetadata` 的新字段最直接），
由 runner 把"本 run 观测到的未知事件类型与次数"传给 `build_aggregate`。
原书写"写入既有的 `aggregate.warnings`"缺了这一截。

**两个必须避开的实现陷阱**

1. **不要复用 Gate 的 `strict` 字段**。该字段已有确定语义
   （"任何 blocking FAIL 都阻断"，Spec §15 与 `release.yaml` 都在用），
   把"协议严格性"也塞进去，会让 Release Gate 的行为取决于一个与门禁无关的维度，
   属于本仓库 quality-guidelines 第 1 条警告的"字段语义被稀释"。
2. **不要把它做成另一条恒 pass 的声明**。如果这个开关默认关、且没人会打开，
   那它就等于不存在 —— 与 `suites` 曾在 YAML 里躺了整个 P4 是同一类问题
   （Spec §15 的教训，已写进 quality-guidelines 门禁约束第 1 条）。
   默认行为必须是"至少可见"，升级开关只是给愿意承担红线的团队用。

**为什么不把 `type` 改成 `Literal`**：PRD §8 会演进（本项目自己就在加
`context.compaction.*`）。Literal 会把"框架该升级词汇表"错判成"接入方违约"，
而后者是 exit 2 —— 一个平台升级事件协议就会让整条流水线红掉。
`EVENT_TYPES` 是**声明式清单**，新增事件类型 = 改这一处，路径必须保留。

**为什么这条是纯通用的**：它校验的是 PRD §8 的固定词汇表，与被测方是谁无关。
它拦的不是"ai-chatbot 特有事件"，而是**任何**接入方的笔误与方言泄漏。

**验收**：一条用例喂入 `type="mcp_call"` 的 SSE 帧，断言 run 产出 warning
且文本里出现违规类型名；另一条断言开启升级开关后同一输入得到 exit 2。
再加一条：`--strict-protocol` **未开**时，同一输入不得改变 verdict 与 exit code
（默认档位只加可见性，不动判定）。

### E2. 观测面能力由接入侧声明，框架不得推断

**机制**：能力声明在 **`health_check` 阶段**由 adapter 上报（不是
`create_session` —— 它是 run 级事实，要整份一次拿到、且在 per-case 执行前到位；
健康检查失败时也不该有 case 悄悄少了声明，见 A2 修订）→ 落
`RunMetadata.metric_capability_snapshot`（`models/run.py:67` 已有该字段）
与 `metric_degradations` 留痕 → 观测面表进 `EvaluationContext`，
**由插件按自己声明的依赖判 skipped**（见 A2 修订四——`resolve_metric` 承接不了
这条降级，它的既有语义是 fallback 或 exit 3）。
A2 复用**同一形状**，落点分两段，不新增概念。

**三条必须拒绝的反模式**：

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

3. **（第三轮修订补）不要把"观测面表"当成 `probe()` 的延长线塞进同一键空间。**
   `probe()` 返回的是 **metric id → bool**（`deepeval_adapter.py:105-120`），
   观测面表是 **事件名 → bool**。两者若共用一个 dict，报告上"两个 False"
   分不出是 judge 缺失还是观测面缺失。字段可以复用（都进
   `metric_capability_snapshot`），键必须能区分（前缀，或分成两个子映射）。

### E3. 源码级边界断言（沿用仓库既有先例）

仓库里已经有这个手法的先例：`tests/test_evaluator_plugin.py:301-305` 用
**读 `runner.py` 源码、断言其中不含任何具体插件名**来证明 PRD §109.4 的可扩展性
（注释原话：这个证明必须落在测试里，否则"可扩展"只是文档承诺）。
框架通用性用同一手法守住即可。

**（第二轮校准）写这份测试前，先确认基线是干净的 —— 现在确实是。**
按下列清单在 `src/agent_eval/` 下 grep，**当前命中数全部为 0**：
`ai-chatbot` / `mcp__` / `data-sub-` / `data-context-usage` /
`tool-approval-request` / `tool-output-denied` / `projectDir` / `conversationId` /
`SIACT_` / `siact`。也就是说这条断言**今天就能写成、就能过**，
不需要先做一轮清理 —— 它是纯粹的"防止以后变脏"，而不是"顺手还债"。
这一点值得写进测试的原因注释：一条从第一天就是绿的边界测试，
下一个人加平台分支时才会知道它一直在那儿看着。

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
| E1 消费 `EVENT_TYPES` | 方言泄漏 / 笔误 → 观测面静默失效 | `models/events.py` 消费点 + `RunMetadata` 计数 → `aggregate.warnings` + 独立升级开关（默认 warn） | 新增 |
| E2 能力声明来自接入侧 | 框架里长出"平台判断"分支 | 复用 `metric_capability_snapshot` 留痕 + 观测面表进 `EvaluationContext`、插件判定（**不复用 `resolve_metric`**，见 A2 修订四） | 新增 |
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
   └─ 30 行 Python 直连 POST /api/chat：ai-chatbot 侧的**静态事实**（载荷字段、
      端点、常量）已源码级核实（附录 §8），冒烟只剩**运行时行为**——
      验收清单五条见本节末。这步仍是把 B2 的"顺序与时序"变成"已验证"的唯一手段。
1. A2 观测面能力 + A3 性能三结局 + E1 词汇校验   （假绿三连的解药，M1 前置）
   └─ runner 阶段顺序调整：health(含能力探测) → _resolve_profiles
   └─ A2 判定落插件侧（`resolve_metric` 承接不了，见 A2 修订四）
   └─ A1 顺带补回执通道的协议面（该字段要一次设计，别等第 4 步再回头改）
   └─ E1 开发期用 strict 跑（把 shim 笔误在第一次就拦下），定稿回到档位默认值
2. E2/E3/E4/E5 边界机制 ── 与第 1 步同批提交
3. B 转译 shim + C 评测集小集（10 case × repeat 2，--no-judge）──> M1
   └─ 隔离用 shim 级约定（每会话独立临时 projectDir），不等 A1
   └─ A4 模型钉住 + 用量口径随 shim 首版一起做（run 请求显式带模型字段；
      两侧不可比的守卫同批落 compare.py）
4. A1 workdir 透传（含同位性回执，需要响应侧字段）──> M2：环境类断言（file_state 等）
5. C 评测集扩全量 + judge 指标 + pin 基线 ──> M3：CI Gate / 回归
```

**M1 的出场条件（第三轮修订补）**：除既有功能验收外，第 1 步与第 3 步各自带一条
负面条件 —— (1) `aggregate.warnings` 里没有协议违规；(2) 报告里没有被观测面覆盖
却静默 pass 的 `harness.*`（E1 默认 warn 后，这两条是"假绿三连的解药"这个定位
成立的唯一依据；不满足则 E1 不算 M1 阻断项，得回头改档位）。

顺序的三条理由：

1. **第 0 步必须最先**：B2 映射表是本方案最大的事实依赖。载荷字段部分已源码级核实
   （第四轮校准，附录 §8），但**顺序与时序仍是推断**——它若错了，
   第 3 步的 shim 会照着错误的表写。
2. **第 1 步是 M1 的真前置**（不是 A1）：M1 的断言不读文件系统，
   但会被假绿污染——`harness.retry` 的 `0<=0`、`max_tokens` 的 `0>limit` 为假、
   MCP 折叠后的恒 pass，三条路都在这一步关掉。E1 同批，它的价值在 shim
   写错的第一次就拦下，而不是等报告全绿之后靠人眼发现 `mcp.calls` 恒空。
3. **A1 降到第 4 步**：只读轨迹 + 安全类用例不需要共享 workdir，
   M1 期间 shim 约定顶住；M2 上 `file_state` / `database_state` 时 A1 必须已落地。

**（第二轮校准）三处"落点"已经明确，无需再花评审时间讨论。**
**（第三轮修订）其中两处被改口径、并新增第四处 —— 以下是当前有效的版本。**

1. **A3 的修法**：与同函数里的 `max_cost` 对齐（判定侧 + `ObservationUnavailable`），
   **触发条件是"该约束依赖的用量分量未观测"，不是"usage 全缺"**，见 §1 A3 修订五。
2. **E1 的默认值**：**已定为默认 warn + 收尾档（`release`/`nightly`）开 strict**，
   并附 M1 出场条件（warnings 里不得有协议违规），见 §5 E1 修订。
   原先"留作评审决定"的那条已收敛。
3. **D16 的落点**：§3.3 **与** §4.3 两处都要改，且**加一条用量口径守卫**，见 §4 D16。
4. **A2 的落点（第三轮新增）**：留痕复用 `metric_capability_snapshot`（键空间要区分），
   判定落**插件侧** —— `resolve_metric` 承接不了（它给的是 fallback 或 exit 3），
   见 §1 A2 修订四。

D 类文档变更与第 1/2 步同批提交 —— 条款先落地，后续实现才有可引用的依据。
D16（模型钉住 + 用量口径）不晚于第 3 步：第一次 pin 基线之前 `agent_model` 与口径
必须已是受校验字段，否则第一批基线就是可被模型漂移与口径漂移污染的。

**第 0 步的性质（第四轮校准后重述）**：它至今仍未执行（全仓库除本文档外
没有任何东西引用 `/api/chat`，也没有 shim 或冒烟脚本）。ai-chatbot 侧的
**静态事实**已由第四轮校准升级为"源码级已核实"（附录 §8），
但**运行时行为仍零实跑**——所以第 0 步依然是本方案的第一优先动作，
只是它的验收清单从"确认全部事实"收窄为"钉死五条运行时行为"（见本节末）。
反过来，harness-bench 侧的 A/B/C/D/E 五类判断在这 5 天里**没有一条被推翻**，
且其中四条（A3 的修法、A4 的落点、E3 的干净基线、A1 无时序障碍）现在有了
比初版更强的证据 —— 这些校准写在各自的章节里，不改变任何一条结论。

**第三轮修订的性质**：它**没有推翻任何结论**，改的全是落点与触发条件
（A2 的降级由谁承接、A3 判 skipped 的条件、A1 回执走哪条通道、E1 的默认值），
外加一处计数更正（`database-core` 初版成文时 15 条而非 31 条）。
四处修正的共性是同一件事：**原文把"能力/口径/回执"当成了直接可用的既有机制，
实际各缺一段落点**。实施时若发现还有第五处同类，按同一办法处理 ——
先找既有机制的**真实语义与真实键空间**，再决定复用它还是另建。

**第四轮校准对第 0 步的细化**：静态事实（载荷字段、端点、常量）已升级为
"已核实"（见附录），冒烟脚本要钉死的只剩**运行时行为**，验收清单明确为五条 ——
(1) chunk 的实际顺序（`start` → 工具 → `finish` → `[DONE]`？`data-*` part 夹在何处）；
(2) 402 发生在**流前**（HTTP 状态码）还是流内 error chunk；
(3) 审批触发后流是否干净以 `finish`+`[DONE]` 收尾，tool part 的 `state` 值实测是什么；
(4) `data-sub-open/done` 的 `id` 是否等于父流的 `toolCallId`；
(5) `data-context-usage` 出现的时机与频率（每轮一次还是每 delta 一次）。
五条对上之后，B2 映射表的"顺序与时序"部分才算从推断变成事实。

---

## 8. 附录：ai-chatbot 侧已核实坐标（第四轮校准，2026-09-29）

> 全部为**源码级静态核实**（`git grep` tracked 文件 + 直读文件；两个独立来源交叉确认：
> stage1 可行性评估报告 + 本仓复核）。**不包含运行时验证**——那仍是第 0 步的事。
> ai-chatbot 仓工作树随开发漂移，行号是 2026-09-29 的快照，信行为不信行号。

| 事实 | 坐标 |
| --- | --- |
| `POST /api/chat`：UIMessage 流 + `X-Conversation-Id` 响应头 | `apps/sime-agent/app/api/chat/route.ts:33-52`（`createUIMessageStreamResponse`） |
| `conversationId` **必填**（缺失 400 `Missing conversationId`），值由调用方自选 | `apps/sime-agent/lib/ai/chat/chat-service.ts`（`handleChatMessage` 开头） |
| license 门禁：`/api/*` 无效/过期 → `402 {error:'LICENSE_INVALID'}`；豁免 `/api/license/*`、`/nest`、`/sime`、`/.well-known/*` | `apps/sime-agent/proxy.ts:55-72` |
| `data-context-usage`：`actualInputTokens` / `cachedTokens` / `contextLimit` / `lastCompactionFreed`，**无 output token** | `packages/core/src/runtime/chat-session/run-session.ts:266-285` |
| 重试只打日志（指数退避），不发任何流事件 | `packages/core/src/foundation/model/retry-middleware.ts:125-150` |
| 子 Agent 事件 `data-sub-open` / `data-sub-done`（done 载荷含 `tokenUsage{input,output,total}` / `status`）；另有 `data-sub-async`（父流无 done，终态查 `subagent_sessions`） | `packages/core/src/extensions/subagents/event-broadcaster.ts:49-145` |
| MCP 工具命名 `mcp__<server>__<tool>` | `packages/core/src/extensions/mcp/registry.ts:26`（逐字构造） |
| 静态工具 registry：`read_file` / `write_file` / `edit_file` / `save_report` / `bash` / `grep` / `glob` / `ls` / `web_search` / `ask_user_question` / `use_skill` / cron 系列 | `packages/core/src/runtime/tools/index.ts:41-56`；`agent` / `task_*` / `exit_plan_mode` 见 `plan-mode.ts:37-53` 白名单 |
| 审批：`needsApproval` 挂起 → 流暂停 → `state:'approval-requested'` 的 tool part 落库；续跑 = 重新 POST `body.message` 含 `approval-responded`；denied 的原因在 `approval.reason` | `packages/core/src/extensions/connector/tool-adapter.ts:142-145`；`docs/zombie-approval-contracts.md` |
| 并发默认 5、队列 30（`CHAT_MAX_CONCURRENCY` / `MAX_QUEUE_SIZE` 可覆盖）；超限发 `{type:'error', errorText:'服务器繁忙，请稍后重试'}` | `packages/core/src/foundation/concurrency/types.ts:14-17`；`apps/sime-agent/lib/ai/chat/chat-service.ts:83-92,120` |
| agent 产物目录：有 projectDir 时写 `<projectDir>/outputs/<conversationId>` | `packages/core/src/api/app/resolve-agent-config.ts:602` |
| workdir 作用域：`SIACT_PROJECT_DIR` 环境变量决定项目根 | `apps/sime-agent/.env.example:27`；`apps/sime-agent/lib/utils/paths.ts:93` |
| 测试设施：vitest + Playwright，无任何 agent 行为级评测；仓内无 harness-bench / agent-eval 痕迹 | `apps/sime-agent/package.json:16-17`；`git grep agent-eval` 零命中 |

**给复核者的工具提醒**：在 ai-chatbot 仓里查东西用 `git grep`（只扫 tracked 文件）。
裸 `grep -rn` 会遍历 `node_modules`，体量大到可能中途死掉且 stderr 被 `2>/dev/null`
吞掉——本轮就因此产生过一轮"三个特征串全仓零命中"的假象，靠直读文件推翻。
