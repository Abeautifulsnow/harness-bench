# PRD §90 Case 级 Artifacts 采集与索引 + FixtureProvider.snapshot

## 问题

PRD §90：

```text
Case Run 可保存：files / screenshots / logs / database snapshot / git diff /
command output / reports
Artifacts 必须与 case_run_id 关联。
```

PRD §89 的 `FixtureProvider` 抽象：

```python
class FixtureProvider(ABC):
    async def prepare(self, context): ...
    async def snapshot(self, context): ...   # ← 未实现
    async def cleanup(self, context): ...
```

当前状态：

- `FixtureProvider`（`fixtures/base.py:24`）**只有 `prepare` 与 `cleanup`**，没有 `snapshot`
- `runner.py:441` 会创建 `run_dir/artifacts/<case_id>/iter<N>/` 目录，
  并把它作为 fixture 的 workdir 传给 provider
- 但**没有任何采集或索引逻辑**：实测一个真实 run，`artifacts/<case>/iter1/` 是**空目录**
- `RunStore` 没有 artifacts 的方法；REST API 的 `/runs/{id}/artifacts` 列的是
  run 级五件套（report.json / gate.json / junit.xml / report.html / summary.md），
  不是 case 级产物

后果：PRD §90 的"Artifacts 必须与 case_run_id 关联"目前不成立。
一个 case 失败时，用户能看到 metric 与 trace，但拿不到它的工作区状态、
命令输出、数据库快照——而"Raw Trace 必须永久可追溯"（PRD §110-2）的精神
要求失败现场可复原。

## 范围

### 1. `FixtureProvider.snapshot()`

- 在 `fixtures/base.py` 的 ABC 上加 `async def snapshot(handle) -> ArtifactBundle | None`
- `FilesystemFixture`：快照"变更过的文件"（相对 prepare 时的状态）
- `SQLiteFixture`：导出数据库快照（`.dump` 或文件副本）
- 默认实现返回 None（不强制每个 provider 都产出快照）
- **快照时机**：case 执行结束、cleanup 之前。cleanup 会销毁 fixture，
  所以采集必须在 cleanup 之前完成——这一条要在 runner 里落实并测试。

### 2. case 级 artifact 采集

在 runner 的执行流程里，case 结束后采集并落盘：

| 类型 | 来源 | 说明 |
| --- | --- | --- |
| command output | trace 里的 tool call 结果 | 可能已有，确认是否落盘 |
| files | fixture workdir diff | 依赖 §1 |
| database snapshot | `SQLiteFixture.snapshot()` | 依赖 §1 |
| git diff | 若 fixture 是 git 仓库 | 与 `semantic-trace-diff` 任务有交叉 |
| logs | run 级日志 | 需确认现在有无 |
| screenshots | 前端/浏览器类 fixture | **当前无此能力**，如实标注未实现 |

**每类都要判断"现在能不能采"**。采不到的不要造空文件占位——
一个 0 字节的 `screenshot.png` 会让"已采集"的统计说谎。

### 3. 索引与关联（PRD §90 的硬要求）

- artifact 必须能通过 `case_run_id` 检索
- 需要一份索引：文件名 → 类型 → case_id → iteration → 大小 → 时间
  （放 case_run JSON 里，或独立的 `artifacts.json`；先看 `RunStore` 的现有形状再定）
- 落盘时**不要**把大文件塞进 report.json —— report 是给人看的摘要

### 4. 只读暴露

REST API 与前端已有 `/runs/{id}/artifacts` 与 `/runs/{id}/artifacts/{name}/raw`。
case 级 artifact 需要一个新路径（如 `/runs/{id}/cases/{case}/artifacts`），
并且**必须遵守 Spec §13 的只读契约**：
路径穿越要拦住（现有 `_safe()` 已有），越界返回 404 而不是读到仓库外文件。

## 交付物

| 文件 | 改动 |
| --- | --- |
| `fixtures/base.py` | `snapshot()` 抽象 + 默认实现 |
| `fixtures/filesystem_fixture.py` / `sqlite_fixture.py` | 各自快照实现 |
| `runner/runner.py` | 采集时机（cleanup 之前）+ 落盘 |
| `storage/run_store.py` | artifact 索引的读写 |
| `api/routers/runs.py` + `schemas.py` | case 级 artifact 端点 |
| `web/src/pages/RunDetail.tsx` | case 级 artifact 呈现（若 API 已有则一并做） |
| `tests/test_artifacts.py`（新） | 快照在 cleanup 前采集；索引关联；路径穿越拒绝 |

## 验收

```bash
uv run agent-eval benchmark run core
# artifacts/<case>/iter1/ 下出现采集到的产物（非空）
# 产物的索引可通过 case_run_id 反查
# 故意让 case 失败 → 失败现场（workdir 状态 / db 快照）仍可获取
```

反例必须红：路径穿越（`../../etc/passwd` 之类作为 artifact name）
必须 404/400，不能读到仓库外内容。

## 依赖

- `semantic-trace-diff`：git diff 类的采集与呈现有交叉
- 与 P1 已落地的报告五件套（run 级）区分清楚：本任务是 **case 级**
