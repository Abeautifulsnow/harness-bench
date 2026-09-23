# Directory Structure

> 本项目（agent-eval 平台）的代码组织规范。契约基线：PRD V2.0.1 §94 / Spec V2.1.1。

---

## 顶层布局

```text
src/agent_eval/        # 平台包（src layout，uv_build 后端）
evals/                 # 评测定义树（YAML，随仓库版本管理）
fixtures/              # 夹具数据（Filesystem 目录 / SQLite seed.sql）
tests/                 # pytest（与 src 结构对应命名 test_<模块>.py）
docs/                  # 契约文档（PRD / Spec），实现以 Spec V2.1.1 为准
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
| `runner/` | 编排：调度/超时/失败语义/落盘触发 | 不直接 import YAML/CLI |
| `regression/` | 稳定性判定（Spec §3）纯函数 | 无 IO，输入输出均为模型对象 |
| `storage/` | RunStore：JSONL + JSON 增量落盘 | 写入用 tmp+replace 原子替换 |
| `reports/` | report.json / summary.md 生成 | 纯函数 build/render + IO write 分离 |
| `cli/` | Typer 子命令（benchmark/case/run/gate） | 只做参数解析与退出码映射 |
| `dev/` | mock server 等本地联调工具 | 不被生产代码 import |

## evals/ 定义树

```text
evals/datasets/<id>/dataset.yaml + cases/*.yaml   # Case 遵循 Spec §2.3 Schema
evals/suites/<name>.yaml                          # tags / case_ids 选择
evals/benchmarks/<name>.yaml                      # dataset@version + suites + default_profile
evals/profiles/<name>.yaml                        # Spec §7.1 列表式 Metric Profile
```

## 运行产物

```text
.agent-eval/runs/<run_id>/run.json
  /case_runs/<case>.iter<N>.json     # 增量写（PRD §109.2）
  /traces/<case>.iter<N>.events.jsonl  # Raw Trace append-only（PRD §110）
  /artifacts/<case>/iter<N>/         # fixture 工作目录
  /report.json + summary.md
```
