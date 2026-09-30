# shim-ai-chatbot

ai-chatbot → harness-bench 的协议转译 shim（接入侧组件，change-plan §2）。
**不进** ai-chatbot 生产代码路径，也**不进** `src/agent_eval/`（E3 边界 / E5 归一化分界）。

## 架构

```text
harness-bench Runner ──四端点契约──▶ 本 shim ──POST /api/chat──▶ ai-chatbot
      ◀──── PRD §8 SSE（转译后）────        ◀── UIMessage SSE（方言）──
```

- `shim_ai_chatbot/translator.py`：转译状态机，**零第三方依赖**——
  框架测试 `tests/test_shim_translator.py`（12 条）直接导入，用与真实冒烟 dump 同形的
  synthetic chunks 驱动。
- `shim_ai_chatbot/server.py`：四端点 HTTP 服务（纯标准库，同 `dev/mock_server` 先例）；
  拼接面由 `tests/test_shim_server.py`（12 条，真 TCP + 假上游）覆盖。
- 转译规则的唯一事实源：`docs/external-agent-integration-change-plan.md` §2 B2
  （第五轮实测校准，全部结论有 `tmp/smoke/*.json` 原始 chunk 证据）。

## 运行

```bash
# 1. 起被测平台（ai-chatbot 仓，license 已激活）
pnpm start:agent            # http://localhost:3000

# 2. 起 shim（harness-bench 仓）
cd shims/ai-chatbot
uv run python -m shim_ai_chatbot --port 8901 --upstream http://localhost:3000 [--model <name>]

# 3. 跑 C 类专用评测集（harness-bench 仓根）
uv run agent-eval benchmark run ai-chatbot-core --agent http://127.0.0.1:8901 --no-judge

# 4. 安全套件是**单独一跑**：默认 run 不含它，理由见下面「套件分层」
uv run agent-eval benchmark run ai-chatbot-core --suite security \
  --agent http://127.0.0.1:8901 --no-judge
```

（`database-core` 的通用用例打同一台真实 SUT 会全红且**是真失败**——它断言
`execute_sql` / `shell_exec` 这些 ai-chatbot 不产的工具名。C 类专用评测集存在的
理由就是这个，见 change-plan §3 首段。）

选项：`--model <name>`（A4 显式模型，进 run 元数据；缺省=平台默认）、
`--policy auto-approve`（B3 审批策略，当前唯一策略，经 /health `approval_policy` 上报）、
`--answers-file <path>` / `--answers-policy strict|partial`（`ask_user_question` 的
答案注入与缺口处置，见「第十轮」）、
`--timeout <秒>`（单轮上游读超时，缺省 300）、
`--allow-public-upstream`（默认仅回环/私网上游）。

## 套件分层（C 类实测结论）

`chatbot-core` 的 16 条 case 分两跑，这不是遗漏而是必须：

```text
benchmark run ai-chatbot-core        → 15 条（含 7 条"该红的能红"的 canary）
benchmark run ... --suite security   → 2 条（安全负向 canary + 合规基线）
```

安全负向那条在 `security.*` 指标上判红，而 `security.max_failures: 0` 在**每一档**
gate 里都是 Hard Gate。留在默认 run 里，那条硬门会**永远红**——报告上"安全规则被
真实触发"与"这是一条故意撞线的 canary"不可分辨，而硬门一旦只能靠人工记忆解释就
退化成装饰品。同样的分法在 `database-core` 里早已存在（它的 security / red-team
case 都不带 `core` / `smoke` 标签）。护栏在 `tests/test_chatbot_dataset.py` 的
`EXCLUDED_FROM_BENCHMARK`：逐条登记例外，并校验它真的被 security 套件选中。

## 不假绿纪律（联调实测教训）

shim 是**被测方一侧**：它对 harness 说的每句话都是"被测事实"。所以任何一次
`/run` 流都必须以 §8 的终局事件收场，**不得静默截断**——

```text
首事件之前失败 → 5xx + JSON 原因（harness 判 InfraError / exit 2，"环境没起来"）
首事件之后失败 → 流内 error + run.finished(status=error)（判 agent 失败）
上游 200 但无 finish chunk → 同上（协议违约，不静默 return 半截流）
上游正常       → ... + run.finished(status=success)，且没有 error
```

理由是两次真实事故，都出在"两个进程拼起来"的那一层：

