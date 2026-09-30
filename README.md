# harness-bench — Agent Evaluation & Regression Platform

Agent 工程质量平台：Benchmark / Experiment / Evaluation / Regression / Failure Intelligence / Quality Gate。

- 契约文档：[docs/agent-eval-engineering-spec-v2.1.md](docs/agent-eval-engineering-spec-v2.1.md)（Spec V2.1.1）、
  [docs/agent-evaluation-regression-platform-engineering-prd-v2.md](docs/agent-evaluation-regression-platform-engineering-prd-v2.md)（PRD V2.0.1）
- CLI 名称：`agent-eval`；当前进度：P0 核心执行链路（Agent → Trace → Eval → Result）

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

## 开发

```bash
uv run pytest        # 测试
uv run ruff check .  # lint
```

## GitLab 仓库

远端：`http://192.100.30.115:9000/siact/tools/harness-bench.git`（main）
