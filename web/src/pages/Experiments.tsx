import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";
import { FlaskConical } from "lucide-react";

import { VerdictBadge } from "@/components/common/badges";
import { Mono, PageHeader, Section } from "@/components/common/primitives";
import { QueryState } from "@/components/common/states";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api } from "@/lib/api";
import { fmtRelative, shortId } from "@/lib/format";

export function Experiments() {
  const query = useQuery({ queryKey: ["experiments"], queryFn: () => api.experiments() });

  const statusVariant: Record<string, "pass" | "fail" | "warn" | "neutral" | "info"> = {
    created: "neutral",
    running: "info",
    completed: "pass",
    partial: "warn",
    failed: "fail",
  };

  return (
    <div className="space-y-5">
      <PageHeader
        title="Experiment（PRD §22–§28）"
        description="Experiment 1—N Variant，Variant 1—N Run。所有 variant 共享同一 benchmark / dataset / profile / repeat / gate —— 这是可比性的前提。"
        actions={
          <Badge variant="outline">
            <FlaskConical /> {query.data?.length ?? 0} 个实验
          </Badge>
        }
      />

      <Section title="实验列表">
        <QueryState
          isLoading={query.isLoading}
          error={query.error}
          isEmpty={query.data?.length === 0}
          emptyTitle="还没有实验"
          emptyHint="用 CLI 创建：`agent-eval experiment create <name> --benchmark <b> --matrix matrix.yaml`"
          onRetry={() => void query.refetch()}
        >
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Experiment</TableHead>
                <TableHead>Name</TableHead>
                <TableHead>Benchmark</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Variants</TableHead>
                <TableHead>Gate</TableHead>
                <TableHead>Created</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {query.data?.map((row) => (
                <TableRow key={row.id}>
                  <TableCell>
                    <Link
                      to={`/experiments/${row.id}`}
                      className="font-mono text-xs text-primary hover:underline"
                    >
                      {shortId(row.id)}
                    </Link>
                  </TableCell>
                  <TableCell>{row.name}</TableCell>
                  <TableCell>{row.benchmark_id}</TableCell>
                  <TableCell>
                    <Badge variant={statusVariant[row.status] ?? "neutral"}>{row.status}</Badge>
                  </TableCell>
                  <TableCell className="tabular">{row.variants}</TableCell>
                  <TableCell>
                    <Mono>{row.gate}</Mono>
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {fmtRelative(row.created_at)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </QueryState>
      </Section>
    </div>
  );
}

export function ExperimentVerdict({ verdict }: { verdict: string | null }) {
  return <VerdictBadge verdict={verdict} />;
}
