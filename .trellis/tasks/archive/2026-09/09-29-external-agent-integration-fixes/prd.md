# PRD：外部 Agent 平台接入缺陷补齐（harness-bench 侧）

需求源：`docs/external-agent-integration-change-plan.md`（四轮评审校准后，状态"评审已过，可作为需求实施"）。
本文只收敛**落在本仓**的工作项；B（转译 shim）/ C（专用评测集）/ D11/D12 属 ai-chatbot 仓，不在本任务范围。

## 背景

接入外部 SUT（ai-chatbot）暴露三类框架缺陷：观测面缺失导致的**假绿**（A2/A3）、
环境链路断裂（A1）、基线可比性无守卫（A4）；以及框架通用性无机制保障（E1–E5）、
文档条款未落地（D 类）。落地顺序按 change-plan §7。

## 工作项（按落地顺序）

### 批次 1（M1 前置，假绿三连解药）

- **A2 观测面能力协商**：
  - `HealthStatus` 扩展观测面表（与 A4 实际模型、A1 回执**一次设计**）；
  - 观测面表（事件名→bool）进 `EvaluationContext`，插件按 `required_events` 声明判
    `skipped`（`metadata.skipped_reason="observation_unavailable"`）；
  - 留痕：`RunMetadata.metric_capability_snapshot`（键空间与 `probe()` 的 metric id 区分）+
    `metric_degradations`；
  - **不复用 `resolve_metric`**（其语义是 fallback/exit 3，承接不了 skipped）；
  - runner 阶段顺序调整：`health_check`（含能力探测）→ `_resolve_profiles`；
  - 声明表查不到 → 按"具备"处理（既有行为不变）。
- **A3 性能三结局**：
  - `EvalScope` 增加用量观测标志（分量粒度：input/output 侧）；
  - `max_tokens` 约束在"依赖的用量分量未观测"时判 skipped（对齐 `max_cost` 的
    `ObservationUnavailable` 机制），禁止坍缩为 0；
  - 用量口径写进 `RunMetadata`；
- **E1 词汇校验**：
  - 消费 `EVENT_TYPES`：未知事件类型 → run 级 `protocol_violations` 计数（执行期累加，
    载体 `RunMetadata` 新字段）→ `aggregate.warnings`（默认 warn，可见不阻断）；
  - 独立升级开关（`--strict-protocol` / profile 级），开启时 `InfraError`（exit 2）；
  - 不复用 Gate 的 `strict` 字段；不改 `type` 为 Literal。

### 批次 2（与批次 1 同批提交）

- **E2**：能力声明来自接入侧（A2 的机制面，反模式三条写进文档）；
- **E3**：源码级边界断言测试 + 专有方言清单（仿 `test_evaluator_plugin.py:301-305`），
  扫描范围 `src/agent_eval/`；
- **E4**：`SessionContext.extra` 保持不透明透传（冻结形状，防具名字段）；
- **E5**：归一化责任在接入侧（契约条款，落 D7/D8/D14）。

### 批次 3

- **A4 模型钉住**（框架侧）：
  - health/`run.started` 回填 `RunMetadata.agent_model`；
  - `regression/compare.py` 增两条守卫：`agent_model` 不一致、用量口径不一致 →
    `comparison.valid=False` + `invalid_reason`（照抄既有三条守卫形状，
    天然继承 gate `undetermined` 链路）；
  - shim 侧显式模型字段属 ai-chatbot 仓，不在本任务。

### 批次 4

- **A1 workdir 透传**：
  - `_open_session` 构造 `SessionContext` 时传 `extra`（fixture workdir handle）；
  - `http_adapter` 把 workdir 放进 `create_session` metadata；
  - 回执通道：`create_session` 响应体新增字段（可达性回执），不可写 → `InfraError`；
    回执缺失按"未知"处理并记 warning，不得默认视为可达。

### 批次 5（与批次 1/2 同批提交）

- **D 类文档**（本仓）：D1–D10、D13–D16（PRD/Spec/quality-guidelines/ROADMAP/README/
  新增接入指南）。D11/D12 属 ai-chatbot 仓，跳过。

## 验收

- A2：SUT 不发 retry 事件 → `harness.retry` 判 skipped（非 pass）；反向用例超阈值仍 FAIL；
  时序用例断言观测面表在 health 阶段采集并到达 `EvaluationContext`；既有测试不因本改动变红。
- A3：全缺/半缺 usage 时 `max_tokens` 判 skipped（半缺 = 依赖输出侧未观测），reason 写明缺哪侧；
  口径进 `RunMetadata`。
- E1：未知事件类型 → warning 含类型名；开升级开关 → exit 2；未开时 verdict/exit code 不变。
- A4：两次 run `agent_model` 或口径不一致 → `INVALID` + `invalid_reason`，gate 落 `undetermined`。
- A1：mock server 记录 create_session metadata 含 workdir；端到端 agent 写文件 + `file_state`
  判 pass/fail；workdir 不可达 → `InfraError`。
- E3：边界断言测试绿（当前基线干净，命中数为 0）。
- `uv run pytest` 全绿、`uv run ruff check .` / `ruff format --check .` 双绿。
