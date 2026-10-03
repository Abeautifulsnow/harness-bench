import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";
import { Plus } from "lucide-react";

import { Mono, PageHeader } from "@/components/common/primitives";
import { QueryState } from "@/components/common/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { fmtRelative } from "@/lib/format";
import {
  JOB_STATUS_LABEL,
  JOB_TERMINAL,
  evaluationApi,
  type EvalRunJob,
} from "@/lib/evaluation-api";

/** 设计文档 §38/§43.2：Evaluation Job 列表（Running / Queued / History）。 */
export function Evaluations() {
  const query = useQuery({
    queryKey: ["eval-runs"],
    queryFn: evaluationApi.jobs,
    // 列表页有活跃 Job 时 3s 轮询兜底（详情页有 SSE，列表不订阅）。
    refetchInterval: (query) =>
      query.state.data?.some((job) => !JOB_TERMINAL.has(job.status)) ? 3000 : false,
  });

  return (
    <div className="space-y-5">
      <PageHeader
        title="Evaluation"
        description="设计文档 §38/§45：评测 Job 账本。CLI / Web / 调度与触发器发起的评测都经 EvalRunService 记在这里；Run 是 Runner 产生的评测事实，完成后经 Run 列进入分析面。"
        actions={
          <Button asChild>
            <Link to="/evaluations/new">
              <Plus /> New Evaluation
            </Link>
          </Button>
        }
      />
      <QueryState
        isLoading={query.isLoading}
        error={query.error}
        isEmpty={query.data?.length === 0}
        emptyTitle="还没有通过 Web 发起的评测"
        emptyHint="点击右上角 New Evaluation 选择 Agent 与 Benchmark 发起一次评测。"
        onRetry={() => void query.refetch()}
      >
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Job</TableHead>
              <TableHead>Benchmark</TableHead>
              <TableHead>Agent</TableHead>
              <TableHead>状态</TableHead>
              <TableHead>进度</TableHead>
              <TableHead>发起人</TableHead>
              <TableHead>发起时间</TableHead>
              <TableHead>Run</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {query.data?.map((job) => (
              <TableRow key={job.job_id}>
                <TableCell>
                  <Link
                    to={`/evaluations/${job.job_id}`}
                    className="font-mono text-xs text-primary hover:underline"
                  >
                    {job.job_id}
                  </Link>
                </TableCell>
                <TableCell className="text-sm">{job.request.benchmark}</TableCell>
                <TableCell className="text-sm">{job.request.agent_profile}</TableCell>
                <TableCell>
                  <JobStatusBadge status={job.status} />
                </TableCell>
                <TableCell className="tabular text-xs">
                  <ProgressCell job={job} />
                </TableCell>
                <TableCell className="text-xs text-muted-foreground">{job.requested_by}</TableCell>
                <TableCell className="text-xs text-muted-foreground">
                  {fmtRelative(job.created_at)}
                </TableCell>
                <TableCell>
                  {job.run_id ? (
                    <Link
                      to={`/runs/${job.run_id}`}
                      className="font-mono text-xs text-primary hover:underline"
                    >
                      {job.run_id.slice(0, 12)}
                    </Link>
                  ) : (
                    <span className="text-xs text-muted-foreground">—</span>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </QueryState>
    </div>
  );
}

function ProgressCell({ job }: { job: EvalRunJob }) {
  const { progress } = job;
  if (progress.total_trials == null) return <span className="text-muted-foreground">—</span>;
  return (
    <span>
      {progress.completed_trials} / {progress.total_trials}
      {progress.error > 0 && <span className="ml-1 text-[var(--fail)]">({progress.error} err)</span>}
    </span>
  );
}

export function JobStatusBadge({ status }: { status: EvalRunJob["status"] }) {
  const tone: Record<string, string> = {
    queued: "bg-muted text-muted-foreground",
    running: "bg-primary/10 text-primary",
    cancelling: "bg-[var(--warn)]/10 text-[var(--warn)]",
    succeeded: "bg-[var(--pass)]/10 text-[var(--pass)]",
    failed: "bg-[var(--fail)]/10 text-[var(--fail)]",
    cancelled: "bg-muted text-muted-foreground",
  };
  return (
    <Badge variant="outline" className={tone[status]}>
      {JOB_STATUS_LABEL[status]}
      {status === "cancelled" && <Mono className="ml-1">cancel</Mono>}
    </Badge>
  );
}
