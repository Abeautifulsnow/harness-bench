# 复核 docs：Spec V2.1 与 PRD V2.0 一致性

## Goal

对 `docs/agent-evaluation-regression-platform-engineering-prd-v2.md`（PRD V2.0，113 节）
与 `docs/agent-eval-engineering-spec-v2.1.md`（Spec V2.1，10 章）做第二次交叉复核，
输出一致性结论与待修订清单，供后续文档修订（V2.2 patch）决策。

## What I already know

* 两份文档为未跟踪新文件（未提交 git）。
* Spec V2.1 定位为 PRD V2.0 的增补工程契约层，冲突时以 Spec 为准。
* 复核范围：章节引用正确性、跨文档术语/Schema 一致性、内部逻辑自洽性、CLI 契约可实施性。

## 复核结论（2026-09-23）

总体：两文档工程上基本对齐，可进入实现；发现 4 项 P1、7 项 P2 问题。

### 已验证一致的部分

* Spec §0.2 章节映射表所引 PRD 章节号全部核对无误。
* review verdict 五值、queue_reason 六类与 PRD §60/§61 完全对齐。
* PRD §44 Harness Evaluator 计数 14 个，与 Spec §7.1 描述一致。
* 矩阵计算正确（§24：3×2×2=12；§106：2×2×2=8）。
* 阈值示例跨文档一致（task_completion 0.70 等）；PR Gate/Main Gate 参数与标定矩阵一致。
* 冲突优先级规则（Spec wins）使 §9 模块边界修正、§3.3 UNDETERMINED 扩展自洽。

### P1（建议实现前修订）

1. **Spec §6.4 CI 示例 `$RUN_ID` 未定义**：`benchmark run` 与 `gate "$RUN_ID"` 之间
   没有 run_id 传递契约（stdout 末行 / `--run-id-file` / `gate --latest` 均未约定）。
2. **Review Queue / promote 挂载粒度不明**：human_reviews.case_run_id 为 iteration 级 FK，
   但 flaky / regression 是 case 聚合级现象；FLAKY case 入队时指向哪个 iteration 未定义。
3. **Metric ID fallback 语法不一致**：Spec §7.1 `fallback: native:step_ratio`（冒号）
   与同文档点号命名空间 `native.*` 冲突，应为 `native.step_ratio`。
4. **PRD §31 与 Spec §3.2 字段词汇不对齐**：success_rate vs pass_rate、
   tool_choice_variance vs tool_sequence_variance；pass@1/3/5、trajectory/token variance
   在 Spec 中未承接也未声明放弃，存在双词汇风险。

### P2（文档质量修订）

5. Spec §6.3 "1 testcase = 1 CaseRun（含 repeat 聚合）" 自相矛盾：聚合后是 Case 级，
   应为 "1 testcase = 1 Case（其多个 CaseRun/iteration 聚合）"。
6. `--baseline-policy` 标志仅出现在 §6.4 GitLab 示例，未进入 §4.5 / PRD §70 CLI 契约。
7. Spec §2.3 session 级挂载点注释含混：output 断言作用于最终轮输出，
   tools/constraints 应作用于整个 session 聚合（forbidden=全程未调用、max_tool_calls=全程总数），
   需一句话明确。
8. `expected.status`（PRD §14 示例）不在统一 Assertion Schema 词汇表，
   也未列入"Case 级扩展字段"清单。
9. `case.context`（PRD §37 DeepEval 映射引用）在两份 Case Schema 中均未定义。
10. PRD §39 Metric Profile 旧格式（`deepeval.task_completion` 块式）与 Spec §7.1
    新格式（`metrics: [{id: agent.task_completion}]` 列表式）并存，未声明迁移/作废规则。
11. PRD §82 `case_runs.failure_category` 将派生层信息写入事实层表，
    与 Spec §1.3 "派生层可重建"原则有轻微张力，应注明为可重建的反范式缓存。

## Decision (ADR-lite)

**Context**: 复核发现 4 项 P1、7 项 P2 问题，需决定修订范围与版本策略。
**Decision**: 用户确认后将 P1+P2 一次性落地；Spec 以 Errata 形式升 V2.1 → V2.1.1
（文件名不变，增补 §10.1 Errata 表）；PRD 同步四处小改，版本记 V2.0 → V2.0.1。
**Consequences**: 两份文档消除双词汇与契约空洞；版本号小步演进，避免全文
"V2.1"自引用字符串的批量替换；后续实现以 Spec V2.1.1 为工程契约基线。

## Acceptance Criteria

* [x] P1-1 §6.4 定义 `--run-id-file` 交接契约，CI 示例不再引用未赋值的 `$RUN_ID`
* [x] P1-2 review 对象改为 run 内 case 聚合（CLI `<run-id> <case-id>`，表挂
  `run_id + case_id`），promote 保持 iteration 级并注明粒度差异
* [x] P1-3 fallback 语法改为 `native.step_ratio`
* [x] P1-4 §3.2 增加 PRD §31 字段对应，pass@k 仅当 repeat ≥ k 输出；PRD §31 加指针
* [x] P2-5 junit testcase 定义改为 Case 级聚合
* [x] P2-6 `--baseline-policy` / `--baseline-run` 进入 §4.5 CLI 契约与 PRD §71
* [x] P2-7 §2.3/§2.4 明确 session 级断言聚合口径与挂载点 AND
* [x] P2-8 `status` / `exit code` 列入 Case 级扩展字段
* [x] P2-9 Case Schema 增加可选 `context` 字段
* [x] P2-10 §7.1 声明 Profile 列表式为准 + 迁移映射
* [x] P2-11 PRD §82 注明 `failure_category` 为可重建反范式缓存
* [x] Spec §10.1 Errata 表（11 项）+ 版本行 V2.1.1；PRD 版本行 V2.0.1
* [x] grep 核验：旧写法无残留（`promote <case-run-id>` 与 Errata 记录文本为有意保留）

## Open Questions

（无 —— 修订范围已确认并执行完毕）

## Out of Scope

* 不在本任务修改任何代码（仓库尚无 src/ 实现，仅 main.py 脚手架）。

## Technical Notes

* 复核方式：全文通读 + 跨文档逐条引用核对；未做外部链接验证
  （Spec §7.5 / PRD §113 的 DeepEval 文档链接未重新抓取）。
