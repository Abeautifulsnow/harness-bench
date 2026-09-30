"""C 类评测集（ai-chatbot 专用）的结构护栏 —— change-plan §3 的硬约束逐条落地。

存在理由：C 类最容易犯的错**都不是"跑不通"，而是"跑通了但断言是假的"**。三类
典型在报告上与"agent 合规"长得一模一样：

1. **工具名拼错**：`tools.required` 恒 FAIL（看起来像 agent 坏了）、
   `tools.forbidden` 恒 pass（看起来像一切正常）。工具名不是自由文本，是外部
   契约，因此对着一份**冻结的权威名单**逐个核对（`tool-surface.yaml`，
   见该文件的"关于权威"一节）。
2. **观测面不存在却声明了断言**：本 SUT 的 SQL 走 bash 子进程，
   `_sql_calls` 只看工具名与参数键 → `sql_result` 永远认不出 SQL 调用。
   这类声明在报告里是 skipped（比假绿好），但它占着"已覆盖"的名分。
3. **负向用例不红**：一条恒 pass 的负向 case 与"断言写对了、agent 也合规"不可
   区分。因此每个维度都要有一条**真断言**的负向成员，且 `golden` 必须是正向
   （否则 release 档的 `required_pass_rate: 1.0` 永远不满足）。

护栏之外还有一件事本文件刻意**不**做：它不检查 case 的断言"写得对不对"——
那需要真机跑（`benchmark run ai-chatbot-core`）；本文件只保证"写歪了能被结构
抓住"，两者互补。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from agent_eval.evaluators import PLUGINS  # 导入即完成内置插件注册（PRD §109.4）
from agent_eval.evaluators.registry import plugin_for
from agent_eval.loading.loader import (
    load_benchmark,
    load_dataset,
    load_profile,
    load_suites,
    resolve_suites,
)
from agent_eval.models.case import Case

REPO = Path(__file__).resolve().parents[1]
EVALS = REPO / "evals"
SHIM = REPO / "shims" / "ai-chatbot"
DATASET = EVALS / "datasets" / "chatbot-core"
BENCHMARK = "ai-chatbot-core"

# C 类覆盖维度 → case 必须带的 tag（与 PRD §103 的六维不同名，因为被测方不同：
# 这里没有 MCP 维度——MCP server 由运行期配置决定，点名等于把断言绑到某台机器，
# 见 tool-surface.yaml 的 stability 说明）。
DIMENSIONS = ("tool-use", "database", "skill", "error-recovery", "context", "security")

# 超时下限（秒）。依据是本轮实测的墙钟分布（tmp/probe_c_cases.py + 预算校准）：
# 单工具往返 6~18s、SQLite 探索 14~36s、子代理 24s 起；再加上 shim 侧
# Semaphore(4) 的排队成本（实测每条被排队的请求多等 10~12s）。
# 15~40s 那档（database-core 的取值）在本链路上等于测超时，不是测行为。
MIN_TIMEOUT = 60.0

# 刻意**不**进默认 benchmark run 的 case，逐条登记理由（空 = 全部进默认 run）。
#
# 目前只有一条，成因是全数据集唯一的"安全指标 canary"：它在 `security.*` 上判红，
# 而 `security.max_failures: 0` 在每档 gate 里都是 Hard Gate。留在默认 run 里的
# 后果是那条硬门**永远红**，gate.json 上"安全规则被触发"与"这是一条故意撞线的
# canary"不可分辨——安全硬门最不能被解释掉。它由 `--suite security` 单独跑。
# 详见该 case 文件头。
EXCLUDED_FROM_BENCHMARK: dict[str, str] = {
    "chatbot.security.forbidden_path.negative": "安全硬门 canary，由 --suite security 单独执行",
}


def _dataset() -> tuple[object, list[Case]]:
    return load_dataset(EVALS, "chatbot-core")


def _cases() -> list[Case]:
    return _dataset()[1]


def _surface() -> dict[str, dict]:
    raw = yaml.safe_load((DATASET / "tool-surface.yaml").read_text(encoding="utf-8"))
    return raw["tools"]


def _declared_tool_names(case: Case) -> set[str]:
    """case 中逐字点名工具名的**全部**位置（required / forbidden / tool_arguments 键）。"""
    names: set[str] = set()
    for _mount, assertion in case.session_assertions():
        names.update(assertion.tools.required)
        names.update(assertion.tools.forbidden)
        names.update(assertion.security.forbidden_tools)
        raw = assertion.extensions.get("tool_arguments")
        if isinstance(raw, dict):
            names.update(str(tool) for tool in raw)
    for turn in case.input.turns or []:
        if turn.expect is None:
            continue
        names.update(turn.expect.tools.required)
        names.update(turn.expect.tools.forbidden)
        names.update(turn.expect.security.forbidden_tools)
        raw = turn.expect.extensions.get("tool_arguments")
        if isinstance(raw, dict):
            names.update(str(tool) for tool in raw)
    return names


class TestDatasetShape:
    def test_benchmark_points_at_this_dataset_and_a_real_suite(self) -> None:
        """change-plan §3.5：gate 声明的套件必须真有套件文件（YAML 装饰品拦不住）。"""
        benchmark = load_benchmark(EVALS, BENCHMARK)
        assert benchmark.dataset == "chatbot-core@1.0.0"
        suites = load_suites(EVALS)
        assert set(benchmark.suites) <= set(suites), (
            f"benchmark 声明了不存在的套件：{set(benchmark.suites) - set(suites)}"
        )
        _selected, counts = resolve_suites(benchmark, suites, _cases())
        assert all(count > 0 for count in counts.values()), counts

    def test_every_case_is_selected_by_some_suite(self) -> None:
        """漏 tag 的 case 不会被任何一次 run 执行——静默漏跑比跑红更危险。

        判定口径是"能被某次 run 选中"，不是"必须带 chatbot 标签"：本数据集有一条
        刻意留在默认 run **之外**的 case（见下面 EXCLUDED_FROM_BENCHMARK），它由
        `--suite security` 单独选中。例外逐条登记、且必须真的被 security 套件选中，
        因此"刻意排除"与"忘了挂标签"在测试里可分辨——这正是登记的用意。
        """
        benchmark = load_benchmark(EVALS, BENCHMARK)
        suites = load_suites(EVALS)
        cases = _cases()
        by_id = {case.id: case for case in cases}

        selected, counts = resolve_suites(benchmark, suites, cases)
        covered = {case.id for case in selected}
        for case_id in EXCLUDED_FROM_BENCHMARK:
            security_only = resolve_suites(benchmark, suites, cases, suite_filter=["security"])
            covered.update(case.id for case in security_only[0])
            assert case_id in {case.id for case in security_only[0]}, (
                f"{case_id} 既不在 benchmark 套件里、也不被 security 套件选中——"
                "登记了例外却没有真的跑到它，等于静默漏跑"
            )
        stray = sorted(set(by_id) - covered)
        assert not stray, f"没有任何套件会选中（静默漏跑）：{stray}"

    def test_excluded_cases_are_really_absent_from_the_benchmark(self) -> None:
        """登记的例外不得真的留在 benchmark 里（登记过期会掩盖一次真实排除）。"""
        benchmark = load_benchmark(EVALS, BENCHMARK)
        suites = load_suites(EVALS)
        selected, _counts = resolve_suites(benchmark, suites, _cases())
        present = {case.id for case in selected} & set(EXCLUDED_FROM_BENCHMARK)
        assert not present, f"{sorted(present)} 已回到默认 run，应从 EXCLUDED_FROM_BENCHMARK 移除"

    def test_case_ids_unique_and_namespaced(self) -> None:
        ids = [case.id for case in _cases()]
        assert len(ids) == len(set(ids))
        bad = [case_id for case_id in ids if not case_id.startswith("chatbot.")]
        assert not bad, f"case id 应与被测方同名空间（chatbot.*）：{bad}"

    def test_dimensions_covered_with_a_negative_each(self) -> None:
        """每维度至少一条负向，且负向必须在 core 之外可辨认（change-plan §3.3）。

        维度覆盖看**数据集**，不看默认 run：安全负向刻意留在 `--suite security` 上
        （见 EXCLUDED_FROM_BENCHMARK），它仍然是 security 维度的负向成员。
        """
        cases = _cases()
        thin = [
            dimension
            for dimension in DIMENSIONS
            if not any(dimension in case.tags and self._is_negative(case) for case in cases)
        ]
        assert not thin, f"维度缺负向 case：{thin}"

    def test_golden_holds_only_positives(self) -> None:
        """golden 里混进负向 → release 档 required_pass_rate: 1.0 永远不满足。"""
        offenders = [
            case.id for case in _cases() if "golden" in case.tags and self._is_negative(case)
        ]
        assert not offenders, f"golden 不得含负向 case：{offenders}"

    def test_every_case_declares_a_real_assertion(self) -> None:
        for case in _cases():
            mounts = case.session_assertions()
            turn_expects = [turn.expect for turn in case.input.turns or []]
            declared = any(
                assertion is not None and not assertion.is_empty() for _mount, assertion in mounts
            ) or any(expect is not None and not expect.is_empty() for expect in turn_expects)
            assert declared, f"{case.id} 没有任何可判定声明"

    def test_repeat_and_timeout_are_calibrated(self) -> None:
        """repeat ≥ 2（真实 LLM 的抖动用重复次数吸收）+ 超时下限（见 MIN_TIMEOUT）。"""
        for case in _cases():
            assert case.execution.repeat >= 2, f"{case.id} repeat={case.execution.repeat} < 2"
            assert case.execution.timeout >= MIN_TIMEOUT, (
                f"{case.id} timeout={case.execution.timeout} < {MIN_TIMEOUT}"
            )

    def test_case_files_are_lf(self) -> None:
        offenders = [
            path.name for path in (DATASET / "cases").glob("*.yaml") if b"\r\n" in path.read_bytes()
        ]
        assert not offenders, f"CRLF 行尾：{offenders}"

    @staticmethod
    def _is_negative(case: Case) -> bool:
        return "negative" in case.tags


class TestToolNamesMatchTheFrozenSurface:
    """change-plan §3.1：与 SUT 实际发出的 toolName 逐字一致。"""

    def test_every_named_tool_is_known_and_exact(self) -> None:
        surface = _surface()
        problems: list[str] = []
        for case in _cases():
            for name in sorted(_declared_tool_names(case)):
                entry = surface.get(name)
                if entry is None:
                    problems.append(f"{case.id}: 未知工具名 {name!r}（不在冻结名单里）")
                elif entry.get("stability") != "exact":
                    problems.append(
                        f"{case.id}: 工具名 {name!r} 的 stability={entry.get('stability')}"
                        "，禁止逐字点名（运行期拼装的名字会随机变红）"
                    )
        assert not problems, problems

    def test_dynamic_names_are_placeholders_in_the_surface_itself(self) -> None:
        """动态名字在名单里也只能以占位形态登记，不能出现具体实例名。"""
        dynamic = [
            name for name, entry in _surface().items() if entry.get("stability") == "dynamic"
        ]
        assert dynamic, "冻结名单必须登记动态命名族（MCP / connector）"
        for name in dynamic:
            assert "<" in name and ">" in name, f"动态族必须以占位符登记：{name!r}"

    def test_sql_result_is_not_declared(self) -> None:
        """本 SUT 的 SQL 走 bash 子进程，``sql_result`` 观测不到（native.py:409）。

        声明它不会假绿（实现侧判 skipped），但它会占着"已覆盖"的名分——
        共享一个"看不见的观测面"与"没测"在覆盖统计里必须可分辨。
        """
        offenders = [
            case.id
            for case in _cases()
            if any(
                "sql_result" in assertion.extensions
                for _mount, assertion in case.session_assertions()
            )
        ]
        assert not offenders, f"chatbot-core 不得声明 sql_result（观测面不存在）：{offenders}"


class TestProfileHandlesMissingObservationSurface:
    """change-plan §3.4：能力裁剪要**两条一起做**，缺一条就是假信号。"""

    # 两档 profile 都要过同一组检查：观测面缺口是 run 级事实，不是某一档的私事。
    # 之所以两档都要查，是因为 case 可以自由挑档（evaluation_profile），
    # 只查默认档等于"另一档漏配条目就没人发现"。
    PROFILES = ("chatbot-plain", "chatbot-strict")

    def _surface_declaration(self) -> dict[str, bool]:
        if str(SHIM) not in sys.path:
            sys.path.insert(0, str(SHIM))
        from shim_ai_chatbot import server as shim_server

        return dict(shim_server.OBSERVATION_SURFACE)

    @pytest.mark.parametrize("name", PROFILES)
    def test_unobservable_metrics_are_kept_and_declared_absent(self, name: str) -> None:
        """保留条目（不是删掉）+ 接入侧声明观测面不存在（不是静默）。"""
        declared = self._surface_declaration()
        profile = load_profile(EVALS, name)
        ids = {spec.id for spec in profile.metrics}
        # 本 SUT 提供不了的观测面：插件的 required_events 被能力表判死的那几个。
        unavailable = {
            metric_id: plugin
            for metric_id in ids
            if (plugin := plugin_for(metric_id)) is not None
            and any(declared.get(event) is False for event in plugin.required_events)
        }
        assert unavailable, (
            f"{name} 声称处理了观测面缺口，但没有任何 metric 命中缺口——"
            "要么声明变了，要么这条护栏已经失效"
        )
        for metric_id, plugin in unavailable.items():
            assert metric_id in ids, f"{metric_id} 被删掉了（应由能力声明留痕，不得删条目）"
            # 条目在、且没有偷偷把参数写成"能过"的样子：期望值不是问题所在，
            # 真正要的是"它会被判 skipped"，由 run_plugin 保证（单元测试覆盖）。
            assert plugin.required_events, f"{metric_id} 没有声明 required_events"

    @pytest.mark.parametrize("name", PROFILES)
    def test_every_unavailable_plugin_is_represented_in_the_profile(self, name: str) -> None:
        """反过来也要成立：能力表判死的插件不得在 profile 里缺席（静默省略）。"""
        declared = self._surface_declaration()
        profile = load_profile(EVALS, name)
        ids = {spec.id for spec in profile.metrics}
        quietly_dropped = [
            metric_id
            for metric_id, plugin in PLUGINS.items()
            if plugin.required_events
            and any(declared.get(event) is False for event in plugin.required_events)
            and metric_id not in ids
        ]
        assert not quietly_dropped, (
            f"观测面不存在的插件未在 {name} 里留痕（删条目 = 看不见的省略）：{quietly_dropped}"
        )

    @pytest.mark.parametrize("name", PROFILES)
    def test_profiles_declare_no_judge_metrics(self, name: str) -> None:
        """首版口径是确定性基线（change-plan §3 末段）：judge 指标一律不进。"""
        profile = load_profile(EVALS, name)
        judge = [
            spec.id
            for spec in profile.metrics
            if plugin_for(spec.id) is None and not spec.id.startswith("native.")
        ]
        assert not judge, f"{name} 不得含 judge/外部 provider 指标：{judge}"

    def test_both_profiles_run_the_same_metric_set(self) -> None:
        """两档只该差在 blocking 上——指标集合漂移会让某档悄悄少测几个维度。"""
        plain = load_profile(EVALS, "chatbot-plain")
        strict = load_profile(EVALS, "chatbot-strict")
        assert {spec.id for spec in plain.metrics} == {spec.id for spec in strict.metrics}

    def test_case_profiles_resolve_to_existing_files(self) -> None:
        """evaluation_profile 写错名字不会报错，只会让整条 case 静默换档。"""
        known = {spec for spec in ("chatbot-plain", "chatbot-strict")}
        stray = [
            (case.id, case.evaluation_profile)
            for case in _cases()
            if case.evaluation_profile is not None and case.evaluation_profile not in known
        ]
        assert not stray, f"case 引用了本数据集之外的 profile：{stray}"


class TestAssertionsCanActuallyFail:
    """`blocking` 归 Profile（Spec §17.2）→ "声明了却不算数"是一条独立的失败模式。

    首跑（run_d487591757ca）实测到它：`chatbot.skill.load.negative` 的
    harness.skill_load 判了 FAIL，但那条 metric 在 chatbot-plain 下 blocking=false，
    于是 case 状态是 **PASS**。一条恒绿的负向用例与"断言写对了、agent 也合规"
    在报告里不可区分——本仓库 §19.1 要拦的假绿，从"插件实现"搬到了"档位选择"。

    native.* 不在此列：它们的阻断权由平台固定（CaseStatus.FAIL 的判据就是
    `blocking_failed`），case 无法声明成不阻断。会被 case 声明的只有插件指标，
    因此只查 metric_params 指向的那些。
    """

    def _profile_of(self, case: Case) -> str:
        benchmark = load_benchmark(EVALS, BENCHMARK)
        return case.evaluation_profile or benchmark.default_profile

    def test_plugin_expectations_are_declared_in_a_blocking_profile(self) -> None:
        problems: list[str] = []
        for case in _cases():
            specs = {spec.id: spec for spec in load_profile(EVALS, self._profile_of(case)).metrics}
            for metric_id in case.metric_params:
                if plugin_for(metric_id) is None:
                    continue  # native.* 的阻断权不由 Profile 决定
                spec = specs.get(metric_id)
                if spec is None:
                    problems.append(
                        f"{case.id}: {metric_id} 不在 profile 里（另有 fail-fast 兜底）"
                    )
                elif not spec.blocking:
                    problems.append(
                        f"{case.id}: 声明了对 {metric_id} 的期望，但 "
                        f"profile '{self._profile_of(case)}' 里它 blocking=false"
                        "（判 FAIL 也不会让 case 变红）"
                    )
        assert not problems, problems


class TestExpectationsArePinnedByThePrompt:
    """期望值落在**模型可选参数**上时，必须由提示词钉死（第九轮实测回修）。

    起因：`chatbot.subagent.delegation` 在第二轮 C 类全量跑里 1/2 失败，
    failure_category=`harness.subagent_routing`。读 case_run 才看清真相：
    `subagent.started` 的 name 来自 `data-sub-open.agentType`，而它等于
    `agentType ?? 'auto'`（agent-tool.ts:335）——**模型填不填 `agentType` 参数**
    决定 name（实测 9 次采样：6 次 `auto`、3 次 `fullstack-engineer`）。首版提示词
    没钉死它，于是同一条 case 两次运行给出不同 name，一次被测成"路由错误"。

    这是"非确定性 SUT"里最难看见的一类假红：报告上是**被测对象的失败**，
    实际是**用例把模型的自由度写进了断言**。修法不是放宽断言（`allow_extra: true`
    只是让红变绿），而是让提示词把被断言的那个值钉成确定的。
    """

    def test_subagent_routing_does_not_expect_the_unspecified_sentinel(self) -> None:
        """`auto` 是"模型没说"的哨兵，不是被路由到的代理名。

        断言 `expected: [auto]` 等于声明"模型应该省略 agentType"——那是模型的自由
        选择，不是平台事实；平台事实只有"模型报了什么，`data-sub-open` 就照传什么"。
        """
        problems: list[str] = []
        for case in _cases():
            params = case.metric_params.get("harness.subagent_routing")
            if not params:
                continue
            expected = {str(name) for name in (params.get("expected") or [])}
            if "auto" in expected:
                problems.append(
                    f"{case.id}: expected 含平台哨兵 'auto'（= 模型未声明 agentType），"
                    "请改为在提示词里显式指定 agentType 并断言那个值"
                )
        assert not problems, problems

    def test_declared_agent_type_is_pinned_in_the_prompt(self) -> None:
        """反向可验：断言了某个 agentType，提示词里就必须真的要求用那个值。

        否则改提示词时很容易只改一半（提示词不再指定、expected 却留着），
        于是断言退化成"模型碰巧选了它"——与刚才修掉的缺陷同一个形态。
        """
        assert isinstance(_cases(), list)  # 显式：下面依赖真机语料存在
        problems: list[str] = []
        for case in _cases():
            params = case.metric_params.get("harness.subagent_routing")
            if not params:
                continue
            prompt = "\n".join(case.input.messages())
            for name in params.get("expected") or []:
                if str(name) == "auto":
                    continue  # 上一条测试负责拦它
                if str(name) not in prompt:
                    problems.append(
                        f"{case.id}: expected 含 {name!r}，但提示词里没出现它"
                        "（模型没有理由一定选它）"
                    )
        assert not problems, problems


class TestRedTeamTagsClassify:
    def test_red_team_cases_land_in_a_category(self) -> None:
        """红队 tag 必须能被归入 PRD §62 的攻击面，否则覆盖矩阵看不见它。"""
        from agent_eval.security.redteam import RED_TEAM_CATEGORIES, classify_red_team_case

        red = [case for case in _cases() if "red-team" in case.tags]
        assert red, "C 类安全用例应带 red-team 标签"
        unclassified = [case.id for case in red if classify_red_team_case(case) is None]
        assert not unclassified, f"红队 case 归类失败：{unclassified}"
        for case in red:
            assert classify_red_team_case(case) in RED_TEAM_CATEGORIES


@pytest.mark.parametrize("fixture", ["chatbot_workspace", "chatbot_ops"])
def test_fixtures_exist(fixture: str) -> None:
    """case 引用的 fixture 必须在 fixtures/ 下真的存在（否则整轮 infra error）。"""
    assert (REPO / "fixtures" / fixture).is_dir(), f"fixture 缺失：{fixture}"
