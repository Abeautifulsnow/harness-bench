# harness-bench — Agent Evaluation & Regression Platform

Agent 工程质量平台：Benchmark / Experiment / Evaluation / Regression / Failure Intelligence / Quality Gate。

- 契约文档：[docs/agent-eval-engineering-spec-v2.1.md](docs/agent-eval-engineering-spec-v2.1.md)（Spec V2.1.1）、
  [docs/agent-evaluation-regression-platform-engineering-prd-v2.md](docs/agent-evaluation-regression-platform-engineering-prd-v2.md)（PRD V2.0.1）、
  [docs/web-evaluation-control-plane-design.md](docs/web-evaluation-control-plane-design.md)（Web 执行控制面）
- CLI 名称：`agent-eval`；当前能力面：
  **Offline**（Benchmark / Experiment / Regression / Stability / Quality Gate / Security /
  Failure Intelligence / Human Review / Promote）+
  **Execution Control Plane**（Web/CLI/Schedule/Trigger 统一经 EvalRunService 发起，
  Job 队列 / 取消 / SSE 实时进度 / Agent Connection Profile）+
  **Production**（Trace 摄取（平台事件与 OTel JSON）/ Trace Viewer / 参考无关 Online Eval /
  Eval Policy）+
  **Automation**（Preset / Cron Schedule / Webhook Trigger / Notification（webhook + 站内））。
  已知边界与后续主线见 `.trellis/tasks/ROADMAP.md` 与评估记录。

## 快速开始

```bash
uv sync                                # 安装依赖（judge 引擎：uv sync --extra judge）
uv run agent-eval benchmark run database-core --tag smoke  # 对内置 fake agent 跑通 smoke（无需真实 Agent）
```

评测定义位于 `evals/`（datasets/suites/benchmarks/profiles），夹具数据位于 `fixtures/`，
运行结果写入 `.agent-eval/runs/<run_id>/`。

对接真实 Agent 时指定 endpoint（HTTP + SSE，协议见 PRD §7/§8）：

```bash
export AGENT_EVAL_AGENT_ENDPOINT=http://127.0.0.1:8802
uv run python -m agent_eval.dev.mock_server --port 8802   # 内置 mock agent
uv run agent-eval benchmark run database-core --tag smoke
```

### 评价一个外部 Agent 平台

外部自研平台的接入形态是 **HTTP 直连 + 接入侧转译 shim**（平台方言在 shim 里
归一化为本平台的 PRD §8 事件词汇）。最小路径（详细契约与清单见
[docs/external-agent-integration-guide.md](docs/external-agent-integration-guide.md)）：

```bash
# 1. 起 shim：对上实现 /health、/api/agent/sessions、/run、/cancel 四端点，
#    在 /health 里上报观测面能力表（observation_surface）与实际生效模型
# 2. 配 profile：SUT 不提供的观测面（如 retry）经能力声明自动 skipped，
#    也可在 profile 里显式排除对应 metric
# 3. 跑 benchmark：
export AGENT_EVAL_AGENT_ENDPOINT=http://127.0.0.1:<shim-port>
uv run agent-eval benchmark run <benchmark> --no-judge      # 先跑确定性指标
#    开发期建议加 --strict-protocol，把 shim 的事件笔误在第一次就拦下
# 4. 读报告：.agent-eval/runs/<run_id>/report.json + report.html
#    （warnings 里不应有协议违规；被观测面跳过的 metric 会标 skipped 而非 pass）
```

现成的落地例子在本仓 `shims/ai-chatbot/`（转译 shim）与 `evals/datasets/chatbot-core/`
（该平台的专用评测集）。**专用评测集不可省**：通用 case 假设的工具名与 fixture
形状来自别的被测方，打在新 SUT 上会全红——而且是真失败，不是接入故障
（原因见 [change-plan §3](docs/external-agent-integration-change-plan.md)）。

写专用评测集时这三条最常踩（都有实测记录）：

- **工具名逐字取自 SUT 的 registry**，并冻结成一份名单（`tool-surface.yaml`）
  用测试核对：拼错的后果是 `required` 恒红（显性）或 `forbidden` 恒绿（隐性）。
  运行期拼装的名字（MCP / connector）只登记为占位符，绝不逐字点名。
- **负向用例必须真的红**，且不能恒绿。注意两个坑：断言落在 harness 指标上时，
  它所在的 profile 必须把该指标设为 `blocking`，否则 metric 判 fail 而 case 仍是
  PASS；"故意不可满足"的声明要真的不可满足（例如 `baseline_steps: 0` 配一个
  必须调工具的题面），否则会随实测值漂回恒绿。
- **会撞安全硬门的 canary 要单独一跑**（`--suite security`）。`security.max_failures: 0`
  是每档 gate 的 Hard Gate，一条故意违规的 case 留在默认 run 里会让那条硬门永远红，
  "规则被触发"与"撞线 canary"就分不清了。

## 开发

```bash
uv run pytest        # 测试
uv run ruff check .  # lint
```

CI（`.github/workflows/`）：PR 与 main 分支跑 lint + 全量测试 + 前端构建 +
fake agent 冒烟评测并执行 pr / main Gate；main 分支缓存 `.agent-eval` 数据目录
累积 main-latest baseline；nightly 跑 repeat=3 主门禁评测；release 为手动触发
（需显式 pin baseline run，符合 release Gate 的 fail-fast 契约）。
