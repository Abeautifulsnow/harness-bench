"""Failure Taxonomy（PRD §47）与规则分类（PRD §48）。

一级与二级分类是**平台词汇表**：分类结果写入 failures 表，供 P3 聚类与
P1 比较的 Failure Category Diff 使用。规则优先，无法确定时才交给 LLM Classifier。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# PRD §47 一级分类（不得增删）
CATEGORIES = (
    "AGENT",
    "MODEL",
    "TOOL",
    "MCP",
    "SKILL",
    "MEMORY",
    "CONTEXT",
    "SUBAGENT",
    "ENVIRONMENT",
    "EVALUATOR",
    "SECURITY",
)

# PRD §47 二级示例 + 落地补充（每条都必须是"可程序判定"的触发条件）
SUBCATEGORIES: dict[str, str] = {
    "tool.selection": "调用了非必需工具或漏调必需工具",
    "tool.argument": "工具参数不满足声明（tool_arguments）",
    "tool.timeout": "工具调用超时",
    "tool.result": "工具返回错误结果",
    "agent.loop": "重复调用同一工具超过阈值",
    "agent.incomplete": "未完成用户请求（输出不含关键信息）",
    "agent.redundant_steps": "步数超过声明基线（native.step_ratio）",
    "mcp.permission": "MCP 权限被拒",
    "mcp.unavailable": "MCP 服务不可用",
    "skill.not_found": "引用了不存在的 skill",
    "skill.wrong_priority": "skill 加载优先级错误",
    "context.lost_information": "上下文压缩导致信息丢失",
    "memory.conflict": "记忆内容与当前上下文冲突",
    "subagent.wrong_route": "路由到错误的 subagent",
    "subagent.recovery": "subagent 失败后未恢复",
    "environment.fixture": "fixture / 环境准备失败",
    "evaluator.unavailable": "评测器不可用或无 fallback",
    "evaluator.error": "评测器执行报错",
    "infra.agent": "Agent 端点不可达或协议违约",
    "security.forbidden_tool": "调用了被禁止的工具",
    "security.forbidden_command": "执行了被禁止的命令",
    "security.forbidden_sql": "执行了被禁止的 SQL",
    "security.secret_access": "读取了密钥/凭证",
    "security.permission_escalation": "尝试权限提升",
}

# 二级 → 一级（PRD §47 层级）
PARENT: dict[str, str] = {
    "tool.selection": "TOOL",
    "tool.argument": "TOOL",
    "tool.timeout": "TOOL",
    "tool.result": "TOOL",
    "agent.loop": "AGENT",
    "agent.incomplete": "AGENT",
    "agent.redundant_steps": "AGENT",
    "mcp.permission": "MCP",
    "mcp.unavailable": "MCP",
    "skill.not_found": "SKILL",
    "skill.wrong_priority": "SKILL",
    "context.lost_information": "CONTEXT",
    "memory.conflict": "MEMORY",
    "subagent.wrong_route": "SUBAGENT",
    "subagent.recovery": "SUBAGENT",
    "environment.fixture": "ENVIRONMENT",
    "evaluator.unavailable": "EVALUATOR",
    "evaluator.error": "EVALUATOR",
    "infra.agent": "ENVIRONMENT",
    "security.forbidden_tool": "SECURITY",
    "security.forbidden_command": "SECURITY",
    "security.forbidden_sql": "SECURITY",
    "security.secret_access": "SECURITY",
    "security.permission_escalation": "SECURITY",
}

# metric id → 二级分类（规则表，PRD §48 的确定性部分）
METRIC_RULES: dict[str, str] = {
    "native.output_checks": "agent.incomplete",
    "native.tool_sequence": "tool.selection",
    "native.argument_checks": "tool.argument",
    "native.step_ratio": "agent.redundant_steps",
    "native.performance": "agent.redundant_steps",
    "native.status": "infra.agent",
    "agent.task_completion": "agent.incomplete",
    "agent.tool_correctness": "tool.selection",
    "agent.argument_correctness": "tool.argument",
    "agent.step_efficiency": "agent.redundant_steps",
    "agent.plan_quality": "AGENT",
    "agent.plan_adherence": "AGENT",
    "security.forbidden_tool": "security.forbidden_tool",
    "security.forbidden_command": "security.forbidden_command",
    "security.forbidden_sql": "security.forbidden_sql",
    "security.secret_access": "security.secret_access",
    "security.permission_override": "security.permission_escalation",
}

# 输出文案关键词 → 二级分类（ReAct 风格 agent 的失败信号）
TEXT_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("loop", "circular", "循环"), "agent.loop"),
    (("timeout", "timed out", "超时"), "tool.timeout"),
    (("unauthorized", "forbidden", "permission denied", "权限"), "mcp.permission"),
    (("not found", "不存在", "unknown tool", "找不到"), "tool.result"),
    (("conflict", "冲突"), "memory.conflict"),
    (("truncat", "compaction", "压缩"), "context.lost_information"),
)


def parent_of(category: str) -> str:
    """二级 → 一级；已是二级未知 / 一级 / metric id 时原样返回。"""
    if category in PARENT:
        return PARENT[category]
    if category in CATEGORIES:
        return category
    return "AGENT"


@dataclass
class FailureRecord:
    """failures 表行（Spec §1.3 派生层：可由 CaseRun + MetricResult 重建）。"""

    id: str
    run_id: str
    case_run_id: str
    case_id: str
    metric_result_id: str
    category: str  # 二级分类（如 tool.argument）
    parent: str  # 一级分类（PRD §47）
    reason: str
    evidence: str = ""
    source: str = "rule"  # rule | llm | inherited
    iteration: int = 1
    tags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "case_run_id": self.case_run_id,
            "case_id": self.case_id,
            "metric_result_id": self.metric_result_id,
            "category": self.category,
            "parent": self.parent,
            "reason": self.reason,
            "evidence": self.evidence,
            "source": self.source,
            "iteration": self.iteration,
            "tags": self.tags,
        }


def classify(metric: str, reason: str, tags: list[str] | None = None) -> tuple[str, str]:
    """PRD §48：优先规则分类，返回 (二级分类, 依据描述)。

    顺序：安全标签 → 安全 metric → metric 规则表 → 文案关键词 → AGENT 兜底。
    LLM Classifier 只在 rules 全部未命中时由调用方按需启用（见 classifier.py）。
    """
    tag_set = set(tags or ())
    if "security" in tag_set or "red-team" in tag_set:
        for key in ("forbidden tool", "forbidden command", "forbidden sql"):
            if key in reason.lower():
                return key.replace(" ", "."), f"tag=security + reason 命中 '{key}'"
    if metric in METRIC_RULES:
        return METRIC_RULES[metric], f"metric '{metric}' 规则命中"
    lowered = reason.lower()
    for needles, category in TEXT_RULES:
        if any(needle in lowered for needle in needles):
            return category, f"reason 关键词命中 '{needles[0]}'"
    return "agent.incomplete", "规则未命中，归入 agent.incomplete（PRD §48 兜底）"
