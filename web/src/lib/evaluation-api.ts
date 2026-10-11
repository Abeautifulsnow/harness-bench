/** 执行面 API 客户端（docs/web-evaluation-control-plane-design.md §16–§18、§63）。
 *
 * 与只读 `api.ts` 分开：执行面的 POST 全部是**执行动词**（§42：Definition
 * mutation 仍然禁止）——发起/取消评测（§16/§17）、发起/取消重放（§63），
 * 外加通知已读标记与生产摄取两类辅助动词。列表/详情/健康探测/重放记录仍是 GET。
 * 定义树数据（benchmark / suite / profile）继续从只读 `api.ts` 获取。
 */

import { ApiError } from "./api";

export interface EvalRunRequest {
  agent_profile: string;
  benchmark: string;
  suite?: string[] | null;
  profile?: string | null;
  repeat?: number | null;
  agent_concurrency?: number | null;
  no_judge?: boolean;
  strict_protocol?: boolean;
  save_trace?: boolean;
  tags?: string[];
  baseline_policy?: string | null;
  baseline_run?: string | null;
}

export interface JobProgress {
  total_trials: number | null;
  completed_trials: number;
  passed: number;
  failed: number;
  error: number;
}

export type JobStatus = "queued" | "running" | "cancelling" | "succeeded" | "failed" | "cancelled";

export interface EvalRunJob {
  job_id: string;
  status: JobStatus;
  request: EvalRunRequest;
  /** §63：账本是共用的，kind 区分 benchmark 评测与 Trace Replay。 */
  kind: "eval" | "replay";
  /** §63：重放不产生 Run——产出的是 source=replay 的一条 trace。 */
  replay_id: string | null;
  requested_by: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  run_id: string | null;
  gate_verdict: string | null;
  error: string | null;
  failure_kind: "INFRA_FAILURE" | "INVALID_REQUEST" | "INTERNAL" | null;
  cancel_requested: boolean;
  /** §25：读时事实（QUEUED 时由服务端动态补，不落盘）。 */
  queue_position: number | null;
  progress: JobProgress;
}

// ---------------------------------------------------------------- §63 Replay

export interface ReplayRunRequest {
  agent_profile: string;
  trace_id: string;
  case_id?: string | null;
  sanitize?: boolean;
  replay_timeout_seconds?: number;
  policy?: string | null;
}

export interface ReplayMetricStat {
  pass: number;
  fail: number;
  error: number;
  mean_score: number | null;
}

export interface ReplayComparison {
  tool_sequence_original: string[];
  tool_sequence_replay: string[];
  tool_sequence_jaccard: number;
  output_original: string | null;
  output_replay: string | null;
  output_changed: boolean;
  /** 给了 policy 才对两侧同尺打分；拿不到重放输出时这里写着为什么没评。 */
  evaluation_note?: string;
  evaluation_original?: Record<string, ReplayMetricStat>;
  evaluation_replay?: Record<string, ReplayMetricStat>;
}

/** ReplayStore 的记录（GET /api/production/replays/<built-in function id>）。 */
export interface ReplayRecord {
  replay_id: string;
  trace_id: string;
  case_id: string;
  status: "completed" | "error";
  error: string | null;
  sanitize: { enabled: boolean; redactions: number };
  source_input: string;
  prompt: string;
  endpoint: string;
  replay_trace_id: string | null;
  comparison: ReplayComparison | null;
  created_at: string;
  finished_at?: string;
  duration_ms?: number;
}

export interface AgentConnection {
  id: string;
  display_name: string;
  endpoint: string;
  enabled: boolean;
  description: string;
  auth_type: "bearer" | null;
  secret_ref: string | null;
  secret_state: "configured" | "missing" | "none";
}

export interface AgentHealthReport {
  ok: boolean;
  agent_model: string | null;
  observation_surface: Record<string, boolean>;
  detail: string;
}

/** V2 §52：Scheduled Evaluation / Preset / Trigger。 */
export interface ScheduleView {
  id: string;
  display_name: string;
  enabled: boolean;
  description: string;
  cron: string;
  preset: string | null;
  last_run_at: string | null;
  last_job_id: string | null;
  next_run_at: string | null;
  error: string | null;
}

