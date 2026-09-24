import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router";
import { ArrowLeft, Play } from "lucide-react";

import { PassRate, VerdictBadge } from "@/components/common/badges";
import { KeyValue, Mono, PageHeader, Section } from "@/components/common/primitives";
import { QueryState } from "@/components/common/states";
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
import { api, ApiError } from "@/lib/api";
import { fmtRelative } from "@/lib/format";

export function Benchmarks() {
  const navigate = useNavigate();
  const query = useQuery({ queryKey: ["benchmarks"], queryFn: api.benchmarks });

  return (
    <div className="space-y-5">
      <PageHeader
        title="Benchmark"
        description="PRD §73：Name / Version / Cases / Last Run / Pass Rate / Regression / Owner，支持版本切换与直接发起 run。"
      />
      <QueryState
        isLoading={query.isLoading}
        error={query.error}
        isEmpty={query.data?.length === 0}
        emptyTitle="还没有 benchmark"
        emptyHint="在 evals/benchmarks/<name>.yaml 定义后刷新。"
        onRetry={() => void query.refetch()}
      >
        <Section title="全部 Benchmark">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Version</TableHead>
                <TableHead>Cases</TableHead>
                <TableHead>Owner</TableHead>
                <TableHead>Last Run</TableHead>
                <TableHead>Pass Rate</TableHead>
                <TableHead>Verdict</TableHead>
                <TableHead>Regression</TableHead>
                <TableHead className="text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {query.data?.map((row) => (
                <TableRow key={row.name}>
                  <TableCell>
                    <Link
                      to={`/benchmarks/${encodeURIComponent(row.name)}`}
                      className="text-sm font-medium text-primary hover:underline"
                    >
                      {row.name}
                    </Link>
                    {row.description && (
                      <div className="max-w-md truncate text-xs text-muted-foreground">
                        {row.description}
                      </div>
                    )}
                  </TableCell>
                  <TableCell className="font-mono text-xs">{row.version ?? "—"}</TableCell>
                  <TableCell className="tabular">{row.cases}</TableCell>
                  <TableCell className="text-xs text-muted-foreground">{row.owner ?? "—"}</TableCell>
                  <TableCell className="text-xs">
                    {row.last_run_id ? (
                      <Link
                        to={`/runs/${row.last_run_id}`}
                        className="font-mono text-primary hover:underline"
                      >
                        {fmtRelative(row.last_run_at)}
                      </Link>
                    ) : (
                      <span className="text-muted-foreground">从未运行</span>
                    )}
                  </TableCell>
                  <TableCell>
                    <PassRate value={row.pass_rate} />
                  </TableCell>
                  <TableCell>
                    <VerdictBadge verdict={row.verdict} />
                  </TableCell>
                  <TableCell>
                    {row.regressions === null || row.regressions === undefined ? (
                      <span className="text-muted-foreground">—</span>
                    ) : (
                      <Badge variant={row.regressions > 0 ? "fail" : "pass"}>{row.regressions}</Badge>
                    )}
                  </TableCell>
                  <TableCell className="text-right">
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => navigate(`/benchmarks/${encodeURIComponent(row.name)}`)}
                    >
                      <Play /> 查看 / 运行
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Section>
      </QueryState>
    </div>
  );
}

export function BenchmarkDetail() {
  const { name = "" } = useParams();
  const cases = useQuery({
    queryKey: ["benchmark-cases", name],
    queryFn: () => api.benchmarkCases(name),
    enabled: Boolean(name),
  });
  const benchmarks = useQuery({ queryKey: ["benchmarks"], queryFn: api.benchmarks });
  const row = benchmarks.data?.find((item) => item.name === name);
  const runs = useQuery({
    queryKey: ["runs", { benchmark: name }],
    queryFn: () => api.runs({ benchmark: name, limit: 20 }),
    enabled: Boolean(name),
  });

  return (
    <div className="space-y-5">
      <div className="flex items-center gap-3">
        <Button variant="ghost" size="sm" asChild>
          <Link to="/benchmarks">
            <ArrowLeft /> 返回
          </Link>
        </Button>
      </div>
      <PageHeader
        title={`Benchmark · ${name}`}
        description="解析后的 Case Set（suite → case，PRD §21）与最近运行。CLI 运行方式见下方命令。"
      />

      {row && (
        <Section title="定义">
          <KeyValue
            columns={3}
            items={[
              { label: "Dataset", value: <Mono>{row.dataset ?? "—"}</Mono> },
              { label: "Version", value: <Mono>{row.version ?? "—"}</Mono> },
              { label: "Owner", value: row.owner ?? "—" },
              { label: "Suites", value: row.suites.length ? row.suites.join(", ") : "—" },
              { label: "Cases", value: String(row.cases) },
              {
                label: "Last verdict",
                value: (
                  <span className="flex items-center gap-2">
                    <VerdictBadge verdict={row.verdict} />
                    <PassRate value={row.pass_rate} />
                  </span>
                ),
              },
            ]}
          />
          <div className="mt-4 rounded-lg border border-border bg-muted/40 px-3 py-2">
            <div className="text-[11px] tracking-wide text-muted-foreground uppercase">
              运行命令（只读 UI 不提供写操作）
            </div>
            <code className="mt-1 block font-mono text-xs text-foreground">
              agent-eval benchmark run {name} --repeat 3 --gate pr
            </code>
          </div>
        </Section>
      )}

      <Section
        title="Case Set"
        description="按 suite 选择规则解析出的实际执行集合（去重、顺序稳定）。"
      >
        <QueryState
          isLoading={cases.isLoading}
          error={cases.error}
          isEmpty={cases.data?.length === 0}
          emptyTitle="没有解析出 case"
          emptyHint="检查 suite 的 tags / case_ids 是否命中数据集中的 case。"
          onRetry={() => void cases.refetch()}
        >
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Case</TableHead>
                <TableHead>Name</TableHead>
                <TableHead>Turns</TableHead>
                <TableHead>Tags</TableHead>
                <TableHead>判定挂载点</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {cases.data?.map((item) => (
                <TableRow key={item.id}>
                  <TableCell className="font-mono text-xs">{item.id}</TableCell>
                  <TableCell>{item.name}</TableCell>
                  <TableCell className="tabular">{item.turns}</TableCell>
                  <TableCell>
                    <div className="flex flex-wrap gap-1">
                      {item.tags.map((tag) => (
                        <Badge key={tag} variant="outline">
                          {tag}
                        </Badge>
                      ))}
                    </div>
                  </TableCell>
                  <TableCell>
                    <div className="flex flex-wrap gap-1">
                      {item.mount_points.map((mount) => (
                        <Badge key={mount} variant="neutral">
                          {mount}
                        </Badge>
                      ))}
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </QueryState>
      </Section>

      <Section title="最近运行">
        {runs.isLoading ? (
          <QueryState isLoading error={null}>
            <span />
          </QueryState>
        ) : runs.data?.length ? (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Run</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>基线模式</TableHead>
                <TableHead>开始</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {runs.data.map((run) => (
                <TableRow key={run.run_id}>
                  <TableCell>
                    <Link
                      to={`/runs/${run.run_id}`}
                      className="font-mono text-xs text-primary hover:underline"
                    >
                      {run.run_id}
                    </Link>
                  </TableCell>
                  <TableCell className="text-xs">{run.status}</TableCell>
                  <TableCell className="text-xs text-muted-foreground">{run.baseline_mode}</TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {fmtRelative(run.started_at)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        ) : (
          <p className="text-sm text-muted-foreground">这个 benchmark 还没有 run。</p>
        )}
      </Section>
    </div>
  );
}

export function isNotFound(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}
