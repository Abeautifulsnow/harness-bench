# Quality Guidelines

> 质量门槛与编码约束（P0 落地时确立，随任务演进）。

---

## 检查命令（合并前必须全绿）

```bash
uv run pytest        # 141 tests（含 e2e：FakeAgent 全链路 + 真实 TCP mock server + REST API）
uv run ruff check .  # 规则：E/F/I/UP/B/SIM；CLI 文件豁免 B008（Typer 惯用法）
uv run ruff format --check .
cd web && bun run typecheck && bun run build   # 前端改动时
```

测试的 judge 隔离约定：`tests/conftest.py` 的 `deepeval_absent` 夹具默认让能力探针
报告"不可用"，使 native 评测路径的结论不取决于开发机是否装了 deepeval。需要真实
judge 行为的用例标 `@pytest.mark.real_judge` 退出该夹具。装了 deepeval 却没有模型
凭据时 judge 调用会失败并让 run 正确降级为 exit 2（Spec §6.1）——那是另一条语义路径，
由专门用例覆盖，不应把整批 native 用例一起染红。

## 编码约束

1. **Python 3.12+**：`StrEnum`（不用 `class X(str, Enum)`）、`asyncio.timeout`/`TaskGroup`、`X | None`。
2. **Pydantic v2**：可变默认一律 `Field(default_factory=...)`；YAML 宽松面用 `extra="allow"` + 显式 extensions 收集。
3. **异步**：每 iteration 完全隔离（session/fixture 均不共享）；`async for` 消费的生成器内不得让异常静默吞掉协议违约。
4. **禁止**：业务代码直接 import deepeval（只允许 `evaluators/deepeval_adapter.py`）；CLI 之外打印（输出走 rich console 或返回值）。
5. **注释**：只在代码无法自表达"契约约束"时写（引用 PRD/Spec 章节号）；不复述实现。
6. **测试**：网络层用 `httpx.MockTransport`；需要真实 socket 时用 `dev/mock_server.serve(port=0)`；
   非确定场景（FLAKY/错误序列）用 `FakeAgentAdapter.script_queue` + `concurrency=1`。
7. **数值语义**：`None` 与 `0` 是两种事实。PRD §59 规定无定价时 cost 为 `None`
   （不是 `0.0`），报告与 Web UI 都必须让二者显示得不一样——把 null 当 0 会让
   趋势图出现"成本降到零"的假象。
8. **派生层只读消费**：DuckDB 投影可随时 `rebuild()`，所以任何"查询前先建库"的
   便利写法都是错的（REST API 会因此把"未物化"伪装成"没有数据"）。只读消费方用
   `Analytics(read_only=True)`。

## 门禁约束（PRD §108，Spec §15）

"声明了却从不求值"是 Gate 层最严重的缺陷类别：它不让任何功能报错，只在该拦住的
时候放行。守住两条：

1. **Gate YAML 的每个字段都必须有求值点**。`GateRules` 解析出的声明如果没人读，
   就是装饰品——`suites` 曾在 YAML 里躺了整个 P4（`evaluate_gate()` 从不读它），
   导致"只跑 smoke 的 run"套上 release 规则也能 PASS。新增字段时同时给出 rule。
2. **套件覆盖只能读事实源，不能靠反推**。必跑套件的判定读
   `RunMetadata.suites_covered`（套件 → 选中 case 数），不从 case tags 反推，
   也不把 `0` 当成"跑过了"。`0` = 未覆盖 = FAIL：`security.max_failures: 0`
   在零个安全 case 时的平凡通过，与真正的"零失败"是两件不同的事。
3. **选择面与求值面必须成对**。`suites.coverage`（求值）之外还要有
   `resolve_suites()` 用 `benchmark.suites ∪ gate.suites` 扩宽实际选择（选择）；
   只做一半会分别得到"永远 FAIL"或"依然静默通过"。
4. `suites: []` 是"不约束"，不是"任何套件都不许跑"——反向语义会让 PR/Main
   两套 Gate 变成永远 FAIL。

## 安全断言约束（PRD §62/§63，Spec §12/§16）

安全层的规则不落在断言"能算"，而落在"观测面有没有接上"：

