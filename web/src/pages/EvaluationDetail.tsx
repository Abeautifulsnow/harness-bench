import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router";
import { ExternalLink, XCircle } from "lucide-react";

import { KeyValue, Mono, PageHeader, Section } from "@/components/common/primitives";
import { QueryState } from "@/components/common/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { fmtRelative } from "@/lib/format";
import {
  JOB_TERMINAL,
  evaluationApi,
  subscribeJobEvents,
} from "@/lib/evaluation-api";
import type { EvalRunJob } from "@/lib/evaluation-api";
import { JobStatusBadge } from "@/pages/Evaluations";

/** 设计文档 §19/§43.3：Evaluation Detail —— 实时进度 / Cancel / 错误 / 跳转 Run。 */
export function EvaluationDetail() {
  const { jobId = "" } = useParams();
  const queryClient = useQueryClient();

  // 初次拉取 + 终态前的轮询兜底：SSE 掉线时页面也不会僵住。
  const query = useQuery({
    queryKey: ["eval-runs", jobId],
    queryFn: () => evaluationApi.job(jobId),
    refetchInterval: (q) =>
      q.state.data && !JOB_TERMINAL.has(q.state.data.status) ? 5000 : false,
  });

  // §35：SSE 只负责订阅，不承载任务本身。组件卸载即退订，任务继续跑。
  React.useEffect(() => {
    if (!jobId) return;
    return subscribeJobEvents(
      jobId,
      (job) => {
        queryClient.setQueryData(["eval-runs", jobId], job);
        if (JOB_TERMINAL.has(job.status)) {
          void queryClient.invalidateQueries({ queryKey: ["eval-runs"] });
        }
      },
      () => {
        /* 断线由轮询兜底；无需特殊处理 */
      },
    );
  }, [jobId, queryClient]);

  const cancel = useMutation({
    mutationFn: () => evaluationApi.cancelEvalRun(jobId),
    onSuccess: (job) => queryClient.setQueryData(["eval-runs", jobId], job),
  });

  if (query.isLoading || query.error) {
    return (
      <QueryState
        isLoading={query.isLoading}
        error={query.error}
        isEmpty={false}
        onRetry={() => void query.refetch()}
      >
        {null}
      </QueryState>
    );
  }

  const job = query.data;
  if (!job) return null;
  const terminal = JOB_TERMINAL.has(job.status);

  return (
    <div className="space-y-5">
      <PageHeader
        title={`Evaluation ${job.job_id}`}
        description={
          job.status === "queued"
            ? "排队中：已有评测在运行，本任务等全局并发空位。"
            : "Job 是一次评测请求；Run 是评测事实。完成后可跳转 Run Detail 进入分析面。"
        }
        actions={
          <div className="flex items-center gap-2">
            <JobStatusBadge status={job.status} />
            {!terminal && (
              <Button
                variant="destructive"
                size="sm"
                disabled={cancel.isPending}
                onClick={() => cancel.mutate()}
              >
                <XCircle /> Cancel Evaluation
              </Button>
            )}
          </div>
        }
      />
      {cancel.error && <CancelError message={(cancel.error as Error).message} />}
      {job.error && <FailureSection job={job} />}
      <ProgressSection job={job} />
      <ConfigSection job={job} />
      {job.run_id && <RunSection job={job} />}
    </div>
  );
}

function CancelError({ message }: { message: string }) {
  return (
    <p className="rounded-md border border-[var(--fail)]/30 bg-[var(--fail)]/5 px-3 py-2 text-xs text-[var(--fail)]">
      取消失败：{message}
    </p>
  );
}

/** §32：Job 失败原因与失败语义分开呈现。 */
function FailureSection({ job }: { job: EvalRunJob }) {
  return (
    <Section title="失败原因" description={`Failure Kind: ${job.failure_kind ?? "—"}`}>
      <p className="font-mono text-xs break-all text-[var(--fail)]">{job.error}</p>
    </Section>
  );
}

