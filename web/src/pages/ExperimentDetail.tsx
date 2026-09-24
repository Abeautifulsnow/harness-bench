import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router";
import { ArrowLeft } from "lucide-react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip as ChartTooltip,
  XAxis,
  YAxis,
} from "recharts";

import { VerdictBadge } from "@/components/common/badges";
import { KeyValue, Mono, PageHeader, Section } from "@/components/common/primitives";
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
import type { VariantRow } from "@/lib/api-types";
import { api } from "@/lib/api";
import { fmtCost, fmtMs, fmtNumber, fmtRatio, fmtTokens, shortId } from "@/lib/format";
import { VARIANT_DIMENSIONS_HINT } from "@/lib/dimensions";
import { cn } from "@/lib/utils";

export function ExperimentDetail() {
  const { experimentId = "" } = useParams();
  const [dimension, setDimension] = React.useState<string>("");
  const [sortKey, setSortKey] = React.useState<string>("task_success");

  const detail = useQuery({
    queryKey: ["experiment", experimentId, dimension],
    queryFn: () => api.experiment(experimentId, dimension || undefined),
    enabled: Boolean(experimentId),
  });
  const comparison = useQuery({
    queryKey: ["experiment-comparison", experimentId, dimension],
    queryFn: () => api.experimentComparison(experimentId, dimension),
    enabled: Boolean(experimentId && dimension),
  });

  const variants = React.useMemo(() => {
    const rows = [...(detail.data?.variants ?? [])];
    rows.sort((a, b) => {
      const left = readMetric(a, sortKey);
      const right = readMetric(b, sortKey);
      if (left === null && right === null) return 0;
      if (left === null) return 1;
      if (right === null) return -1;
      // 成本/延迟/规模类：升序更直观（越小越好）；质量类降序
      const ascending = ["cost", "latency_ms", "tokens", "tool_calls"].includes(sortKey);
      return ascending ? left - right : right - left;
    });
    return rows;
  }, [detail.data, sortKey]);

  const experiment = detail.data?.experiment;

  return (
    <div className="space-y-5">
      <div className="flex items-center gap-3">
        <Button variant="ghost" size="sm" asChild>
          <Link to="/experiments">
            <ArrowLeft /> 返回
          </Link>
        </Button>
      </div>

      <PageHeader
        title={`Experiment · ${experiment?.name ?? experimentId}`}
        description="PRD §74 对比表：按任意 Metric 排序。指标取自各 variant 对应 run 的 report.json，与 Run Detail 页不会互相矛盾。"
      />

      {experiment && (
        <Section title="定义">
          <KeyValue
            columns={3}
            items={[
              { label: "Experiment", value: <Mono>{experiment.id}</Mono> },
              { label: "Benchmark", value: experiment.benchmark_id },
              { label: "Dataset version", value: <Mono>{experiment.dataset_version || "—"}</Mono> },
              { label: "Profile", value: <Mono>{experiment.profile ?? "—"}</Mono> },
              { label: "Repeat", value: String(experiment.repeat ?? "—") },
              { label: "Gate", value: <Mono>{experiment.gate}</Mono> },
              { label: "Variants", value: String(detail.data?.variants.length ?? 0) },
              { label: "Created", value: experiment.created_at.slice(0, 19).replace("T", " ") },
              {
                label: "Status",
                value: <Badge variant="neutral">{experiment.status}</Badge>,
              },
            ]}
          />
          {experiment.note && (
            <p className="mt-3 text-xs text-muted-foreground">{experiment.note}</p>
          )}
        </Section>
      )}

      <Section
        title="Variant 对比（PRD §74）"
        description="Success / Completion / Efficiency（step_ratio）/ Tool Accuracy（argument_checks）/ Cost / Latency。"
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <Select value={dimension || "none"} onValueChange={(value) => setDimension(value === "none" ? "" : value)}>
              <SelectTrigger className="w-44">
                <SelectValue placeholder="按维度分组" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="none">不分组</SelectItem>
                {(detail.data?.dimensions ?? []).map((name) => (
                  <SelectItem key={name} value={name}>
                    {name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select value={sortKey} onValueChange={setSortKey}>
              <SelectTrigger className="w-44">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="task_success">Success</SelectItem>
                <SelectItem value="task_completion">Completion</SelectItem>
                <SelectItem value="step_ratio">Efficiency</SelectItem>
                <SelectItem value="argument_checks">Tool Accuracy</SelectItem>
                <SelectItem value="cost">Cost</SelectItem>
                <SelectItem value="latency_ms">Latency</SelectItem>
                <SelectItem value="tokens">Tokens</SelectItem>
                <SelectItem value="tool_calls">Tool calls</SelectItem>
              </SelectContent>
            </Select>
          </div>
        }
      >
        <QueryState
          isLoading={detail.isLoading}
          error={detail.error}
          isEmpty={detail.data?.variants.length === 0}
          emptyTitle="实验里没有 variant"
          onRetry={() => void detail.refetch()}
        >
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Variant</TableHead>
                <TableHead>Dimensions</TableHead>
                <TableHead>Run</TableHead>
                <TableHead>Verdict</TableHead>
                <TableHead>Success</TableHead>
                <TableHead>Completion</TableHead>
                <TableHead>Efficiency</TableHead>
                <TableHead>Tool Accuracy</TableHead>
                <TableHead>Cost</TableHead>
                <TableHead>Latency</TableHead>
                <TableHead>Tokens</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {variants.map((row) => (
                <TableRow key={row.variant_id}>
                  <TableCell>
                    <div className="text-sm">{row.name}</div>
                    <Mono className="text-muted-foreground">{shortId(row.variant_id, 10)}</Mono>
                  </TableCell>
                  <TableCell>
                    <div className="flex flex-wrap gap-1">
                      {Object.entries(row.dimensions).map(([key, value]) => (
                        <Badge key={key} variant="outline" title={VARIANT_DIMENSIONS_HINT[key]}>
                          {key}={String(value)}
                        </Badge>
                      ))}
                    </div>
                  </TableCell>
                  <TableCell className="text-xs">
                    {row.run_id ? (
                      <Link
                        to={`/runs/${row.run_id}`}
                        className="font-mono text-primary hover:underline"
                      >
                        {shortId(row.run_id)}
                      </Link>
                    ) : (
                      <span className="text-muted-foreground">{row.status}</span>
                    )}
                  </TableCell>
                  <TableCell>
                    <VerdictBadge verdict={row.verdict} />
                  </TableCell>
                  <TableCell className={cn("tabular", metricTone(row.task_success, "high"))}>
                    {fmtRatio(row.task_success)}
                  </TableCell>
                  <TableCell className="tabular">{fmtRatio(row.task_completion)}</TableCell>
                  <TableCell className="tabular">{fmtRatio(row.step_ratio)}</TableCell>
                  <TableCell className="tabular">{fmtRatio(row.argument_checks)}</TableCell>
                  <TableCell className="tabular">{fmtCost(row.cost)}</TableCell>
                  <TableCell className="tabular">{fmtMs(row.latency_ms)}</TableCell>
                  <TableCell className="tabular">{fmtTokens(row.tokens)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </QueryState>
      </Section>

      {dimension && (
        <Section
          title={`按 ${dimension} 分组对比（PRD §26/§27/§28）`}
          description="同一维度取值下的 variant 取均值；用于回答「换模型 / 换 prompt / 换 harness 各带来什么」。"
        >
          <QueryState
            isLoading={comparison.isLoading}
            error={comparison.error}
            isEmpty={comparison.data?.length === 0}
            emptyTitle="该维度没有可比分组"
          >
            {comparison.data && (
              <>
                <div className="h-64 w-full">
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={comparison.data.map((row) => ({
                      name: row.value || "（空）",
                      Success: (row.task_success ?? 0) * 100,
                      "Cost/case": row.cost ?? 0,
                    }))}>
                      <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" />
                      <XAxis dataKey="name" stroke="var(--color-muted-foreground)" fontSize={11} />
                      <YAxis stroke="var(--color-muted-foreground)" fontSize={11} />
                      <ChartTooltip
                        contentStyle={{
                          background: "var(--color-popover)",
                          border: "1px solid var(--color-border)",
                          borderRadius: 8,
                          fontSize: 12,
                        }}
                      />
                      <Legend wrapperStyle={{ fontSize: 11 }} />
                      <Bar dataKey="Success" fill="var(--color-primary)" />
                    </BarChart>
                  </ResponsiveContainer>
                </div>

                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>{dimension}</TableHead>
                      <TableHead>Variants</TableHead>
                      <TableHead>Success</TableHead>
                      <TableHead>Efficiency</TableHead>
                      <TableHead>Tool Accuracy</TableHead>
                      <TableHead>Cost（均值）</TableHead>
                      <TableHead>Latency（均值）</TableHead>
                      <TableHead>Tokens（均值）</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {comparison.data.map((row) => (
                      <TableRow key={row.value}>
                        <TableCell className="font-medium">{row.value || "（空）"}</TableCell>
                        <TableCell className="text-xs text-muted-foreground">
                          {row.variants.length}
                        </TableCell>
                        <TableCell className="tabular">{fmtRatio(row.task_success)}</TableCell>
                        <TableCell className="tabular">{fmtRatio(row.step_ratio)}</TableCell>
                        <TableCell className="tabular">{fmtRatio(row.argument_checks)}</TableCell>
                        <TableCell className="tabular">{fmtCost(row.cost)}</TableCell>
                        <TableCell className="tabular">{fmtMs(row.latency_ms)}</TableCell>
                        <TableCell className="tabular">{fmtTokens(row.tokens)}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </>
            )}
          </QueryState>
        </Section>
      )}

      <Section title="可比性约束（PRD §110-6）">
        <p className="text-xs text-muted-foreground">
          实验内所有 variant 共享同一 benchmark、dataset_version、profile、repeat 与 gate。
          任何一项不同都不应放进同一个实验——否则表格里的"差异"可能来自数据集而不是被测变量。
          变体间比较只对 <Mono>dimensions</Mono> 里的受控维度做出结论。
        </p>
      </Section>
    </div>
  );
}

function readMetric(row: VariantRow, key: string): number | null {
  const value = (row as unknown as Record<string, unknown>)[key];
  return typeof value === "number" ? value : null;
}

function metricTone(value: number | null, direction: "high" | "low"): string {
  if (value === null) return "text-muted-foreground";
  if (direction === "high") {
    if (value >= 1) return "text-[var(--pass)]";
    if (value === 0) return "text-[var(--fail)]";
    return "text-[var(--warn)]";
  }
  return "";
}

export function ExperimentCount({ count }: { count: number }) {
  return <span className="tabular">{fmtNumber(count, 0)}</span>;
}
