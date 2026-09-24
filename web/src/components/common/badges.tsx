import { Badge } from "@/components/ui/badge";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { fmtNumber, fmtRatio } from "@/lib/format";
import {
  baselineLabel,
  exitCodeHint,
  metricDiffLabel,
  regressionLabel,
  runStatusLabel,
  stabilityLabel,
  verdictVariant,
} from "@/lib/verdicts";

export function VerdictBadge({ verdict, className }: { verdict: string | null; className?: string }) {
  return (
    <Badge variant={verdictVariant(verdict)} className={className}>
      {verdict ?? "—"}
    </Badge>
  );
}

export function StabilityBadge({ stability }: { stability: string }) {
  return <Badge variant={verdictVariant(stability)}>{stabilityLabel(stability)}</Badge>;
}

export function RegressionBadge({ state }: { state: string }) {
  return <Badge variant={verdictVariant(state)}>{regressionLabel(state)}</Badge>;
}

export function RunStatusBadge({ status }: { status: string }) {
  return <Badge variant={verdictVariant(status)}>{runStatusLabel(status)}</Badge>;
}

export function MetricDiffBadge({ verdict }: { verdict: string }) {
  const variant =
    verdict === "improved"
      ? "pass"
      : verdict === "regressed"
        ? "fail"
        : verdict === "unchanged"
          ? "neutral"
          : "warn";
  return <Badge variant={variant}>{metricDiffLabel(verdict)}</Badge>;
}

/** baseline 模式：NO_BASELINE 是**降级状态**（Spec §4.3），必须与真实模式视觉区分。 */
export function BaselineBadge({ mode, reason }: { mode: string; reason?: string | null }) {
  const content = (
    <Badge variant={mode === "NO_BASELINE" ? "warn" : "info"}>{baselineLabel(mode)}</Badge>
  );
  if (!reason) return content;
  return (
    <Tooltip>
      <TooltipTrigger asChild>{content}</TooltipTrigger>
      <TooltipContent>{reason}</TooltipContent>
    </Tooltip>
  );
}

export function ExitCodeBadge({ code }: { code: number }) {
  const variant = code === 0 ? "pass" : code === 1 ? "fail" : "warn";
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Badge variant={variant}>exit {code}</Badge>
      </TooltipTrigger>
      <TooltipContent>{exitCodeHint(code)}</TooltipContent>
    </Tooltip>
  );
}

export function PassRate({ value }: { value: number | null }) {
  if (value === null) return <span className="text-muted-foreground">—</span>;
  const tone = value >= 1 ? "text-[var(--pass)]" : value === 0 ? "text-[var(--fail)]" : "text-foreground";
  return <span className={`tabular ${tone}`}>{fmtRatio(value)}</span>;
}

export function MetricValue({
  value,
  digits = 2,
  unit,
}: {
  value: number | null | undefined;
  digits?: number;
  unit?: string;
}) {
  return (
    <span className="tabular">
      {fmtNumber(value, digits)}
      {unit && value !== null && value !== undefined ? (
        <span className="ml-0.5 text-xs text-muted-foreground">{unit}</span>
      ) : null}
    </span>
  );
}
