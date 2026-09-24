import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";

import { PassRate, StabilityBadge } from "@/components/common/badges";
import { Mono, PageHeader, Section } from "@/components/common/primitives";
import { QueryState } from "@/components/common/states";
import { TraceTree } from "@/components/common/TraceTree";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api } from "@/lib/api";
import { fmtBytes, fmtTokens, shortId } from "@/lib/format";

export function TraceViewer() {
  const [runId, setRunId] = React.useState<string>("");
  const runs = useQuery({
    queryKey: ["runs", { limit: 100 }],
    queryFn: () => api.runs({ limit: 100 }),
  });
  const index = useQuery({
    queryKey: ["traces", runId || "all"],
    queryFn: () => api.traces({ run_id: runId || undefined, limit: 500 }),
  });

  React.useEffect(() => {
    if (!runId && runs.data?.length) setRunId(runs.data[0].run_id);
  }, [runs.data, runId]);

  const [selected, setSelected] = React.useState<{ caseId: string; iteration: number } | null>(null);
  React.useEffect(() => {
    setSelected(null);
  }, [runId]);

  const trace = useQuery({
    queryKey: ["trace", runId, selected?.caseId, selected?.iteration],
    queryFn: () => api.trace(runId, selected!.caseId, selected!.iteration),
    enabled: Boolean(runId && selected),
  });

  return (
    <div className="space-y-5">
      <PageHeader
        title="Trace Viewer"
        description="PRD §76：Agent → LLM / Tool / SubAgent 的调用树。数据来自 run 时落盘的 raw trace（PRD §52），可直接回放。"
        actions={
          <Select value={runId} onValueChange={setRunId}>
            <SelectTrigger className="w-72">
              <SelectValue placeholder="选择 run" />
            </SelectTrigger>
            <SelectContent>
              {runs.data?.map((run) => (
                <SelectItem key={run.run_id} value={run.run_id}>
                  {shortId(run.run_id)} · {run.benchmark_id}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        }
      />

      <QueryState
        isLoading={index.isLoading}
        error={index.error}
        isEmpty={index.data?.length === 0}
        emptyTitle="没有可回放的 trace"
        emptyHint="run 时未保存 trace 时不会有 raw trace：检查 run 是否带 --save-trace。"
        onRetry={() => void index.refetch()}
      >
        <div className="grid gap-4 xl:grid-cols-[380px_minmax(0,1fr)]">
          <Section title="Trace 索引" description={`${index.data?.length ?? 0} 个 (case, iteration)`}>
            <div className="max-h-[70vh] space-y-1 overflow-y-auto">
              {index.data?.map((row) => {
                const active =
                  selected?.caseId === row.case_id && selected?.iteration === row.iteration;
                return (
                  <button
                    key={`${row.case_id}-${row.iteration}`}
                    type="button"
                    onClick={() => setSelected({ caseId: row.case_id, iteration: row.iteration })}
                    className={`flex w-full items-center justify-between gap-2 rounded-md border px-3 py-2 text-left text-xs transition-colors ${
                      active
                        ? "border-primary/50 bg-accent"
                        : "border-border hover:bg-muted/50"
                    }`}
                  >
                    <span className="min-w-0">
                      <span className="block truncate font-mono">{row.case_id}</span>
                      <span className="text-muted-foreground">iter {row.iteration}</span>
                    </span>
                    <span className="shrink-0 tabular text-muted-foreground">
                      {fmtBytes(row.bytes)}
                    </span>
                  </button>
                );
              })}
            </div>
          </Section>

          <Section
            title={selected ? `Span Tree · ${selected.caseId} (iter ${selected.iteration})` : "Span Tree"}
            description={
              trace.data
                ? `${trace.data.span_count} spans · ${fmtTokens(trace.data.tokens)} tokens`
                : "从左侧选择一个 iteration。"
            }
          >
            {!selected ? (
              <p className="text-sm text-muted-foreground">未选择 trace。</p>
            ) : (
              <Tabs defaultValue="tree">
                <TabsList>
                  <TabsTrigger value="tree">Span Tree</TabsTrigger>
                  <TabsTrigger value="events">Raw Events</TabsTrigger>
                </TabsList>
                <TabsContent value="tree">
                  <QueryState
                    isLoading={trace.isLoading}
                    error={trace.error}
                    onRetry={() => void trace.refetch()}
                  >
                    {trace.data?.tool_sequence.length ? (
                      <div className="mb-3 flex flex-wrap gap-1">
                        {trace.data.tool_sequence.map((tool, index) => (
                          <Badge key={`${tool}-${index}`} variant="outline">
                            {tool}
                          </Badge>
                        ))}
                      </div>
                    ) : null}
                    <TraceTree root={trace.data?.root ?? null} />
                  </QueryState>
                </TabsContent>
                <TabsContent value="events">
                  <RawEvents runId={runId} caseId={selected.caseId} iteration={selected.iteration} />
                </TabsContent>
              </Tabs>
            )}
          </Section>
        </div>
      </QueryState>

      <Section title="全部 Run 快速入口" description="近 100 次 run。">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Run</TableHead>
              <TableHead>Benchmark</TableHead>
              <TableHead>Status</TableHead>
              <TableHead className="text-right" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {runs.data?.slice(0, 30).map((run) => (
              <TableRow key={run.run_id}>
                <TableCell className="font-mono text-xs">{shortId(run.run_id)}</TableCell>
                <TableCell>{run.benchmark_id}</TableCell>
                <TableCell className="text-xs">{run.status}</TableCell>
                <TableCell className="text-right">
                  <Button size="sm" variant="ghost" asChild>
                    <Link to={`/runs/${run.run_id}`}>详情</Link>
                  </Button>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </Section>
    </div>
  );
}

function RawEvents({
  runId,
  caseId,
  iteration,
}: {
  runId: string;
  caseId: string;
  iteration: number;
}) {
  const query = useQuery({
    queryKey: ["trace-events", runId, caseId, iteration],
    queryFn: () => api.traceEvents(runId, caseId, iteration),
  });

  return (
    <QueryState
      isLoading={query.isLoading}
      error={query.error}
      isEmpty={query.data?.count === 0}
      emptyTitle="没有事件"
      emptyHint={query.data?.hint ?? undefined}
    >
      {query.data && (
        <div className="space-y-1">
          <div className="text-xs text-muted-foreground">
            {query.data.count} events · <Mono>{query.data.trace_id ?? "—"}</Mono>
          </div>
          <div className="max-h-[65vh] divide-y divide-border overflow-y-auto rounded-lg border border-border">
            {query.data.events.map((event) => (
              <EventRow key={event.event_id} event={event} />
            ))}
          </div>
        </div>
      )}
    </QueryState>
  );
}

function EventRow({ event }: { event: import("@/lib/api-types").TraceEventRow }) {
  return (
    <Collapsible>
      <CollapsibleTrigger className="flex w-full items-center justify-between gap-3 px-3 py-1.5 text-left hover:bg-muted/50">
        <span className="flex min-w-0 items-center gap-2">
          <Badge variant="outline">{event.type}</Badge>
          <span className="truncate font-mono text-[11px] text-muted-foreground">
            {event.event_id}
          </span>
        </span>
        <span className="shrink-0 text-[11px] text-muted-foreground">
          {event.timestamp ? new Date(event.timestamp).toLocaleTimeString("zh-CN", { hour12: false }) : "—"}
        </span>
      </CollapsibleTrigger>
      <CollapsibleContent>
        <pre className="overflow-x-auto border-t border-border bg-muted/30 px-3 py-2 font-mono text-[11px] whitespace-pre-wrap">
          {JSON.stringify(event.data, null, 2)}
        </pre>
      </CollapsibleContent>
    </Collapsible>
  );
}

export function TraceStatsBadge({ stability, passRate }: { stability: string; passRate: number }) {
  return (
    <span className="flex items-center gap-2">
      <StabilityBadge stability={stability} />
      <PassRate value={passRate} />
    </span>
  );
}
