import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";
import { ArrowRight, GitCompareArrows } from "lucide-react";

import {
  BaselineBadge,
  MetricDiffBadge,
  RegressionBadge,
  StabilityBadge,
} from "@/components/common/badges";
import { KeyValue, Mono, PageHeader, Section } from "@/components/common/primitives";
import { EmptyState, ErrorState, Loading } from "@/components/common/states";
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
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api, ApiError } from "@/lib/api";
import type { RegressionAnalysis } from "@/lib/api-types";
import { fmtDelta, fmtNumber, fmtPercentDelta, fmtRelative, shortId } from "@/lib/format";

export function Regression() {
  const runs = useQuery({
    queryKey: ["runs", { limit: 200 }],
    queryFn: () => api.runs({ limit: 200 }),
  });
  const benchmark = React.useState("all");
  const [benchmarkValue] = benchmark;
  const withBaseline = (runs.data ?? []).filter(
    (run) => run.baseline_run_id && (benchmarkValue === "all" || run.benchmark_id === benchmarkValue),
  );
  const [selected, setSelected] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!selected && withBaseline.length) setSelected(withBaseline[0].run_id);
  }, [withBaseline, selected]);

  return (
    <div className="space-y-5">
      <PageHeader
        title="Regression"
        description="PRD §77 左右对比。方向语义：质量类指标上升为改善；tokens / tool_calls / latency / cost 上升是退步——用更多资源跑出同样结果是退步。"
      />

      <div className="grid gap-4 xl:grid-cols-[340px_minmax(0,1fr)]">
        <Section
          title="有基线的 Run"
          description="只列出记录了 baseline_run_id 的 run；NO_BASELINE 的 run 没有可比对象。"
        >
          {runs.isLoading ? (
            <Loading />
          ) : runs.error ? (
            <ErrorState error={runs.error} onRetry={() => void runs.refetch()} />
          ) : withBaseline.length === 0 ? (
            <EmptyState
              title="没有带基线的 run"
              emptyHint="先 pin 一个 baseline（`agent-eval baseline pin <run> --benchmark <name> --mode release`），或跑第二次让 main-latest 解析生效。"
            />
          ) : (
            <div className="max-h-[70vh] space-y-1 overflow-y-auto">
              {withBaseline.map((run) => (
                <button
                  key={run.run_id}
                  type="button"
                  onClick={() => setSelected(run.run_id)}
                  className={`w-full rounded-md border px-3 py-2 text-left text-xs transition-colors ${
                    selected === run.run_id
                      ? "border-primary/50 bg-accent"
                      : "border-border hover:bg-muted/50"
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono">{shortId(run.run_id)}</span>
                    <BaselineBadge mode={run.baseline_mode} />
                  </div>
                  <div className="mt-0.5 text-muted-foreground">
                    {run.benchmark_id} · {fmtRelative(run.started_at)}
                  </div>
                </button>
              ))}
            </div>
          )}
        </Section>

        <div className="min-w-0 space-y-4">
          {!selected ? (
            <EmptyState title="选择一个 run" emptyHint="右侧显示它与基线的逐项对比。" />
          ) : (
            <RegressionDetail runId={selected} />
          )}
        </div>
      </div>
    </div>
  );
}

function RegressionDetail({ runId }: { runId: string }) {
  const query = useQuery({
    queryKey: ["regression", runId],
    queryFn: () => api.regressionOfRun(runId),
  });

  if (query.isLoading) return <Loading />;
  if (query.isError) {
    // 409 = 该 run 没有 baseline（Spec §4.3 降级），是可解释状态而不是故障
    if (query.error instanceof ApiError && query.error.status === 409) {
      return (
        <Section title="无可比基线">
          <div className="space-y-2 text-sm">
            <p>{query.error.message}</p>
            <p className="text-xs text-muted-foreground">
              Spec §4.3：NO_BASELINE 下 run 正常执行，但所有回归判定为 UNDETERMINED，
              Gate 降级为绝对阈值。这里不合成一个"看起来正常"的对比。
            </p>
          </div>
        </Section>
      );
    }
    return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  }
  const data = query.data!;

  if (!data.valid) {
    return (
      <Section
        title="比较无效（Spec §1.2-3）"
        actions={<Badge variant="fail">INVALID</Badge>}
      >
        <p className="text-sm">{data.invalid_reason}</p>
        <p className="mt-2 text-xs text-muted-foreground">
          两侧 dataset_version 或 benchmark 不一致时，禁止产出 REGRESSION —— 这不是退步，
          而是"不可比"。请用同一 dataset version 重跑候选。
        </p>
      </Section>
    );
  }

  return (
    <div className="space-y-4">
      <Section
        title="比较对象"
        actions={
          <div className="flex items-center gap-2">
            <Button size="sm" variant="ghost" asChild>
              <Link to={`/runs/${data.baseline_run_id}`}>
                基线 <ArrowRight />
              </Link>
            </Button>
            <Button size="sm" variant="ghost" asChild>
              <Link to={`/runs/${data.candidate_run_id}`}>
                候选 <ArrowRight />
              </Link>
            </Button>
          </div>
        }
      >
        <KeyValue
          columns={3}
          items={[
            { label: "Baseline", value: <Mono>{data.baseline_run_id ?? "—"}</Mono> },
            { label: "Candidate", value: <Mono>{data.candidate_run_id}</Mono> },
            { label: "Baseline mode", value: <BaselineBadge mode={data.baseline_mode} /> },
            {
              label: "Cases",
              value: `${data.counts.cases ?? 0}（退步 ${data.counts.regression ?? 0} / 进步 ${
                data.counts.improved ?? 0
              } / 波动 ${data.counts.flaky ?? 0}）`,
            },
            {
              label: "无法判定",
              value: String(data.counts.undetermined ?? 0),
            },
          ]}
        />
      </Section>

      <Tabs defaultValue="metric">
        <TabsList>
          <TabsTrigger value="metric">Metric Diff</TabsTrigger>
          <TabsTrigger value="cases">Case Diff</TabsTrigger>
          <TabsTrigger value="performance">Performance</TabsTrigger>
          <TabsTrigger value="failures">Failure Category</TabsTrigger>
          <TabsTrigger value="trace">Trace Diff</TabsTrigger>
        </TabsList>

        <TabsContent value="metric">
          <Section
            title="Run-level Metric Diff（PRD §105）"
            description="只比对两侧都存在的 metric；verdict 已按「该指标越大越好还是越小越好」判定。"
          >
            {data.metric_diffs.length === 0 ? (
              <EmptyState title="没有可比的 metric" />
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Metric</TableHead>
                    <TableHead>Baseline</TableHead>
                    <TableHead>Candidate</TableHead>
                    <TableHead>Δ</TableHead>
                    <TableHead>Δ%</TableHead>
                    <TableHead>Verdict</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.metric_diffs.map((diff) => (
                    <TableRow key={diff.metric}>
                      <TableCell className="font-mono text-xs">{diff.metric}</TableCell>
                      <TableCell className="tabular">{fmtNumber(diff.baseline, 4)}</TableCell>
                      <TableCell className="tabular">{fmtNumber(diff.candidate, 4)}</TableCell>
                      <TableCell className="tabular">{fmtDelta(diff.delta, 4)}</TableCell>
                      <TableCell className="tabular">
                        {fmtPercentDelta(diff.delta_percent)}
                      </TableCell>
                      <TableCell>
                        <MetricDiffBadge verdict={diff.verdict} />
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </Section>
        </TabsContent>

        <TabsContent value="cases">
          <Section title="Case Diff（PRD §54 判定表）">
            {data.cases.length === 0 ? (
              <EmptyState title="没有 case 级差异" />
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Case</TableHead>
                    <TableHead>Baseline</TableHead>
                    <TableHead>Candidate</TableHead>
                    <TableHead>State</TableHead>
                    <TableHead>Note</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.cases.map((row) => (
                    <TableRow key={row.case_id}>
                      <TableCell className="font-mono text-xs">{row.case_id}</TableCell>
                      <TableCell>
                        <StabilityBadge stability={row.baseline_stability} />
                      </TableCell>
                      <TableCell>
                        <StabilityBadge stability={row.candidate_stability} />
                      </TableCell>
                      <TableCell>
                        <RegressionBadge state={row.state} />
                      </TableCell>
                      <TableCell className="max-w-sm truncate text-xs text-muted-foreground">
                        {row.invalid_reason ?? "—"}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </Section>
        </TabsContent>

        <TabsContent value="performance">
          <Section
            title="Performance Diff（PRD §55）"
            description="超出阈值即视为性能退步——即使 case 仍然 PASS。"
          >
            {data.performance.length === 0 ? (
              <EmptyState title="没有性能差异数据" />
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Case</TableHead>
                    <TableHead>Metric</TableHead>
                    <TableHead>Baseline</TableHead>
                    <TableHead>Candidate</TableHead>
                    <TableHead>Δ%</TableHead>
                    <TableHead>Threshold</TableHead>
                    <TableHead>Regressed</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.performance.map((row, index) => (
                    <TableRow key={`${row.case_id}-${row.metric}-${index}`}>
                      <TableCell className="font-mono text-xs">{row.case_id ?? "—"}</TableCell>
                      <TableCell className="font-mono text-xs">{row.metric}</TableCell>
                      <TableCell className="tabular">{fmtNumber(row.baseline, 2)}</TableCell>
                      <TableCell className="tabular">{fmtNumber(row.candidate, 2)}</TableCell>
                      <TableCell className="tabular">{fmtPercentDelta(row.delta_percent)}</TableCell>
                      <TableCell className="tabular text-xs text-muted-foreground">
                        {row.threshold_percent === null ? "—" : `${row.threshold_percent}%`}
                      </TableCell>
                      <TableCell>
                        {row.regressed ? (
                          <Badge variant="fail">是</Badge>
                        ) : (
                          <Badge variant="neutral">否</Badge>
                        )}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </Section>
        </TabsContent>

        <TabsContent value="failures">
          <Section title="Failure Category Diff（PRD §105）">
            {data.failure_categories.length === 0 ? (
              <EmptyState title="两侧都没有 failure" />
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Category</TableHead>
                    <TableHead>Baseline</TableHead>
                    <TableHead>Candidate</TableHead>
                    <TableHead>Δ</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.failure_categories.map((row) => (
                    <TableRow key={row.category}>
                      <TableCell className="font-mono text-xs">{row.category}</TableCell>
                      <TableCell className="tabular">{row.baseline}</TableCell>
                      <TableCell className="tabular">{row.candidate}</TableCell>
                      <TableCell>
                        <Badge variant={row.delta > 0 ? "fail" : row.delta < 0 ? "pass" : "neutral"}>
                          {row.delta > 0 ? `+${row.delta}` : row.delta}
                        </Badge>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </Section>
        </TabsContent>

        <TabsContent value="trace">
          <TraceDiffSection data={data} />
        </TabsContent>
      </Tabs>
    </div>
  );
}

function TraceDiffSection({ data }: { data: RegressionAnalysis }) {
  // `changed` 为假但带 semantic_equal / diff_notes 的 case 也要展示：前者是
  // "文字不同但语义相同"（Spec §20.2），后者是"这次结论有低置信部分"。
  // 只按 changed 过滤会让这两类信息消失，用户看到空表会以为没比对。
  const changed = data.trace_diffs.filter(
    (diff) => diff.changed || diff.semantic_equal.length > 0 || diff.diff_notes.length > 0,
  );
  if (changed.length === 0) {
    return (
      <Section title="Trace Diff（PRD §56）">
        <EmptyState
          title="没有 trace 差异"
          emptyHint="两侧 trace 完全一致，或两侧都没有保存 trace。"
        />
      </Section>
    );
  }
  return (
    <Section
      title="Trace Diff（PRD §56 十项对比 + §57 参数结构化 / 语义 diff）"
      description={`${changed.length} 个 case 的执行路径发生变化。`}
    >
      <div className="space-y-3">
        {changed.map((diff) => (
          <div key={diff.case_id} className="rounded-lg border border-border">
            <div className="flex flex-wrap items-center gap-2 border-b border-border px-3 py-2">
              <GitCompareArrows className="size-3.5 text-muted-foreground" />
              <span className="font-mono text-xs">{diff.case_id}</span>
              {diff.added_tools.map((tool) => (
                <Badge key={`add-${tool}`} variant="info">
                  +{tool}
                </Badge>
              ))}
              {diff.removed_tools.map((tool) => (
                <Badge key={`del-${tool}`} variant="warn">
                  -{tool}
                </Badge>
              ))}
              {diff.final_answer_changed && <Badge variant="fail">最终答案变化</Badge>}
            </div>
            <div className="space-y-3 p-3">
              <div className="flex flex-wrap gap-4 text-[11px] text-muted-foreground">
                <span>
                  model calls {diff.model_calls[0]} → {diff.model_calls[1]}
                </span>
                <span>
                  tokens {diff.tokens[0]} → {diff.tokens[1]}
                </span>
                <span>
                  latency {diff.latency_ms[0]} → {diff.latency_ms[1]} ms
                </span>
                <span>
                  errors {diff.errors[0]} → {diff.errors[1]}
                </span>
                <span>
                  retries {diff.retries[0]} → {diff.retries[1]}
                </span>
                <span>
                  subagents {diff.subagent_calls[0]} → {diff.subagent_calls[1]}
                </span>
              </div>

              <div className="space-y-0.5 font-mono text-xs">
                {diff.tool_sequence.map((op, index) => (
                  <div
                    key={`${op.kind}-${op.value}-${index}`}
                    className={
                      op.kind === "added"
                        ? "text-[var(--pass)]"
                        : op.kind === "removed"
                          ? "text-[var(--fail)]"
                          : "text-muted-foreground"
                    }
                  >
                    {op.kind === "added" ? "+ " : op.kind === "removed" ? "- " : "  "}
                    {op.value}
                  </div>
                ))}
              </div>

              {diff.argument_diffs.length > 0 && (
                <div className="space-y-1">
                  <div className="text-[11px] tracking-wide text-muted-foreground uppercase">
                    参数差异
                  </div>
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Tool</TableHead>
                        <TableHead>Path</TableHead>
                        <TableHead>Baseline</TableHead>
                        <TableHead>Candidate</TableHead>
                        <TableHead>Change</TableHead>
                        <TableHead>判定层级</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {diff.argument_diffs.slice(0, 20).map((arg, index) => (
                        <TableRow key={`${arg.tool}-${arg.path}-${index}`}>
                          <TableCell className="font-mono text-xs">{arg.tool}</TableCell>
                          <TableCell className="font-mono text-xs">{arg.path}</TableCell>
                          <TableCell className="max-w-[16rem] truncate font-mono text-xs text-muted-foreground">
                            {JSON.stringify(arg.baseline)}
                          </TableCell>
                          <TableCell className="max-w-[16rem] truncate font-mono text-xs">
                            {JSON.stringify(arg.candidate)}
                          </TableCell>
                          <TableCell>
                            <Badge variant={arg.change === "changed" ? "warn" : "neutral"}>
                              {arg.change}
                            </Badge>
                          </TableCell>
                          {/* 判定层级（Spec §20.2）：semantic / ast 表示归一化或 AST 介入过，
                              structural 表示逐字比对。用户据此判断结论的可信度。 */}
                          <TableCell className="font-mono text-[10px] text-muted-foreground">
                            {arg.comparison}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              )}

              {diff.semantic_equal.length > 0 && (
                <div className="space-y-1">
                  <div className="text-[11px] tracking-wide text-muted-foreground uppercase">
                    语义相同（不算差异）
                  </div>
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Tool</TableHead>
                        <TableHead>Path</TableHead>
                        <TableHead>Baseline</TableHead>
                        <TableHead>Candidate</TableHead>
                        <TableHead>层级</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {diff.semantic_equal.slice(0, 20).map((item, index) => (
                        <TableRow key={`${item.tool}-${item.path}-${index}`}>
                          <TableCell className="font-mono text-xs">{item.tool}</TableCell>
                          <TableCell className="font-mono text-xs">{item.path}</TableCell>
                          <TableCell className="max-w-[16rem] truncate font-mono text-xs text-muted-foreground">
                            {JSON.stringify(item.baseline)}
                          </TableCell>
                          <TableCell className="max-w-[16rem] truncate font-mono text-xs text-muted-foreground">
                            {JSON.stringify(item.candidate)}
                          </TableCell>
                          <TableCell>
                            <Badge variant="neutral">{item.comparison}</Badge>
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              )}

              {diff.diff_notes.length > 0 && (
                <div className="space-y-1 rounded-md border border-[var(--warn)]/40 bg-[var(--warn)]/5 p-2">
                  <div className="text-[11px] tracking-wide text-muted-foreground uppercase">
                    降级说明
                  </div>
                  {diff.diff_notes.map((note) => (
                    <div key={note} className="text-xs text-muted-foreground">
                      {note}
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        ))}
      </div>
    </Section>
  );
}
