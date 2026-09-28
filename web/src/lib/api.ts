/** 只读 API 客户端。
 *
 * 两条硬约定：
 *  1. 只发 GET。写入操作留在 CLI，前端不存在"绕过 Gate / Review 流程改数据"的路径。
 *  2. 后端返回的 409/404 语义要原样带到 UI：例如"没有 baseline"不是错误页，
 *     而是 Spec §4.3 的 NO_BASELINE 降级状态，必须显示成可解释的提示。
 */

import type * as T from "./api-types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

const BASE = "/api";

function query(params?: Record<string, unknown>): string {
  if (!params) return "";
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    if (Array.isArray(value)) {
      for (const item of value) if (item !== undefined && item !== null) search.append(key, String(item));
    } else {
      search.set(key, String(value));
    }
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

async function get<T>(path: string, params?: Record<string, unknown>): Promise<T> {
  const response = await fetch(`${BASE}${path}${query(params)}`, {
    headers: { accept: "application/json" },
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

export const api = {
  health: () => get<T.Health>("/health"),
  dashboard: (benchmark?: string) => get<T.Dashboard>("/dashboard", { benchmark }),

  benchmarks: () => get<T.BenchmarkRow[]>("/benchmarks"),
  benchmarkCases: (name: string) => get<T.CaseRow[]>(`/benchmarks/${encodeURIComponent(name)}/cases`),

  datasets: () => get<T.DatasetInfo[]>("/datasets"),
  cases: (params?: { dataset?: string; tag?: string[]; q?: string; limit?: number }) =>
    get<T.CaseRow[]>("/cases", params),
  case: (id: string, dataset?: string) =>
    get<Record<string, unknown>>(`/cases/${encodeURIComponent(id)}`, { dataset }),
  suites: () => get<T.SuiteRow[]>("/suites"),

  runs: (params?: {
    benchmark?: string;
    dataset_version?: string;
    experiment_id?: string;
    status?: string;
    limit?: number;
  }) => get<T.RunMetadata[]>("/runs", params),
  run: (id: string) => get<T.RunOverview>(`/runs/${id}`),
  runCases: (id: string) => get<T.CaseResultRow[]>(`/runs/${id}/cases`),
  caseMetrics: (runId: string, caseId: string) =>
    get<T.MetricRow[]>(`/runs/${runId}/cases/${encodeURIComponent(caseId)}/metrics`),
  caseRuns: (runId: string, caseId: string) =>
    get<T.CaseRunRow[]>(`/runs/${runId}/cases/${encodeURIComponent(caseId)}/runs`),
  runArtifacts: (id: string) => get<T.ArtifactRow[]>(`/runs/${id}/artifacts`),
  artifact: (id: string, name: string) =>
    get<T.ArtifactContent>(`/runs/${id}/artifacts/${name}`),
  /** PRD §90 case 级产物：索引 + 采集记账 + 能力表（三者含义不同，别合并展示）。 */
  caseArtifacts: (runId: string, caseId: string) =>
    get<T.CaseArtifacts>(`/runs/${runId}/cases/${encodeURIComponent(caseId)}/artifacts`),
  caseArtifact: (runId: string, caseId: string, name: string, iteration: number) =>
    get<T.CaseArtifactContent>(
      `/runs/${runId}/cases/${encodeURIComponent(caseId)}/artifacts/${name}`,
      { iteration },
    ),
  runStatus: (id: string) =>
    get<{ run_id: string; status: string; verdict: string | null; finished_at: string | null }>(
      `/runs/${id}/status`,
    ),

  traces: (params?: { run_id?: string; limit?: number }) =>
    get<T.TraceIndexRow[]>("/traces", params),
  trace: (runId: string, caseId: string, iteration: number) =>
    get<T.TraceView>(`/runs/${runId}/traces/${encodeURIComponent(caseId)}`, { iteration }),
  traceEvents: (runId: string, caseId: string, iteration: number) =>
    get<T.TraceEvents>(`/runs/${runId}/traces/${encodeURIComponent(caseId)}/events`, {
      iteration,
    }),

  regressions: (limit?: number) => get<Record<string, unknown>[]>("/regressions", { limit }),
  regressionOfRun: (runId: string, traceDiff = true) =>
    get<T.RegressionAnalysis>(`/regressions/${runId}`, { trace_diff: traceDiff }),
  regressionBetween: (baseline: string, candidate: string, traceDiff = true) =>
    get<T.RegressionAnalysis>(`/regressions/${baseline}/${candidate}`, { trace_diff: traceDiff }),

  experiments: (status?: string) => get<T.ExperimentRow[]>("/experiments", { status }),
  experiment: (id: string, dimension?: string) =>
    get<T.ExperimentDetail>(`/experiments/${id}`, { dimension }),
  experimentComparison: (id: string, dimension: string) =>
    get<T.ComparisonRow[]>(`/experiments/${id}/comparison`, { dimension }),
  experimentRuns: (id: string) => get<Record<string, unknown>[]>(`/experiments/${id}/runs`),

  failureTaxonomy: () => get<T.TaxonomyRow[]>("/failures/taxonomy"),
  failuresOfRun: (runId: string, params?: { category?: string; parent?: string }) =>
    get<T.FailureList>(`/failures/${runId}`, params),
  failureRuns: (params?: { benchmark?: string; category?: string; limit?: number }) =>
    get<T.FailureList[]>("/failures", params),
  clusters: (runId: string) => get<T.ClusterList>(`/failures/${runId}/clusters`),
  drafts: (params?: { suite?: string; status?: string }) => get<T.DraftRow[]>("/drafts", params),

  gateRules: () => get<T.GateRulesetRow[]>("/gates/rules"),
  gateHistory: (params?: { benchmark?: string; limit?: number }) => get<T.GateView[]>("/gates", params),
  gate: (runId: string, gate?: string) => get<T.GateView>(`/gates/${runId}`, { gate }),

  reviewOptions: () =>
    get<{ verdicts: string[]; queue_reasons: string[] }>("/reviews/options"),
  reviews: (params?: { run_id?: string; status?: string }) => get<T.ReviewList>("/reviews", params),
  reviewQueue: (runId: string) => get<T.ReviewQueue>(`/reviews/queue/${runId}`),

  security: (runId?: string) => get<T.SecurityPosture>("/security", { run_id: runId }),

  baselines: (benchmark?: string) => get<T.BaselineRow[]>("/baselines", { benchmark }),
  resolveBaseline: (benchmark: string, mode: string, dataset_version?: string) =>
    get<T.BaselineRow | null>("/baselines/resolve", { benchmark, mode, dataset_version }),

  trends: (params?: {
    benchmark?: string;
    dataset_version?: string;
    metric?: string;
    limit?: number;
  }) => get<T.TrendSeries>("/trends", params),
  cost: (params?: { benchmark?: string; limit?: number }) => get<T.CostBreakdown[]>("/cost", params),
  flaky: (limit?: number) => get<T.FlakyList>("/flaky", { limit }),
  storageStatus: () => get<T.StorageStatus>("/storage/status"),
};
