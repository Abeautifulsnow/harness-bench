"""Challenge Set（PRD §19）与 Nightly Profile（PRD §40）。

Challenge 的两条契约：
1. **七类各至少一条，且断言可红**：每条 case 的断言都落在真实观测面上
   （工具参数、tool.result 载荷、subagent span、compaction 计数），
   不是"输出里出现某个词"的假覆盖——用"拿掉行为标记后必须变红"证明。
2. **默认不作为 PR Hard Gate**：pr/main/release 三个 gate 的必跑套件
   都不含 challenge（PRD §19 原文），失败是能力探测结论，不阻塞 PR。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent_eval.loading.loader import load_profile, resolve_suites
from agent_eval.models.regression import BaselineMode
from agent_eval.quality.gates import load_gate_rules
from agent_eval.runner.runner import RunConfig, Runner

REPO = Path(__file__).resolve().parents[1]

CHALLENGE_CATEGORIES = {
    "challenge:long-chain",
    "challenge:ambiguous-intent",
    "challenge:multi-tool-collab",
    "challenge:tool-error",
    "challenge:context-conflict",
    "challenge:long-context",
    "challenge:multi-subagent",
}


def test_challenge_suite_covers_all_seven_categories() -> None:
    selected, counts = resolve_suites(
        _benchmark(),
        _suites(),
        _cases(),
        ["challenge"],
        None,
    )
    assert counts["challenge"] == 7
    tags = {t for case in selected for t in case.tags}
    assert tags >= CHALLENGE_CATEGORIES, "PRD §19 七类能力上限必须各至少一条"


def test_challenge_is_not_a_hard_gate_suite() -> None:
    """PRD §19：默认不作为 PR Hard Gate——三个 gate 的必跑套件都不含 challenge。"""
    for gate in ("pr", "main", "release"):
        rules = load_gate_rules(REPO / "evals", gate)
        assert "challenge" not in rules.suites, f"{gate} gate 不得要求 challenge 套件"


@pytest.mark.asyncio
async def test_challenge_suite_passes_end_to_end(evals_tree) -> None:
    evals_root, data_root = evals_tree
    fixtures_root = Path(__file__).resolve().parents[1] / "fixtures"
    outcome = await Runner(
        RunConfig(
            evals_root=evals_root,
            fixtures_root=fixtures_root,
            data_root=data_root,
            benchmark="database-core",
            agent_endpoint="fake://",
            suites=["challenge"],
            no_judge=True,
            baseline_policy=BaselineMode.no_baseline.value,
        )
    ).run()
    assert outcome.verdict == "pass", _failure_detail(outcome)
    failing = [c.case_id for c in outcome.aggregate.cases if c.blocking_failures]
    assert not failing
    # smoke profile 的 judge metric 在 --no-judge 下 skipped：挑战集的判定
    # 全部来自 native / harness，这正是"确定性可判"的自我验证。
    assert all(c.evaluated_metrics > 0 for c in outcome.aggregate.cases)


@pytest.mark.asyncio
async def test_challenge_assertions_can_go_red(evals_tree) -> None:
    """拿掉行为标记（模糊意图不落参数）后，断言必须变红。

    这是"不是假覆盖"的直接证明：tool_arguments 断言的是参数里真的绑了口径，
    默认脚本（无 sql/days 参数）必挂——输出里那句话不存在，参数也不存在。
    """
    evals_root, data_root = evals_tree
    case_path = (
        evals_root / "datasets" / "database-core" / "cases" / "challenge.ambiguous_intent.yaml"
    )
    payload = yaml.safe_load(case_path.read_text(encoding="utf-8"))
    payload["input"]["prompt"] = "帮我看下销售情况。"  # 无标记 → 默认脚本（无口径参数）
    case_path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")

    fixtures_root = Path(__file__).resolve().parents[1] / "fixtures"
    outcome = await Runner(
        RunConfig(
            evals_root=evals_root,
            fixtures_root=fixtures_root,
            data_root=data_root,
            benchmark="database-core",
            agent_endpoint="fake://",
            suites=["challenge"],
            no_judge=True,
            baseline_policy=BaselineMode.no_baseline.value,
        )
    ).run()
    red = next(c for c in outcome.aggregate.cases if c.case_id == "challenge.ambiguous_intent")
    assert red.blocking_failures, "无口径参数时 tool_arguments 必须判 fail"
    assert outcome.verdict == "fail"
    assert outcome.exit_code == 1


def test_nightly_profile_enables_the_six_judge_metrics() -> None:
    """PRD §40：nightly 启用六个 judge 指标；GEval（custom.*）未实现故缺位。"""
    profile = load_profile(REPO / "evals", "nightly")
    ids = [spec.id for spec in profile.metrics]
    assert ids == [
        "agent.task_completion",
        "agent.step_efficiency",
        "agent.tool_correctness",
        "agent.argument_correctness",
        "agent.plan_quality",
        "agent.plan_adherence",
    ]
    # 不带 fallback：夜间档要的是语义判定全量信号，judge 不可用应记 exit 2
    # 而不是静默降级成 native（profile 级别的口径，见模块 description）。
    assert all(spec.fallback is None for spec in profile.metrics)
    assert all(spec.provider == "deepeval" for spec in profile.metrics)


# ---------------------------------------------------------------- helpers


def _benchmark():
    from agent_eval.loading.loader import load_benchmark

    return load_benchmark(REPO / "evals", "database-core")


def _suites():
    from agent_eval.loading.loader import load_suites

    return load_suites(REPO / "evals")


def _cases():
    from agent_eval.loading.loader import load_dataset

    _, cases = load_dataset(REPO / "evals", "database-core")
    return cases


def _failure_detail(outcome) -> str:
    lines = [f"verdict={outcome.verdict}"]
    for case in outcome.aggregate.cases:
        for failure in case.blocking_failures:
            lines.append(f"  {case.case_id}: {failure.get('metric')} {failure.get('reason')}")
    return "\n".join(lines)
