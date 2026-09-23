# harness-bench — Agent Evaluation & Regression Platform

Agent 工程质量平台：Benchmark / Experiment / Evaluation / Regression / Failure Intelligence / Quality Gate。

- 契约文档：[docs/agent-eval-engineering-spec-v2.1.md](docs/agent-eval-engineering-spec-v2.1.md)（Spec V2.1.1）、
  [docs/agent-evaluation-regression-platform-engineering-prd-v2.md](docs/agent-evaluation-regression-platform-engineering-prd-v2.md)（PRD V2.0.1）
- CLI 名称：`agent-eval`；当前进度：P0 核心执行链路（Agent → Trace → Eval → Result）

## 快速开始

```bash
uv sync                                # 安装依赖（judge 引擎：uv sync --extra judge）
uv run agent-eval benchmark run smoke  # 对内置 fake agent 跑通 smoke（无需真实 Agent）
```

评测定义位于 `evals/`（datasets/suites/benchmarks/profiles），夹具数据位于 `fixtures/`，
运行结果写入 `.agent-eval/runs/<run_id>/`。

对接真实 Agent 时指定 endpoint（HTTP + SSE，协议见 PRD §7/§8）：

```bash
export AGENT_EVAL_AGENT_ENDPOINT=http://127.0.0.1:8802
uv run python -m agent_eval.dev.mock_server --port 8802   # 内置 mock agent
uv run agent-eval benchmark run smoke
```

## 开发

```bash
uv run pytest        # 测试
uv run ruff check .  # lint
```

## GitLab 仓库

远端：`http://192.100.30.115:9000/siact/tools/harness-bench.git`（main）
