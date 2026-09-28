import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router";
import { Download, ExternalLink, FileJson2 } from "lucide-react";

import {
  BaselineBadge,
  ExitCodeBadge,
  MetricDiffBadge,
  PassRate,
  RegressionBadge,
  RunStatusBadge,
  StabilityBadge,
  VerdictBadge,
} from "@/components/common/badges";
import { KeyValue, Mono, Section, StatCard } from "@/components/common/primitives";
import { QueryState } from "@/components/common/states";
import { TraceTree } from "@/components/common/TraceTree";
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
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api } from "@/lib/api";
import type { CaseArtifactRow, CaseResultRow, RunOverview } from "@/lib/api-types";
import {
  fmtBytes,
  fmtCost,
  fmtMs,
  fmtNumber,
  fmtRatio,
  fmtRelative,
  fmtTokens,
  shortCommit,
} from "@/lib/format";
import { baselineLabel, runStatusLabel } from "@/lib/verdicts";

export function RunDetail() {
  const { runId = "" } = useParams();
  const overview = useQuery({
    queryKey: ["run", runId],
    queryFn: () => api.run(runId),
    enabled: Boolean(runId),
  });

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <h1 className="font-mono text-base font-semibold">{runId}</h1>
            {overview.data && <VerdictBadge verdict={overview.data.verdict} />}
          </div>
          {overview.data && (
            <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <span>{overview.data.run.benchmark_id}</span>
              <span>·</span>
              <Mono>
                {overview.data.run.dataset_id}@{overview.data.run.dataset_version}
              </Mono>
              <span>·</span>
              <span>{fmtRelative(overview.data.run.started_at)}</span>
            </div>
          )}
        </div>
        {overview.data && (
          <div className="flex items-center gap-2">
            <RunStatusBadge status={overview.data.run.status} />
            <BaselineBadge
              mode={overview.data.run.baseline_mode}
              reason={overview.data.run.baseline_reason}
            />
          </div>
        )}
      </div>

      <QueryState isLoading={overview.isLoading} error={overview.error} onRetry={() => void overview.refetch()}>
        {overview.data && <RunTabs overview={overview.data} />}
      </QueryState>
    </div>
  );
}

function RunTabs({ overview }: { overview: RunOverview }) {
  const runId = overview.run.run_id;
  const [tab, setTab] = React.useState("overview");

  return (
    <Tabs value={tab} onValueChange={setTab}>
      <TabsList>
        <TabsTrigger value="overview">Overview</TabsTrigger>
        <TabsTrigger value="cases">Cases</TabsTrigger>
        <TabsTrigger value="trace">Trace</TabsTrigger>
        <TabsTrigger value="failures">Failures</TabsTrigger>
        <TabsTrigger value="gate">Gate</TabsTrigger>
        <TabsTrigger value="artifacts">Artifacts</TabsTrigger>
        <TabsTrigger value="config">Configuration</TabsTrigger>
      </TabsList>

      <TabsContent value="overview">
        <OverviewTab overview={overview} />
      </TabsContent>
      <TabsContent value="cases">
        <CasesTab runId={runId} />
      </TabsContent>
      <TabsContent value="trace">
        <TraceTab runId={runId} />
      </TabsContent>
      <TabsContent value="failures">
        <FailuresTab runId={runId} />
      </TabsContent>
      <TabsContent value="gate">
        <GateTab runId={runId} />
      </TabsContent>
      <TabsContent value="artifacts">
        <ArtifactsTab runId={runId} />
      </TabsContent>
      <TabsContent value="config">
        <ConfigTab overview={overview} />
      </TabsContent>
    </Tabs>
  );
}

