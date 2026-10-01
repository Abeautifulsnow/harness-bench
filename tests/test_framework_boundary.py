"""E3/E4：框架通用性的机制化边界（change-plan §5）。

E3 是 deny-list：拦"字符串渗透"（平台名、字段名、方言前缀），拦不住语义渗透
——后者靠评审。定位是"把完全靠自觉升级为弱机制兜底"，不是"机制保证"。

判断标准比清单重要：**这个标识在换一个 SUT 之后还成立吗？**
成立 = 通用；不成立 = 越界。清单会过时，判断标准不会——这句话就是给
下一个人的维护说明：往清单里加东西之前先过一遍这条标准。

这条测试从第一天就是绿的（基线命中数为 0）：它不还债，只防止以后变脏——
下一个人往框架里加平台分支时，得先知道它一直在这儿看着。

---

**2026-09-30 复核（四处收口，全部是"护栏自己不够严"，不是框架变脏了）**：

1. **大小写变体漏网**：原实现是 `token in text` 的**大小写敏感精确子串**。
   于是 `AI-Chatbot` / `ai_chatbot` / `conversation_id` / `toolCallId` /
   `parentMessageId` / `tool_approval_request` 这些**同一个标识的写法差异**
   全部能溜过去——而写 Python 的人恰恰更可能写下划线形式。改为
   `token.lower() in text.lower()`，并补齐 snake_case 变体。
2. **只扫 `*.py`**：`reports/templates/report.html.j2` 是包内唯一的非 .py 文件，
   平台名写进 Jinja 模板原本无人拦。改为扫包内全部文本文件
   （跳过 `__pycache__` 与 `*.pyc`，它们不是源码）。
3. **注释与代码不一致**：原注释写"dev/ 与 tests/ 豁免"，但 `SRC.rglob` 从来没
   豁免过 `dev/`。**取更严的一侧**（继续扫 dev/，把注释改对）：`dev/mock_server.py`
   实现的是**通用协议**（`/health`、`/api/agent/sessions/*`），它没有任何理由
   出现某个 SUT 的方言；给它开豁免=在护栏上主动开洞，而它本来就是干净的。
4. **`data-connector-event` / `data-permission-mode` / `data-queue-status` 缺登记**：
   前三者是 `data-sub-` 一族的同类（UIMessage 自定义 data part），后者是 platform
   专有 part。同族标识只登记一半，等于给"新写一个 data part"留了后门。

清单仍然**只是清单**：它拦不住"用另一套词说同一件事"（语义渗透）。这一条写在
docstring 里而不是靠测试表达，因为它是无法机检的——机制能给的就是这么多。
"""

from __future__ import annotations

from pathlib import Path

from agent_eval.adapters.base import SessionContext

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "agent_eval"

# 专有方言登记表（change-plan §5 E3）。扫描范围 `src/agent_eval/` 下的**全部
# 文本文件**（含模板），`__pycache__`/`*.pyc` 除外。tests/ 不在包内，天然不扫。
# 只登记"专有方言"，不登记机制：metadata 这类 PRD §7.2 定义的通用形状不进清单，
# 否则防渗透的测试会变成阻碍正常开发的噪音。
PROPRIETARY_DIALECTS: tuple[str, ...] = (
    # --- 平台名（含下划线/连字符两种写法）---
    "ai-chatbot",
    "ai_chatbot",
    # --- MCP 工具命名空间分隔符（mcp__<server>__<tool>）---
    "mcp__",
    # --- UIMessage 自定义 data part（ai-chatbot 侧方言，§8 词汇里没有对应物）---
    "data-sub-",
    "data-context-usage",
    "data-task",
    "data-connector-event",
    "data-permission-mode",
    "data-queue-status",
    # --- UIMessage stream 方言 ---
    "tool-approval-request",
    "tool-output-denied",
    # --- 会话字段名（camelCase 与 snake_case 都要拦）---
    "projectDir",
    "project_dir",
    "conversationId",
    "conversation_id",
    "toolCallId",
    "tool_call_id",
    "parentMessageId",
    "parent_message_id",
    # --- 环境变量前缀 ---
    "SIACT_",
    "siact",
)


def _text_files() -> list[Path]:
    """包内全部文本文件：`__pycache__` 与字节码不是源码，排除。"""
    return [
        path
        for path in sorted(SRC.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    ]


def test_framework_source_is_free_of_proprietary_dialects() -> None:
    offenders: list[str] = []
    for path in _text_files():
        lowered = path.read_text(encoding="utf-8").lower()
        for token in PROPRIETARY_DIALECTS:
            if token.lower() in lowered:
                offenders.append(f"{path.relative_to(REPO)}: {token}")
    assert offenders == []


def test_the_sweep_actually_covers_files_and_templates() -> None:
    """护栏自检：扫描面必须非空**且**包含非 .py 文件。

    否则"零命中"可能只是因为扫了个空目录——第一版改大小写/后缀时最容易踩的坑
    （把 glob 写错，测试照样绿，护栏静默失效）。
    """
    files = _text_files()
    assert len(files) > 50, f"扫描面太小，glob 可能写错了：{len(files)}"
    assert any(path.suffix != ".py" for path in files), (
        "扫描面里没有非 .py 文件：模板（reports/templates/*.j2）会脱离护栏"
    )


def test_variant_spellings_of_registered_dialects_would_be_caught() -> None:
    """对照组：同一标识的常见写法差异必须都能命中（大小写与分隔符变体）。

    这条不扫仓库，只验证匹配逻辑本身——与被拦的具体名单无关，
    防止"以后有人把匹配改回大小写敏感"。
    """
    samples = {
        "AI-Chatbot": "ai-chatbot",
        "ai_chatbot": "ai_chatbot",
        "CONVERSATIONID": "conversationId",
        "conversation_id": "conversation_id",
        "toolCallId": "toolCallId",
        "parent_message_id": "parent_message_id",
        "Data-Sub-Open": "data-sub-",
        "Tool-Approval-Request": "tool-approval-request",
    }
    for sample, token in samples.items():
        assert token.lower() in sample.lower(), f"{sample} 未被 {token} 命中"


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
