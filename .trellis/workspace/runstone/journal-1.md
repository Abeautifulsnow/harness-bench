# Journal - runstone (Part 1)

> AI development session journal
> Started: 2026-09-23

---



## Session 1: P0 核心执行链路实现

**Date**: 2026-09-23
**Task**: P0 核心执行链路实现
**Branch**: `main`

### Summary

实现 agent-eval P0：models/loading/adapters(SSE+HTTP+Fake+mock)/trace/evaluators(native+registry+deepeval)/runner/storage/reports/CLI，60 tests 全绿，ruff 清零，fake://与真实 mock server 双链路 e2e 通过；确立 uv_build 后端绕开本机桥接 bug

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `wip` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 2: P0 实现 review 修复 + 双批次提交

**Date**: 2026-09-23
**Task**: P0 实现 review 修复 + 双批次提交
**Branch**: `main`

### Summary

review-workflow 全流程：11 项 findings 修复（turn 级断言参与终判、expected.final 加载期报错、exit_code/max_cost fail-fast、endpoint exit 3、judge 移出 agent 槽位、docs 排除 ruff format、fixture 路径防护等），72 测试全绿，f8225e0(P0 实现 77 文件) + 83be168(脚手架) 已本地提交；push 因内网 GitLab 不可达挂起，待网络恢复后 git push origin main

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `83be168` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 3: 缺口任务清单：9 项遗留建为 Trellis 任务

**Date**: 2026-09-24
**Task**: 缺口任务清单：9 项遗留建为 Trellis 任务
**Branch**: `main`

### Summary

P0–P5 交付后复核 PRD/Spec 验收条款，识别 9 处未完成并建为任务（含 prd + spec 上下文），产出 ROADMAP.md；同时补正 4 个过期任务状态

### Main Changes

复核 PRD（V2.0.1）与 Spec（V2.2）的验收条款后，把 9 处未完成登记为 Trellis 任务，
每项都有 prd.md + implement/check 的 spec 上下文：

| 任务 | 优先级 | 性质 |
| --- | --- | --- |
| `09-24-release-gate-suites` | P0 | 代码 + 数据：gate 的 suites 字段未被校验、三个套件文件不存在 |
| `09-24-security-cases` | P0 | 数据 + 疑似引擎缺陷：安全用例数为 0，门禁空转 |
| `09-24-mvp-case-expansion` | P1 | 数据：§103 要求 20~30 cases，实际 6 个，缺 Skill/MCP/Context |
| `09-24-harness-evaluators` | P1 | 代码：§44 的 14 个 evaluator 全缺 |
| `09-24-evaluator-plugin-sdk` | P2 | 代码（契约）：§43 的公开扩展点不存在 |
| `09-24-semantic-trace-diff` | P2 | 代码：只有 structural diff |
| `09-24-case-artifacts` | P2 | 代码 + 契约：缺 snapshot，case 级产物为空目录 |
| `09-24-assertion-extensions` | P2 | 代码：12 个词汇表键只实现 3 个 |
| `09-24-case-scheduler` | P3 | 纯重构：PRD §85 的形状，功能已在 |

关键判断：
- **两条 P0 是"门禁失真"而非"功能缺失"** —— 不会报错，只会让 Gate 在该拦时放行。
  `release.yaml` 写了 `suites: [golden, regression, security, core]` 却无人校验；
  安全评测引擎完整但零条 case 用它，`security.max_failures: 0` 平凡通过。
- **`security-cases` 附带一处疑似引擎缺陷**：`runner` 调 `evaluate_security` 时
  只传 `tool_calls` 与 `final_output`，`mcp_names` 恒为 None，
  `security.forbidden_mcp` 可能永不触发。要求在任务内**实测**后下结论，不预设。
- **`assertion-extensions` 的 `pytest`/`build`/`lint` 被显式排除**：它们本质是
  "在 fixture 里执行代码"，而 V1 只在 local 执行、无隔离，需先有 PRD §88 的沙箱。
- **`case-scheduler` 标 P3 且可永远不做**：纯重构，动全绿的执行核心，收益不明确。

同时补正 4 个过期任务状态：`00-bootstrap-guidelines`、`09-23-docs-review-spec-v2-1`、
`09-23-p0-core-execution` 的工作早已完成却仍是 in_progress；
`09-25-p5-web-platform` 已提交（dcf61ea + b9a9ec1）却仍是 in_progress。

另产出 `.trellis/tasks/ROADMAP.md`：优先级视图、依赖图、排序理由，
以及 PRD 自己标注为"未来/后续"的三项（§52 Trace Replay、§88 docker/remote、
§89 Postgres/Git fixture）为何不计入缺口。

### Git Commits

| Hash | Message |
|------|---------|
| `2e645a5` | chore(trellis): P0–P5 交付后的缺口任务清单（9 项） |

### Testing

- [OK] `uv run pytest -q` → 103 passed（本批为文档/任务登记，代码零改动）
- [OK] `uv run ruff check .` → All checks passed
- [OK] `uv run ruff format --check .` → 108 files already formatted
- [OK] 9 个新任务的 `task.py validate` 全部通过；`task.py list` 正常（14 个任务）
- [OK] 4 个被修改的 task.json 逐个 json.loads 校验通过

### Status

[OK] **Completed**

### Next Steps

- 批次一（门禁）：`release-gate-suites` → `security-cases`
- 批次二（体量）：`harness-evaluators`（先 2~3 个）→ `evaluator-plugin-sdk` → `mvp-case-expansion`
- 批次三（扩展面）：`semantic-trace-diff` / `case-artifacts` / `assertion-extensions`
- 可选：`case-scheduler`
