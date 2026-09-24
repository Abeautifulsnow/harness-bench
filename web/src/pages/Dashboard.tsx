import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";
import { ArrowUpRight, CircleSlash } from "lucide-react";

import { PassRate, RunStatusBadge, VerdictBadge, BaselineBadge } from "@/components/common/badges";
import { KeyValue, PageHeader, Section, StatCard } from "@/components/common/primitives";
import { QueryState } from "@/components/common/states";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api } from "@/lib/api";
import type { DashboardCard } from "@/lib/api-types";
import { fmtCost, fmtMs, fmtNumber, fmtRelative, shortCommit, shortId } from "@/lib/format";

function cardTone(label: string, value: number | string | null): "default" | "pass" | "fail" | "warn" {
  if (value === null) return "default";
  if (typeof value !== "number") return "default";
  switch (label) {
    case "Regression Count":
    case "Security Failures":
    case "Flaky Cases":
      return value === 0 ? "pass" : "fail";
    case "Task Success":
      return value >= 1 ? "pass" : value === 0 ? "fail" : "warn";
    default:
      return "default";
  }
}

function cardValue(card: DashboardCard): string | number | null {
  if (card.value === null) return null;
  if (card.unit === "ratio" && typeof card.value === "number") {
    return `${(card.value * 100).toFixed(1)}%`;
  }
  if (card.unit === "USD" && typeof card.value === "number") return fmtCost(card.value);
  if (card.unit === "ms" && typeof card.value === "number") return fmtMs(card.value);
  if (typeof card.value === "number") return fmtNumber(card.value);
  return card.value;
}

export function Dashboard() {
  const query = useQuery({ queryKey: ["dashboard"], queryFn: () => api.dashboard() });

  return (
    <div className="space-y-5">
      <PageHeader
        title="Dashboard"
        description="PRD §72：当前发布点 + 核心健康指标。数字全部来自最近一次 run 的 report.json（事实层），不依赖派生层。"
      />
      <QueryState
        isLoading={query.isLoading}
        error={query.error}
        onRetry={() => void query.refetch()}
        isEmpty={false}
      >
        {query.data && (
          <>
            {query.data.hint && (
              <div className="rounded-lg border border-[color-mix(in_oklch,var(--warn)_35%,transparent)] bg-[color-mix(in_oklch,var(--warn)_8%,transparent)] px-3 py-2 text-xs text-muted-foreground">
                {query.data.hint}
              </div>
            )}

            <Card>
              <CardContent className="flex flex-wrap items-center justify-between gap-4 pt-4">
                <div className="space-y-1">
                  <div className="text-[11px] tracking-wide text-muted-foreground uppercase">
                    Current Release
                  </div>
                  {query.data.current_release ? (
                    <Link
                      to={`/runs/${query.data.current_release}`}
                      className="inline-flex items-center gap-1.5 font-mono text-sm text-primary hover:underline"
                    >
                      {query.data.current_release}
                      <ArrowUpRight className="size-3.5" />
                    </Link>
                  ) : (
                    <div className="flex items-center gap-2 text-sm text-muted-foreground">
                      <CircleSlash className="size-4" />
                      尚未 pin release baseline
                      <span className="text-xs">
                        （`agent-eval baseline pin &lt;run&gt; --mode release`）
                      </span>
                    </div>
                  )}
                </div>
                <Badge variant={query.data.projection === "ok" ? "pass" : "warn"}>
                  派生层 {query.data.projection === "ok" ? "已构建" : "未构建"}
                </Badge>
              </CardContent>
            </Card>

            {query.data.cards.length === 0 ? (
              <Card>
                <CardContent className="pt-4 text-sm text-muted-foreground">
                  还没有任何 run：「agent-eval benchmark run &lt;benchmark&gt;」跑一次后这里会出现指标卡。
                </CardContent>
              </Card>
            ) : (
              <div className="grid grid-cols-2 gap-3 lg:grid-cols-3 xl:grid-cols-6">
                {query.data.cards.map((card) => (
                  <StatCard
                    key={card.label}
                    label={card.label}
                    value={cardValue(card)}
                    detail={card.detail}
                    tone={cardTone(card.label, card.value)}
                  />
                ))}
              </div>
            )}

            <Section
              title="最近 Run"
              description="按 started_at 倒序；点进任意一行看 Overview / Cases / Trace / Gate / Artifacts。"
            >
              {query.data.recent_runs.length === 0 ? (
                <p className="text-sm text-muted-foreground">暂无 run。</p>
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Run</TableHead>
                      <TableHead>Benchmark</TableHead>
                      <TableHead>Status</TableHead>
                      <TableHead>基线</TableHead>
                      <TableHead>模型</TableHead>
                      <TableHead>Commit</TableHead>
                      <TableHead>开始</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {query.data.recent_runs.slice(0, 12).map((run) => (
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
                        <TableCell className="text-xs text-muted-foreground">
                          {fmtRelative(run.started_at)}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </Section>
          </>
        )}
      </QueryState>
    </div>
  );
}

export function RunSummaryRow({
  runId,
  passRate,
  verdict,
  baselineMode,
}: {
  runId: string;
  passRate: number | null;
  verdict: string | null;
  baselineMode: string;
}) {
  return (
    <KeyValue
      items={[
        { label: "Run", value: shortId(runId) },
        { label: "Verdict", value: <VerdictBadge verdict={verdict} /> },
        { label: "Pass rate", value: <PassRate value={passRate} /> },
        { label: "Baseline", value: <BaselineBadge mode={baselineMode} /> },
      ]}
    />
  );
}
