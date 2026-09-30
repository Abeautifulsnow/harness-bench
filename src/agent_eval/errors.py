"""Error taxonomy and CI exit codes (Spec V2.1.1 §6.1).

0  Gate evaluated and PASS
1  Gate evaluated and FAIL (blocking failure)
2  Gate cannot be reliably evaluated (infrastructure failure)
3  Invalid call, not retryable (bad benchmark / config / unresolved metric)
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_GATE_FAIL = 1
EXIT_INFRA = 2
EXIT_INVALID = 3


class AgentEvalError(Exception):
    """Base class: carries the CLI exit code it maps to."""

    exit_code = 1

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InvalidCallError(AgentEvalError):
    """Invalid, non-retryable invocation (Spec §6.1 exit 3)."""

    exit_code = EXIT_INVALID


class InfraError(AgentEvalError):
    """Infrastructure failure: agent endpoint unreachable, transport broken (exit 2)."""

    exit_code = EXIT_INFRA


class EvaluationInfraError(AgentEvalError):
    """Judge / evaluation infrastructure failure after retries (exit 2)."""

    exit_code = EXIT_INFRA


class JudgeInputUnavailableError(Exception):
    """Judge 的输入在本次观测里不存在（Spec §19.1.1 第三结局：观测不到）。

    **刻意不继承 ``EvaluationInfraError``**：二者在报告上是两种结论。
    "判分器坏了"是基础设施故障（exit 2）；"这一次观测里根本没有判分器要的那个
    分量"是观测不到（skipped）。并进 infra 会让一整条 case 变成
    EVALUATION_FAILURE、整个 run 升到 exit 2——而 agent 可能什么都没做错。

    实测来源（C 类第二版，2026-09-30）：本数据集 16 条 case 里 6 条不声明
    ``tools.required``、7 条没有 ``expected.output``。SDK 的
    ``check_llm_test_case_params`` 对 ``tools_called`` / ``expected_tools`` 为 None
    抛 ``MissingTestCaseParamsError``，对空 ``actual_output`` 抛的是同一个类——
    三者的语义都是"缺输入"，不是"判分器故障"。
    """


class MetricUnavailableError(InvalidCallError):
    """Profile metric needs a provider capability that is absent and has no fallback (§7.4)."""


class AgentFailureError(Exception):
    """Agent-side failure surfaced through the event stream (PRD §46 AGENT_FAILURE).

    Not an AgentEvalError: an agent failing a case is a *result*, not a CLI error.
    """


class AgentTimeoutError(AgentFailureError):
    """Agent exceeded its per-turn / session timeout (treated as agent behavior)."""


class UnsupportedAssertionError(Exception):
    """Assertion vocabulary not expressible by the P0 native evaluator."""
