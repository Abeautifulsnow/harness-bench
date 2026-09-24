import { useQuery } from "@tanstack/react-query";
import { ShieldCheck } from "lucide-react";

import { PageHeader, Section } from "@/components/common/primitives";
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

export function Suites() {
  const query = useQuery({ queryKey: ["suites"], queryFn: api.suites });

  const kindLabel: Record<string, { label: string; variant: "neutral" | "info" | "fail" }> = {
    suite: { label: "定义套件", variant: "neutral" },
    security: { label: "安全回归", variant: "info" },
    "red-team": { label: "红队", variant: "fail" },
  };

  return (
    <div className="space-y-5">
      <PageHeader
        title="Suite"
        description="PRD §19/§62：suites/*.yaml 是显式定义；安全与红队套件按 tag 选择 case（不写死列表），因此在本页单列。"
      />
      <QueryState
        isLoading={query.isLoading}
        error={query.error}
        isEmpty={query.data?.length === 0}
        emptyTitle="还没有 suite"
        onRetry={() => void query.refetch()}
      >
        <Section title="全部套件">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Suite</TableHead>
                <TableHead>Kind</TableHead>
                <TableHead>Selection</TableHead>
                <TableHead>Cases</TableHead>
                <TableHead>说明</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {query.data?.map((row) => (
                <TableRow key={`${row.kind}-${row.name}`}>
                  <TableCell className="flex items-center gap-2 font-medium">
                    {row.kind !== "suite" && <ShieldCheck className="size-3.5 text-muted-foreground" />}
                    {row.name}
                  </TableCell>
                  <TableCell>
                    <Badge variant={kindLabel[row.kind]?.variant ?? "neutral"}>
                      {kindLabel[row.kind]?.label ?? row.kind}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {row.tags.length > 0 && (
                      <span>
                        tags: <span className="font-mono">{row.tags.join(", ")}</span>
                      </span>
                    )}
                    {row.case_ids.length > 0 && (
                      <span className="ml-2">
                        case_ids: <span className="font-mono">{row.case_ids.length} 条</span>
                      </span>
                    )}
                    {row.tags.length === 0 && row.case_ids.length === 0 && "—"}
                  </TableCell>
                  <TableCell className="tabular">{row.cases}</TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {row.description || "—"}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Section>
      </QueryState>
    </div>
  );
}
