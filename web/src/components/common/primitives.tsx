import type { ReactNode } from "react";
import { Link } from "react-router";
import { ArrowRight } from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/common/skeleton";
import { cn } from "@/lib/utils";

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div className="space-y-1">
        <h1 className="text-lg font-semibold tracking-tight">{title}</h1>
        {description && (
          <div className="max-w-3xl text-xs text-muted-foreground">{description}</div>
        )}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  );
}

export function Section({
  title,
  description,
  actions,
  children,
  className,
}: {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <Card className={className}>
      <CardHeader className="gap-1">
        <div className="flex items-center justify-between gap-3">
          <CardTitle>{title}</CardTitle>
          {actions}
        </div>
        {description && <div className="text-xs text-muted-foreground">{description}</div>}
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  );
}

export function StatCard({
  label,
  value,
  unit,
  detail,
  tone = "default",
  loading = false,
}: {
  label: string;
  value: ReactNode;
  unit?: string | null;
  detail?: ReactNode;
  tone?: "default" | "pass" | "fail" | "warn";
  loading?: boolean;
}) {
  const toneClass = {
    default: "text-foreground",
    pass: "text-[var(--pass)]",
    fail: "text-[var(--fail)]",
    warn: "text-[var(--warn)]",
  }[tone];
  return (
    <Card className="gap-1 py-3">
      <CardContent className="space-y-1 px-4">
        <div className="text-[11px] font-medium tracking-wide text-muted-foreground uppercase">
          {label}
        </div>
        {loading ? (
          <Skeleton className="h-7 w-24" />
        ) : (
          <div className={cn("tabular text-xl font-semibold", toneClass)}>
            {value ?? "—"}
            {unit && <span className="ml-1 text-xs font-normal text-muted-foreground">{unit}</span>}
          </div>
        )}
        {detail && <div className="text-xs text-muted-foreground">{detail}</div>}
      </CardContent>
    </Card>
  );
}

/** 键值对表：详情页用来展示"这个对象的身份/配置"，避免一堆零散 label。 */
export function KeyValue({
  items,
  columns = 2,
}: {
  items: { label: string; value: ReactNode }[];
  columns?: 1 | 2 | 3 | 4;
}) {
  const grid = {
    1: "sm:grid-cols-1",
    2: "sm:grid-cols-2",
    3: "sm:grid-cols-3",
    4: "sm:grid-cols-2 lg:grid-cols-4",
  }[columns];
  return (
    <dl className={cn("grid grid-cols-1 gap-x-6 gap-y-3", grid)}>
      {items.map((item) => (
        <div key={item.label} className="min-w-0 space-y-0.5">
          <dt className="text-[11px] tracking-wide text-muted-foreground uppercase">{item.label}</dt>
          <dd className="truncate text-sm" title={typeof item.value === "string" ? item.value : undefined}>
            {item.value ?? "—"}
          </dd>
        </div>
      ))}
    </dl>
  );
}

export function LinkArrow({ to, children }: { to: string; children: ReactNode }) {
  return (
    <Link
      to={to}
      className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
    >
      {children}
      <ArrowRight className="size-3" />
    </Link>
  );
}

export function Mono({ children, className }: { children: ReactNode; className?: string }) {
  return <span className={cn("font-mono text-xs", className)}>{children}</span>;
}
