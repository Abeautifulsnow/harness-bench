"""Case 级产物测试（PRD §90，Spec §21）。

四组断言对应四条契约，每条都要能红：

1. **采集时机**：快照必须在 ``provider.cleanup`` **之前**取出——cleanup 会删掉
   workspace 与库文件。判据不是"调用顺序看起来对"，而是"cleanup 之后现场还能读到"。
2. **前置校验**：name 会变成磁盘路径，所以白名单 + 逐段 ``..`` 检查缺一不可；
   越界名必须**记账后跳过**，不能静默消失。
3. **采集不污染判定**：provider 抛异常 / 返回畸形值都只记 note，跑完的执行仍是
   completed（否则一件附属品就能把整次 run 的结论改掉）。
4. **只读暴露**：请求里的 name 必须与索引全等才解析，路径穿越一律 404。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from agent_eval import adapters
from agent_eval.adapters.fake import FakeAgentAdapter, ScriptTurn
from agent_eval.api.app import create_app
from agent_eval.fixtures import filesystem_fixture as fs_mod
from agent_eval.fixtures import sqlite_fixture as sqlite_mod
from agent_eval.fixtures.base import FixtureHandle, FixtureProvider, get_provider
from agent_eval.fixtures.filesystem_fixture import FilesystemFixture
from agent_eval.fixtures.sqlite_fixture import SQLiteFixture
from agent_eval.models.artifacts import (
    UNAVAILABLE_KINDS,
    ArtifactKind,
    ArtifactRecord,
    SnapshotArtifact,
)
from agent_eval.models.case import EnvironmentSpec
from agent_eval.runner import artifacts as artifacts_mod
from agent_eval.runner import runner as runner_mod
from agent_eval.runner.artifacts import (
    collect_artifacts,
    safe_artifact_name,
    trace_artifact_record,
)
from agent_eval.runner.runner import RunConfig, Runner
from agent_eval.storage.run_store import RunStore

NOW = datetime(2026, 9, 28, 12, 0, 0)
SEED_SQL = """
CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT);
INSERT INTO customers VALUES (1, 'acmeCorp'), (2, 'globex');
"""


# ------------------------------------------------------------------ helpers


def _fixtures_root(tmp_path: Path) -> Path:
    """临时 fixtures 树：只放本文件用到的 fixture，避免依赖仓库示例数据。

    一律 write_bytes：文件大小参与断言（变更清单里有字节数），而 write_text 的
    newline 翻译会让同一份内容在不同平台上差一个字节。
    """
    root = tmp_path / "fixtures_artifacts"
    (root / "sales").mkdir(parents=True, exist_ok=True)
    (root / "sales" / "seed.sql").write_bytes(SEED_SQL.encode("utf-8"))
    (root / "workspace").mkdir(parents=True, exist_ok=True)
    (root / "workspace" / "notes.txt").write_bytes(b"initial\n")
    return root


def _run_case_json(data_root: Path, case_id: str, iteration: int | None = None) -> list[dict]:
    runs = sorted((data_root / "runs").iterdir())
    run_dir = runs[-1]
    out: list[dict] = []
    for path in sorted((run_dir / "case_runs").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload["case_id"] != case_id:
            continue
        if iteration is not None and payload["iteration"] != iteration:
            continue
        out.append(payload)
    return out


class TestSafeArtifactName:
    """落盘前的 name 校验（Spec §21.4 的第一道闸）。"""

    @pytest.mark.parametrize(
        "name",
        [
            "database.sql",
            "files.changes.txt",
            "files/src/app.py",
            "files/a-b_c.d/e.f",
            "nested/deep/path.sql",
        ],
    )
    def test_relative_names_are_accepted(self, name: str) -> None:
        assert safe_artifact_name(name) == name

    @pytest.mark.parametrize(
        "name",
        [
            "",
            "../escape.txt",
            "files/../../escape.txt",
            "..",
            "files/..",
            "/etc/passwd",
            "C:/Windows/win.ini",
            "C:\\Windows\\win.ini",
            "a\\b.txt",
            "files/%2e%2e/x",
            "with space.txt",
            "a" * 201,
        ],
    )
    def test_unsafe_names_are_rejected(self, name: str) -> None:
        assert safe_artifact_name(name) is None

    def test_dotdot_passes_the_charset_but_fails_the_segment_check(self) -> None:
        """两层判断缺一不可：``.`` 在字符集里，所以 ``../x`` 能过正则。

        这条是防回归的：把第二层（逐段 ``..`` 检查）删掉时，本用例是唯一会红的。
        """
        assert fs_mod.__doc__  # 保持 fixture 模块导入有据（见 FilesystemFixture 组）
        assert artifacts_mod._UNSAFE_NAME.match("../x") is not None, "第一层确实拦不住 .."
        assert safe_artifact_name("../x") is None
        assert safe_artifact_name("a/../b") is None


class TestCollectArtifacts:
    """落盘 + 索引 + 跳过记账。"""

    def test_writes_content_and_records_run_relative_path(self, tmp_path: Path) -> None:
        workdir = tmp_path / "run" / "artifacts" / "case" / "iter1"
        workdir.mkdir(parents=True)
        records, skipped = collect_artifacts(
            [
                SnapshotArtifact(
                    name="files/src/app.py",
                    kind=ArtifactKind.files,
                    content=b"print('hi')\n",
                )
            ],
            workdir=workdir,
            run_dir=tmp_path / "run",
            now=NOW,
        )
        assert skipped == []
        assert len(records) == 1
        record = records[0]
        assert record.path == "artifacts/case/iter1/artifacts/files/src/app.py"
        assert (tmp_path / "run" / record.path).read_bytes() == b"print('hi')\n"
        assert record.bytes == len(b"print('hi')\n") and record.kind is ArtifactKind.files

    def test_unsafe_name_is_skipped_and_accounted(self, tmp_path: Path) -> None:
        workdir = tmp_path / "run" / "iter1"
        workdir.mkdir(parents=True)
        records, skipped = collect_artifacts(
            [
                SnapshotArtifact(name="../../etc/passwd", kind=ArtifactKind.files, content=b"x"),
                SnapshotArtifact(name="ok.txt", kind=ArtifactKind.files, content=b"ok"),
            ],
            workdir=workdir,
            run_dir=tmp_path / "run",
            now=NOW,
        )
        assert [r.name for r in records] == ["ok.txt"]
        assert len(skipped) == 1 and "unsafe path" in skipped[0]
        assert "passwd" in skipped[0]
        # 越界内容一个字节都没落盘：不是"写到别处去了"。
        assert not (tmp_path / "etc").exists()

    def test_malformed_elements_are_accounted_not_raised(self, tmp_path: Path) -> None:
        """provider 的 snapshot 是对外扩展点：畸形元素记账跳过，不炸整次执行。"""
        workdir = tmp_path / "run" / "iter1"
        workdir.mkdir(parents=True)

        class _Alien:
            name = "alien.txt"
            kind = ArtifactKind.files
            content = b"x"

        records, skipped = collect_artifacts(
            [
                _Alien(),  # type: ignore[list-item]
                SnapshotArtifact(name="text.txt", kind=ArtifactKind.files, content="not bytes"),  # type: ignore[arg-type]
                SnapshotArtifact(name="good.txt", kind=ArtifactKind.files, content=b"good"),
            ],
            workdir=workdir,
            run_dir=tmp_path / "run",
            now=NOW,
        )
        assert [r.name for r in records] == ["good.txt"]
        assert len(skipped) == 2
        assert any("not a SnapshotArtifact" in note for note in skipped)
        assert any("must be bytes" in note for note in skipped)

    def test_binary_content_survives_the_round_trip(self, tmp_path: Path) -> None:
        """按 bytes 采是为了不丢内容：按 utf-8 读会抛异常或静默替换字符。"""
        workdir = tmp_path / "run" / "iter1"
        workdir.mkdir(parents=True)
        blob = bytes(range(256))
        records, _ = collect_artifacts(
            [SnapshotArtifact(name="blob.bin", kind=ArtifactKind.files, content=blob)],
            workdir=workdir,
            run_dir=tmp_path / "run",
            now=NOW,
        )
        assert (tmp_path / "run" / records[0].path).read_bytes() == blob


class TestTraceRegistration:
    def test_trace_inside_run_dir_is_indexed(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run"
        (run_dir / "traces").mkdir(parents=True)
        trace = run_dir / "traces" / "case.iter1.events.jsonl"
        trace.write_text('{"type":"x"}\n', encoding="utf-8")
        records = trace_artifact_record(trace, run_dir, NOW)
        assert [r.kind for r in records] == [ArtifactKind.trace]
        assert records[0].path == "traces/case.iter1.events.jsonl"

    @pytest.mark.parametrize("missing", [None, "absent"])
    def test_missing_trace_registers_nothing(self, tmp_path: Path, missing: str | None) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir(parents=True)
        path = None if missing is None else str(run_dir / "traces" / "nope.jsonl")
        assert trace_artifact_record(path, run_dir, NOW) == []

    def test_trace_outside_run_dir_is_not_indexed(self, tmp_path: Path) -> None:
        """相对路径无法安全回读 → 不登记（登记了也只会得到一个取不到的链接）。"""
        outside = tmp_path / "elsewhere.jsonl"
        outside.write_text("{}", encoding="utf-8")
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        assert trace_artifact_record(outside, run_dir, NOW) == []


class TestFilesystemFixtureSnapshot:
    async def _prepared(self, tmp_path: Path, fixture: str | None = "workspace"):
        provider = FilesystemFixture(_fixtures_root(tmp_path))
        spec = EnvironmentSpec(fixture=fixture, database="filesystem")
        handle = await provider.prepare(spec, tmp_path / "iter1")
        return provider, handle

    async def test_reports_added_modified_and_deleted(self, tmp_path: Path) -> None:
        """三种变更都要报出来，且 each 在不同 iteration 上成立（iter 间零共享）。"""
        provider, handle = await self._prepared(tmp_path)
        workdir = handle.workdir
        (workdir / "notes.txt").write_bytes(b"changed\n")  # modified
        (workdir / "src").mkdir()
        (workdir / "src" / "app.py").write_bytes(b"print(1)\n")  # added

        provider2 = FilesystemFixture(_fixtures_root(tmp_path))
        handle2 = await provider2.prepare(
            EnvironmentSpec(fixture="workspace", database="filesystem"), tmp_path / "iter2"
        )
        (handle2.workdir / "notes.txt").unlink()  # deleted（来自 fixture 初态）

        artifacts = await provider.snapshot(handle)
        text = next(a for a in artifacts if a.name.endswith("files.changes.txt")).content.decode()
        # 行格式：变更类型 \t 变更后字节数 \t 路径（删掉的文件报初态大小，0 会读成"空文件"）。
        assert "added\t9\tsrc/app.py" in text
        assert "modified\t8\tnotes.txt" in text

        artifacts2 = await provider2.snapshot(handle2)
        text2 = next(a for a in artifacts2 if a.name.endswith("files.changes.txt")).content.decode()
        assert "deleted\t8\tnotes.txt" in text2

    async def test_zero_changes_still_produces_a_conclusion(self, tmp_path: Path) -> None:
        """零变更也要产出：与"采不到的观测面造假文件"是两件事（Spec §21.1 第 2 条）。"""
        provider, handle = await self._prepared(tmp_path)
        artifacts = await provider.snapshot(handle)
        changes = next(a for a in artifacts if a.name.endswith("files.changes.txt"))
        assert "无变更" in changes.content.decode()
        assert changes.note == "本次执行未改动工作区"
        assert changes.truncated is False

    async def test_snapshot_without_a_manifest_reports_nothing(self, tmp_path: Path) -> None:
        """没有初态清单就没有"变更"可言——不把所有 fixture 文件冒充成 agent 产出。"""
        provider = FilesystemFixture(_fixtures_root(tmp_path))
        workdir = tmp_path / "manual"
        workdir.mkdir()
        (workdir / "existing.txt").write_bytes(b"x")
        assert await provider.snapshot(FixtureHandle(name="manual", workdir=workdir)) == []

    async def test_changed_file_content_is_kept(self, tmp_path: Path) -> None:
        # 一律用 write_bytes：内容按字节采（Spec §21.1 第 1 条），用 write_text
        # 断言会让结果随平台变（Windows 上 '\n' 会变 '\r\n'）。
        provider, handle = await self._prepared(tmp_path)
        (handle.workdir / "notes.txt").write_bytes(b"changed\n")
        artifacts = await provider.snapshot(handle)
        kept = next(a for a in artifacts if a.name == "files/notes.txt")
        assert kept.content == b"changed\n"
        assert kept.truncated is False and kept.note is None

    async def test_large_file_is_truncated_and_says_so(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(fs_mod, "MAX_FILE_CONTENT_BYTES", 8)
        provider, handle = await self._prepared(tmp_path)
        (handle.workdir / "big.txt").write_bytes(b"0123456789abc")
        artifacts = await provider.snapshot(handle)
        kept = next(a for a in artifacts if a.name == "files/big.txt")
        assert kept.content == b"01234567"
        assert kept.truncated is True
        assert kept.note is not None and "13" in kept.note

    async def test_artifact_count_limit_is_reported(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(fs_mod, "MAX_FILE_ARTIFACTS", 3)
        provider, handle = await self._prepared(tmp_path)
        for index in range(5):
            (handle.workdir / f"f{index}.txt").write_bytes(b"x")
        artifacts = await provider.snapshot(handle)
        contents = [a for a in artifacts if a.name.startswith("files/")]
        assert len(contents) == 3
        assert "另有 2 个变更文件未留存内容" in (contents[-1].note or "")

    async def test_binary_change_is_kept_as_bytes(self, tmp_path: Path) -> None:
        provider, handle = await self._prepared(tmp_path)
        (handle.workdir / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")
        artifacts = await provider.snapshot(handle)
        kept = next(a for a in artifacts if a.name == "files/blob.bin")
        assert kept.content == b"\xff\xfe\x00\x01"


class TestSqliteFixtureSnapshot:
    async def _prepared(self, tmp_path: Path):
        provider = SQLiteFixture(_fixtures_root(tmp_path))
        handle = await provider.prepare(
            EnvironmentSpec(fixture="sales", database="sqlite"), tmp_path / "iter1"
        )
        return provider, handle

    async def test_dump_is_readable_sql_text(self, tmp_path: Path) -> None:
        provider, handle = await self._prepared(tmp_path)
        artifacts = await provider.snapshot(handle)
        assert [a.name for a in artifacts] == ["database.sql"]
        text = artifacts[0].content.decode()
        assert "CREATE TABLE customers" in text
        assert "acmeCorp" in text
        assert artifacts[0].kind is ArtifactKind.database
        assert artifacts[0].truncated is False

    async def test_missing_database_returns_nothing(self, tmp_path: Path) -> None:
        """库被 agent 删掉时不抛异常：少一件产物，不是整轮 ERROR。"""
        provider, handle = await self._prepared(tmp_path)
        Path(handle.info["path"]).unlink()
        assert await provider.snapshot(handle) == []

    async def test_dump_is_truncated_at_a_statement_boundary(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(sqlite_mod, "MAX_DUMP_BYTES", 60)
        provider, handle = await self._prepared(tmp_path)
        artifacts = await provider.snapshot(handle)
        text = artifacts[0].content.decode()
        assert artifacts[0].truncated is True
        assert sqlite_mod.TRUNCATION_NOTE in text
        body = text.split(f"-- {sqlite_mod.TRUNCATION_NOTE}")[0]
        assert body.endswith(";\n"), "截断必须落在完整语句边界上"

    async def test_cleanup_removes_database_files(self, tmp_path: Path) -> None:
        """cleanup 会删库——这正是"采集必须在 cleanup 之前"的原因（Spec §21.2）。"""
        provider, handle = await self._prepared(tmp_path)
        db_path = Path(handle.info["path"])
        assert db_path.is_file()
        await provider.cleanup(handle)
        assert not db_path.exists()


class TestSnapshotContract:
    def test_default_snapshot_returns_empty_not_none(self) -> None:
        """抽象基类的缺省实现：返回 []，让调用侧不必多一条 None 分支。"""
        import inspect

        source = inspect.getsource(FixtureProvider.snapshot)
        assert "return []" in source

    def test_provider_abc_exposes_a_non_abstract_snapshot(self) -> None:
        """有缺省实现 → 子类不被强制实现（"无 fixture"的用例没有现场可采）。"""
        assert "snapshot" in vars(FixtureProvider)
        assert "snapshot" not in FixtureProvider.__abstractmethods__

    def test_unavailable_kinds_each_explain_why(self) -> None:
        """能力表里每一项都要给出原因，不许笼统的"暂不支持"。"""
        assert set(UNAVAILABLE_KINDS) == {
            "screenshots",
            "logs",
            "git_diff",
            "command_output",
            "reports",
        }
        for kind, reason in UNAVAILABLE_KINDS.items():
            assert len(reason) > 10, kind
            assert "暂不支持" not in reason, kind


# ------------------------------------------------------------ runner wiring


def _cfg(evals_root: Path, data_root: Path, fixtures_root: Path, **kw) -> RunConfig:
    return RunConfig(
        evals_root=evals_root,
        fixtures_root=fixtures_root,
        data_root=data_root,
        benchmark="database-core",
        agent_endpoint="fake://",
        tag_filter=["smoke"],
        repeat=1,
        concurrency=1,
        **kw,
    )


class TestRunnerCollectsArtifacts:
    async def test_artifacts_are_collected_before_cleanup(self, evals_tree, fixtures_root) -> None:
        """判据是"cleanup 之后现场还在"，不是"调用顺序看起来对"。

        cleanup 删掉的是 ``workspace/`` 与库文件；产物写在**同级**的
        ``artifacts/``（Spec §21.3 的布局），所以两者能同时成立：
        workspace 没了，产物还在。
        """
        evals_root, data_root = evals_tree
        outcome = await Runner(_cfg(evals_root, data_root, fixtures_root)).run()
        assert outcome.exit_code == 0

        run_dir = data_root / "runs" / outcome.run_id
        iter_dir = run_dir / "artifacts" / "database.query.top_customers" / "iter1"
        assert not (iter_dir / "workspace").exists(), "cleanup 应已删掉工作区"
        dump = iter_dir / "artifacts" / "database.sql"
        assert dump.is_file() and dump.stat().st_size > 0
        assert "CREATE TABLE" in dump.read_text(encoding="utf-8")

    async def test_index_is_reachable_by_case_run_id(self, evals_tree, fixtures_root) -> None:
        evals_root, data_root = evals_tree
        outcome = await Runner(_cfg(evals_root, data_root, fixtures_root)).run()

        payloads = _run_case_json(data_root, "database.query.top_customers")
        assert payloads
        payload = payloads[0]
        indexed = payload["artifacts"]
        assert {item["kind"] for item in indexed} == {"database", "trace"}
        # 反查无需映射表：索引就挂在 CaseRunResult 上，id 就是 case_run_id。
        assert all(item["path"] for item in indexed)
        assert all(
            (data_root / "runs" / outcome.run_id / item["path"]).is_file() for item in indexed
        )

    async def test_filesystem_case_reports_no_changes_honestly(
        self, evals_tree, fixtures_root
    ) -> None:
        """fake adapter 不写文件 → 如实产出"无变更"，而不是不产出清单。"""
        evals_root, data_root = evals_tree
        await Runner(_cfg(evals_root, data_root, fixtures_root)).run()
        payloads = _run_case_json(data_root, "smoke.echo.basic", iteration=1)
        assert payloads
        names = [item["name"] for item in payloads[0]["artifacts"]]
        assert "files.changes.txt" in names
        assert "database.sql" not in names

    async def test_save_artifacts_false_collects_nothing(self, evals_tree, fixtures_root) -> None:
        """--no-save-artifacts：索引为空，磁盘上也不留任何采集文件。

        workdir 目录本身仍会被 prepare 创建（fixture 就住在那里），所以判据是
        "没有文件"而不是"没有目录"——后者会把 fixture 的正常行为误判成违规。
        """
        evals_root, data_root = evals_tree
        outcome = await Runner(
            _cfg(evals_root, data_root, fixtures_root, save_artifacts=False)
        ).run()
        assert outcome.exit_code == 0
        payloads = _run_case_json(data_root, "database.query.top_customers")
        assert payloads[0]["artifacts"] == []
        assert payloads[0]["artifact_notes"] == []
        run_dir = data_root / "runs" / outcome.run_id
        assert list(run_dir.glob("artifacts/**/artifacts/*")) == []

    async def test_failed_case_still_leaves_a_scene(self, evals_tree, fixtures_root) -> None:
        """PRD §90 的"失败现场仍可获取"：case FAIL 不影响现场采集。"""
        evals_root, data_root = evals_tree
        benchmark = _scripted_sqlite_failure(evals_root)
        runner = Runner(
            RunConfig(
                evals_root=evals_root,
                fixtures_root=fixtures_root,
                data_root=data_root,
                benchmark=benchmark,
                agent_endpoint="fake://",
                repeat=1,
                concurrency=1,
            )
        )
        runner.adapter = FakeAgentAdapter(script_queue=[ScriptTurn(output="sorry")])
        outcome = await runner.run()
        assert outcome.exit_code == 1

        payloads = _run_case_json(data_root, "scripted.artifact.fail")
        assert payloads[0]["status"] == "FAIL"
        dump = next(item for item in payloads[0]["artifacts"] if item["name"] == "database.sql")
        assert (data_root / "runs" / outcome.run_id / dump["path"]).is_file()

    async def test_snapshot_failure_is_a_note_not_a_verdict(
        self, evals_tree, fixtures_root, monkeypatch
    ) -> None:
        """provider 抛异常只记账——不能让它把一次跑完的执行改判成 ERROR。"""
        evals_root, data_root = evals_tree

        async def _boom(handle: FixtureHandle) -> list[SnapshotArtifact]:
            raise RuntimeError("snapshot exploded")

        def _broken_provider(spec, root):
            provider = get_provider(spec, root)
            provider.snapshot = _boom  # type: ignore[method-assign]
            return provider

        monkeypatch.setattr(runner_mod, "get_provider", _broken_provider)
        outcome = await Runner(_cfg(evals_root, data_root, fixtures_root)).run()
        assert outcome.exit_code == 0

        payload = _run_case_json(data_root, "database.query.top_customers")[0]
        assert payload["status"] == "PASS"
        assert any("snapshot exploded" in note for note in payload["artifact_notes"])
        # trace 登记独立于 fixture 快照：provider 坏了不代表没有 trace 可看。
        assert [item["kind"] for item in payload["artifacts"]] == ["trace"]

    async def test_malformed_snapshot_return_is_a_note(
        self, evals_tree, fixtures_root, monkeypatch
    ):
        evals_root, data_root = evals_tree

        async def _wrong_type(handle: FixtureHandle):
            return {"name": "nope"}

        def _provider(spec, root):
            provider = get_provider(spec, root)
            provider.snapshot = _wrong_type  # type: ignore[method-assign]
            return provider

        monkeypatch.setattr(runner_mod, "get_provider", _provider)
        outcome = await Runner(_cfg(evals_root, data_root, fixtures_root)).run()
        assert outcome.exit_code == 0
        payload = _run_case_json(data_root, "database.query.top_customers")[0]
        assert any("expected list" in note for note in payload["artifact_notes"])


def _scripted_sqlite_failure(evals_root: Path) -> str:
    """注入一个必然失败的 sqlite case：断言要的输出不存在。"""
    ds = evals_root / "datasets" / "scripted-artifacts"
    (ds / "cases").mkdir(parents=True, exist_ok=True)
    (ds / "dataset.yaml").write_text("id: scripted-artifacts\nversion: 1.0.0\n", encoding="utf-8")
    (ds / "cases" / "fail.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "scripted.artifact.fail",
                "version": 1,
                "name": "失败现场",
                "tags": ["scripted-artifacts"],
                "input": {"type": "single_turn", "prompt": "ping"},
                "environment": {"fixture": "sales_v2", "database": "sqlite"},
                "execution": {"timeout": 10, "repeat": 1},
                "expected": {"output": {"contains": ["QUERY COMPLETE"]}},
            }
        ),
        encoding="utf-8",
    )
    (evals_root / "benchmarks" / "scripted-artifacts.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "scripted-artifacts",
                "dataset": "scripted-artifacts@1.0.0",
                "suites": ["artifact-fail"],
                "default_profile": "default",
            }
        ),
        encoding="utf-8",
    )
    (evals_root / "suites" / "artifact-fail.yaml").write_text(
        yaml.safe_dump({"name": "artifact-fail", "tags": ["scripted-artifacts"]}), encoding="utf-8"
    )
    return "scripted-artifacts"


# ------------------------------------------------------------------- API


@pytest.fixture()
def client(evals_tree) -> TestClient:
    evals_root, data_root = evals_tree
    app = create_app(evals_root=evals_root, data_root=data_root)
    return TestClient(app, raise_server_exceptions=False)


async def _smoke_run(evals_tree, fixtures_root) -> str:
    evals_root, data_root = evals_tree
    outcome = await Runner(_cfg(evals_root, data_root, fixtures_root)).run()
    return outcome.run_id


def _urls(run_id: str, case_id: str, name: str, iteration: int = 1) -> tuple[str, str]:
    """(预览, 原文) 两条 URL。原文走平级前缀 ``artifact-raw/`` 而不是 ``/raw`` 后缀
    ——后缀会被含 ``/`` 的产物名吃掉歧义（见 `services._case_artifact_url`）。"""
    base = f"/api/runs/{run_id}/cases/{case_id}"
    return (
        f"{base}/artifacts/{name}?iteration={iteration}",
        f"{base}/artifact-raw/{name}?iteration={iteration}",
    )


def _inject_artifact(
    data_root: Path, run_id: str, case_id: str, record: ArtifactRecord, content: bytes
) -> Path:
    """把一条产物直接塞进索引 + 磁盘（模拟"索引与磁盘都被改过"的情形）。

    走 ``RunStore`` 而不是手写 JSON：那才与生产写入路径同源，索引损坏类的断言
    才有意义。
    """
    store = RunStore(data_root / "runs")
    _meta, results = store.load_run(run_id)
    target = next(r for r in results if r.case_id == case_id)
    target.artifacts.append(record)
    store.run_dir = store.run_dir_for(run_id)
    store.save_case_run(target)
    path = data_root / "runs" / run_id / record.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


class TestCaseArtifactApi:
    async def test_index_lists_artifacts_with_capability_table(
        self, evals_tree, fixtures_root, client: TestClient
    ) -> None:
        run_id = await _smoke_run(evals_tree, fixtures_root)
        payload = client.get(
            f"/api/runs/{run_id}/cases/database.query.top_customers/artifacts"
        ).json()
        assert payload["run_id"] == run_id
        assert payload["case_id"] == "database.query.top_customers"
        names = {item["name"] for item in payload["items"]}
        assert {"database.sql", "raw.trace.jsonl"} == names
        assert all(item["case_run_id"] for item in payload["items"])
        assert payload["notes"] == []
        # 能力表随索引一起返回（PRD §57 可见性）：UI 才能区分"没有能力"与"采集失败"。
        assert set(payload["unavailable"]) == set(UNAVAILABLE_KINDS)

    async def test_unknown_case_is_404(self, evals_tree, fixtures_root, client: TestClient) -> None:
        run_id = await _smoke_run(evals_tree, fixtures_root)
        response = client.get(f"/api/runs/{run_id}/cases/nope.case/artifacts")
        assert response.status_code == 404

    async def test_text_artifact_preview_and_raw_download(
        self, evals_tree, fixtures_root, client: TestClient
    ) -> None:
        run_id = await _smoke_run(evals_tree, fixtures_root)
        preview_url, raw_url = _urls(run_id, "database.query.top_customers", "database.sql")
        preview = client.get(preview_url).json()
        assert preview["content_type"] == "application/sql"
        assert "CREATE TABLE" in preview["text"]
        assert preview["truncated"] is False

        raw = client.get(raw_url)
        assert raw.status_code == 200
        assert "CREATE TABLE" in raw.text
        assert raw.headers["content-type"].startswith("application/sql")

    async def test_a_file_named_raw_is_not_shadowed_by_the_raw_route(
        self, evals_tree, fixtures_root, client: TestClient
    ) -> None:
        """产物名 ``files/raw`` 不能被"原文路由"吃掉——两条 URL 必须各自指向自己的端点。

        旧写法是 ``.../artifacts/{name}/raw``：``/artifacts/files/raw`` 会被当成
        "``files`` 的原文"（name=``files``），于是"下载得到、预览 404 说产物不存在"。
        现在原文端点用平级前缀 ``artifact-raw/``，歧义在结构上就不存在：预览必须
        给出**真实原因**（无后缀 → 不给文本预览，用 400 + 指向下载），而不是
        谎称产物不存在（404）。
        """
        evals_root, data_root = evals_tree
        run_id = await _smoke_run(evals_tree, fixtures_root)
        _inject_artifact(
            data_root,
            run_id,
            "smoke.echo.basic",
            ArtifactRecord(
                name="files/raw",
                kind=ArtifactKind.files,
                path="artifacts/smoke.echo.basic/iter1/artifacts/files/raw",
                bytes=6,
                collected_at=NOW,
            ),
            b"body\n\n",
        )
        preview_url, raw_url = _urls(run_id, "smoke.echo.basic", "files/raw")
        preview = client.get(preview_url)
        assert preview.status_code == 400, preview.text
        assert "artifact-raw" in preview.json()["error"]

        raw = client.get(raw_url)
        assert raw.status_code == 200
        assert raw.content == b"body\n\n"

    async def test_extensionless_file_is_download_only_but_never_404(
        self, evals_tree, fixtures_root, client: TestClient
    ) -> None:
        """没有后缀的产物（``Makefile`` 类）判成二进制：只给下载，不猜文本。

        宁可不预览也不硬解：按 utf-8 读一个二进制文件会抛异常或替换成乱码，
        两种都算"现场失真"，而下载永远是对的。
        """
        evals_root, data_root = evals_tree
        run_id = await _smoke_run(evals_tree, fixtures_root)
        _inject_artifact(
            data_root,
            run_id,
            "smoke.echo.basic",
            ArtifactRecord(
                name="files/Makefile",
                kind=ArtifactKind.files,
                path="artifacts/smoke.echo.basic/iter1/artifacts/files/Makefile",
                bytes=8,
                collected_at=NOW,
            ),
            b"all:\n\techo\n",
        )
        preview_url, raw_url = _urls(run_id, "smoke.echo.basic", "files/Makefile")
        assert client.get(preview_url).status_code == 400
        raw = client.get(raw_url)
        assert raw.status_code == 200
        assert raw.content == b"all:\n\techo\n"

    async def test_iteration_discriminates_the_record(
        self, evals_tree, fixtures_root, client: TestClient
    ) -> None:
        """iteration 是查名的一部分：命中 iter1 的记录不会在 iter9 上被"顺手找到"。"""
        evals_root, data_root = evals_tree
        run_id = await _smoke_run(evals_tree, fixtures_root)
        case_id = "database.query.top_customers"
        _inject_artifact(
            data_root,
            run_id,
            case_id,
            ArtifactRecord(
                name="probe.txt",
                kind=ArtifactKind.files,
                path=f"artifacts/{case_id}/iter1/artifacts/probe.txt",
                bytes=4,
                collected_at=NOW,
            ),
            b"iter1",
        )
        preview_url, _ = _urls(run_id, case_id, "probe.txt", iteration=1)
        assert client.get(preview_url).status_code == 200
        wrong_iter_url, _ = _urls(run_id, case_id, "probe.txt", iteration=9)
        assert client.get(wrong_iter_url).status_code == 404

    async def test_nested_artifact_name_round_trips(
        self, evals_tree, fixtures_root, client: TestClient
    ) -> None:
        """``files/src/app.py`` 这类带 ``/`` 的名字要在 URL 里原样走通。"""
        run_id = await _smoke_run(evals_tree, fixtures_root)
        payload = client.get(f"/api/runs/{run_id}/cases/smoke.echo.basic/artifacts").json()
        item = payload["items"][0]
        response = client.get(item["url"])
        assert response.status_code == 200
        assert "workspace 变更" in response.json()["text"]
        # 原文端点走同一个查名校验：带 / 的名字必须同样通，否则"预览能看、下载 404"
        # 这种半通状态只会在浏览器里才被发现。
        raw = client.get(item["raw_url"])
        assert raw.status_code == 200
        assert "workspace 变更" in raw.text

    @pytest.mark.parametrize(
        "name",
        [
            "../../../../etc/passwd",
            "..%2F..%2Fetc%2Fpasswd",
            "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
            "C:%5CWindows%5Cwin.ini",
            "....//....//etc/passwd",
        ],
    )
    async def test_path_traversal_is_never_served(
        self, evals_tree, fixtures_root, client: TestClient, name: str
    ) -> None:
        """反例必须红（PRD §90 验收）：越界名一律 404/400，绝不读到仓库外内容。"""
        run_id = await _smoke_run(evals_tree, fixtures_root)
        case_id = "database.query.top_customers"
        preview_url, raw_url = _urls(run_id, case_id, name)
        for url in (preview_url, raw_url):
            response = client.get(url)
            assert response.status_code in {400, 404}, (name, url, response.status_code)
            assert "root:" not in response.text

    async def test_index_path_escaping_the_run_dir_is_refused(
        self, evals_tree, fixtures_root, client: TestClient
    ) -> None:
        """第二层：索引自己不可信（它来自磁盘上的 case_runs/*.json）。

        即使 name 与索引全等（请求"合法"），只要 ``path`` 解析到 run 目录之外，
        仍然回 404——resolve 之后判包含关系是唯一能挡住 symlink 的顺序。
        """
        evals_root, data_root = evals_tree
        run_id = await _smoke_run(evals_tree, fixtures_root)
        # ``../outside.txt`` 相对 run 目录解析到 runs/ 下，所以"仓库外"的诱饵放这里，
        # 并在改索引**之前**写好：否则测到的是"文件不存在"而不是"越界被拒"。
        secret = data_root / "runs" / "outside.txt"
        secret.write_bytes(b"classified")
        assert (data_root / "runs" / run_id / ".." / "outside.txt").resolve() == secret.resolve()
        store = RunStore(data_root / "runs")
        _meta, results = store.load_run(run_id)
        target = next(r for r in results if r.case_id == "database.query.top_customers")
        target.artifacts.append(
            ArtifactRecord(
                name="evil.txt",
                kind=ArtifactKind.files,
                path="../outside.txt",
                bytes=10,
                collected_at=NOW,
            )
        )
        store.run_dir = store.run_dir_for(run_id)
        store.save_case_run(target)

        preview_url, raw_url = _urls(run_id, "database.query.top_customers", "evil.txt")
        for url in (preview_url, raw_url):
            response = client.get(url)
            assert response.status_code == 404, response.text
            assert "classified" not in response.text

    async def test_index_path_missing_on_disk_is_404(
        self, evals_tree, fixtures_root, client: TestClient
    ) -> None:
        """索引里有、磁盘上没有 → 404（不是 500）：被清理掉也是"取不到"。"""
        evals_root, data_root = evals_tree
        run_id = await _smoke_run(evals_tree, fixtures_root)
        _inject_artifact(
            data_root,
            run_id,
            "database.query.top_customers",
            ArtifactRecord(
                name="ghost.txt",
                kind=ArtifactKind.files,
                path="artifacts/ghost.txt",
                bytes=1,
                collected_at=NOW,
            ),
            b"",
        )
        (data_root / "runs" / run_id / "artifacts" / "ghost.txt").unlink()
        preview_url, raw_url = _urls(run_id, "database.query.top_customers", "ghost.txt")
        assert client.get(preview_url).status_code == 404
        assert client.get(raw_url).status_code == 404

    async def test_binary_preview_is_refused_with_a_pointer_to_raw(
        self, evals_tree, fixtures_root, client: TestClient
    ) -> None:
        """非文本不给预览：按 utf-8 硬解会抛异常或替换成乱码，两种都算失真。"""
        evals_root, data_root = evals_tree
        run_id = await _smoke_run(evals_tree, fixtures_root)
        blob = b"\x89PNG\r\n\x1a\n\xff\xfe"
        _inject_artifact(
            data_root,
            run_id,
            "database.query.top_customers",
            ArtifactRecord(
                name="shot.png",
                kind=ArtifactKind.files,
                path="artifacts/shot.png",
                bytes=len(blob),
                collected_at=NOW,
            ),
            blob,
        )
        preview_url, raw_url = _urls(run_id, "database.query.top_customers", "shot.png")
        response = client.get(preview_url)
        assert response.status_code == 400
        assert "artifact-raw" in response.json()["error"]

        raw = client.get(raw_url)
        assert raw.status_code == 200
        assert raw.content == blob
        assert raw.headers["content-type"] == "application/octet-stream"

    def test_unknown_run_is_404(self, client: TestClient) -> None:
        assert client.get("/api/runs/run_missing/cases/x/artifacts").status_code == 404

    def test_imports_stay_wired(self) -> None:
        """防止本轮新增的模块被误删（导入即契约）。"""
        assert adapters.FakeAgentAdapter is FakeAgentAdapter
        assert callable(collect_artifacts)
