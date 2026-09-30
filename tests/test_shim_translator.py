"""shim 转译器测试：synthetic chunks 与真实冒烟 dump（tmp/smoke/*.json）同形。

转译规则的契约在 change-plan §2 B2（第五轮实测校准）；本测试钉住：
basic 链序与单侧 usage / mcp 拆流 / bash command 事件与 exit_code 解码 /
审批挂起与续跑消息形状 / subagent 关联 / 方言丢弃 / 错误收尾。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "shims" / "ai-chatbot"))

from shim_ai_chatbot.translator import (  # noqa: E402
    TranslationSession,
    resolve_answers,
)

from agent_eval.models.events import TraceEvent  # noqa: E402
from agent_eval.models.spans import TraceSpan  # noqa: E402
from agent_eval.trace.builder import TraceBuilder  # noqa: E402


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


def test_bash_nonzero_exit_is_reported_as_error_not_ok() -> None:
    """实测（联调第六轮）：非零退出**不走** tool-output-error，仍走 available + error:true。

    实收载荷（父级直连 `exit 7`）：
    ``{"stdout":"(no output)","stderr":"","exitCode":7,"command":"exit 7",
    "error":true,"message":"命令以退出码 7 结束"}``。无条件写 status="ok" 会让报告里
    "退出码 7"与"调用成功"并存——builder 按 status 决定 span 状态，exit_code 另有其用。
    """
    session = make_session()
    events = feed_all(
        session,
        bash_chunks(
            {
                "stdout": "(no output)",
                "stderr": "",
                "exitCode": 7,
                "command": "exit 7",
                "error": True,
                "message": "命令以退出码 7 结束",
            }
        ),
    )
    result = next(e for e in events if e["type"] == "tool.result")
    command_finished = next(e for e in events if e["type"] == "command.finished")
    assert result["data"]["status"] == "error"
    assert result["data"]["error"] == "命令以退出码 7 结束"
    assert command_finished["data"]["status"] == "error"
    assert command_finished["data"]["exit_code"] == 7


def test_bash_nonzero_exit_without_error_flag_is_still_error() -> None:
    """`error:true` 不是判据的唯一来源：只看 exitCode 也得判 error。

    载荷形状可能不带 message（自愈/裁剪路径），只靠 error 标记就会漏。
    """
    session = make_session()
    events = feed_all(session, bash_chunks({"stdout": "", "stderr": "boom", "exitCode": 3}))
    result = next(e for e in events if e["type"] == "tool.result")
    assert result["data"]["status"] == "error"
    assert result["data"]["error"] == "exit code 3"


def test_bash_zero_exit_stays_ok() -> None:
    """正常退出不能因为这次修正被顺手标成 error（两侧都要钉）。"""
    session = make_session()
    events = feed_all(
        session,
        bash_chunks({"stdout": "hello-from-bash\r\n", "stderr": "", "exitCode": 0}),
    )
    result = next(e for e in events if e["type"] == "tool.result")
    command_finished = next(e for e in events if e["type"] == "command.finished")
    assert result["data"]["status"] == "ok"
    assert command_finished["data"]["status"] == "ok"
    assert command_finished["data"]["exit_code"] == 0


def test_tool_output_error_is_translated_as_error_result() -> None:
    """实测（联调第六轮）：`tool-output-error` 确实在流上（第五轮静态结论被推翻）。

    实收载荷 `{toolCallId, errorText}`，无 output；实测例是连接器工具报
    "Connector tool error: SQL query template is empty: ..."。
    """
    session = make_session()
    events = feed_all(
        session,
        [
            ev("start", messageId="msg-5"),
            ev("start-step"),
            ev(
                "tool-input-available",
                toolCallId="call_e1",
                toolName="pg-query_describe_table",
                input={},
            ),
            ev(
                "tool-output-error",
                toolCallId="call_e1",
                errorText="Connector tool error: SQL query template is empty",
            ),
            ev("finish", finishReason="stop"),
        ],
    )
    result = next(e for e in events if e["type"] == "tool.result")
    assert result["data"]["status"] == "error"
    assert "SQL query template is empty" in result["data"]["error"]


# ---------------------------------------------------------------- skill 加载


def skill_chunks(result_output: dict) -> list[dict]:
    """`use_skill` 的一次真实往返（实测 2026-09-30 第八轮，载荷取自真实 dump）。"""
    return [
        ev("start", messageId="msg-6"),
        ev("start-step"),
        ev(
            "tool-input-available",
            toolCallId="call_sk1",
            toolName="use_skill",
            input={"skillName": "SQL执行流程"},
        ),
        ev(
            "tool-output-available",
            toolCallId="call_sk1",
            output=result_output,
        ),
        ev("finish", finishReason="stop"),
    ]


def test_use_skill_result_emits_skill_loaded() -> None:
    """实际加载的 skill 必须出现在 `skill.loaded` 上（第八轮修正）。

    ai-chatbot 的技能加载是一次普通工具调用（`toolName="use_skill"`），上游流上
    没有 `skill.*` 方言；此前转译器不发该事件，而 /health 把 `skill.loaded` 声明为
    true —— `harness.skill_load` 于是按"加载了 0 个"判 FAIL：一次成功的加载被报成
    失败。这条测试钉住"声明的观测面必须真的有事件"。
    """
    session = make_session()
    events = feed_all(
        session,
        skill_chunks(
            {
                "success": True,
                "skillName": "SQL执行流程",
                "skillDir": "C:/skills/sql-execution-workflow",
                "workspace": ["SKILL.md"],
            }
        ),
    )
    loaded = [e for e in events if e["type"] == "skill.loaded"]
    assert len(loaded) == 1
    assert loaded[0]["data"]["name"] == "SQL执行流程"
    # 挂在 tool.result 之下（父级 = 该工具的 call span），不另建 span
    result = next(e for e in events if e["type"] == "tool.result")
    assert loaded[0]["parent_span_id"] == result["event_id"]


def test_failed_skill_load_emits_no_skill_loaded() -> None:
    """以**结果**而非入参为据：技能名不存在时平台回 success=false + availableSkills。

    把入参当事实源会记成"已加载"——那是另一种不实，且方向是**假绿**（断言在
    一个没加载成功的技能上通过）。
    """
    session = make_session()
    events = feed_all(
        session,
        skill_chunks({"success": False, "error": "skill not found", "availableSkills": ["docx"]}),
    )
    assert [e for e in events if e["type"] == "skill.loaded"] == []


def test_other_tool_results_do_not_emit_skill_loaded() -> None:
    """护栏：只有 `use_skill` 的结果能产生 `skill.loaded`（避免误报成"加载了技能"）。"""
    session = make_session()
    events = feed_all(
        session,
        [
            ev("start", messageId="msg-7"),
            ev("start-step"),
            ev(
                "tool-input-available",
                toolCallId="call_r1",
                toolName="read_file",
                input={"filePath": "SKILL.md"},
            ),
            ev(
                "tool-output-available",
                toolCallId="call_r1",
                output={"success": True, "skillName": "看起来像技能的东西"},
            ),
            ev("finish", finishReason="stop"),
        ],
    )
    assert [e for e in events if e["type"] == "skill.loaded"] == []


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


def test_resume_message_keeps_reasoning_parts_in_order() -> None:
    """续跑消息要带 reasoning——丢了它整条消息会被模型侧拒绝。

    第十一轮真机实测：`ask_user_question` 之后的续跑 POST 恒以流内 error 收场
    （`The reasoning_content in the thinking mode must be passed back to the API`，
    四次独立运行全中）。根因是转译器把 `reasoning-*` 当方言整族丢弃，续跑重建
    只带 text + tool part。带进 §8 流仍是错的（词汇表里没有推理位置），但**必须
    为续跑留档**。断言两件事：顺序（text→reasoning→text）与形状。
    """
    session = make_session()
    feed_all(
        session,
        [
            ev("start", messageId="msg-r"),
            ev("start-step"),
            ev("text-start", id="txt-0"),
            ev("text-delta", id="txt-0", delta="我先问一下。"),
            ev("text-end", id="txt-0"),
            ev("reasoning-start", id="reasoning-0"),
            ev("reasoning-delta", id="reasoning-0", delta="需要先确认交付格式"),
            ev("reasoning-end", id="reasoning-0"),
            ev("text-start", id="txt-1"),
            ev("text-delta", id="txt-1", delta="再补充一句。"),
            ev("text-end", id="txt-1"),
            ev(
                "tool-input-available",
                toolCallId="call_a1",
                toolName="ask_user_question",
                input={"questions": []},
            ),
            ev("tool-approval-request", approvalId="aitxt-r1", toolCallId="call_a1"),
            ev("finish-step"),
            ev("finish", finishReason="tool-calls"),
        ],
    )
    message = session.build_resume_message()
    assert message is not None
    narrative = [p for p in message["parts"] if p.get("type") in ("text", "reasoning")]
    assert [p["type"] for p in narrative] == ["text", "reasoning", "text"]
    assert narrative[0] == {"type": "text", "id": "txt-0", "text": "我先问一下。"}
    # ReasoningUIPart 没有 id 字段（ai/dist/index.d.ts:1706）——不凭空加键
    assert narrative[1] == {
        "type": "reasoning",
        "text": "需要先确认交付格式",
        "state": "done",
    }
    assert narrative[2] == {"type": "text", "id": "txt-1", "text": "再补充一句。"}


def test_reasoning_still_produces_no_eight_vocabulary_event() -> None:
    """推理留档不等于进 §8 流：词汇表里没有推理位置，事件数一个不许多。"""
    session = make_session()
    events = feed_all(
        session,
        [
            ev("start", messageId="msg-r2"),
            ev("start-step"),
            ev("reasoning-start", id="reasoning-0"),
            ev("reasoning-delta", id="reasoning-0", delta="想想"),
            ev("reasoning-end", id="reasoning-0"),
            ev("finish", finishReason="stop"),
        ],
    )
    assert [e["type"] for e in events] == [
        "run.started",
        "model.request",
        "run.finished",
    ]


def test_resume_message_without_reasoning_is_unchanged() -> None:
    """没有推理片段时，续跑形状与第七轮实测逐字一致（不得顺手加字段）。"""
    session = make_session()
    feed_all(session, approval_chunks())
    message = session.build_resume_message()
    assert message == {
        "id": "msg-4",
        "role": "assistant",
        "parts": [
            {"type": "text", "id": "txt-0", "text": "我先问一下。"},
            {
                "toolCallId": "call_a1",
                "input": {"questions": []},
                "type": "tool-ask_user_question",
                "state": "approval-responded",
                "approval": {"id": "aitxt-abc123", "approved": True},
            },
        ],
    }


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


def _span_status(events: list[dict]) -> list[TraceSpan]:
    builder = TraceBuilder()
    for event in events:
        builder.feed(TraceEvent(**event))
    return builder.build().spans


def test_subagent_finished_is_paired_with_its_started_span() -> None:
    """closing 必须挂在 opening 的 event_id 上——挂错地方等于**没有** closing。

    builder 的配对规则是 ``closing.parent_span_id == opening.event_id``
    （builder.py:127）。早先 ``_on_data_sub_done`` 把 parent 写成 root_id：builder
    找不到该 span（"孤立 closing：忽略"），span 永不闭合，``run.finished`` 统一
    收口时被判 ``status=error`` / "span never closed (stream ended)"。于是一次
    **成功的**子代理委派在报告里呈现为失败。

    这不是异步路径引入的问题，而是同步路径一直存在的缺陷：用真实 dump
    （``tmp/smoke/smoke-subagent.json``）重放时同样报错，只是
    ``harness.subagent_routing`` 只读 span.name、C 类子代理用例也不断言 span
    状态，所以一路没被发现。异步路径会放大它——异步子代理是本 run 里唯一必然
    "父流无终态"的形态，收口判错就成了它唯一可能的结局。
    """
    session = make_session()
    events = feed_all(
        session,
        [
            ev("start", messageId="msg-6"),
            ev("data-sub-open", id="call_s2", data={"agentType": "general-purpose"}),
            ev("data-sub-done", id="call_s2", data={"status": "completed"}),
            ev("finish", finishReason="stop"),
        ],
    )
    subs = [span for span in _span_status(events) if span.type == "subagent"]
    assert len(subs) == 1
    assert subs[0].status == "ok", subs[0].error
    assert subs[0].finished_at is not None


def test_async_delegation_has_no_fake_done_and_closes_before_run_finished() -> None:
    """`data-sub-async`：父流不会再有 done，转译器不得替平台宣布一个未发生的结果。

    实测形状（2026-09-30 第九轮，``tmp/probe-async/turn1-chunks.json``，序号即
    到达序）：259 tool-input-available(agent, waitMode="async") → 260 data-sub-open
    → 261 data-sub-async{submitted, subConversationId} → 262 tool-output-available
    → 266-273 父回合自己的 text-*（"SUBMITTED-ACK"）→ 276 finish(finishReason="stop")。
    该轮直方图里**没有** data-sub-done。

    三条断言各钉一件事：
      - 不合成 done（否则"还在跑"与"跑完了"在报告里同形）；
      - span 在 run.finished **之前**闭合（晚一步就被收口判成 error span）；
      - 终态写成 ``submitted`` 并带上 subConversationId（真实终态在
        ``subagent_sessions``，父流说不了更多）。
    """
    session = make_session()
    events = feed_all(
        session,
        [
            ev("start", messageId="msg-7"),
            ev(
                "tool-input-available",
                toolCallId="call_a1",
                toolName="agent",
                input={"waitMode": "async"},
            ),
            ev("data-sub-open", id="call_a1", data={"agentType": "fullstack-engineer"}),
            ev(
                "data-sub-async",
                id="call_a1",
                data={"submitted": True, "subConversationId": "sub:c1:call_a1"},
            ),
            ev("tool-output-available", toolCallId="call_a1", output={"status": "submitted"}),
            ev("text-start", id="txt-0"),
            ev("text-delta", id="txt-0", delta="SUBMITTED-ACK"),
            ev("text-end", id="txt-0"),
            ev("finish", finishReason="stop"),
        ],
    )
    types = [e["type"] for e in events]
    assert types.count("subagent.started") == 1
    assert types.count("subagent.finished") == 1
    assert types.index("subagent.finished") < types.index("run.finished")
    finished = next(e for e in events if e["type"] == "subagent.finished")
    assert finished["data"]["status"] == "submitted"
    assert finished["data"]["sub_conversation_id"] == "sub:c1:call_a1"
    # 父回合自己的回答不被子代理污染（异步子代理的文本走 data-sub-text-delta，
    # 实测不出现在父流上；这里的 text-* 就是父回合的 SUBMITTED-ACK）
    done = next(e for e in events if e["type"] == "run.finished")
    assert done["data"]["status"] == "success"
    assert done["data"]["output"] == "SUBMITTED-ACK"
    subs = [span for span in _span_status(events) if span.type == "subagent"]
    assert [span.status for span in subs] == ["ok"], subs[0].error


def test_async_delegation_survives_a_failing_run() -> None:
    """失败出口（``fail()``）同样要关闭异步子代理 span，且顺序在 run.finished 之前。"""
    session = make_session()
    events = feed_all(
        session,
        [
            ev("start", messageId="msg-8"),
            ev("data-sub-open", id="call_a2", data={"agentType": "general-purpose"}),
            ev("data-sub-async", id="call_a2", data={"submitted": True}),
        ],
    )
    events.extend(session.fail("upstream died"))
    types = [e["type"] for e in events]
    assert types.index("subagent.finished") < types.index("run.finished")
    assert next(e for e in events if e["type"] == "run.finished")["data"]["status"] == "error"
    # 子代理本身不是失败原因：它的 span 保持 ok，run 级 error 才是
    subs = [span for span in _span_status(events) if span.type == "subagent"]
    assert [span.status for span in subs] == ["ok"], subs[0].error


def test_subagent_error_chunk_closes_the_span_with_its_own_reason() -> None:
    """``data-sub-error``（静态已知、流上尚未观测到）：不得丢掉，否则失败原因被收口代答。

    ``event-broadcaster.ts:145`` 声明了这个类型，但第九轮 async 轮与既往冒烟 dump
    里都没出现。丢掉它的后果不是"少一条事件"：未闭合 span 会被 ``_finalize_open``
    收口成 "span never closed (stream ended)"，于是子代理自己报的原因（真实、可修）
    被替换成一句协议层猜测。
    """
    session = make_session()
    events = feed_all(
        session,
        [
            ev("start", messageId="msg-9"),
            ev("data-sub-open", id="call_s3", data={"agentType": "researcher"}),
            ev("data-sub-error", id="call_s3", data={"error": "child aborted by parent"}),
            ev("finish", finishReason="stop"),
        ],
    )
    finished = next(e for e in events if e["type"] == "subagent.finished")
    assert finished["data"]["status"] == "error"
    assert finished["data"]["error"] == "child aborted by parent"
    subs = [span for span in _span_status(events) if span.type == "subagent"]
    assert subs[0].error == "child aborted by parent"


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


# ---------------------------------------------------------------- 审批答案注入

QUESTIONS = [
    {
        "question": "你希望最终产出以哪种文件格式交付？",
        "header": "输出格式",
        "options": ["Markdown 文档（.md）", "表格（Excel .xlsx）"],
        "multiSelect": False,
    },
    {
        "question": "内容详略程度希望如何？",
        "header": "详略程度",
        "options": ["精简要点版", "标准完整版"],
        "multiSelect": True,
    },
]


def test_resolve_answers_accepts_three_key_spellings() -> None:
    """问题全文 / header / `#序号` 三种写法都认，但**发出去的键一律是问题全文**。

    第七轮实测：平台对 resume 消息里的 answer 键**不做校验、原样回显**，所以写错键
    不会报错，只会让 agent 按 question 全文查不到答案（等于没给）。放宽解析是为了
    吸收写法差异，不是为了掩盖缺口。
    """
    answers, unanswered, unused = resolve_answers(
        QUESTIONS,
        {"你希望最终产出以哪种文件格式交付？": "Markdown 文档（.md）", "详略程度": "精简要点版"},
    )
    assert answers == {
        "你希望最终产出以哪种文件格式交付？": "Markdown 文档（.md）",
        "内容详略程度希望如何？": "精简要点版",
    }
    assert unanswered == [] and unused == []
    # `#2` 是 1-based 序号（第 2 个问题），multiSelect 的答案是字符串数组
    answers2, _, unused2 = resolve_answers(
        QUESTIONS, {"输出格式": "表格（Excel .xlsx）", "#2": ["a", "b"]}
    )
    assert answers2["你希望最终产出以哪种文件格式交付？"] == "表格（Excel .xlsx）"
    assert answers2["内容详略程度希望如何？"] == ["a", "b"]
    assert unused2 == []


def test_resolve_answers_reports_gaps_and_typos_separately() -> None:
    """缺口（问了没答）与错键（答了没问）是两件事，不能混成一个计数。

    前者让 agent 拿不到答案 → 本轮行为与用例预期不符；后者多半是配置写错了。
    混在一起就分不出"这一轮本来就只问了一个问题"与"答案文件写歪了"。
    """
    answers, unanswered, unused = resolve_answers(
        QUESTIONS, {"输出格式": "Markdown 文档（.md）", "写错的键": "x"}
    )
    assert set(answers) == {"你希望最终产出以哪种文件格式交付？"}
    assert unanswered == ["内容详略程度希望如何？"]
    assert unused == ["写错的键"]


def _ask_chunks(questions: list[dict], approval_id: str = "aitxt-a1") -> list[dict]:
    return [
        ev("start", messageId="msg-ask"),
        ev("start-step"),
        ev(
            "tool-input-available",
            toolCallId="call_q1",
            toolName="ask_user_question",
            input={"questions": questions},
        ),
        ev("tool-approval-request", approvalId=approval_id, toolCallId="call_q1"),
        ev("finish-step"),
        ev("finish", finishReason="tool-calls"),
    ]


def test_answers_are_injected_as_question_keyed_json_reason() -> None:
    """续跑消息的 approval.reason 逐字照第七轮实测：`JSON({answers: {问题全文: 答案}})`。"""
    session = make_session(answers={"输出格式": "Markdown 文档（.md）"})
    feed_all(session, _ask_chunks(QUESTIONS))
    assert session.suspended is True
    message = session.build_resume_message()
    part = next(p for p in message["parts"] if p.get("approval"))
    reason = part["approval"]["reason"]
    assert json.loads(reason) == {
        "answers": {"你希望最终产出以哪种文件格式交付？": "Markdown 文档（.md）"}
    }
    # 缺口如实记账（问题 2 没配答案）——由 server 按 policy 决定收场方式
    assert session.answer_gaps == ["内容详略程度希望如何？"]
    assert session.unused_answers == []


def test_no_answers_configured_keeps_the_seventh_round_shape() -> None:
    """未配置答案时不带 reason（第七轮行为）：平台回显空答案、agent 降级追问。"""
    session = make_session()
    feed_all(session, _ask_chunks(QUESTIONS))
    part = next(p for p in session.build_resume_message()["parts"] if p.get("approval"))
    assert "reason" not in part["approval"]


def test_answers_are_not_injected_into_other_tools() -> None:
    """答案只对 `ask_user_question` 生效——往别的工具的 approval 里塞 reason 就是伪造入参。

    同一轮里另一个待审批工具必须保持"只有 id/approved"的形状。
    """
    session = make_session(answers={"输出格式": "Markdown 文档（.md）"})
    feed_all(
        session,
        [
            ev("start", messageId="msg-mix"),
            ev("start-step"),
            ev(
                "tool-input-available",
                toolCallId="call_bash",
                toolName="bash",
                input={"command": "rm -rf x"},
            ),
            ev("tool-approval-request", approvalId="aitxt-b", toolCallId="call_bash"),
            ev(
                "tool-input-available",
                toolCallId="call_q1",
                toolName="ask_user_question",
                input={"questions": QUESTIONS},
            ),
            ev("tool-approval-request", approvalId="aitxt-q", toolCallId="call_q1"),
            ev("finish-step"),
            ev("finish", finishReason="tool-calls"),
        ],
    )
    parts = {
        p["toolCallId"]: p["approval"]
        for p in session.build_resume_message()["parts"]
        if p.get("approval")
    }
    assert "reason" not in parts["call_bash"]
    assert "reason" in parts["call_q1"]


# ---------------------------------------------------------------- 审批拒绝（deny）


def denied_resume_chunks() -> list[dict]:
    """实测（2026-09-30 第七轮联调，approval-deny.json 同形）：
    拒绝的续跑流以 `tool-output-denied` 开头，**只有 toolCallId**。
    """
    return [
        ev("start", messageId="msg-4c"),
        ev("tool-output-denied", toolCallId="call_a1"),
        ev("start-step"),
        ev("text-start", id="txt-1"),
        ev("text-delta", id="txt-1", delta="好的，那我不追问了。"),
        ev("text-end", id="txt-1"),
        ev("finish-step"),
        ev("finish", finishReason="stop"),
    ]


def test_tool_output_denied_closes_the_tool_span() -> None:
    """拒绝必须落在流上：原先没这个处理函数 → 被当方言丢弃 → span 永不闭合。

    builder 会把未闭合的 opening 在 run.finished 时收口成 "span never closed"，
    于是一次"用户拒绝"被报告成"工具调用失败"。
    """
    session = make_session()
    first = feed_all(session, approval_chunks())
    session.suspended = False
    resume = feed_all(session, denied_resume_chunks())

    result = next(e for e in resume if e["type"] == "tool.result")
    call = next(e for e in first if e["type"] == "tool.call")
    assert result["parent_span_id"] == call["event_id"]  # 配对到原 tool.call
    # denied 而非 error：工具没有失败，是策略拒绝了执行
    assert result["data"]["status"] == "denied"

    from agent_eval.models.events import TraceEvent
    from agent_eval.trace.builder import TraceBuilder

    builder = TraceBuilder(trace_id="t")
    for raw in first + resume:
        builder.feed(TraceEvent.model_validate(raw))
    span = next(s for s in builder.build().spans if s.type == "tool")
    assert span.status == "ok"  # 拒绝不是失败，不要把 span 判红
    assert span.attributes["tool_status"] == "denied"  # 但事实必须可观测
    assert span.finished_at is not None  # 已闭合，不是 "span never closed"


def test_bash_denied_also_closes_the_command_span() -> None:
    """bash 被拒时 command.* 是独立观测面，只补 tool.result 会留下永不闭合的 command span。"""
    session = make_session()
    events = feed_all(
        session,
        [
            ev("start", messageId="msg-8"),
            ev(
                "tool-input-available",
                toolCallId="call_b9",
                toolName="bash",
                input={"command": "rm -rf /tmp/x"},
            ),
            ev("tool-approval-request", approvalId="aitxt-z", toolCallId="call_b9"),
            ev("finish", finishReason="tool-calls"),
        ],
    )
    session.suspended = False
    resume = feed_all(session, [ev("tool-output-denied", toolCallId="call_b9")])

    command_finished = next(e for e in resume if e["type"] == "command.finished")
    command_started = next(e for e in events if e["type"] == "command.started")
    assert command_finished["parent_span_id"] == command_started["event_id"]
    assert command_finished["data"]["status"] == "denied"
    # 命令没跑 → 没有退出码。Spec §19.4：exit_code 断言据此判 skipped，不猜 0。
    assert command_finished["data"]["exit_code"] is None


def test_auto_deny_policy_produces_a_deny_resume_message() -> None:
    """auto-deny：拒绝也走续跑 POST（拒绝是一次真实上游往返），消息里带 approved=false。"""
    session = make_session(policy="auto-deny")
    feed_all(session, approval_chunks())
    assert session.suspended is True
    assert session.decision == "deny"
    message = session.build_resume_message()
    assert message is not None
    tool_part = next(p for p in message["parts"] if str(p.get("type", "")).startswith("tool-"))
    assert tool_part["approval"]["approved"] is False
    # reason 是拒绝原因的唯一落点：流上回来的 tool-output-denied 只有 toolCallId
    assert "reason" in tool_part["approval"]


def test_auto_approve_still_approves() -> None:
    """两侧都要钉：新增 deny 分支不得把默认策略改掉。"""
    session = make_session()
    feed_all(session, approval_chunks())
    assert session.decision == "approve"
    message = session.build_resume_message()
    tool_part = next(p for p in message["parts"] if str(p.get("type", "")).startswith("tool-"))
    assert tool_part["approval"]["approved"] is True


def test_second_approval_round_does_not_regress_the_first_tool_part() -> None:
    """一轮里发生第二次审批时，第一次那个工具必须保持它的真实状态。

    续跑消息是"整体覆盖"语义（平台按 part.state upsert），所以第二轮重建消息时，
    已经执行完的工具必须是 `output-available`（带 output），**不能**因为遗留的
    pending 条目被再次写成 `approval-responded`——那会让平台丢掉它的输出、
    甚至重新执行一次。
    """
    session = make_session()
    feed_all(session, approval_chunks())  # 第一轮：tool call_a1 挂起
    first_message = session.build_resume_message()
    assert first_message is not None

    session.suspended = False
    feed_all(
        session,
        [
            ev("start", messageId="msg-4b"),
            # 第一个工具批准后执行完
            ev("tool-output-available", toolCallId="call_a1", output={"ok": True}),
            # 第二个工具又要审批 → 再次挂起
            ev(
                "tool-input-available",
                toolCallId="call_a2",
                toolName="bash",
                input={"command": "ls"},
            ),
            ev("tool-approval-request", approvalId="aitxt-def456", toolCallId="call_a2"),
            ev("finish", finishReason="tool-calls"),
        ],
    )
    assert session.suspended is True  # 第二轮挂起

    message = session.build_resume_message()
    assert message is not None
    parts = {p.get("toolCallId"): p for p in message["parts"] if p.get("toolCallId")}
    assert parts["call_a1"]["state"] == "output-available"
    assert parts["call_a1"]["output"] == {"ok": True}
    assert parts["call_a2"]["state"] == "approval-responded"
    assert parts["call_a2"]["approval"]["id"] == "aitxt-def456"
