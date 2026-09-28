"""Eval Case models: unified Assertion Schema + multi-turn Case (Spec V2.1.1 §2).

Assertion Schema is the single platform vocabulary; mount points:
  - case level ``expected``           (Aggregation semantics see §2.4)
  - turn level ``input.turns[i].expect``
  - session level ``expected.final``
Fields outside the vocabulary (status, exit_code, database state, git diff, ...)
are case-level extensions stored in ``Assertion.extensions`` and consumed by the
native evaluator only.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

_SPLIT_PATH = re.compile(r"\.")
_INDEX_PATH = re.compile(r"^([A-Za-z_][\w\-]*)?\[(\d+)\]$")

EXTENSION_KEYS = frozenset(
    {
        "status",
        "exit_code",
        "database_state",
        "file_state",
        "git_diff",
        "pytest",
        "build",
        "lint",
        "sql_result",
        "tool_arguments",
        "step_efficiency",
    }
)

# 词汇表内但明确不实现（执行型断言）：Spec §19。
# 它们要在 fixture 里**运行**命令并取结果，等于给评测流程引入任意代码执行，
# 与 PRD §88 的沙箱规划直接冲突（V1 只在 local 执行、无隔离）。
# 保留在词汇表里是因为 PRD §34 列了它们；但 `case validate` 必须报"依赖沙箱"，
# 而不是笼统的"尚未实现"——前者的下一步动作是明确的。
SANDBOX_DEPENDENT_KEYS = frozenset({"pytest", "build", "lint"})
# 词汇表内、但已**裁决不做**的键（Spec §20.4）：键 → "为什么不做 + 替代手段"。
# 与上面的执行型分开：那些是"等沙箱"，这些是"有替代手段，不打算做"。
# 理由与键同一处声明——分开放的话，加键的人会漏掉理由，于是报错退化成空字符串的
# 笼统"尚未实现"，那正是这个子集存在的理由（让下一步动作明确）。
# 报错文案从这里取，集合也从这里取：**唯一来源**，不存在两边漂移的可能。
DEFERRED_REASONS: dict[str, str] = {
    "git_diff": (
        "不实现——fixture workdir 会被 copytree 到平台仓库工作树内部，"
        "那里跑 git diff 报的是平台源码的改动（agent 新建的文件反而不可见）；"
        "改用 file_state 做文件快照比对（Spec §20.4）"
    ),
}
DEFERRED_EXTENSION_KEYS = frozenset(DEFERRED_REASONS)


class OutputAssertion(BaseModel):
    exact: str | None = None
    contains: list[str] = Field(default_factory=list)
    not_contains: list[str] = Field(default_factory=list)
    regex: str | None = None
    json_schema: dict[str, Any] | None = None

    def is_empty(self) -> bool:
        return (
            self.exact is None
            and not self.contains
            and not self.not_contains
            and self.regex is None
            and self.json_schema is None
        )


class ToolAssertion(BaseModel):
    required: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.required and not self.forbidden


class ConstraintAssertion(BaseModel):
    max_tool_calls: int | None = None
    max_latency_ms: int | None = None
    max_tokens: int | None = None
    max_cost: float | None = None

    def is_empty(self) -> bool:
        return (
            self.max_tool_calls is None
            and self.max_latency_ms is None
            and self.max_tokens is None
            and self.max_cost is None
        )


class ToolArgumentMatcher(BaseModel):
    """声明式工具参数断言（Spec V2.2 §11.2，`tool_arguments` 扩展）。

    参数路径用点号表示嵌套（``payload.sql``），列表下标用 ``[i]``（``rows[0].id``）。
    三种匹配器互斥；都不给即"该路径必须存在"。

    ``semantic: true`` 是**显式开关**（Spec §20.3，PRD §57）：打开后字符串值按
    归一化规则比对（SQL 大小写/空白/标识符引号、数字串、路径写法）。默认关闭——
    `exact` 的字面语义不变，因为"把大小写差异判成不同"在某些用例里正是要断言的事。
    """

    model_config = {"extra": "forbid"}

    exact: Any = None
    contains: str | None = None
    regex: str | None = None
    semantic: bool = False

    def check(self, value: Any, *, sql_dialect: str | None = None) -> str | None:
        """返回不匹配原因；``None`` 表示通过。"""
        if self.exact is not None:
            if value == self.exact:
                return None
            if self.semantic:
                from agent_eval.regression.semantic import compare_values

                verdict = compare_values(self.exact, value, sql_dialect=sql_dialect)
                if verdict.same:
                    return None
                if verdict.degraded:
                    return (
                        f"expected {self.exact!r} (semantic), got {value!r}"
                        f"；语义比对已降级：{verdict.degraded}"
                    )
            return f"expected {self.exact!r}, got {value!r}"
        if self.contains is not None:
            if not isinstance(value, str) or self.contains not in value:
                return f"expected to contain {self.contains!r}, got {value!r}"
            return None
        if self.regex is not None:
            import re

            if not isinstance(value, str) or re.search(self.regex, value) is None:
                return f"expected to match {self.regex!r}, got {value!r}"
            return None
        return None


def argument_path(value: Any, path: str) -> tuple[bool, Any]:
    """按点号/下标路径取值：返回 (是否存在, 值)。路径语法的唯一实现点。"""
    current = value
    for part in _SPLIT_PATH.split(path):
        if not part:
            continue
        match = _INDEX_PATH.fullmatch(part)
        if match is not None:
            name, index = match.group(1), int(match.group(2))
            if name:
                if not isinstance(current, dict) or name not in current:
                    return False, None
                current = current[name]
            if not isinstance(current, list) or index >= len(current):
                return False, None
            current = current[index]
            continue
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


class StepEfficiencyAssertion(BaseModel):
    """步数效率声明（Spec V2.2 §11.1，扩展键 ``step_efficiency``）。

    显式声明才评测：把 ``tools.required`` 或 ``max_tool_calls`` 隐式当作步数基线
    会让既有 case 突然判 FAIL（那些声明表达的是"必须调用"与"上限"，不是理想步数）。
    """

    model_config = {"extra": "forbid"}

    baseline_steps: int  # 理想步数（一次成功执行最少需要的工具调用数）
    max_ratio_delta: float = 0.0  # 允许超出的比例，0 = 不得超过基线

    def limit(self) -> float:
        return self.baseline_steps * (1.0 + self.max_ratio_delta)


class TableExpectation(BaseModel):
    """单表期望（Spec §19.2，扩展键 ``database_state``）。三者可并用，是 AND。"""

    model_config = {"extra": "forbid"}

    min_rows: int | None = None
    max_rows: int | None = None
    exists: bool | None = None  # None = 只判行数，不要求存在性


class DatabaseStateAssertion(BaseModel):
    """``database_state`` 扩展（Spec §19.2）。

    判定对象是**执行结束后的 fixture 库**，不是 agent 的输出：agent 说"已清理"
    与表真的空了是两件事，这条断言只认后者。观测来源是 fixture handle 的
    ``db_path``——只有 sqlite fixture 提供，其余环境判 skipped（不判 pass）。
    """

    model_config = {"extra": "forbid"}

    tables: dict[str, TableExpectation] = Field(default_factory=dict)
    tables_absent: list[str] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.tables and not self.tables_absent


class FileExpectation(BaseModel):
    """单文件期望（Spec §19.3，扩展键 ``file_state``）。"""

    model_config = {"extra": "forbid"}

    exists: bool | None = None
    contains: list[str] = Field(default_factory=list)
    not_contains: list[str] = Field(default_factory=list)


class FileStateAssertion(BaseModel):
    """``file_state`` 扩展（Spec §19.3）。路径相对 fixture workdir，越界即报错。"""

    model_config = {"extra": "forbid"}

    files: dict[str, FileExpectation] = Field(default_factory=dict)
    absent: list[str] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.files and not self.absent


class ExitCodeAssertion(BaseModel):
    """``exit_code`` 扩展（Spec §19.4）。

    语义固定为"**被观测到的命令**的退出码"，不是"agent 进程的退出码"——
    Agent 走事件协议，没有进程退出码这个东西。缺省要求全部命令以 0 结束：
    忽略了失败命令的 agent 是最常见的退化路径。``command`` 只看指定可执行名。
    """

    model_config = {"extra": "forbid"}

    expect: int = 0
    command: str | None = None


class SqlResultAssertion(BaseModel):
    """``sql_result`` 扩展（Spec §19.5）。

    判定对象是 SQL 工具的**返回结果**（``tool.result`` 的 payload），不是输出文本。
    能判的行数/内容都必须能在不引入语义判断的前提下算出来；结果形状无法识别时
    判 skipped（观测不足），不判 pass。
    """

    model_config = {"extra": "forbid"}

    min_rows: int | None = None
    max_rows: int | None = None
    contains: list[str] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return self.min_rows is None and self.max_rows is None and not self.contains


class SecurityAssertion(BaseModel):
    """``security:`` 挂载点（PRD §62/§63，Spec V2.2 §12）。

    与 output/tools/constraints 并列的独立挂载点：消费"被观测到的行为"
    （工具参数、命令、路径、SQL、MCP 调用），而不是输出文本 —— 输出文本可被
    攻击者改写，不能作为安全判定依据。
    """

    model_config = {"extra": "forbid"}

    forbidden_tools: list[str] = Field(default_factory=list)
    forbidden_paths: list[str] = Field(default_factory=list)  # 前缀匹配
    forbidden_commands: list[str] = Field(default_factory=list)  # 可执行名，如 rm/curl
    forbidden_sql: list[str] = Field(default_factory=list)  # 正则
    forbidden_mcp: list[str] = Field(default_factory=list)
    secret_patterns: list[str] = Field(default_factory=list)  # 正则
    allow_permission_override: bool = False  # False = 出现提权即失败

    def is_empty(self) -> bool:
        return (
            not any(
                (
                    self.forbidden_tools,
                    self.forbidden_paths,
                    self.forbidden_commands,
                    self.forbidden_sql,
                    self.forbidden_mcp,
                    self.secret_patterns,
                )
            )
            and not self.allow_permission_override
        )


class Assertion(BaseModel):
    """One assertion block; all checks inside combine with AND (Spec §2.2)."""

    model_config = {"extra": "ignore"}

    output: OutputAssertion = Field(default_factory=OutputAssertion)
    tools: ToolAssertion = Field(default_factory=ToolAssertion)
    constraints: ConstraintAssertion = Field(default_factory=ConstraintAssertion)
    security: SecurityAssertion = Field(default_factory=SecurityAssertion)
    # Case-level extensions (never valid on turn level, Spec §2.2).
    extensions: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _extract_extensions(cls, data: Any) -> Any:
        if isinstance(data, dict):
            known = {"output", "tools", "constraints", "security", "extensions"}
            ext = dict(data.get("extensions") or {})
            for key, value in data.items():
                if key not in known:
                    ext[key] = value
            data = {k: v for k, v in data.items() if k in known} | {"extensions": ext}
        return data

    def is_empty(self) -> bool:
        return (
            self.output.is_empty()
            and self.tools.is_empty()
            and self.constraints.is_empty()
            and self.security.is_empty()
            and not self.extensions
        )


class TurnInput(BaseModel):
    user: str
    expect: Assertion | None = None
    timeout: float | None = None  # seconds; inherits execution.timeout when None


class CaseInput(BaseModel):
    type: Literal["single_turn", "multi_turn"] = "single_turn"
    prompt: str | None = None
    turns: list[TurnInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_shape(self) -> CaseInput:
        if self.type == "single_turn":
            if not self.prompt:
                raise ValueError("single_turn input requires 'prompt'")
            if self.turns:
                raise ValueError("single_turn input must not define 'turns'")
        elif not self.turns:
            raise ValueError("multi_turn input requires at least one turn")
        return self

    def messages(self) -> list[str]:
        if self.type == "single_turn":
            return [self.prompt or ""]
        return [t.user for t in self.turns]


class EnvironmentSpec(BaseModel):
    """Fixture / environment binding for a Case (PRD §12/§89)."""

    model_config = {"extra": "allow"}

    fixture: str | None = None
    database: str | None = None  # provider hint: sqlite | postgres | ...


class ExecutionSpec(BaseModel):
    timeout: float = 120  # whole-session timeout, seconds
    repeat: int = 1


class Case(BaseModel):
    id: str
    version: int
    name: str
    tags: list[str] = Field(default_factory=list)
    difficulty: str | None = None
    context: list[str] | None = None  # judge-facing background (PRD §37 mapping)
    source: dict[str, Any] | None = None  # provenance (PRD §16)
    input: CaseInput
    environment: EnvironmentSpec = Field(default_factory=EnvironmentSpec)
    execution: ExecutionSpec = Field(default_factory=ExecutionSpec)
    expected: Assertion = Field(default_factory=Assertion)
    # multi-turn 的 session 级挂载点（Spec §2.3 `expected.final`）：
    # output 作用于最终轮输出，tools/constraints 按 session 聚合（Spec §2.4）
    expected_final: Assertion | None = None
    evaluation_profile: str | None = None
    # metric id → 参数覆盖（Spec §17.2 的 case 级入口）。Profile 决定"跑哪些 metric、
    # 阈值多严"，Case 决定"这条用例的期望是什么"——harness 专项指标（该加载哪个
    # skill、该在哪一段压缩）是**用例的属性**，塞进 Profile 会让所有用例被迫共用一个期望，
    # 只能得到"上限类"的弱判定。键必须在 Profile 的 metrics 里存在，否则启动期 fail-fast
    # （声明了却不生效与"永不失败的断言"同类）。
    metric_params: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _split_final(cls, data: Any) -> Any:
        """把 ``expected.final`` 拆到独立字段（Spec §2.3 的 session 级挂载点）。

        单轮 Case 不得出现 final 层（Spec §2.3：单轮的等价表示"无 final 层"）。
        静默丢弃会让整组断言失效却仍报 PASS，因此这里直接拒绝。
        """
        if not isinstance(data, dict):
            return data
        expected = data.get("expected")
        if not (isinstance(expected, dict) and "final" in expected):
            return data
        input_spec = data.get("input")
        input_type = (
            input_spec.get("type", "single_turn") if isinstance(input_spec, dict) else "single_turn"
        )
        if input_type != "multi_turn":
            raise ValueError(
                "'expected.final' is only valid for multi_turn cases (Spec §2.3); "
                "single_turn cases declare assertions directly under 'expected'"
            )
        data = dict(data)
        data["expected_final"] = expected["final"]
        data["expected"] = {k: v for k, v in expected.items() if k != "final"}
        return data

    def session_assertions(self) -> list[tuple[str, Assertion]]:
        """session 作用域的全部挂载点：case 级 ``expected`` + ``expected.final``。

        各挂载点之间是 AND（Spec §2.2/§2.4），因此两处都要评测，不能只取其一。
        """
        mounts: list[tuple[str, Assertion]] = [("expected", self.expected)]
        if self.input.type == "multi_turn" and self.expected_final is not None:
            mounts.append(("expected.final", self.expected_final))
        return mounts
