# Directory Structure

> 本项目（agent-eval 平台）的代码组织规范。契约基线：PRD V2.0.1 §94 / Spec V2.2。

---

## 顶层布局

```text
src/agent_eval/        # 平台包（src layout，uv_build 后端）
evals/                 # 评测定义树（YAML，随仓库版本管理）
fixtures/              # 夹具数据（Filesystem 目录 / SQLite seed.sql）
tests/                 # pytest（与 src 结构对应命名 test_<模块>.py）
docs/                  # 契约文档（PRD / Spec），实现以 Spec V2.2 为准
web/                   # P5 前端（React + TS + shadcn/ui + Tailwind，bun 管理）
.agent-eval/           # 运行产物（gitignored，见下）
```

## src/agent_eval 分层

| 包 | 职责 | 关键约束 |
|---|---|---|
| `models/` | Pydantic 数据模型（Case/Run/CaseRun/MetricResult/TraceSpan/TraceEvent） | 唯一事实词汇来源；命名对齐 PRD §80 表名 |
| `loading/` | evals/ YAML 加载与 case 选择 | 加载失败一律 `InvalidCallError`（exit 3） |
| `adapters/` | AgentAdapter 抽象 + Http(SSE)/Fake 实现 | SDK/协议细节不出本层；传输错误 → `InfraError` |
| `trace/` | TraceBuilder：事件流 → Span Tree | 只消费 TraceEvent，不发起 IO |
| `fixtures/` | FixtureProvider 抽象 + filesystem/sqlite | 每 iteration prepare/cleanup，零共享 |
| `evaluators/` | registry（Metric ID + fallback 链）、native（断言词汇表）、deepeval_adapter | DeepEval SDK 调用只允许出现在 deepeval_adapter |
| `security/` | Security 断言求值 + 安全/红队套件清单 | 判定**行为**（工具参数/命令/路径/SQL/MCP），一律 blocking 且不可被 judge 覆盖（Spec §12） |
| `quality/` | Gate Rules Engine（pr/main/release 共用一份求值器） | 规则集是数据（`evals/gates/*.yaml`）；NO_BASELINE 时相对规则降级为 undetermined |
| `failures/` | Taxonomy / 规则分类 / 聚类 / Promote 草稿 | 分类确定性；SECURITY 一级分类不可被 LLM 改写 |
| `review/` | Human Review 台账 + Review Queue 候选识别 | 只产出建议，不写库（入队是审计动作） |
| `experiment/` | Experiment/Variant/Matrix 定义与执行 | 同一实验内各 variant 共享 benchmark/dataset/profile/repeat/gate |
| `runner/` | 编排：调度/超时/失败语义/落盘触发 | 不直接 import YAML/CLI |
| `execution/` | Execution Plane：EvalRunJob 生命周期、LocalJobExecutor（独立线程 loop）、EvalRunService、Agent Connection Profile | 唯一编排 Runner 的入口；Router 不直接碰 Runner；Secret 只以 env 现场解析，不进 Job 记录 |
| `regression/` | 稳定性判定（Spec §3）+ compare / trace diff | 判定是纯函数；成本/性能类指标方向语义见 Spec §14 |
| `storage/` | RunStore（增量落盘）、BaselineStore、Analytics（DuckDB 投影） | 写入 tmp+replace 原子替换；派生层永远可重建 |
| `reports/` | 五个产物由同一个 RunAggregate 一次写入 | build/render 纯函数与 IO write 分离 |
| `api/` | P5 REST API（PRD §84 + Web Execution 控制面） | **Definition mutation verbs = forbidden；Execution verbs = allowed**（POST 仅 `/eval-runs` 发起/取消，`test_api.py::TestReadOnlyContract` 断言）；派生层 `read_only=True`（Spec §13） |
| `cli/` | Typer 子命令（app.py 聚合 + commands/* 一命令族一模块） | 只做参数解析与退出码映射 |
| `dev/` | mock server 等本地联调工具 | 不被生产代码 import |

## web/ 前端布局

```text
web/src/lib/           # api.ts（只发 GET）/ api-types.ts（REST 契约）/ format.ts / verdicts.ts
web/src/components/ui/ # shadcn/ui 组件（源码入仓，可直接改）
web/src/components/    # common（状态块/徽标/Trace 树）、layout（侧栏健康状态）
web/src/pages/         # 每个导航项一个文件（PRD §72–§78）
```

前端约束：只发 GET；`null` 与 `0` 必须显示得不一样；派生层未构建显示可执行提示
而不是空表格。详见 `web/README.md`。

## evals/ 定义树

```text
evals/datasets/<id>/dataset.yaml + cases/*.yaml   # Case 遵循 Spec §2.3 Schema
evals/suites/<name>.yaml                          # tags / case_ids 选择
evals/benchmarks/<name>.yaml                      # dataset@version + suites + default_profile
evals/profiles/<name>.yaml                        # Spec §7.1 列表式 Metric Profile
evals/gates/{pr,main,release}.yaml                # Spec §6.2 Gate 规则集（数据）
evals/pricing.yaml                                # PRD §59 模型单价（缺失即 cost=null）
```

## 运行产物

```text
.agent-eval/runs/<run_id>/run.json
  /case_runs/<case>.iter<N>.json     # 增量写（PRD §109.2）
  /traces/<case>.iter<N>.events.jsonl  # Raw Trace append-only（PRD §110）
  /artifacts/<case>/iter<N>/         # fixture 工作目录
  /report.json + gate.json + junit.xml + report.html + summary.md   # Spec §6.2 五产物
.agent-eval/state/                   # 派生/台账：baselines.jsonl、experiments/、reviews.jsonl、drafts/
.agent-eval/analytics.duckdb         # DuckDB 投影（可随时 rebuild，非事实源）
```