function OverviewTab({ overview }: { overview: RunOverview }) {
  const counts = overview.counts;
  const metrics = overview.metrics;
  const cost = overview.cost;

  return (
    <div className="space-y-4">
      {overview.warnings.length > 0 && (
        <div className="space-y-1 rounded-lg border border-[color-mix(in_oklch,var(--warn)_35%,transparent)] bg-[color-mix(in_oklch,var(--warn)_8%,transparent)] px-3 py-2">
          {overview.warnings.map((warning) => (
            <div key={warning} className="text-xs text-muted-foreground">
              ⚠ {warning}
            </div>
          ))}
        </div>
      )}

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4 xl:grid-cols-6">
        <StatCard
          label="Cases"
          value={counts.cases ?? 0}
          detail={`${counts.iterations ?? 0} iterations`}
        />
        <StatCard
          label="Passed"
          value={fmtRatio(
            counts.iterations ? (counts.passed_iterations ?? 0) / counts.iterations : null,
          )}
          tone={
            counts.failed_iterations
              ? "fail"
              : counts.error_iterations
                ? "warn"
                : "pass"
          }
          detail={`${counts.passed_iterations ?? 0}/${counts.iterations ?? 0}`}
        />
        <StatCard
          label="Regression"
          value={counts.regression_cases ?? 0}
          tone={(counts.regression_cases ?? 0) > 0 ? "fail" : "pass"}
          detail={baselineLabel(overview.baseline_mode)}
        />
        <StatCard
          label="Flaky"
          value={counts.flaky_cases ?? 0}
          tone={(counts.flaky_cases ?? 0) > 0 ? "warn" : "pass"}
        />
        <StatCard
          label="Avg tool calls"
          value={fmtNumber(metrics.tool_calls, 2)}
          detail={`tokens ${fmtTokens(metrics.tokens)}`}
        />
        <StatCard
          label="Avg latency"
          value={fmtMs(metrics.latency_ms)}
          tone="default"
          detail={cost.total_cost === null ? "无定价" : `cost ${fmtCost(cost.total_cost)}`}
        />
      </div>

      <Section title="Run-level Metric（PRD §81/§105）">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Metric</TableHead>
              <TableHead>Value</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {Object.entries(metrics)
              .sort(([a], [b]) => a.localeCompare(b))
              .map(([metric, value]) => (
                <TableRow key={metric}>
                  <TableCell className="font-mono text-xs">{metric}</TableCell>
                  <TableCell className="tabular">
                    {metric === "cost" ? fmtCost(value) : fmtNumber(value, 4)}
                  </TableCell>
                </TableRow>
              ))}
          </TableBody>
        </Table>
      </Section>

      {cost.unpriced_cases > 0 && (
        <div className="rounded-lg border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
          {cost.priced_cases} 个 CaseRun 可计价，{cost.unpriced_cases} 个无匹配定价。
          无定价时 cost 为 null（PRD §59 禁止用 0.0 冒充），因此均值只按可计价部分计算。
        </div>
      )}
    </div>
  );
}

const REGRESSION_TABS = ["全部", "退步", "进步", "波动", "无法判定"] as const;

