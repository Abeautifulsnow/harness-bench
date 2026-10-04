import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router";
import { ArrowLeft, Play, Radio } from "lucide-react";

import { Mono, PageHeader, Section } from "@/components/common/primitives";
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
import type { SpanNode } from "@/lib/api-types";
import { fmtRelative } from "@/lib/format";
import { evaluationApi, type EvaluationResult } from "@/lib/evaluation-api";

function eventCount(entry: unknown): number {
  if (typeof entry === "number") return entry;
  if (entry && typeof entry === "object" && "events" in entry) {
    return Number((entry as { events: number }).events);
  }
  return 0;
}

/** §53 Production Trace：摄取自生产侧的链路（OTel / 平台事件），只读查看。
 *  Span Tree 与 Run Trace Viewer 共用同一构建器——分析面同构的第一步。 */
export function Production() {
  const traces = useQuery({
    queryKey: ["production-traces"],
    queryFn: evaluationApi.productionTraces,
  });

  return (
    <div className="space-y-5">
      <PageHeader
        title="生产 Trace"
        description="设计文档 §53 第一片：生产侧链路摄取（POST /api/production/traces，token 门，OTel JSON / 平台事件双格式）。查看与分析面共用同一 Span Tree。"
      />
      <QueryState
        isLoading={traces.isLoading}
        error={traces.error}
        isEmpty={traces.data?.length === 0}
        emptyTitle="还没有摄取的生产 Trace"
        emptyHint="由采集器/CI 调用 POST /api/production/traces 摄取（可参考设计文档 §60）。"
        onRetry={() => void traces.refetch()}
      >
        <div className="space-y-3">
          {traces.data?.map((meta) => (
            <Section
              key={meta.trace_id}
              title={meta.trace_id}
              description={`${meta.source} · 摄取于 ${fmtRelative(meta.ingested_at)}${meta.model ? ` · ${meta.model}` : ""}`}
              actions={
                <Button variant="outline" size="sm" asChild>
                  <Link to={`/production/${meta.trace_id}`}>
                    <Radio /> 查看
                  </Link>
                </Button>
              }
            >
              <div className="flex flex-wrap gap-2 text-xs text-muted-foreground">
                <Badge variant="outline">{Object.keys(meta.cases).length} cases</Badge>
                <Badge variant="outline">{meta.total_events} events</Badge>
                {meta.endpoint && <Mono>{meta.endpoint}</Mono>}
              </div>
            </Section>
          ))}
        </div>
      </QueryState>
    </div>
  );
}

