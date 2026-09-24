# PRD §57 Semantic Trace Diff / SQL AST Diff / git diff

## 问题

PRD §57 要求 Tool Arguments 支持两种 diff：

```text
structural diff
semantic diff
```

并对特定类型给出手段：

```text
SQL 建议使用 SQLGlot 增加 AST Diff
文件修改支持 git diff
```

当前状态：`regression/trace_diff.py` **只有 structural diff**。

- `_flatten(value, prefix)` 把嵌套 dict/list 展平成"路径 → 值"映射
- `_argument_diffs()` 按扁平路径逐条比对，产出 `ArgumentDiff`
- `_lcs_ops()` 用 LCS 比对 span 序列

也就是说：`SELECT * FROM t WHERE id = 1` 与 `select * from t where id=1`
会被判成"参数变了"——语义相同、字面不同。SQL 场景下这类误报会直接吃掉
`native.argument_checks` 与 trace diff 的可用性。

## 范围

### 1. semantic diff（不引入 SQLGlot 的部分）

先明确"semantic"在这里指什么。合理的定义是**归一化后再比对**，至少包括：

- SQL：大小写、空白、标识符引号（`` `t` `` vs `"t"` vs `t`）
- JSON：键序无关（现有 `_flatten` 已按键路径比对，键序无关基本满足）
- 数字：`1` vs `1.0` vs `"1"`
- 路径：`./a/b` vs `a/b`、结尾斜杠

归一化规则必须**显式列出并测试**，不能是"看起来差不多就算一样"——
diff 的结论会进报告和 Gate，模糊的等价关系会让回归判定不可复现。

### 2. SQL AST diff（SQLGlot）

- 新增可选依赖（判断放 `judge` extra 还是新 extra，或 core 依赖）
- 用 SQLGlot parse 两侧 SQL → 比对 AST（可先做"归一化后 SQL 字符串相等"，
  再按需深入到 AST 级结构比对）
- parse 失败（非法 SQL）时的降级：**退回 structural diff 并标注**，
  不要静默判"不同"或判"相同"
- 方言问题：SQLGlot 需要 `dialect`（本平台 fixture 用的是 SQLite），
  方言必须来自 case 的 environment 声明，不能硬编码

### 3. git diff（文件修改）

文件类 fixture（`FilesystemFixture`）下，case 可能修改工作区文件。
PRD 要求支持 git diff——需要先确认：
**fixture 目录是不是 git 仓库？** 若不是，`git diff` 无从谈起，
要么给 fixture workdir 初始化 git，要么改用"文件快照前后比对"。
这个选择要在实现前定，并写入 prd 或 spec。

### 4. 与现有结构的关系

- `ArgumentDiff` 增加"diff 类型"字段（structural / semantic / ast）？
  还是新增独立的结果类型？倾向**在同一结果里标注类型**，
  这样报告侧不需要改两处渲染。
- 报告与 REST API 的呈现要能看到"这是语义级相同"，
  否则用户看到 diff 为空会以为丢数据了。

## 交付物

| 文件 | 改动 |
| --- | --- |
| `src/agent_eval/regression/trace_diff.py` | 归一化 + 语义比对 + 类型标注 |
| `src/agent_eval/regression/sql_diff.py`（新，可选） | SQLGlot 封装与降级 |
| `pyproject.toml` | 依赖声明 |
| `tests/test_trace_diff.py`（新） | 每条归一化规则一个用例 + parse 失败降级 |
| `reports/templates/report.html.j2`、`web/src/components/common/TraceTree.tsx` | 呈现 diff 类型 |
| `docs/agent-eval-engineering-spec-v2.1.md` | §57 的归一化规则写进契约 |

## 验收

```python
# 语义相同、字面不同 → 不判为差异
diff("SELECT * FROM t WHERE id = 1", "select * from t where id=1") == 无差异

# 语义不同 → 必须判为差异（不能因为归一化过头而漏判）
diff("SELECT * FROM t WHERE id = 1", "SELECT * FROM t WHERE id = 2") != 无差异
```

反例必须红：**归一化不能过度**。`id=1` 与 `id = 1` 相同，
但 `id=1` 与 `id like '%1%'` 必须不同。
每个归一化规则都要有一条"不该被归一化掉"的对照用例。

## 风险

归一化是"让差异变少"的手段，而漏判（false negative）在回归平台里比误报
（false positive）危险得多：误报会让人多点一次"确认"，漏判会让真实回归静默通过。
所以每条规则的默认态度是**宁可保留差异**，只在明确等价时才归一化。
