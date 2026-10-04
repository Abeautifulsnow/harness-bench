import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router";
import { ArrowLeft, Radio } from "lucide-react";

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
import type { SpanNode } from "@/lib/api-types";
import { fmtRelative } from "@/lib/format";
import { evaluationApi } from "@/lib/evaluation-api";

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
                    {id}（{data.cases[id]} events）
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
    </div>
  );
}
