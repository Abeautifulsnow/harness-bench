"""Human Review / Review Queue（PRD §60/§61，Spec §5.2/§5.4）。

约束（Spec §5.2，不得放宽）：
  - verdict 五值，不得增删
  - human_verdict 与 machine_verdict 并存，人工结论不覆盖机器结果
  - Review 对象 = run 内 case 聚合（全部 iteration），不针对单个 iteration
  - EXPECTED / FALSE_POSITIVE / FALSE_NEGATIVE 的 note 必填
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from agent_eval.errors import InvalidCallError
from agent_eval.ids import new_id
from agent_eval.models.results import CaseRunResult, CaseStatus, Stability
from agent_eval.models.run import RunMetadata
from agent_eval.regression.stability import compute_stability

# PRD §60 五值（不得增删）
VERDICTS = ("PASS", "FAIL", "EXPECTED", "FALSE_POSITIVE", "FALSE_NEGATIVE")
NOTE_REQUIRED = ("EXPECTED", "FALSE_POSITIVE", "FALSE_NEGATIVE")

# PRD §61 六类入队条件（Spec §5.4 的 queue_reason 取值）
QUEUE_REASONS = (
    "near-threshold",
    "evaluator-conflict",
    "regression",
    "security",
    "flaky",
    "evaluation-failure",
)

NEAR_THRESHOLD_MARGIN = 0.05  # "接近阈值"的定义：|score - threshold| <= 0.05


class ReviewStore:
    """``<state_root>/human_reviews.jsonl``（append-only，最后一条同 (run, case) 生效）。"""

    def __init__(self, state_root: Path) -> None:
        self.root = state_root
        self.path = state_root / "human_reviews.jsonl"

    def add(
        self,
        run_id: str,
        case_id: str,
        verdict: str,
        *,
        reviewer: str | None = None,
        note: str = "",
        queue_reason: str | None = None,
    ) -> dict:
        verdict = verdict.upper()
        if verdict not in VERDICTS:
            raise InvalidCallError(
                f"invalid review verdict '{verdict}' (PRD §60: {', '.join(VERDICTS)})"
            )
        if verdict in NOTE_REQUIRED and not note.strip():
            raise InvalidCallError(f"verdict {verdict} requires --note (Spec §5.2: 必须留判定理由)")
        if queue_reason is not None and queue_reason not in QUEUE_REASONS:
            raise InvalidCallError(
                f"invalid queue_reason '{queue_reason}' (PRD §61: {', '.join(QUEUE_REASONS)})"
            )
        record = {
            "id": new_id("hr"),
            "run_id": run_id,
            "case_id": case_id,
            "reviewer": reviewer,
            "verdict": verdict,
            "note": note,
            "queue_reason": queue_reason,
            "created_at": datetime.now().astimezone().isoformat(),
        }
        self.root.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def records(self) -> list[dict]:
        if not self.path.is_file():
            return []
        out: list[dict] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def effective(self, run_id: str | None = None, case_id: str | None = None) -> dict[tuple, dict]:
        latest: dict[tuple, dict] = {}
        for record in self.records():
            if run_id and record.get("run_id") != run_id:
                continue
            if case_id and record.get("case_id") != case_id:
                continue
            latest[(record.get("run_id"), record.get("case_id"))] = record
        return latest

    def list(
        self,
        run_id: str | None = None,
        status: str | None = None,
        queue_reason: str | None = None,
    ) -> list[dict]:
        """``--status`` 过滤的是"待评审状态"：pending = 已入队但尚无人工 verdict。"""
        out: list[dict] = []
        for record in self.effective(run_id).values():
            if queue_reason and record.get("queue_reason") != queue_reason:
                continue
            if status == "pending" and record.get("verdict"):
                continue
            out.append(record)
        return sorted(out, key=lambda r: (r.get("run_id") or "", r.get("case_id") or ""))


def queue_candidates(meta: RunMetadata, results: list[CaseRunResult]) -> list[dict]:
    """PRD §61：自动识别应进入 Review Queue 的 case，并给出入队理由。

    只做"入队建议"，不写库：入队是审计动作，需要人确认（或由 CI 显式调用）。
    """
    queue: list[dict] = []
    for case_id in sorted({r.case_id for r in results}):
        iterations = sorted([r for r in results if r.case_id == case_id], key=lambda r: r.iteration)
        stability = compute_stability(case_id, iterations)
        reasons: list[str] = []
        if stability.stability == Stability.FLAKY:
            reasons.append("flaky")
        if any(r.status == CaseStatus.ERROR for r in iterations):
            reasons.append("evaluation-failure")
        if any(
            m.metric.startswith("security.") and m.verdict in {"fail", "error"}
            for r in iterations
            for m in r.all_metric_results
        ):
            reasons.append("security")
        if _near_threshold(iterations):
            reasons.append("near-threshold")
        if _evaluator_conflict(iterations):
            reasons.append("evaluator-conflict")
        if stability.regression_state.value == "REGRESSION":
            reasons.append("regression")
        if reasons:
            queue.append(
                {
                    "run_id": meta.run_id,
                    "case_id": case_id,
                    "queue_reason": reasons[0],
                    "reasons": reasons,
                    "stability": stability.stability.value,
                    "machine_verdict": _machine_verdict(iterations),
                }
            )
    return queue


def _machine_verdict(iterations: list[CaseRunResult]) -> str:
    """机器结论（case 聚合口径，Spec §5.2）：PASS / FAIL / FLAKY。"""
    valid = [r for r in iterations if r.status in {CaseStatus.PASS, CaseStatus.FAIL}]
    if not valid:
        return "ERROR"
    passes = sum(1 for r in valid if r.status == CaseStatus.PASS)
    if passes == len(valid):
        return "PASS"
    if passes == 0:
        return "FAIL"
    return "FLAKY"


def _near_threshold(iterations: list[CaseRunResult]) -> bool:
    for result in iterations:
        for metric in result.all_metric_results:
            if metric.score is None or metric.threshold is None:
                continue
            if abs(metric.score - metric.threshold) <= NEAR_THRESHOLD_MARGIN:
                return True
    return False


def _evaluator_conflict(iterations: list[CaseRunResult]) -> bool:
    """Native 与 Semantic Evaluator 结论冲突（PRD §61）。"""
    for result in iterations:
        by_evaluator: dict[str, set[str]] = {}
        for metric in result.all_metric_results:
            by_evaluator.setdefault(metric.evaluator, set()).add(metric.verdict)
        native = by_evaluator.get("native", set())
        semantic = by_evaluator.get("deepeval", set())
        disagree = ("pass" in native and "fail" in semantic) or (
            "fail" in native and "pass" in semantic
        )
        if native and semantic and disagree:
            return True
    return False
