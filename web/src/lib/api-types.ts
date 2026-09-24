/** REST 契约类型（PRD §81/§82/§84），与 `src/agent_eval/api/schemas.py` 一一对应。
 *
 * 手工维护而非代码生成：字段少、稳定性高，且生成器会把 OpenAPI 里的
 * 可选性差异抹平（平台里 null 表示"无定价/无基线"，0 表示真值，这个区别必须留住）。 */

export type Projection = "ok" | "missing";
export type Verdict = "pass" | "fail" | "infra_failure" | "undetermined";
export type Stability = "STABLE_PASS" | "STABLE_FAIL" | "FLAKY" | "UNKNOWN";
export type RegressionState =
  | "PASSED"
  | "FAILED"
  | "REGRESSION"
  | "IMPROVED"
  | "UNCHANGED"
  | "FLAKY"
  | "UNDETERMINED"
  | "INVALID";
export type BaselineMode = "explicit" | "release" | "main-latest" | "NO_BASELINE";
export type RunStatus = "queued" | "running" | "completed" | "partial";

export interface Health {
  status: "ok";
  version: string;
  evals_root: string;
  data_root: string;
  projection: Projection;
  runs: number;
}

export interface DashboardCard {
  label: string;
  value: number | string | null;
  unit: string | null;
  delta: number | null;
  detail: string | null;
}

export interface RunMetadata {
  run_id: string;
  benchmark_id: string;
  dataset_id: string;
  dataset_version: string;
  dataset_hash: string;
  profile: string;
  suite: string | null;
  tag_filter: string[];
  experiment_id: string | null;
  variant_id: string | null;
  variant: string | null;
  agent_version: string | null;
  agent_endpoint: string;
  git_commit: string | null;
  git_branch: string | null;
  git_dirty: boolean | null;
  agent_model: string | null;
  judge_model: string | null;
  deepeval_version: string | null;
  eval_platform_version: string;
  started_at: string;
  finished_at: string | null;
  environment: string;
  status: RunStatus;
  baseline_policy: string | null;
  baseline_run_id: string | null;
  baseline_mode: string;
  baseline_reason: string | null;
  metric_capability_snapshot: Record<string, boolean>;
  metric_degradations: Record<string, string>;
  no_judge: boolean;
}

export interface Dashboard {
  current_release: string | null;
  release_run_id: string | null;
  cards: DashboardCard[];
  recent_runs: RunMetadata[];
  projection: Projection;
  hint: string | null;
}

export interface BenchmarkRow {
  name: string;
  version: string | null;
  description: string;
  owner: string | null;
  dataset: string | null;
  suites: string[];
  cases: number;
  last_run_id: string | null;
  last_run_at: string | null;
  pass_rate: number | null;
  verdict: string | null;
  regressions: number | null;
  baseline_mode: string | null;
}

export interface DatasetInfo {
  id: string;
  version: string;
  description: string;
  hash: string;
}

export interface CaseRow {
  id: string;
  name: string | null;
  version: number;
  dataset_id: string;
  tags: string[];
  difficulty: string | null;
  turns: number;
  mount_points: string[];
  description: string;
}

export interface SuiteRow {
  name: string;
  tags: string[];
  case_ids: string[];
  cases: number;
  kind: "suite" | "security" | "red-team";
  description: string;
}

export interface RunOverview {
  run: RunMetadata;
  counts: Record<string, number>;
  metrics: Record<string, number>;
  verdict: string;
  warnings: string[];
  cost: CostReport;
  baseline_mode: string;
}

export interface CaseResultRow {
  case_id: string;
  case_run_ids: string[];
  iterations: number;
  pass_rate: number;
  stability: Stability;
  regression_state: RegressionState;
  valid_iterations: number;
  infra_error_count: number;
  score_mean: number | null;
  score_stddev: number | null;
  tool_calls_mean: number;
  tokens_mean: number;
  latency_mean: number;
  cost_mean: number;
  pass_at_k: Record<string, number>;
  blocking_failures: BlockingFailure[];
  error_semantics: string[];
  failure_category: string | null;
  tags: string[];
}

