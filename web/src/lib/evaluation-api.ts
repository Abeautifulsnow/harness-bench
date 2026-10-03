/** 执行面 API 客户端（docs/web-evaluation-control-plane-design.md §16–§18）。
 *
 * 与只读 `api.ts` 分开：整个执行面只有"发起评测 / 取消评测"两个写动作，
 * 对应后端唯一允许 POST 的两个端点（§42：Definition mutation 仍然禁止）。
 * 列表/详情/健康探测仍是 GET。定义树数据（benchmark / suite / profile）
 * 继续从只读 `api.ts` 获取。
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
};

/** §18：订阅 Job 的 run-level progress SSE。
 *
 * 返回取消订阅函数。终态事件到达后由调用方决定何时退订（本函数不自动退订，
 * 让 Detail 页在收到终态后还能做一次收尾刷新）。
 */
export function subscribeJobEvents(
  jobId: string,
  onEvent: (job: EvalRunJob, event: string) => void,
  onError?: () => void,
): () => void {
  const source = new EventSource(`${BASE}/eval-runs/${encodeURIComponent(jobId)}/events`);
  const handle = (event: MessageEvent) => {
    try {
      onEvent(JSON.parse(event.data) as EvalRunJob, event.type);
    } catch {
      /* 半截帧：等下一帧 */
    }
  };
  for (const name of ["job.updated", "job.completed", "job.failed", "job.cancelled"]) {
    source.addEventListener(name, handle as EventListener);
  }
  if (onError) source.onerror = onError;
  return () => source.close();
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
