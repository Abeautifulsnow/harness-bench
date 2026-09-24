import { useQuery } from "@tanstack/react-query";
import { Database, Server } from "lucide-react";

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

/** 侧栏底部状态：API 是否活着 + 派生层是否已构建。
 *  "投影缺失"会直接影响趋势/聚类等页面，所以在全局导航里就要可见。 */
export function HealthPill() {
  const health = useQuery({ queryKey: ["health"], queryFn: api.health, refetchInterval: 30_000 });

  if (health.isLoading) {
    return <div className="h-6 animate-pulse rounded-md bg-muted" />;
  }
  if (health.isError) {
    return (
      <div className="flex items-center gap-2 rounded-md border border-destructive/40 px-2 py-1 text-[11px] text-destructive">
        <Server className="size-3" />
        API 不可达
      </div>
    );
  }
  const data = health.data!;
  const projectionOk = data.projection === "ok";
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <div className="space-y-1 rounded-md border border-border px-2 py-1.5 text-[11px]">
          <div className="flex items-center justify-between gap-2">
            <span className="flex items-center gap-1 text-muted-foreground">
              <Server className="size-3" />
              API
            </span>
            <span className="tabular text-[var(--pass)]">v{data.version}</span>
          </div>
          <div className="flex items-center justify-between gap-2">
            <span className="flex items-center gap-1 text-muted-foreground">
              <Database className="size-3" />
              投影
            </span>
            <span className={cn("tabular", projectionOk ? "text-[var(--pass)]" : "text-[var(--warn)]")}>
              {projectionOk ? "已构建" : "未构建"}
            </span>
          </div>
          <div className="flex items-center justify-between gap-2">
            <span className="text-muted-foreground">Runs</span>
            <span className="tabular">{data.runs}</span>
          </div>
        </div>
      </TooltipTrigger>
      <TooltipContent side="right" className="space-y-1">
        <div>evals: {data.evals_root}</div>
        <div>data: {data.data_root}</div>
        {!projectionOk && <div>趋势/聚类需先执行 storage rebuild</div>}
      </TooltipContent>
    </Tooltip>
  );
}