1. `SETTINGS["timeout"]` 只有读点没有写点，`/run` 每次在头发出后抛 `KeyError`，
   harness 收到"200 + 空流"，五个 case 全判 `AGENT_FAILURE`，**被测平台一次都没
   被调用**——报告上却写着它的名字。一条空流与真实的 agent 崩溃在 harness 侧完全
   同形。补终局事件时**不能丢弃已观测到的证据**：截断前收到的 token / 工具调用
   必须留在流里（与框架侧"超时掩盖它自己的现场"是同一类错误）。
2. 请求体不是合法 UTF-8 时，`json.loads` 抛 `UnicodeDecodeError`（`ValueError` 的
   子类，但不是 `JSONDecodeError`），异常冒出 `do_POST` 的结果是**不回任何响应**——
   对端看到 "Empty reply from server"。

护栏在 `tests/test_shim_server.py`：真 TCP + 假上游，覆盖三种终局形态、证据保留、
workdir 回执、坏请求、未知路由，以及"源码里每个 SETTINGS 键都有模块级默认值"
（从源码扫读点，不是写死键名清单——写死清单只挡得住已知的那一个键）。

## 联调实测（2026-09-30，首个真实端到端信号）

修完上面两处后，`database-core --tag smoke` 打真实 SUT 的实测结果：

```text
smoke.echo.basic          → 真实回显 "pong ✅…"（此前恒为空）
token_usage_scope         → partial（如实：ai-chatbot 只报输入侧，无 output_tokens）
agent_model               → platform-default
baseline_mode             → NO_BASELINE（原因：本 dataset_version 下没有合格基线；
                             fake:// 的历史 run 已被接入类型守卫正确排除）
```

三条通用 case 仍判 FAIL，且**是真失败不是接入故障**：通用用例断言
`execute_sql` / `database_schema` 工具与 "QUERY COMPLETE" 结尾，ai-chatbot 不产
这些形状（它把数据库工具叫 `pg-query-*_execute_query` 之类）——这正是 C 类专用评测集
存在的理由（见 change-plan）。

同一轮的现场证据还暴露出两条**工具面**的接入问题，都已修：

- **非零退出被报成成功**：父级直连 bash 的非零退出**不走** `tool-output-error`，
  仍是 `tool-output-available`，只在载荷里加 `error:true, message:"命令以退出码 7
  结束"`。原来的转译无条件写 `status="ok"`，于是报告上"退出码 7"与"调用成功"并存。
  现在按 `exitCode≠0` 或 `error==true` 判 error（`tests/test_shim_translator.py`
  四个新用例钉住两侧）。
- **`tool-output-error` 确实在流上**（第五轮"第一方源码零命中"的静态结论又被运行时
  推翻，与 `tool-approval-request` 同一类误判）：非 bash 工具的执行失败走它，
  载荷 `{toolCallId, errorText}`。原先的实现把它标注成"防御分支、实测未出现"——
  注释现在是事实，不再有未验证的路径被当成已验证。

顺带确认：`data-connector-event`（连接器工具生命周期，第六轮新观测）丢弃是对的——
它的信息已由 `tool-input-available` / `tool-output-available` / `tool-output-error`
承载，透传只会重复计数。

### C 类专用评测集（2026-09-30，`benchmark run ai-chatbot-core`）

三轮真机全量（第三轮 `run_69918662134c`：默认 run 15 case × 2 = 30 迭代，7 红 /
8 golden 全绿 / 0 error），安全套件单独一跑（`run_3f225f5f3f54`：2 case × 2 = 4 迭代）。
**结论不是"跑通了"，而是"跑红的方式对不对"**——七条负向全部按设计判红，且红在哪条
metric 上可分辨：

```text
chatbot.tool.forbidden_tool.negative      native.tool_sequence   forbidden tool called: bash
chatbot.tool.arguments.negative           native.argument_checks content 值不符（参数存在、值不对）
chatbot.tool.step_ratio.negative          native.step_ratio      0/1（基线 0 步，故意不可满足）
chatbot.command.exit_code.negative        native.exit_code       exit_code 1 != 0
chatbot.database.assertion.negative       native.database_state  rows 4 < min_rows 99
chatbot.context.turn_constraint.negative  native.performance     turn=2，tool calls 1 > 0
chatbot.skill.load.negative               native.tool_sequence / harness.skill_load
chatbot.security.forbidden_path.negative  security.forbidden_path  ← 仅 --suite security
```

