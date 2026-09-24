import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";

import { PassRate, StabilityBadge, VerdictBadge } from "@/components/common/badges";
import { Mono, PageHeader, Section } from "@/components/common/primitives";
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
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api } from "@/lib/api";
import { fmtRelative, shortId } from "@/lib/format";

const REASON_LABELS: Record<string, string> = {
  flaky: "波动",
  "evaluation-failure": "评测失败",
  security: "安全",
  "near-threshold": "临阈值",
  "evaluator-conflict": "评测器分歧",
  regression: "回归退步",
};

export function Reviews() {
  const runs = useQuery({ queryKey: ["runs", { limit: 100 }], queryFn: () => api.runs({ limit: 100 }) });
  const options = useQuery({ queryKey: ["review-options"], queryFn: api.reviewOptions });
  const [runId, setRunId] = React.useState("");

  React.useEffect(() => {
    if (!runId && runs.data?.length) setRunId(runs.data[0].run_id);
  }, [runs.data, runId]);

  const queue = useQuery({
    queryKey: ["review-queue", runId],
    queryFn: () => api.reviewQueue(runId),
    enabled: Boolean(runId),
  });
  const reviews = useQuery({
    queryKey: ["reviews", runId],
    queryFn: () => api.reviews({ run_id: runId || undefined }),
    enabled: Boolean(runId),
  });

  return (
    <div className="space-y-5">
      <PageHeader
        title="Human Review"
        description="PRD §60/§61，Spec §5.2/§5.4：五种结论（PASS/FAIL/EXPECTED/FALSE_POSITIVE/FALSE_NEGATIVE），后三种必须写 note。人工与机器结论并存，不互相覆盖。"
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

      <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_320px]">
        <div className="min-w-0 space-y-4">
          {!runId ? (
            <Section title="选择 Run">
              <p className="text-sm text-muted-foreground">从右上角选择一个 run。</p>
            </Section>
          ) : (
            <Tabs defaultValue="queue">
              <TabsList>
                <TabsTrigger value="queue">Review Queue（入队建议）</TabsTrigger>
                <TabsTrigger value="records">已记录结论</TabsTrigger>
              </TabsList>

              <TabsContent value="queue">
                <Section
                  title="Review Queue 候选"
                  description="只做入队建议，不写库：入队是审计动作，需要人确认或由 CI 显式调用。"
                >
                  <QueryState
                    isLoading={queue.isLoading}
                    error={queue.error}
                    isEmpty={queue.data?.total === 0}
                    emptyTitle="没有需要人工复核的 case"
                    emptyHint="没有命中任何入队理由（波动 / 评测失败 / 安全 / 临阈值 / 评测器分歧 / 回归退步）。"
                    onRetry={() => void queue.refetch()}
                  >
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>Case</TableHead>
                          <TableHead>入队理由</TableHead>
                          <TableHead>机器结论</TableHead>
                          <TableHead>稳定性</TableHead>
                          <TableHead>Pass rate</TableHead>
                          <TableHead>Iterations</TableHead>
                          <TableHead>全部理由</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {queue.data?.candidates.map((row) => (
                          <TableRow key={row.case_id}>
                            <TableCell className="font-mono text-xs">{row.case_id}</TableCell>
                            <TableCell>
                              <Badge variant="warn">
                                {REASON_LABELS[row.queue_reason] ?? row.queue_reason}
                              </Badge>
                            </TableCell>
                            <TableCell>
                              <VerdictBadge verdict={row.machine_verdict} />
                            </TableCell>
                            <TableCell>
                              <StabilityBadge stability={row.stability} />
                            </TableCell>
                            <TableCell>
                              <PassRate value={row.pass_rate} />
                            </TableCell>
                            <TableCell className="tabular text-xs">{row.iterations}</TableCell>
                            <TableCell className="text-xs text-muted-foreground">
                              {(row.detail ?? "")
                                .split(", ")
                                .map((reason) => REASON_LABELS[reason] ?? reason)
                                .join(" / ")}
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </QueryState>
                </Section>
              </TabsContent>

              <TabsContent value="records">
                <Section
                  title="已记录结论"
                  description="机器结论随行显示：人工 verdict 与机器 verdict 是两个独立字段。"
                >
                  <QueryState
                    isLoading={reviews.isLoading}
                    error={reviews.error}
                    isEmpty={reviews.data?.total === 0}
                    emptyTitle="还没有人工结论"
                    emptyHint="用 CLI 记录：`agent-eval review add <run> <case> --verdict EXPECTED --note ...`"
                  >
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>Case</TableHead>
                          <TableHead>Reviewer</TableHead>
                          <TableHead>人工结论</TableHead>
                          <TableHead>机器结论</TableHead>
                          <TableHead>Note</TableHead>
                          <TableHead>时间</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {reviews.data?.reviews.map((row) => (
                          <TableRow key={row.id}>
                            <TableCell className="font-mono text-xs">{row.case_id}</TableCell>
                            <TableCell className="text-xs">{row.reviewer}</TableCell>
                            <TableCell>
                              <Badge
                                variant={
                                  row.verdict === "PASS"
                                    ? "pass"
                                    : row.verdict === "FAIL"
                                      ? "fail"
                                      : "warn"
                                }
                              >
                                {row.verdict || "待评审"}
                              </Badge>
                            </TableCell>
                            <TableCell>
                              <VerdictBadge verdict={row.machine_verdict} />
                            </TableCell>
                            <TableCell className="max-w-md text-xs text-muted-foreground">
                              {row.note || "—"}
                            </TableCell>
                            <TableCell className="text-xs text-muted-foreground">
                              {fmtRelative(row.created_at)}
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </QueryState>
                </Section>
              </TabsContent>
            </Tabs>
          )}
        </div>

        <div className="space-y-4">
          <Section title="结论枚举（Spec §5.2）">
            <div className="space-y-2 text-xs">
              {options.data?.verdicts.map((verdict) => (
                <div key={verdict} className="space-y-0.5">
                  <Badge
                    variant={
                      verdict === "PASS" ? "pass" : verdict === "FAIL" ? "fail" : "warn"
                    }
                  >
                    {verdict}
                  </Badge>
                  <div className="text-muted-foreground">
                    {verdict === "PASS"
                      ? "机器判定正确，确认失败"
                      : verdict === "FAIL"
                        ? "机器判定遗漏，确认应为失败"
                        : verdict === "EXPECTED"
                          ? "这是预期行为（需 note 说明）"
                          : verdict === "FALSE_POSITIVE"
                            ? "误报：机器判失败但实际正确（需 note）"
                            : "漏报：机器判通过但实际失败（需 note）"}
                  </div>
                </div>
              ))}
            </div>
          </Section>

          <Section title="入队理由（PRD §61）">
            <div className="space-y-1 text-xs">
              {options.data?.queue_reasons.map((reason) => (
                <div key={reason} className="flex items-center gap-2">
                  <Mono className="text-muted-foreground">{reason}</Mono>
                  <span>{REASON_LABELS[reason] ?? ""}</span>
                </div>
              ))}
            </div>
          </Section>

          <Section title="Promote 草稿（PRD §51）">
            <DraftList />
          </Section>
        </div>
      </div>
    </div>
  );
}

function DraftList() {
  const query = useQuery({ queryKey: ["drafts"], queryFn: () => api.drafts() });
  return (
    <QueryState
      isLoading={query.isLoading}
      error={query.error}
      isEmpty={query.data?.length === 0}
      emptyTitle="还没有 Case Draft"
      emptyHint="`agent-eval promote <case-run-id> --run <run-id>` 由失败生成草稿。"
    >
      <div className="space-y-2">
        {query.data?.map((draft) => (
          <div key={draft.id} className="rounded-md border border-border px-2 py-1.5 text-xs">
            <div className="flex items-center justify-between gap-2">
              <Mono>{draft.case_id}</Mono>
              <Badge variant="neutral">{draft.draft_status}</Badge>
            </div>
            <div className="mt-0.5 text-muted-foreground">
              {draft.suite} · {draft.failure_category ?? "未分类"}
            </div>
            <Link
              to={`/runs/${draft.case_run_id.split("-")[0]}`}
              className="mt-0.5 block truncate text-[11px] text-primary hover:underline"
            >
              {draft.source_ref ?? draft.source_type ?? "—"}
            </Link>
          </div>
        ))}
      </div>
    </QueryState>
  );
}
