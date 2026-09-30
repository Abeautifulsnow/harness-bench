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

# 3. 跑评测（harness-bench 仓根）
uv run agent-eval benchmark run database-core --tag smoke --agent http://127.0.0.1:8901 --no-judge
```

选项：`--model <name>`（A4 显式模型，进 run 元数据；缺省=平台默认）、
`--policy auto-approve`（B3 审批策略，当前唯一策略，经 /health `approval_policy` 上报）、
`--timeout <秒>`（单轮上游读超时，缺省 300）、
`--allow-public-upstream`（默认仅回环/私网上游）。

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
这些形状——这正是 C 类专用评测集存在的理由（见 change-plan）。

同一轮还暴露两条待办（不阻塞本 shim 的可用性）：

- `database.query.top_customers`（`execution.timeout: 30`）与
  `database.query.multi_turn_refine`（40）在 shim `CONCURRENCY=4` 下排队超时；
  单轮首事件延迟约 2.6s，4 路并发时第 4 个请求要等到 21s+。
- 通用 case 的 timeout 预算对真实 LLM 太紧（15s 跑一格真实对话本就勉强）。

## 转译要点（详见 change-plan B2/B3）

- `mcp__<server>__<tool>` 拆成独立 `mcp.call`/`mcp.result`（name=server 段）；
  MCP 输出 text 二次 JSON 编码需解码
- `bash` 同时产 `tool.call/result` 与 `command.started/finished`（exit_code 唯一来源，
  命令名取命令行首词）；结果可能二次 JSON 编码需解码
- `tool-approval-request` → 挂起 → 自动批准（重建 approval-responded part 续跑 POST）；
  `finishReason="tool-calls"` 是判别信号。**续跑请求形状尚未实测**（change-plan
  第五轮未覆盖清单第 1 项）——联调第一优先验证点
- `data-context-usage` → `model.response` 的 usage **仅输入侧**（A3：无 output_tokens，
  max_tokens 自动判 skipped，口径 partial）
- `skill.loaded` 由 `use_skill` 工具调用合成（无专用 chunk）
- 方言（reasoning-*、start-step/finish-step、data-task、data-sub 中间事件族…）一律丢弃

## 已知边界

- 审批策略仅 `auto-approve`；deny 路径未实现（未覆盖清单第 1 项关闭后再扩）
- 会话表在内存（shim 重启即失）；并发闸 4 < 平台容量 5
- `data-sub-async`（异步子代理）按"父流无 done"如实不合成 finished——终态查询属联调项
