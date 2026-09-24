# 缺口任务清单（P0–P5 交付后的遗留）

> 生成于 2026-09-24。基线：`main` @ `24e3f1a`，103 pytest 通过、ruff 干净、
> 前端 typecheck/build 通过。这里列的是**已知未完成**，不是待办灵感。

单项细节见各自的 `task.json` 与 `prd.md` 目录。判定依据是 PRD（V2.0.1）与
Spec（V2.2）的验收条款，不是"感觉还差点"。

---

## 优先级视图

| 任务 | 优先级 | 性质 | 阻塞了什么 |
| --- | --- | --- | --- |
| [`release-gate-suites`](./09-24-release-gate-suites/prd.md) | P0 | 代码 + 数据 | PRD §108 的 Release Gate 验收无法真正验证 |
| [`security-cases`](./09-24-security-cases/prd.md) | P0 | 数据 + 疑似引擎缺陷 | 安全 Hard Gate 空转；可能暴露 MCP 规则失效 |
| [`mvp-case-expansion`](./09-24-mvp-case-expansion/prd.md) | P1 | 数据 | PRD §103 用例量与 Skill/MCP/Context 覆盖 |
| [`harness-evaluators`](./09-24-harness-evaluators/prd.md) | P1 | 代码 | PRD §44 的 14 个 evaluator；Skill/MCP/Context 断言能力 |
| [`evaluator-plugin-sdk`](./09-24-evaluator-plugin-sdk/prd.md) | P2 | 代码（契约） | PRD §43/§109.4 的对外扩展点 |
| [`semantic-trace-diff`](./09-24-semantic-trace-diff/prd.md) | P2 | 代码 | PRD §57 的 semantic / SQL AST diff |
| [`case-artifacts`](./09-24-case-artifacts/prd.md) | P2 | 代码 + 契约 | PRD §89 snapshot + §90 case 级产物 |
| [`assertion-extensions`](./09-24-assertion-extensions/prd.md) | P2 | 代码 | 9 个已声明未求值的断言扩展键 |
| [`case-scheduler`](./09-24-case-scheduler/prd.md) | P3 | 纯重构 | 无。PRD §85 的形状，不是功能 |

---

## 依赖图

```text
release-gate-suites ──┐
                      ├──> security-cases ──────┐
                      │        │                │
                      │        └─(暴露引擎缺陷)─┤
                      │                         │
mvp-case-expansion ───┴──────────────────────┐  │
        │                                     │  │
        └──> harness-evaluators <──> evaluator-plugin-sdk
                     │
                     └──> assertion-extensions

case-artifacts ──> assertion-extensions（database_state / file_state）
semantic-trace-diff ──> case-artifacts（git diff 采集）

case-scheduler（独立，可一直不做）
```

`harness-evaluators` 与 `evaluator-plugin-sdk` 是**互为先后**的关系：
写 2~3 个真实 evaluator 再抽 SDK，抽象会比反过来准确；先定 SDK 则至少要
拿一个真实 evaluator 当验证用例。不要试图一次成型。

---

## 为什么按这个顺序

**P0 的两条是"门禁失真"，不是"功能缺失"。** 它们不会让任何功能报错，
只会让 Gate 在该拦住的时候放行——这类缺陷不会自己暴露，
所以优先级高于所有"看得见的功能"。

具体地：

1. `release-gate-suites`：`evals/gates/release.yaml` 里写了
   `suites: [golden, regression, security, core]`，但 `evaluate_gate()`
   从不读它，且 `evals/suites/` 下三个套件文件根本不存在。
   现在一个只跑了 smoke 的 run 也能拿到 Release PASS。
2. `security-cases`：安全评测引擎是完整的（7 条确定性规则、脱敏、
   不可被 judge 覆盖），但**零条 case 用它**。于是
   `security.max_failures: 0` 以"0 个 case、0 个失败"平凡通过。

**P1 是体量与能力。** `mvp-case-expansion` 的 Skill / MCP / Context 三类
在 `harness-evaluators` 落地前**写不出有意义的断言**——这两个任务必须先
想清楚"拿什么断言"，再动手加 case。宁可数量不到 20，也不要造只会
"输出里出现某个词"就算过的假覆盖。

**P2 是扩展面。** 每一项都独立可用，不影响现有验收。

**P3 可以永远不做。** `case-scheduler` 是纯重构：PRD §85 要求的五项控制
（Queue/Concurrency/Timeout/Cancellation/Retry）**功能上都在**，
只是以内联 `Semaphore` + `TaskGroup` 实现。价值在队列可观测性与千级 case 的
调度开销，不在正确性。风险是动了全绿的执行核心，收益不明确。

---

## PRD 自己标注为"未来/后续"的（不列为任务）

- §52 Production Trace Replay —— 原文"未来支持"
- §88 docker / remote environment —— 原文"V1 实现：local"
- §89 PostgresFixture / GitFixture —— 代码显式 `raise ... planned but not implemented`

这三项在 PRD 里就是"以后再说"，不计入缺口。

但注意 §88 与 `assertion-extensions` 的交叉：`pytest` / `build` / `lint`
三个断言键的**本质是在 fixture 里执行代码**，而 V1 只在 local 执行、无任何隔离。
在那三个键之前，PRD §88 的沙箱必须先落地——所以它们被显式排除在
`assertion-extensions` 之外，该任务的 prd 里记了这个判断。

---

## 建议的执行批次

1. **批次一（门禁）**：`release-gate-suites` → `security-cases`
   目标：让 PRD §108 的 Release Gate 验收真的能红也能绿。
2. **批次二（体量）**：`harness-evaluators`（先 2~3 个）→ `evaluator-plugin-sdk`
   → 剩余 harness evaluator → `mvp-case-expansion`
   目标：PRD §44 与 §103 达标，且 Skill/MCP/Context 是**真覆盖**。
3. **批次三（扩展面）**：`semantic-trace-diff` / `case-artifacts` /
   `assertion-extensions` 按依赖顺序推进。
4. **可选**：`case-scheduler`。
