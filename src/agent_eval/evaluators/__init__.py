"""Evaluator 层入口：注册平台内置的 harness 插件（PRD §43/§44）。

任何 ``agent_eval.evaluators.*`` 的子模块导入都会先执行本包，因此内置插件
在任何使用路径下都已就位。第三方插件走同一条 ``register_plugin()`` 通道——
注册是新增 Evaluator 的唯一动作，Runner 不需要知道有哪些插件（PRD §109.4）。
"""

from agent_eval.evaluators.harness import HARNESS_EVALUATORS
from agent_eval.evaluators.plugin import EvaluationContext, EvaluatorPlugin
from agent_eval.evaluators.registry import PLUGINS, register_plugin

for _plugin in HARNESS_EVALUATORS:
    register_plugin(_plugin)

__all__ = [
    "PLUGINS",
    "EvaluationContext",
    "EvaluatorPlugin",
    "HARNESS_EVALUATORS",
    "register_plugin",
]