function CasesTab({ runId }: { runId: string }) {
  const query = useQuery({ queryKey: ["run-cases", runId], queryFn: () => api.runCases(runId) });
  const [filter, setFilter] = React.useState<(typeof REGRESSION_TABS)[number]>("全部");

  const rows = React.useMemo(() => {
    const data = query.data ?? [];
    switch (filter) {
      case "退步":
        return data.filter((row) => row.regression_state === "REGRESSION");
      case "进步":
        return data.filter((row) => row.regression_state === "IMPROVED");
      case "波动":
        return data.filter((row) => row.stability === "FLAKY");
      case "无法判定":
        return data.filter((row) => row.regression_state === "UNDETERMINED");
      default:
        return data;
    }
  }, [query.data, filter]);

  return (
    <Section
      title="Case 聚合（CaseRun 按 case 聚合）"
      description="稳定性按 Spec §3.1 判定：repeat/valid ≥ 3 才给 STABLE/FLAKY，否则 UNKNOWN；ERROR 轮不参与。"
      actions={
        <Select value={filter} onValueChange={(value) => setFilter(value as typeof filter)}>
          <SelectTrigger className="w-36">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {REGRESSION_TABS.map((item) => (
              <SelectItem key={item} value={item}>
                {item}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      }
    >
      <QueryState
        isLoading={query.isLoading}
        error={query.error}
        isEmpty={rows.length === 0}
        emptyTitle="没有匹配的 case"
        onRetry={() => void query.refetch()}
      >
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Case</TableHead>
              <TableHead>Stability</TableHead>
              <TableHead>Pass rate</TableHead>
              <TableHead>Regression</TableHead>
              <TableHead>pass@k</TableHead>
              <TableHead>Tool calls</TableHead>
              <TableHead>Tokens</TableHead>
              <TableHead>Latency</TableHead>
              <TableHead>失败分类</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((row) => (
              <CaseRow key={row.case_id} runId={runId} row={row} />
            ))}
          </TableBody>
        </Table>
      </QueryState>
    </Section>
  );
}

function CaseRow({ runId, row }: { runId: string; row: CaseResultRow }) {
  const [open, setOpen] = React.useState(false);
  const metrics = useQuery({
    queryKey: ["case-metrics", runId, row.case_id],
    queryFn: () => api.caseMetrics(runId, row.case_id),
    enabled: open,
  });

  return (
    <>
      <TableRow className="cursor-pointer" onClick={() => setOpen((value) => !value)}>
        <TableCell className="font-mono text-xs">
          {row.case_id}
          {row.infra_error_count > 0 && (
            <Badge variant="warn" className="ml-2">
              {row.infra_error_count} ERROR
            </Badge>
          )}
        </TableCell>
        <TableCell>
          <StabilityBadge stability={row.stability} />
        </TableCell>
        <TableCell>
          <PassRate value={row.pass_rate} />
        </TableCell>
        <TableCell>
          {row.regression_state === "UNDETERMINED" ? (
            <span className="text-xs text-muted-foreground">无法判定</span>
          ) : (
            <RegressionBadge state={row.regression_state} />
          )}
        </TableCell>
        <TableCell className="tabular text-xs">
          {Object.entries(row.pass_at_k).length === 0
            ? "—"
            : Object.entries(row.pass_at_k)
                .map(([k, v]) => `${k}=${v.toFixed(2)}`)
                .join(" ")}
        </TableCell>
        <TableCell className="tabular">{fmtNumber(row.tool_calls_mean, 2)}</TableCell>
        <TableCell className="tabular">{fmtTokens(row.tokens_mean)}</TableCell>
        <TableCell className="tabular">{fmtMs(row.latency_mean)}</TableCell>
        <TableCell className="text-xs">
          {row.failure_category ? <Badge variant="fail">{row.failure_category}</Badge> : "—"}
        </TableCell>
      </TableRow>
      {open && (
        <TableRow className="bg-muted/20 hover:bg-muted/20">
          <TableCell colSpan={9} className="p-0">
            <div className="space-y-2 px-3 py-3">
              {row.blocking_failures.length > 0 && (
                <div className="space-y-1">
                  <div className="text-[11px] tracking-wide text-muted-foreground uppercase">
                    blocking failures
                  </div>
                  {row.blocking_failures.map((failure, index) => (
                    <div key={index} className="flex flex-wrap items-center gap-2 text-xs">
                      <Badge variant="fail">{String(failure.metric ?? "—")}</Badge>
                      {failure.mount && <Badge variant="neutral">{String(failure.mount)}</Badge>}
                      <span className="text-muted-foreground">{String(failure.reason ?? "")}</span>
                    </div>
                  ))}
                </div>
              )}
              {row.error_semantics.length > 0 && (
                <div className="text-xs text-[var(--warn)]">
                  failure_semantics: {row.error_semantics.join(", ")}
                </div>
              )}
              <QueryState isLoading={metrics.isLoading} error={metrics.error}>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Iter</TableHead>
                      <TableHead>Metric</TableHead>
                      <TableHead>Evaluator</TableHead>
                      <TableHead>Mount</TableHead>
                      <TableHead>Score</TableHead>
                      <TableHead>Threshold</TableHead>
                      <TableHead>Verdict</TableHead>
                      <TableHead>Reason</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {metrics.data?.map((metric) => (
                      <TableRow key={`${metric.case_run_id}-${metric.metric}-${metric.turn ?? ""}`}>
                        <TableCell className="tabular text-xs">{metric.iteration}</TableCell>
                        <TableCell className="font-mono text-xs">{metric.metric}</TableCell>
                        <TableCell className="text-xs text-muted-foreground">
                          {metric.evaluator}
                        </TableCell>
                        <TableCell className="text-xs">
                          {metric.mount ?? "—"}
                          {metric.turn !== null && (
                            <span className="ml-1 text-muted-foreground">turn {metric.turn}</span>
                          )}
                        </TableCell>
                        <TableCell className="tabular text-xs">
                          {metric.score === null ? "—" : fmtNumber(metric.score, 4)}
                        </TableCell>
                        <TableCell className="tabular text-xs">
                          {metric.threshold === null ? "—" : fmtNumber(metric.threshold, 4)}
                        </TableCell>
                        <TableCell>
                          <VerdictBadge verdict={metric.verdict} />
                        </TableCell>
                        <TableCell className="max-w-md truncate text-xs text-muted-foreground">
                          {metric.reason ?? "—"}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </QueryState>

              <CaseArtifactsPanel runId={runId} caseId={row.case_id} />
            </div>
          </TableCell>
        </TableRow>
      )}
    </>
  );
}

/** PRD §90 / Spec §21：某 case 的现场产物（工作区变更、库快照、Raw Trace）。
 *
 * 三块内容含义不同，分开显示：
 *  - items：这次真采到了什么（含 truncated / note，不静默）；
 *  - notes：采集期的记账（名字非法 / 写盘失败 / provider 抛异常）——有内容就说明
 *    这次确实少了一件，与"本来就没有"必须看得出差别；
 *  - unavailable：能力表（Spec §21.1）——采不到的观测面如实列出，不造假产物。
 */
function CaseArtifactsPanel({ runId, caseId }: { runId: string; caseId: string }) {
  const query = useQuery({
    queryKey: ["case-artifacts", runId, caseId],
    queryFn: () => api.caseArtifacts(runId, caseId),
  });
  const [preview, setPreview] = React.useState<CaseArtifactRow | null>(null);
  const [showUnavailable, setShowUnavailable] = React.useState(false);
  const content = useQuery({
    queryKey: ["case-artifact", runId, caseId, preview?.name, preview?.iteration],
    queryFn: () => api.caseArtifact(runId, caseId, preview!.name, preview!.iteration),
    enabled: Boolean(preview),
    retry: false,
  });

  const unavailable = Object.entries(query.data?.unavailable ?? {});

  return (
    <div className="space-y-2 border-t border-border pt-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-[11px] tracking-wide text-muted-foreground uppercase">
          case 现场产物（PRD §90）
        </div>
        {unavailable.length > 0 && (
          <Button size="sm" variant="ghost" onClick={() => setShowUnavailable((v) => !v)}>
            {showUnavailable ? "隐藏" : "采集能力表"}
          </Button>
        )}
      </div>

      <QueryState
        isLoading={query.isLoading}
        error={query.error}
        isEmpty={query.data?.items.length === 0}
        emptyTitle="没有采集到产物"
        emptyHint="该 run 以 --no-save-artifacts 运行，或 case 未产生可采集的现场。"
      >
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Iter</TableHead>
              <TableHead>产物</TableHead>
              <TableHead>类型</TableHead>
              <TableHead>大小</TableHead>
              <TableHead>case_run_id</TableHead>
              <TableHead>说明</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {query.data?.items.map((item) => (
              <TableRow key={`${item.case_run_id}-${item.name}`}>
                <TableCell className="tabular text-xs">{item.iteration}</TableCell>
                <TableCell className="font-mono text-xs">{item.name}</TableCell>
                <TableCell className="text-xs text-muted-foreground">{item.kind}</TableCell>
                <TableCell className="tabular text-xs">
                  {fmtBytes(item.bytes)}
                  {item.truncated && (
                    <Badge variant="warn" className="ml-2">
                      截断
                    </Badge>
                  )}
                </TableCell>
                <TableCell className="font-mono text-[11px] text-muted-foreground">
                  {item.case_run_id}
                </TableCell>
                <TableCell className="max-w-sm text-xs text-muted-foreground">
                  {item.note ?? "—"}
                </TableCell>
                <TableCell>
                  <div className="flex items-center justify-end gap-1">
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => setPreview(item)}
                      title="文本预览；二进制请用下载"
                    >
                      <FileJson2 /> 查看
                    </Button>
                    <Button size="sm" variant="ghost" asChild>
                      <a href={item.raw_url} target="_blank" rel="noreferrer">
                        <ExternalLink /> 原文
                      </a>
                    </Button>
                    <Button size="sm" variant="ghost" asChild>
                      <a href={item.raw_url} download={item.name.split("/").pop()}>
                        <Download />
                      </a>
                    </Button>
                  </div>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </QueryState>

      {(query.data?.notes.length ?? 0) > 0 && (
        <div className="space-y-1 rounded-md border border-[var(--warn)]/40 bg-[var(--warn)]/5 p-2">
          <div className="text-[11px] text-[var(--warn)]">
            采集记账（有内容 = 这次少了某件产物，不是"本来就没有"）
          </div>
          {query.data?.notes.map((note, index) => (
            <div key={index} className="font-mono text-[11px] text-muted-foreground">
              {note}
            </div>
          ))}
        </div>
      )}

      {showUnavailable && (
        <div className="space-y-1 rounded-md border border-border bg-muted/20 p-2">
          <div className="text-[11px] text-muted-foreground">
            采集能力表（Spec §21.1）：以下类型的观测面当前采不到，故不落空文件占位。
          </div>
          {unavailable.map(([kind, reason]) => (
            <div key={kind} className="text-[11px]">
              <span className="font-mono text-muted-foreground">{kind}</span>
              <span className="mx-1">·</span>
              <span className="text-muted-foreground">{reason}</span>
            </div>
          ))}
        </div>
      )}

      {preview && (
        <div className="space-y-1">
          <div className="text-[11px] text-muted-foreground">
            预览 · <span className="font-mono">{preview.name}</span>（iter{preview.iteration}）
          </div>
          {content.error ? (
            <p className="text-xs text-[var(--warn)]">
              无法预览：{content.error.message}（用"原文 / 下载"取原文件）
            </p>
          ) : (
            <QueryState isLoading={content.isLoading} error={null}>
              <pre className="max-h-[40vh] overflow-auto rounded-md border border-border bg-muted/30 p-3 font-mono text-xs whitespace-pre-wrap">
                {content.data?.truncated ? `${content.data.text}\n\n… 已截断（预览上限）` : content.data?.text}
              </pre>
            </QueryState>
          )}
        </div>
      )}
    </div>
  );
}

function TraceTab({ runId }: { runId: string }) {
  const cases = useQuery({ queryKey: ["run-cases", runId], queryFn: () => api.runCases(runId) });
  const [caseId, setCaseId] = React.useState<string | null>(null);
  const [iteration, setIteration] = React.useState(1);

  React.useEffect(() => {
    if (!caseId && cases.data?.length) setCaseId(cases.data[0].case_id);
  }, [cases.data, caseId]);

  const trace = useQuery({
    queryKey: ["trace", runId, caseId, iteration],
    queryFn: () => api.trace(runId, caseId!, iteration),
    enabled: Boolean(caseId),
  });
  const index = useQuery({
    queryKey: ["traces", runId],
    queryFn: () => api.traces({ run_id: runId, limit: 500 }),
  });

  const iterationsForCase = index.data?.filter((row) => row.case_id === caseId) ?? [];

  return (
    <Section
      title="Trace Viewer（PRD §76）"
      description="Span Tree 由实例落盘的 raw trace 重放得出；case 级判定挂在根节点（Spec §2.2 的挂载点不属于单个 span）。"
      actions={
        <div className="flex items-center gap-2">
          <Select value={caseId ?? ""} onValueChange={setCaseId}>
            <SelectTrigger className="w-64">
              <SelectValue placeholder="选择 case" />
            </SelectTrigger>
            <SelectContent>
              {cases.data?.map((row) => (
                <SelectItem key={row.case_id} value={row.case_id}>
                  {row.case_id}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Select value={String(iteration)} onValueChange={(value) => setIteration(Number(value))}>
            <SelectTrigger className="w-28">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {(iterationsForCase.length
                ? iterationsForCase.map((row) => row.iteration)
                : [1]
              ).map((value) => (
                <SelectItem key={value} value={String(value)}>
                  iter {value}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      }
    >
      {trace.data && (
        <div className="mb-3 flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
          <span>{trace.data.span_count} spans</span>
          <span>·</span>
          <span className="tabular">{fmtTokens(trace.data.tokens)} tokens</span>
          {trace.data.tool_sequence.length > 0 && (
            <>
              <span>·</span>
              <span className="font-mono">{trace.data.tool_sequence.join(" → ")}</span>
            </>
          )}
        </div>
      )}
      <QueryState isLoading={trace.isLoading} error={trace.error} onRetry={() => void trace.refetch()}>
        <TraceTree
          root={trace.data?.root ?? null}
          empty="该 iteration 没有保存 trace（run 时未开启 --save-trace）"
        />
      </QueryState>
    </Section>
  );
}

function FailuresTab({ runId }: { runId: string }) {
  const failures = useQuery({
    queryKey: ["run-failures", runId],
    queryFn: () => api.failuresOfRun(runId),
  });
  const clusters = useQuery({
    queryKey: ["run-clusters", runId],
    queryFn: () => api.clusters(runId),
  });

  return (
    <div className="space-y-4">
      <Section
        title="Failure 分类（PRD §47/§48 规则引擎）"
        description="分类是确定性的：同一个 run 每次请求得到同一结果，不依赖 LLM。"
      >
        <QueryState
          isLoading={failures.isLoading}
          error={failures.error}
          isEmpty={failures.data?.total === 0}
          emptyTitle="没有 failure"
          emptyHint="该 run 的所有判定都通过了。"
        >
          {failures.data && (
            <div className="space-y-3">
              <div className="flex flex-wrap gap-1.5">
                {Object.entries(failures.data.by_parent).map(([parent, count]) => (
                  <Badge key={parent} variant="fail">
                    {parent} · {count}
                  </Badge>
                ))}
              </div>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Case</TableHead>
                    <TableHead>Iter</TableHead>
                    <TableHead>Category</TableHead>
                    <TableHead>Parent</TableHead>
                    <TableHead>Metric</TableHead>
                    <TableHead>Source</TableHead>
                    <TableHead>Reason</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {failures.data.failures.map((failure) => (
                    <TableRow key={failure.failure_id}>
                      <TableCell className="font-mono text-xs">{failure.case_id}</TableCell>
                      <TableCell className="tabular text-xs">{failure.iteration}</TableCell>
                      <TableCell>
                        <Badge variant="fail">{failure.category}</Badge>
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground">{failure.parent}</TableCell>
                      <TableCell className="font-mono text-xs">{failure.metric ?? "—"}</TableCell>
                      <TableCell className="text-xs text-muted-foreground">{failure.source}</TableCell>
                      <TableCell className="max-w-md truncate text-xs" title={failure.reason ?? ""}>
                        {failure.reason ?? "—"}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </QueryState>
      </Section>

      <Section
        title="Failure Clusters（PRD §49/§50）"
        description="按 (工具序列, 归一化错误) 聚类，每组给一个代表 case。"
      >
        <QueryState
          isLoading={clusters.isLoading}
          error={clusters.error}
          isEmpty={clusters.data?.total_clusters === 0}
          emptyTitle="没有可聚类的 failure"
        >
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Cluster</TableHead>
                <TableHead>Category</TableHead>
                <TableHead>Size</TableHead>
                <TableHead>Representative</TableHead>
                <TableHead>Tool sequence</TableHead>
                <TableHead>Common error</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {clusters.data?.clusters.map((cluster) => (
                <TableRow key={cluster.cluster_id}>
                  <TableCell className="font-mono text-xs">{cluster.cluster_id}</TableCell>
                  <TableCell>
                    <Badge variant="fail">{cluster.category}</Badge>
                  </TableCell>
                  <TableCell className="tabular">{cluster.size}</TableCell>
                  <TableCell className="font-mono text-xs">
                    {cluster.representative_case_id}
                  </TableCell>
                  <TableCell className="font-mono text-xs text-muted-foreground">
                    {cluster.common_tool_sequence || "—"}
                  </TableCell>
                  <TableCell
                    className="max-w-md truncate text-xs text-muted-foreground"
                    title={cluster.common_error ?? ""}
                  >
                    {cluster.common_error ?? "—"}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </QueryState>
      </Section>
    </div>
  );
}

function GateTab({ runId }: { runId: string }) {
  const rulesets = useQuery({ queryKey: ["gate-rules"], queryFn: api.gateRules });
  const [gate, setGate] = React.useState("pr");
  const query = useQuery({
    queryKey: ["gate", runId, gate],
    queryFn: () => api.gate(runId, gate),
  });

  return (
    <Section
      title="Gate 结论（Spec §6.2 gate.json）"
      description="source=stored 表示当时落盘的判定；source=replayed 表示用当前规则集重放（规则改过就会出现）。"
      actions={
        <Select value={gate} onValueChange={setGate}>
          <SelectTrigger className="w-36">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {(rulesets.data?.map((row) => row.gate) ?? ["pr", "main", "release"]).map((name) => (
              <SelectItem key={name} value={name}>
                {name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      }
    >
      <QueryState isLoading={query.isLoading} error={query.error} onRetry={() => void query.refetch()}>
        {query.data && (
          <div className="space-y-4">
            <div className="flex flex-wrap items-center gap-3">
              <VerdictBadge verdict={query.data.verdict} />
              <ExitCodeBadge code={query.data.exit_code} />
              <Badge variant={query.data.source === "stored" ? "neutral" : "warn"}>
                {query.data.source === "stored" ? "当时判定" : "重放结果"}
              </Badge>
              <BaselineBadge mode={query.data.baseline_mode} />
            </div>

            {query.data.notes.length > 0 && (
              <div className="space-y-1 rounded-md border border-border bg-muted/40 px-3 py-2">
                {query.data.notes.map((note) => (
                  <div key={note} className="text-xs text-muted-foreground">
                    {note}
                  </div>
                ))}
              </div>
            )}

            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Rule</TableHead>
                  <TableHead>Observed</TableHead>
                  <TableHead>Threshold</TableHead>
                  <TableHead>Verdict</TableHead>
                  <TableHead>Blocking</TableHead>
                  <TableHead>Detail</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {query.data.rules.map((rule) => (
                  <TableRow key={rule.rule}>
                    <TableCell className="font-mono text-xs">{rule.rule}</TableCell>
                    <TableCell className="tabular text-xs">{fmtNumber(rule.observed, 4)}</TableCell>
                    <TableCell className="tabular text-xs">{fmtNumber(rule.threshold, 4)}</TableCell>
                    <TableCell>
                      <VerdictBadge verdict={rule.verdict} />
                    </TableCell>
                    <TableCell>
                      {rule.blocking ? (
                        <Badge variant="fail">blocking</Badge>
                      ) : (
                        <span className="text-xs text-muted-foreground">no</span>
                      )}
                    </TableCell>
                    <TableCell className="max-w-lg text-xs text-muted-foreground">
                      {rule.detail || "—"}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>

            <KeyValue
              columns={4}
              items={Object.entries(query.data.aggregate).map(([key, value]) => ({
                label: key,
                value: String(value),
              }))}
            />
          </div>
        )}
      </QueryState>
    </Section>
  );
}

function ArtifactsTab({ runId }: { runId: string }) {
  const query = useQuery({ queryKey: ["artifacts", runId], queryFn: () => api.runArtifacts(runId) });
  const [preview, setPreview] = React.useState<string | null>(null);
  const content = useQuery({
    queryKey: ["artifact", runId, preview],
    queryFn: () => api.artifact(runId, preview!),
    enabled: Boolean(preview),
  });

  return (
    <div className="grid gap-4 xl:grid-cols-[420px_minmax(0,1fr)]">
      <Section
        title="产物（Spec §6.2）"
        description="五个文件由同一个 RunAggregate 一次写入；从各自路径分别推导计数是被禁止的。"
      >
        <QueryState
          isLoading={query.isLoading}
          error={query.error}
          isEmpty={query.data?.length === 0}
          emptyTitle="没有产物"
          emptyHint="run 尚未完成写入，或产物已被清理。"
        >
          <div className="space-y-2">
            {query.data?.map((artifact) => (
              <div
                key={artifact.name}
                className="flex items-center justify-between gap-2 rounded-md border border-border px-3 py-2"
              >
                <div className="min-w-0">
                  <div className="font-mono text-xs">{artifact.name}</div>
                  <div className="text-[11px] text-muted-foreground">
                    {fmtBytes(artifact.bytes)} · {artifact.content_type}
                  </div>
                </div>
                <div className="flex shrink-0 items-center gap-1">
                  {artifact.name !== "report.html" && (
                    <Button
                      size="sm"
                      variant={preview === artifact.name ? "secondary" : "ghost"}
                      onClick={() => setPreview(artifact.name)}
                    >
                      <FileJson2 /> 查看
                    </Button>
                  )}
                  <Button size="sm" variant="ghost" asChild>
                    <a href={`${artifact.url}/raw`} target="_blank" rel="noreferrer">
                      <ExternalLink /> 原文
                    </a>
                  </Button>
                  <Button size="sm" variant="ghost" asChild>
                    <a href={`${artifact.url}/raw`} download={artifact.name}>
                      <Download />
                    </a>
                  </Button>
                </div>
              </div>
            ))}
          </div>
        </QueryState>
      </Section>

      <Section title={preview ? `预览 · ${preview}` : "预览"} description={preview ? undefined : "选一个产物查看内容。"}>
        {!preview ? (
          <p className="text-sm text-muted-foreground">未选择产物。</p>
        ) : (
          <QueryState isLoading={content.isLoading} error={content.error}>
            <pre className="max-h-[70vh] overflow-auto rounded-md border border-border bg-muted/30 p-3 font-mono text-xs whitespace-pre-wrap">
              {content.data?.text}
            </pre>
          </QueryState>
        )}
      </Section>
    </div>
  );
}

function ConfigTab({ overview }: { overview: RunOverview }) {
  const run = overview.run;
  const capability = Object.entries(run.metric_capability_snapshot);
  const degradations = Object.entries(run.metric_degradations);

  return (
    <div className="space-y-4">
      <Section title="运行配置（PRD §30/§109.3 可复现性）">
        <KeyValue
          columns={3}
          items={[
            { label: "Benchmark", value: run.benchmark_id },
            { label: "Dataset", value: <Mono>{`${run.dataset_id}@${run.dataset_version}`}</Mono> },
            { label: "Dataset hash", value: <Mono>{run.dataset_hash.slice(0, 16)}</Mono> },
            { label: "Profile", value: <Mono>{run.profile}</Mono> },
            { label: "Suite", value: run.suite ?? "—" },
            { label: "Tag filter", value: run.tag_filter.join(", ") || "—" },
            { label: "Agent endpoint", value: <Mono>{run.agent_endpoint || "—"}</Mono> },
            { label: "Agent model", value: run.agent_model ?? "—" },
            { label: "Judge model", value: run.judge_model ?? "—" },
            { label: "No judge", value: run.no_judge ? "是" : "否" },
            { label: "Git branch", value: run.git_branch ?? "—" },
            { label: "Git commit", value: <Mono>{shortCommit(run.git_commit)}</Mono> },
            {
              label: "Git dirty",
              value:
                run.git_dirty === null ? "—" : run.git_dirty ? "有未提交改动" : "干净",
            },
            { label: "Environment", value: run.environment },
            { label: "Eval platform", value: <Mono>{run.eval_platform_version}</Mono> },
            { label: "DeepEval", value: <Mono>{run.deepeval_version ?? "—"}</Mono> },
            {
              label: "Status",
              value: <RunStatusBadge status={run.status} />,
            },
            { label: "Started", value: fmtRelative(run.started_at) },
            { label: "Finished", value: fmtRelative(run.finished_at) },
          ]}
        />
      </Section>

      <div className="grid gap-4 lg:grid-cols-2">
        <Section
          title="Metric 能力快照（Spec §7.3）"
          description="本次 run 探测到的 provider 能力；fallback 只在能力缺失时启用。"
        >
          {capability.length === 0 ? (
            <p className="text-sm text-muted-foreground">未记录（P0 前产物）。</p>
          ) : (
            <div className="flex flex-wrap gap-1.5">
              {capability.map(([metric, available]) => (
                <Badge key={metric} variant={available ? "pass" : "neutral"}>
                  {metric}
                </Badge>
              ))}
            </div>
          )}
        </Section>

        <Section
          title="降级记录（Spec §7.4）"
          description="原 metric → 实际生效的 fallback。降级是显式记账的，不是静默替换。"
        >
          {degradations.length === 0 ? (
            <p className="text-sm text-muted-foreground">本次 run 没有降级。</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Declared</TableHead>
                  <TableHead>Effective</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {degradations.map(([declared, effective]) => (
                  <TableRow key={declared}>
                    <TableCell className="font-mono text-xs">{declared}</TableCell>
                    <TableCell className="font-mono text-xs text-muted-foreground">
                      {effective || "skipped"}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </Section>
      </div>

      <Section title="比较上下文">
        <KeyValue
          columns={3}
          items={[
            { label: "Baseline policy", value: run.baseline_policy ?? "—" },
            { label: "Baseline mode", value: baselineLabel(run.baseline_mode) },
            {
              label: "Baseline run",
              value: run.baseline_run_id ? (
                <Link to={`/runs/${run.baseline_run_id}`} className="font-mono text-xs text-primary hover:underline">
                  {run.baseline_run_id}
                </Link>
              ) : (
                "—"
              ),
            },
            { label: "Baseline reason", value: run.baseline_reason ?? "—" },
            { label: "Experiment", value: run.experiment_id ?? "—" },
            { label: "Variant", value: run.variant_id ?? "—" },
            { label: "Run status", value: runStatusLabel(run.status) },
            {
              label: "Verdict",
              value: <VerdictBadge verdict={overview.verdict} />,
            },
          ]}
        />
      </Section>

      {overview.metrics && (
        <Section title="Diff 提示" description="与基线比较时的方向语义：成本/性能类指标数值下降才是改善。">
          <div className="flex flex-wrap items-center gap-2 text-xs">
            {(["tokens", "tool_calls", "latency_ms", "cost"] as const).map((metric) => (
              <span key={metric} className="flex items-center gap-1">
                <span className="font-mono">{metric}</span>
                <MetricDiffBadge verdict={metric === "cost" ? "undetermined" : "unchanged"} />
              </span>
            ))}
            <span className="text-muted-foreground">
              实际判定见 Regression 视图（需有 baseline）。
            </span>
          </div>
        </Section>
      )}
    </div>
  );
}
