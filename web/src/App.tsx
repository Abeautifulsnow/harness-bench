import * as React from "react";
import { NavLink, Navigate, Route, Routes, useLocation } from "react-router";
import {
  Activity,
  BadgeCheck,
  Beaker,
  Boxes,
  CalendarClock,
  ClipboardCheck,
  FlaskConical,
  GitCompareArrows,
  LayoutDashboard,
  ListTree,
  Menu,
  PlayCircle,
  Rocket,
  ShieldAlert,
  TrendingUp,
  Wallet,
  X,
} from "lucide-react";

import { HealthPill } from "@/components/layout/HealthPill";
import { NotificationBell } from "@/components/layout/NotificationBell";
import { Loading } from "@/components/common/states";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { cn } from "@/lib/utils";

// 路由级懒加载：recharts 只被 Trends / Cost / Experiment 用到，而绝大多数会话
// 停在 Run Detail / Trace Viewer。整包打进首屏会让只想看一次 trace 的人付图表的钱。
const Dashboard = React.lazy(() =>
  import("@/pages/Dashboard").then((module) => ({ default: module.Dashboard })),
);
const Trends = React.lazy(() =>
  import("@/pages/Trends").then((module) => ({ default: module.Trends })),
);
const Benchmarks = React.lazy(() =>
  import("@/pages/Benchmarks").then((module) => ({ default: module.Benchmarks })),
);
const BenchmarkDetail = React.lazy(() =>
  import("@/pages/Benchmarks").then((module) => ({ default: module.BenchmarkDetail })),
);
const Cases = React.lazy(() =>
  import("@/pages/Cases").then((module) => ({ default: module.Cases })),
);
const Suites = React.lazy(() =>
  import("@/pages/Suites").then((module) => ({ default: module.Suites })),
);
const Runs = React.lazy(() => import("@/pages/Runs").then((module) => ({ default: module.Runs })));
const RunDetail = React.lazy(() =>
  import("@/pages/RunDetail").then((module) => ({ default: module.RunDetail })),
);
const TraceViewer = React.lazy(() =>
  import("@/pages/TraceViewer").then((module) => ({ default: module.TraceViewer })),
);
const Experiments = React.lazy(() =>
  import("@/pages/Experiments").then((module) => ({ default: module.Experiments })),
);
const ExperimentDetail = React.lazy(() =>
  import("@/pages/ExperimentDetail").then((module) => ({ default: module.ExperimentDetail })),
);
const Regression = React.lazy(() =>
  import("@/pages/Regression").then((module) => ({ default: module.Regression })),
);
const Failures = React.lazy(() =>
  import("@/pages/Failures").then((module) => ({ default: module.Failures })),
);
const Quality = React.lazy(() =>
  import("@/pages/Quality").then((module) => ({ default: module.Quality })),
);
const Reviews = React.lazy(() =>
  import("@/pages/Reviews").then((module) => ({ default: module.Reviews })),
);
const Security = React.lazy(() =>
  import("@/pages/Security").then((module) => ({ default: module.Security })),
);
const Costs = React.lazy(() => import("@/pages/Costs").then((module) => ({ default: module.Costs })));
const Evaluations = React.lazy(() =>
  import("@/pages/Evaluations").then((module) => ({ default: module.Evaluations })),
);
const EvaluationNew = React.lazy(() =>
  import("@/pages/EvaluationNew").then((module) => ({ default: module.EvaluationNew })),
);
const EvaluationDetail = React.lazy(() =>
  import("@/pages/EvaluationDetail").then((module) => ({ default: module.EvaluationDetail })),
);
const Automation = React.lazy(() =>
  import("@/pages/Automation").then((module) => ({ default: module.Automation })),
);

/** PRD §72–§78 的导航面。Review / Cost / Trends 是**一级入口**（PRD §9.2），
 *  不塞进"管理"折叠菜单——它们回答的是每天都会问的问题。 */
const NAV: { group: string; items: { to: string; label: string; icon: React.ElementType }[] }[] = [
  {
    group: "总览",
    items: [
      { to: "/", label: "Dashboard", icon: LayoutDashboard },
      { to: "/trends", label: "趋势", icon: TrendingUp },
    ],
  },
  {
    group: "资产",
    items: [
      { to: "/benchmarks", label: "Benchmark", icon: Boxes },
      { to: "/cases", label: "Case", icon: ListTree },
      { to: "/suites", label: "Suite", icon: BadgeCheck },
    ],
  },
  {
    group: "执行",
    items: [
      { to: "/evaluations", label: "Evaluation", icon: Rocket },
      { to: "/automation", label: "自动化", icon: CalendarClock },
      { to: "/runs", label: "Run", icon: PlayCircle },
      { to: "/traces", label: "Trace Viewer", icon: Activity },
      { to: "/experiments", label: "Experiment", icon: FlaskConical },
    ],
  },
  {
    group: "质量",
    items: [
      { to: "/regressions", label: "Regression", icon: GitCompareArrows },
      { to: "/failures", label: "Failure", icon: Beaker },
      { to: "/quality", label: "Quality Gate", icon: ClipboardCheck },
      { to: "/reviews", label: "Review", icon: ClipboardCheck },
      { to: "/security", label: "Security", icon: ShieldAlert },
      { to: "/costs", label: "Cost", icon: Wallet },
    ],
  },
];

