"""TraceBuilder 测试：配对、嵌套、错误归属、usage 统计。"""

from datetime import datetime, timedelta

from agent_eval.models.events import TraceEvent
from agent_eval.trace.builder import TraceBuilder

T0 = datetime(2026, 9, 23, 11, 0, 0)


def evt(
    eid: str, parent: str | None, etype: str, data: dict | None = None, t: int = 0
) -> TraceEvent:
    return TraceEvent(
        event_id=eid,
        trace_id="t1",
        parent_span_id=parent,
        type=etype,
        timestamp=T0 + timedelta(milliseconds=t),
        data=data or {},
    )


def test_tool_pair_and_nesting() -> None:
    b = TraceBuilder()
    b.feed(evt("r1", None, "run.started"))
    b.feed(evt("a1", None, "agent.started"))
    b.feed(evt("s1", "a1", "subagent.started", {"name": "researcher"}))
    b.feed(evt("t1", "s1", "tool.call", {"name": "web_search"}))
    b.feed(evt("t1r", "t1", "tool.result", {"status": "ok"}))
    b.feed(evt("f1", "s1", "subagent.finished", {"status": "ok"}))
    b.feed(evt("t2", "a1", "tool.call", {"name": "execute_sql"}))
    b.feed(evt("t2r", "t2", "tool.result", {"status": "ok"}))
    b.feed(evt("end", None, "run.finished", {"status": "success"}))

    tree = b.build()
    assert tree.tool_sequence() == ["web_search", "execute_sql"]
    sub = tree.find("subagent", "researcher")[0]
    assert sub.parent_span_id == "a1"
    assert tree.children("s1")[0].name == "web_search"
    assert all(s.status == "ok" for s in tree.spans)


def test_unpaired_open_closed_at_finished() -> None:
    b = TraceBuilder()
    b.feed(evt("r1", None, "run.started"))
    b.feed(evt("t1", None, "tool.call", {"name": "orphan"}))
    b.feed(evt("end", None, "run.finished", {"status": "success"}))
    tree = b.build()
    orphan = tree.find("tool", "orphan")[0]
    assert orphan.finished_at is not None
    assert orphan.status == "error"  # 从未收口的 span 标记错误


def test_orphan_close_ignored_but_usage_kept() -> None:
    b = TraceBuilder()
    b.feed(evt("r1", None, "run.started"))
    b.feed(evt("a1", None, "agent.started"))
    b.feed(evt("m1", "a1", "model.request"))
    b.feed(
        evt(
            "m1r",
            "m1",
            "model.response",
            {"text": "hi", "usage": {"input_tokens": 10, "output_tokens": 5}},
        )
    )
    b.feed(evt("bad", "nonexistent", "tool.result", {"status": "ok"}))
    tree = b.build()
    llm = tree.find("llm")[0]
    assert llm.attributes["usage"]["output_tokens"] == 5
    assert tree.token_count() == 15


def test_error_marks_owner_span() -> None:
    b = TraceBuilder()
    b.feed(evt("r1", None, "run.started"))
    b.feed(evt("a1", None, "agent.started"))
    b.feed(evt("e1", "a1", "error", {"message": "boom"}))
    b.feed(evt("end", None, "run.finished", {"status": "error"}))
    tree = b.build()
    root = tree.spans[0]
    assert root.status == "error"


def test_usage_observed_distinguishes_zero_from_absent() -> None:
    """A3：0 值与"协议没给"必须可区分——后者才是 max_tokens 判 skipped 的依据。"""
    b = TraceBuilder()
    b.feed(evt("r1", None, "run.started"))
    b.feed(evt("m1", "r1", "model.request", {"model": "m"}))
    # 只带 input_tokens（=0 也是观测）：output/cache 两侧未观测
    b.feed(evt("m2", "m1", "model.response", {"usage": {"input_tokens": 0}}))
    b.feed(evt("end", None, "run.finished", {"status": "success"}))
    tree = b.build()
    assert tree.usage_observed() == {
        "input_tokens": True,
        "output_tokens": False,
        "cache_tokens": False,
    }
    assert tree.usage_totals()["input_tokens"] == 0  # 观测到 0 与没观测是两件事