export interface BlockingFailure {
  metric?: string;
  verdict?: string;
  reason?: string | null;
  mount?: string | null;
  turn?: number | null;
  [key: string]: unknown;
}

export interface MetricRow {
  case_run_id: string;
  case_id: string;
  iteration: number;
  metric: string;
  evaluator: string;
  score: number | null;
  threshold: number | null;
  verdict: string;
  blocking: boolean;
  mount: string | null;
  turn: number | null;
  reason: string | null;
  hard_gate: boolean;
}

export interface CaseRunRow {
  case_run_id: string;
  iteration: number;
  status: string;
  failure_semantics: string | null;
  failure_category: string | null;
  latency_ms: number;
  token_count: number;
  input_tokens: number;
  output_tokens: number;
  cache_tokens: number;
  cost: number | null;
  cost_known: boolean;
  tool_calls: string[];
  final_output: string | null;
  error: string | null;
  trace_available: boolean;
  started_at: string | null;
  finished_at: string | null;
}

export interface ArtifactRow {
  name: string;
  path: string;
  bytes: number;
  content_type: string;
  url: string;
}

export interface ArtifactContent {
  name: string;
  content_type: string;
  text: string;
}

export interface SpanNode {
  id: string;
  parent_span_id: string | null;
  type: string;
  name: string;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  status: string;
  error: string | null;
  tokens: number | null;
  input: unknown;
  output: unknown;
  metric_results: MetricRow[];
  attributes: Record<string, unknown>;
  children: SpanNode[];
}

export interface TraceView {
  run_id: string;
  case_id: string;
  iteration: number;
  trace_id: string | null;
  span_count: number;
  tokens: number;
  root: SpanNode | null;
  tool_sequence: string[];
}

export interface TraceIndexRow {
  run_id: string;
  benchmark_id: string;
  case_id: string;
  iteration: number;
  bytes: number;
  url: string;
}

export interface TraceEventRow {
  event_id: string;
  type: string;
  timestamp: string | null;
  parent_span_id: string | null;
  data: Record<string, unknown>;
}

export interface TraceEvents {
  projection: Projection;
  hint: string | null;
  run_id: string;
  case_id: string;
  iteration: number;
  trace_id: string | null;
  count: number;
  events: TraceEventRow[];
}

export interface MetricDiff {
  metric: string;
  baseline: number | null;
  candidate: number | null;
  delta: number | null;
  delta_percent: number | null;
  verdict: "improved" | "regressed" | "unchanged" | "undetermined";
}

export interface PerformanceDiff {
  case_id?: string;
  metric: string;
  baseline: number | null;
  candidate: number | null;
  delta_percent: number | null;
  regressed: boolean;
  threshold_percent: number | null;
}

export interface CaseDiff {
  case_id: string;
  case_version: number;
  baseline_stability: Stability;
  candidate_stability: Stability;
  state: RegressionState;
  invalid_reason: string | null;
  performance: PerformanceDiff[];
  metric_diffs: MetricDiff[];
  baseline_iterations: number;
  candidate_iterations: number;
}

export interface TraceDiffOp {
  kind: "equal" | "added" | "removed";
  value: string;
  side: "baseline" | "candidate";
}

export interface ArgumentDiff {
  tool: string;
  path: string;
  baseline: unknown;
  candidate: unknown;
  change: "added" | "removed" | "changed";
}

export interface TraceDiff {
  case_id: string;
  baseline_iteration: number;
  candidate_iteration: number;
  tool_sequence: TraceDiffOp[];
  added_tools: string[];
  removed_tools: string[];
  argument_diffs: ArgumentDiff[];
  model_calls: [number, number];
  subagent_calls: [number, number];
  errors: [number, number];
  retries: [number, number];
  tokens: [number, number];
  latency_ms: [number, number];
  final_answer_changed: boolean;
  changed: boolean;
}

