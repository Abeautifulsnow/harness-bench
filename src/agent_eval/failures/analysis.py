"""Failure Classification & Clustering（PRD §47–§50，§107 验收）。

分类：规则优先（PRD §48）；未命中且提供 LLMClassifier 时才调用 LLM。
聚类：按 (category, 归一化工具序列, 归一化错误签名) 分组，产出每个 Cluster 的
代表 Case / 规模 / 共同工具序列 / 共同错误（PRD §49/§50）。
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Protocol

from agent_eval.failures.taxonomy import CATEGORIES, classify, parent_of
from agent_eval.models.results import CaseRunResult, CaseStatus
from agent_eval.models.run import RunMetadata

_NUMBERS = re.compile(r"\d+")
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
_WS = re.compile(r"\s+")


class LLMClassifier(Protocol):
    """PRD §48 的 LLM Failure Classifier（无法规则判定时使用）。"""

    def classify(self, category_l1: str, reason: str, evidence: str) -> tuple[str, str]: ...


@dataclass
class Cluster:
    """PRD §50 Failure Cluster Detail。"""

    cluster_id: str
    label: str
    category: str
    parent: str
    size: int = 0
    representative_case_id: str = ""
    representative_case_run_id: str = ""
    common_tool_sequence: str = ""
    common_error: str = ""
    case_ids: list[str] = field(default_factory=list)
    affected_versions: list[int] = field(default_factory=list)
    first_seen: str | None = None
    latest_seen: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "cluster_id": self.cluster_id,
            "label": self.label,
            "category": self.category,
            "parent": self.parent,
            "size": self.size,
            "representative_case_id": self.representative_case_id,
            "representative_case_run_id": self.representative_case_run_id,
            "common_tool_sequence": self.common_tool_sequence,
            "common_error": self.common_error,
            "case_ids": self.case_ids,
            "affected_versions": self.affected_versions,
            "first_seen": self.first_seen,
            "latest_seen": self.latest_seen,
        }


@dataclass
class AnalysisResult:
    failures: list[dict[str, Any]]
    clusters: list[Cluster]

    @property
    def by_category(self) -> dict[str, int]:
        return dict(Counter(f["category"] for f in self.failures).most_common())

    @property
    def by_parent(self) -> dict[str, int]:
        return dict(Counter(f["parent"] for f in self.failures).most_common())

    @property
    def by_tool(self) -> dict[str, int]:
        counter: Counter[str] = Counter()
        for failure in self.failures:
            for tool in failure.get("tools", []):
                counter[tool] += 1
        return dict(counter.most_common())


def _normalize(text: str) -> str:
    """错误签名归一化：去掉字面量与空白，便于把同类错误聚到一起。"""
    text = _QUOTED.sub("<v>", text or "")
    text = _NUMBERS.sub("<n>", text)
    return _WS.sub(" ", text).strip()[:200]


def _tool_sequence(result: CaseRunResult) -> list[str]:
    return [tool.name for tool in result.tool_calls]


def signature(tools: list[str], error: str) -> str:
    """聚类键的工具/错误签名（PRD §49 输入：Tool Sequence + Error Message）。"""
    seq = ">".join(tools) if tools else "<no-tool>"
    digest = hashlib.sha256(f"{seq}|{_normalize(error)}".encode()).hexdigest()[:10]
    return f"{seq}|{digest}"


def classify_failure(
    result: CaseRunResult,
    metric: str,
    reason: str,
    *,
    llm: LLMClassifier | None = None,
) -> tuple[str, str, str]:
    """返回 (二级分类, 依据, source)。安全类断言不得被 LLM 覆盖（PRD §110-10）。"""
    category, why = classify(metric, reason, list(result.case_tags))
    source = "rule"
    parent = parent_of(category)
    if (
        llm is not None
        and parent not in {"SECURITY"}
        and why.endswith("兜底）")
        and category not in CATEGORIES
    ):
        try:
            category, llm_why = llm.classify(parent, reason, result.error or "")
            why = f"LLM classifier: {llm_why}"
            source = "llm"
        except Exception:  # noqa: BLE001 — LLM 不可用不影响规则结论
            pass
    return category, why, source


def analyse_run(
    meta: RunMetadata,
    results: list[CaseRunResult],
    *,
    llm: LLMClassifier | None = None,
) -> AnalysisResult:
    """PRD §107：一个 Run 的全部 Failure → 分类 + 聚类 + 代表 Case。"""
    failures: list[dict[str, Any]] = []
    for result in results:
        if result.status == CaseStatus.PASS:
            continue
        tools = _tool_sequence(result)
        reason = result.error or ""
        blocking = [
            m for m in result.all_metric_results if m.blocking and m.verdict in {"fail", "error"}
        ]
        if not blocking and result.status == CaseStatus.ERROR:
            blocking = []  # 已由 failure_semantics 表达，下面用 error 兜底
        if blocking:
            for metric in blocking:
                category, why, source = classify_failure(
                    result, metric.metric, metric.reason or reason, llm=llm
                )
                failures.append(
                    {
                        "id": f"fl-{metric.id}",
                        "run_id": meta.run_id,
                        "case_run_id": result.id,
                        "case_id": result.case_id,
                        "case_version": result.case_version,
                        "iteration": result.iteration,
                        "metric_result_id": metric.id,
                        "metric": metric.metric,
                        "mount": metric.metadata.get("mount"),
                        "category": category,
                        "parent": parent_of(category),
                        "reason": metric.reason or reason,
                        "evidence": why,
                        "source": source,
                        "tool_sequence": tools,
                        "tools": tools,
                    }
                )
        elif result.status == CaseStatus.ERROR:
            semantics = (
                result.failure_semantics.value if result.failure_semantics else "INFRA_FAILURE"
            )
            category = {
                "INFRA_FAILURE": "infra.agent",
                "EVALUATION_FAILURE": "evaluator.error",
                "AGENT_FAILURE": "agent.incomplete",
            }.get(semantics, "agent.incomplete")
            failures.append(
                {
                    "id": f"fl-err-{result.id}",
                    "run_id": meta.run_id,
                    "case_run_id": result.id,
                    "case_id": result.case_id,
                    "case_version": result.case_version,
                    "iteration": result.iteration,
                    "metric_result_id": "",
                    "metric": None,
                    "mount": None,
                    "category": category,
                    "parent": parent_of(category),
                    "reason": reason or semantics,
                    "evidence": f"failure_semantics={semantics}",
                    "source": "rule",
                    "tool_sequence": tools,
                    "tools": tools,
                }
            )
    return AnalysisResult(failures=failures, clusters=_cluster(failures, meta))


def _cluster(failures: list[dict[str, Any]], meta: RunMetadata) -> list[Cluster]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for failure in failures:
        groups.setdefault(signature(failure["tool_sequence"], failure["reason"]), []).append(
            failure
        )
    clusters: list[Cluster] = [
        _build_cluster(key, items, meta, index)
        for index, (key, items) in enumerate(
            sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])), start=1
        )
    ]
    return clusters


def _build_cluster(key: str, items: list[dict[str, Any]], meta: RunMetadata, index: int) -> Cluster:
    first = items[0]
    tools = first["tool_sequence"]
    case_ids = sorted({item["case_id"] for item in items})
    versions = sorted({int(item.get("case_version") or 1) for item in items})
    common_tool_sequence = " → ".join(tools) if tools else "(no tool call)"
    cluster = Cluster(
        cluster_id=f"cl-{index:02d}-{hashlib.sha256(key.encode()).hexdigest()[:6]}",
        label=f"{first['category']} · {len(case_ids)} cases",
        category=first["category"],
        parent=first["parent"],
        size=len(case_ids),
        representative_case_id=case_ids[0],
        representative_case_run_id=first["case_run_id"],
        common_tool_sequence=common_tool_sequence,
        common_error=_normalize(first["reason"]),
        case_ids=case_ids,
        affected_versions=versions,
        first_seen=str(meta.started_at.date()) if meta.started_at else None,
        latest_seen=str(meta.finished_at.date()) if meta.finished_at else None,
    )
    return cluster
