import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";
import {
  Area,
  AreaChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip as ChartTooltip,
  XAxis,
  YAxis,
} from "recharts";

import { VerdictBadge } from "@/components/common/badges";
import { Mono, PageHeader, Section, StatCard } from "@/components/common/primitives";
import { ProjectionNotice, QueryState } from "@/components/common/states";
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
import { api } from "@/lib/api";
import { fmtCost, fmtNumber, fmtRatio, fmtTokens, shortCommit, shortId } from "@/lib/format";

const AXIS = { stroke: "var(--color-muted-foreground)", fontSize: 11 } as const;

export function Trends() {
  const benchmarks = useQuery({ queryKey: ["benchmarks"], queryFn: api.benchmarks });
  const flaky = useQuery({ queryKey: ["flaky"], queryFn: () => api.flaky(200) });
  const storage = useQuery({ queryKey: ["storage"], queryFn: api.storageStatus });
  const [benchmark, setBenchmark] = React.useState("all");

  const trends = useQuery({
    queryKey: ["trends", benchmark],
    queryFn: () => api.trends({ benchmark: benchmark === "all" ? undefined : benchmark, limit: 200 }),
  });

  const points = trends.data?.points ?? [];
  const latest = points.at(-1);
  const previous = points.at(-2);

  return (
    <div className="space-y-5">
      <PageHeader
        title="趋势（PRD §58）"
        description="Historical Trend：按 commit / 时间 / 模型看核心指标。数据来自 DuckDB 派生层（可随时重建），不是事实层。"
        actions={
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
        }
      />

      <ProjectionNotice projection={trends.data?.projection ?? "missing"} hint={trends.data?.hint} />

      {trends.data?.projection === "ok" && (
        <>
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <StatCard
              label="Task Success"
              value={fmtRatio(latest?.task_success ?? null)}
              detail={latest ? `上次 ${fmtRatio(previous?.task_success ?? null)}` : undefined}
              tone={
                latest?.task_success === null || latest?.task_success === undefined
                  ? "default"
                  : latest.task_success >= (previous?.task_success ?? 0)
                    ? "pass"
                    : "warn"
              }
              loading={trends.isLoading}
            />
            <StatCard
              label="Runs"
              value={points.length}
              detail={latest?.agent_model ?? undefined}
              loading={trends.isLoading}
            />
            <StatCard
              label="Tokens（最近）"
              value={fmtTokens(latest?.total_tokens ?? null)}
              loading={trends.isLoading}
            />
            <StatCard
              label="Cost（最近）"
              value={fmtCost(latest?.total_cost ?? null)}
              detail={
                latest?.total_cost === null
                  ? "无定价：cost 为 null（PRD §59）"
                  : undefined
              }
              loading={trends.isLoading}
            />
          </div>

          <Section
            title="Task Success 趋势"
            description="横轴是 run 的 started_at（等距排列，不按时长缩放——run 是离散事件）。"
          >
            <div className="h-64 w-full">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={points.map((point, index) => ({ ...point, index }))}>
                  <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" />
                  <XAxis
                    dataKey="started_at"
                    tickFormatter={(value: string) => value?.slice(5, 16) ?? ""}
                    {...AXIS}
                  />
                  <YAxis domain={[0, 1]} tickFormatter={(value: number) => `${value * 100}%`} {...AXIS} />
                  <ChartTooltip
                    contentStyle={{
                      background: "var(--color-popover)",
                      border: "1px solid var(--color-border)",
                      borderRadius: 8,
                      fontSize: 12,
                    }}
                    labelFormatter={(value) => String(value).slice(0, 19).replace("T", " ")}
                    formatter={(value) => {
                      const ratio = typeof value === "number" ? value : Number(value ?? 0);
                      return [`${(ratio * 100).toFixed(1)}%`, "task success"];
                    }}
                  />
                  <Line
                    type="monotone"
                    dataKey="task_success"
                    stroke="var(--color-primary)"
                    strokeWidth={2}
                    dot={{ r: 3 }}
                    connectNulls
                  />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </Section>

          <Section
            title="成本与 Token"
            description="cost 缺失点会断开而不是掉到 0：无定价时用 0 会让「成本骤降」成为假象。"
          >
            <div className="h-64 w-full">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={points}>
                  <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" />
                  <XAxis
                    dataKey="started_at"
                    tickFormatter={(value: string) => value?.slice(5, 16) ?? ""}
                    {...AXIS}
                  />
                  <YAxis yAxisId="tokens" {...AXIS} />
                  <YAxis yAxisId="cost" orientation="right" {...AXIS} />
                  <ChartTooltip
                    contentStyle={{
                      background: "var(--color-popover)",
                      border: "1px solid var(--color-border)",
                      borderRadius: 8,
                      fontSize: 12,
                    }}
                  />
                  <Legend wrapperStyle={{ fontSize: 11 }} />
                  <Area
                    yAxisId="tokens"
                    type="monotone"
                    dataKey="total_tokens"
                    name="tokens"
                    stroke="var(--color-info)"
                    fill="var(--color-info)"
                    fillOpacity={0.15}
                    connectNulls
                  />
                  <Area
                    yAxisId="cost"
                    type="monotone"
                    dataKey="total_cost"
                    name="cost (USD)"
                    stroke="var(--color-warn)"
                    fill="var(--color-warn)"
                    fillOpacity={0.12}
                    connectNulls={false}
                  />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          </Section>

          <Section title="Run 序列">
            <QueryState
              isLoading={trends.isLoading}
              error={trends.error}
              isEmpty={points.length === 0}
              emptyTitle="投影里还没有 run"
              emptyHint="先执行 `agent-eval storage rebuild`，然后跑几次 run。"
              onRetry={() => void trends.refetch()}
            >
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Run</TableHead>
                    <TableHead>开始</TableHead>
                    <TableHead>模型</TableHead>
                    <TableHead>Commit</TableHead>
                    <TableHead>Task Success</TableHead>
                    <TableHead>Cases</TableHead>
                    <TableHead>Tokens</TableHead>
                    <TableHead>Cost</TableHead>
                    <TableHead>Verdict</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {[...points].reverse().map((point) => (
                    <TableRow key={point.run_id}>
                      <TableCell>
                        <Link
                          to={`/runs/${point.run_id}`}
                          className="font-mono text-xs text-primary hover:underline"
                        >
                          {shortId(point.run_id)}
                        </Link>
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground">
                        {point.started_at?.slice(0, 19).replace("T", " ") ?? "—"}
                      </TableCell>
                      <TableCell className="text-xs">{point.agent_model ?? "—"}</TableCell>
                      <TableCell className="font-mono text-xs text-muted-foreground">
                        {shortCommit(point.git_commit)}
                      </TableCell>
                      <TableCell className="tabular text-xs">
                        {fmtRatio(point.task_success)}
                      </TableCell>
                      <TableCell className="tabular text-xs">
                        {point.passed_cases ?? "—"}/{point.total_cases ?? "—"}
                      </TableCell>
                      <TableCell className="tabular text-xs">
                        {fmtTokens(point.total_tokens)}
                      </TableCell>
                      <TableCell className="tabular text-xs">
                        {fmtCost(point.total_cost)}
                      </TableCell>
                      <TableCell>
                        <VerdictBadge verdict={point.verdict} />
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </QueryState>
          </Section>
        </>
      )}

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_400px]">
        <Section
          title="Flaky Cases（PRD §32）"
          description="同一 run 内同 case 出现 PASS/FAIL 混合即为波动候选。"
        >
          <ProjectionNotice projection={flaky.data?.projection ?? "missing"} hint={flaky.data?.hint} />
          <QueryState
            isLoading={flaky.isLoading}
            error={flaky.error}
            isEmpty={flaky.data?.total === 0}
            emptyTitle="没有检出 flaky case"
            emptyHint="需要 repeat ≥ 2 且同一 case 出现 PASS/FAIL 混合。"
            onRetry={() => void flaky.refetch()}
          >
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Run</TableHead>
                  <TableHead>Case</TableHead>
                  <TableHead>Iterations</TableHead>
                  <TableHead>Pass</TableHead>
                  <TableHead>Fail</TableHead>
                  <TableHead>Error</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {flaky.data?.cases.map((row) => (
                  <TableRow key={`${row.run_id}-${row.case_id}`}>
                    <TableCell>
                      <Link
                        to={`/runs/${row.run_id}`}
                        className="font-mono text-xs text-primary hover:underline"
                      >
                        {shortId(row.run_id)}
                      </Link>
                    </TableCell>
                    <TableCell className="font-mono text-xs">{row.case_id}</TableCell>
                    <TableCell className="tabular">{row.iterations}</TableCell>
                    <TableCell className="tabular text-[var(--pass)]">{row.passes}</TableCell>
                    <TableCell className="tabular text-[var(--fail)]">{row.fails}</TableCell>
                    <TableCell className="tabular text-[var(--warn)]">{row.errors}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </QueryState>
        </Section>

        <Section
          title="派生层状态（Spec §1.3）"
          description="派生层永远可重建：清空重建不会丢任何事实数据。"
        >
          <div className="space-y-3">
            <div className="flex items-center gap-2">
              <Badge variant={storage.data?.projection === "ok" ? "pass" : "warn"}>
                {storage.data?.projection === "ok" ? "已构建" : "未构建"}
              </Badge>
              <Mono className="truncate text-muted-foreground">{storage.data?.path}</Mono>
            </div>
            {storage.data?.hint && (
              <p className="text-xs text-muted-foreground">{storage.data.hint}</p>
            )}
            {storage.data?.counts && (
              <div className="space-y-0.5">
                {Object.entries(storage.data.counts).map(([table, count]) => (
                  <div key={table} className="flex items-center justify-between text-xs">
                    <Mono className="text-muted-foreground">{table}</Mono>
                    <span className="tabular">{fmtNumber(count, 0)}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </Section>
      </div>
    </div>
  );
}