/** §18/§19：run-level progress。total 在 Runner 选定 case 后才可知。 */
function ProgressSection({ job }: { job: EvalRunJob }) {
  const { progress } = job;
  const percent =
    progress.total_trials && progress.total_trials > 0
      ? Math.min(100, Math.round((progress.completed_trials / progress.total_trials) * 100))
      : null;
  const runningTrials =
    progress.total_trials != null
      ? Math.max(0, progress.total_trials - progress.completed_trials)
      : null;

  return (
    <Section
      title="Progress"
      description={
        progress.total_trials == null
          ? "总 trial 数在 Runner 选定 case 后可知（§18：run-level progress）。"
          : undefined
      }
    >
      <div className="space-y-4">
        <div className="flex items-baseline gap-2">
          <span className="tabular text-2xl font-semibold">
            {progress.completed_trials}
            {progress.total_trials != null && ` / ${progress.total_trials}`}
          </span>
          {percent != null && <span className="text-xs text-muted-foreground">{percent}%</span>}
        </div>
        <Progress value={percent ?? 0} />
        <div className="flex flex-wrap gap-2 text-xs">
          <Badge variant="outline" className="text-[var(--pass)]">
            PASS {progress.passed}
          </Badge>
          <Badge variant="outline" className="text-[var(--fail)]">
            FAIL {progress.failed}
          </Badge>
          <Badge variant="outline" className="text-[var(--fail)]">
            ERROR {progress.error}
          </Badge>
          {runningTrials != null && runningTrials > 0 && (
            <Badge variant="outline" className="text-primary">
              RUNNING {runningTrials}
            </Badge>
          )}
        </div>
        <div className="text-xs text-muted-foreground">
          发起 {fmtRelative(job.created_at)}
          {job.started_at && ` · 开始 ${fmtRelative(job.started_at)}`}
          {job.finished_at && ` · 结束 ${fmtRelative(job.finished_at)}`}
        </div>
      </div>
    </Section>
  );
}

/** 请求参数区：这些值直接进入 RunConfig，与 CLI 参数一一对应。 */
function ConfigSection({ job }: { job: EvalRunJob }) {
  return (
    <Section title="配置" description="这些值直接进入 RunConfig，与 CLI 参数一一对应。">
      <KeyValue
        columns={3}
        items={[
          { label: "Benchmark", value: job.request.benchmark },
          { label: "Agent", value: job.request.agent_profile },
          { label: "Profile", value: job.request.profile ?? "跟随定义" },
          { label: "Suite", value: job.request.suite?.join(", ") ?? "跟随定义" },
          { label: "Repeat", value: job.request.repeat ?? "跟随定义" },
          { label: "Agent 并发", value: job.request.agent_concurrency ?? "默认 4" },
          { label: "Judge", value: job.request.no_judge ? "禁用" : "启用" },
          { label: "Strict Protocol", value: job.request.strict_protocol ? "开启" : "关闭" },
          { label: "Tags", value: job.request.tags?.join(", ") || "—" },
          { label: "发起人", value: job.requested_by },
        ]}
      />
    </Section>
  );
}

/** §40：Job → run_id → Run Detail，后续分析复用既有分析面页面。 */
function RunSection({ job }: { job: EvalRunJob }) {
  if (!job.run_id) return null;
  return (
    <Section
      title="Run"
      description="评测事实已落盘：Trace / Regression / Failure / Quality Gate 都从 Run Detail 进入。"
    >
      <div className="flex flex-wrap items-center gap-3">
        {job.gate_verdict && (
          <Badge
            variant="outline"
            className={
              job.gate_verdict === "pass"
                ? "bg-[var(--pass)]/10 text-[var(--pass)]"
                : job.gate_verdict === "fail"
                  ? "bg-[var(--fail)]/10 text-[var(--fail)]"
                  : "bg-muted text-muted-foreground"
            }
          >
            Gate: {job.gate_verdict}
          </Badge>
        )}
        <Button variant="outline" size="sm" asChild>
          <Link to={`/runs/${job.run_id}`}>
            <ExternalLink /> Open Run Detail
          </Link>
        </Button>
        <Mono>{job.run_id}</Mono>
      </div>
    </Section>
  );
}
