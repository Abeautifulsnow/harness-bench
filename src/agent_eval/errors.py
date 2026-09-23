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
