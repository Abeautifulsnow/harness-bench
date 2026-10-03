"""benchmark 命令族（PRD §70/§71/§104）。"""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path

import typer
from rich.table import Table

from agent_eval.cli.commands import DATA_ROOT, EVALS_ROOT, FIXTURES_ROOT, console
from agent_eval.execution.models import (
    TERMINAL_STATUSES,
    EvalRunJob,
    EvalRunRequest,
    JobStatus,
)
from agent_eval.execution.service import EvalRunService, InvalidSubmission
from agent_eval.loading.loader import load_benchmark

app = typer.Typer(help="Benchmark 操作", no_args_is_help=True)


@app.command("list")
def benchmark_list(root: Path = typer.Option(EVALS_ROOT, "--root")) -> None:
    benchmarks_dir = root / "benchmarks"
    table = Table(title="Benchmarks")
    for col in ("Name", "Dataset", "Suites", "Default Profile", "Owner"):
        table.add_column(col)
    if benchmarks_dir.is_dir():
        for path in sorted(benchmarks_dir.glob("*.yaml")):
            b = load_benchmark(root, path.stem)
            table.add_row(b.name, b.dataset, ", ".join(b.suites), b.default_profile, b.owner or "-")
    console.print(table)


@app.command("run")
def benchmark_run(
    benchmark: str = typer.Argument(..., help="benchmark name under evals/benchmarks/"),
    profile: str | None = typer.Option(None, "--profile", "-p"),
    repeat: int | None = typer.Option(None, "--repeat"),
    concurrency: int = typer.Option(4, "--concurrency"),
    agent_endpoint: str = typer.Option("fake://", "--agent", envvar="AGENT_EVAL_AGENT_ENDPOINT"),
    baseline_policy: str | None = typer.Option(None, "--baseline-policy"),
    baseline_run: str | None = typer.Option(None, "--baseline-run"),
    gate: str = typer.Option("pr", "--gate", help="pr | main | release"),
    suite: list[str] = typer.Option(
        [], "--suite", help="显式套件（覆盖 benchmark 声明；release 门禁按 PRD §108 校验覆盖）"
    ),
    run_id_file: Path | None = typer.Option(None, "--run-id-file"),
    no_judge: bool = typer.Option(False, "--no-judge"),
    strict_protocol: bool = typer.Option(
        False,
        "--strict-protocol",
        help="E1：未知事件类型升级为 exit 2（默认 warn 只计数进报告 warnings；profile 亦可声明）",
    ),
    judge_skip_policy: str = typer.Option(
        "none",
        "--judge-skip-policy",
        help="none | skip_blocked：case 已被阻断判死时跳过其非阻断 judge（PRD §92 省成本）",
    ),
    no_save_artifacts: bool = typer.Option(
        False,
        "--no-save-artifacts",
        help="跳过 case 级产物采集（工作区变更 / 库 dump）；现场留存是可选行为",
    ),
    tag: list[str] = typer.Option([], "--tag"),
    timeout: float | None = typer.Option(
        None,
        "--timeout",
        help="运行期预算覆盖（秒）：替换 case 声明的 execution.timeout，session 与轮层同时生效",
    ),
    model: str | None = typer.Option(None, "--model", help="agent model label (PRD §30)"),
    agent_version: str | None = typer.Option(
        None, "--agent-version", help="agent / Harness 版本（PRD §30、§109.3 可复现信息）"
    ),
    judge_model: str | None = typer.Option(None, "--judge-model"),
    experiment_id: str | None = typer.Option(None, "--experiment-id"),
    variant_id: str | None = typer.Option(None, "--variant-id"),
    root: Path = typer.Option(EVALS_ROOT, "--root"),
    fixtures_root: Path = typer.Option(FIXTURES_ROOT, "--fixtures-root"),
    data_dir: Path = typer.Option(DATA_ROOT, "--data-dir"),
) -> None:
    """运行 benchmark：Agent → Trace → Eval → Result → Gate（PRD §104 验收链）。

    §45 统一入口：经 EvalRunService 提交（同一套服务端校验 + Job 账本），原地等待
    终态后按既有约定输出与退出。Job 落在 <data-dir>/jobs/，与 Web 发起的评测同账本。
    """
    request = _build_run_request(
        benchmark=benchmark,
        agent_endpoint=agent_endpoint,
        profile=profile,
        repeat=repeat,
        concurrency=concurrency,
        gate=gate,
        suites=list(suite),
        tags=list(tag),
        no_judge=no_judge,
        strict_protocol=strict_protocol,
        no_save_artifacts=no_save_artifacts,
        baseline_policy=baseline_policy,
        baseline_run=baseline_run,
        judge_skip_policy=judge_skip_policy,
        timeout=timeout,
        model=model,
        agent_version=agent_version,
        judge_model=judge_model,
        experiment_id=experiment_id,
        variant_id=variant_id,
    )
    service = EvalRunService(
        evals_root=root,
        data_root=data_dir,
        fixtures_root=fixtures_root,
        # review #I02/#I03：CLI 是本机可信入口——不收割共享账本里 Web 正在跑的
        # Job，也不受 §13 浏览器上限约束。
        reap_orphans=False,
        repeat_cap=None,
        agent_concurrency_cap=None,
    )
    job: EvalRunJob | None = None

    def _on_submitted(submitted: EvalRunJob) -> None:
        # review #I01：KeyboardInterrupt 可能发生在 asyncio.run 返回之前——
        # 提交一落地就捕获引用，KI 分支才有东西可取消。
        nonlocal job
        job = submitted

    try:
        job = asyncio.run(
            service.run_sync(
                request,
                requested_by="cli",
                agent_endpoint=agent_endpoint,
                on_run_created=_writer(run_id_file),
                on_submitted=_on_submitted,
            )
        )
    except InvalidSubmission as exc:
        console.print(f"[red]error[/red] {exc}")
        raise typer.Exit(3) from exc
    except KeyboardInterrupt:
        # 与旧直跑行为对齐：Ctrl+C → 真实取消 + exit 130。有界等待 Job 落终态
        # （Runner 要把 run.json 收口成 cancelled），超时则如实退出、不留假象。
        if job is not None:
            with contextlib.suppress(Exception):
                service.cancel(job.job_id)
            _await_terminal_bounded(service, job.job_id, timeout=10.0)
        console.print("[yellow]cancelled[/yellow]")
        raise typer.Exit(130) from None
    finally:
        service.shutdown()

    _finish(job)


