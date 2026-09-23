"""CLI 测试：exit code 契约（Spec §6.1）、run-id-file（§6.4）、查询命令。"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from agent_eval.cli.app import app

cli = CliRunner()


def test_benchmark_run_smoke_exit_0_and_run_id_file(evals_tree, fixtures_root, tmp_path) -> None:
    evals_root, data_root = evals_tree
    run_id_file = tmp_path / "run.id"
    result = cli.invoke(
        app,
        [
            "benchmark",
            "run",
            "database-core",
            "--tag",
            "smoke",
            "--root",
            str(evals_root),
            "--data-dir",
            str(data_root),
            "--run-id-file",
            str(run_id_file),
            "--no-judge",
        ],
    )
    assert result.exit_code == 0
    run_id = run_id_file.read_text(encoding="utf-8").strip()
    assert (Path(data_root) / "runs" / run_id / "report.json").is_file()
    meta = (Path(data_root) / "runs" / run_id / "run.json").read_text(encoding="utf-8")
    assert "NO_BASELINE" in meta  # P0 无 baseline 存储（Spec §4.3）


def test_benchmark_run_core_exit_1(evals_tree, fixtures_root) -> None:
    evals_root, data_root = evals_tree
    result = cli.invoke(
        app,
        [
            "benchmark",
            "run",
            "database-core",
            "--root",
            str(evals_root),
            "--data-dir",
            str(data_root),
        ],
    )
    assert result.exit_code == 1


def test_benchmark_run_unknown_benchmark_exit_3(evals_tree, fixtures_root) -> None:
    evals_root, data_root = evals_tree
    result = cli.invoke(
        app,
        [
            "benchmark",
            "run",
            "no-such",
            "--root",
            str(evals_root),
            "--data-dir",
            str(data_root),
        ],
    )
    assert result.exit_code == 3


def test_benchmark_run_unreachable_agent_exit_2(evals_tree, fixtures_root) -> None:
    evals_root, data_root = evals_tree
    result = cli.invoke(
        app,
        [
            "benchmark",
            "run",
            "database-core",
            "--tag",
            "smoke",
            "--agent",
            "http://127.0.0.1:59999",
            "--root",
            str(evals_root),
            "--data-dir",
            str(data_root),
        ],
    )
    assert result.exit_code == 2


def test_benchmark_run_invalid_endpoint_exit_3(evals_tree, fixtures_root) -> None:
    """回归 #I05：endpoint 配置非法是无效调用（exit 3），不得穿透为未捕获异常 exit 1。

    CI 按 exit code 归因（Spec §6.4）：误判为 1 会把配置错误当成 PR 引入的回归。
    """
    evals_root, data_root = evals_tree
    result = cli.invoke(
        app,
        [
            "benchmark",
            "run",
            "database-core",
            "--tag",
            "smoke",
            "--agent",
            "ftp://nope",
            "--root",
            str(evals_root),
            "--data-dir",
            str(data_root),
        ],
    )
    assert result.exit_code == 3
    assert "unsupported agent endpoint" in result.output


def test_gate_replay_exit_codes(evals_tree, fixtures_root, tmp_path) -> None:
    evals_root, data_root = evals_tree
    run_id_file = tmp_path / "run.id"
    cli.invoke(
        app,
        [
            "benchmark",
            "run",
            "database-core",
            "--tag",
            "smoke",
            "--root",
            str(evals_root),
            "--data-dir",
            str(data_root),
            "--run-id-file",
            str(run_id_file),
        ],
    )
    passing = run_id_file.read_text(encoding="utf-8").strip()
    assert cli.invoke(app, ["gate", passing, "--data-dir", str(data_root)]).exit_code == 0

    result = cli.invoke(
        app,
        [
            "benchmark",
            "run",
            "database-core",
            "--root",
            str(evals_root),
            "--data-dir",
            str(data_root),
            "--run-id-file",
            str(run_id_file),
        ],
    )
    assert result.exit_code == 1
    failing = run_id_file.read_text(encoding="utf-8").strip()
    assert cli.invoke(app, ["gate", failing, "--data-dir", str(data_root)]).exit_code == 1
    assert cli.invoke(app, ["gate", "run_missing", "--data-dir", str(data_root)]).exit_code == 3


def test_query_commands(evals_tree, fixtures_root) -> None:
    evals_root, data_root = evals_tree
    assert cli.invoke(app, ["benchmark", "list", "--root", str(evals_root)]).exit_code == 0
    assert cli.invoke(app, ["case", "list", "--root", str(evals_root)]).exit_code == 0
    show = cli.invoke(app, ["case", "show", "smoke.echo.basic", "--root", str(evals_root)])
    assert show.exit_code == 0 and "smoke.echo.basic" in show.output
    missing = cli.invoke(app, ["case", "show", "ghost", "--root", str(evals_root)])
    assert missing.exit_code == 3

    run_result = cli.invoke(
        app,
        [
            "benchmark",
            "run",
            "database-core",
            "--tag",
            "smoke",
            "--root",
            str(evals_root),
            "--data-dir",
            str(data_root),
        ],
    )
    assert run_result.exit_code == 0
    assert cli.invoke(app, ["run", "list", "--data-dir", str(data_root)]).exit_code == 0
    import re

    run_id = re.search(r"run_[0-9a-f]{12}", run_result.output)
    assert run_id is not None
    detail = cli.invoke(app, ["run", "show", run_id.group(0), "--data-dir", str(data_root)])
    assert detail.exit_code == 0
