"""稳定性判定（Spec §3）与回归判定表（§3.3）测试。"""

from __future__ import annotations

from agent_eval.models.results import (
    CaseRunResult,
    CaseStability,
    CaseStatus,
    Stability,
)
from agent_eval.regression.stability import compute_stability, regression_state


def run(status: CaseStatus, tools: list[str] | None = None, latency: int = 100) -> CaseRunResult:
    from agent_eval.models.results import ToolCallRecord

    return CaseRunResult(
        id=f"cr_{status}_{latency}_{tools}",
        run_id="r1",
        case_id="c1",
        case_version=1,
        iteration=1,
        status=status,
        latency_ms=latency,
        tool_calls=[ToolCallRecord(name=t) for t in (tools or [])],
    )


def _cycles(statuses: list[CaseStatus]) -> list[CaseRunResult]:
    return [run(s, latency=100 + i * 10) for i, s in enumerate(statuses)]


def test_stable_pass_fail_and_flaky() -> None:
    s = compute_stability("c1", _cycles([CaseStatus.PASS] * 3))
    assert s.stability == Stability.STABLE_PASS and s.pass_rate == 1.0
    s = compute_stability("c1", _cycles([CaseStatus.FAIL] * 4))
    assert s.stability == Stability.STABLE_FAIL and s.pass_rate == 0.0
    s = compute_stability("c1", _cycles([CaseStatus.PASS, CaseStatus.FAIL, CaseStatus.PASS]))
    assert s.stability == Stability.FLAKY


def test_error_iterations_excluded() -> None:
    iters = [
        run(CaseStatus.PASS),
        run(CaseStatus.ERROR),
        run(CaseStatus.PASS),
        run(CaseStatus.PASS),
    ]
    s = compute_stability("c1", iters)
    assert s.stability == Stability.STABLE_PASS
    assert s.valid_iterations == 3
    assert s.infra_error_count == 1


def test_unknown_when_fewer_than_three() -> None:
    assert compute_stability("c1", [run(CaseStatus.PASS)]).stability == Stability.UNKNOWN
    assert compute_stability("c1", []).stability == Stability.UNKNOWN


def test_variance_fields() -> None:
    iters = [
        run(CaseStatus.PASS, tools=["a", "b"], latency=100),
        run(CaseStatus.PASS, tools=["a", "b"], latency=100),
        run(CaseStatus.PASS, tools=["a", "b"], latency=100),
    ]
    s: CaseStability = compute_stability("c1", iters)
    assert s.tool_sequence_variance == 0.0
    mixed = [
        run(CaseStatus.PASS, tools=["a"], latency=100),
        run(CaseStatus.PASS, tools=["b"], latency=300),
        run(CaseStatus.PASS, tools=["c"], latency=200),
    ]
    s2 = compute_stability("c1", mixed)
    assert s2.tool_sequence_variance == 1.0  # 两两完全不交
    assert s2.latency_cv is not None and s2.latency_cv > 0


def test_pass_hat_k_all_must_pass() -> None:
    """pass^k 与 pass@k 对照：exploration 看至少一次成功，"从不失败"看全部成功。"""
    stable = compute_stability("c1", _cycles([CaseStatus.PASS] * 3))
    assert stable.pass_at_k == {"pass@1": 1.0, "pass@3": 1.0}
    assert stable.pass_hat_k == {"pass^1": 1.0, "pass^3": 1.0}

    flaky = compute_stability("c1", _cycles([CaseStatus.PASS, CaseStatus.FAIL, CaseStatus.PASS]))
    assert flaky.pass_at_k["pass@3"] == 1.0  # 至少一次成功
    assert flaky.pass_hat_k["pass^3"] == 0.0  # 但不是每次都成功


def test_pass_hat_k_key_absent_below_repeat() -> None:
    """与 pass@k 同约定：repeat < k 时该 key 不出现（repeat=1 全空）。"""
    one = compute_stability("c1", [run(CaseStatus.PASS)])
    assert one.pass_at_k == {"pass@1": 1.0}
    assert one.pass_hat_k == {"pass^1": 1.0}
    two = compute_stability("c1", _cycles([CaseStatus.FAIL, CaseStatus.FAIL]))
    assert "pass^3" not in two.pass_hat_k
    assert two.pass_hat_k["pass^1"] == 0.0


def test_pass_hat_k_error_iterations_not_counted_as_failures() -> None:
    """ERROR 轮不参与统计：基础设施抖动不得把稳定 case 的 pass^k 打成 0。"""
    iters = [
        run(CaseStatus.PASS),
        run(CaseStatus.ERROR),
        run(CaseStatus.PASS),
        run(CaseStatus.PASS),
    ]
    s = compute_stability("c1", iters)
    assert s.pass_hat_k["pass^3"] == 1.0
    assert s.infra_error_count == 1


def test_regression_table() -> None:
    from agent_eval.models.results import RegressionState as R

    assert regression_state(Stability.STABLE_PASS, Stability.STABLE_PASS) == "UNCHANGED"
    assert regression_state(Stability.STABLE_PASS, Stability.STABLE_FAIL) == "REGRESSION"
    assert regression_state(Stability.STABLE_PASS, Stability.FLAKY) == "REGRESSION"
    assert regression_state(Stability.STABLE_FAIL, Stability.STABLE_PASS) == "IMPROVED"
    assert regression_state(Stability.STABLE_FAIL, Stability.FLAKY) == "UNCHANGED"
    assert regression_state(Stability.FLAKY, Stability.STABLE_PASS) == "FLAKY"
    assert regression_state(None, Stability.STABLE_PASS) == R.UNDETERMINED.value
    assert regression_state(Stability.UNKNOWN, Stability.STABLE_PASS) == "UNDETERMINED"
    assert regression_state(None, Stability.STABLE_PASS, candidate_invalid=True) == "INVALID"
