# shim-ai-chatbot

ai-chatbot → harness-bench 的协议转译 shim（接入侧组件，change-plan §2）。
**不进** ai-chatbot 生产代码路径，也**不进** `src/agent_eval/`（E3 边界 / E5 归一化分界）。

## 架构

```text
harness-bench Runner ──四端点契约──▶ 本 shim ──POST /api/chat──▶ ai-chatbot
      ◀──── PRD §8 SSE（转译后）────        ◀── UIMessage SSE（方言）──
```

- `shim_ai_chatbot/translator.py`：转译状态机，**零第三方依赖**——
  框架测试 `tests/test_shim_translator.py` 直接导入，用与真实冒烟 dump 同形的
  synthetic chunks 驱动。
- `shim_ai_chatbot/server.py`：四端点 HTTP 服务（纯标准库，同 `dev/mock_server` 先例）。
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
```

理由是一次真实事故：`SETTINGS["timeout"]` 只有读点没有写点，`/run` 每次在头发出后
抛 `KeyError`，harness 收到"200 + 空流"，五个 case 全判 `AGENT_FAILURE`，
**被测平台一次都没被调用**——报告上却写着它的名字。一条空流与真实的 agent 崩溃
在 harness 侧完全同形。护栏在 `tests/test_shim_server.py`（真 TCP + 假上游，
跨 `server.py` × `translator.py` 的拼接面；此前 12 条转译单测全绿而链路恒空流）。

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
