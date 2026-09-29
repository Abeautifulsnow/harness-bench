"""Baseline Policy 的解析算法（Spec §4.2）：main-latest 的候选集必须限定在 main 分支。

回归背景：候选集只按 benchmark/status/dataset_version/gate 过滤，不看 `git_branch`，
于是一个在 feature 分支上恰好 Gate PASS 的 run 会被选成 PR Gate 的 baseline——
Spec 原文警告的"最近一次可能本身已带 Regression"的宽松变体。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_eval.models.regression import BaselineMode
from agent_eval.models.run import RunMetadata, RunStatus
from agent_eval.storage.baseline_store import BaselineStore
from agent_eval.storage.run_store import RunStore


def _meta(run_id: str, **overrides) -> RunMetadata:
    payload = {
        "run_id": run_id,
        "benchmark_id": "b1",
        "dataset_id": "d1",
        "dataset_version": "v1",
        "dataset_hash": "h1",
        "profile": "mock",
        "status": RunStatus.completed,
    }
    payload.update(overrides)
    return RunMetadata(**payload)


def _seed_run(runs_root: Path, meta: RunMetadata, *, gate_verdict: str = "pass") -> None:
    store = RunStore(runs_root)
    store.create_run(meta)
    store.save_meta(meta)
    (runs_root / meta.run_id / "gate.json").write_text(
        json.dumps({"verdict": gate_verdict}), encoding="utf-8"
    )


def _store(tmp_path: Path) -> tuple[BaselineStore, Path]:
    runs_root = tmp_path / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    return BaselineStore(state_root, runs_root), runs_root


class TestMainLatestBranchFilter:
    def test_feature_branch_run_is_not_a_candidate(self, tmp_path: Path) -> None:
        store, runs_root = _store(tmp_path)
        _seed_run(runs_root, _meta("run_feature", git_branch="feature/x"))

        assert store.resolve("b1", "v1", BaselineMode.main_latest) is None

    def test_main_branch_run_wins_over_newer_feature_run(self, tmp_path: Path) -> None:
        store, runs_root = _store(tmp_path)
        now = datetime.now(UTC)
        _seed_run(
            runs_root,
            _meta("run_main", git_branch="main", started_at=now - timedelta(hours=2)),
        )
        # 更新的 feature 分支 run：不得抢走 baseline
        _seed_run(runs_root, _meta("run_feature", git_branch="feature/x", started_at=now))

        resolved = store.resolve("b1", "v1", BaselineMode.main_latest)
        assert resolved is not None
        assert resolved.pinned_run_id == "run_main"

    def test_unknown_branch_is_not_treated_as_main(self, tmp_path: Path) -> None:
        """detached HEAD 记录为 None：宁可退化 NO_BASELINE，也不拿来源不明的 run 当基准。"""
        store, runs_root = _store(tmp_path)
        _seed_run(runs_root, _meta("run_detached", git_branch=None))

        assert store.resolve("b1", "v1", BaselineMode.main_latest) is None

    def test_gate_failed_main_run_is_not_a_candidate(self, tmp_path: Path) -> None:
        store, runs_root = _store(tmp_path)
        _seed_run(runs_root, _meta("run_main", git_branch="main"), gate_verdict="fail")

        assert store.resolve("b1", "v1", BaselineMode.main_latest) is None

    def test_main_run_is_resolved(self, tmp_path: Path) -> None:
        store, runs_root = _store(tmp_path)
        _seed_run(runs_root, _meta("run_main", git_branch="main"))

        resolved = store.resolve("b1", "v1", BaselineMode.main_latest)
        assert resolved is not None and resolved.pinned_run_id == "run_main"


class TestMainLatestSuiteComposition:
    """ROADMAP「发现的 2」：main-latest 的候选必须与当前 run 的套件组成全等。

    run 级均值（tool_calls / tokens / latency）定义在"该 run 选中的 case 集合"上；
    `--suite smoke`（3 条）对 `--suite golden`（24 条）的历史 run 比回归，是拿两个
    不同总体的均值作差——判定结果是噪声。全等而不是"覆盖"：超集的均值同样不可比。
    """

    def test_mismatched_composition_is_not_a_candidate(self, tmp_path: Path) -> None:
        store, runs_root = _store(tmp_path)
        _seed_run(runs_root, _meta("run_golden", git_branch="main", suites_covered={"golden": 24}))

        resolved = store.resolve("b1", "v1", BaselineMode.main_latest, suites_covered={"smoke": 3})
        assert resolved is None

    def test_matching_composition_beats_newer_mismatched_run(self, tmp_path: Path) -> None:
        """宁可取更早但可比的 main run，也不取更新但集合不同的。"""
        store, runs_root = _store(tmp_path)
        now = datetime.now(UTC)
        _seed_run(
            runs_root,
            _meta(
                "run_smoke_old",
                git_branch="main",
                started_at=now - timedelta(hours=2),
                suites_covered={"smoke": 3},
            ),
        )
        _seed_run(
            runs_root,
            _meta(
                "run_golden_new",
                git_branch="main",
                started_at=now,
                suites_covered={"golden": 24},
            ),
        )

        resolved = store.resolve("b1", "v1", BaselineMode.main_latest, suites_covered={"smoke": 3})
        assert resolved is not None and resolved.pinned_run_id == "run_smoke_old"

    def test_old_runs_without_composition_still_match_each_other(self, tmp_path: Path) -> None:
        """空字典 = 未记录套件的旧 run：彼此之间仍可比，不被新约束误伤。"""
        store, runs_root = _store(tmp_path)
        _seed_run(runs_root, _meta("run_old", git_branch="main", suites_covered={}))

        resolved = store.resolve("b1", "v1", BaselineMode.main_latest, suites_covered={})
        assert resolved is not None and resolved.pinned_run_id == "run_old"

    def test_no_constraint_when_composition_unknown(self, tmp_path: Path) -> None:
        """诊断命令（baseline resolve）没有"当前 run"，不传即不做该约束。"""
        store, runs_root = _store(tmp_path)
        _seed_run(runs_root, _meta("run_golden", git_branch="main", suites_covered={"golden": 24}))

        resolved = store.resolve("b1", "v1", BaselineMode.main_latest)
        assert resolved is not None and resolved.pinned_run_id == "run_golden"
