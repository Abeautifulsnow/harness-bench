# P5 Web Platform（PRD §84 / §72–§78 / §9.2）

## 范围

只读 Web 平台：**FastAPI 只读 REST API** + **React/TS/shadcn/tailwind 控制台**。
写操作留在 CLI——前端不存在绕过 Gate / Baseline / Review 流程改数据的路径。

## 交付物

| 层 | 位置 | 说明 |
| --- | --- | --- |
| REST API | `src/agent_eval/api/` | 12 个路由模块挂 PRD §84 的全部资源路径 + Dashboard/Trends/Cost/Flaky |
| API 契约类型 | `src/agent_eval/api/schemas.py` | 可复用平台模型的地方一律直接复用（OpenAPI 与契约不会漂移） |
| 前端 | `web/` | bun + Vite 8(rolldown) + React 19 + shadcn/ui + Tailwind v4 + TanStack Query + Recharts |
| 入口 | `agent-eval serve` | 同源托管 `web/dist`，避免 CORS 通配 |
| 测试 | `tests/test_api.py` | 20 个用例，含"动词集合恰为 {get}"的只读断言 |

## 关键判定

1. **只读是可断言的，不是约定。** `TestReadOnlyContract.test_no_write_routes_are_exposed`
   断言 OpenAPI 里出现的 HTTP 动词集合恰好是 `{get}`。新增写端点会立刻红。
2. **派生层只读打开。** DuckDB `read_only=True`；投影文件不存在时返回
   `projection: "missing"` + 可执行提示，**不隐式建库**。隐式建空库会把
   "还没物化"伪装成"平台里没有数据"。
3. **Gate 结论标注来源。** `stored`（当时 gate.json）vs `replayed`（当前规则集重放）。
   改过阈值后看历史 run，两者必须能分别看到。
4. **NO_BASELINE 是一个状态。** `GET /api/regressions/{run}` 返回 409 并说明这是
   Spec §4.3 的降级；不合成一个"看起来正常"的对比。
5. **`null` ≠ `0`。** 无定价时 cost 为 null；前端 `fmtCost(null) === "—"`。
   把 null 当 0 会让趋势图重新出现"成本降到零"。
6. **API 复用平台模型作为 response_model。** 契约模型改字段时 OpenAPI 同步变，
   不会出现"文档一份、实现一份"。

## 验收

```bash
uv run agent-eval serve --port 8123 --static web/dist
# GET /             → SPA（index.html，前端路由回退）
# GET /api/health   → {"status":"ok", ...}
# GET /api/docs     → OpenAPI 文档
```

浏览器实测（13 条路由 + Run Detail 七个 tab）：全部渲染出数据，
无加载卡死、无未捕获错误。

## 遗留

- 前端首包 320 kB（gzip 103 kB），图表按路由懒加载（Trends/Costs/Experiment 才拉
  `CartesianChart-*.js`）。进一步压缩需要把 Recharts 换成更轻的图表库，暂不值得。
- Vite 8 用 rolldown，配置项是 `build.rolldownOptions`（不是 `build.rollupOptions`）。
  自动 chunk 已足够，未手工分块。
