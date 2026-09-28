"""Native Evaluator：确定性规则优先（PRD §34 + Spec §2.2 统一断言词汇表）。

输入是单次执行（iteration / 或单个 turn）的观测切片 EvalScope，
输出一组 MetricResult（组粒度：status / output / tools / performance）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from jsonschema import ValidationError
from jsonschema import validate as jsonschema_validate
from pydantic import ValidationError as PydanticValidationError

from agent_eval.errors import UnsupportedAssertionError
from agent_eval.fixtures.snapshot import EnvironmentSnapshot, escapes_relative, valid_table_name
from agent_eval.ids import new_id
from agent_eval.models.case import (
    DEFERRED_EXTENSION_KEYS,
    DEFERRED_REASONS,
    EXTENSION_KEYS,
    SANDBOX_DEPENDENT_KEYS,
    Assertion,
    DatabaseStateAssertion,
    ExitCodeAssertion,
    FileStateAssertion,
    SqlResultAssertion,
    StepEfficiencyAssertion,
    ToolArgumentMatcher,
    argument_path,
)
from agent_eval.models.results import MetricResultModel, ToolCallRecord
from agent_eval.regression.sql_diff import dialect_for

# Case 级扩展断言中本层已实现的部分。
# 未列出的词汇（无论是否在 Spec 词汇表内）一律 fail-fast，而不是静默接受后失效：
# 声明了却永不生效的断言与"永不失败的断言"都会污染 Gate 结论。
IMPLEMENTED_EXTENSIONS = {
    "status",
    "exit_code",
    "database_state",
    "file_state",
    "sql_result",
    "tool_arguments",
    "step_efficiency",
}
# Spec §2.2 词汇表内、但本层不实现的扩展。它被两个更具体的子集进一步细分
# （执行型 → SANDBOX_DEPENDENT_KEYS；已裁决不做 → DEFERRED_EXTENSION_KEYS，
# Spec §20.4），剩下来的理论上是空集——所以它的报错文案是"尚未实现"，
# 服务于"往词汇表里加了键却还没决定怎么处置"这一刻：那种键必须报错，
# 而不是被判成"不在词汇表内"（后者会误导人去改 Spec §2.2）。
KNOWN_EXTENSIONS = EXTENSION_KEYS - IMPLEMENTED_EXTENSIONS


class ObservationUnavailable(Exception):
    """观测面不足以判定（Spec §19.1）。

    这不是失败也不是通过：fixture 没提供数据库、命令没上报退出码、
    agent 从没调用过 SQL 工具——这些情况下"判 pass"是假信号，"判 fail"是冤枉。
    统一处置为 ``skipped``，并在 reason 里说明缺的是哪个观测面。
    """


@dataclass
class EvalScope:
    """一次被评测执行的观测切片。

    只放 P0 真正能观测到的量；没有来源的字段（cost、exit code）不设默认值占位，
    否则会变成"永远判定为 pass/fail"的假信号。

    ``mcp_calls`` / ``command_calls`` 与 ``tool_calls`` 并列：Spec §12.1 的安全规则
    分别消费 MCP 调用名与 command.* 事件的可执行名，它们不以 ToolCallRecord 形式
    出现（span type 分别是 mcp / command），漏掉会让对应规则恒 pass。

    ``environment`` 是执行结束后的环境事实（Spec §19）：``database_state`` /
    ``file_state`` 判定的是"环境变成了什么样"，不是 agent 说了什么。它由 fixture
    层提供，缺省 None = 观测不足（判 skipped，不判 pass）。
    """

    run_status: str  # success | error | timeout
    final_output: str | None
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    mcp_calls: list[ToolCallRecord] = field(default_factory=list)
    command_calls: list[ToolCallRecord] = field(default_factory=list)
    latency_ms: int = 0
    tokens: int = 0
    # PRD §59：无定价时 cost 为 None，不是 0.0。max_cost 因此只在 cost is not None
    # 时可判；缺定价的 run 判 skipped（而不是"0 <= max_cost → pass"）。
    cost: float | None = None
    environment: EnvironmentSnapshot | None = None
    # case.environment.database：SQL 语义比对的方言来源（Spec §20.3）。
    # 不硬编码 sqlite——换 fixture provider 时它必须跟着变。
    database: str | None = None


def _result(
    case_run_id: str,
    metric: str,
    verdict: str,
    reason: str,
    *,
    score: float | None = None,
    metadata: dict[str, Any] | None = None,
    blocking: bool = True,
) -> MetricResultModel:
    return MetricResultModel(
        id=new_id("mr"),
        case_run_id=case_run_id,
        metric=metric,
        evaluator="native",
        score=score,
        verdict=verdict,  # type: ignore[arg-type]
        blocking=blocking,
        reason=reason,
        metadata=metadata or {},
    )


def _check_output(assertion: Assertion, scope: EvalScope) -> list[str]:
    problems: list[str] = []
    output = assertion.output
    if output.is_empty():
        return problems
    text = scope.final_output or ""
    if output.exact is not None and text != output.exact:
        problems.append(f"exact mismatch: got {text[:120]!r}")
    for needle in output.contains:
        if needle not in text:
            problems.append(f"missing expected substring {needle!r}")
    for needle in output.not_contains:
        if needle in text:
            problems.append(f"forbidden substring {needle!r} present")
    if output.regex is not None and re.search(output.regex, text) is None:
        problems.append(f"regex {output.regex!r} did not match")
    if output.json_schema is not None:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            problems.append("output is not valid JSON (json_schema assertion)")
        else:
            try:
                jsonschema_validate(parsed, output.json_schema)
            except ValidationError as exc:
                problems.append(f"json_schema validation failed: {exc.message}")
    return problems


def _check_tools(assertion: Assertion, scope: EvalScope) -> list[str]:
    problems: list[str] = []
    tools = assertion.tools
    if tools.is_empty():
        return problems
    called = [t.name for t in scope.tool_calls]
    for required in tools.required:
        if required not in called:
            problems.append(f"required tool not called: {required}")
    for forbidden in tools.forbidden:
        if forbidden in called:
            problems.append(f"forbidden tool called: {forbidden}")
    return problems


def _check_constraints(assertion: Assertion, scope: EvalScope) -> list[str]:
    problems: list[str] = []
    limits = assertion.constraints
    if limits.is_empty():
        return problems
    if limits.max_cost is not None:
        # PRD §59：无定价时 cost 为 None。把 None 当 0 会让"成本降到零"这种假象
        # 通过 max_cost 断言，所以这里判 skipped 而不是 pass（Spec §19.1）。
        if scope.cost is None:
            raise ObservationUnavailable(
                "constraints.max_cost 无法评测：本次 run 无定价表，cost 未计算（PRD §59）"
            )
        if scope.cost > limits.max_cost:
            problems.append(f"cost {scope.cost} > max_cost {limits.max_cost}")
    if limits.max_tool_calls is not None and len(scope.tool_calls) > limits.max_tool_calls:
        problems.append(
            f"tool calls {len(scope.tool_calls)} > max_tool_calls {limits.max_tool_calls}"
        )
    if limits.max_latency_ms is not None and scope.latency_ms > limits.max_latency_ms:
        problems.append(f"latency {scope.latency_ms}ms > max_latency_ms {limits.max_latency_ms}")
    if limits.max_tokens is not None and scope.tokens > limits.max_tokens:
        problems.append(f"tokens {scope.tokens} > max_tokens {limits.max_tokens}")
    return problems


def _check_extensions(assertion: Assertion, scope: EvalScope) -> list[str]:
    problems: list[str] = []
    for key in assertion.extensions:
        if key not in IMPLEMENTED_EXTENSIONS:
            raise UnsupportedAssertionError(
                f"assertion extension '{key}' is outside the implemented surface"
            )
    if "status" in assertion.extensions:
        expected_status = str(assertion.extensions["status"])
        if scope.run_status != expected_status:
            problems.append(f"run status {scope.run_status!r} != expected {expected_status!r}")
    return problems


def parse_database_state(assertion: Assertion) -> DatabaseStateAssertion | None:
    raw = assertion.extensions.get("database_state")
    if raw is None:
        return None
    try:
        return DatabaseStateAssertion.model_validate(raw)
    except Exception:
        return None


def _check_database_state(assertion: Assertion, scope: EvalScope) -> list[str]:
    """``database_state``（Spec §19.2）：判定执行结束后的 fixture 库，不是输出文本。"""
    spec = parse_database_state(assertion)
    if spec is None:
        raise UnsupportedAssertionError(
            "database_state must be a mapping {tables: {name: {...}}, tables_absent: [...]} "
            "(Spec §19.2)"
        )
    for name in [*spec.tables, *spec.tables_absent]:
        if not valid_table_name(name):
            raise UnsupportedAssertionError(f"database_state: 非法表名 {name!r}")
    if scope.environment is None:
        raise ObservationUnavailable("database_state 无法评测：本次执行没有可读的环境快照")
    problems: list[str] = []
    for name, expectation in spec.tables.items():
        state = scope.environment.table(name)
        if state is None:
            raise ObservationUnavailable(
                "database_state 无法评测：fixture 未提供数据库（仅 sqlite fixture 支持）"
            )
        if expectation.exists is True and not state.exists:
            problems.append(f"table '{name}' expected to exist but does not")
            continue
        if expectation.exists is False:
            if state.exists:
                problems.append(f"table '{name}' expected to be absent but exists")
            continue
        if not state.exists:
            problems.append(f"table '{name}' does not exist (row-count assertion)")
            continue
        rows = state.rows or 0
        suffix = "+" if state.truncated else ""
        if expectation.min_rows is not None and rows < expectation.min_rows:
            problems.append(f"table '{name}' rows {rows}{suffix} < min_rows {expectation.min_rows}")
        if expectation.max_rows is not None and rows > expectation.max_rows:
            problems.append(f"table '{name}' rows {rows}{suffix} > max_rows {expectation.max_rows}")
    for name in spec.tables_absent:
        state = scope.environment.table(name)
        if state is None:
            raise ObservationUnavailable(
                "database_state 无法评测：fixture 未提供数据库（仅 sqlite fixture 支持）"
            )
        if state.exists:
            problems.append(f"table '{name}' expected to be absent but exists")
    return problems


def parse_file_state(assertion: Assertion) -> FileStateAssertion | None:
    raw = assertion.extensions.get("file_state")
    if raw is None:
        return None
    try:
        return FileStateAssertion.model_validate(raw)
    except Exception:
        return None


def _check_file_state(assertion: Assertion, scope: EvalScope) -> list[str]:
    """``file_state``（Spec §19.3）：路径相对 fixture workdir。"""
    spec = parse_file_state(assertion)
    if spec is None:
        raise UnsupportedAssertionError(
            "file_state must be a mapping {files: {path: {...}}, absent: [...]} (Spec §19.3)"
        )
    if scope.environment is None:
        raise ObservationUnavailable("file_state 无法评测：本次执行没有工作目录快照")
    problems: list[str] = []
    for path in [*spec.files, *spec.absent]:
        if escapes_relative(path):
            raise UnsupportedAssertionError(
                f"file_state: 路径 {path!r} 越出 fixture workdir（声明非法，Spec §19.3）"
            )
    for path, expectation in spec.files.items():
        exists = scope.environment.file_exists(path)
        if exists is None:
            raise ObservationUnavailable("file_state 无法评测：fixture 未提供工作目录")
        if expectation.exists is False:
            if exists:
                problems.append(f"file '{path}' expected to be absent but exists")
            continue
        if not exists:
            problems.append(f"file '{path}' does not exist")
            continue
        if not expectation.contains and not expectation.not_contains:
            continue
        text = scope.environment.file_text(path)
        if text is None:
            raise ObservationUnavailable(f"file_state: file '{path}' 存在但无法读取")
        for needle in expectation.contains:
            if needle not in text:
                problems.append(f"file '{path}' missing expected substring {needle!r}")
        for needle in expectation.not_contains:
            if needle in text:
                problems.append(f"file '{path}' contains forbidden substring {needle!r}")
    for path in spec.absent:
        exists = scope.environment.file_exists(path)
        if exists is None:
            raise ObservationUnavailable("file_state 无法评测：fixture 未提供工作目录")
        if exists:
            problems.append(f"file '{path}' expected to be absent but exists")
    return problems


def parse_exit_code(assertion: Assertion) -> ExitCodeAssertion | None:
    raw = assertion.extensions.get("exit_code")
    if raw is None:
        return None
    try:
        if isinstance(raw, int):  # 简写：`exit_code: 0`
            return ExitCodeAssertion(expect=int(raw))
        return ExitCodeAssertion.model_validate(raw)
    except Exception:
        return None


def _check_exit_code(assertion: Assertion, scope: EvalScope) -> list[str]:
    """``exit_code``（Spec §19.4）：判定"被观测到的命令"的退出码。

    协议没有进程退出码这个概念（agent 走事件流），所以语义固定为命令退出码。
    ``command.*`` 事件未上报退出码时判 skipped——那是观测不足，不是"命令成功了"。

    部分观测（有的命令报了码、有的没报）按已报的判：没报码是协议缺口，不是
    agent 的行为问题，判 fail 会冤枉它；整轮判 skipped 又会丢掉已经拿到的观测。
    想要逐命令严格，就在声明里写上 ``command``——那时"命令未执行"本身即 skipped。
    """
    spec = parse_exit_code(assertion)
    if spec is None:
        raise UnsupportedAssertionError(
            "exit_code must be an int or a mapping {expect: int, command?: str} (Spec §19.4)"
        )
    calls = scope.command_calls
    if spec.command is not None:
        calls = [call for call in calls if call.name == spec.command]
        if not calls:
            # 指定命令一次都没执行：这条断言无从判定（不是"没执行就算过"）
            raise ObservationUnavailable(
                f"exit_code 无法评测：命令 '{spec.command}' 未被执行（无退出码可观测）"
            )
    reported = [call for call in calls if call.exit_code is not None]
    if not reported:
        raise ObservationUnavailable(
            "exit_code 无法评测：command.* 事件未上报退出码（协议未提供该字段）"
        )
    return [
        f"command '{call.name}' exit_code {call.exit_code} != expected {spec.expect}"
        for call in reported
        if call.exit_code != spec.expect
    ]


def parse_sql_result(assertion: Assertion) -> SqlResultAssertion | None:
    raw = assertion.extensions.get("sql_result")
    if raw is None:
        return None
    try:
        return SqlResultAssertion.model_validate(raw)
    except Exception:
        return None


def _sql_row_count(result: object) -> int | None:
    """从 SQL 工具返回里认出"行数"：行列表 / {rows: [...]} / {row_count: n}。"""
    if isinstance(result, list):
        return len(result)
    if isinstance(result, dict):
        for key in ("rows", "data", "records", "result"):
            value = result.get(key)
            if isinstance(value, list):
                return len(value)
        for key in ("row_count", "count", "rows_affected"):
            value = result.get(key)
            if isinstance(value, int):
                return value
    return None


def _sql_calls(scope: EvalScope) -> list[ToolCallRecord]:
    """认出 SQL 工具的调用：名字含 sql，或参数里带 sql/query 键。

    只用"有返回值"当判据会把任何返回 list 的工具都当成 SQL 工具，
    只用 name 会把 `db.query` 这类命名漏掉——两条判据取并集。
    """
    return [
        call
        for call in scope.tool_calls
        if call.result is not None
        and ("sql" in call.name.lower() or {"sql", "query"} & set(call.arguments))
    ]


def _check_sql_result(assertion: Assertion, scope: EvalScope) -> list[str]:
    """``sql_result``（Spec §19.5）：判定 SQL 工具的返回结果，不是输出文本。"""
    spec = parse_sql_result(assertion)
    if spec is None:
        raise UnsupportedAssertionError(
            "sql_result must be a mapping {min_rows?, max_rows?, contains?} (Spec §19.5)"
        )
    sql_calls = _sql_calls(scope)
    if not sql_calls:
        raise ObservationUnavailable("sql_result 无法评测：没有带返回结果的 SQL 工具调用可观测")
    problems: list[str] = []
    rows = [_sql_row_count(call.result) for call in sql_calls]
    known = [value for value in rows if value is not None]
    if spec.min_rows is not None or spec.max_rows is not None:
        if not known:
            raise ObservationUnavailable(
                "sql_result 无法评测：工具返回结果里认不出行数（形状未知）"
            )
        # 多语句取跨语句总和：单条语句的期望写在 case 里，聚合口径是 session
        total = sum(known)
        if spec.min_rows is not None and total < spec.min_rows:
            problems.append(f"sql result rows {total} < min_rows {spec.min_rows}")
        if spec.max_rows is not None and total > spec.max_rows:
            problems.append(f"sql result rows {total} > max_rows {spec.max_rows}")
    if spec.contains:
        serialized = [
            json.dumps(call.result, ensure_ascii=False, default=str) for call in sql_calls
        ]
        for needle in spec.contains:
            if not any(needle in text for text in serialized):
                problems.append(f"sql result missing expected substring {needle!r}")
    return problems


def parse_tool_arguments(assertion: Assertion) -> dict[str, dict[str, ToolArgumentMatcher]] | None:
    """解析 ``tool_arguments`` 扩展（Spec V2.2 §11.2）；形状非法 → None（由调用方判 error）。"""
    raw = assertion.extensions.get("tool_arguments")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return None
    parsed: dict[str, dict[str, ToolArgumentMatcher]] = {}
    for tool, paths in raw.items():
        if not isinstance(paths, dict):
            return None
        try:
            parsed[str(tool)] = {
                str(path): ToolArgumentMatcher.model_validate(matcher or {})
                for path, matcher in paths.items()
            }
        except Exception:
            return None
    return parsed


def tool_arguments_problem(assertion: Assertion) -> str:
    """非法形状的**具体**原因，供启动期与运行期共用（Spec §19.1 的可执行报错）。

    ``ToolArgumentMatcher`` 是 ``extra="forbid"`` 的封闭词表，最常见的失败是拼错的
    匹配器键（``semanticx``）。只说"形状非法"会让用户对着正确的路径反复排查，
    必须把 pydantic 拒绝的那个键名带出来。
    """
    raw = assertion.extensions.get("tool_arguments")
    if not isinstance(raw, dict):
        return "应为 {tool: {arg_path: matcher}}"
    for tool, paths in raw.items():
        if not isinstance(paths, dict):
            return f"tool '{tool}' 的参数表应为 {{arg_path: matcher}}"
        for path, matcher in paths.items():
            if not isinstance(matcher, dict):
                return f"tool '{tool}' 参数 '{path}' 的匹配器应为对象"
            try:
                ToolArgumentMatcher.model_validate(matcher)
            except PydanticValidationError as exc:
                first = exc.errors()[0]
                field = ".".join(str(part) for part in first.get("loc", ())) or "matcher"
                return f"tool '{tool}' 参数 '{path}' 的 '{field}': {first.get('msg')}"
    return "应为 {tool: {arg_path: matcher}}"


def _check_tool_arguments(assertion: Assertion, scope: EvalScope) -> list[str]:
    """逐 required tool 比对已声明参数子集（PRD §56/§57 的确定性版本）。

    ``sql_dialect`` 来自 case 的 ``environment.database``（Spec §20.3）：声明了
    ``semantic: true`` 的参数值要按方言解析 SQL，方言不能硬编码——fixture 换
    provider 时那是唯一需要跟着变的事实。
    """
    parsed = parse_tool_arguments(assertion)
    if parsed is None:
        raise UnsupportedAssertionError(
            f"tool_arguments 形状非法：{tool_arguments_problem(assertion)}（Spec V2.2 §11.2）"
        )
    dialect = dialect_for(scope.database)
    calls_by_tool: dict[str, list[ToolCallRecord]] = {}
    for call in scope.tool_calls:
        calls_by_tool.setdefault(call.name, []).append(call)
    problems: list[str] = []
    for tool, paths in parsed.items():
        calls = calls_by_tool.get(tool)
        if not calls:
            problems.append(f"tool_arguments declared for '{tool}' but it was never called")
            continue
        for path, matcher in paths.items():
            # Spec §11.2：**每个 occurrence 都要通过**，全部通过才 pass。
            # 早退（任一调用匹配即 pass）会让"同一工具调两次、其中一次参数错"
            # 判成通过——参数正确性的漏判方向，正是断言要拦的那一侧。
            for index, call in enumerate(calls, start=1):
                present, value = argument_path(call.arguments, path)
                if not present:
                    problems.append(
                        f"tool '{tool}' argument {path} (call #{index}): path '{path}' missing"
                    )
                    continue
                reason = matcher.check(value, sql_dialect=dialect)
                if reason is not None:
                    problems.append(f"tool '{tool}' argument {path} (call #{index}): {reason}")
    return problems


def argument_checks_ratio(assertion: Assertion, scope: EvalScope) -> float:
    """``native.argument_checks`` 的连续分：通过的检查数 / 检查总数（Spec §11.2）。

    检查粒度是 ``(tool, path, 该工具的每次调用)``：与 ``_check_tool_arguments``
    的 all-occurrence 判定同源——同一次调用错一个参数就扣一份，分数能反映
    "错了几处"，而不只是"有没有错"。
    """
    parsed = parse_tool_arguments(assertion)
    if parsed is None:
        return 0.0
    dialect = dialect_for(scope.database)
    calls_by_tool: dict[str, list[ToolCallRecord]] = {}
    for call in scope.tool_calls:
        calls_by_tool.setdefault(call.name, []).append(call)
    total = 0
    passed = 0
    for tool, paths in parsed.items():
        calls = calls_by_tool.get(tool) or [None]  # 未调用：整体记一次未通过
        for path, matcher in paths.items():
            for call in calls:
                total += 1
                if call is None:
                    continue
                present, value = argument_path(call.arguments, path)
                if present and matcher.check(value, sql_dialect=dialect) is None:
                    passed += 1
    return round(passed / total, 6) if total else 0.0


def parse_step_efficiency(assertion: Assertion) -> StepEfficiencyAssertion | None:
    """解析 ``step_efficiency`` 扩展；非法形状 → None（由调用方按 error 处理）。"""
    raw = assertion.extensions.get("step_efficiency")
    if raw is None:
        return None
    try:
        return StepEfficiencyAssertion.model_validate(raw)
    except Exception:
        return None


def _check_step_ratio(assertion: Assertion, scope: EvalScope) -> list[str]:
    """``native.step_ratio``（Spec V2.2 §11.1）：实际步数不得超过声明基线。"""
    spec = parse_step_efficiency(assertion)
    if spec is None:
        raise UnsupportedAssertionError(
            "step_efficiency must be a mapping {baseline_steps: int, max_ratio_delta?: float} "
            "(Spec V2.2 §11.1)"
        )
    actual = len(scope.tool_calls)
    limit = spec.limit()
    if actual <= limit:
        return []
    return [
        f"step ratio {spec.baseline_steps}/{actual} = "
        f"{step_ratio(assertion, scope):.3f} < 1.0（基线 {spec.baseline_steps}，上限 {limit:g}）"
    ]


def step_ratio(assertion: Assertion, scope: EvalScope) -> float:
    """步数效率得分：``min(1, baseline/actual)``（PRD §31 step efficiency 口径）。"""
    spec = parse_step_efficiency(assertion)
    baseline = spec.baseline_steps if spec is not None else 0
    actual = len(scope.tool_calls)
    if actual == 0:
        return 1.0 if baseline == 0 else 0.0
    return round(min(1.0, baseline / actual), 6)


def _needs_status_group(assertion: Assertion) -> bool:
    """``native.status`` 组的声明条件：声明了 ``status``，或出现了本层不实现的键。

    只在两者之一成立时产出。`database_state` 这类**已实现**的扩展有自己的组，
    让 native.status 陪跑只会多出一条永远 pass 的指标——那是假覆盖（Spec §19.1）。

    但词汇表外的键（未知键 / git_diff / 执行型键）必须落在这里报 error：
    `_GROUPS` 里它们没有别的组可归属，取消这条就会把"看不懂的声明"静默吞掉。
    """
    if "status" in assertion.extensions:
        return True
    return any(key not in IMPLEMENTED_EXTENSIONS for key in assertion.extensions)


def unsupported_declarations(assertion: Assertion, mount: str) -> list[str]:
    """列出该挂载点上无法评测的声明（启动期 fail-fast 的输入，Spec §2.2）。

    报错要可执行：不说"尚未实现"，而是说清**下一步动作**——
      1. 执行型键（pytest / build / lint）→ 依赖 PRD §88 沙箱，是设计选择不是漏做；
      2. 已裁决不做的键（git_diff）→ 指出替代手段（`file_state` 快照比对，Spec §20.4）；
      3. 形状非法（tool_arguments / step_efficiency / database_state / file_state /
         sql_result / exit_code）→ 给出正确形状，不静默接受后失效。
    """
    problems: list[str] = []
    for key in assertion.extensions:
        if key in IMPLEMENTED_EXTENSIONS:
            continue
        if key in SANDBOX_DEPENDENT_KEYS:
            problems.append(
                f"{mount}.{key}: 执行型断言，依赖 PRD §88 沙箱（V1 无隔离，不实现）——Spec §19.6"
            )
        elif key in DEFERRED_EXTENSION_KEYS:
            problems.append(f"{mount}.{key}: {DEFERRED_REASONS[key]}")
        elif key in KNOWN_EXTENSIONS:
            problems.append(f"{mount}.{key}: 尚未实现（观测面依赖其他任务，Spec §19.1）")
        else:
            problems.append(f"{mount}.{key}: 不在断言词汇表内（Spec §2.2）")
    if parse_tool_arguments(assertion) is None and "tool_arguments" in assertion.extensions:
        problems.append(f"{mount}.tool_arguments: 形状非法，{tool_arguments_problem(assertion)}")
    if parse_step_efficiency(assertion) is None and "step_efficiency" in assertion.extensions:
        problems.append(
            f"{mount}.step_efficiency: 形状非法，应为 "
            "{baseline_steps: int, max_ratio_delta?: float}"
        )
    if parse_database_state(assertion) is None and "database_state" in assertion.extensions:
        problems.append(
            f"{mount}.database_state: 形状非法，应为 "
            "{tables: {name: {min_rows?/max_rows?/exists?}}, tables_absent?: [name]}"
        )
    if parse_file_state(assertion) is None and "file_state" in assertion.extensions:
        problems.append(
            f"{mount}.file_state: 形状非法，应为 "
            "{files: {path: {exists?/contains?/not_contains?}}, absent?: [path]}"
        )
    if parse_exit_code(assertion) is None and "exit_code" in assertion.extensions:
        problems.append(f"{mount}.exit_code: 形状非法，应为 int 或 {{expect: int, command?: str}}")
    if parse_sql_result(assertion) is None and "sql_result" in assertion.extensions:
        problems.append(
            f"{mount}.sql_result: 形状非法，应为 "
            "{min_rows?: int, max_rows?: int, contains?: [str]}"
        )
    if "database_state" in assertion.extensions:
        spec = parse_database_state(assertion)
        if spec is not None:
            problems.extend(
                f"{mount}.database_state: 非法表名 {name!r}"
                for name in [*spec.tables, *spec.tables_absent]
                if not valid_table_name(name)
            )
    if "file_state" in assertion.extensions:
        spec = parse_file_state(assertion)
        if spec is not None:
            problems.extend(
                f"{mount}.file_state: 路径 {path!r} 越出 fixture workdir（Spec §19.3）"
                for path in [*spec.files, *spec.absent]
                if escapes_relative(path)
            )
    return problems


def synthesize_platform_verdicts(scope: EvalScope, case_run_id: str) -> list[MetricResultModel]:
    """平台级判定（非 Case 声明）：未正常结束的 run 不得判 PASS。

    放在评测层而不是编排层：这是评测策略，编排层只负责驱动。
    """
    if scope.run_status == "success":
        return []
    return [
        _result(
            case_run_id,
            "native.status",
            "fail",
            f"agent run finished with status={scope.run_status!r}",
            metadata={"platform": True},
        )
    ]


def _declares(key: str, parser: Callable[[Assertion], Any]) -> Callable[[Assertion], bool]:
    """扩展键的声明条件：声明了该键，且**不是空块**。

    空块（`sql_result: {}`）与未声明同口径——`output: {}` / `tools: {}` 一直是
    这个处理方式：不产出 metric，于是也不会产出"永远 pass"的指标（Spec §19.1）。

    形状非法时仍要产出：那里必须报 error，不能与"没声明"合并
    （Spec §19.3 的"声明写错了"与"没声明"是两件事）。
    """

    def predicate(assertion: Assertion) -> bool:
        if key not in assertion.extensions:
            return False
        spec = parser(assertion)
        if spec is None:
            return True  # 交给 checker 报 error
        return not spec.is_empty()

    return predicate


_GROUPS = (
    ("native.status", _needs_status_group, _check_extensions),
    ("native.output_checks", lambda a: not a.output.is_empty(), _check_output),
    ("native.tool_sequence", lambda a: not a.tools.is_empty(), _check_tools),
    ("native.argument_checks", lambda a: "tool_arguments" in a.extensions, _check_tool_arguments),
    ("native.performance", lambda a: not a.constraints.is_empty(), _check_constraints),
    ("native.step_ratio", lambda a: "step_efficiency" in a.extensions, _check_step_ratio),
    ("native.sql_result", _declares("sql_result", parse_sql_result), _check_sql_result),
    (
        "native.database_state",
        _declares("database_state", parse_database_state),
        _check_database_state,
    ),
    ("native.file_state", _declares("file_state", parse_file_state), _check_file_state),
    # exit_code 没有"空块"形态：`exit_code: {}` 就是 expect=0（缺省全部命令以 0 结束），
    # 是一条真断言，不该被当成空块丢掉。
    ("native.exit_code", lambda a: "exit_code" in a.extensions, _check_exit_code),
)


def evaluate_assertions(
    assertion: Assertion,
    scope: EvalScope,
    case_run_id: str,
    *,
    turn_index: int | None = None,
    mount: str | None = None,
) -> list[MetricResultModel]:
    """All checks inside a mount point combine with AND (Spec §2.2).

    每个声明了检查的组产出一条 MetricResult（组内全过 → pass）；
    未声明任何检查的组不产出，避免空断言刷分。

    观测不足（``ObservationUnavailable``）判 ``skipped`` 且 blocking=False：
    它既不是通过也不是失败，进不了 ``blocking_failed``——把"看不到"当成
    "没问题"会让 Gate 在该拦的时候放行（Spec §19.1）。
    """
    results: list[MetricResultModel] = []
    extra_meta: dict[str, Any] = {}
    if turn_index is not None:
        extra_meta["turn"] = turn_index
    if mount is not None:
        extra_meta["mount"] = mount
    for metric_id, declared, checker in _GROUPS:
        try:
            if not declared(assertion):
                continue
            problems = checker(assertion, scope)
        except ObservationUnavailable as exc:
            results.append(
                _result(
                    case_run_id,
                    metric_id,
                    "skipped",
                    str(exc),
                    metadata={**extra_meta, "skipped_reason": "observation_unavailable"},
                    blocking=False,
                )
            )
            continue
        except UnsupportedAssertionError as exc:
            results.append(
                _result(case_run_id, metric_id, "error", str(exc), metadata=dict(extra_meta))
            )
            continue
        verdict = "pass" if not problems else "fail"
        score = _group_score(metric_id, assertion, scope, not problems)
        results.append(
            _result(
                case_run_id,
                metric_id,
                verdict,
                "all checks passed" if not problems else "; ".join(problems),
                score=score,
                metadata=dict(extra_meta),
            )
        )
    return results


def _group_score(metric_id: str, assertion: Assertion, scope: EvalScope, passed: bool) -> float:
    """组得分。

    ``native.step_ratio`` 上报连续分（Spec §11.1，供降级与趋势使用）；
    ``native.argument_checks`` 上报"通过的检查数 / 检查总数"（Spec §11.2 明列）。
    其余组是"全过 / 没过"的二值语义，0/1。
    """
    if metric_id == "native.step_ratio":
        return step_ratio(assertion, scope)
    if metric_id == "native.argument_checks":
        return argument_checks_ratio(assertion, scope)
    return 1.0 if passed else 0.0