export interface FailureCategoryDiff {
  category: string;
  baseline: number;
  candidate: number;
  delta: number;
}

export interface RegressionAnalysis {
  run_id: string;
  candidate_run_id: string;
  baseline_run_id: string | null;
  baseline_mode: string;
  valid: boolean;
  invalid_reason: string | null;
  counts: Record<string, number>;
  metric_diffs: MetricDiff[];
  performance: PerformanceDiff[];
  flaky_cases: string[];
  failure_categories: FailureCategoryDiff[];
  cases: CaseDiff[];
  trace_diffs: TraceDiff[];
  baseline_totals: Record<string, number>;
  candidate_totals: Record<string, number>;
}

export interface FailureRow {
  failure_id: string;
  case_run_id: string;
  case_id: string;
  iteration: number;
  category: string;
  parent: string;
  metric: string | null;
  reason: string | null;
  evidence: string | null;
  source: string;
  tags: string[];
}

export interface FailureList {
  projection: Projection;
  hint: string | null;
  run_id: string;
  total: number;
  by_category: Record<string, number>;
  by_parent: Record<string, number>;
  by_tool: Record<string, number>;
  by_model: Record<string, number>;
  by_version: Record<string, number>;
  by_benchmark: Record<string, number>;
  failures: FailureRow[];
}

export interface ClusterRow {
  cluster_id: string;
  label: string;
  category: string;
  parent: string;
  size: number;
  case_ids: string[];
  representative_case_id: string;
  representative_case_run_id: string | null;
  common_tool_sequence: string;
  common_error: string | null;
  first_seen: string | null;
  latest_seen: string | null;
  affected_versions: string[];
}

export interface ClusterList {
  projection: Projection;
  hint: string | null;
  run_id: string;
  total_clusters: number;
  total_failures: number;
  clusters: ClusterRow[];
}

export interface TaxonomyRow {
  category: string;
  parent: string;
  description: string;
}

export interface GateRuleRow {
  rule: string;
  observed: number | null;
  threshold: number | null;
  verdict: "pass" | "fail" | "undetermined";
  blocking: boolean;
  affected_case_runs: string[];
  detail: string;
}

export interface GateView {
  run_id: string;
  gate: string;
  verdict: string;
  baseline_mode: string;
  baseline_run_id: string | null;
  rules: GateRuleRow[];
  aggregate: Record<string, number>;
  notes: string[];
  exit_code: number;
  source: "stored" | "replayed";
}

export interface GateRulesetRow {
  gate: string;
  strict: boolean;
  suites: string[];
  thresholds: Record<string, Record<string, number | null>>;
  hard_failure_categories: string[];
}

export interface ReviewRow {
  id: string;
  run_id: string;
  case_id: string;
  reviewer: string;
  verdict: string;
  note: string;
  queue_reason: string | null;
  created_at: string | null;
  case_run_id: string | null;
  machine_verdict: string | null;
  status: "pending" | "reviewed";
}

export interface ReviewList {
  projection: Projection;
  run_id: string | null;
  status: string | null;
  queue_reasons: string[];
  verdicts: string[];
  total: number;
  reviews: ReviewRow[];
}

export interface ReviewQueueRow {
  case_id: string;
  queue_reason: string;
  machine_verdict: string | null;
  iterations: number;
  pass_rate: number;
  stability: Stability;
  detail: string | null;
}

export interface ReviewQueue {
  projection: Projection;
  hint: string | null;
  run_id: string;
  total: number;
  candidates: ReviewQueueRow[];
}

export interface ExperimentRow {
  id: string;
  name: string;
  benchmark_id: string;
  status: string;
  variants: number;
  created_at: string | null;
  dataset_version: string | null;
  profile: string | null;
  repeat: number | null;
  gate: string;
  note: string;
}

