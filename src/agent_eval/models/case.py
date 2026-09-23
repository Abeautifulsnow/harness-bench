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

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

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
        "permission",
    }
)


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


class Assertion(BaseModel):
    """One assertion block; all checks inside combine with AND (Spec §2.2)."""

    model_config = {"extra": "ignore"}

    output: OutputAssertion = Field(default_factory=OutputAssertion)
    tools: ToolAssertion = Field(default_factory=ToolAssertion)
    constraints: ConstraintAssertion = Field(default_factory=ConstraintAssertion)
    # Case-level extensions (never valid on turn level, Spec §2.2).
    extensions: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _extract_extensions(cls, data: Any) -> Any:
        if isinstance(data, dict):
            known = {"output", "tools", "constraints", "extensions"}
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
