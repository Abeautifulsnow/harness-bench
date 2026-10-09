"""Promote to Benchmark（PRD §51，Spec §5.3）。

promote 的对象是具体 CaseRun（iteration），不是 case 聚合：需要确定的那次 trace
作为种子（Spec §5.3-5）。产出是 Case Draft（PRD §15 生命周期 Draft → Reviewed → Active），
不直接修改 Suite。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from agent_eval.errors import InvalidCallError
from agent_eval.evaluators.online_eval import EvalPair
from agent_eval.failures.taxonomy import classify
from agent_eval.models.case import Assertion
from agent_eval.models.results import CaseRunResult
from agent_eval.models.run import RunMetadata
from agent_eval.storage.run_store import RunStore


@dataclass
class CaseDraft:
    """PRD §51 自动生成的 Case Draft。"""

    id: str
    case_id: str
    case_run_id: str
    run_id: str
    suite: str
    benchmark_id: str | None = None
    draft_status: str = "draft"  # PRD §15: draft → reviewed → active
    source_type: str = "issue"  # issue | bug | production
    source_ref: str | None = None
    failure_category: str | None = None
    classification_evidence: str = ""  # PRD §48：分类依据必须留存
    trace_reference: str | None = None
    tool_expectations: dict[str, list[str]] = field(default_factory=dict)
    suggested_assertions: dict[str, Any] = field(default_factory=dict)
    inputs: dict[str, Any] = field(default_factory=dict)
    environment: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now().astimezone().isoformat())

    def as_yaml(self) -> str:
        """Case Draft 的 YAML 形态（可直接落到 evals/datasets/<id>/cases/ 待评审）。"""
        return yaml.safe_dump(
            {
                "id": self.case_id,
                "version": 1,
                "name": f"promoted from {self.run_id}/{self.case_run_id}",
                "tags": ["regression", "promoted"],
                "difficulty": "medium",
                "source": {
                    "type": self.source_type,
                    "ref": self.source_ref,
                    "run_id": self.run_id,
                    "case_run_id": self.case_run_id,
                    "trace_reference": self.trace_reference,
                },
                "input": self.inputs,
                "environment": self.environment,
                "execution": {"timeout": 60, "repeat": 1},
                "expected": self.suggested_assertions,
            },
            allow_unicode=True,
            sort_keys=False,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "case_id": self.case_id,
            "case_run_id": self.case_run_id,
            "run_id": self.run_id,
            "suite": self.suite,
            "benchmark_id": self.benchmark_id,
            "draft_status": self.draft_status,
            "source_type": self.source_type,
            "source_ref": self.source_ref,
            "failure_category": self.failure_category,
            "classification_evidence": self.classification_evidence,
            "trace_reference": self.trace_reference,
            "tool_expectations": self.tool_expectations,
            "suggested_assertions": self.suggested_assertions,
            "inputs": self.inputs,
            "environment": self.environment,
            "created_at": self.created_at,
        }


class DraftStore:
    """Draft 落盘：``<state_root>/drafts/<draft_id>.json``（PRD §15 生命周期）。"""

    def __init__(self, state_root: Path) -> None:
        self.root = state_root / "drafts"
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, draft: CaseDraft) -> Path:
        path = self.root / f"{draft.id}.json"
        _write_json(path, draft.as_dict())
        return path

    def load(self, draft_id: str) -> dict[str, Any]:
        path = self.root / f"{draft_id}.json"
        if not path.is_file():
            raise InvalidCallError(f"case draft not found: {draft_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def list(self, suite: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if suite and payload.get("suite") != suite:
                continue
            if status and payload.get("draft_status") != status:
                continue
            out.append(payload)
        return out

    def set_status(self, draft_id: str, status: str) -> dict[str, Any]:
        payload = self.load(draft_id)
        payload["draft_status"] = status
        _write_json(self.root / f"{draft_id}.json", payload)
        return payload

    def to_case_yaml(self, draft_id: str, out_dir: Path) -> Path:
        """把 Draft 导出为 Case YAML（评审通过后由人工落库，见 PRD §15）。"""
        payload = self.load(draft_id)
        draft = CaseDraft(**{k: v for k, v in payload.items() if k in _DRAFT_FIELDS})
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{draft.case_id}.yaml"
        path.write_text(draft.as_yaml(), encoding="utf-8")
        return path


_DRAFT_FIELDS = set(CaseDraft.__dataclass_fields__)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def build_draft(
    meta: RunMetadata,
    case_run: CaseRunResult,
    *,
    suite: str = "regression",
    source_type: str = "issue",
    source_ref: str | None = None,
    benchmark_id: str | None = None,
    case_id: str | None = None,
    messages: list[str] | None = None,
    environment: dict[str, Any] | None = None,
    draft_id: str | None = None,
) -> CaseDraft:
    """PRD §51：由一次失败 CaseRun 反推 Case Draft。

    - 原始 input：优先用调用方传入的真实 messages（来自 Case 定义）；
      trace 里没有原始 prompt，编造 input 会让 Draft 无法回归，因此显式要求。
    - tool expectations：由该次实际工具序列给出 required/forbidden 建议
    - suggested assertions：由失败 metric 反推（保守：只声明确定性断言）
    """
    tools = [tool.name for tool in case_run.tool_calls]
    failed_metrics = [
        m for m in case_run.all_metric_results if m.blocking and m.verdict in {"fail", "error"}
    ]
    category: str | None = None
    evidence = ""
    if failed_metrics:
        category, evidence = classify(
            failed_metrics[0].metric, failed_metrics[0].reason or "", list(case_run.case_tags)
        )
    elif case_run.failure_semantics is not None:
        category, evidence = "infra.agent", case_run.failure_semantics.value
    tool_assertions: dict[str, list[str]] = {}
    if tools:
        tool_assertions["required"] = _unique(tools)
    suggested: dict[str, Any] = {}
    for metric in failed_metrics:
        if metric.metric in {"native.tool_sequence", "agent.tool_correctness"}:
            # 工具选择失败：把"该次调用过的可疑工具"建议为 forbidden，交由人工确认
            tool_assertions.setdefault("required", [])
            tool_assertions["forbidden"] = _candidates_from_reason(metric.reason, tools)
        elif metric.metric in {"native.output_checks", "agent.task_completion"}:
            suggested.setdefault("output", {})["not_contains"] = ["ERROR"]
        elif metric.metric == "native.status":
            suggested["status"] = "success"
    if tool_assertions:
        suggested["tools"] = tool_assertions

    return CaseDraft(
        id=draft_id or f"draft-{case_run.id}",
        case_id=case_id or f"promoted.{case_run.case_id}",
        case_run_id=case_run.id,
        run_id=meta.run_id,
        suite=suite,
        benchmark_id=benchmark_id or meta.benchmark_id,
        source_type=source_type,
        source_ref=source_ref,
        failure_category=category,
        classification_evidence=evidence,
        trace_reference=case_run.trace_path,
        tool_expectations=tool_assertions,
        suggested_assertions=suggested,
        inputs=_inputs_from(messages),
        environment=environment or {},
    )


def build_draft_from_production(
    trace_id: str,
    pair: EvalPair,
    *,
    evaluation_id: str | None = None,
    failure_reason: str = "",
    draft_id: str | None = None,
) -> CaseDraft:
    """P1-2：production 失败 -> Case Draft（source_type="production"）。

    与 run 侧 build_draft 的关键差异：online eval 是 reference-free 的——生产失败
    没有"标准答案"，所以 output 断言**留给人工补**，draft 只固化确定性事实：
    原始 input（可回归的前提）与观测到的工具序列（required 建议，可删）。
    suite 固定 regression：生产失败回流的第一站就是回归集。
    """
    tools = _unique(list(pair.tools_called))
    suggested: dict[str, Any] = {}
    tool_assertions: dict[str, list[str]] = {}
    if tools:
        tool_assertions["required"] = tools
        suggested["tools"] = tool_assertions
    prompt = pair.input or ""
    return CaseDraft(
        id=draft_id or f"draft-prod-{trace_id}-{pair.case_id}",
        case_id=f"promoted.{trace_id}.{pair.case_id}",
        # production trace 没有 run/iteration 概念：留空，出处以 run_id/source_ref 记
        case_run_id="",
        run_id=f"production:{trace_id}",
        suite="regression",
        source_type="production",
        source_ref=trace_id,
        failure_category=None,
        classification_evidence=failure_reason,
        trace_reference=f"production:{trace_id}",
        tool_expectations=tool_assertions,
        suggested_assertions=suggested,
        inputs={"type": "single_turn", "prompt": prompt},
        environment={},
    )


def _inputs_from(messages: list[str] | None) -> dict[str, Any]:
    if messages:
        if len(messages) == 1:
            return {"type": "single_turn", "prompt": messages[0]}
        return {"type": "multi_turn", "turns": [{"user": m} for m in messages]}
    # 待人工补齐：不编造 input，否则 Draft 无法用于回归
    return {"type": "single_turn", "prompt": ""}


def validate_assertions(assertion_payload: dict[str, Any]) -> list[str]:
    """Draft 的 suggested assertions 必须能被 Assertion Schema 解析（否则 Draft 不可用）。"""
    try:
        Assertion.model_validate(assertion_payload)
    except Exception as exc:  # noqa: BLE001 — 报错文本给人工看
        return [str(exc)]
    return []


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _candidates_from_reason(reason: str | None, tools: list[str]) -> list[str]:
    """从失败原因中挑出该次调用过的工具名，作为 forbidden 候选（需人工确认）。"""
    text = reason or ""
    return [tool for tool in tools if tool in text] or list(tools[:1])


def load_case_messages(evals_root: Path, dataset_id: str, case_id: str) -> list[str]:
    """从 evals 定义树读回原始 input（PRD §51 "原始 Input"）。"""
    from agent_eval.loading.loader import load_dataset

    _, cases = load_dataset(evals_root, dataset_id)
    case = next((c for c in cases if c.id == case_id), None)
    if case is None:
        raise InvalidCallError(f"case not found for promote: {case_id}")
    return case.input.messages()


def case_run_of(store: RunStore, run_id: str, case_run_id: str) -> CaseRunResult:
    _, results = store.load_run(run_id)
    for result in results:
        if result.id == case_run_id:
            return result
    raise InvalidCallError(f"case_run not found: {case_run_id} in run {run_id}")