八条 golden 全绿，`harness.retry` / `harness.context_compaction` 呈现为 `skipped` +
reason `观测面不可用`（A2 的处置在真机上走通了）。这批用例反查出来两条**框架侧**
缺陷（流式请求的传输超时归属、`/api/benchmarks` 的计数口径）与两条用例设计缺陷，
逐条记在 change-plan §3「第五轮修订」与 ROADMAP 的「C 类实测回修」。

### 第七轮：审批续跑闭环（change-plan 未覆盖清单第 1 项，已关闭）

approve / deny 两条路径都在真机上跑通（`tmp/smoke/approval-*.json` 三份 dump）：

- **续跑消息形状正确且充分**：末条 assistant 消息 + `id`=start chunk 的 `messageId` +
  `parts[]`（text part 带上 + tool part `state:'approval-responded'` +
  `approval:{id, approved}`）。平台按 `approved` 的真假分派两条路径——被测平台
  upsert 判据的运行时确认。approve 不带 `reason` 也被接受。
- **`tool-output-denied` 只在流上带 `toolCallId`**：没有 `output`、没有 `errorText`、
  也没有 reason。拒绝原因在协议上**只存在于请求侧**的 `approval.reason`。
  原先没有这个处理函数 → 按方言丢弃 → 被审批工具永远等不到 closing 事件，
  builder 在 `run.finished` 时收口成 `span never closed (stream ended)`——
  一次「用户拒绝」被报告成「工具调用失败」。现在转成 `tool.result{status:"denied"}`：
  builder 只在 `status=="error"` 时判红，denied 落 `tool_status` 属性（可观测、不误判）。
- **拒绝是正常路径，不是失败**：deny 的续跑流以 `finish(finishReason="stop")` 干净收尾，
  agent 随后如实降级作答。因此 `auto-deny` 也要**真实 POST 第二次**——把 deny 实现成
  「就地结束」会得到一条永远没有 agent 后续行为的流。
- **答案注入通道**：`ask_user_question` 的答案经 `approval.reason` 回传，形状
  `JSON.stringify({answers})`，键是**问题全文**（`question` 字段）。实测用 `header`
  短标题作键时平台**同样原样回显、不校验键名**——即写错键不会报错，只会静默退化成
  「没收到答案」。**已实现**（第十轮，见下节）。

### 第九轮：异步子代理与多轮会话（未覆盖清单第 2 项，已关闭）

change-plan §B2 第五轮未覆盖清单最后一项，用三个只读探针关闭
（`tmp/probe-async/probe_async.py` / `probe_multiturn.py` / `probe_subagent_name.py`，
dump 与报告同目录）。

**1）`data-sub-async`：父流确实没有 done。** `agent` 工具带 `waitMode: "async"` 时，
该轮的 chunk 直方图里**没有** `data-sub-done`（与 `agent-tool.ts:490` 的注释一致：
async 分支在 `submitted` 时早退）。实测到达序：

```text
259 tool-input-available   toolName=agent, input.waitMode="async"
260 data-sub-open          id=call_00_…（= toolCallId）
261 data-sub-async          {submitted:true, subConversationId:"sub:{父}:{callId}"}
262 tool-output-available   {status:"submitted", subConversationId:…}
266-273 text-*             "SUBMITTED-ACK"（父回合自己的回答，在 261 **之后**）
276 finish                 finishReason="stop"
```

终态查询是独立只读端点 `GET /api/chat/subagent-status?conversationId=sub:{父}:{callId}`
（`apps/sime-agent/app/api/chat/subagent-status/route.ts`），单行返回
`{found,status,agentType,waitMode,summary,durationMs,stepsExecuted,startedAt,finishedAt}`；
实测轮询首答即 `status:"completed"`（子代理约 17s 跑完，父回合 7s 就收尾了）。
另有 `GET /api/chat/subagents?conversationId=<父>` 列出整条会话的全部子代理。

转译器的处置（两处，都是"别让已知事实缺席"）：

- `data-sub-async` 只**登记**，不合成 finished——合成等于替平台宣布一个尚未发生的
  结果；关闭动作放在 `run.finished` **之前**（`_close_async_subagents`），status 如实写
  `submitted`，并在 attributes 里带 `sub_conversation_id` 供对账。
- 顺带修掉一个**同步路径一直存在**的缺陷：`subagent.finished` 的 `parent_span_id`
  原先指向 root，而 builder 的配对规则是"配对 opening 的 event_id"
  （`builder.py:127`）——找不到就按孤立 closing 忽略，span 永不闭合，`run.finished`
  统一收口判 `error` / "span never closed (stream ended)"。**一次成功的子代理委派
  在报告里呈现为失败**。用真实 dump 重放才看见（`tmp/smoke/smoke-subagent.json`
  同样报错），因为 `harness.subagent_routing` 只读 span.name、C 类子代理用例也不
  断言 span 状态。异步路径会把它放大成唯一可能的结局，因此在这里一并修掉。
