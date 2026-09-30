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
uv run agent-eval benchmark run smoke --agent http://127.0.0.1:8901 --no-judge
```

选项：`--model <name>`（A4 显式模型，进 run 元数据；缺省=平台默认）、
`--policy auto-approve`（B3 审批策略，当前唯一策略，经 /health `approval_policy` 上报）、
`--allow-public-upstream`（默认仅回环/私网上游）。

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