1. **观测面必须逐条透传**。`forbidden_mcp` 曾因 runner 不传 `mcp_names` 而**恒 pass**
   （`mcp` 永远是空列表）；`forbidden_command` 也看不到 `command.*` 事件。
   现在 `EvalScope` / `TurnResult` / `CaseRunResult` 都带 `tool_calls` /
   `mcp_calls` / `command_calls` 三个并列观测面，Runner 逐轮收集。
   新增安全规则时必须先确认它的观测面在事件流里有来源。
2. **判定对象是行为，不是文本**。只有 `secret_patterns` 读最终输出，方向是
   "检测泄漏"；路径与提权标记只读工具参数。把两者混在一起会让"解释为什么
   不做 rm -rf"的合规 Agent 被判违规。
3. **命中可红**。每条规则都要有一条"违规行为 → FAIL"的实测用例；
   只有负向 case 时"什么都拒"的 Agent 也能全过，所以正向 case 同样是必需的。
4. **脱敏按 Spec §12.3**：回显前 4 字符 + `***`。推论：**测试源码里不得出现完整
   密钥字面量**（用片段拼接），否则密钥扫描会把测试本身报成泄漏事件。
5. **安全脚本单一实现**：`adapters/fake.py` 的 `SECURITY_RULES` / `turn_events()`
   是唯一脚本源，`dev/mock_server.py` 复用它们——安全用例最不能容忍两份实现漂移。

## 断言有效性不变式（2026-09-23 review #I01–#I04 的教训）

评测平台最严重的缺陷类别是"声明了却不算数的断言"。守住三条：

1. **全部挂载点参与判决**：case 级 `expected`、session 级 `expected.final`、turn 级
   `expect` 的判定都必须进入 `CaseRunResult.all_metric_results`，由 `blocking_failed`
   统一消费。新增挂载点时必须同时接进这个聚合视图。
2. **没有观测来源就不得评测**：观测切片 `EvalScope` 只放真正能观测到的量。
   词汇表内但尚无来源的声明（`exit_code`、`max_cost`）必须在启动期
   `scan_unsupported_assertions` fail-fast（exit 3），绝不允许落进
   "默认值恒 pass / 恒 fail" 的分支——两者都会污染 Gate 结论。
3. **新增断言词汇的步骤**：先在 `native.py` 实现 checker + 加入
   `IMPLEMENTED_EXTENSIONS`/约束实现面，再补 `unsupported_declarations` 的放行，
   最后补正向+边界测试。顺序反了就会复现"声明了却静默失效"。
4. **不要发明隐式基线**：需要"理想值"的连续型断言（如 `step_efficiency`）必须由
   Case 显式声明基线。从 `tools.required` / `constraints.max_tool_calls` 反推理想步数
   会把"必须调用"读成"只应调用"，误伤多轮 case。判据与算式在 Spec §11。

## 源文件行尾（Windows 环境硬约束）

源码必须是 **LF**。本机 ruff 与文件读取工具都会拒绝 CRLF 的 `.py` 文件
（报 `E902 stream did not contain valid UTF-8`，且 Read 工具无法打开）。
凡是经过 python `write_text()` 重写的文件会变成 CRLF——因此：
- **禁止**用 Bash 里的 python 脚本改写源码文件，一律走 Write/Edit 工具；
- 写入后若 ruff 报 E902，先查行尾（`file` 或统计 `\r\n`），删文件用 Write 重建；
- 提交前 `uv run ruff format --check . && uv run ruff check .` 必须双绿
  （`docs/` 已从 ruff 范围排除：内嵌 Python 片段是契约文本，不是待格式化代码）。
- 同一条规则适用于 `web/`：`.tsx` 源码同样必须 LF。用 Bash 的 python 脚本改写
  前端文件会复现同一个损坏（Read/tsc 都会失败），改前端也走 Write/Edit。

## 本机构建怪癖（重要）

uv 的通用 PEP 517 桥接子进程在本机损坏（`stream did not contain valid UTF-8`）。
pyproject 已配置 `uv_build` 原生构建后端绕开。**不要**把 build-backend 改回 hatchling/setuptools。

## 示例数据集约定

`evals/` 下的 smoke suite 必须对 fake:// 与 mock server 保持确定性 PASS（作为回归基线）；
负向用例（`negative` tag）只允许进入 core suite，用于演示 exit 1 路径。
