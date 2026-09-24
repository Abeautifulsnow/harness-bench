import * as React from "react";
import {
  AlertOctagon,
  Bot,
  Brain,
  ChevronDown,
  ChevronRight,
  Database,
  FileTerminal,
  GitBranch,
  MemoryStick,
  Plug,
  Sparkles,
  Wrench,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { VerdictBadge } from "@/components/common/badges";
import { fmtMs, fmtTokens } from "@/lib/format";
import type { MetricRow, SpanNode } from "@/lib/api-types";
import { cn } from "@/lib/utils";

/** PRD §76 节点类型 → 图标。类型是平台契约的一部分（PRD §10），不做自由命名。 */
const SPAN_ICON: Record<string, React.ElementType> = {
  agent: Bot,
  llm: Brain,
  tool: Wrench,
  mcp: Plug,
  retriever: Database,
  skill: Sparkles,
  subagent: GitBranch,
  memory: MemoryStick,
  command: FileTerminal,
  workflow: ChevronRight,
};

function payloadPreview(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function SpanRow({ span, depth }: { span: SpanNode; depth: number }) {
  const [open, setOpen] = React.useState(depth < 2);
  const Icon = SPAN_ICON[span.type] ?? ChevronRight;
  const hasChildren = span.children.length > 0;
  const input = payloadPreview(span.input);
  const output = payloadPreview(span.output);
  const expanded = open;

  return (
    <div className="select-none">
      <div
        className={cn(
          "group flex items-start gap-2 rounded-md px-2 py-1.5 hover:bg-muted/50",
          span.status === "error" && "bg-destructive/5",
        )}
        style={{ paddingLeft: `${depth * 16 + 8}px` }}
      >
        <button
          type="button"
          className="mt-0.5 grid size-4 shrink-0 place-items-center text-muted-foreground"
          onClick={() => setOpen((value) => !value)}
          aria-label={expanded ? "折叠" : "展开"}
        >
          {hasChildren ? (
            expanded ? (
              <ChevronDown className="size-3.5" />
            ) : (
              <ChevronRight className="size-3.5" />
            )
          ) : (
            <span className="size-1 rounded-full bg-border" />
          )}
        </button>

        <Icon className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />

        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-xs">{span.name}</span>
            <Badge variant="outline">{span.type}</Badge>
            {span.duration_ms !== null && (
              <span className="tabular text-[11px] text-muted-foreground">
                {fmtMs(span.duration_ms)}
              </span>
            )}
            {span.tokens ? (
              <span className="tabular text-[11px] text-muted-foreground">
                {fmtTokens(span.tokens)} tok
              </span>
            ) : null}
            {span.status === "error" && (
              <span className="flex items-center gap-1 text-[11px] text-[var(--fail)]">
                <AlertOctagon className="size-3" />
                error
              </span>
            )}
          </div>
          {span.error && (
            <div className="mt-1 rounded border border-destructive/30 bg-destructive/5 px-2 py-1 text-[11px] text-[var(--fail)]">
              {span.error}
            </div>
          )}
          {expanded && (input || output) && (
            <div className="mt-1.5 space-y-1">
              {input && <Payload label="input" text={input} />}
              {output && <Payload label="output" text={output} />}
            </div>
          )}
          {expanded && span.metric_results.length > 0 && (
            <div className="mt-1.5 space-y-1">
              {span.metric_results.map((metric) => (
                <MetricLine key={`${metric.metric}-${metric.mount ?? ""}`} metric={metric} />
              ))}
            </div>
          )}
        </div>
      </div>

      {expanded &&
        span.children.map((child) => <SpanRow key={child.id} span={child} depth={depth + 1} />)}
    </div>
  );
}

function Payload({ label, text }: { label: string; text: string }) {
  const [open, setOpen] = React.useState(false);
  const long = text.length > 240 || text.includes("\n");
  const shown = !long || open ? text : `${text.slice(0, 240)}…`;
  return (
    <div className="rounded border border-border bg-muted/30">
      <div className="flex items-center justify-between border-b border-border/70 px-2 py-0.5">
        <span className="text-[10px] tracking-wide text-muted-foreground uppercase">{label}</span>
        {long && (
          <button
            type="button"
            className="text-[10px] text-primary hover:underline"
            onClick={() => setOpen((value) => !value)}
          >
            {open ? "收起" : "展开"}
          </button>
        )}
      </div>
      <pre className="max-h-72 overflow-auto px-2 py-1 font-mono text-[11px] whitespace-pre-wrap">
        {shown}
      </pre>
    </div>
  );
}

function MetricLine({ metric }: { metric: MetricRow }) {
  return (
    <div className="flex flex-wrap items-center gap-2 text-[11px]">
      <VerdictBadge verdict={metric.verdict} />
      <span className="font-mono">{metric.metric}</span>
      {metric.mount && <Badge variant="neutral">{metric.mount}</Badge>}
      {metric.blocking && <Badge variant="fail">blocking</Badge>}
      {metric.hard_gate && <Badge variant="fail">hard gate</Badge>}
      {metric.reason && <span className="text-muted-foreground">{metric.reason}</span>}
    </div>
  );
}

export function TraceTree({ root, empty }: { root: SpanNode | null; empty?: React.ReactNode }) {
  if (!root) {
    return <div className="py-10 text-center text-sm text-muted-foreground">{empty ?? "没有 span"}</div>;
  }
  return (
    <div className="overflow-hidden rounded-lg border border-border">
      <SpanRow span={root} depth={0} />
    </div>
  );
}
