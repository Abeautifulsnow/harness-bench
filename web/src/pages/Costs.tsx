import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip as ChartTooltip,
  XAxis,
  YAxis,
  ZAxis,
} from "recharts";

import { PageHeader, Section, StatCard } from "@/components/common/primitives";
import { QueryState } from "@/components/common/states";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api } from "@/lib/api";
import { fmtCost, fmtNumber, fmtTokens, shortId } from "@/lib/format";

export function Costs() {
  const benchmarks = useQuery({ queryKey: ["benchmarks"], queryFn: api.benchmarks });
  const cost = useQuery({ queryKey: ["cost"], queryFn: () => api.cost({ limit: 50 }) });

  const priced = cost.data?.filter((row) => row.total_cost !== null) ?? [];
  const unpriced = cost.data?.filter((row) => row.total_cost === null) ?? [];
  const total = priced.reduce((sum, row) => sum + (row.total_cost ?? 0), 0);
  const last = cost.data?.[0];

  const chartData =
    priced
      .slice(0, 20)
      .reverse()
      .map((row) => ({
        run: shortId(row.run_id, 8),
        Agent: row.agent_cost ?? 0,
        Judge: row.judge_cost ?? 0,
      })) ?? [];

  const scatter =
    cost.data
      ?.filter((row) => row.total_cost !== null && row.avg_cost_per_case !== null)
      .map((row) => ({
        x: row.total_cost ?? 0,
        y: row.avg_cost_per_case ?? 0,
        z: row.priced_cases,
        run: shortId(row.run_id, 8),
      })) ?? [];

  return (
    <div className="space-y-5">
      <PageHeader
        title="Cost Analysis（PRD §59）"
        description="成本只有存在匹配定价时才计算。无定价时 cost 为 null 而不是 0.0——否则趋势图会凭空出现「成本降到零」。"
        actions={
          <Badge variant={priced.length > 0 ? "pass" : "warn"}>
            {priced.length > 0 ? `${priced.length} 个 run 可计价` : "无可用定价"}
          </Badge>
        }
      />

      <QueryState isLoading={cost.isLoading} error={cost.error} onRetry={() => void cost.refetch()}>
        {cost.data && (
          <>
            <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
              <StatCard
                label="总成本（可计价 run）"
                value={priced.length ? fmtCost(total) : null}
                detail={
                  priced.length ? `${priced.length} 个 run 求和` : "evals/pricing.yaml 未匹配到模型"
                }
                tone={priced.length ? "default" : "warn"}
              />
              <StatCard
                label="单 case 成本（最近）"
                value={last ? fmtCost(last.avg_cost_per_case) : null}
                detail={last ? shortId(last.run_id) : undefined}
              />
              <StatCard
                label="每次成功成本（最近）"
                value={last ? fmtCost(last.cost_per_success) : null}
                detail="cost_per_success = total / 通过 case 数"
              />
              <StatCard
                label="Judge 成本占比（最近）"
                value={
                  last?.judge_cost_ratio === null || last?.judge_cost_ratio === undefined
                    ? null
                    : `${(last.judge_cost_ratio * 100).toFixed(1)}%`
                }
                detail="judge 太贵时优先收敛 judge 范围"
              />
            </div>

            {unpriced.length > 0 && (
              <div className="rounded-lg border border-[color-mix(in_oklch,var(--warn)_35%,transparent)] bg-[color-mix(in_oklch,var(--warn)_8%,transparent)] px-3 py-2 text-xs text-muted-foreground">
                {unpriced.length} 个 run 的 cost 为 null：这些 run 的 case 没有匹配到定价
                （PRD §59 禁止用 0.0 冒充）。在 <code className="font-mono">evals/pricing.yaml</code>{" "}
                里补上模型单价即可让它们进入成本视图。
              </div>
            )}

            {chartData.length > 0 ? (
              <div className="grid gap-4 xl:grid-cols-2">
                <Section
                  title="成本构成（agent vs judge）"
                  description="最近 20 个可计价 run。分摊到 judge 的支出是「评测本身」的开销。"
                >
                  <div className="h-64 w-full">
                    <ResponsiveContainer width="100%" height="100%">
                      <BarChart data={chartData}>
                        <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" />
                        <XAxis dataKey="run" stroke="var(--color-muted-foreground)" fontSize={11} />
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
                        <Bar dataKey="Agent" stackId="cost" fill="var(--color-primary)" />
                        <Bar dataKey="Judge" stackId="cost" fill="var(--color-warn)" />
                      </BarChart>
                    </ResponsiveContainer>
                  </div>
                </Section>

                <Section
                  title="规模 vs 单 case 成本"
                  description="点大小 = 可计价 case 数。用来发现「run 变大但单 case 成本失控」。"
                >
                  <div className="h-64 w-full">
                    <ResponsiveContainer width="100%" height="100%">
                      <ScatterChart>
                        <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" />
                        <XAxis
                          type="number"
                          dataKey="x"
                          name="total cost"
                          stroke="var(--color-muted-foreground)"
                          fontSize={11}
                        />
                        <YAxis
                          type="number"
                          dataKey="y"
                          name="cost / case"
                          stroke="var(--color-muted-foreground)"
                          fontSize={11}
                        />
                        <ZAxis type="number" dataKey="z" range={[40, 260]} />
                        <ChartTooltip
                          contentStyle={{
                            background: "var(--color-popover)",
                            border: "1px solid var(--color-border)",
                            borderRadius: 8,
                            fontSize: 12,
                          }}
                        />
                        <Scatter data={scatter}>
                          {scatter.map((entry, index) => (
                            <Cell key={`${entry.run}-${index}`} fill="var(--color-info)" />
                          ))}
                        </Scatter>
                      </ScatterChart>
                    </ResponsiveContainer>
                  </div>
                </Section>
              </div>
            ) : (
              <Section title="成本视图不可用">
                <p className="text-sm text-muted-foreground">
                  没有任何 run 匹配到定价。在 <code className="font-mono">evals/pricing.yaml</code>{" "}
                  里为实际使用的模型配置 input/output 单价（可用 <code className="font-mono">"*"</code>{" "}
                  作为兜底）后重跑 run。
                </p>
              </Section>
            )}

            <Section title="逐 Run 成本明细">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Run</TableHead>
                    <TableHead>可计价 / 不可计价</TableHead>
                    <TableHead>总成本</TableHead>
                    <TableHead>Agent</TableHead>
                    <TableHead>Judge</TableHead>
                    <TableHead>单 case</TableHead>
                    <TableHead>每次成功</TableHead>
                    <TableHead>Judge 占比</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {cost.data.map((row) => (
                    <TableRow key={row.run_id}>
                      <TableCell>
                        <Link
                          to={`/runs/${row.run_id}`}
                          className="font-mono text-xs text-primary hover:underline"
                        >
                          {shortId(row.run_id)}
                        </Link>
                      </TableCell>
                      <TableCell className="tabular text-xs">
                        <span className="text-[var(--pass)]">{row.priced_cases}</span>
                        <span className="text-muted-foreground"> / </span>
                        <span className="text-[var(--warn)]">{row.unpriced_cases}</span>
                      </TableCell>
                      <TableCell className="tabular">{fmtCost(row.total_cost)}</TableCell>
                      <TableCell className="tabular text-xs">{fmtCost(row.agent_cost)}</TableCell>
                      <TableCell className="tabular text-xs">{fmtCost(row.judge_cost)}</TableCell>
                      <TableCell className="tabular text-xs">
                        {fmtCost(row.avg_cost_per_case)}
                      </TableCell>
                      <TableCell className="tabular text-xs">
                        {fmtCost(row.cost_per_success)}
                      </TableCell>
                      <TableCell className="tabular text-xs">
                        {row.judge_cost_ratio === null
                          ? "—"
                          : `${(row.judge_cost_ratio * 100).toFixed(1)}%`}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </Section>

            <Section title="Token 明细" description="成本的分母：input / output / cache tokens。">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Run</TableHead>
                    <TableHead>Input</TableHead>
                    <TableHead>Output</TableHead>
                    <TableHead>Cache</TableHead>
                    <TableHead>合计</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {cost.data.slice(0, 20).map((row) => (
                    <TableRow key={row.run_id}>
                      <TableCell className="font-mono text-xs">{shortId(row.run_id)}</TableCell>
                      <TableCell className="tabular text-xs">{fmtTokens(row.input_tokens)}</TableCell>
                      <TableCell className="tabular text-xs">
                        {fmtTokens(row.output_tokens)}
                      </TableCell>
                      <TableCell className="tabular text-xs">{fmtTokens(row.cache_tokens)}</TableCell>
                      <TableCell className="tabular text-xs">
                        {fmtTokens(row.input_tokens + row.output_tokens + row.cache_tokens)}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </Section>
          </>
        )}
      </QueryState>

      <Section title="Benchmark 维度" description="当前纳入统计的 benchmark 规模。">
        <div className="flex flex-wrap gap-2">
          {benchmarks.data?.map((row) => (
            <Badge key={row.name} variant="outline">
              {row.name} · {row.cases} cases
            </Badge>
          ))}
        </div>
        <p className="mt-2 text-xs text-muted-foreground">
          {fmtNumber(cost.data?.length ?? 0, 0)} 个 run · 单次请求上限 50
          {last?.price_configured ? " · 已配置定价" : ""}
        </p>
      </Section>
    </div>
  );
}
