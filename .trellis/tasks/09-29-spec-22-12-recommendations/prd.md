# Spec §22.12 两项建议项收口：定义层解析开销 + junit skipped 覆盖

## 来源

Spec §22.12 末尾记了两处**未修**的建议项，原文：

```text
另记录两处未修的建议项，留作独立增量：/api/suites 的计数现每次请求全量装载
dataset（case 数上千时再议缓存）；junit skipped 分支仍只有单测覆盖，没有真实
fixture 能让一条 case 的全部 metric 都 skipped——补 fixture 要动套件组成，不应
顺手做。
```

本次把两项一起收口。测量与判定依据记在下方（结论修正了原记录的一处理由，见 §2）。

---

## 1. 定义层解析开销（原："case 上千时再议缓存"）

### 现状实测（本机，40 条 case 的 `database-core`）

| 度量 | 数值 |
| --- | --- |
| `load_dataset`（40 case + dataset.yaml） | 中位 **62ms** |
| `GET /api/suites` | 中位 **249ms**，解析 **88 个** YAML 文件 |
| `GET /api/benchmarks` | 181ms / 42 次 |
| `GET /api/cases` | 100ms / 41 次 |

`load_dataset` 的 62ms 拆解（同一组文件反复测）：

```text
yaml.safe_load（纯 Python 解析器）   40.1ms   ← 82%
读文件                                4.6ms
Case.model_validate                   0.4ms
```

而本机 PyYAML 的 **libyaml 可用**（`yaml.CSafeLoader`）：

```text
yaml.load(..., Loader=CSafeLoader)    4.4ms   ← 约 9 倍
```

`/api/suites` 的 88 次里，**41 次来自同一请求内的重复装载**：

- `list_suite_rows`（`api/routers/catalog.py`）自己遍历 dataset 选 case：41 次；
- `security.suites.list_suites(root)` 把**同一个 dataset 又完整读一遍**：41 次；
- 剩下 6 次是 `suites/*.yaml`。

### 范围

1. **换解析器**：`loading/loader.py::_read_yaml` 改用 libyaml，带回退
   （`CSafeLoader` 不可用时用 `SafeLoader`）。行为不变——同一个 `safe_load`
   语义，只换 C 实现。
2. **去掉同一请求内的重复装载**：`security.suites.list_suites` 接收已经装好的
   case 列表（按 dataset 分组），不再自己去 `load_dataset`。

### 不做缓存，以及为什么

缓存留作**不做**，理由是契约冲突而非性能不够：

- Spec §13 写的契约是"定义层是事实：直接读 `evals/` 文件树，不经过 DuckDB"。
  TTL 缓存会让刚改完的 case 定义在页面上不更新——那是**把事实层变成近似层**，
  与"UI 不会整体变空"同一段契约冲突。
- 做完 1+2，实测 `GET /api/suites` 从 249ms 降到约 88ms；按 1000 条 case 线性外推
  约 240ms 量级，只读接口够用。等真的不够，正确的下一步是**内容哈希 / (mtime,size)
  为键的记忆化**（`load_dataset` 本来就在算 sha256），而不是 TTL——但那要等触发条件
  成立，不在本次范围。

### 验收

- `/api/suites`、`/api/cases`、`/api/benchmarks`、`/api/datasets` 响应完全不变
  （已有测试断言这些端点的形状；另补一条"同一请求内不重复装载"的护栏测试）。
- 全量 pytest 绿；ruff 绿。
- 实测耗时回填到 Spec §22.12 的关账条目。

---

## 2. junit skipped 分支的真实覆盖

### 原记录的理由站不住

原文写"补 fixture 要动套件组成"。实测**不需要**：仓库里已有现成范式，
在**拷贝出来的临时 `evals/` 树**里新写 dataset / benchmark / suite / profile
即可，完全不碰 `database-core` 的组成（`tests/test_harness_evaluators.py::
_harness_cfg`、`tests/test_case_coverage.py::TestMetricParamsResolution` 都是
这么做的）。

### 已确认的两条端到端可达路径

两条都用真实 `Runner` 跑通，落盘 `junit.xml` 真的写出 `<skipped>`：

1. **声明侧全 skipped**：case 不声明任何 `expected` 块 + profile 只挂
   `harness.skill_load` / `harness.mcp_permission` 这类"未声明参数即 skipped"
   的插件（`evaluators/harness.py`）→ 所有 metric `skipped` →
   `evaluated_metrics == 0`。
2. **观测面不可用**：case 只声明 `constraints.max_cost` 而不提供
   `evals/pricing.yaml` → `native.performance` 走 `ObservationUnavailable`
   判 skipped（`evaluators/native.py`）。

两条都落到 `case_status_for_junit() == "skipped"`，`gate.json` 的 aggregate 记
`skipped: 1`。

### 顺带暴露的问题（本次一并处置）

在全 skipped 的 run 上：`report.json` 的 `verdict` 是 `pass`、`warnings` 里
**只有 baseline 那条**（没有任何"什么都没判"的提示）、`gate.json` 的 `verdict`
也是 `pass`。即：**"零验证"的 run 与"全量验证通过"的 run 在门禁结论与 CI 日志上
完全同形**，只有去读 junit 的 `skipped` 属性才分得出来。这正是 Spec §19.1.1 与
§22 整轮在修的"假绿"类别，还留着这一处。

**本次的处置口径（保守选项，不动门禁结论）**：

- 加一条 run 级 warning：存在 `is_unjudged` 的 case 时，`warnings` 里指名道姓
  写出条数与 case id，让"零验证"在报告与 summary.md 上可见；
- **不改 gate rule、不改 exit code**。让全 skipped 变红是判定口径变更（会影响
  既有 CI 的绿/红分布），应由独立增量拍板，不在补覆盖时顺手改。
- 测试钉住：全 skipped 的 run 仍判 `pass`（当前口径）+ warning 存在。

---

## 范围外

- 定义层缓存的实现（见 §1"不做缓存"）。
- 全 skipped 是否应让 Gate 变红（口径变更，独立决策）。
- Spec §22.12 另记的 `/api/suites` 之外端点未逐一测量——本次只把量到的事实回填。

## 交付物

| # | 文件 | 内容 |
| --- | --- | --- |
| 1 | `src/agent_eval/loading/loader.py` | `_read_yaml` 用 libyaml，带回退 |
| 2 | `src/agent_eval/security/suites.py` | `list_suites` 接收已装好的 cases，不再重复装载 |
| 3 | `src/agent_eval/api/routers/catalog.py` | 配合 2 传参 |
| 4 | `src/agent_eval/reports/aggregate.py` | 全 skipped → run 级 warning |
| 5 | `tests/test_loaders.py`（或新文件） | 解析器一致性 + 不重复装载护栏 |
| 6 | `tests/test_gate_fidelity.py`（或新文件） | junit skipped 端到端（真实 Runner + 临时树） |
| 7 | `docs/agent-eval-engineering-spec-v2.1.md` | §22.12 两项关账 + 新增 §25 口径 |
| 8 | `.trellis/tasks/ROADMAP.md` | 关账记录 |
