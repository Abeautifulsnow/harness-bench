import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";
import { ShieldAlert, ShieldCheck } from "lucide-react";

import { VerdictBadge } from "@/components/common/badges";
import { Mono, PageHeader, Section, StatCard } from "@/components/common/primitives";
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
import { shortId } from "@/lib/format";

export function Security() {
  const runs = useQuery({ queryKey: ["runs", { limit: 100 }], queryFn: () => api.runs({ limit: 100 }) });
  const [runId, setRunId] = React.useState("");
  const posture = useQuery({
    queryKey: ["security", runId],
    queryFn: () => api.security(runId || undefined),
  });

  React.useEffect(() => {
    if (!runId && runs.data?.length) setRunId(runs.data[0].run_id);
  }, [runs.data, runId]);

  return (
    <div className="space-y-5">
      <PageHeader
        title="Security"
        description="PRD §62/§63，Spec §12：安全判定走独立 security 挂载点，评估的是**行为**（工具参数/命令/路径/ SQL /MCP），不是输出文本。所有规则都是 blocking，且不可被 LLM judge 覆盖（PRD §110-10）。"
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

      <QueryState
        isLoading={posture.isLoading}
        error={posture.error}
        onRetry={() => void posture.refetch()}
      >
        {posture.data && (
          <>
            <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
              <StatCard
                label="安全失败"
                value={posture.data.failed_findings}
                tone={posture.data.failed_findings > 0 ? "fail" : "pass"}
                detail={`${posture.data.findings.length} 条判定`}
              />
              <StatCard
                label="安全套件 Case"
                value={posture.data.suites.find((suite) => suite.tag === "security")?.cases ?? 0}
              />
              <StatCard
                label="红队 Case"
                value={posture.data.suites.find((suite) => suite.tag === "red-team")?.cases ?? 0}
              />
              <StatCard
                label="攻击面覆盖"
                value={`${posture.data.coverage.filter((row) => row.covered).length}/${
                  posture.data.coverage.length
                }`}
                tone={
                  posture.data.coverage.every((row) => row.covered) ? "pass" : "warn"
                }
                detail="缺一类即视为可见负债"
              />
            </div>

            <div className="grid gap-4 xl:grid-cols-2">
              <Section
                title="安全 / 红队套件（PRD §62/§63）"
                description="套件按 tag 选择 case，清单不硬编码。"
              >
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Suite</TableHead>
                      <TableHead>Tag</TableHead>
                      <TableHead>Cases</TableHead>
                      <TableHead>说明</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {posture.data.suites.map((suite) => (
                      <TableRow key={suite.name}>
                        <TableCell className="font-medium">{suite.name}</TableCell>
                        <TableCell>
                          <Mono>{suite.tag}</Mono>
                        </TableCell>
                        <TableCell className="tabular">{suite.cases}</TableCell>
                        <TableCell className="text-xs text-muted-foreground">
                          {suite.description}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </Section>

              <Section
                title="红队覆盖矩阵（PRD §62 八类攻击面）"
                description="每一类至少需要一个 case；未覆盖的类别直接标红，不做粉饰。"
              >
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>攻击面</TableHead>
                      <TableHead>覆盖</TableHead>
                      <TableHead>Cases</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {posture.data.coverage.map((row) => (
                      <TableRow key={row.category}>
                        <TableCell className="flex items-center gap-2">
                          {row.covered ? (
                            <ShieldCheck className="size-3.5 text-[var(--pass)]" />
                          ) : (
                            <ShieldAlert className="size-3.5 text-[var(--fail)]" />
                          )}
                          <Mono>{row.category}</Mono>
                        </TableCell>
                        <TableCell>
                          <Badge variant={row.covered ? "pass" : "fail"}>
                            {row.covered ? "已覆盖" : "缺失"}
                          </Badge>
                        </TableCell>
                        <TableCell className="text-xs text-muted-foreground">
                          {row.case_ids.length ? row.case_ids.join(", ") : "—"}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </Section>
            </div>

            <Section
              title="本次 Run 的安全判定"
              description="每条失败的 reason 里，被命中的密钥/凭据已做脱敏。"
            >
              <QueryState
                isLoading={false}
                error={null}
                isEmpty={posture.data.findings.length === 0}
                emptyTitle="该 run 没有安全断言命中"
                emptyHint="这个 run 的 case 里没有声明 security:，也不属于安全/红队套件。"
              >
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Case</TableHead>
                      <TableHead>Iter</TableHead>
                      <TableHead>Rule</TableHead>
                      <TableHead>Verdict</TableHead>
                      <TableHead>Blocking</TableHead>
                      <TableHead>Reason</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {posture.data.findings.map((finding, index) => (
                      <TableRow key={`${finding.case_run_id}-${finding.metric}-${index}`}>
                        <TableCell className="font-mono text-xs">
                          <Link
                            to={`/runs/${runId}`}
                            className="text-primary hover:underline"
                          >
                            {finding.case_id}
                          </Link>
                        </TableCell>
                        <TableCell className="tabular text-xs">{finding.iteration}</TableCell>
                        <TableCell className="font-mono text-xs">{finding.metric}</TableCell>
                        <TableCell>
                          <VerdictBadge verdict={finding.verdict} />
                        </TableCell>
                        <TableCell>
                          {finding.blocking ? (
                            <Badge variant="fail">hard gate</Badge>
                          ) : (
                            <span className="text-xs text-muted-foreground">no</span>
                          )}
                        </TableCell>
                        <TableCell className="max-w-lg text-xs text-muted-foreground">
                          {finding.reason ?? "—"}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </QueryState>
            </Section>
          </>
        )}
      </QueryState>
    </div>
  );
}
