import * as React from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router";
import { Activity, ArrowLeft } from "lucide-react";

import { PageHeader, Section } from "@/components/common/primitives";
import { ErrorState } from "@/components/common/states";
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
import { api } from "@/lib/api";
import {
  evaluationApi,
  type AgentConnection,
  type AgentHealthReport,
  type EvalRunRequest,
} from "@/lib/evaluation-api";
import type * as T from "@/lib/api-types";

/** 表单状态：select 存 "default" 哨兵（= 跟随 Benchmark 定义 / null），数值项存字符串。 */
interface EvaluationForm {
  agentProfile: string;
  benchmark: string;
  suite: string;
  profile: string;
  repeat: string;
  concurrency: string;
  noJudge: string;
  strictProtocol: string;
  tags: string;
  // §31：baseline 策略。"default" = 不传（Runner 按默认策略解析）；explicit 必须给 run。
  baselinePolicy: string;
  baselineRun: string;
}

type FormUpdater = <K extends keyof EvaluationForm>(key: K, value: EvaluationForm[K]) => void;

/** 设计文档 §7.1/§43.1：New Evaluation —— 选 Agent → 选 Benchmark → 参数 → Health Check → Run。 */
export function EvaluationNew() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();

  const benchmarks = useQuery({ queryKey: ["benchmarks"], queryFn: api.benchmarks });
  const suites = useQuery({ queryKey: ["suites"], queryFn: api.suites });
  const profiles = useQuery({ queryKey: ["profiles"], queryFn: api.profiles });
  const connections = useQuery({
    queryKey: ["agent-connections"],
    queryFn: evaluationApi.agentConnections,
  });
  // V2 §52：评测预设——一键把模板参数填进表单（字段允许部分给出）。
  const presets = useQuery({ queryKey: ["eval-presets"], queryFn: evaluationApi.presets });

  const [form, setForm] = React.useState<EvaluationForm>({
    agentProfile: "",
    benchmark: searchParams.get("benchmark") ?? "",
    suite: "default",
    profile: "default",
    repeat: "1",
    concurrency: "4",
    noJudge: "false",
    strictProtocol: "false",
    tags: "",
    baselinePolicy: "default",
    baselineRun: "",
  });
  const update: FormUpdater = (key, value) => setForm((prev) => ({ ...prev, [key]: value }));

  const applyPreset = (presetId: string) => {
    const preset = presets.data?.find((p) => p.id === presetId);
    if (!preset) return;
    const r = preset.request;
    setForm((prev) => ({
      ...prev,
      benchmark: r.benchmark ?? prev.benchmark,
      agentProfile: r.agent_profile ?? prev.agentProfile,
      suite: r.suite?.length === 1 ? r.suite[0] : "default",
      profile: r.profile ?? "default",
      repeat: r.repeat != null ? String(r.repeat) : prev.repeat,
      concurrency: r.agent_concurrency != null ? String(r.agent_concurrency) : prev.concurrency,
      noJudge: r.no_judge != null ? String(r.no_judge) : prev.noJudge,
      strictProtocol: r.strict_protocol != null ? String(r.strict_protocol) : prev.strictProtocol,
      tags: r.tags?.join(", ") ?? prev.tags,
      baselinePolicy: r.baseline_policy ?? prev.baselinePolicy,
      baselineRun: r.baseline_run ?? prev.baselineRun,
    }));
  };

  // §36：每次进入页面生成一个幂等键；连点/浏览器重试不会产生两个相同评测。
  const idempotencyKey = React.useRef(
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `web-${Date.now()}`,
  );

  React.useEffect(() => {
    if (!form.agentProfile && connections.data) {
      const firstEnabled = connections.data.find((c) => c.enabled);
      if (firstEnabled) update("agentProfile", firstEnabled.id);
    }
    // 仅在连接列表首次到达时填默认值，不随 form 变化重跑。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connections.data]);

  const submit = useMutation({
    mutationFn: () => evaluationApi.createEvalRun(toRequest(form), idempotencyKey.current),
    onSuccess: (job) => {
      void navigate(`/evaluations/${job.job_id}`);
    },
  });

  const repeatNum = Number(form.repeat);
  const concurrencyNum = Number(form.concurrency);
  const formInvalid =
    !form.agentProfile ||
    !form.benchmark ||
    !(repeatNum >= 1 && repeatNum <= 10) ||
    !(concurrencyNum >= 1 && concurrencyNum <= 16) ||
    (form.baselinePolicy === "explicit" && !form.baselineRun.trim());

  return (
    <div className="space-y-5">
      <PageHeader
        title="New Evaluation"
        description="设计文档 §4/§7：不走 CLI 完成一次标准化评测。参数受服务端限制（repeat ≤ 10、agent_concurrency ≤ 16），Profile 与 Suite 来自 Git 定义树，Web 不临时修改。"
        actions={
          <div className="flex items-center gap-2">
            {presets.data && presets.data.length > 0 && (
              <Select onValueChange={applyPreset}>
                <SelectTrigger className="w-52">
                  <SelectValue placeholder="从预设填充…" />
                </SelectTrigger>
                <SelectContent>
                  {presets.data.map((p) => (
                    <SelectItem key={p.id} value={p.id}>
                      {p.display_name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
            <Button variant="ghost" asChild>
              <a onClick={() => void navigate(-1)} role="button">
                <ArrowLeft /> 返回
              </a>
            </Button>
          </div>
        }
      />

      <div className="grid gap-5 lg:grid-cols-2">
        <TargetSection
          form={form}
          update={update}
          connections={connections.data}
          benchmarks={benchmarks.data}
          suites={suites.data}
          profiles={profiles.data}
        />
        <ParamsSection form={form} update={update} />
      </div>

      <HealthCheckCard agentProfile={form.agentProfile} />

      {submit.error && <ErrorState error={submit.error} />}

      <div className="flex items-center justify-end gap-3">
        <Button size="lg" disabled={formInvalid || submit.isPending} onClick={() => submit.mutate()}>
          {submit.isPending ? "提交中…" : "Run Evaluation"}
        </Button>
      </div>
    </div>
  );
}

function toRequest(form: EvaluationForm): EvalRunRequest {
  return {
    agent_profile: form.agentProfile,
    benchmark: form.benchmark,
    suite: form.suite === "default" ? null : [form.suite],
    profile: form.profile === "default" ? null : form.profile,
    repeat: Number(form.repeat) || 1,
    agent_concurrency: Number(form.concurrency) || 4,
    no_judge: form.noJudge === "true",
    strict_protocol: form.strictProtocol === "true",
    tags: form.tags
      .split(",")
      .map((tag) => tag.trim())
      .filter(Boolean),
    baseline_policy: form.baselinePolicy === "default" ? null : form.baselinePolicy,
    baseline_run: form.baselineRun.trim() || null,
  };
}

/** 设计文档 §7 的 Agent / Benchmark / Suite / Profile 选择区。 */
function TargetSection({
  form,
  update,
  connections,
  benchmarks,
  suites,
  profiles,
}: {
  form: EvaluationForm;
  update: FormUpdater;
  connections?: AgentConnection[];
  benchmarks?: T.BenchmarkRow[];
  suites?: T.SuiteRow[];
  profiles?: T.ProfileRow[];
}) {
  return (
    <Section title="目标" description="选择被测 Agent 与要执行的 Benchmark。">
      <div className="space-y-4">
        <Field label="Agent">
          <Select value={form.agentProfile} onValueChange={(v) => update("agentProfile", v)}>
            <SelectTrigger className="w-full">
              <SelectValue placeholder="选择已注册的 Agent Connection" />
            </SelectTrigger>
            <SelectContent>
              {connections?.map((c) => (
                <SelectItem key={c.id} value={c.id} disabled={!c.enabled}>
                  {c.display_name}
                  {c.secret_state === "missing" && "（凭证缺失）"}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>
        <Field label="Benchmark">
          <Select value={form.benchmark} onValueChange={(v) => update("benchmark", v)}>
            <SelectTrigger className="w-full">
              <SelectValue placeholder="选择 benchmark" />
            </SelectTrigger>
            <SelectContent>
              {benchmarks?.map((b) => (
                <SelectItem key={b.name} value={b.name}>
                  {b.name}（{b.cases} cases）
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Suite">
            <Select value={form.suite} onValueChange={(v) => update("suite", v)}>
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="default">跟随 Benchmark 定义</SelectItem>
                {suites
                  ?.filter((s) => s.kind === "suite")
                  .map((s) => (
                    <SelectItem key={s.name} value={s.name}>
                      {s.name}（{s.cases}）
                    </SelectItem>
                  ))}
              </SelectContent>
            </Select>
          </Field>
          <Field label="Profile">
            <Select value={form.profile} onValueChange={(v) => update("profile", v)}>
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="default">跟随 Benchmark 定义</SelectItem>
                {profiles?.map((p) => (
                  <SelectItem key={p.name} value={p.name}>
                    {p.name}（{p.metrics} metrics）
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
        </div>
      </div>
    </Section>
  );
}

/** repeat / 并发 / Judge / Strict Protocol / Tags——直接映射 RunConfig，语义与 CLI 一致。 */
function ParamsSection({
  form,
  update,
}: {
  form: EvaluationForm;
  update: FormUpdater;
}) {
  return (
    <Section
      title="运行参数"
      description="这些参数直接映射到 Runner 的 RunConfig，语义与 CLI 完全一致。"
    >
      <div className="space-y-4">
        <div className="grid grid-cols-2 gap-3">
          <Field label="Repeat（1–10）">
            <Input
              value={form.repeat}
              onChange={(e) => update("repeat", e.target.value)}
              inputMode="numeric"
            />
          </Field>
          <Field label="Agent 并发（1–16）">
            <Input
              value={form.concurrency}
              onChange={(e) => update("concurrency", e.target.value)}
              inputMode="numeric"
            />
          </Field>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Judge">
            <Select value={form.noJudge} onValueChange={(v) => update("noJudge", v)}>
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="false">启用</SelectItem>
                <SelectItem value="true">禁用（no_judge）</SelectItem>
              </SelectContent>
            </Select>
          </Field>
          <Field label="Strict Protocol">
            <Select value={form.strictProtocol} onValueChange={(v) => update("strictProtocol", v)}>
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="false">默认（warn）</SelectItem>
                <SelectItem value="true">开启（未知事件 → exit 2）</SelectItem>
              </SelectContent>
            </Select>
          </Field>
        </div>
        <Field label="Tags（逗号分隔）">
          <Input
            value={form.tags}
            onChange={(e) => update("tags", e.target.value)}
            placeholder="smoke, regression"
          />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Baseline 策略">
            <Select value={form.baselinePolicy} onValueChange={(v) => update("baselinePolicy", v)}>
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="default">自动（Gate / 实验默认策略）</SelectItem>
                <SelectItem value="main-latest">main 最新（main-latest）</SelectItem>
                <SelectItem value="explicit">指定 baseline run</SelectItem>
                <SelectItem value="NO_BASELINE">不比基线（NO_BASELINE）</SelectItem>
              </SelectContent>
            </Select>
          </Field>
          <Field label="Baseline Run ID（explicit 必填）">
            <Input
              value={form.baselineRun}
              onChange={(e) => update("baselineRun", e.target.value)}
              placeholder="run_…"
              disabled={form.baselinePolicy !== "explicit"}
            />
          </Field>
        </div>
      </div>
    </Section>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block space-y-1.5">
      <span className="text-[11px] font-medium tracking-wide text-muted-foreground uppercase">
        {label}
      </span>
      {children}
    </label>
  );
}

/** 设计文档 §9：启动前先看健康与观测面——哪些 Metric 会被真实评估、哪些会 skipped。 */
function HealthCheckCard({ agentProfile }: { agentProfile: string }) {
  const health = useMutation({ mutationFn: () => evaluationApi.agentHealth(agentProfile) });

  return (
    <Section
      title="Health Check"
      description="读取 Agent 接入协议的 /health：确认可达、实际生效模型与观测面能力表。"
      actions={
        <Button
          variant="outline"
          size="sm"
          disabled={!agentProfile || health.isPending}
          onClick={() => health.mutate()}
        >
          <Activity /> {health.isPending ? "探测中…" : "Health Check"}
        </Button>
      }
    >
      {health.error && <ErrorState error={health.error} />}
      {health.data && <HealthReport report={health.data} />}
      {!health.data && !health.error && (
        <p className="text-xs text-muted-foreground">选择 Agent 后点击 Health Check。</p>
      )}
    </Section>
  );
}

function HealthReport({ report }: { report: AgentHealthReport }) {
  const surface = Object.entries(report.observation_surface);
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <Badge
          variant="outline"
          className={report.ok ? "bg-[var(--pass)]/10 text-[var(--pass)]" : "bg-[var(--fail)]/10 text-[var(--fail)]"}
        >
          {report.ok ? "Healthy" : "Unhealthy"}
        </Badge>
        {report.agent_model && (
          <span className="text-xs text-muted-foreground">
            Model: <span className="font-mono">{report.agent_model}</span>
          </span>
        )}
      </div>
      {surface.length > 0 && (
        <div>
          <div className="mb-1.5 text-[11px] tracking-wide text-muted-foreground uppercase">
            Observation Surface（未声明/False 的观测面对应 Metric 会 skipped）
          </div>
          <div className="flex flex-wrap gap-1.5">
            {surface.map(([event, ok]) => (
              <Badge
                key={event}
                variant="outline"
                className={ok ? "text-[var(--pass)]" : "text-muted-foreground line-through"}
              >
                {event} {ok ? "✓" : "✗"}
              </Badge>
            ))}
          </div>
        </div>
      )}
      {!report.ok && report.detail && (
        <p className="font-mono text-xs break-all text-[var(--fail)]">{report.detail}</p>
      )}
    </div>
  );
}