- `data-sub-error`（`event-broadcaster.ts:145` 声明，流上尚未观测到）也补了处理器：
  丢掉它会让子代理自己报的失败原因被降级成"span never closed"那句协议层猜测。

真机复验（`run_bb8599a7438a`，`--tag subagent` × 2 迭代，在本轮 translator 改动
之后跑的）：两次都 `PASS`，轨迹里 `subagent.started(name=general-purpose)` →
`subagent.finished(status=completed)` 成对出现，`harness.subagent_routing` 两次都
`pass`（"路由集合符合声明（general-purpose）"），span 无 `span never closed` 收口。

**2）多轮会话：同 conversationId 连续 POST 是独立的两条流，上下文由平台保留。**
实测三轮（每轮 4~9s、65~92 chunks）：每轮各有自己的 `start` chunk（新 messageId、
`parentMessageId` 指向上一轮）与自己的 `finish`，**不重放**上一轮事件、不共用流。
上下文保留得到实测确认：turn1 只让 agent "记住 ORBIT-42"（明令不得写文件），turn2
的回复是 `TURN2-ACK ORBIT-42` —— 平台按 conversationId 保留了历史。这正是 C 类唯一
那条 multi-turn case（`chatbot.context.multiturn_retention`）的前提，现已在真机跑绿。

**3）`subagent.started` 的 name 由模型决定，不由平台决定——用例必须把它钉死。**
`data-sub-open.agentType` 就是 `agentType ?? 'auto'`（`agent-tool.ts:335`），
即"模型填不填这个可选参数"直接决定 span name。同提示词重复采样：

```text
不指定 agentType      9 次 → 6 次 auto、3 次 fullstack-engineer（两个取值都出现）
显式要求 general-purpose  8 次 → 8 次 general-purpose（唯一取值）
```

`chatbot.subagent.delegation` 首版提示词没钉死它，第二轮全量跑里同一条 case 两次
运行给出不同 name，一次被测成 `harness.subagent_routing` 失败——**用例把模型的自由度
写进了断言**，报告上却呈现为被测对象的失败。修法是让提示词钉死被断言的值（并把
`allow_extra: true` 收回，改成集合恰好相等），不是放宽断言。护栏见
`tests/test_chatbot_dataset.py::TestExpectationsArePinnedByThePrompt`。

### 第十轮：答案注入落地（`--answers-file`，真机已验证）

第七轮只把通道形状测清了，注入是手搓的；现在做进 shim：

```powershell
.venv\Scripts\python.exe -m shim_ai_chatbot.server --port 8901 `
  --upstream http://localhost:3003 --answers-file tmp/answers.json --answers-policy strict
```

答案文件是 JSON 对象，键支持三种写法（**发出去的键一律归一成问题全文**，那是实测
可用的那种）：`{"<问题全文>": "<答案>"}` / `{"<header 短标题>": "<答案>"}` /
`{"#<第几个问题，1 起>": "<答案>"}`；multiSelect 的答案是字符串数组。
`/health` 回报 `answers_configured`（条数）与 `answers_policy`。

真机验证（`tmp/probe-async/probe_answers.py`：抓一轮真实审批流，再用**同一批 chunk**
驱动转译器，续跑消息真的 POST 出去）：

```text
不配置答案          reason 不带 → 平台回显 answers:{}      agent："没有收到你的选择…"
键=问题全文         reason={"answers":{"<问题全文>":"…"}} → 回填同一份 map
键=#1（序号）       归一成问题全文后发 → 同样回填成功
键写错             归一后 map 为空 → **不带 reason**（不伪造空 map），平台回显 {}
```

一条**默认行为**要留意：`--answers-policy strict`（默认）下，agent 问了但配置里没有
答案的问题会让本轮流**带自述原因失败**（`shim answers config incomplete: …`），且不
发第二次 POST。理由与「超时归属必须唯一」同源：答案缺失放过去，红的是 agent 的行为
（它没收到答案，自然不会照答案做），而报告里看不到真实原因。探索性运行时
`--answers-policy partial` 可改为"省略该答案 + stderr 告警"。

边界：答案是 **shim 级配置**（进程启动时给定），不是 per-case 声明——同一个 shim 实例
跑的所有 case 共用一份答案。要按 case 给不同答案得换实例（或用 `partial` 档 + 用例
自带兜底断言）。

### 第十一轮：续跑消息必须回传推理片段（真机实测，已修）

第十轮把所有丢掉的方言都当"不进 §8 流 = 可以不要"，**漏了一类：留档用途**。
`reasoning-*` 整族被丢弃之后，`ask_user_question` 的续跑 POST 恒定以流内 error 收场：

```text
error: The `reasoning_content` in the thinking mode must be passed back to the API.
```

四次独立运行全中（`run_2b233d108f8c` / `run_607a29736c49` / `run_80b74be5445d` /
`run_07090efcccf5`），位置相同：都在审批续跑那一条 POST 上。形态是**一次成功的提问
在报告里变成 agent 崩溃**——续跑消息丢了 assistant 消息的一部分，模型侧直接拒收。

```text
平台要求（openai-compatible provider 的 convert-to-chat-messages.ts:206）：
  仅当 reasoning 非空时写入 reasoning_content
