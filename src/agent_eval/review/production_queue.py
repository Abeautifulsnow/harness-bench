"""Production 失败 → Review Queue 接线（P1-2：生产反馈闭环的第一段）。

语义裁决：
- **run 侧入队是纯建议不写库**（``queue_candidates``，PRD §61）；**production 侧入队
  是审计动作**：online eval 的 FAIL 是机器结论，人工复核才是判定，因此以
  ``verdict="" + queue_reason`` 落账为 pending 条目，人给结论后同键覆盖生效
  （ReviewStore 的 append-only + last-wins 语义）。
- **queue_reason 复用 "regression"**：PRD §61 的六类是 run 侧词汇，不为 production
  另开第七类——生产失败进 Review 的目的就是"成为回归候选"，词汇本义一致。
- **去重**：同 (production:<trace_id>, case_id) 已有 pending 条目时不重复入队；
  已有人工结论的条目同理（人工结论不覆盖）。
"""

from __future__ import annotations

from agent_eval.review.store import ReviewStore

PRODUCTION_RUN_PREFIX = "production:"


def production_run_key(trace_id: str) -> str:
    """ReviewStore 的 run 维度键空间里，production trace 的命名段。"""
    return f"{PRODUCTION_RUN_PREFIX}{trace_id}"


def queue_production_failures(
    review_store: ReviewStore,
    *,
    trace_id: str,
    evaluation: dict,
    note_prefix: str = "online-eval fail",
) -> list[dict]:
    """把一次 Online Eval 的 FAIL 行入队（pending）；每个 case 至多一条；返回新建条目。"""

    run_key = production_run_key(trace_id)
    existing = review_store.effective(run_id=run_key)
    # 多 metric 同 case 全 FAIL 是一次失败，不是多次：按 case 聚合后再入队
    failed_metrics: dict[str, list[dict]] = {}
    for row in evaluation.get("rows") or []:
        if row.get("verdict") != "fail":
            continue
        failed_metrics.setdefault(str(row.get("case_id")), []).append(row)

    enqueued: list[dict] = []
    for case_id in sorted(failed_metrics):
        if (run_key, case_id) in existing:
            continue  # 已有 pending 或已有人工结论：前者不重复，后者不覆盖
        rows = failed_metrics[case_id]
        metrics_desc = ", ".join(f"{row.get('metric')}@{row.get('score')}" for row in rows)
        record = review_store.add(
            run_key,
            case_id,
            "",
            queue_reason="regression",
            note=(f"{note_prefix}: {metrics_desc} evaluation={evaluation.get('evaluation_id')}"),
            machine_verdict="FAIL",
        )
        enqueued.append(record)
    return enqueued
