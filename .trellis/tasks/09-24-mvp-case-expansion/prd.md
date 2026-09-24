# MVP 用例扩充至 20~30 并补齐覆盖维度

## 问题

PRD §103 要求：

```text
至少 20~30 Cases
覆盖：Tool / Database / Skill / MCP / Context / Error Recovery
```

当前实际：`evals/datasets/database-core/cases/` 下 **6 个** case：

| case | tags |
| --- | --- |
| `smoke.echo.basic` | smoke |
| `database.query.top_customers` | database, smoke, core, tool-use |
| `database.query.multi_turn_refine` | database, smoke, multi-turn |
| `database.error.recovery` | database, core, negative |
| `database.guard.forbidden_shell` | database, core, negative |
| `agent.subagent.routing` | database, core, subagent |

六类覆盖维度里，Tool / Database / Error Recovery 有，
**Skill / MCP / Context 三类一条都没有**。

后果不只是"数量不足"：`evals/gates/*.yaml` 里的阈值规则依赖 case 集合的统计意义。
`task_success.max_regression_percent: 1` 在 6 个 case 上的分辨力极低——
一个 case 翻转就是 16.7% 的回归幅度，阈值形同虚设。

## 范围

补 case 数据，不改评测引擎、不改 runner。

### 1. 维度覆盖（PRD §103）

按维度各补若干 case，每一维至少 2~3 条，凑到 20~30：

- **Tool**：已有 `tool-use` 基础，补多工具串联、工具选择错误恢复
- **Database**：已有 3 条，补写入类、事务类、只读约束类
- **Skill**：**全新** —— skill 优先级、skill 加载失败降级
- **MCP**：**全新** —— MCP 调用成功、MCP 权限越界（与 `security-cases` 交叉）
- **Context**：**全新** —— 上下文压缩后的信息保留、（若适用）冲突消解
- **Error Recovery**：已有 1 条，补重试成功、重试耗尽、超时恢复

### 2. 与 `harness-evaluators` 的依赖关系

Skill / MCP / Context 这三类**当前的判定手段有限**：

- `native.py` 已实现 `status` / `tool_arguments` / `step_efficiency` 三个扩展
- 其余 9 个扩展（含可能需要的 `permission`）抛 `UnsupportedAssertionError`
- PRD §44 的 14 个 harness-specific evaluator 一个都还没有

所以这三类 case 现在只能用 `output_checks` / `tool_sequence` / `tool_arguments`
这类已有能力去断言。**先确认能不能写**：

- 能写 → 本任务补齐，并在 prd 里说明用了哪些已有能力
- 不能写 → **不要**为了凑数写出断言不到位的 case（那会制造"看起来在测、实际没测"的假覆盖）。
  把缺口如实记在本任务 notes 里，并在 `harness-evaluators` /
  `assertion-extensions` 里补齐对应能力后再回来加 case。

这条是本任务的核心判断点：**宁可数量不到 20，也不要造不断言的 case。**

### 3. Fixture 依赖

新 case 可能引入新的 `environment` 需求（数据库类型、文件系统布局）。
`fixtures/` 下目前只有 `sales_v2/seed.sql`，且 `get_provider()` 只实现
`filesystem` 与 `sqlite`（postgres/git 显式 raise planned）。
新 case 必须落在**已实现**的 fixture 能力内，否则会以 infra error 收场。

## 交付物

| 文件 | 改动 |
| --- | --- |
| `evals/datasets/<ds>/cases/*.yaml` | 新增 case 至 20~30 |
| `evals/datasets/<ds>/dataset.yaml` | 若新增 case 目录需同步索引 |
| `fixtures/` | 若需新 fixture 种子数据 |
| `evals/suites/*.yaml` | 给新 case 打上 `golden` / `core` 等 tag，供 gate 套件选取 |
| `tests/` | 若新增 dimension 的断言路径需要测试保护 |

## 验收

```bash
uv run agent-eval benchmark run core       # 全量 case 跑过，无 infra error
# 六个维度都有 case：按 tag 统计
uv run agent-eval case list                # 数量落在 20~30
```

反例必须红：若某类 case 只能靠"输出里出现某个词"来断言，那它不构成该维度的覆盖，
应在 prd 里标注为**受限覆盖**而不是计入维度达标。

## 依赖

- `harness-evaluators` / `assertion-extensions`：Skill / MCP / Context 断言的判定能力
- `security-cases`：MCP 越界类 case 与安全套件共享
- `release-gate-suites`：`golden` tag 的 case 来源
