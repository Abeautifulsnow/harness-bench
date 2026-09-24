"""Trace Diff（PRD §56 十项 + §57 结构化参数 diff）。

比较对象是同 case 两侧 iteration 的 Raw Trace：工具序列按顺序对齐（LCS），
参数做递归结构化 diff，其余为标量对。
"""

from __future__ import annotations

from typing import Any

from agent_eval.models.regression import ArgumentDiff, TraceDiff, TraceDiffOp
from agent_eval.models.results import CaseRunResult

_MAX_ARG_DIFFS = 200  # 防病态 trace 把 diff 产物撑爆


def _lcs_ops(baseline: list[str], candidate: list[str]) -> list[TraceDiffOp]:
    """最长公共子序列对齐 → equal/added/removed 序列（顺序保持）。"""
    n, m = len(baseline), len(candidate)
    table = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            if baseline[i] == candidate[j]:
                table[i][j] = table[i + 1][j + 1] + 1
            else:
                table[i][j] = max(table[i + 1][j], table[i][j + 1])
    ops: list[TraceDiffOp] = []
    i = j = 0
    while i < n and j < m:
        if baseline[i] == candidate[j]:
            ops.append(TraceDiffOp(kind="equal", value=baseline[i], side="baseline"))
            i += 1
            j += 1
        elif table[i + 1][j] >= table[i][j + 1]:
            ops.append(TraceDiffOp(kind="removed", value=baseline[i], side="baseline"))
            i += 1
        else:
            ops.append(TraceDiffOp(kind="added", value=candidate[j], side="candidate"))
            j += 1
    ops.extend(TraceDiffOp(kind="removed", value=v, side="baseline") for v in baseline[i:])
    ops.extend(TraceDiffOp(kind="added", value=v, side="candidate") for v in candidate[j:])
    return ops


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """嵌套 dict/list → 扁平路径映射，供逐路径比对（PRD §57 structural diff）。"""
    flat: dict[str, Any] = {}
    if isinstance(value, dict):
        for key in sorted(value):
            flat.update(_flatten(value[key], f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            flat.update(_flatten(item, f"{prefix}[{index}]"))
    else:
        flat[prefix] = value
    return flat


def _argument_diffs(baseline: CaseRunResult, candidate: CaseRunResult) -> list[ArgumentDiff]:
    """按工具出现次序配对同名调用，比对参数路径（只报差异路径）。"""
    diffs: list[ArgumentDiff] = []
    by_tool_base: dict[str, list[dict]] = {}
    for call in baseline.tool_calls:
        by_tool_base.setdefault(call.name, []).append(call.arguments)
    seen: dict[str, int] = {}
    for call in candidate.tool_calls:
        index = seen.get(call.name, 0)
        seen[call.name] = index + 1
        candidates = by_tool_base.get(call.name, [])
        if index >= len(candidates):
            diffs.append(ArgumentDiff(tool=call.name, path="", change="added"))
            continue
        base_flat = _flatten(candidates[index])
        cand_flat = _flatten(call.arguments)
        for path in sorted(set(base_flat) | set(cand_flat)):
            if path not in base_flat:
                diffs.append(
                    ArgumentDiff(
                        tool=call.name, path=path, candidate=cand_flat[path], change="added"
                    )
                )
            elif path not in cand_flat:
                diffs.append(
                    ArgumentDiff(
                        tool=call.name, path=path, baseline=base_flat[path], change="removed"
                    )
                )
            elif base_flat[path] != cand_flat[path]:
                diffs.append(
                    ArgumentDiff(
                        tool=call.name,
                        path=path,
                        baseline=base_flat[path],
                        candidate=cand_flat[path],
                        change="changed",
                    )
                )
            if len(diffs) >= _MAX_ARG_DIFFS:
                return diffs
    return diffs


def diff_case_runs(
    baseline: CaseRunResult,
    candidate: CaseRunResult,
    *,
    baseline_spans: list | None = None,
    candidate_spans: list | None = None,
) -> TraceDiff:
    """One case-level trace diff. ``*_spans`` 可选（提供时统计 llm/subagent/error/retry）。"""
    base_tools = [t.name for t in baseline.tool_calls]
    cand_tools = [t.name for t in candidate.tool_calls]
    ops = _lcs_ops(base_tools, cand_tools)
    base_spans = baseline_spans or []
    cand_spans = candidate_spans or []

    def count(spans: list, span_type: str) -> int:
        return sum(1 for s in spans if getattr(s, "type", None) == span_type)

    def errors(spans: list) -> int:
        return sum(1 for s in spans if getattr(s, "status", None) == "error")

    def retries(spans: list) -> int:
        return sum(
            len(s.attributes.get("events", []))
            for s in spans
            if "retry" in (s.attributes.get("events") or [])
        )

    arg_diffs = _argument_diffs(baseline, candidate)
    diff = TraceDiff(
        case_id=candidate.case_id,
        baseline_iteration=baseline.iteration,
        candidate_iteration=candidate.iteration,
        tool_sequence=ops,
        added_tools=[op.value for op in ops if op.kind == "added"],
        removed_tools=[op.value for op in ops if op.kind == "removed"],
        argument_diffs=arg_diffs,
        model_calls=(count(base_spans, "llm"), count(cand_spans, "llm")),
        subagent_calls=(count(base_spans, "subagent"), count(cand_spans, "subagent")),
        errors=(errors(base_spans), errors(cand_spans)),
        retries=(retries(base_spans), retries(cand_spans)),
        tokens=(baseline.token_count, candidate.token_count),
        latency_ms=(baseline.latency_ms, candidate.latency_ms),
        final_answer_changed=(baseline.final_output or "") != (candidate.final_output or ""),
    )
    diff.changed = bool(
        diff.added_tools
        or diff.removed_tools
        or arg_diffs
        or diff.final_answer_changed
        or diff.model_calls[0] != diff.model_calls[1]
        or diff.subagent_calls[0] != diff.subagent_calls[1]
        or diff.errors[0] != diff.errors[1]
        or diff.retries[0] != diff.retries[1]
    )
    return diff