export interface VariantRow {
  variant_id: string;
  name: string;
  dimensions: Record<string, unknown>;
  status: string;
  run_id: string | null;
  verdict: string | null;
  task_success: number | null;
  task_completion: number | null;
  step_ratio: number | null;
  argument_checks: number | null;
  tool_calls: number | null;
  tokens: number | null;
  latency_ms: number | null;
  cost: number | null;
  stability: string | null;
  flaky_cases: number | null;
}

export interface ExperimentDetail {
  experiment: ExperimentRow & { variants: unknown[]; created_at: string };
  variants: VariantRow[];
  dimensions: string[];
  groups: Record<string, string[]>;
}

export interface ComparisonRow {
  value: string;
  variants: string[];
  runs: (string | null)[];
  task_success: number | null;
  cost: number | null;
  latency_ms: number | null;
  tokens: number | null;
  tool_calls: number | null;
  step_ratio: number | null;
  argument_checks: number | null;
}

export interface CostReport {
  total_cost: number | null;
  agent_cost: number | null;
  judge_cost: number | null;
  tool_cost: number | null;
  input_tokens: number;
  output_tokens: number;
  cache_tokens: number;
  avg_cost_per_case: number | null;
  cost_per_success: number | null;
  judge_cost_ratio: number | null;
  priced_cases: number;
  unpriced_cases: number;
}

export interface CostBreakdown extends CostReport {
  run_id: string;
  price_configured: boolean;
  by_model: Record<string, number>;
  note: string | null;
}

export interface TrendPoint {
  run_id: string;
  benchmark_id: string | null;
  agent_model: string | null;
  git_commit: string | null;
  dataset_version: string | null;
  started_at: string | null;
  verdict: string | null;
  task_success: number | null;
  total_cases: number | null;
  passed_cases: number | null;
  failed_cases: number | null;
  total_tokens: number | null;
  total_cost: number | null;
}

export interface TrendSeries {
  projection: Projection;
  hint: string | null;
  benchmark: string | null;
  dataset_version: string | null;
  metric: string | null;
  points: TrendPoint[];
  metric_points: {
    run_id: string;
    benchmark_id?: string | null;
    started_at: string | null;
    agent_model?: string | null;
    score: number;
    samples: number;
  }[];
}

export interface FlakyRow {
  run_id: string;
  case_id: string;
  iterations: number;
  passes: number;
  fails: number;
  errors: number;
}

export interface FlakyList {
  projection: Projection;
  hint: string | null;
  total: number;
  cases: FlakyRow[];
}

export interface SecuritySuiteRow {
  name: string;
  tag: string;
  cases: number;
  description: string;
}

export interface RedTeamCoverageRow {
  category: string;
  cases: number;
  case_ids: string[];
  covered: boolean;
}

export interface SecurityPosture {
  projection: Projection;
  hint: string | null;
  run_id: string | null;
  suites: SecuritySuiteRow[];
  coverage: RedTeamCoverageRow[];
  red_team_categories: string[];
  findings: {
    case_id: string;
    case_run_id: string;
    iteration: number;
    metric: string;
    verdict: string;
    blocking: boolean;
    mount: string | null;
    reason: string | null;
  }[];
  failed_findings: number;
}

export interface BaselineRow {
  id: string;
  benchmark_id: string;
  dataset_version: string;
  mode: BaselineMode;
  pinned_run_id: string | null;
  pinned_by: string | null;
  pinned_at: string | null;
  gate_evidence_run_id: string | null;
  note: string;
}

export interface DraftRow {
  id: string;
  case_id: string;
  case_run_id: string;
  suite: string;
  draft_status: string;
  failure_category: string | null;
  source_type: string | null;
  source_ref: string | null;
  created_at: string | null;
}

export interface StorageStatus {
  projection: Projection;
  path: string;
  hint?: string;
  counts: Record<string, number>;
}
