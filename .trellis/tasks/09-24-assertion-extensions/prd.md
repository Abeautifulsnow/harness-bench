# 补齐已声明但未求值的 Assertion Extension

## 问题

`Assertion.extensions` 是 PRD / Spec §2.2 的断言词汇表。当前实现：

```python
# src/agent_eval/evaluators/native.py:31
IMPLEMENTED_EXTENSIONS = {"status", "tool_arguments", "step_efficiency"}
KNOWN_EXTENSIONS = EXTENSION_KEYS - IMPLEMENTED_EXTENSIONS
```

`EXTENSION_KEYS`（`models/case.py:22`）共 12 个，**9 个只声明未实现**：

```text
exit_code   database_state   file_state   git_diff   pytest
build       lint             sql_result   permission
```

遇到这 9 个键时，`_check_extensions()` 会产出一条
"在词汇表内但尚未实现"的 problem，`unsupported_declarations()` 也让
`agent-eval case validate` 能提前报出来。**这个 fail-fast 行为是对的**
（Spec §2.2 的"不静默忽略"），但它是**安全网**，不是**交付**。

另外一条相关缺口：`ConstraintAssertion.max_cost` 也在
`unsupported_declarations()` 里被显式标注"尚未实现（PRD §59 cost 属 P2）"，
而 P2（成本分析）其实已经做完了（`reports/cost.py`、DuckDB 的 cost 投影、
REST `/api/cost` 都在）。**这条提示已经过期**，需要重新评估：
`max_cost` 是否可以直接实现？如果能，本任务一并做掉。

## 范围

逐个实现，**每个都先问"观测数据从哪来"**。

### 1. 逐键评估表（实现前必须填）

| 键 | 观测来源是否已存在 | 判断 |
| --- | --- | --- |
| `exit_code` | `scope.run_status` 已有；但"退出码"是 Agent 的还是工具的？ | 需澄清语义 |
| `database_state` | fixture 层（SQLiteFixture） | 依赖 `case-artifacts` 的快照能力 |
| `file_state` | fixture workdir | 同上 |
| `git_diff` | 需 fixture 是 git 仓库 | 与 `semantic-trace-diff` 交叉 |
| `pytest` | 需在 fixture 里跑 pytest | 需要执行能力，不只是观测 |
| `build` | 同上 | 同上 |
| `lint` | 同上 | 同上 |
| `sql_result` | trace 里的 SQL 工具返回 | 可能已有 |
| `permission` | `security` 挂载点已覆盖 permission_override | **可能重复**，需判断是否仍需要 |

这张表要在动手前填完。**填不出来（说不清观测来源）的键不要实现**，
而是把结论写进 prd/notes：要么它不该在这个词汇表里（应当移除），
要么它依赖别的任务。

### 2. 关键判断点

- **`pytest` / `build` / `lint` 是执行型断言，不是观测型断言。**
  它们要求在 fixture 环境里运行命令并取结果。这引入了新的能力
  （在受控环境里执行命令、超时、输出捕获），与 PRD §88 的
  "Coding / 高风险脚本场景后续使用 Docker / gVisor / WASM Sandbox"直接相关。
  **本任务不应顺手实现一个不受限的"在 fixture 里跑任意命令"**——
  那是安全面扩张，需要单独评估（见风险）。
- **`permission` 可能与 `security.permission_override` 重复。**
  若确认重复，正确处理是**从词汇表移除 `permission`**（并更新 Spec 与测试），
  而不是实现两套语义。
- **`exit_code` 的语义要先定。** Agent 的 HTTP 会话没有"退出码"这个东西；
  若指的是"工具/命令的退出码"，那它在 tool call 结果里，与 `sql_result` 同类。

### 3. 每个实现的键都要双向测试

- 断言不满足 → FAIL（且是 blocking 还是非 blocking 要与声明一致）
- 断言满足 → PASS
- 声明形状非法 → 明确报错，不静默通过

### 4. 词汇表与 Spec 同步

实现或移除之后，`EXTENSION_KEYS`、`IMPLEMENTED_EXTENSIONS`、
`unsupported_declarations()` 的提示文案、
以及 Spec §2.2 的词汇表**必须三者一致**。
不一致会让 `case validate` 的输出与实际能力脱节。

## 交付物

| 文件 | 改动 |
| --- | --- |
| `src/agent_eval/evaluators/native.py` | 逐键的 `_check_*` 实现 |
| `src/agent_eval/models/case.py` | `EXTENSION_KEYS` 调整（若移除某些键） |
| `src/agent_eval/evaluators/native.py` | `unsupported_declarations()` 文案同步；`max_cost` 复审 |
| `tests/test_native_eval.py` | 每键双向用例 |
| `docs/agent-eval-engineering-spec-v2.1.md` | §2.2 词汇表与实现状态一致 |

## 验收

```bash
uv run agent-eval case validate        # 不再有"在词汇表内但尚未实现"的输出
                                       # （要么已实现，要么已从词汇表移除）
```

反例必须红：对一个**未实现且未移除**的键声明断言，必须仍然报错（不能静默忽略）。
如果本任务把某个键移除了，那声明它应当报"不在断言词汇表内（Spec §2.2）"。

## 风险

`pytest` / `build` / `lint` 的实现等于给评测平台加"在 fixture 里执行代码"的能力。
这与 PRD §88 的沙箱规划冲突：当前 V1 只在 local 环境执行，
没有任何隔离。**在沙箱落地前实现这三个键，等于把任意代码执行引入评测流程。**
建议：本任务实现纯观测型的键（exit_code / database_state / file_state /
sql_result / git_diff / permission 的处置），把执行型的三个
（pytest / build / lint）**显式列为一个独立任务**，并标注依赖 PRD §88 的沙箱。