function SidebarNav({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <nav className="flex flex-col gap-4 p-3">
      {NAV.map((section) => (
        <div key={section.group} className="space-y-1">
          <div className="px-2 text-[10px] font-semibold tracking-wider text-muted-foreground uppercase">
            {section.group}
          </div>
          {section.items.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === "/"}
              onClick={onNavigate}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-2 rounded-md px-2 py-1.5 text-sm transition-colors",
                  isActive
                    ? "bg-accent text-accent-foreground"
                    : "text-muted-foreground hover:bg-accent/60 hover:text-foreground",
                )
              }
            >
              <item.icon className="size-4 shrink-0" />
              {item.label}
            </NavLink>
          ))}
        </div>
      ))}
    </nav>
  );
}

function AppShell({ children }: { children: React.ReactNode }) {
  const [mobileOpen, setMobileOpen] = React.useState(false);
  const location = useLocation();

  React.useEffect(() => {
    setMobileOpen(false);
  }, [location.pathname]);

  return (
    <div className="flex min-h-screen">
      <aside className="sticky top-0 hidden h-screen w-56 shrink-0 flex-col border-r border-border bg-card/40 lg:flex">
        <div className="flex h-12 items-center gap-2 px-3">
          <div className="grid size-6 place-items-center rounded-md bg-primary text-[11px] font-bold text-primary-foreground">
            ae
          </div>
          <div className="text-sm font-semibold">agent-eval</div>
        </div>
        <Separator />
        <div className="flex-1 overflow-y-auto">
          <SidebarNav />
        </div>
        <Separator />
        <div className="space-y-1 p-3">
          <NotificationBell />
          <HealthPill />
        </div>
      </aside>

      {mobileOpen && (
        <div className="fixed inset-0 z-40 lg:hidden">
          <div
            className="absolute inset-0 bg-background/70 backdrop-blur-sm"
            onClick={() => setMobileOpen(false)}
          />
          <aside className="absolute top-0 left-0 h-full w-64 border-r border-border bg-card">
            <div className="flex h-12 items-center justify-between px-3">
              <div className="text-sm font-semibold">agent-eval</div>
              <Button variant="ghost" size="icon" onClick={() => setMobileOpen(false)}>
                <X />
              </Button>
            </div>
            <Separator />
            <SidebarNav onNavigate={() => setMobileOpen(false)} />
          </aside>
        </div>
      )}

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex h-12 items-center gap-3 border-b border-border bg-background/85 px-4 backdrop-blur lg:hidden">
          <Button variant="ghost" size="icon" onClick={() => setMobileOpen(true)}>
            <Menu />
          </Button>
          <span className="text-sm font-semibold">agent-eval</span>
        </header>
        <main className="mx-auto w-full max-w-[1600px] flex-1 space-y-5 p-4 lg:p-6">{children}</main>
      </div>
    </div>
  );
}

export function AppRoutes() {
  return (
    <AppShell>
      <React.Suspense fallback={<Loading label="加载页面" />}>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/trends" element={<Trends />} />
          <Route path="/benchmarks" element={<Benchmarks />} />
          <Route path="/benchmarks/:name" element={<BenchmarkDetail />} />
          <Route path="/cases" element={<Cases />} />
          <Route path="/suites" element={<Suites />} />
          <Route path="/runs" element={<Runs />} />
          <Route path="/runs/:runId" element={<RunDetail />} />
          <Route path="/evaluations" element={<Evaluations />} />
          <Route path="/evaluations/new" element={<EvaluationNew />} />
          <Route path="/evaluations/:jobId" element={<EvaluationDetail />} />
          <Route path="/automation" element={<Automation />} />
          <Route path="/traces" element={<TraceViewer />} />
          <Route path="/experiments" element={<Experiments />} />
          <Route path="/experiments/:experimentId" element={<ExperimentDetail />} />
          <Route path="/regressions" element={<Regression />} />
          <Route path="/failures" element={<Failures />} />
          <Route path="/quality" element={<Quality />} />
          <Route path="/reviews" element={<Reviews />} />
          <Route path="/security" element={<Security />} />
          <Route path="/costs" element={<Costs />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </React.Suspense>
    </AppShell>
  );
}
