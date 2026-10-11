import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";
import { RotateCcw, XCircle } from "lucide-react";

import { Mono, Section } from "@/components/common/primitives";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
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
import {
  JOB_STATUS_LABEL,
  JOB_TERMINAL,
  evaluationApi,
  subscribeJobEvents,
  type EvalRunJob,
  type ReplayRecord,
} from "@/lib/evaluation-api";
import { fmtRelative } from "@/lib/format";

const NO_POLICY = "__none__";
/** §63 review #S01：重放预算的前端上限——后端只挡非正数，巨值会让 UI 挂起很久。 */
const MAX_TIMEOUT_SECONDS = 600;

/** 超时输入钳制：挡 NaN/0/负数，并封顶。 */
function clampTimeoutSeconds(value: number): number {
  if (!Number.isFinite(value) || value < 1) return 1;
  return Math.min(MAX_TIMEOUT_SECONDS, Math.floor(value));
}

/** §18 订阅的 replay 侧薄封装：终态即退订（EventSource 会对已结束的流自动重连）。
 *
 * 依赖刻意只挂 jobId：onEvent/onActivity 是调用方稳定的 setState/查询客户端，
 * 只有换 job 才需要重建订阅。
 */
function useReplayJobEvents(
  jobId: string | null,
  onEvent: (job: EvalRunJob) => void,
  onActivity: (type: string) => void,
): void {
  const unsubscribe = React.useRef<(() => void) | null>(null);
  React.useEffect(() => {
    if (!jobId) return;
    unsubscribe.current = subscribeJobEvents({
      jobId,
      kind: "replay",
      onEvent: (next) => {
        onEvent(next);
        if (JOB_TERMINAL.has(next.status)) {
          unsubscribe.current?.();
          unsubscribe.current = null;
        }
      },
      onActivity: (event) => onActivity(event.type),
    });
    return () => {
      unsubscribe.current?.();
      unsubscribe.current = null;
    };
  }, [jobId]);
}

/** 会话控制器：选择状态、订阅与两个 mutation。渲染结构见 ReplaySection。 */
function useReplayController(traceId: string, cases: string[]) {
  const queryClient = useQueryClient();
  const connections = useQuery({
    queryKey: ["agent-connections"],
    queryFn: evaluationApi.agentConnections,
  });
  const policies = useQuery({ queryKey: ["eval-policies"], queryFn: evaluationApi.evalPolicies });
  const records = useQuery({
    queryKey: ["production-replays", traceId],
    // 服务端过滤（§63 review）：别把全部记录拉到浏览器再筛。
    queryFn: () => evaluationApi.productionReplays(traceId),
  });

  const [profileId, setProfileId] = React.useState("");
  const [caseId, setCaseId] = React.useState(cases[0] ?? "");
  const [policyId, setPolicyId] = React.useState(NO_POLICY);
  const [sanitize, setSanitize] = React.useState(true);
  const [timeoutSeconds, setTimeoutSeconds] = React.useState(60);
  const [job, setJob] = React.useState<EvalRunJob | null>(null);
  const [activity, setActivity] = React.useState("");

  React.useEffect(() => {
    if (!profileId && connections.data?.length) {
      const first = connections.data.find((c) => c.enabled) ?? connections.data[0];
      setProfileId(first.id);
    }
  }, [connections.data, profileId]);

  React.useEffect(() => {
    setCaseId((previous) => (previous && cases.includes(previous) ? previous : (cases[0] ?? "")));
  }, [cases]);

  useReplayJobEvents(
    job?.job_id ?? null,
    (next) => {
      setJob(next);
      if (JOB_TERMINAL.has(next.status)) {
        void queryClient.invalidateQueries({ queryKey: ["production-replays", traceId] });
      }
    },
    setActivity,
  );

  const start = useMutation({
    mutationFn: () =>
      evaluationApi.createReplayRun({
        agent_profile: profileId,
        trace_id: traceId,
        case_id: caseId || null,
        sanitize,
        replay_timeout_seconds: timeoutSeconds,
        policy: policyId === NO_POLICY ? null : policyId,
      }),
    onSuccess: (created) => {
      setActivity("");
      setJob(created);
    },
  });

  // §63 review：长重放必须能从中止——这是 Job 化的核心收益之一，没有按钮等于没有。
  const cancel = useMutation({
    mutationFn: () => {
      if (!job) throw new Error("no active replay job");
      return evaluationApi.cancelReplayRun(job.job_id);
    },
    onSuccess: (updated) => setJob(updated),
  });

  return {
    connections,
    policies,
    records,
    cases,
    profileId,
    setProfileId,
    caseId,
    setCaseId,
    policyId,
    setPolicyId,
    sanitize,
    setSanitize,
    timeoutSeconds,
    setTimeoutSeconds: (value: number) => setTimeoutSeconds(clampTimeoutSeconds(value)),
    job,
    activity,
    running: job !== null && !JOB_TERMINAL.has(job.status),
    start,
    cancel,
  };
}

