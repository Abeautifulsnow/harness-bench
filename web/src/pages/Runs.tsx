import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";

import { BaselineBadge, PassRate, RunStatusBadge } from "@/components/common/badges";
import { Mono, PageHeader, Section } from "@/components/common/primitives";
import { QueryState } from "@/components/common/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api } from "@/lib/api";
import { fmtRelative, shortCommit, shortId } from "@/lib/format";

export function Runs() {
  const [benchmark, setBenchmark] = React.useState("all");
  const [status, setStatus] = React.useState("all");

  const benchmarks = useQuery({ queryKey: ["benchmarks"], queryFn: api.benchmarks });
  const runs = useQuery({
    queryKey: ["runs", { benchmark, status }],
    queryFn: () =>
      api.runs({
        benchmark: benchmark === "all" ? undefined : benchmark,
        status: status === "all" ? undefined : status,
        limit: 300,
      }),
  });

  return (
    <div className="space-y-5">
      <PageHeader
        title="Run"
        description="PRD §81：Run 列表。pass rate 由 report.json 的 passed/iterations 得出（不是 UI 自己算的）。"
      />

      <div className="flex flex-wrap items-center gap-2">
        <Select value={benchmark} onValueChange={setBenchmark}>
          <SelectTrigger className="w-52">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部 benchmark</SelectItem>
            {benchmarks.data?.map((row) => (
              <SelectItem key={row.name} value={row.name}>
                {row.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select value={status} onValueChange={setStatus}>
          <SelectTrigger className="w-40">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部状态</SelectItem>
            <SelectItem value="completed">已完成</SelectItem>
            <SelectItem value="partial">部分完成</SelectItem>
            <SelectItem value="running">运行中</SelectItem>
            <SelectItem value="queued">排队</SelectItem>
          </SelectContent>
        </Select>
        <span className="text-xs text-muted-foreground">
          {runs.data ? `${runs.data.length} 条` : ""}
        </span>
      </div>

      <Section title="Run 列表">
        <QueryState
          isLoading={runs.isLoading}
          error={runs.error}
          isEmpty={runs.data?.length === 0}
          emptyTitle="没有匹配的 run"
          emptyHint="先跑一次：`agent-eval benchmark run <benchmark>`。"
          onRetry={() => void runs.refetch()}
        >
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Run</TableHead>
                <TableHead>Benchmark</TableHead>
                <TableHead>Dataset</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>基线</TableHead>
                <TableHead>模型</TableHead>
                <TableHead>Commit</TableHead>
                <TableHead>No judge</TableHead>
                <TableHead>开始</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {runs.data?.map((run) => (
                <TableRow key={run.run_id}>
                  <TableCell>
                    <Link
                      to={`/runs/${run.run_id}`}
                      className="font-mono text-xs text-primary hover:underline"
                    >
                      {shortId(run.run_id)}
                    </Link>
                  </TableCell>
                  <TableCell>{run.benchmark_id}</TableCell>
                  <TableCell>
                    <Mono>
                      {run.dataset_id}@{run.dataset_version}
                    </Mono>
                  </TableCell>
                  <TableCell>
                    <RunStatusBadge status={run.status} />
                  </TableCell>
                  <TableCell>
                    <BaselineBadge mode={run.baseline_mode} reason={run.baseline_reason} />
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {run.agent_model ?? "—"}
                  </TableCell>
                  <TableCell className="font-mono text-xs text-muted-foreground">
                    {shortCommit(run.git_commit)}
                  </TableCell>
                  <TableCell>
                    {run.no_judge ? <Badge variant="warn">no-judge</Badge> : <span className="text-muted-foreground">—</span>}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {fmtRelative(run.started_at)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </QueryState>
      </Section>

      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <Button variant="link" size="sm" asChild>
          <Link to="/trends">看趋势视图</Link>
        </Button>
        <span>·</span>
        <span>Pass rate 与回归计数来自各 run 的 report.json</span>
      </div>
    </div>
  );
}

export function RunPassRateCell({ value }: { value: number | null }) {
  return <PassRate value={value} />;
}