export function ProductionTraceDetail() {
  const { traceId = "" } = useParams();
  const [caseId, setCaseId] = React.useState<string>("");

  const meta = useQuery({
    queryKey: ["production-trace", traceId],
    queryFn: () => evaluationApi.productionTrace(traceId),
  });
  const caseIds = Object.keys(meta.data?.cases ?? {});
  const activeCase = caseId || caseIds[0] || "";
  const view = useQuery({
    queryKey: ["production-trace-view", traceId, activeCase],
    queryFn: () => evaluationApi.productionTraceView(traceId, activeCase),
    enabled: Boolean(traceId && activeCase),
  });

  if (meta.isLoading || meta.error) {
    return (
      <QueryState
        isLoading={meta.isLoading}
        error={meta.error}
        isEmpty={false}
        onRetry={() => void meta.refetch()}
      >
        {null}
      </QueryState>
    );
  }
  const data = meta.data;
  if (!data) return null;

  return (
    <div className="space-y-5">
      <PageHeader
        title={data.trace_id}
        description={`${data.source} · 摄取于 ${fmtRelative(data.ingested_at)}${data.model ? ` · 模型 ${data.model}` : ""}`}
        actions={
          <Button variant="ghost" asChild>
            <Link to="/production">
              <ArrowLeft /> 返回
            </Link>
          </Button>
        }
      />
      <Section
        title="Span Tree"
        description={
          view.data
            ? `${view.data.span_count} spans · ${view.data.tokens} tokens`
            : undefined
        }
        actions={
          caseIds.length > 1 ? (
            <Select value={activeCase} onValueChange={setCaseId}>
              <SelectTrigger className="w-56">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {caseIds.map((id) => (
                  <SelectItem key={id} value={id}>
                    {id}（{eventCount(data.cases[id])} events）
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          ) : caseIds.length === 1 ? (
            <Mono>{caseIds[0]}</Mono>
          ) : undefined
        }
      >
        <QueryState
          isLoading={view.isLoading}
          error={view.error}
          isEmpty={!view.data?.root}
          emptyTitle="重建不出 Span Tree"
          emptyHint="事件缺失 parent 关系或为空。"
          onRetry={() => void view.refetch()}
        >
          <TraceTree root={(view.data?.root ?? null) as SpanNode | null} />
        </QueryState>
      </Section>
      <EvaluationSection traceId={traceId} />
    </div>
  );
}

/** §61 Online Eval：参考无关 judge——选策略、运行、看结果与历史。 */
function EvaluationSection({ traceId }: { traceId: string }) {
  const queryClient = useQueryClient();
  const policies = useQuery({ queryKey: ["eval-policies"], queryFn: evaluationApi.evalPolicies });
  const history = useQuery({
    queryKey: ["production-evaluations", traceId],
    queryFn: () => evaluationApi.productionEvaluations(traceId),
  });
  const [policyId, setPolicyId] = React.useState("");
  const [latest, setLatest] = React.useState<EvaluationResult | null>(null);

  React.useEffect(() => {
    if (!policyId && policies.data?.length) setPolicyId(policies.data[0].id);
  }, [policies.data, policyId]);

  const run = useMutation({
    mutationFn: () => evaluationApi.evaluateProductionTrace(traceId, policyId),
    onSuccess: (result) => {
      setLatest(result);
      void queryClient.invalidateQueries({ queryKey: ["production-evaluations", traceId] });
    },
  });

  const shown = latest ?? history.data?.[0] ?? null;
  const summary = shown?.summary;

  return (
    <Section
      title="Online Eval"
      description="§61：参考无关 judge——评测生产对话自身的 (input, output) 对；无 dataset 对齐、无 Gate、不落 Run。"
      actions={
        <div className="flex items-center gap-2">
          {policies.data && policies.data.length > 0 && (
            <Select value={policyId} onValueChange={setPolicyId}>
              <SelectTrigger className="w-52">
                <SelectValue placeholder="选择评测策略" />
              </SelectTrigger>
              <SelectContent>
                {policies.data.map((p) => (
                  <SelectItem key={p.id} value={p.id}>
                    {p.display_name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
          <Button
            size="sm"
            disabled={!policyId || run.isPending}
            onClick={() => run.mutate()}
          >
            <Play /> {run.isPending ? "评测中…" : "Run Evaluation"}
          </Button>
        </div>
      }
    >
      {run.error && (
        <p className="mb-3 rounded-md border border-[var(--fail)]/30 bg-[var(--fail)]/5 px-3 py-2 text-xs text-[var(--fail)]">
          {(run.error as Error).message}
        </p>
      )}
      {!shown && (
        <p className="text-xs text-muted-foreground">
          {policies.data?.length
            ? "选择策略后运行；judge 需要 deepeval + 模型凭证。"
            : "在 evals/eval-policies/*.yaml 定义评测策略后刷新。"}
        </p>
      )}
      {summary && (
        <div className="space-y-3">
          <div className="flex flex-wrap gap-2 text-xs">
            <Badge variant="outline">
              {summary.cases_evaluated} / {summary.cases_total} cases
            </Badge>
            {summary.cases_unextractable.length > 0 && (
              <Badge variant="outline" className="text-muted-foreground">
                {summary.cases_unextractable.length} 条无法提取（跳过）
              </Badge>
            )}
            {Object.entries(summary.metrics).map(([metric, stat]) => (
              <Badge
                key={metric}
                variant="outline"
                className={stat.fail > 0 || stat.error > 0 ? "text-[var(--fail)]" : "text-[var(--pass)]"}
              >
                {metric}: {stat.pass}✓ {stat.fail}✗ {stat.error}err
                {stat.mean_score != null && ` · mean ${stat.mean_score}`}
              </Badge>
            ))}
          </div>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Case</TableHead>
                <TableHead>Metric</TableHead>
                <TableHead>Score</TableHead>
                <TableHead>Verdict</TableHead>
                <TableHead>Reason</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {shown.rows.map((row, index) => (
                <TableRow key={`${row.case_id}-${row.metric}-${index}`}>
                  <TableCell className="font-mono text-xs">{row.case_id}</TableCell>
                  <TableCell className="text-xs">{row.metric}</TableCell>
                  <TableCell className="tabular text-xs">{row.score ?? "—"}</TableCell>
                  <TableCell>
                    <Badge
                      variant="outline"
                      className={
                        row.verdict === "pass"
                          ? "text-[var(--pass)]"
                          : row.verdict === "fail"
                            ? "text-[var(--fail)]"
                            : "text-muted-foreground"
                      }
                    >
                      {row.verdict}
                    </Badge>
                  </TableCell>
                  <TableCell className="max-w-md truncate text-xs text-muted-foreground">
                    {row.reason ?? "—"}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          {history.data && history.data.length > 1 && (
            <p className="text-xs text-muted-foreground">
              共 {history.data.length} 次评测；最新 {shown.evaluation_id}。
            </p>
          )}
        </div>
      )}
    </Section>
  );
}
