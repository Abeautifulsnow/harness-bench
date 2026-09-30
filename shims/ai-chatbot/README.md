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

三轮真机全量（第三轮 `run_69918662134c`：15 case × 2 = 30 迭代，7 红 / 0 error），
安全套件单独一跑（`run_3f225f5f3f54`：2 case × 2 = 4 迭代）。**结论不是"跑通了"，
而是"跑红的方式对不对"**——七条负向全部按设计判红，且红在哪条 metric 上可分辨：

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

六条 golden 全绿（含 `subagent.delegation`：`subagent.started` 的 name 实测 `auto`），
`harness.retry` / `harness.context_compaction` 呈现为 `skipped` + reason
`观测面不可用`（A2 的处置在真机上走通了）。这批用例反查出来两条**框架侧**缺陷
（流式请求的传输超时归属、`/api/benchmarks` 的计数口径）与两条用例设计缺陷，
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
  「没收到答案」。C 类用例作者按 `question` 全文拼答案。

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
- 方言（reasoning-*、start-step/finish-step、data-task、data-sub 中间事件族…）一律丢弃

## 已知边界

- 审批策略 `auto-approve` / `auto-deny`（`--policy`，随 /health 上报）；
  策略的「答案」注入（`ask_user_question` 的 answers）尚不实现——当前续跑消息不带
  `approval.reason`，平台因此回显空答案、agent 降级追问（实测行为，非缺陷）。
  C 类用例需要「设计好的答案」时再补（通道与形状已实测，见上）
- 会话表在内存（shim 重启即失）；并发闸 4 < 平台容量 5
- `data-sub-async`（异步子代理）按"父流无 done"如实不合成 finished——终态查询属联调项
