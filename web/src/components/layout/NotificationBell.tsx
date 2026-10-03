import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";
import { Bell, CheckCheck } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { fmtRelative } from "@/lib/format";
import {
  JOB_STATUS_LABEL,
  evaluationApi,
  type NotificationItem,
} from "@/lib/evaluation-api";

/** §59 站内通知铃铛：终态 Job 投影 + 已读标记。15s 轮询足够（非实时承诺）。 */
export function NotificationBell() {
  const [open, setOpen] = React.useState(false);
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ["notifications"],
    queryFn: evaluationApi.notifications,
    refetchInterval: 15_000,
  });

  const markRead = useMutation({
    mutationFn: (jobIds: string[]) => evaluationApi.markNotificationsRead(jobIds),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["notifications"] }),
  });

  const items = query.data ?? [];
  const unread = items.filter((item) => !item.read);
  const shown = open ? items.slice(0, 12) : [];

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((prev) => !prev)}
        className="relative flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-sm text-muted-foreground transition-colors hover:bg-accent/60 hover:text-foreground"
      >
        <Bell className="size-4 shrink-0" />
        通知
        {unread.length > 0 && (
          <span className="ml-auto grid size-5 place-items-center rounded-full bg-[var(--fail)] text-[10px] font-semibold text-white">
            {unread.length > 99 ? "99+" : unread.length}
          </span>
        )}
      </button>

      {open && (
        <div className="absolute bottom-full left-0 z-50 mb-2 w-80 rounded-md border border-border bg-card shadow-lg">
          <div className="flex items-center justify-between border-b border-border px-3 py-2">
            <span className="text-xs font-semibold">通知</span>
            {unread.length > 0 && (
              <Button
                variant="ghost"
                size="sm"
                className="h-6 px-2 text-xs"
                disabled={markRead.isPending}
                onClick={() => markRead.mutate(unread.map((item) => item.job_id))}
              >
                <CheckCheck /> 全部已读
              </Button>
            )}
          </div>
          <div className="max-h-80 overflow-y-auto">
            {shown.length === 0 && (
              <p className="px-3 py-4 text-xs text-muted-foreground">暂无通知。</p>
            )}
            {shown.map((item) => (
              <NotificationRow
                key={item.job_id}
                item={item}
                onOpen={() => {
                  if (!item.read) markRead.mutate([item.job_id]);
                  setOpen(false);
                }}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function NotificationRow({ item, onOpen }: { item: NotificationItem; onOpen: () => void }) {
  const failed = item.status === "failed";
  return (
    <Link
      to={`/evaluations/${item.job_id}`}
      onClick={onOpen}
      className={
        "block border-b border-border px-3 py-2 text-xs transition-colors last:border-b-0 hover:bg-accent/50 " +
        (item.read ? "opacity-60" : "")
      }
    >
      <div className="flex items-center justify-between gap-2">
        <Badge
          variant="outline"
          className={failed ? "bg-[var(--fail)]/10 text-[var(--fail)]" : "text-[var(--pass)]"}
        >
          {JOB_STATUS_LABEL[item.status]}
        </Badge>
        <span className="text-muted-foreground">{fmtRelative(item.finished_at)}</span>
      </div>
      <div className="mt-1 truncate">
        {item.benchmark}
        {item.gate_verdict && (
          <span className="text-muted-foreground"> · gate {item.gate_verdict}</span>
        )}
      </div>
      {!item.read && <div className="mt-1 size-1.5 w-1.5 rounded-full bg-primary" />}
    </Link>
  );
}
