"""UIMessage chunk 流 → PRD §8 事件流 转译器（change-plan §2 B2，第五轮实测校准版）。

零第三方依赖：框架测试（tests/test_shim_translator.py）直接导入本模块，
以与真实冒烟 dump（tmp/smoke/*.json）同形的 synthetic chunks 驱动。

一个 run（turn）= 一条 PRD §8 事件流；审批挂起时由同一条流内的续跑 POST 拼接
（对 harness-bench 透明：1 turn = 1 run 调用，run.finished 才结算）。

转译规则的唯一事实源是 change-plan §2 B2（第五轮实测校准）；本文件注释只在
规则无法自表达处引用它，不复制表格。
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

# 审批续跑轮数上限：防"批准→又挂起"死循环（每次续跑消耗一轮）
MAX_APPROVAL_ROUNDS = 10


def _now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat()


def _new_event_id() -> str:
    return f"evt-{uuid.uuid4().hex[:12]}"


def maybe_decode_json(value: object) -> object:
    """bash / MCP 的 result 常是二次 JSON 编码的字符串（实测），解码一次；非 JSON 原样返回。"""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                return value
    return value


class TranslationSession:
    """一次 run 的转译会话（跨审批续跑的多条 POST 流拼接为一条 §8 流）。

    feed() 消费一条被测平台 SSE data 载荷（已解析为 dict），返回 0..n 条 PRD §8
    事件（dict，字段与 harness-bench 的 TraceEvent 对齐）。suspend 状态由 server
    层检查：suspended=True 时构造续跑请求、再次 POST 并继续 feed。
    """

    def __init__(
        self,
        trace_id: str,
        user_message: str,
        *,
        model: str | None = None,
        policy: str = "auto-approve",
        max_approval_rounds: int = MAX_APPROVAL_ROUNDS,
    ) -> None:
        self.trace_id = trace_id
        self.user_message = user_message
        self.model = model
        self.policy = policy
        self.max_approval_rounds = max_approval_rounds
        self.approval_rounds = 0
        self.suspended = False  # True = 审批挂起，server 应构造续跑请求再 POST
        self.finished = False

        self._started = False
        self._root_id = ""
        self._message_id = ""  # start chunk 的 messageId（续跑重建 assistant 消息用）
        self._step = 0
        self._step_text = ""
        self._last_text = ""  # 最近完成的 text part = 最终回答
        self._model_request_id = ""  # 当前 step 的 model.request 事件 id
        # toolCallId -> {"kind": tool|bash|mcp, "name", "server", "input",
        #                "call_event_id", "command_event_id", "output"}
        self._tools: dict[str, dict] = {}
        self._pending: list[dict] = []  # 审批挂起项
        self._text_parts: list[dict] = []  # 续跑重建用：{id, text}

    # ------------------------------------------------------------------ emit

    def _event(self, etype: str, data: dict, parent: str | None = None) -> dict:
        return {
            "event_id": _new_event_id(),
            "trace_id": self.trace_id,
            "parent_span_id": parent,
            "type": etype,
            "timestamp": _now_iso(),
            "data": data,
        }

    # ------------------------------------------------------------------ feed

    def feed(self, chunk: dict) -> list[dict]:
        ctype = str(chunk.get("type", ""))
        handler = getattr(self, f"_on_{ctype.replace('-', '_').replace('.', '_')}", None)
        if handler is not None:
            return handler(chunk)
        # 其余方言（reasoning-*、start-step/finish-step、tool-input-start/delta、
        # data-task、data-sub-text-delta/tool-call/tool-result…）一律丢弃（B2 表）
        return []

    # ---- 流生命周期 ----

    def _on_start(self, chunk: dict) -> list[dict]:
        self._message_id = str(chunk.get("messageId", "")) or self._message_id
        if self._started:
            return []  # 审批续跑的第二条流：start 忽略（run.started 已发过）
        self._started = True
        self._root_id = _new_event_id()
        started = self._event("run.started", {"message": self.user_message})
        started["event_id"] = self._root_id  # 后续事件挂在这个根下
        self._root_id = started["event_id"]
        return [started]

    def _on_start_step(self, chunk: dict) -> list[dict]:
        self._step += 1
        self._step_text = ""
        request = self._event("model.request", {"model": self.model}, parent=self._root_id)
        self._model_request_id = request["event_id"]
        return [request]

    def _on_text_start(self, chunk: dict) -> list[dict]:
        return []

    def _on_text_delta(self, chunk: dict) -> list[dict]:
        self._step_text += str(chunk.get("delta", ""))
        return []

    def _on_text_end(self, chunk: dict) -> list[dict]:
        if self._step_text:
            self._last_text = self._step_text
            self._text_parts.append({"id": str(chunk.get("id", "")), "text": self._step_text})
        return []

    def _on_data_context_usage(self, chunk: dict) -> list[dict]:
        # A3 单侧口径：usage 只有输入侧（actualInputTokens/cachedTokens），
        # **不构造 output_tokens**——harness 据此把 max_tokens 判 skipped、口径记 partial。
        usage = chunk.get("data") or chunk
        model_response = self._event(
            "model.response",
            {
                "text": self._step_text,
                "usage": {
                    "input_tokens": int(usage.get("actualInputTokens") or 0),
                    "cache_tokens": int(usage.get("cachedTokens") or 0),
                },
            },
            parent=self._model_request_id,
        )
        return [model_response]

    # ---- 工具调用 ----

    def _on_tool_input_available(self, chunk: dict) -> list[dict]:
        call_id = str(chunk.get("toolCallId", ""))
        tool_name = str(chunk.get("toolName", ""))
        tool_input = chunk.get("input") or {}
        events: list[dict] = []
        if tool_name.startswith("mcp__"):
            # B2：mcp__<server>__<tool> 前缀实测成立 → 拆成独立 mcp.call（name=server 段，
            # 安全规则 forbidden_mcp 匹配的是 server 名）
            segments = tool_name.split("__")
            server = segments[1] if len(segments) > 1 else tool_name
            call = self._event(
                "mcp.call", {"name": server, "arguments": tool_input}, parent=self._root_id
            )
            self._tools[call_id] = {
                "kind": "mcp",
                "name": server,
                "tool_name": tool_name,
                "input": tool_input,
                "call_event_id": call["event_id"],
                "dynamic": bool(chunk.get("dynamic")),
            }
            events.append(call)
        else:
            call = self._event(
                "tool.call", {"name": tool_name, "arguments": tool_input}, parent=self._root_id
            )
            entry: dict = {
                "kind": "tool",
                "name": tool_name,
                "input": tool_input,
                "call_event_id": call["event_id"],
                "dynamic": bool(chunk.get("dynamic")),
            }
            events.append(call)
            if tool_name == "bash":
                # bash 同时是 command.* 观测面（Spec §19.4 exit_code 的唯一来源）：
                # 命令名取命令行首词（forbidden_commands 匹配"可执行名"）
                command = str(tool_input.get("command", ""))
                words = command.split()
                command_started = self._event(
                    "command.started",
                    {"name": words[0] if words else "bash", "arguments": {"command": command}},
                    parent=self._root_id,
                )
                entry["kind"] = "bash"
                entry["command"] = command
                entry["command_event_id"] = command_started["event_id"]
                events.append(command_started)
            self._tools[call_id] = entry
        return events

    def _on_tool_output_available(self, chunk: dict) -> list[dict]:
        call_id = str(chunk.get("toolCallId", ""))
        entry = self._tools.get(call_id)
        output = maybe_decode_json(chunk.get("output"))
        if entry is not None:
            entry["output"] = output
        kind = (entry or {}).get("kind", "tool")
        if kind == "mcp":
            is_error = bool(isinstance(output, dict) and output.get("isError"))
            text = ""
            if isinstance(output, dict):
                content = output.get("content") or []
                if content and isinstance(content[0], dict):
                    text = str(content[0].get("text", ""))
            return [
                self._event(
                    "mcp.result",
                    {"status": "error" if is_error else "ok", "result": maybe_decode_json(text)},
                    parent=(entry or {}).get("call_event_id"),
                )
            ]
        if kind == "bash":
            parsed = output if isinstance(output, dict) else {}
            result = self._event(
                "tool.result",
                {"status": "ok", "result": parsed},
                parent=(entry or {}).get("call_event_id"),
            )
            command_finished = self._event(
                "command.finished",
                {"status": "ok", "exit_code": parsed.get("exitCode")},
                parent=(entry or {}).get("command_event_id"),
            )
            return [result, command_finished]
        return [
            self._event(
                "tool.result",
                {"status": "ok", "result": output},
                parent=(entry or {}).get("call_event_id"),
            )
        ]

    def _on_tool_output_error(self, chunk: dict) -> list[dict]:
        # 防御分支：实测流未出现该类型（B2 标注待实测），出现时按错误结果转译
        call_id = str(chunk.get("toolCallId", ""))
        entry = self._tools.get(call_id)
        event_type = "mcp.result" if (entry or {}).get("kind") == "mcp" else "tool.result"
        return [
            self._event(
                event_type,
                {"status": "error", "error": str(chunk.get("errorText", ""))},
                parent=(entry or {}).get("call_event_id"),
            )
        ]

    # ---- 审批（B3：轮次即结束、等待在客户端、续跑重新 POST） ----

    def _on_tool_approval_request(self, chunk: dict) -> list[dict]:
        call_id = str(chunk.get("toolCallId", ""))
        entry = self._tools.get(call_id, {})
        self._pending.append(
            {
                "approval_id": str(chunk.get("approvalId", "")),
                "tool_call_id": call_id,
                "tool_name": str(entry.get("tool_name", entry.get("name", ""))),
                "input": entry.get("input"),
                "dynamic": bool(entry.get("dynamic")),
            }
        )
        return []  # 审批是方言：不进 §8 流；挂起状态由 suspend + 续跑表达

    def _on_finish(self, chunk: dict) -> list[dict]:
        reason = str(chunk.get("finishReason", "stop"))
        if reason == "tool-calls" and self._pending:
            if self.policy == "auto-approve" and self.approval_rounds < self.max_approval_rounds:
                self.approval_rounds += 1
                self.suspended = True  # server 构造续跑请求后再次 POST，翻译继续
                return []
            # 无策略/超轮数：如实按 error 收尾（不伪造成功）
            return self.fail(f"approval suspended (finishReason={reason})")
        status = "error" if reason == "error" else "success"
        finished = self._event(
            "run.finished", {"status": status, "output": self._last_text}, parent=self._root_id
        )
        self.finished = True
        return [finished]

    def _on_error(self, chunk: dict) -> list[dict]:
        message = chunk.get("errorText") or chunk.get("message") or "agent error"
        return self.fail(str(message))

    # ---- 终局出口（唯一） ----

    def fail(self, message: str) -> list[dict]:
        """以 error + run.finished(status=error) 收场（幂等）。

        这是本转译器**唯一**的失败出口，server 层的三条路径（上游失败、审批续跑
        构造失败、上游 200 但无 finish chunk）全部经由它，形状因此不可能漂移。
        它存在的理由是不假绿：一条没有终局事件的 §8 流在 harness 侧只表现为
        "流提前结束"，与真实的 agent 崩溃不可分辨（联调实测教训）。
        """
        if self.finished:
            return []
        error = self._event("error", {"message": message})
        finished = self._event(
            "run.finished", {"status": "error", "output": self._last_text}, parent=self._root_id
        )
        self.finished = True
        return [error, finished]

    # ---- 子代理（实测：全事件族 id === toolCallId） ----

    def _on_data_sub_open(self, chunk: dict) -> list[dict]:
        data = chunk.get("data") or {}
        return [
            self._event("subagent.started", {"name": data.get("agentType")}, parent=self._root_id)
        ]

    def _on_data_sub_done(self, chunk: dict) -> list[dict]:
        data = chunk.get("data") or {}
        return [
            self._event("subagent.finished", {"status": data.get("status")}, parent=self._root_id)
        ]

    # ------------------------------------------------------------------ 续跑

    def build_resume_message(self) -> dict | None:
        """构造审批续跑的 POST body.message（末条 assistant 消息，含 approval-responded part）。

        形状依据 ai-chatbot `docs/zombie-approval-contracts.md` + chat-service.ts 的
        upsert 判据（part.state === 'approval-responded'）。**尚未实测**（change-plan
        第五轮未覆盖清单第 1 项）——联调第一优先验证点。
        """
        if not self._pending or not self._message_id:
            return None
        parts: list[dict] = []
        # 已完成的 text part 原样带上（服务端按 id 整体覆盖该消息）
        for part in self._text_parts:
            rebuilt = {"type": "text", "text": part["text"]}
            if part["id"]:
                rebuilt["id"] = part["id"]
            parts.append(rebuilt)
        for call_id, entry in self._tools.items():
            pending = next((p for p in self._pending if p["tool_call_id"] == call_id), None)
            part: dict = {"toolCallId": call_id, "input": entry.get("input")}
            if entry.get("dynamic"):
                part["type"] = "dynamic-tool"
                part["toolName"] = entry.get("tool_name") or entry.get("name")
            else:
                part["type"] = f"tool-{entry['name']}"
            if pending is not None:
                part["state"] = "approval-responded"
                part["approval"] = {"id": pending["approval_id"], "approved": True}
            elif "output" in entry:
                part["state"] = "output-available"
                part["output"] = entry["output"]
            else:
                part["state"] = "input-available"
            parts.append(part)
        return {"id": self._message_id, "role": "assistant", "parts": parts}