export interface EvalPreset {
  id: string;
  display_name: string;
  description: string;
  request: Partial<EvalRunRequest>;
}

export interface TriggerView {
  id: string;
  display_name: string;
  enabled: boolean;
  description: string;
  preset: string | null;
  token_ref: string | null;
  token_state: "none" | "configured" | "missing";
}

/** §59：站内通知——终态 Job 的投影 + 已读标记。 */
export interface NotificationItem {
  job_id: string;
  status: JobStatus;
  run_id: string | null;
  benchmark: string;
  gate_verdict: string | null;
  requested_by: string;
  error: string | null;
  finished_at: string | null;
  read: boolean;
}

const BASE = "/api";

async function get<T>(path: string): Promise<T> {
  const response = await fetch(`${BASE}${path}`, { headers: { accept: "application/json" } });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = (await response.json()) as { error?: string; detail?: string };
      detail = body.error ?? body.detail ?? detail;
    } catch {
      /* 非 JSON 错误体：保留状态行 */
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

async function post<T>(path: string, body: unknown, idempotencyKey?: string): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      accept: "application/json",
      ...(idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {}),
    },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = (await response.json()) as { error?: string; detail?: string };
      detail = body.error ?? body.detail ?? detail;
    } catch {
      /* 非 JSON 错误体：保留状态行 */
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

export const evaluationApi = {
  jobs: () => get<EvalRunJob[]>("/eval-runs"),
  job: (jobId: string) => get<EvalRunJob>(`/eval-runs/${encodeURIComponent(jobId)}`),

  createEvalRun: (request: EvalRunRequest, idempotencyKey?: string) =>
    post<EvalRunJob>("/eval-runs", request, idempotencyKey),
  cancelEvalRun: (jobId: string) =>
    post<EvalRunJob>(`/eval-runs/${encodeURIComponent(jobId)}/cancel`, {}),

  agentConnections: () => get<AgentConnection[]>("/agent-connections"),
  agentHealth: (id: string) =>
    get<AgentHealthReport>(`/agent-connections/${encodeURIComponent(id)}/health`),

  schedules: () => get<ScheduleView[]>("/schedules"),
  presets: () => get<EvalPreset[]>("/eval-presets"),
  triggers: () => get<TriggerView[]>("/triggers"),

  notifications: () => get<NotificationItem[]>("/notifications"),
  markNotificationsRead: (jobIds: string[]) =>
    post<{ marked_read: number }>("/notifications/read", { job_ids: jobIds }),

  // §53 Production Trace（只读；摄取走 CI/采集器的 token 化 POST）。
  productionTraces: () => get<ProductionTraceMeta[]>("/production/traces"),
  productionTrace: (id: string) =>
    get<ProductionTraceMeta>(`/production/traces/${encodeURIComponent(id)}`),
  productionTraceView: (id: string, caseId?: string) =>
    get<ProductionTraceView>(
      `/production/traces/${encodeURIComponent(id)}/trace${caseId ? `?case_id=${encodeURIComponent(caseId)}` : ""}`,
    ),
  // §61 Online Eval
  evalPolicies: () => get<EvalPolicyRow[]>("/eval-policies"),
  evaluateProductionTrace: (id: string, policy: string) =>
    post<EvaluationResult>(
      `/production/traces/${encodeURIComponent(id)}/evaluate`,
      { policy },
    ),
  productionEvaluations: (id: string) =>
    get<EvaluationResult[]>(`/production/traces/${encodeURIComponent(id)}/evaluations`),

  // §63 Trace Replay：Job 路径（执行面） + 记录只读（§62 落账）。
  replayRuns: () => get<EvalRunJob[]>("/replay-runs"),
  replayRun: (jobId: string) => get<EvalRunJob>(`/replay-runs/${encodeURIComponent(jobId)}`),
  createReplayRun: (request: ReplayRunRequest, idempotencyKey?: string) =>
    post<EvalRunJob>("/replay-runs", request, idempotencyKey),
  cancelReplayRun: (jobId: string) =>
    post<EvalRunJob>(`/replay-runs/${encodeURIComponent(jobId)}/cancel`, {}),
  productionReplays: (traceId?: string) =>
    get<ReplayRecord[]>(
      `/production/replays${traceId ? `?trace_id=${encodeURIComponent(traceId)}` : ""}`,
    ),
  productionReplay: (replayId: string) =>
    get<ReplayRecord>(`/production/replays/${encodeURIComponent(replayId)}`),
};

export interface EvalPolicyRow {
  id: string;
  display_name: string;
  description: string;
  judge_model: string | null;
  timeout_seconds: number;
  metrics: { id: string; threshold: number | null }[];
}

export interface EvaluationRow {
  case_id: string;
  metric: string;
  threshold: number | null;
  score: number | null;
  verdict: "pass" | "fail" | "error" | "skipped";
  reason: string | null;
}

export interface EvaluationSummary {
  policy: string;
  cases_total: number;
  cases_evaluated: number;
  cases_unextractable: string[];
  metrics: Record<
    string,
    { pass: number; fail: number; error: number; mean_score: number | null }
  >;
}

export interface EvaluationResult {
  evaluation_id: string;
  trace_id?: string;
  summary: EvaluationSummary;
  rows: EvaluationRow[];
}

export interface ProductionTraceMeta {
  trace_id: string;
  source: string;
  ingested_at: string;
  cases: Record<string, number>;
  total_events: number;
  endpoint: string | null;
  model: string | null;
}

/** Span Tree 视图（与 Run Trace Viewer 同构；节点结构见 api-types SpanNode）。 */
export interface ProductionTraceView {
  trace_id: string;
  case_id: string | null;
  span_count: number;
  tokens: number;
  root: unknown;
  tool_sequence: string[];
}

/** §18：订阅 Job 的实时流——run-level 快照（job.*）+ 细粒度活动（job.activity）。
 *
 * 返回取消订阅函数。终态事件到达后由调用方决定何时退订（本函数不自动退订，
 * 让 Detail 页在收到终态后还能做一次收尾刷新）。
 */
export interface JobEventSubscription {
  jobId: string;
  /** §63：两条流共用帧契约，只有路径前缀不同（eval-runs / replay-runs）。 */
  kind?: "eval" | "replay";
  onEvent: (job: EvalRunJob, event: string) => void;
  onError?: () => void;
  onActivity?: (activity: JobActivityEvent) => void;
}

export function subscribeJobEvents(options: JobEventSubscription): () => void {
  const { jobId, kind = "eval", onEvent, onError, onActivity } = options;
  const collection = kind === "replay" ? "replay-runs" : "eval-runs";
  const source = new EventSource(`${BASE}/${collection}/${encodeURIComponent(jobId)}/events`);
  const handle = (event: MessageEvent) => {
    try {
      onEvent(JSON.parse(event.data) as EvalRunJob, event.type);
    } catch {
      /* 半截帧：等下一帧 */
    }
  };
  const activityHandler = (event: MessageEvent) => {
    if (!onActivity) return;
    try {
      onActivity(JSON.parse(event.data) as JobActivityEvent);
    } catch {
      /* 半截帧：等下一帧 */
    }
  };
  for (const name of ["job.updated", "job.completed", "job.failed", "job.cancelled"]) {
    source.addEventListener(name, handle as EventListener);
  }
  source.addEventListener("job.activity", activityHandler as EventListener);
  if (onError) source.onerror = onError;
  return () => source.close();
}

/** §18 细粒度活动事件（case/turn/tool 生命周期）。data 字段只含白名单标量。 */
export interface JobActivityEvent {
  type: string;
  case_id?: string;
  iteration?: number;
  turn?: number;
  run_id?: string | null;
  status?: string;
  name?: string;
  tool?: string;
  event_id?: string;
}

export const JOB_STATUS_LABEL: Record<JobStatus, string> = {
  queued: "排队中",
  running: "运行中",
  cancelling: "取消中",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

export const JOB_TERMINAL: ReadonlySet<JobStatus> = new Set(["succeeded", "failed", "cancelled"]);
