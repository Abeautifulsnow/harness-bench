"""report.html 生成（Spec §6.2 / PRD §97）。

自包含单文件（内联 CSS，无外部资源）：CI artifacts 直接下载即可读，
不依赖网络、不向外部服务发起请求。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from agent_eval.models.regression import GateReport
from agent_eval.reports.aggregate import RunAggregate
from agent_eval.reports.report import build_report

TEMPLATE_DIR = Path(__file__).parent / "templates"


def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(enabled_extensions=("html", "j2"), default_for_string=True),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["tojson_pretty"] = lambda value: json.dumps(value, ensure_ascii=False, indent=2)
    env.filters["pct"] = lambda value: f"{float(value or 0) * 100:.1f}%"
    return env


def render_html(aggregate: RunAggregate, gate: GateReport) -> str:
    report = build_report(aggregate)
    template = _environment().get_template("report.html.j2")
    return template.render(
        report=report,
        gate=gate,
        comparison=report.get("regression"),
        state_colors=_STATE_COLORS,
    )


_STATE_COLORS: dict[str, str] = {
    "REGRESSION": "#b42318",
    "IMPROVED": "#027a48",
    "UNCHANGED": "#475467",
    "FLAKY": "#b54708",
    "UNDETERMINED": "#6941c6",
    "INVALID": "#344054",
    "PASSED": "#027a48",
    "FAILED": "#b42318",
    "STABLE_PASS": "#027a48",
    "STABLE_FAIL": "#b42318",
    "UNKNOWN": "#667085",
    "pass": "#027a48",
    "fail": "#b42318",
    "undetermined": "#6941c6",
}


def render_html_safe(aggregate: RunAggregate, gate: GateReport) -> str | None:
    """渲染失败不应让整份报告丢失：退回 None，调用方保留其余四个产物。"""
    try:
        return render_html(aggregate, gate)
    except Exception:
        return None


def summary_payload(aggregate: RunAggregate) -> dict[str, Any]:
    """供 REST API / Web UI 复用的轻量摘要。"""
    report = build_report(aggregate)
    return {
        "run_id": report["run_id"],
        "benchmark_id": report["benchmark_id"],
        "dataset": report["dataset"],
        "status": report["status"],
        "verdict": report["verdict"],
        "baseline_mode": report["baseline_mode"],
        "totals": report["totals"],
        "warnings": report["warnings"],
    }
