"""shim 转译器测试：synthetic chunks 与真实冒烟 dump（tmp/smoke/*.json）同形。

转译规则的契约在 change-plan §2 B2（第五轮实测校准）；本测试钉住：
basic 链序与单侧 usage / mcp 拆流 / bash command 事件与 exit_code 解码 /
审批挂起与续跑消息形状 / subagent 关联 / 方言丢弃 / 错误收尾。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "shims" / "ai-chatbot"))

from shim_ai_chatbot.translator import TranslationSession  # noqa: E402


def make_session(**kw) -> TranslationSession:
    kw.setdefault("trace_id", "eval-test")
    kw.setdefault("user_message", "hi")
    return TranslationSession(**kw)


def feed_all(session: TranslationSession, chunks: list[dict]) -> list[dict]:
    events: list[dict] = []
    for chunk in chunks:
        events.extend(session.feed(chunk))
    return events


def ev(etype: str, **data) -> dict:
    return {"type": etype, **data}


# ---------------------------------------------------------------- basic 链序


def basic_chunks() -> list[dict]:
    return [
        ev("start", messageId="msg-1"),
        ev("start-step"),
        ev("text-start", id="txt-0"),
        ev("text-delta", id="txt-0", delta="等于 2。"),
        ev("text-end", id="txt-0"),
        ev("finish-step"),
        ev(
            "data-context-usage",
            id="ctx-1",
            data={"actualInputTokens": 40921, "cachedTokens": 1152, "totalTokens": 107751},
        ),
        ev("finish", finishReason="stop"),
    ]


def test_basic_chain_and_single_side_usage() -> None:
    session = make_session()
    events = feed_all(session, basic_chunks())
    types = [e["type"] for e in events]
    assert types == [
        "run.started",
        "model.request",
        "model.response",
        "run.finished",
    ]
    # A3 单侧口径：usage 不构造 output_tokens → harness 记 partial、max_tokens 判 skipped
    response = events[2]
    assert response["data"]["usage"] == {"input_tokens": 40921, "cache_tokens": 1152}
    assert "output_tokens" not in response["data"]["usage"]
    assert response["parent_span_id"] == events[1]["event_id"]  # 配对 model.request
    finished = events[-1]
    assert finished["data"]["status"] == "success"
    assert finished["data"]["output"] == "等于 2。"  # 最终回答 = 最后完成的 text part


def test_usage_observation_flag_absent_for_platform() -> None:
    """SpanTree.usage_observed 依赖 usage 键存在与否：input 有、output 缺 → 输出侧未观测。"""
    session = make_session()
    events = feed_all(session, basic_chunks())
    response = next(e for e in events if e["type"] == "model.response")
    usage = response["data"]["usage"]
    assert "input_tokens" in usage and "output_tokens" not in usage


# ---------------------------------------------------------------- MCP 拆流


def mcp_chunks() -> list[dict]:
    return [
        ev("start", messageId="msg-2"),
        ev("start-step"),
        ev(
            "tool-input-start",
            toolCallId="call_m1",
            toolName="mcp__arxiv__search_papers",
            dynamic=True,
        ),
        ev(
            "tool-input-available",
            toolCallId="call_m1",
            toolName="mcp__arxiv__search_papers",
            dynamic=True,
            input={"query": "agent evaluation"},
        ),
        ev(
            "tool-output-available",
            toolCallId="call_m1",
            output={
                "content": [
                    {
                        "type": "text",
                        "text": '{"returned": 1, "papers": [{"title": "Agent-as-a-Judge"}]}',
                    }
                ],
                "isError": False,
            },
            dynamic=True,
        ),
        ev("finish-step"),
        ev("finish", finishReason="stop"),
    ]


def test_mcp_prefix_splits_to_dedicated_events() -> None:
    """B2 实测：mcp__ 前缀逐字出现 → 必须拆成独立 mcp.call/mcp.result（name=server 段）。"""
    session = make_session()
    events = feed_all(session, mcp_chunks())
    types = [e["type"] for e in events]
    assert "mcp.call" in types and "mcp.result" in types
    assert "tool.call" not in types  # 折叠即缺陷（forbidden_mcp 恒 pass 的病灶）
    call = next(e for e in events if e["type"] == "mcp.call")
    assert call["data"]["name"] == "arxiv"  # 安全规则 forbidden_mcp 匹配 server 名
    assert call["data"]["arguments"] == {"query": "agent evaluation"}
    result = next(e for e in events if e["type"] == "mcp.result")
    assert result["data"]["status"] == "ok"
    assert result["data"]["result"] == {"returned": 1, "papers": [{"title": "Agent-as-a-Judge"}]}
    assert result["parent_span_id"] == call["event_id"]  # 配对 mcp.call（span 闭合）


# ---------------------------------------------------------------- bash / command


def bash_chunks(result_output: object) -> list[dict]:
    return [
        ev("start", messageId="msg-3"),
        ev("start-step"),
        ev(
            "tool-input-available",
            toolCallId="call_b1",
            toolName="bash",
            input={"command": "rm -rf tmp/"},
        ),
        ev("tool-output-available", toolCallId="call_b1", output=result_output),
        ev("finish", finishReason="stop"),
    ]


def test_bash_emits_command_events_with_exit_code() -> None:
    """bash 是 command.* 观测面：exit_code 唯一来源；命令名取命令行首词。"""
    session = make_session()
    events = feed_all(session, bash_chunks({"stdout": "", "stderr": "", "exitCode": 1}))
    types = [e["type"] for e in events]
    assert "tool.call" in types and "tool.result" in types
    command_started = next(e for e in events if e["type"] == "command.started")
    command_finished = next(e for e in events if e["type"] == "command.finished")
    assert command_started["data"]["name"] == "rm"  # forbidden_commands 匹配可执行名
    assert command_finished["data"]["exit_code"] == 1
    assert command_finished["parent_span_id"] == command_started["event_id"]


def test_bash_double_encoded_result_is_decoded() -> None:
    """实测：result 常是二次 JSON 编码字符串——不解码 exit_code 就永远观测不到。"""
    import json

    encoded = json.dumps({"stdout": "ok", "stderr": "", "exitCode": 0})
    session = make_session()
    events = feed_all(session, bash_chunks(encoded))
    command_finished = next(e for e in events if e["type"] == "command.finished")
    assert command_finished["data"]["exit_code"] == 0


# ---------------------------------------------------------------- 审批挂起与续跑


def approval_chunks() -> list[dict]:
    return [
        ev("start", messageId="msg-4"),
        ev("start-step"),
        ev("text-start", id="txt-0"),
        ev("text-delta", id="txt-0", delta="我先问一下。"),
        ev(
            "tool-input-available",
            toolCallId="call_a1",
            toolName="ask_user_question",
            input={"questions": []},
        ),
        ev("tool-approval-request", approvalId="aitxt-abc123", toolCallId="call_a1"),
        ev("text-end", id="txt-0"),
        ev("finish-step"),
        ev("finish", finishReason="tool-calls"),
    ]


def test_approval_suspends_without_run_finished() -> None:
    session = make_session()
    events = feed_all(session, approval_chunks())
    assert session.suspended is True
    assert session.finished is False
    assert "run.finished" not in [e["type"] for e in events]  # 结算必须等续跑完成


def test_resume_message_shape() -> None:
    """续跑消息：末条 assistant 消息 + approval-responded part（id 取 approvalId）。"""
    session = make_session()
    feed_all(session, approval_chunks())
    message = session.build_resume_message()
    assert message is not None
    assert message["id"] == "msg-4"  # start chunk 的 messageId
    assert message["role"] == "assistant"
    tool_part = next(p for p in message["parts"] if str(p.get("type", "")).startswith("tool-"))
    assert tool_part["type"] == "tool-ask_user_question"
    assert tool_part["toolCallId"] == "call_a1"
    assert tool_part["state"] == "approval-responded"
    assert tool_part["approval"] == {"id": "aitxt-abc123", "approved": True}
    text_part = next(p for p in message["parts"] if p.get("type") == "text")
    assert text_part["text"] == "我先问一下。"  # 服务端按 id 整体覆盖，文本要带上


def test_resume_stream_completes_the_run() -> None:
    """审批续跑的第二条流：start 忽略（不重复 run.started），run.finished 在此收尾。"""
    session = make_session()
    feed_all(session, approval_chunks())
    assert session.suspended is True
    session.suspended = False
    resume_events = feed_all(
        session,
        [
            ev("start", messageId="msg-4b"),  # 续跑流有自己的 start
            ev("start-step"),
            ev(
                "tool-output-available",
                toolCallId="call_a1",
                output={"success": True, "_skillOutput": "回答"},
            ),
            ev("finish-step"),
            ev("finish", finishReason="stop"),
        ],
    )
    types = [e["type"] for e in resume_events]
    assert "run.started" not in types  # 一条 §8 流只有一个 run.started
    assert types[-1] == "run.finished"
    assert resume_events[-1]["data"]["status"] == "success"


# ---------------------------------------------------------------- subagent 关联


def test_subagent_events_use_agent_type_as_name() -> None:
    session = make_session()
    events = feed_all(
        session,
        [
            ev("start", messageId="msg-5"),
            ev(
                "data-sub-open",
                id="call_s1",
                data={"agentType": "general-purpose", "task": "count files"},
            ),
            ev("data-sub-text-delta", id="call_s1", data={"text": "..."}),
            ev(
                "data-sub-done",
                id="call_s1",
                data={"success": True, "status": "completed", "tokenUsage": {}},
            ),
            ev("finish", finishReason="stop"),
        ],
    )
    types = [e["type"] for e in events]
    assert "subagent.started" in types and "subagent.finished" in types
    started = next(e for e in events if e["type"] == "subagent.started")
    assert started["data"]["name"] == "general-purpose"  # SubAgentRoutingEvaluator 消费 span name


# ---------------------------------------------------------------- 方言丢弃与错误收尾


def test_dialect_chunks_are_dropped_silently() -> None:
    """reasoning-* / data-task / start-step 等方言：丢弃，不得透传（E1 词汇校验会响）。"""
    session = make_session()
    events = feed_all(
        session,
        [
            ev("start", messageId="msg-6"),
            ev("start-step"),
            ev("reasoning-start", id="r0"),
            ev("reasoning-delta", id="r0", delta="think"),
            ev("reasoning-end", id="r0"),
            ev("data-task", id="task-1", data={"kind": "update", "tasks": []}),
            ev("finish-step"),
            ev("finish", finishReason="stop"),
        ],
    )
    types = [e["type"] for e in events]
    assert types == ["run.started", "model.request", "run.finished"]


def test_error_chunk_finishes_with_error_status() -> None:
    session = make_session()
    events = feed_all(
        session,
        [
            ev("start", messageId="msg-7"),
            ev("error", errorText="服务器繁忙，请稍后重试"),
        ],
    )
    types = [e["type"] for e in events]
    assert types == ["run.started", "error", "run.finished"]
    assert events[-1]["data"]["status"] == "error"
    assert events[1]["data"]["message"] == "服务器繁忙，请稍后重试"


def test_event_envelope_matches_trace_event_schema() -> None:
    """产出事件必须能被 harness 的 TraceEvent 校验（event_id/trace_id/type/timestamp/data）。"""
    from agent_eval.models.events import TraceEvent

    session = make_session()
    events = feed_all(session, basic_chunks())
    for event in events:
        assert TraceEvent.model_validate(event)
