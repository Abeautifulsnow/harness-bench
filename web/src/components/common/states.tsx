/** 通用状态块：加载 / 错误 / 空 / 派生层未构建。
 *
 * 这四态在前端必须区分开，尤其是最后两种：
 *  - "空" = 查询成功但没有数据（例如这个 benchmark 还没跑过 run）
 *  - "投影缺失" = 派生层还没 rebuild（数据可能都在，只是还没物化）
 * 把它们都渲染成"暂无数据"会让用户误以为事实层是空的。
 */

import type * as React from "react";
import { AlertTriangle, Inbox, Loader2, RefreshCw } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { ApiError } from "@/lib/api";
import type { Projection } from "@/lib/api-types";
import { cn } from "@/lib/utils";

export function Loading({ label = "加载中" }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 py-16 text-sm text-muted-foreground">
      <Loader2 className="size-4 animate-spin" />
      {label}…
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const isApi = error instanceof ApiError;
  const title = isApi ? `${error.status}` : "请求失败";
  const message = error instanceof Error ? error.message : String(error);
  const hint =
    isApi && error.status === 409
      ? "这是一个可解释的状态（例如 Spec §4.3 的 NO_BASELINE 降级），不是接口故障。"
      : isApi && error.status === 404
        ? "对象不存在：检查 id 是否拼对，或该 run 是否已被清理。"
        : null;
  return (
    <Card className="border-destructive/40">
      <CardContent className="flex items-start gap-3 pt-4">
        <AlertTriangle className="mt-0.5 size-4 shrink-0 text-destructive" />
        <div className="space-y-2">
          <div className="text-sm font-medium text-destructive">{title}</div>
          <p className="text-sm text-foreground/90">{message}</p>
          {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
          {onRetry && (
            <Button size="sm" variant="outline" onClick={onRetry}>
              <RefreshCw /> 重试
            </Button>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

export function EmptyState({
  title = "暂无数据",
  emptyHint,
  className,
}: {
  title?: string;
  emptyHint?: string;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border py-14 text-center",
        className,
      )}
    >
      <Inbox className="size-5 text-muted-foreground" />
      <div className="text-sm text-foreground/90">{title}</div>
      {emptyHint && <p className="max-w-md text-xs text-muted-foreground">{emptyHint}</p>}
    </div>
  );
}

/** 派生层（DuckDB）未构建时的提示：必须给出可执行命令，而不是空白图表。 */
export function ProjectionNotice({
  projection,
  hint,
  className,
}: {
  projection: Projection;
  hint?: string | null;
  className?: string;
}) {
  if (projection === "ok") return null;
  return (
    <div
      className={cn(
        "flex items-start gap-2 rounded-lg border border-[color-mix(in_oklch,var(--warn)_40%,transparent)] bg-[color-mix(in_oklch,var(--warn)_10%,transparent)] px-3 py-2 text-xs",
        className,
      )}
    >
      <AlertTriangle className="mt-0.5 size-3.5 shrink-0 text-[var(--warn)]" />
      <div className="space-y-1">
        <div className="font-medium text-[var(--warn)]">派生层未构建</div>
        <p className="text-muted-foreground">
          {hint ?? "该视图依赖 DuckDB 投影，请先执行 `agent-eval storage rebuild`。"}
        </p>
      </div>
    </div>
  );
}

export function QueryState({
  isLoading,
  error,
  isEmpty,
  emptyTitle,
  emptyHint,
  onRetry,
  children,
}: {
  isLoading: boolean;
  error: unknown;
  isEmpty?: boolean;
  emptyTitle?: string;
  emptyHint?: string;
  onRetry?: () => void;
  children: React.ReactNode;
}) {
  if (isLoading) return <Loading />;
  if (error) return <ErrorState error={error} onRetry={onRetry} />;
  if (isEmpty) return <EmptyState title={emptyTitle} emptyHint={emptyHint} />;
  return <>{children}</>;
}
