"""E3/E4：框架通用性的机制化边界（change-plan §5）。

E3 是 deny-list：拦"字符串渗透"（平台名、字段名、方言前缀），拦不住语义渗透
——后者靠评审。定位是"把完全靠自觉升级为弱机制兜底"，不是"机制保证"。

判断标准比清单重要：**这个标识在换一个 SUT 之后还成立吗？**
成立 = 通用；不成立 = 越界。清单会过时，判断标准不会——这句话就是给
下一个人的维护说明：往清单里加东西之前先过一遍这条标准。

这条测试从第一天就是绿的（基线命中数为 0）：它不还债，只防止以后变脏——
下一个人往框架里加平台分支时，得先知道它一直在这儿看着。
"""

from __future__ import annotations

from pathlib import Path

from agent_eval.adapters.base import SessionContext

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "agent_eval"

# 专有方言登记表（change-plan §5 E3）。扫描范围 src/agent_eval/；dev/ 与 tests/
# 豁免——dev/mock_server.py 是测试替身，允许出现具体形状。
# 只登记"专有方言"，不登记机制：metadata 这类 PRD §7.2 定义的通用形状不进清单，
# 否则防渗透的测试会变成阻碍正常开发的噪音。
PROPRIETARY_DIALECTS: tuple[str, ...] = (
    "ai-chatbot",  # 平台名
    "mcp__",  # MCP 工具命名空间分隔符（mcp__<server>__<tool>）
    "data-sub-",  # 自定义 data part（子 agent 事件）
    "data-context-usage",  # 自定义 data part（用量）
    "data-task",  # 自定义 data part（任务）
    "tool-approval-request",  # UIMessage stream 方言
    "tool-output-denied",  # UIMessage stream 方言
    "projectDir",  # 会话字段名
    "project_dir",  # 会话字段名（snake_case 变体）
    "conversationId",  # 会话字段名
    "SIACT_",  # 环境变量前缀
    "siact",  # 环境变量前缀（小写变体）
)


def test_framework_source_is_free_of_proprietary_dialects() -> None:
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for token in PROPRIETARY_DIALECTS:
            if token in text:
                offenders.append(f"{path.relative_to(REPO)}: {token}")
    assert offenders == []


def test_session_context_keeps_extra_opaque() -> None:
    """E4：环境透传走不透明 extra，不得长出具名官方字段（各平台字段的并集）。

    一旦官方字段（如 project_dir）出现，第二个 SUT 的环境概念不同就得再加一个，
    SessionContext 会退化成字段并集——E2 反模式 1 的字段版本。
    """
    assert set(SessionContext.model_fields) == {
        "eval_run_id",
        "case_id",
        "variant_id",
        "iteration",
        "extra",
    }
    ctx = SessionContext(eval_run_id="r", case_id="c", iteration=1)
    assert ctx.extra == {}  # 缺省空 dict：接入方按需扩展，框架不解其义
