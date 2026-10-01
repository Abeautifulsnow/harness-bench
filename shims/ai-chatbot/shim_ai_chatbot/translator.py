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


def resolve_answers(
    questions: list[dict], configured: dict[str, object]
) -> tuple[dict[str, object], list[str], list[str]]:
    """把配置的答案对齐到本轮**实际被问的**问题。

    返回 ``(answers, unanswered, unused)``：

    - ``answers``：键是 **question 全文**（实测可用的那种键，第七轮）；
    - ``unanswered``：agent 问了、配置里没有的问题（答案缺失的**唯一**判据）；
    - ``unused``：配置里没被任何问题命中的键（写错键的表现，第七轮实测：
      平台不校验键名，写错了只会静默返回一份查不到的答案）。

    三种键写法都认（question 全文 / header / ``#<1-based 序号>``），按此顺序取第一个
    命中的。放宽是**为了吸收写法差异**，不是为了让缺口看不见：``unanswered`` 仍然按
    问题算，与用哪种键写法无关。
    """
    answers: dict[str, object] = {}
    unanswered: list[str] = []
    used: set[str] = set()
    for index, question in enumerate(questions, start=1):
        text = str(question.get("question", ""))
        header = str(question.get("header", ""))
        for key in (text, header, f"#{index}"):
            if key and key in configured:
                answers[text] = configured[key]
                used.add(key)
                break
        else:
            if text:
                unanswered.append(text)
    unused = [key for key in configured if key not in used]
    return answers, unanswered, unused


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
        policy: str = "auto-approve",  # auto-approve | auto-deny（B3 策略名，进 /health）
        max_approval_rounds: int = MAX_APPROVAL_ROUNDS,
        answers: dict[str, object] | None = None,
        answers_policy: str = "strict",  # strict | partial（见 resolve_answers 的说明）
    ) -> None:
        self.trace_id = trace_id
        self.user_message = user_message
        self.model = model
        self.policy = policy
        self.max_approval_rounds = max_approval_rounds
        # 审批答案（`ask_user_question` 的 answers）：None = 不注入（保持第七轮行为）。
        self.answers = answers
        self.answers_policy = answers_policy
        self.answer_gaps: list[str] = []  # 问了却没配到答案的问题（本 run 的接入侧缺陷）
        self.unused_answers: list[str] = []  # 配了却没被问到的键（多半是写错了）
        self.approval_rounds = 0
        self.suspended = False  # True = 审批挂起，server 应构造续跑请求再 POST
        self.decision = "approve"  # 本次挂起的决定（B3 策略的产物，续跑消息据它构造）
        self.finished = False

        self._started = False
        self._root_id = ""
        self._message_id = ""  # start chunk 的 messageId（续跑重建 assistant 消息用）
        self._step = 0
        self._step_text = ""
        # part 级累加器：`text-start` 是**每个** text part 的起点，所以 part 的文本
        # 不能拿整步的 `_step_text` 当（那样第二个 part 会连前一个一起记，续跑会把
        # 文本重复回传）。`_step_text` 只留给"这一步的响应文本"（model.response）。
        self._part_text = ""
        self._last_text = ""  # 最近完成的 text part = 最终回答
        self._model_request_id = ""  # 当前 step 的 model.request 事件 id
        # toolCallId -> {"kind": tool|bash|mcp, "name", "server", "input",
        #                "call_event_id", "command_event_id", "output"}
        self._tools: dict[str, dict] = {}
        self._pending: list[dict] = []  # 审批挂起项
        # 续跑重建用的**叙述型 part**，按到达顺序记 text / reasoning 两种。
        # reasoning 不得不带：第十一轮实测，平台在 thinking 模式下要求把
        # reasoning_content 回传，只重建 text + tool part 会让续跑 POST 直接
        # 以 `The reasoning_content in the thinking mode must be passed back to
        # the API` 失败——形态是"一次成功的提问在报告里变成 agent 崩溃"。
        self._narrative_parts: list[dict] = []
        self._step_reasoning = ""
        # toolCallId -> {"sub_conversation_id"}：本 run 受理过的**异步**子代理委派。
        # 它们不会收到 data-sub-done（见 _on_data_sub_async），为避免 builder 把
        # 未闭合的 subagent span 收口成 error，在 run.finished 前显式关闭。
        self._async_subs: dict[str, dict] = {}
        # toolCallId -> started 事件的 event_id。builder 的配对规则是
        # "closing.parent_span_id == opening.event_id"（builder.py:127），
        # 不记这个 id 就只能挂在 root 上 → 找不到 span → 静默忽略 → 未闭合 → 判 error。
        self._sub_spans: dict[str, str] = {}

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
        # 其余方言（start-step/finish-step、tool-input-start/delta、
        # data-task、data-connector-event、data-sub-text-delta/tool-call/tool-result…）
        # 一律丢弃（B2 表）。
        #
        # `data-connector-event` 是**逐条登记**进来的，不是顺手补的（2026-09-30
        # 复核）：它此前落在"…"里静默丢弃，而它的载荷里有一条**别处没有**的信号——
        # `{type: 'connector.call.retrying', attempts}`（`connector/observability.ts`
        # 定义、`runtime/agent/tools.ts` 经 `writerRef.write` 写到父流）。
        # 其余两个变体（started/succeeded/failed）丢弃是对的：信息已由
        # tool-input-available / tool-output-available / tool-output-error 承载，
        # 透传只会重复计数（change-plan §B2 第五轮那行）。retrying 这条则**没有**
        # 对应物，丢弃它的后果是"某个连接器工具重试了 N 次"在报告里完全不可见。
        # 维持丢弃的理由只有一条，且要写明：本数据集不覆盖 connector 维度，
        # 且 `harness.retry` 判的是模型层重试（见 server.OBSERVATION_SURFACE 的
        # `retry` 口径）——转成 `retry` 事件会让两种重试混为一谈。**要覆盖它就得
        # 新增一条工具级指标**，不能靠这条转译。
        # `data-sub-text-delta` 丢弃是安全的：异步子代理在后台跑，writer 置空，
        # 它的事件走子会话自己的 registry 通道，**不会**串进父流（第九轮实测：
        # 异步轮的直方图里一条 data-sub-text-delta 都没有；同步轮里的那批
        # 属于子代理内部过程，父回合的回答由 data-sub-done 之后自己的 text-* 给出）。
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
        self._part_text = ""
        return []

    def _on_text_delta(self, chunk: dict) -> list[dict]:
        delta = str(chunk.get("delta", ""))
        self._step_text += delta
        self._part_text += delta
        return []

    def _on_text_end(self, chunk: dict) -> list[dict]:
        if self._part_text:
            self._last_text = self._part_text
            self._narrative_parts.append(
                {"type": "text", "id": str(chunk.get("id", "")), "text": self._part_text}
            )
        return []

    # ---- 推理片段（不进 §8 流，但必须为续跑留档）----
    #
    # 这三条处理器原本不存在，`reasoning-*` 走 feed() 的方言兜底被丢掉。丢进 §8
    # 是对的（词汇表里没有推理位置），**只丢不留档是错的**：它是 assistant 消息的
    # 一部分，续跑回传时缺了它，模型侧会拒绝整条消息（见 _narrative_parts 注释）。

    def _on_reasoning_start(self, chunk: dict) -> list[dict]:
        return []

    def _on_reasoning_delta(self, chunk: dict) -> list[dict]:
        self._step_reasoning += str(chunk.get("delta", ""))
        return []

    def _on_reasoning_end(self, chunk: dict) -> list[dict]:
        if self._step_reasoning:
            self._narrative_parts.append({"type": "reasoning", "text": self._step_reasoning})
            self._step_reasoning = ""
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
            exit_code = parsed.get("exitCode")
            # 实测（2026-09-30 联调第六轮，父级直连 bash）：非零退出**不走**
            # tool-output-error，仍是 tool-output-available，只在载荷里加
            # `error: true, message: "命令以退出码 7 结束"`（实测 `exit 7`）。无条件写
            # status="ok" 会让一条失败命令在报告里同时呈现"退出码 7"与"调用成功"：
            # builder 按这里的 status 决定 span 状态，而 exit_code 另有其用
            # （Spec §19.4）。两个观测面都要如实。
            failed = bool(parsed.get("error")) or (isinstance(exit_code, int) and exit_code != 0)
            status = "error" if failed else "ok"
            result_data: dict = {"status": status, "result": parsed}
            if failed:
                result_data["error"] = str(parsed.get("message") or f"exit code {exit_code}")
            result = self._event(
                "tool.result",
                result_data,
                parent=(entry or {}).get("call_event_id"),
            )
            command_finished = self._event(
                "command.finished",
                {"status": status, "exit_code": exit_code},
                parent=(entry or {}).get("command_event_id"),
            )
            return [result, command_finished]
        result = self._event(
            "tool.result",
            {"status": "ok", "result": output},
            parent=(entry or {}).get("call_event_id"),
        )
        events = [result]
        if (entry or {}).get("name") == "use_skill":
            events.extend(self._skill_loaded(output, result["event_id"]))
        return events

    def _skill_loaded(self, output: object, parent: str) -> list[dict]:
        """`use_skill` 工具结果 → `skill.loaded`（第八轮：此前是**假声明**）。

        实测（2026-09-30 C 类评测集勘察）：ai-chatbot 的技能加载**就是一次普通工具
        调用**（`toolName="use_skill"`，入参 `{skillName}`），上游流上没有任何
        `skill.*` 方言 chunk——`packages/core/src/runtime/tools/skill.ts` 里
        loadSkills 的结果只从 tool-output 回。但 /health 的观测面表把
        `skill.loaded` 声明为 **true** 而转译器从不发它：`harness.skill_load` 的
        `required_events` 因此得到满足（没被能力表否决），插件按"加载了 0 个 skill"
        判 **FAIL** —— 一次成功的技能加载被报成失败（假红，与"假绿"同源：声明与事实
        不符）。二选一（真发事件 / 声明为 false）里选前者，因为这条事实是**可观测且
        有权威来源**的：`tool.result` 的载荷里有 `success` 与 `skillName`。

        以**结果**而非入参为据：入参表达的是"想加载谁"，技能名不存在时平台返回
        `success: false` + `availableSkills`，把它记成"已加载"同样是不实。
        """
        if not (isinstance(output, dict) and output.get("success")):
            return []
        name = str(output.get("skillName", ""))
        if not name:
            return []
        return [self._event("skill.loaded", {"name": name}, parent=parent)]

    def _on_tool_output_error(self, chunk: dict) -> list[dict]:
        # 实测（2026-09-30 联调第六轮）：该类型**确实出现在流上**（第五轮"第一方源码
        # 零命中"的静态结论再次被运行时推翻，与 tool-approval-request 同一类误判）。
        # 非 bash 工具的执行失败走这里：载荷 {toolCallId, errorText}，没有 output，
        # 原因在 errorText（实测例：连接器工具报 "Connector tool error: SQL query
        # template is empty: ..."）。
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

    def _on_tool_output_denied(self, chunk: dict) -> list[dict]:
        # 实测（2026-09-30 第七轮联调，审批 deny 路径）：用户拒绝时，续跑流上出现
        # `{"type":"tool-output-denied","toolCallId":"call_00_…"}`——**只有 toolCallId**，
        # 没有 output、没有 errorText、也没有 reason（reason 只回落在**请求侧**的
        # approval 字段里，见 B3）。
        # 原先没有这个处理函数 → 按方言丢弃 → 被审批工具永远等不到 closing 事件，
        # builder 在 run.finished 时统一收口成 "span never closed (stream ended)" 的
        # error span：一次"用户拒绝"被报告成"工具调用失败"。收口逻辑本身是对的，
        # 错在转译器没把这条唯一的事实送上流。
        # 语义取 `denied` 而非 `error`：工具**没有失败**，是策略拒绝了执行，agent 随后
        # 的降级行为属正常路径。builder 只在 `status=="error"` 时把 span 判红，
        # 因此 denied 不改 span 状态，只落 `tool_status="denied"`（可观测、不误判）。
        call_id = str(chunk.get("toolCallId", ""))
        entry = self._tools.get(call_id)
        kind = (entry or {}).get("kind", "tool")
        event_type = "mcp.result" if kind == "mcp" else "tool.result"
        events = [
            self._event(
                event_type,
                {"status": "denied", "result": {"denied": True}},
                parent=(entry or {}).get("call_event_id"),
            )
        ]
        if kind == "bash":
            # 与 tool-output-available 的 bash 分支同构：command.* 是独立观测面，
            # 只补 tool.result 会留下一个永不闭合的 command span。退出码取 None
            # （命令没跑，没有退出码）——Spec §19.4 的 exit_code 断言据此判 skipped，
            # 不猜成 0。
            events.append(
                self._event(
                    "command.finished",
                    {"status": "denied", "exit_code": None},
                    parent=(entry or {}).get("command_event_id"),
                )
            )
        return events

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
            if self.policy in ("auto-approve", "auto-deny") and (
                self.approval_rounds < self.max_approval_rounds
            ):
                self.approval_rounds += 1
                # B3：策略决定"批准还是拒绝"，但两条路径都走续跑 POST——拒绝也是
                # 一次真实的上游往返（实测：deny 的续跑流以 tool-output-denied 开头，
                # agent 随后降级作答）。策略名留痕在 self.policy，进 /health。
                self.decision = "approve" if self.policy == "auto-approve" else "deny"
                self.suspended = True  # server 构造续跑请求后再次 POST，翻译继续
                return []
            # 无策略/超轮数：如实按 error 收尾（不伪造成功）
            return self.fail(f"approval suspended (finishReason={reason})")
        status = "error" if reason == "error" else "success"
        # 异步委派的收尾在 run.finished **之前**：晚一步就落进 builder 的未闭合收口，
        # 被判成 error。审批挂起分支已在上方返回，走到这里的一定是终局。
        closed = self._close_async_subagents()
        finished = self._event(
            "run.finished", {"status": status, "output": self._last_text}, parent=self._root_id
        )
        self.finished = True
        return [*closed, finished]

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
        # 失败出口同样要关闭异步子代理：否则 run.finished 的收口会把它们再判一次
        # error，报告上"后台任务正常受理"与真正的失败混成同一形状。
        closed = self._close_async_subagents()
        error = self._event("error", {"message": message})
        finished = self._event(
            "run.finished", {"status": "error", "output": self._last_text}, parent=self._root_id
        )
        self.finished = True
        return [*closed, error, finished]

    # ---- 子代理（实测：全事件族 id === toolCallId） ----

    def _on_data_sub_open(self, chunk: dict) -> list[dict]:
        data = chunk.get("data") or {}
        started = self._event(
            "subagent.started", {"name": data.get("agentType")}, parent=self._root_id
        )
        # 记下 opening 的 event_id：closing 必须挂在它身上，否则 builder 视作孤立
        # closing 忽略掉，span 永不闭合（实测重放暴露，见 module 顶部补丁说明）。
        self._sub_spans[str(chunk.get("id", ""))] = started["event_id"]
        return [started]

    def _on_data_sub_async(self, chunk: dict) -> list[dict]:
        """异步委派标记（`waitMode: "async"`）：父流不会再有 `data-sub-done`。

        实测（2026-09-30 第九轮，见 tmp/probe-async/）：一条 `data-sub-async` 带
        `{submitted: true, subConversationId}`，同 run 的 `data-sub-done` 缺席，
        `finishReason` 仍是 `"stop"`。

        转译器不合成一个假的 finished 结果：`subagent.started` 的观测面是路由断言
        （`harness.subagent_routing` 读 span.name），而"子代理有没有跑完"在 §8 里
        **没有承载它的词汇**——真实终态在 `subagent_sessions`（另有只读端点）。
        所以这里只登记，关闭动作放到 `_close_async_subagents`，状态如实写
        `submitted`：报告里"这一轮结束时它还在跑"与"它跑完了"必须可分辨。
        """
        call_id = str(chunk.get("id", ""))
        data = chunk.get("data") or {}
        self._async_subs[call_id] = {
            "sub_conversation_id": str(data.get("subConversationId", "")),
        }
        return []  # 方言：不进 §8 流（span 由 data-sub-open 开，close 见下）

    def _close_async_subagents(self) -> list[dict]:
        """关闭本 run 受理过的异步子代理 span，在 `run.finished` **之前**调用。

        不关闭的后果不是"少一条事件"：`builder._finalize_open` 会把仍未闭合的 span
        统一判成 `status=error` / `"span never closed (stream ended)"`，于是一次
        正常的后台委派在报告里呈现为"子代理失败了"（与 `tool-output-denied` 那类
        误判同源：转译器没把已知事实送上流，收口逻辑只好按最坏情况猜）。
        """
        events = [
            self._event(
                "subagent.finished",
                {
                    "status": "submitted",
                    "sub_conversation_id": info.get("sub_conversation_id", ""),
                },
                parent=self._sub_spans.get(call_id) or self._root_id,
            )
            for call_id, info in self._async_subs.items()
        ]
        self._async_subs = {}
        return events

    def _on_data_sub_done(self, chunk: dict) -> list[dict]:
        data = chunk.get("data") or {}
        call_id = str(chunk.get("id", ""))
        return [
            self._event(
                "subagent.finished",
                {"status": data.get("status")},
                parent=self._sub_spans.get(call_id) or self._root_id,
            )
        ]

    def _on_data_sub_error(self, chunk: dict) -> list[dict]:
        """子代理失败/被中止的收尾事件。

        **静态已知、运行时尚未观测到**（`event-broadcaster.ts:145` 声明了
        `data-sub-error`，第九轮探针的 async 轮与既往冒烟 dump 里都没出现）。
        仍要处理：丢掉它会把一个**真实失败**的子代理留成未闭合 span，虽然结局
        同为 error span，但 error 文案会从子代理自己的原因退化成
        "span never closed (stream ended)"——报告该说的事被收口逻辑代答了。
        """
        data = chunk.get("data") or {}
        call_id = str(chunk.get("id", ""))
        return [
            self._event(
                "subagent.finished",
                {"status": "error", "error": str(data.get("error", ""))},
                parent=self._sub_spans.get(call_id) or self._root_id,
            )
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
        # 已完成的叙述型 part 按**到达顺序**带上（服务端按 id 整体覆盖该消息）。
        # reasoning 与 text 一样是 assistant 消息的一部分，顺序也会影响模型看到的
        # 消息结构，所以两者合在一个列表里保序，而不是分两轮追加。
        for part in self._narrative_parts:
            if part["type"] == "reasoning":
                # AI SDK 的 ReasoningUIPart 是 `{type, text, state?}`——**没有 id**
                # （ai/dist/index.d.ts:1706）。TextUIPart 的 id 是本平台实测可用的，
                # 故保留；reasoning 这边不凭空加字段（多出来的键没有任何依据）。
                parts.append({"type": "reasoning", "text": part["text"], "state": "done"})
                continue
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
                # 实测（2026-09-30 第七轮）：approve 的 `{id, approved:true}` 被平台接受
                # 并接着跑（无 reason 字段亦可）；deny 则必须带 `reason`——流上回来的
                # `tool-output-denied` **只有 toolCallId**，拒绝原因的唯一落点是请求侧。
                approval = {"id": pending["approval_id"], "approved": self.decision == "approve"}
                if self.decision == "deny":
                    approval["reason"] = "denied by shim approval policy (auto-deny)"
                elif self.answers is not None and pending.get("tool_name") == "ask_user_question":
                    # 答案注入：形状逐字照第七轮实测（reason = JSON({answers: {问题全文: 答案}})）。
                    # 只对 ask_user_question 生效——往别的工具塞 reason 是伪造入参。
                    questions = (entry.get("input") or {}).get("questions") or []
                    answers, unanswered, unused = resolve_answers(questions, self.answers)
                    self.answer_gaps.extend(unanswered)
                    self.unused_answers.extend(unused)
                    if answers:
                        approval["reason"] = json.dumps({"answers": answers}, ensure_ascii=False)
                part["approval"] = approval
            elif "output" in entry:
                part["state"] = "output-available"
                part["output"] = entry["output"]
            else:
                part["state"] = "input-available"
            parts.append(part)
        # 消费语义：本方法产出的消息已把**当前**的全部 pending 渲染进去了。
        # （此条为**推理**所得，未在真实流上遇到——第七轮只跑了单轮审批。
        # 但代价为零而失效代价高：平台整体覆盖该消息，回退的 state 会丢掉已执行
        # 工具的 output。）
        # 不清空的话，一轮里发生第二轮审批时，第一轮那个工具会再次命中遗留 pending
        # → 被重建为 `approval-responded`，而它真实的状态已是 `output-available`
        # （平台按 part.state upsert，那会丢掉它的输出、甚至再执行一次）。
        self._pending = []
        return {"id": self._message_id, "role": "assistant", "parts": parts}
