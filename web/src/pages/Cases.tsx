import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { Search } from "lucide-react";

import { KeyValue, Mono, PageHeader, Section } from "@/components/common/primitives";
import { QueryState } from "@/components/common/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api } from "@/lib/api";

export function Cases() {
  const [search, setSearch] = React.useState("");
  const [debounced, setDebounced] = React.useState("");
  const [dataset, setDataset] = React.useState<string>("all");
  const [selected, setSelected] = React.useState<string | null>(null);

  React.useEffect(() => {
    const timer = setTimeout(() => setDebounced(search), 250);
    return () => clearTimeout(timer);
  }, [search]);

  const datasets = useQuery({ queryKey: ["datasets"], queryFn: api.datasets });
  const cases = useQuery({
    queryKey: ["cases", { q: debounced, dataset }],
    queryFn: () =>
      api.cases({
        q: debounced || undefined,
        dataset: dataset === "all" ? undefined : dataset,
        limit: 1000,
      }),
  });
  const detail = useQuery({
    queryKey: ["case", selected, dataset],
    queryFn: () => api.case(selected!, dataset === "all" ? undefined : dataset),
    enabled: Boolean(selected),
  });

  return (
    <div className="space-y-5">
      <PageHeader
        title="Case"
        description="PRD §14：Case 目录。判定挂载点列显示这条 case 实际声明在哪些位置（output / tools / constraints / security / turns / final）。"
      />

      <div className="flex flex-wrap items-center gap-2">
        <div className="relative">
          <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="搜索 case id / 名称"
            className="w-64 pl-8"
          />
        </div>
        <Select value={dataset} onValueChange={setDataset}>
          <SelectTrigger className="w-52">
            <SelectValue placeholder="全部 dataset" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部 dataset</SelectItem>
            {datasets.data?.map((item) => (
              <SelectItem key={item.id} value={`${item.id}@${item.version}`}>
                {item.id}@{item.version}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <span className="text-xs text-muted-foreground">
          {cases.data ? `${cases.data.length} 条` : ""}
        </span>
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_380px]">
        <Section title="Case 列表">
          <QueryState
            isLoading={cases.isLoading}
            error={cases.error}
            isEmpty={cases.data?.length === 0}
            emptyTitle="没有匹配的 case"
            emptyHint="换个关键字，或清掉 dataset 过滤。"
            onRetry={() => void cases.refetch()}
          >
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Case</TableHead>
                  <TableHead>Name</TableHead>
                  <TableHead>Tags</TableHead>
                  <TableHead>挂载点</TableHead>
                  <TableHead className="text-right" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {cases.data?.map((item) => (
                  <TableRow key={`${item.dataset_id}-${item.id}`}>
                    <TableCell className="font-mono text-xs">{item.id}</TableCell>
                    <TableCell className="max-w-xs truncate">{item.name}</TableCell>
                    <TableCell>
                      <div className="flex flex-wrap gap-1">
                        {item.tags.slice(0, 4).map((tag) => (
                          <Badge key={tag} variant="outline">
                            {tag}
                          </Badge>
                        ))}
                      </div>
                    </TableCell>
                    <TableCell>
                      <div className="flex flex-wrap gap-1">
                        {item.mount_points.map((mount) => (
                          <Badge key={mount} variant="neutral">
                            {mount}
                          </Badge>
                        ))}
                      </div>
                    </TableCell>
                    <TableCell className="text-right">
                      <Button size="sm" variant="ghost" onClick={() => setSelected(item.id)}>
                        断言
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </QueryState>
        </Section>

        <Section
          title="断言定义"
          description={selected ? "直接读 evals/ 定义树（事实层）。" : "从左侧选一条 case。"}
        >
          {!selected ? (
            <p className="text-sm text-muted-foreground">未选择 case。</p>
          ) : (
            <QueryState
              isLoading={detail.isLoading}
              error={detail.error}
              onRetry={() => void detail.refetch()}
            >
              {detail.data && <CaseAssertionView payload={detail.data} />}
            </QueryState>
          )}
        </Section>
      </div>
    </div>
  );
}

function CaseAssertionView({ payload }: { payload: Record<string, unknown> }) {
  const expected = (payload.expected ?? {}) as Record<string, unknown>;
  const output = (expected.output ?? {}) as Record<string, unknown>;
  const tools = (expected.tools ?? {}) as Record<string, unknown>;
  const constraints = (expected.constraints ?? {}) as Record<string, unknown>;
  const security = (expected.security ?? {}) as Record<string, unknown>;
  const extensions = (expected.extensions ?? {}) as Record<string, unknown>;
  const input = (payload.input ?? {}) as Record<string, unknown>;
  const turns = (input.turns ?? []) as Record<string, unknown>[];

  const securityRules = Object.entries(security).filter(
    ([key, value]) => key !== "allow_permission_override" && Array.isArray(value) && value.length,
  );

  return (
    <div className="space-y-4">
      <KeyValue
        columns={1}
        items={[
          { label: "ID", value: <Mono>{String(payload.id)}</Mono> },
          { label: "Name", value: String(payload.name ?? "—") },
          { label: "Type", value: <Mono>{String(input.type ?? "single_turn")}</Mono> },
          { label: "Turns", value: String(turns.length || 1) },
        ]}
      />

      {(output.exact || output.regex || (output.contains as unknown[])?.length) && (
        <div className="space-y-1">
          <div className="text-[11px] tracking-wide text-muted-foreground uppercase">output</div>
          <pre className="overflow-x-auto rounded-md border border-border bg-muted/40 p-2 text-xs">
            {JSON.stringify(output, null, 2)}
          </pre>
        </div>
      )}

      {((tools.required as string[])?.length || (tools.forbidden as string[])?.length) && (
        <div className="space-y-1">
          <div className="text-[11px] tracking-wide text-muted-foreground uppercase">tools</div>
          <div className="space-y-1 text-xs">
            {(tools.required as string[])?.length > 0 && (
              <div>
                <span className="text-muted-foreground">required：</span>
                {(tools.required as string[]).join(", ")}
              </div>
            )}
            {(tools.forbidden as string[])?.length > 0 && (
              <div>
                <span className="text-muted-foreground">forbidden：</span>
                {(tools.forbidden as string[]).join(", ")}
              </div>
            )}
          </div>
        </div>
      )}

      {Object.keys(constraints).length > 0 && (
        <div className="space-y-1">
          <div className="text-[11px] tracking-wide text-muted-foreground uppercase">constraints</div>
          <KeyValue
            columns={2}
            items={Object.entries(constraints).map(([key, value]) => ({
              label: key,
              value: String(value),
            }))}
          />
        </div>
      )}

      {securityRules.length > 0 && (
        <div className="space-y-1">
          <div className="text-[11px] tracking-wide text-muted-foreground uppercase">
            security（Spec §12 独立挂载点）
          </div>
          <div className="space-y-1">
            {securityRules.map(([key, value]) => (
              <div key={key} className="flex flex-wrap items-baseline gap-2 text-xs">
                <span className="text-muted-foreground">{key}</span>
                {(value as string[]).map((item) => (
                  <Badge key={item} variant="fail">
                    {item}
                  </Badge>
                ))}
              </div>
            ))}
            {security.allow_permission_override === true && (
              <Badge variant="warn">允许权限覆盖</Badge>
            )}
          </div>
        </div>
      )}

      {Object.keys(extensions).length > 0 && (
        <div className="space-y-1">
          <div className="text-[11px] tracking-wide text-muted-foreground uppercase">
            extensions（native 评测器消费）
          </div>
          <pre className="overflow-x-auto rounded-md border border-border bg-muted/40 p-2 text-xs">
            {JSON.stringify(extensions, null, 2)}
          </pre>
        </div>
      )}

      {turns.length > 0 && (
        <div className="space-y-1">
          <div className="text-[11px] tracking-wide text-muted-foreground uppercase">
            turn 级断言
          </div>
          <div className="space-y-2">
            {turns.map((turn, index) => (
              <div key={index} className="rounded-md border border-border p-2 text-xs">
                <div className="text-muted-foreground">turn {index + 1}</div>
                <div className="mt-1 line-clamp-3">{String(turn.user ?? "")}</div>
                {turn.expect != null && (
                  <pre className="mt-1 overflow-x-auto text-[11px] text-muted-foreground">
                    {JSON.stringify(turn.expect, null, 2)}
                  </pre>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
