# PRD：ai-chatbot 转译 shim（B 类，change-plan §2）

需求源：`docs/external-agent-integration-change-plan.md` §2（B1–B4）+ §7 第 3 步。
B2 映射表已全部实测验证（第五轮校准），shim 可照表实写。

## 交付物

`shims/ai-chatbot/`（harness-bench 仓顶层，**不进** `src/agent_eval/`——E3 边界与 E5
归一化责任的分界）：一个对上实现 harness-bench 四端点契约、对下调 ai-chatbot
原生 API 的测试侧转译组件。

### 架构

- `translator.py`：纯函数状态机（**零第三方依赖**，可被框架测试直接导入）——
  UIMessage chunk 流 → PRD §8 事件流；跨审批续跑的会话级状态。
- `server.py`：FastAPI 四端点（/health、/api/agent/sessions、/run、/cancel）+
  会话表 + 并发闸（Semaphore 4 < CHAT_MAX_CONCURRENCY 5）。
- `pyproject.toml`：独立依赖（fastapi/uvicorn/httpx），不混入框架依赖树。

### 转译规则（全部来自实测 B2 表）

- `start`（首条 POST）→ `run.started`；后续 POST 的 start 忽略（取 messageId 供续跑）
- `start-step` → `model.request`；`data-context-usage` → `model.response`
  （usage 仅输入侧：input_tokens/cached_tokens，**无 output_tokens** → A3 单侧口径）
- `tool-input-available`：`mcp__*` → `mcp.call`（name=server 段）；`bash` →
  `tool.call` + `command.started`（name=命令行首词）；其余 → `tool.call`
- `tool-output-available`：bash 结果（可能二次 JSON 编码）解码取 `exitCode` →
  `command.finished`；MCP 输出 `{content[0].text, isError}` 解码 → `mcp.result`；
  其余 → `tool.result`
- `tool-approval-request` → 挂起（不 emit run.finished）；自动批准策略：
  从已观测 chunk 重建 assistant UIMessage（approval-responded part）→ 续跑 POST；
  `finishReason="tool-calls"` 为判别信号
- `data-sub-open/done` → `subagent.started/finished`（name=agentType；id===toolCallId 已验证）；
  `data-sub-text-delta/tool-call/tool-result` → 丢弃
- `reasoning-*` / `start-step` / `finish-step` / `data-task` / `tool-input-start/delta` → 丢弃
- `finish`（finishReason=stop）→ `run.finished`{status:success, output:最后文本块}
- ai-chatbot 非 2xx（402 license/500）→ shim 透传为非 2xx（harness 归 InfraError/exit 2）

### /health 义务（PRD §6.2.1）

服务可达检查 + 观测面能力表（retry=false、context.compaction.*=false，其余 true）+
实际生效模型（shim 配置回显）。

## 已知未实测点（联调期关闭，均已登记 change-plan）

1. 审批续跑 POST 的确切形状（按源码契约构造：末条 assistant 消息 + tool part
   `state:'approval-responded'` + `approval:{id,approved:true}`，messageId 取 start chunk）；
2. 父级直连 bash 的 output 形状（解码 helper 兼容 str/obj 两种）。

## 验收

- 转译器单测（`tests/test_shim_translator.py`，synthetic chunks 对应五份真实 dump 形状）：
  basic 链序 / mcp 拆流(name=arxiv) / bash command 事件与 exit_code / approval 挂起与
  续跑消息形状 / subagent 关联 / usage 无 output 分量；
- `uv run pytest` 全绿、ruff 双绿（shims/ 在检查范围内）；
- E3 边界测试不受影响（shims/ 不在扫描范围）。