type ReplayController = ReturnType<typeof useReplayController>;

/** 工具栏：候选 agent / case / policy 选择 + 脱敏与预算 + 发起/取消。
 *
 * 纯 props 渲染（§63.7 review #C02 的拆分产物），选择项的默认值逻辑都在控制器里。
 */
function ReplayToolbar({ controller }: { controller: ReplayController }) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Select value={controller.profileId} onValueChange={controller.setProfileId}>
        <SelectTrigger className="w-48">
          <SelectValue placeholder="选择候选 agent" />
        </SelectTrigger>
        <SelectContent>
          {(controller.connections.data ?? []).map((connection) => (
            <SelectItem key={connection.id} value={connection.id} disabled={!connection.enabled}>
              {connection.display_name}
              {!connection.enabled && "（已禁用）"}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {controller.cases.length > 1 && (
        <Select value={controller.caseId} onValueChange={controller.setCaseId}>
          <SelectTrigger className="w-44">
            <SelectValue placeholder="选择 case" />
          </SelectTrigger>
          <SelectContent>
            {controller.cases.map((id) => (
              <SelectItem key={id} value={id}>
                {id}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}
      <Select value={controller.policyId} onValueChange={controller.setPolicyId}>
        <SelectTrigger className="w-44">
          <SelectValue placeholder="评测策略（可选）" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={NO_POLICY}>不评（只比结构）</SelectItem>
          {(controller.policies.data ?? []).map((policy) => (
            <SelectItem key={policy.id} value={policy.id}>
              {policy.display_name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <label className="flex items-center gap-1 text-xs text-muted-foreground">
        <input
          type="checkbox"
          className="h-4 w-4 rounded border border-input"
          checked={controller.sanitize}
          onChange={(event) => controller.setSanitize(event.target.checked)}
        />
        发送前脱敏
      </label>
      <Input
        type="number"
        min={1}
        max={MAX_TIMEOUT_SECONDS}
        className="w-20"
        value={controller.timeoutSeconds}
        onChange={(event) => controller.setTimeoutSeconds(Number(event.target.value))}
      />
      <Button
        size="sm"
        disabled={
          !controller.profileId || !controller.caseId || controller.running || controller.start.isPending
        }
        onClick={() => controller.start.mutate()}
      >
        <RotateCcw /> {controller.running ? "重放中…" : "Run Replay"}
      </Button>
      {controller.running && (
        <Button
          variant="destructive"
          size="sm"
          disabled={controller.cancel.isPending}
          onClick={() => controller.cancel.mutate()}
        >
          <XCircle /> {controller.cancel.isPending ? "取消中…" : "取消重放"}
        </Button>
      )}
    </div>
  );
}

/** 运行中/已结束 Job 的状态行：status、排队位次、活动、failure 与 replay_id。 */
function JobStatusBar({ job, activity }: { job: EvalRunJob; activity: string }) {
  return (
    <div className="mb-3 flex flex-wrap items-center gap-2 text-xs">
      <Badge variant="outline">{JOB_STATUS_LABEL[job.status]}</Badge>
      <Mono>{job.job_id}</Mono>
      {job.queue_position != null && <span>排队第 {job.queue_position} 位</span>}
      {activity && <span className="text-muted-foreground">· {activity}</span>}
      {job.status === "failed" && job.failure_kind && (
        <Badge variant="outline" className="text-[var(--fail)]">
          {job.failure_kind}
        </Badge>
      )}
      {job.replay_id && <Mono>{job.replay_id}</Mono>}
    </div>
  );
}

/** 可选 judge 打分的双侧对照表（§62：给了 policy 才有，没给不编造分数）。 */
function JudgeScoreTable({
  original,
  replay,
}: {
  original: NonNullable<ReplayRecord["comparison"]>["evaluation_original"];
  replay: NonNullable<ReplayRecord["comparison"]>["evaluation_replay"];
}) {
  if (!original || !replay) return null;
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Metric</TableHead>
          <TableHead>原分</TableHead>
          <TableHead>重放分</TableHead>
          <TableHead>Δ</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {Object.entries(original).map(([metric, stat]) => {
          const replayed = replay[metric];
          const delta =
            stat.mean_score != null && replayed?.mean_score != null
              ? replayed.mean_score - stat.mean_score
              : null;
          return (
            <TableRow key={metric}>
              <TableCell className="text-xs">{metric}</TableCell>
              <TableCell className="tabular text-xs">{stat.mean_score ?? "—"}</TableCell>
              <TableCell className="tabular text-xs">{replayed?.mean_score ?? "—"}</TableCell>
              <TableCell
                className={
                  delta == null
                    ? "tabular text-xs text-muted-foreground"
                    : delta < 0
                      ? "tabular text-xs text-[var(--fail)]"
                      : "tabular text-xs text-[var(--pass)]"
                }
              >
                {delta == null ? "—" : delta.toFixed(3)}
              </TableCell>
            </TableRow>
          );
        })}
      </TableBody>
    </Table>
  );
}

/** 单条重放记录的对比视图：结论徽标 + 工具序列 + 可选 judge 分差。 */
function ComparisonView({ record }: { record: ReplayRecord }) {
  const comparison = record.comparison;
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <Badge
          variant="outline"
          className={record.status === "completed" ? "text-[var(--pass)]" : "text-[var(--fail)]"}
        >
          {record.status === "completed" ? "完成" : "失败"}
        </Badge>
        <Badge variant="outline">脱敏 {record.sanitize.redactions} 处</Badge>
        <span className="text-muted-foreground">{fmtRelative(record.created_at)}</span>
        {record.duration_ms != null && (
          <span className="text-muted-foreground">· {(record.duration_ms / 1000).toFixed(1)}s</span>
        )}
        {comparison && (
          <Badge
            variant="outline"
            className={comparison.output_changed ? "text-[var(--fail)]" : "text-[var(--pass)]"}
          >
            输出{comparison.output_changed ? "已变化" : "未变"}
          </Badge>
        )}
        {comparison && (
          <Badge variant="outline">
            工具序列差异 {comparison.tool_sequence_jaccard.toFixed(3)}
          </Badge>
        )}
        {record.replay_trace_id && (
          <Button variant="ghost" size="sm" asChild>
            <Link to={`/production/${record.replay_trace_id}`}>看重放 trace</Link>
          </Button>
        )}
      </div>
      {record.error && (
        <p className="rounded-md border border-[var(--fail)]/30 bg-[var(--fail)]/5 px-3 py-2 text-xs text-[var(--fail)]">
          {record.error}
        </p>
      )}
      {comparison && (
        <div className="space-y-2">
          <div className="grid gap-1 text-xs">
            <div>
              <span className="text-muted-foreground">原始工具序列：</span>
              <span className="font-mono">
                {comparison.tool_sequence_original.join(" → ") || "（无）"}
              </span>
            </div>
            <div>
              <span className="text-muted-foreground">重放工具序列：</span>
              <span className="font-mono">
                {comparison.tool_sequence_replay.join(" → ") || "（无）"}
              </span>
            </div>
          </div>
          {comparison.evaluation_note && (
            <p className="text-xs text-muted-foreground">{comparison.evaluation_note}</p>
          )}
          {comparison.evaluation_original && comparison.evaluation_replay && (
            <JudgeScoreTable
              original={comparison.evaluation_original}
              replay={comparison.evaluation_replay}
            />
          )}
        </div>
      )}
    </div>
  );
}

/** §63：把生产 Trace 用候选 agent 重放，并与历史行为对比。
 *
 * 走执行面 Job（`POST /api/replay-runs`）而不是 §62 的同步端点：重放天然是长任务
 * （真实 agent 几十秒到数分钟），同步入口会把浏览器挂在一个请求上（§14）。
 * 进度来自同一个 SSE（§18 的帧契约），状态与 mutations 在 useReplayController。
 */
export function ReplaySection({ traceId, cases }: { traceId: string; cases: string[] }) {
  const controller = useReplayController(traceId, cases);
  const mine = controller.records.data ?? [];
  const shown = mine[0] ?? null;

  return (
    <Section
      title="Trace Replay"
      description="§63：脱敏后用候选 agent 重跑这条生产输入，与历史行为比较。走执行面 Job，可取消、有实时进度；不落 Run（产出的是 source=replay 的一条 trace）。"
      actions={<ReplayToolbar controller={controller} />}
    >
      {controller.start.error && (
        <p className="mb-3 rounded-md border border-[var(--fail)]/30 bg-[var(--fail)]/5 px-3 py-2 text-xs text-[var(--fail)]">
          {(controller.start.error as Error).message}
        </p>
      )}
      {controller.job && (
        <JobStatusBar job={controller.job} activity={controller.activity} />
      )}
      {controller.job?.error && (
        <p className="mb-3 rounded-md border border-[var(--fail)]/30 bg-[var(--fail)]/5 px-3 py-2 text-xs text-[var(--fail)]">
          {controller.job.error}
        </p>
      )}
      {controller.cancel.error && (
        <p className="mb-3 rounded-md border border-[var(--fail)]/30 bg-[var(--fail)]/5 px-3 py-2 text-xs text-[var(--fail)]">
          取消失败：{(controller.cancel.error as Error).message}
        </p>
      )}
      {!shown && !controller.running && (
        <p className="text-xs text-muted-foreground">
          {controller.connections.data?.length
            ? "选择候选 agent 后运行；重放会真实调用一次该 agent（消耗其算力/额度）。"
            : "先在 evals/agents/*.yaml 注册 Agent Connection 后刷新。"}
        </p>
      )}
      {shown && <ComparisonView record={shown} />}
      {mine.length > 1 && (
        <p className="mt-2 text-xs text-muted-foreground">
          该 trace 共 {mine.length} 次重放；上方为最新一次（{shown?.replay_id}）。
        </p>
      )}
    </Section>
  );
}
