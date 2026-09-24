# Release Gate 必跑套件强制校验 + 补齐套件定义

## 问题

PRD §108 的验收是"Release 执行 Golden / Regression / Security / Core Business 四类套件，
任一 Hard Gate 失败 → Release Gate = FAIL"。当前这条验收无法被真正验证，有两处断链：

1. **`suites` 字段是装饰性的。** `evals/gates/release.yaml` 写了
   `suites: [golden, regression, security, core]`，`GateRules.suites` 也解析了它
   （`src/agent_eval/quality/gates.py:49`），但 `evaluate_gate()` 全程没有读它。
   也就是说：跑一个只含 smoke 的 run，用 release 规则集判 Gate，`suites` 声明
   完全落空，Gate 照样能给 PASS。
2. **三个套件文件不存在。** `evals/suites/` 下只有 `smoke.yaml` 与 `core.yaml`。
   `golden`、`regression`、`security` 三个名字只出现在 gate YAML 里，
   不是可被 `load_suites()` 解析的定义。

后果：Release Gate 现在等于"把当前 run 的结果套一遍阈值"，而不是"确认四类套件都跑过了"。
一个漏跑安全套件的 release 会静默放行。

## 范围

只做"套件覆盖"这一层校验，不碰阈值语义（阈值已在 `_absolute_rules` / `_baseline_rules` 里正确实现）。

### 1. `GateRules.suites` 变成可判定的约束

在 `evaluate_gate()` 里新增一条 rule：`gate.required_suites`（或按现有命名风格
`suites.coverage`）。判定口径需要明确：

- run 实际覆盖的套件从哪来？`RunMetadata` 里已有 benchmark / suites 信息（`load_benchmark`
  会把 `benchmark.suites` 带进 loader），但**run 结果里是否记录了"本次跑了哪些 suite"
  需要先确认**。如果没有，这是本任务的第一件事：把跑过的 suite 名字写进 run.json。
  没有这个事实源，"必跑套件"就只能靠推断（比如从 case tags 反推），那是脆的。
- 缺哪个套件 → rule = fail，`affected` 列出缺失的套件名，`detail` 说明这是 PRD §108 的硬约束。
- `suites: []`（PR/Main Gate 的现状）→ rule 不产出，即"没有必跑列表"。
  这个语义要在 PRD 里写清楚：空列表 = 不约束，而不是"任何套件都不许跑"。

### 2. 补齐三个套件定义

- `evals/suites/golden.yaml`
- `evals/suites/regression.yaml`
- `evals/suites/security.yaml`（tag 指向 `security`）

套件定义格式已定：`{name, tags[], case_ids[]}`（`loader.py:7`）。这三个文件按 tag 选 case。

**注意：** 这三个套件能否选出 case，取决于 case 的 tag 现状。目前
`evals/datasets/database-core/cases/` 下 6 个 case 的 tag 只有
`database/core/smoke/negative/multi-turn/tool-use/subagent/hard/medium/easy` 这几种，
没有 `golden`，也没有 `security`。所以本任务会**暴露**对 `security-cases` 与
`mvp-case-expansion` 两个任务的依赖：套件文件先建，case 由那两个任务补。
若本任务单独落地时套件为空，要让它**显式报空**（"security 套件 0 个 case"），
而不是靠"没 case 就没失败"平凡通过——这正是当前 gate 行为最危险的地方。

## 交付物

| 文件 | 改动 |
| --- | --- |
| `src/agent_eval/quality/gates.py` | 新增必跑套件 rule；空列表语义 |
| `src/agent_eval/models/run.py` | run.json 记录本次覆盖的 suites（若尚无） |
| `src/agent_eval/runner/runner.py` | 写入覆盖套件；把 suites 传给 `evaluate_gate` |
| `evals/suites/{golden,regression,security}.yaml` | 新增三个套件定义 |
| `evals/gates/release.yaml` | 已声明，确认断言可执行 |
| `tests/test_gates.py` | 覆盖：缺套件 → fail；`suites: []` → 不产出该 rule；套件空 → 显式报空 |

## 验收

```bash
# 只跑 smoke，用 release 规则集判 Gate → 必须 FAIL，并指出缺 golden/regression/security
uv run agent-eval run --benchmark database-core --suite smoke --gate release
uv run python -c "..."   # 或经 gate.json 断言 suites rule 的 verdict

# 四类套件都跑 → 该 rule PASS
```

反例必须红：把 `release.yaml` 的 `suites` 删掉后，该 rule 消失而不是变成 PASS。

## 依赖

- 与 `security-cases`（提供 security tag 的 case）、`mvp-case-expansion`（提供 golden tag）
  有数据依赖，但本任务的代码与校验逻辑可独立完成并测试。
- spec 需同步：`.trellis/spec/backend/quality-guidelines.md` 增加"必跑套件"约定；
  `docs/agent-eval-engineering-spec-v2.1.md` 的 Gate 章节补 `suites` 判定口径
  （归入 Spec V2.3 或 V2.2 的后续修订）。
