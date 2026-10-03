import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { Link } from "react-router";

import { Mono, PageHeader, Section } from "@/components/common/primitives";
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
import { fmtRelative } from "@/lib/format";
import { evaluationApi, type ScheduleView, type TriggerView } from "@/lib/evaluation-api";

/** V2 §52：自动化视图——定时评测（Scheduled/Nightly）与 Webhook/CI 触发器。
 *  定义都在 evals/ 的 Git 树里（schedules/ presets/ triggers/），这里只读。 */
export function Automation() {
  const schedules = useQuery({ queryKey: ["schedules"], queryFn: evaluationApi.schedules });
  const triggers = useQuery({ queryKey: ["triggers"], queryFn: evaluationApi.triggers });

  return (
    <div className="space-y-5">
      <PageHeader
        title="自动化"
        description="V2 §52：定时评测与触发器。定义随 Git 管理（evals/schedules|triggers/*.yaml），运行台账在 .agent-eval/state/。"
      />
      <ScheduleSection query={schedules} />
      <TriggerSection query={triggers} />
    </div>
  );
}

function ScheduleSection({
  query,
}: {
  query: UseQueryResult<ScheduleView[]>;
}) {
  return (
    <Section
      title="定时评测"
      description="本地时间 cron（5 字段）。进程停机跨过触发点时合并为一次补跑，不回填历史。"
    >
      <QueryState
        isLoading={query.isLoading}
        error={query.error}
        isEmpty={query.data?.length === 0}
        emptyTitle="还没有调度定义"
        emptyHint="在 evals/schedules/<id>.yaml 定义（可参考 nightly-database-core.yaml），保存后刷新。"
        onRetry={() => void query.refetch()}
      >
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>ID</TableHead>
              <TableHead>Cron</TableHead>
              <TableHead>Preset</TableHead>
              <TableHead>状态</TableHead>
              <TableHead>上次触发</TableHead>
              <TableHead>上次 Job</TableHead>
              <TableHead>下次触发</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {query.data?.map((row) => (
              <TableRow key={row.id}>
                <TableCell>
                  <div className="text-sm font-medium">{row.display_name}</div>
                  <Mono className="text-muted-foreground">{row.id}</Mono>
                </TableCell>
                <TableCell>
                  <Mono>{row.cron}</Mono>
                </TableCell>
                <TableCell className="text-xs">{row.preset ?? "—"}</TableCell>
                <TableCell>
                  {row.error ? (
                    <Badge variant="outline" className="bg-[var(--fail)]/10 text-[var(--fail)]" title={row.error}>
                      提交失败
                    </Badge>
                  ) : (
                    <Badge variant="outline" className={row.enabled ? "text-[var(--pass)]" : "text-muted-foreground"}>
                      {row.enabled ? "启用" : "停用"}
                    </Badge>
                  )}
                </TableCell>
                <TableCell className="text-xs text-muted-foreground">{fmtRelative(row.last_run_at)}</TableCell>
                <TableCell>
                  {row.last_job_id ? (
                    <Link to={`/evaluations/${row.last_job_id}`} className="font-mono text-xs text-primary hover:underline">
                      {row.last_job_id.slice(0, 14)}
                    </Link>
                  ) : (
                    <span className="text-xs text-muted-foreground">—</span>
                  )}
                </TableCell>
                <TableCell className="text-xs text-muted-foreground">
                  {row.enabled ? fmtRelative(row.next_run_at) : "—"}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </QueryState>
    </Section>
  );
}

function TriggerSection({
  query,
}: {
  query: UseQueryResult<TriggerView[]>;
}) {
  return (
    <Section
      title="Webhook / CI 触发器"
      description="POST /api/triggers/{id}/run，Bearer token（env 变量，值不进仓库）。CI 传 Idempotency-Key 防重放。"
    >
      <QueryState
        isLoading={query.isLoading}
        error={query.error}
        isEmpty={query.data?.length === 0}
        emptyTitle="还没有触发器定义"
        emptyHint="在 evals/triggers/<id>.yaml 定义（可参考 ci-database.yaml.example），保存后刷新。"
        onRetry={() => void query.refetch()}
      >
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>ID</TableHead>
              <TableHead>Preset</TableHead>
              <TableHead>状态</TableHead>
              <TableHead>Token</TableHead>
              <TableHead>调用方式</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {query.data?.map((row) => (
              <TableRow key={row.id}>
                <TableCell>
                  <div className="text-sm font-medium">{row.display_name}</div>
                  <Mono className="text-muted-foreground">{row.id}</Mono>
                </TableCell>
                <TableCell className="text-xs">{row.preset ?? "内联 request"}</TableCell>
                <TableCell>
                  <Badge variant="outline" className={row.enabled ? "text-[var(--pass)]" : "text-muted-foreground"}>
                    {row.enabled ? "启用" : "停用"}
                  </Badge>
                </TableCell>
                <TableCell>
                  <Badge
                    variant="outline"
                    className={
                      row.token_state === "configured"
                        ? "text-[var(--pass)]"
                        : row.token_state === "missing"
                          ? "text-[var(--fail)]"
                          : "text-muted-foreground"
                    }
                  >
                    {row.token_state === "none" ? "无鉴权" : row.token_state === "configured" ? "已配置" : "缺失"}
                  </Badge>
                  {row.token_ref && <Mono className="ml-2 text-muted-foreground">{row.token_ref}</Mono>}
                </TableCell>
                <TableCell>
                  <Mono className="text-muted-foreground">POST /api/triggers/{row.id}/run</Mono>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </QueryState>
    </Section>
  );
}
