# agent-eval Web 控制台（P5）

React + TypeScript + shadcn/ui + Tailwind 的控制台，覆盖 PRD §72–§78 与 §9.2
的全部导航面（Dashboard / Benchmark / Case / Suite / Run / Trace / Experiment /
Regression / Failure / Quality / Review / Security / Cost / Trends），以及
Web Execution 控制面（Evaluation，docs/web-evaluation-control-plane-design.md §38）。

## 安全边界：Definition 只读 + 受控执行

Definition 数据（Benchmark / Dataset / Case / Suite / Gate）依然只读：前端不存在
"绕过 Gate / Baseline / Review 流程改数据"的路径——这不是约定，而是结构性的：
OpenAPI 里 PUT/DELETE/PATCH 不存在，POST 只出现在 `/api/eval-runs`
（发起/取消评测两个执行动词，`tests/test_api.py::TestReadOnlyContract` 断言这一点）。

执行面的 POST 只有两个，全部经受控入口进入 Runner：

| 动作 | 端点 |
| --- | --- |
| 发起评测（202；幂等重放 200） | `POST /api/eval-runs` |
| 取消评测（Runner 收到真实 cancellation） | `POST /api/eval-runs/{job_id}/cancel` |

Agent endpoint 由管理员预注册在 `evals/agents/*.yaml`，Web 只能选择；
凭证（secret_ref → env var）只在服务端解析，浏览器永远看不到值。

## 工具链选择

| 用途 | 选择 | 原因 |
| --- | --- | --- |
| 包管理 / 运行时 | **bun** | 全局约定；bun 有问题时退化为 pnpm（两者都不提交锁文件冲突：仓库只认 `bun.lock`） |
| 构建 | **Vite 8**（rolldown 内核） | Vite 生态里构建最快的默认选项 |
| 样式 | **Tailwind v4**（`@tailwindcss/vite`） | 无需 PostCSS 配置，令牌用 `@theme` 写在一个 CSS 文件里 |
| 组件 | **shadcn/ui**（Radix + CVA） | 组件源码入仓，可直接改；不引入运行时主题库 |
| 数据 | **TanStack Query 5** | 只读查询的缓存/失效语义清晰 |
| 图表 | **Recharts 3** | 与 React 19 兼容，声明式 |

Vite 8 用的是 **rolldown**（Rust 内核）。因此配置项里没有 `build.rollupOptions`，
代码分割走自动 chunk 识别——这解释了为什么 `dist/assets/` 里会出现
`CartesianChart-*.js`、`select-*.js` 这类按依赖自动切出来的 chunk。

## 命令

```bash
bun install          # 安装依赖
bun run dev          # 开发服务器（/api 代理到 127.0.0.1:8000）
bun run typecheck    # tsc --noEmit
bun run build        # typecheck + 产出 web/dist
```

后端启动（同源托管前端构建产物，不需要 CORS 通配）：

```bash
cd ..
uv run agent-eval serve --port 8000 --static web/dist
# → http://127.0.0.1:8000
```

开发期把后端跑在 8000、前端跑在 5173，`vite.config.ts` 里的 proxy 会把 `/api`
转发过去。若后端换端口，用 `AGENT_EVAL_API=http://127.0.0.1:PORT bun run dev`。

## 目录

```
src/
  lib/
    api.ts            只读客户端（只发 GET；409/404 语义原样上抛）
    evaluation-api.ts 执行面客户端（仅有的两个 POST：发起/取消评测；Job SSE 订阅）
    api-types.ts      REST 契约类型，与 src/agent_eval/api/schemas.py 对齐
    format.ts         数字/时间格式化（null ≠ 0 的显示约定集中在这里）
    verdicts.ts       判定 → 配色/中文标签
    dimensions.ts     PRD §25 实验维度清单
  components/
    ui/               shadcn/ui 组件（源码入仓）
    common/           状态块、徽标、Trace 树等跨页复用件
    layout/           侧栏健康状态
  pages/              每个导航项一个文件（Evaluation 三页见 §43：列表/新建/详情）
```

## 两条硬约定

1. **`null` 与 `0` 必须显示得不一样。** PRD §59 规定无定价时 cost 为 `null` 而不是
   `0.0`；`fmtCost(null) === "—"`，`fmtCost(0) === "$0"`。任何"把 null 当 0"的改动
   都会让成本趋势图重新出现"成本降到零"的假象。
2. **派生层未构建 ≠ 没有数据。** DuckDB 投影缺失时页面显示可执行的提示
   （`agent-eval storage rebuild`），而不是空表格——`ProjectionNotice` 组件负责这点。
   "查询成功但为空"和"投影还没物化"是两种状态，UI 必须能区分。
