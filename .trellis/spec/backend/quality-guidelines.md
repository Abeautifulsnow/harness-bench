# Quality Guidelines

> 质量门槛与编码约束（P0 落地时确立，随任务演进）。

---

## 检查命令（合并前必须全绿）

```bash
uv run pytest        # 60 tests（含 e2e：FakeAgent 全链路 + 真实 TCP mock server）
uv run ruff check .  # 规则：E/F/I/UP/B/SIM；CLI 文件豁免 B008（Typer 惯用法）
```

## 编码约束

1. **Python 3.12+**：`StrEnum`（不用 `class X(str, Enum)`）、`asyncio.timeout`/`TaskGroup`、`X | None`。
2. **Pydantic v2**：可变默认一律 `Field(default_factory=...)`；YAML 宽松面用 `extra="allow"` + 显式 extensions 收集。
3. **异步**：每 iteration 完全隔离（session/fixture 均不共享）；`async for` 消费的生成器内不得让异常静默吞掉协议违约。
4. **禁止**：业务代码直接 import deepeval（只允许 `evaluators/deepeval_adapter.py`）；CLI 之外打印（输出走 rich console 或返回值）。
5. **注释**：只在代码无法自表达"契约约束"时写（引用 PRD/Spec 章节号）；不复述实现。
6. **测试**：网络层用 `httpx.MockTransport`；需要真实 socket 时用 `dev/mock_server.serve(port=0)`；
   非确定场景（FLAKY/错误序列）用 `FakeAgentAdapter.script_queue` + `concurrency=1`。

## 断言有效性不变式（2026-09-23 review #I01–#I04 的教训）

评测平台最严重的缺陷类别是"声明了却不算数的断言"。守住三条：

1. **全部挂载点参与判决**：case 级 `expected`、session 级 `expected.final`、turn 级
   `expect` 的判定都必须进入 `CaseRunResult.all_metric_results`，由 `blocking_failed`
   统一消费。新增挂载点时必须同时接进这个聚合视图。
2. **没有观测来源就不得评测**：观测切片 `EvalScope` 只放真正能观测到的量。
   词汇表内但 P0 无来源的声明（`exit_code`、`max_cost`）必须在启动期
   `scan_unsupported_assertions` fail-fast（exit 3），绝不允许落进
   "默认值恒 pass / 恒 fail" 的分支——两者都会污染 Gate 结论。
3. **新增断言词汇的步骤**：先在 `native.py` 实现 checker + 加入
   `IMPLEMENTED_EXTENSIONS`/约束实现面，再补 `unsupported_declarations` 的放行，
   最后补正向+边界测试。顺序反了就会复现"声明了却静默失效"。

## 源文件行尾（Windows 环境硬约束）

源码必须是 **LF**。本机 ruff 与文件读取工具都会拒绝 CRLF 的 `.py` 文件
（报 `E902 stream did not contain valid UTF-8`，且 Read 工具无法打开）。
凡是经过 python `write_text()` 重写的文件会变成 CRLF——因此：
- **禁止**用 Bash 里的 python 脚本改写源码文件，一律走 Write/Edit 工具；
- 写入后若 ruff 报 E902，先查行尾（`file` 或统计 `\r\n`），删文件用 Write 重建；
- 提交前 `uv run ruff format --check . && uv run ruff check .` 必须双绿
  （`docs/` 已从 ruff 范围排除：内嵌 Python 片段是契约文本，不是待格式化代码）。

## 本机构建怪癖（重要）

uv 的通用 PEP 517 桥接子进程在本机损坏（`stream did not contain valid UTF-8`）。
pyproject 已配置 `uv_build` 原生构建后端绕开。**不要**把 build-backend 改回 hatchling/setuptools。

## 示例数据集约定

`evals/` 下的 smoke suite 必须对 fake:// 与 mock server 保持确定性 PASS（作为回归基线）；
负向用例（`negative` tag）只允许进入 core suite，用于演示 exit 1 路径。
