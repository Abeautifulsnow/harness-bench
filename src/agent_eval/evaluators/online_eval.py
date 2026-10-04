"""Online Eval（§53 第二片：对摄取的生产 Trace 跑 judge 评测）。

**对齐语义（本片的设计决策，§61 有记录）**：参考无关（reference-free）——
评测对象是生产 trace 自身的 (input, actual_output) 对，judge 从 policy 取
判定标准；**不做**生产 Q/A 到 benchmark dataset 的伪映射。需要 dataset
对齐的 Trace Replay 仍是独立后续特性。

pair 来源（优先级递减，全部显式、绝不编造）：
  1. 摄取时显式给出的 input / expected_output / context（meta 落账）；
  2. 事件提取：``run.started.data.input`` / ``model.request`` 末条 user 文本；
     ``run.finished.data.output``；
  3. 都没有 → 该 case 记为 unextractable，**跳过并如实计数**，不构造空 pair。

执行复用 Runner 的同一条 judge 路径（DeepEvalCapabilityAdapter.evaluate），
每个 (case, metric) 独立收口：error / timeout 都落行，绝不中断整批。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from yaml import SafeLoader
from yaml import load as _yaml_load

from agent_eval.errors import InvalidCallError

DEFAULT_EVAL_TIMEOUT_SECONDS = 120.0


# ----------------------------------------------------------------- policy


class EvalPolicyMetric(BaseModel):
    id: str
    threshold: float | None = None


class EvalPolicy(BaseModel):
    """生产评测策略（Definition Plane 资产，`evals/eval-policies/*.yaml`）。"""

    id: str
    display_name: str
    description: str = ""
    judge_model: str | None = None
    timeout_seconds: float = DEFAULT_EVAL_TIMEOUT_SECONDS
    metrics: list[EvalPolicyMetric] = Field(default_factory=list)


def load_eval_policies(evals_root: Path) -> list[EvalPolicy]:
    directory = evals_root / "eval-policies"
    if not directory.is_dir():
        return []
    policies: list[EvalPolicy] = []
    for path in sorted(directory.glob("*.yaml")):
        with path.open("r", encoding="utf-8") as fh:
            raw = _yaml_load(fh, SafeLoader)
        try:
            policies.append(EvalPolicy.model_validate(raw))
        except Exception as exc:
            raise InvalidCallError(f"invalid eval policy '{path.name}': {exc}") from exc
    return policies


# ----------------------------------------------------------------- pairs


@dataclass
class EvalPair:
    case_id: str
    input: str | None
    actual_output: str | None
    expected_output: str | None = None
    context: list[str] = field(default_factory=list)
    tools_called: list[str] = field(default_factory=list)

    def to_trace(self) -> dict[str, Any]:
        """构造 DeepEvalCapabilityAdapter.evaluate 消费的 trace dict。"""

        return {
            "input": self.input,
            "actual_output": self.actual_output,
            "expected_output": self.expected_output,
            "context": self.context or None,
            "tools_called": [{"name": name} for name in self.tools_called] or None,
            "completion_time": None,
        }


def extract_pair(
    case_id: str,
    meta_entry: dict | None,
    events: list[dict],
) -> EvalPair | None:
    """从摄取 meta（显式字段）+ 事件流提取评测对；提取不出返回 None。"""

    meta_entry = meta_entry or {}

    def _explicit(key: str) -> Any:
        return meta_entry.get(key)

    user_input = _explicit("input") or _input_from_events(events)
    output = _explicit("actual_output") or _output_from_events(events)
    if not user_input or not output:
        # 观测不足 ≠ 编造：skip 并由上层如实计数。
        return None
    return EvalPair(
        case_id=case_id,
        input=str(user_input),
        actual_output=str(output),
        expected_output=(
            str(_explicit("expected_output")) if _explicit("expected_output") else None
        ),
        # review #I01：context 必须是字符串列表。str 原样单元素包裹（str 的
        # 迭代语义会把"背景说明"拆成单字，静默污染 judge 输入）；其余类型
        # 大声拒绝，不做启发式转换。
        context=_normalize_context(_explicit("context")),
        tools_called=_tools_from_events(events),
    )


def _normalize_context(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        return list(raw)
    raise InvalidCallError(
        f"context must be a string or a list of strings, got {type(raw).__name__}"
    )


def _input_from_events(events: list[dict]) -> str | None:
    for event in events:
        if event.get("type") == "run.started":
            value = (event.get("data") or {}).get("input")
            if value:
                return str(value)
    # model.request 的末条 user 文本兜底（多轮取最后一问，与 Runner 终局切片同语义）。
    # review #I02：跳过非 user 消息——SUT 记录完整对话时，自己的 assistant
    # 回复会排在后面，拿它当"用户问题"评是错误结论。
    for event in reversed(events):
        if event.get("type") == "model.request":
            messages = (event.get("data") or {}).get("messages")
            if isinstance(messages, list):
                for message in reversed(messages):
                    role = message.get("role") if isinstance(message, dict) else None
                    if role is not None and role != "user":
                        continue
                    text = message.get("content") if isinstance(message, dict) else message
                    if text:
                        return str(text)
    return None


def _output_from_events(events: list[dict]) -> str | None:
    for event in reversed(events):
        if event.get("type") == "run.finished":
            value = (event.get("data") or {}).get("output")
            if value:
                return str(value)
    return None


def _tools_from_events(events: list[dict]) -> list[str]:
    names: list[str] = []
    for event in events:
        if event.get("type") in {"tool.call", "mcp.call"}:
            data = event.get("data") or {}
            name = data.get("name") or data.get("tool")
            if name:
                names.append(str(name))
    return names


# ----------------------------------------------------------------- runner


async def run_online_evaluation(
    *,
    pairs: list[EvalPair],
    policy: EvalPolicy,
    adapter: Any,
    unextractable_cases: list[str],
) -> dict:
    """执行一次 Online Eval，返回可落盘的完整结果（含 rows 与 summary）。"""

    rows: list[dict] = []
    for pair in pairs:
        trace = pair.to_trace()
        for metric in policy.metrics:
            rows.append(await _evaluate_one(pair, metric, trace, policy, adapter))

    by_metric: dict[str, dict[str, Any]] = {}
    for row in rows:
        bucket = by_metric.setdefault(
            row["metric"], {"pass": 0, "fail": 0, "error": 0, "skipped": 0, "scores": []}
        )
        bucket[row["verdict"]] += 1
        if row["score"] is not None:
            bucket["scores"].append(row["score"])
    summary = {
        "policy": policy.id,
        "cases_total": len(pairs) + len(unextractable_cases),
        "cases_evaluated": len(pairs),
        "cases_unextractable": unextractable_cases,
        "metrics": {
            metric: {
                "pass": bucket["pass"],
                "fail": bucket["fail"],
                "error": bucket["error"],
                "skipped": bucket["skipped"],
                "mean_score": (
                    round(sum(bucket["scores"]) / len(bucket["scores"]), 4)
                    if bucket["scores"]
                    else None
                ),
            }
            for metric, bucket in sorted(by_metric.items())
        },
    }
    return {
        "evaluation_id": f"eval_{int(time.time() * 1000):08x}{uuid.uuid4().hex[:8]}",
        "rows": rows,
        "summary": summary,
    }


async def _evaluate_one(
    pair: EvalPair, metric: EvalPolicyMetric, trace: dict, policy: EvalPolicy, adapter: Any
) -> dict:
    base = {"case_id": pair.case_id, "metric": metric.id, "threshold": metric.threshold}
    if getattr(adapter, "version", lambda: None)() is None:
        # 缺 DeepEval SDK：如实 skipped（与 Runner 的能力探测同语义）。
        return {**base, "score": None, "verdict": "skipped", "reason": "deepeval unavailable"}
    try:
        score, reason = await asyncio.wait_for(
            adapter.evaluate(metric.id, metric.threshold, trace, model=policy.judge_model),
            timeout=policy.timeout_seconds,
        )
    except TimeoutError:
        return {**base, "score": None, "verdict": "error", "reason": "evaluation timeout"}
    except Exception as exc:  # noqa: BLE001 - 单格失败不中断整批
        return {
            **base,
            "score": None,
            "verdict": "error",
            "reason": f"{type(exc).__name__}: {exc}",
        }
    if score is None:
        return {**base, "score": None, "verdict": "error", "reason": reason or "no score"}
    verdict = "pass" if metric.threshold is None or score >= metric.threshold else "fail"
    return {**base, "score": score, "verdict": verdict, "reason": reason}