def _build_run_request(
    *,
    benchmark: str,
    agent_endpoint: str,
    profile: str | None,
    repeat: int | None,
    concurrency: int,
    gate: str,
    suites: list[str],
    tags: list[str],
    no_judge: bool,
    strict_protocol: bool,
    no_save_artifacts: bool,
    baseline_policy: str | None,
    baseline_run: str | None,
    judge_skip_policy: str,
    timeout: float | None,
    model: str | None,
    agent_version: str | None,
    judge_model: str | None,
    experiment_id: str | None,
    variant_id: str | None,
) -> EvalRunRequest:
    return EvalRunRequest(
        agent_profile=agent_endpoint,  # CLI ad-hoc：仅作请求记录，不查注册表
        benchmark=benchmark,
        suite=suites or None,
        profile=profile,
        repeat=repeat,
        agent_concurrency=concurrency,
        no_judge=no_judge,
        strict_protocol=strict_protocol,
        save_artifacts=not no_save_artifacts,
        tags=tags,
        baseline_policy=baseline_policy,
        baseline_run=baseline_run,
        gate=gate,
        judge_skip_policy=judge_skip_policy,
        timeout=timeout,
        agent_model=model,
        agent_version=agent_version,
        judge_model=judge_model,
        experiment_id=experiment_id,
        variant_id=variant_id,
    )


_KI_DRAIN_POLL_SECONDS = 0.2


def _await_terminal_bounded(service: EvalRunService, job_id: str, timeout: float) -> None:
    """Ctrl+C 后等待 Job 落终态（Runner 收口 run.json），超时即放弃。"""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = service.get(job_id)
        if job is None or job.status in TERMINAL_STATUSES:
            return
        time.sleep(_KI_DRAIN_POLL_SECONDS)


def _finish(job: EvalRunJob | None) -> None:
    """终态输出与退出码（旧直跑路径的契约逐字保留）。"""

    if job is None:  # 提交即被 KI：run_sync 未及回调；cancel 已无从谈起
        raise typer.Exit(130)
    if job.status is JobStatus.FAILED:
        console.print(f"[red]error[/red] {job.error}")
        raise typer.Exit(job.exit_code if job.exit_code is not None else 1)
    if job.status is JobStatus.CANCELLED:
        console.print("[yellow]cancelled[/yellow]")
        raise typer.Exit(130)

    console.print(
        f"run [bold]{job.run_id}[/bold] status={job.run_status} "
        f"gate={job.gate or '-'} verdict={job.gate_verdict}"
    )
    for warning in job.warnings:
        console.print(f"[yellow]warn[/yellow] {warning}")
    raise typer.Exit(job.exit_code if job.exit_code is not None else 0)


def _writer(run_id_file: Path | None):
    def write(run_id: str) -> None:
        if run_id_file is not None:
            run_id_file.parent.mkdir(parents=True, exist_ok=True)
            run_id_file.write_text(run_id, encoding="utf-8")  # Spec §6.4

    return write