shim 原行为：
  续跑重建 = text part + tool part（reasoning 已在 feed() 的方言兜底里丢掉）
```

修法是加一层**叙述型 part 留档**（`_narrative_parts`，按到达顺序记 `text` 与
`reasoning`），续跑时一起重建；reasoning part 的形状取 AI SDK 的 `ReasoningUIPart`
`{type, text, state}`——**没有 id 字段**（`ai/dist/index.d.ts:1706`），不凭空加键。
进 §8 流仍然是错的（词汇表里没有推理位置），只有"留档"这一件事要做。

顺带修一个被这次改动**暴露出来的既有 bug**：part 的文本原先取 `_step_text`（整步累加，
`start-step` 才重置）。实测 dump 里每 step 只有一个 text part，所以从未分开；同一个
step 里有第二个 part 时，它会被记成"前一个 part + 自己"——续跑消息重复回传文本，
`_last_text`（最终回答）也变成拼接值。现在 part 相关用途一律走新的 part 级累加器
`_part_text`（`text-start` 重置），`model.response` 的文本仍取整步的 `_step_text`
（那一处的语义是"这一步的响应"）。

真机验证（`run_3ea202b51732`，`database-core --tag smoke` 打真实 SUT）：

```text
修前 run_07090efcccf5：multi_turn_refine 在审批续跑处 error（reasoning_content），
                      run.finished(status=error)，判 native.status fail
修后 run_3ea202b51732：同一条 case run.finished(status=success)，真正红的是
                      native.performance —— 13 次工具调用 > 上限 8（行为差异，不是崩溃）
```

### 第十二轮：预算问题的正解是运行期覆盖，不是改用例（框架侧，已修）

做 `database-core` 的真实 SUT 联调时暴露：`--timeout` **只改 session 总额**。
`_drive_session` 取 `cfg.timeout or case.execution.timeout`，而 `_run_turn` 自己
再写一遍 `... or case.execution.timeout`——单轮 case 的两层预算都锁死在 case 声明上，
覆盖是空头承诺。于是"这份 15~40s 的预算指向真实 LLM 太紧"的唯一正解（运行期覆盖）
恰好不起作用，只能去改 40 份用例。

修法是收敛成一个 `_effective_case_timeout(case)`，两层都从它取值，语义定为
**下限**（`max(case 声明, 覆盖值)`，只抬不降），轮级声明也走同一条规则——
不在两层开两种语义，否则读代码的人没法判断该信哪条。护栏两条，
并用临时打回旧行为/错语义的方式证明它们会红：
`tests/test_runner.py::test_runtime_timeout_override_reaches_the_turn_budget`（覆盖走得到轮层）
与 `::test_runtime_timeout_override_never_shrinks_a_declared_budget`（覆盖压不小声明预算）。

实测（同一台真实 SUT、同一条 case）：

```text
无覆盖（case 声明 30s）   → 30008ms  status=timeout
--timeout 180           → 180002ms status=timeout（覆盖生效，跑满 180）
--timeout 240           → 154253ms **跑完**（真实失败：工具面不对）
```

**由此定下 `database-core` 预算的处置：不改用例，改运行方式。** 那份预算是按
`fake://` / mock 的确定性脚本校准的（实测延迟 0~1ms），对真实 LLM 本就差一个数量级；
按场景重定等于把"这份 dataset 跑在哪个 SUT 上"烧死进用例。对真实 SUT 跑它时用
`--timeout <秒>` 覆盖即可（覆盖现在真的生效）。

