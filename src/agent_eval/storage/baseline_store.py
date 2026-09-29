"""Baseline Policy（Spec V2.1.1 §4）。

作者态数据（pin/unpin）落在 ``<state_root>/baselines.jsonl``（append-only，
最后一条同 (benchmark, dataset_version, mode) 记录为有效值）。
``main-latest`` 不落 pin，按 §4.2 解析算法实时计算并缓存解析结果。
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_eval.errors import InvalidCallError
from agent_eval.models.regression import Baseline, BaselineMode
from agent_eval.models.run import RunStatus
from agent_eval.storage.run_store import RunStore

# main-latest 候选集的分支判据（Spec §4.2）：main / master 的本地与远程写法。
_MAIN_BRANCHES = frozenset({"main", "master", "origin/main", "origin/master", "refs/heads/main"})


def _is_main_branch(branch: str | None) -> bool:
    if not branch:
        return False
    return branch.strip() in _MAIN_BRANCHES


class BaselineStore:
    def __init__(self, state_root: Path, runs_root: Path) -> None:
        self.state_root = state_root
        self.runs_root = runs_root
        self.path = state_root / "baselines.jsonl"
        self.store = RunStore(runs_root)

    # -------------------------------------------------------------- authoring

    def _append(self, baseline: Baseline) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(baseline.model_dump(mode="json"), ensure_ascii=False) + "\n")

    def _records(self) -> list[Baseline]:
        if not self.path.is_file():
            return []
        out: list[Baseline] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(Baseline.model_validate(json.loads(line)))
            except Exception:  # 撕裂写：跳过该行而不是整体失效
                continue
        return out

    def pin(
        self,
        run_id: str,
        *,
        benchmark_id: str | None = None,
        mode: BaselineMode = BaselineMode.explicit,
        pinned_by: str | None = None,
        note: str = "",
        gate_evidence_run_id: str | None = None,
    ) -> Baseline:
        """Spec §4.5: pin 要求目标 run 满足 completed、同 benchmark、Gate PASS。"""
        if mode == BaselineMode.main_latest:
            raise InvalidCallError("main-latest 由解析算法计算，不允许 pin（Spec §4.1）")
        meta, _ = self.store.load_run(run_id)  # 不存在 → InvalidCallError（exit 3）
        if meta.status != RunStatus.completed:
            raise InvalidCallError(
                f"cannot pin run '{run_id}': status={meta.status.value} (要求 completed)"
            )
        if benchmark_id is not None and meta.benchmark_id != benchmark_id:
            raise InvalidCallError(
                f"cannot pin run '{run_id}': benchmark={meta.benchmark_id} != {benchmark_id}"
            )
        if not self._gate_passed(run_id):
            raise InvalidCallError(
                f"cannot pin run '{run_id}': Gate 未 PASS"
                "（Spec §4.5 要求 pin 的目标 run Gate PASS）"
            )
        baseline = Baseline(
            id=f"bs-{run_id}",
            benchmark_id=meta.benchmark_id,
            dataset_version=meta.dataset_version,
            mode=mode,
            pinned_run_id=run_id,
            pinned_by=pinned_by,
            gate_evidence_run_id=gate_evidence_run_id or run_id,
            note=note,
        )
        self._append(baseline)
        return baseline

    def unpin(self, benchmark_id: str, mode: BaselineMode) -> int:
        """追加一条 tombstone（pinned_run_id=None）表示解除。"""
        removed = [b for b in self.effective() if b.benchmark_id == benchmark_id and b.mode == mode]
        if not removed:
            raise InvalidCallError(f"no {mode.value} baseline pinned for '{benchmark_id}'")
        for existing in removed:
            self._append(
                Baseline(
                    id=existing.id,
                    benchmark_id=existing.benchmark_id,
                    dataset_version=existing.dataset_version,
                    mode=existing.mode,
                    pinned_run_id=None,
                    pinned_by=None,
                    note="unpinned",
                )
            )
        return len(removed)

    def effective(self) -> list[Baseline]:
        """折叠 append-only 日志：同 key 最新一条生效。"""
        collapsed: dict[tuple[str, str, str], Baseline] = {}
        for record in self._records():
            collapsed[(record.benchmark_id, record.dataset_version, record.mode.value)] = record
        return [b for b in collapsed.values() if b.pinned_run_id]

    # --------------------------------------------------------------- resolving

    def show(self, benchmark_id: str) -> list[Baseline]:
        return [b for b in self.effective() if b.benchmark_id == benchmark_id]

    def resolve(
        self,
        benchmark_id: str,
        dataset_version: str | None,
        mode: BaselineMode,
        *,
        suites_covered: dict[str, int] | None = None,
    ) -> Baseline | None:
        """Spec §4.2 的解析算法；未命中返回 None（调用方降级 NO_BASELINE）。

        ``suites_covered`` 是当前 run 的套件组成：main-latest 的候选必须与它
        **全等**，否则 run 级均值在不同 case 集合之间比较，回归判定是噪声
        （ROADMAP「发现的 2」）。``baseline resolve`` 诊断命令没有"当前 run"，
        传 None 表示不做该约束。
        """
        if mode == BaselineMode.main_latest:
            return self._resolve_main_latest(benchmark_id, dataset_version, suites_covered)
        candidates = [
            b
            for b in self.effective()
            if b.benchmark_id == benchmark_id
            and b.mode == mode
            and (dataset_version is None or b.dataset_version == dataset_version)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda b: b.pinned_at)

    def _resolve_main_latest(
        self,
        benchmark_id: str,
        dataset_version: str | None,
        suites_covered: dict[str, int] | None = None,
    ) -> Baseline | None:
        """Spec §4.2：候选集是 **main 分支上的** runs，不是"任意分支最近一次"。

        少了分支过滤，feature 分支上恰好 Gate PASS 的 run 会被选成 PR Gate 的
        baseline —— Spec 原文警告的"最近一次可能本身已带 Regression"就是这个宽松
        变体。``git_branch`` 未知（None/空）按不匹配处理：宁可退化成 NO_BASELINE，
        也不拿来源不明的 run 当基准。

        ``suites_covered`` 给定时（当前 run 的套件组成），候选必须与它全等：
        run 级均值定义在"该 run 选中的 case 集合"上，`--suite smoke` 对
        `--suite golden` 的历史 run 比回归是拿两个不同总体的均值作差。全等而不是
        "覆盖"——超集的均值同样不可比。旧 run（空字典）只与同为空字典的候选匹配。
        """
        candidates = []
        for meta in self.store.list_runs():
            if meta.benchmark_id != benchmark_id:
                continue
            if meta.status != RunStatus.completed:
                continue
            if not _is_main_branch(meta.git_branch):
                continue
            if dataset_version is not None and meta.dataset_version != dataset_version:
                continue
            if suites_covered is not None and meta.suites_covered != suites_covered:
                continue
            if not self._gate_passed(meta.run_id):
                continue
            candidates.append(meta)
        if not candidates:
            return None
        latest = max(candidates, key=lambda m: m.started_at)
        return Baseline(
            id=f"bs-{latest.run_id}",
            benchmark_id=benchmark_id,
            dataset_version=latest.dataset_version,
            mode=BaselineMode.main_latest,
            pinned_run_id=latest.run_id,
            note="resolved by §4.2 algorithm",
        )

    def _gate_passed(self, run_id: str) -> bool:
        """Gate 证据优先读 gate.json（Spec §6.2 产物），缺失时退回 report.json。"""
        run_dir = self.runs_root / run_id
        for name in ("gate.json", "report.json"):
            path = run_dir / name
            if not path.is_file():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return False
            return payload.get("verdict") == "pass"
        return False

    def gate_evidence_path(self, run_id: str) -> Path:
        run_dir = self.runs_root / run_id
        gate_path = run_dir / "gate.json"
        return gate_path if gate_path.is_file() else run_dir / "report.json"
