import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";

import { BaselineBadge } from "@/components/common/badges";
import { KeyValue, Mono, PageHeader, Section } from "@/components/common/primitives";
import { EmptyState, Loading, QueryState } from "@/components/common/states";
import { Badge } from "@/components/ui/badge";
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
import { fmtRelative, shortId } from "@/lib/format";

/** PRD §78 的六个视角。切换视角 = 换一个切片，不换数据源。 */
const VIEWS = [
  { key: "by_category", label: "By Category" },
  { key: "by_parent", label: "By Parent" },
  { key: "by_tool", label: "By Tool" },
  { key: "by_model", label: "By Model" },
  { key: "by_version", label: "By Version" },
  { key: "by_benchmark", label: "By Benchmark" },
] as const;

export function Failures() {
  const runs = useQuery({
    queryKey: ["failure-runs"],
    queryFn: () => api.failureRuns({ limit: 50 }),
  });
  const taxonomy = useQuery({ queryKey: ["taxonomy"], queryFn: api.failureTaxonomy });
  const [runId, setRunId] = React.useState<string>("");

  const withFailures = runs.data ?? [];
  React.useEffect(() => {
    if (!runId && withFailures.length) setRunId(withFailures[0].run_id);
  }, [withFailures, runId]);

  const detail = useQuery({
    queryKey: ["failures", runId],
    queryFn: () => api.failuresOfRun(runId),
    enabled: Boolean(runId),
  });
  const clusters = useQuery({
    queryKey: ["clusters", runId],
    queryFn: () => api.clusters(runId),
    enabled: Boolean(runId),
  });

  return (
    <div className="space-y-5">
      <PageHeader
        title="Failure Intelligence"
        description="PRD §47–§50：规则引擎分类 + 工具序列/错误签名聚类。分类与聚类都是确定性的，同一 run 重复请求结果不变。"
        actions={
          <Select value={runId} onValueChange={setRunId}>
            <SelectTrigger className="w-72">
              <SelectValue placeholder="选择 run" />
            </SelectTrigger>
            <SelectContent>
              {withFailures.map((item) => (
                <SelectItem key={item.run_id} value={item.run_id}>
                  {shortId(item.run_id)} · {item.total} failures
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        }
      />

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
        <div className="min-w-0 space-y-4">
          {!runId ? (
            <EmptyState
              title="没有带 failure 的 run"
              emptyHint="所有 run 都通过了，或还没有 run。"
            />
          ) : (
            <Section
              title="Failure 列表"
              description="每一条都带分类依据（metric → 规则命中）与来源（rule / llm）。"
            >
              <QueryState
                isLoading={detail.isLoading}
                error={detail.error}
                isEmpty={detail.data?.total === 0}
                emptyTitle="该 run 没有 failure"
                onRetry={() => void detail.refetch()}
              >
                {detail.data && detail.data.total > 0 && (
                  <Tabs defaultValue="list">
                    <TabsList>
                      <TabsTrigger value="list">明细</TabsTrigger>
                      {VIEWS.map((view) => (
                        <TabsTrigger key={view.key} value={view.key}>
                          {view.label}
                        </TabsTrigger>
                      ))}
                    </TabsList>

                    <TabsContent value="list">
                      <Table>
                        <TableHeader>
                          <TableRow>
                            <TableHead>Case</TableHead>
                            <TableHead>Iter</TableHead>
                            <TableHead>Category</TableHead>
                            <TableHead>Metric</TableHead>
                            <TableHead>Source</TableHead>
                            <TableHead>Reason</TableHead>
                          </TableRow>
                        </TableHeader>
                        <TableBody>
                          {detail.data.failures.map((failure) => (
                            <TableRow key={failure.failure_id}>
                              <TableCell className="font-mono text-xs">
                                <Link
                                  to={`/runs/${runId}`}
                                  className="text-primary hover:underline"
                                >
                                  {failure.case_id}
                                </Link>
                              </TableCell>
                              <TableCell className="tabular text-xs">{failure.iteration}</TableCell>
                              <TableCell>
                                <Badge variant="fail">{failure.category}</Badge>
                              </TableCell>
                              <TableCell className="font-mono text-xs">
                                {failure.metric ?? "—"}
                              </TableCell>
                              <TableCell className="text-xs text-muted-foreground">
                                {failure.source}
                              </TableCell>
                              <TableCell
                                className="max-w-lg truncate text-xs"
                                title={failure.reason ?? ""}
                              >
                                {failure.reason ?? "—"}
                              </TableCell>
                            </TableRow>
                          ))}
                        </TableBody>
                      </Table>
                    </TabsContent>

                    {VIEWS.map((view) => (
                      <TabsContent key={view.key} value={view.key}>
                        <SliceTable
                          rows={Object.entries(
                            (detail.data?.[view.key] ?? {}) as Record<string, number>,
                          ).sort(([, a], [, b]) => b - a)}
                          label={view.label.replace("By ", "")}
                        />
                      </TabsContent>
                    ))}
                  </Tabs>
                )}
              </QueryState>
            </Section>
          )}

          {runId && (
            <Section
              title="Failure Clusters（PRD §49/§50/§107）"
              description="聚类键 = 工具序列 + 归一化错误；每簇给出代表 case 与受影响版本。"
            >
              <QueryState
                isLoading={clusters.isLoading}
                error={clusters.error}
                isEmpty={clusters.data?.total_clusters === 0}
                emptyTitle="没有 cluster"
              >
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Cluster</TableHead>
                      <TableHead>Category</TableHead>
                      <TableHead>Size</TableHead>
                      <TableHead>Representative</TableHead>
                      <TableHead>Tool sequence</TableHead>
                      <TableHead>Initial seen</TableHead>
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
                        <TableCell className="text-xs text-muted-foreground">
                          {cluster.first_seen ?? "—"}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </QueryState>
            </Section>
          )}
        </div>

        <div className="space-y-4">
          <Section
            title="Failure Taxonomy（PRD §47）"
            description="11 个一级分类 + 二级分类。一级分类是分流依据（SECURITY 不可被 LLM 覆盖）。"
          >
            {taxonomy.isLoading ? (
              <Loading />
            ) : (
              <div className="space-y-3">
                {Array.from(new Set(taxonomy.data?.map((row) => row.parent) ?? [])).map((parent) => (
                  <div key={parent} className="space-y-1">
                    <div className="text-[11px] font-semibold tracking-wide text-muted-foreground uppercase">
                      {parent}
                    </div>
                    <div className="space-y-0.5">
                      {taxonomy.data
                        ?.filter((row) => row.parent === parent)
                        .map((row) => (
                          <div
                            key={row.category}
                            className="flex items-start gap-2 text-xs"
                            title={row.description}
                          >
                            <Mono className="shrink-0 text-muted-foreground">{row.category}</Mono>
                            <span className="text-muted-foreground">{row.description}</span>
                          </div>
                        ))}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </Section>

          <Section title="Run 信息">
            {detail.data && (
              <KeyValue
                columns={1}
                items={[
                  { label: "Run", value: <Mono>{detail.data.run_id}</Mono> },
                  { label: "Failures", value: String(detail.data.total) },
                  {
                    label: "Clusters",
                    value: String(clusters.data?.total_clusters ?? "—"),
                  },
                ]}
              />
            )}
          </Section>
        </div>
      </div>
    </div>
  );
}

function SliceTable({ rows, label }: { rows: [string, number][]; label: string }) {
  if (rows.length === 0) {
    return <EmptyState title={`没有 ${label} 维度的数据`} />;
  }
  const total = rows.reduce((sum, [, count]) => sum + count, 0);
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>{label}</TableHead>
          <TableHead>Failures</TableHead>
          <TableHead>占比</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map(([key, count]) => (
          <TableRow key={key}>
            <TableCell className="font-mono text-xs">{key || "（空）"}</TableCell>
            <TableCell className="tabular">{count}</TableCell>
            <TableCell className="tabular text-xs text-muted-foreground">
              {total ? `${((count / total) * 100).toFixed(1)}%` : "—"}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

export function FailureRunLink({
  runId,
  baselineMode,
  createdAt,
}: {
  runId: string;
  baselineMode: string;
  createdAt: string;
}) {
  return (
    <Link to={`/runs/${runId}`} className="flex items-center gap-2 text-xs">
      <Mono>{shortId(runId)}</Mono>
      <BaselineBadge mode={baselineMode} />
      <span className="text-muted-foreground">{fmtRelative(createdAt)}</span>
    </Link>
  );
}