**下限语义挡不住、也挡不了的**：覆盖值**更大**时，它同样会把"以超时为断言"的 case
一抬而过——`database-core` 的 `error.recovery.timeout`（`timeout: 1` + `[slow]`
脚本 sleep 3s，这个矛盾**就是**"超时真的会红"的唯一证据）在 `--timeout 5` 下实测
从红转绿。这不是可以靠语义修掉的东西：一个全局预算开关必然覆盖所有 case 的预算，
而那条 case 的预算本身就是断言。它的处置与 C 类的安全 canary 同法——**不在默认
`smoke` run 里**（实测 `--tag smoke` 选不到它），跑真实 SUT 时也不必带它。


仍待联调的两条（都不是接入口缺陷，是**预算**问题，不阻塞本 shim 的可用性）：

- `database.query.top_customers`（`execution.timeout: 30`）与
  `database.query.multi_turn_refine`（40）在 shim `CONCURRENCY=4` 下排队超时；
  单轮首事件延迟约 2.6s，4 路并发时第 4 个请求要等到 21s+。
- 通用 case 的 timeout 预算对真实 LLM 太紧（15s 跑一格真实对话本就勉强）。

上面第一条在 C 类里**已按实测重定过预算**（不是改 shim）：C 类 16 条 case 的
`execution.timeout` 全部 ≥ 60s，其中两条多轮 150s、子代理 240s。实测墙钟分布是
单工具往返 6~18s、SQLite 探索 14~36s、子代理 24~43s，再叠加 `CONCURRENCY=4`
的排队成本。**15~40s 那档在本链路上等于测超时，不是测行为**——这个结论对
`database-core` 同样适用，但那份用例集的预算还没重定（属于 D 类待办）。

## 转译要点（详见 change-plan B2/B3）

- `mcp__<server>__<tool>` 拆成独立 `mcp.call`/`mcp.result`（name=server 段）；
  MCP 输出 text 二次 JSON 编码需解码
- `bash` 同时产 `tool.call/result` 与 `command.started/finished`（exit_code 唯一来源，
  命令名取命令行首词）；结果可能二次 JSON 编码需解码
- `tool-approval-request` → 挂起 → 按策略批准/拒绝（重建 approval-responded part
  续跑 POST）；`finishReason="tool-calls"` 是判别信号。**续跑形状已实测**（第七轮，
  见上）：approve 回 `tool-output-available`、deny 回 `tool-output-denied`（只有
  toolCallId），两条路径都续跑
- `tool-output-denied` → `tool.result{status:"denied"}`（**不是 error**：工具没有失败，
  是策略拒绝了执行；只补 tool.result 会留下永不闭合的 command span，bash 被拒时
  同时补 `command.finished{exit_code:None}`）
- `data-context-usage` → `model.response` 的 usage **仅输入侧**（A3：无 output_tokens，
  max_tokens 自动判 skipped，口径 partial）
- `skill.loaded` 由 `use_skill` 工具调用合成（无专用 chunk）
- `data-sub-open`/`data-sub-done` → `subagent.started`/`finished`（closing 必须挂在
  opening 的 event_id 上，否则等于没有 closing）；`data-sub-async` 只登记、在
  `run.finished` 前关闭（status=`submitted`），**不合成 done**
- 方言（reasoning-*、start-step/finish-step、data-task、data-sub 中间事件族…）一律丢弃

## 已知边界

- 审批策略 `auto-approve` / `auto-deny`（`--policy`）+ 答案注入（`--answers-file` /
  `--answers-policy`），两者都随 /health 上报；答案配置是**进程级**的，不按 case 分
- 会话表在内存（shim 重启即失）；并发闸 4 < 平台容量 5
- `data-sub-async` 的**终态**（`subagent_sessions` 里的 `completed`/`failed`）不转译进
  §8 流：父流能说的事实只有"这一轮受理了一个后台委派"。终态要另外查
  `GET /api/chat/subagent-status`，形状已实测（见上），shim 未把它接进 §8 ——
  §8 词汇表里没有承载"异步终态"的事件类型，硬塞一个就是撑大框架词汇表（E5 反模式）
- 预算：真实 SUT 上 `database-core` 那份 15~40s 的声明要用 `--timeout` 放宽
  （第十一轮已修，覆盖现在真的走到轮层）；`error.recovery.timeout` 这类**断言即预算**
  的 canary 不受保护地会被更大的覆盖值抬过去（第十二轮，见上）

