import type { VariantProps } from "class-variance-authority";

import type { badgeVariants } from "@/components/ui/badge";
import type { RegressionState, Stability } from "@/lib/api-types";

type BadgeVariant = NonNullable<VariantProps<typeof badgeVariants>["variant"]>;

/** 判定 → 配色。**只用于判定列**：数字本身不着色，避免"颜色代替读数"。 */
const VERDICTS: Record<string, BadgeVariant> = {
  pass: "pass",
  PASS: "pass",
  success: "pass",
  completed: "pass",
  STABLE_PASS: "pass",
  IMPROVED: "pass",
  OK: "pass",
  fail: "fail",
  FAIL: "fail",
  failed: "fail",
  STABLE_FAIL: "fail",
  REGRESSION: "fail",
  error: "fail",
  ERROR: "fail",
  infra_failure: "fail",
  undetermined: "warn",
  UNDETERMINED: "warn",
  UNKNOWN: "warn",
  FLAKY: "warn",
  partial: "warn",
  running: "info",
  UNCHANGED: "neutral",
  INVALID: "neutral",
  created: "neutral",
  queued: "neutral",
  queued_unknown: "neutral",
};

export function verdictVariant(verdict: string | null | undefined): BadgeVariant {
  if (!verdict) return "neutral";
  return VERDICTS[verdict] ?? "neutral";
}

export function stabilityLabel(stability: Stability | string): string {
  switch (stability) {
    case "STABLE_PASS":
      return "稳定通过";
    case "STABLE_FAIL":
      return "稳定失败";
    case "FLAKY":
      return "波动";
    default:
      return "样本不足";
  }
}

/** PRD §54 判定 → 中文标签；UNDETERMINED 必须显式显示，不得省略成空白。 */
export function regressionLabel(state: RegressionState | string): string {
  const labels: Record<string, string> = {
    PASSED: "通过",
    FAILED: "失败",
    REGRESSION: "退步",
    IMPROVED: "进步",
    UNCHANGED: "持平",
    FLAKY: "波动",
    UNDETERMINED: "无法判定",
    INVALID: "比较无效",
  };
  return labels[state] ?? String(state);
}

export function metricDiffLabel(verdict: string): string {
  const labels: Record<string, string> = {
    improved: "改善",
    regressed: "退步",
    unchanged: "持平",
    undetermined: "无法判定",
  };
  return labels[verdict] ?? verdict;
}

export function baselineLabel(mode: string): string {
  const labels: Record<string, string> = {
    explicit: "显式指定",
    release: "发布基线",
    "main-latest": "主干最新",
    NO_BASELINE: "无基线（降级）",
  };
  return labels[mode] ?? mode;
}

export function runStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    queued: "排队",
    running: "运行中",
    completed: "已完成",
    partial: "部分完成",
  };
  return labels[status] ?? status;
}

export function exitCodeHint(code: number): string {
  switch (code) {
    case 0:
      return "Gate 通过";
    case 1:
      return "Gate 失败";
    case 2:
      return "无法可靠评估（基础设施/评测失败）";
    case 3:
      return "无效调用";
    default:
      return `exit ${code}`;
  }
}
