/** PRD §25 的受控实验维度。与 `agent_eval/experiment/models.py` 的
 *  ``VARIANT_DIMENSIONS`` 保持同一份清单——UI 用它做维度提示，不做自由命名。 */
export const VARIANT_DIMENSIONS_HINT: Record<string, string> = {
  agent_version: "Agent 版本",
  model: "模型",
  prompt: "提示词模板",
  system_prompt: "系统提示词",
  planner_prompt: "规划提示词",
  tool_description: "工具描述",
  tool_policy: "工具使用策略",
  tool_set: "工具集合",
  skill_set: "技能集合",
  skill_version: "技能版本",
  mcp_set: "MCP 集合",
  memory_strategy: "记忆策略",
  context_strategy: "上下文策略",
  compression_strategy: "压缩策略",
  reasoning_effort: "推理强度",
  temperature: "采样温度",
};

export const VARIANT_DIMENSIONS = Object.keys(VARIANT_DIMENSIONS_HINT);
