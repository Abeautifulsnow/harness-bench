import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";

import { BaselineBadge, ExitCodeBadge, VerdictBadge } from "@/components/common/badges";
import { KeyValue, Mono, PageHeader, Section } from "@/components/common/primitives";
import { QueryState } from "@/components/common/states";
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
import { fmtNumber, fmtRelative, shortId } from "@/lib/format";

export function Quality() {
  const rulesets = useQuery({ queryKey: ["gate-rules"], queryFn: api.gateRules });
  const runs = useQuery({ queryKey: ["runs", { limit: 100 }], queryFn: () => api.runs({ limit: 100 }) });
  const [gate, setGate] = React.useState("pr");
  const history = useQuery({
    queryKey: ["gate-history", gate],
    queryFn: () => api.gateHistory({ limit: 50 }),
  });
  const [runId, setRunId] = React.useState<string>("");

  React.useEffect(() => {
    if (!runId && runs.data?.length) setRunId(runs.data[0].run_id);
  }, [runs.data, runId]);

  const detail = useQuery({
    queryKey: ["gate", runId, gate],
    queryFn: () => api.gate(runId, gate),
    enabled: Boolean(runId),
  });

  return (
    <div className="space-y-5">
      <PageHeader
        title="Quality Gate"
        description="PRD §64–§69：三套 Gate（pr / main / release）共用一份求值器，规则集是数据（evals/gates/*.yaml）。Release Gate 默认要求显式 release baseline。"
        actions={
          <Select value={gate} onValueChange={setGate}>
            <SelectTrigger className="w-36">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="pr">pr</SelectItem>
              <SelectItem value="main">main</SelectItem>
              <SelectItem value="release">release</SelectItem>
            </SelectContent>
          </Select>
        }
      />

      <div className="grid gap-4 lg:grid-cols-3">
        {rulesets.data?.map((ruleset) => (
          <Section
            key={ruleset.gate}
            title={ruleset.gate}
            description={ruleset.strict ? "严格模式（strict）" : "常规模式"}
            actions={ruleset.strict ? <Badge variant="warn">strict</Badge> : undefined}
          >
            <div className="space-y-3">
              <div className="space-y-1">
                <div className="text-[11px] tracking-wide text-muted-foreground uppercase">
                  阈值（相对基线退步上限）
                </div>
                <div className="space-y-0.5 text-xs">
                  {Object.entries(ruleset.thresholds).map(([key, value]) => (
                    <div key={key} className="flex items-center justify-between gap-2">
                      <Mono className="text-muted-foreground">{key}</Mono>
                      <span className="tabular">{JSON.stringify(value)}</span>
                    </div>
                  ))}
                </div>
              </div>
              {ruleset.suites.length > 0 && (
                <div className="space-y-1">
                  <div className="text-[11px] tracking-wide text-muted-foreground uppercase">
                    必跑套件
                  </div>
                  <div className="flex flex-wrap gap-1">
                    {ruleset.suites.map((suite) => (
                      <Badge key={suite} variant="outline">
                        {suite}
                      </Badge>
                    ))}
                  </div>
                </div>
              )}
              {ruleset.hard_failure_categories.length > 0 && (
                <div className="space-y-1">
                  <div className="text-[11px] tracking-wide text-muted-foreground uppercase">
                    硬失败分类
                  </div>
                  <div className="flex flex-wrap gap-1">
                    {ruleset.hard_failure_categories.map((category) => (
                      <Badge key={category} variant="fail">
                        {category}
                      </Badge>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </Section>
        ))}
      </div>

      <div className="grid gap-4 xl:grid-cols-[360px_minmax(0,1fr)]">
        <Section
          title="Run 选择"
          description="点一个 run 查看它在当前 Gate 下的逐条规则结论。"
        >
          <QueryState
            isLoading={runs.isLoading}
            error={runs.error}
            isEmpty={runs.data?.length === 0}
            emptyTitle="还没有 run"
          >
            <div className="max-h-[60vh] space-y-1 overflow-y-auto">
              {runs.data?.map((run) => (
                <button
                  key={run.run_id}
                  type="button"
                  onClick={() => setRunId(run.run_id)}
                  className={`flex w-full items-center justify-between gap-2 rounded-md border px-3 py-2 text-left text-xs transition-colors ${
                    runId === run.run_id
                      ? "border-primary/50 bg-accent"
                      : "border-border hover:bg-muted/50"
                  }`}
                >
                  <span className="font-mono">{shortId(run.run_id)}</span>
                  <BaselineBadge mode={run.baseline_mode} />
                </button>
              ))}
            </div>
          </QueryState>
        </Section>

        <div className="min-w-0 space-y-4">
          <Section
            title="逐条规则结论"
            description="UNDETERMINED 是合法结论（Spec §4.3 无基线时相对规则无法判定），不是加载失败。"
          >
            {!runId ? (
              <p className="text-sm text-muted-foreground">从左侧选择 run。</p>
            ) : (
              <QueryState
                isLoading={detail.isLoading}
                error={detail.error}
                onRetry={() => void detail.refetch()}
              >
                {detail.data && (
                  <div className="space-y-4">
                    <div className="flex flex-wrap items-center gap-3">
                      <VerdictBadge verdict={detail.data.verdict} />
                      <ExitCodeBadge code={detail.data.exit_code} />
                      <Badge variant={detail.data.source === "stored" ? "neutral" : "warn"}>
                        {detail.data.source === "stored" ? "当时判定（gate.json）" : "重放结果"}
                      </Badge>
                      <span className="text-xs text-muted-foreground">
                        gate={detail.data.gate}
                      </span>
                    </div>

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
                        {detail.data.rules.map((rule) => (
                          <TableRow key={rule.rule}>
                            <TableCell className="font-mono text-xs">{rule.rule}</TableCell>
                            <TableCell className="tabular text-xs">
                              {fmtNumber(rule.observed, 4)}
                            </TableCell>
                            <TableCell className="tabular text-xs">
                              {fmtNumber(rule.threshold, 4)}
                            </TableCell>
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
                            <TableCell className="text-xs text-muted-foreground">
                              {rule.detail || "—"}
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>

                    <KeyValue
                      columns={4}
                      items={Object.entries(detail.data.aggregate).map(([key, value]) => ({
                        label: key,
                        value: String(value),
                      }))}
                    />
                  </div>
                )}
              </QueryState>
            )}
          </Section>

          <Section
            title="Gate 历史"
            description="按 started_at 倒序的最近 run 结论。"
          >
            <QueryState
              isLoading={history.isLoading}
              error={history.error}
              isEmpty={history.data?.length === 0}
              emptyTitle="还没有 gate 记录"
            >
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Run</TableHead>
                    <TableHead>Verdict</TableHead>
                    <TableHead>Baseline</TableHead>
                    <TableHead>Exit</TableHead>
                    <TableHead>Cases</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {history.data?.map((row) => (
                    <TableRow key={`${row.run_id}-${row.gate}`}>
                      <TableCell>
                        <Link
                          to={`/runs/${row.run_id}`}
                          className="font-mono text-xs text-primary hover:underline"
                        >
                          {shortId(row.run_id)}
                        </Link>
                      </TableCell>
                      <TableCell>
                        <VerdictBadge verdict={row.verdict} />
                      </TableCell>
                      <TableCell>
                        <BaselineBadge mode={row.baseline_mode} />
                      </TableCell>
                      <TableCell>
                        <ExitCodeBadge code={row.exit_code} />
                      </TableCell>
                      <TableCell className="tabular text-xs">
                        {row.aggregate.cases ?? "—"} cases / {row.aggregate.failures ?? 0} failures
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </QueryState>
          </Section>
        </div>
      </div>

      <Section title="Baseline 台账（Spec §4.4）" description="pin / unpin 记录的当前有效状态。">
        <BaselineTable />
      </Section>
    </div>
  );
}

function BaselineTable() {
  const query = useQuery({ queryKey: ["baselines"], queryFn: () => api.baselines() });
  const [resolved, setResolved] = React.useState<string | null>(null);
  const [benchmark, setBenchmark] = React.useState("database-core");
  const resolution = useQuery({
    queryKey: ["baseline-resolve", benchmark, resolved],
    queryFn: () => api.resolveBaseline(benchmark, resolved!),
    enabled: Boolean(resolved),
  });

  return (
    <div className="space-y-3">
      <QueryState
        isLoading={query.isLoading}
        error={query.error}
        isEmpty={query.data?.length === 0}
        emptyTitle="还没有 pin 任何 baseline"
        emptyHint="`agent-eval baseline pin <run> --benchmark <name> --mode release`"
      >
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Benchmark</TableHead>
              <TableHead>Dataset</TableHead>
              <TableHead>Mode</TableHead>
              <TableHead>Pinned run</TableHead>
              <TableHead>Pinned at</TableHead>
              <TableHead>Note</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {query.data?.map((row) => (
              <TableRow key={row.id}>
                <TableCell>{row.benchmark_id}</TableCell>
                <TableCell className="font-mono text-xs">{row.dataset_version}</TableCell>
                <TableCell>
                  <BaselineBadge mode={row.mode} />
                </TableCell>
                <TableCell className="font-mono text-xs">
                  {row.pinned_run_id ? (
                    <Link
                      to={`/runs/${row.pinned_run_id}`}
                      className="text-primary hover:underline"
                    >
                      {shortId(row.pinned_run_id)}
                    </Link>
                  ) : (
                    "—"
                  )}
                </TableCell>
                <TableCell className="text-xs text-muted-foreground">
                  {fmtRelative(row.pinned_at)}
                </TableCell>
                <TableCell className="max-w-xs truncate text-xs text-muted-foreground">
                  {row.note || "—"}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </QueryState>

      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs text-muted-foreground">解析预览（Spec §4.2）：</span>
        <Select value={resolved ?? ""} onValueChange={setResolved}>
          <SelectTrigger className="w-40">
            <SelectValue placeholder="选择模式" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="release">release</SelectItem>
            <SelectItem value="main-latest">main-latest</SelectItem>
          </SelectContent>
        </Select>
        <Select value={benchmark} onValueChange={setBenchmark}>
          <SelectTrigger className="w-52">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="database-core">database-core</SelectItem>
          </SelectContent>
        </Select>
        {resolved && resolution.data !== undefined && (
          <span className="text-xs">
            {resolution.isLoading
              ? "解析中…"
              : resolution.data
                ? `→ ${resolution.data.pinned_run_id}`
                : "→ 无合格基线（将降级 NO_BASELINE）"}
          </span>
        )}
      </div>
    </div>
  );
}
