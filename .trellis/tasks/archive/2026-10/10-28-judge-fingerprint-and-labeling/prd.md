# PRD：评测版本指纹绑定校准链 + 校准标注升级

- 任务目录：`.trellis/tasks/10-28-judge-fingerprint-and-labeling/`
- 需求源：小红书 REDtech《推荐系统评测 Agent：上下文驱动的 LLM-as-Judge 实践》
  （分析沉淀见 `research/xhs-article-alignment.md`）+ ROADMAP 剩余缺口 #1/#4 的
  机制准备度。
- 契约基线：Spec V2.4 §26（Judge Calibration 与统计置信）。本任务**只扩展不推翻**
  既有契约：criteria_version 守卫（§26.5）、校准台账边界（§26.1）、二值一致性口径
  （§26.2）全部保持。

## 背景与问题

文章要求"每个评测结果记录生效的配置版本；配置显式变更走新旧对比；重放/比较前校验
口径一致"。我们已有手工声明的 `criteria_version`（Spec §26.5，进 compare 守卫），但：

1. **未声明时无指纹**：criteria_version 依赖 profile 作者自觉声明与递增；未声明
   （现状绝大多数 run）时，run 没有任何"生效 judge 配置"的指纹——改了 threshold /
   换了 judge model / 升了 deepeval 版本，基线守卫全部放行，judge 口径漂移被伪装成
   回归或"分布波动"。
2. **校准链不接版本**：`calibrate create/evaluate/snapshot` 完全不携带
   criteria_version——校准快照无法回答"这份一致性结论描述的是哪个判准版本"，
   而 Spec §26.5 自己写明"版本不变而 agreement 掉了 = 分布漂移；版本变了 = 判准
   变更。这是校准趋势可解读的前提"。
3. **标注机制缺两样**：文章的校准方法论里，一致性要对照"人人一致性上限"看，分歧
   要归因（输入缺失 / 标准边界模糊 / 模型理解偏差）。我们的 CalibrationItem 只有
   单人 `human_verdict`，分歧无归因字段——机制缺位，等真标注数据到位时补字段会
   造二次迁移。

## 交付范围

### A. judge_config_digest（自动指纹，run 级）

**A1. 纯函数**：新增 `compute_judge_config_digest`（建议落
`src/agent_eval/evaluators/fingerprint.py`，纯 stdlib）：

- 输入：`criteria_version: str | None`、`judge_model: str | None`、
  `deepeval_version: str | None`、`judge_specs: list[MetricSpec]`（**解析与
  degradation 之后的生效集**）。
- 计算：规范化 JSON（`sort_keys=True`，字段：criteria_version / judge_model /
  deepeval_version / `metrics: [{metric, threshold}, ...]` 按 metric id 排序）
  → sha256 → 取前 16 位 hex。
- 边界：`judge_specs` 为空（`--no-judge` 或 probe 全 False）→ 返回 `None`——
  "没有生效的 judge 口径"如实缺席，不造空串指纹。
- 同一配置必须产出同一 digest（确定性是"同口径"判定的前提）。

**A2. Runner 落账**：`RunMetadata` 新增字段
`judge_config_digest: str | None = None`（缺省 None = 旧 run / 无 judge 口径，
不参与守卫）。在 profile 解析完成、`resolved.judge_specs` 可知之后回填
（先例：`meta.metric_capability_snapshot` 也是 create_run 后回填；
`runner.py:351` 建 meta、`runner.py:580-603` 解析 judge_specs，顺序不可反）。

**A3. compare 守卫**（`regression/compare.py` 守卫链，Spec §4.3 模式）：

- 双侧都有 `judge_config_digest` 且不等 → 追加 INVALID 理由，指名两侧值，
  并提示"对照 judge_model / criteria_version / deepeval_version / profile 阈值
  排查哪一项变了"。
- 双侧相等或任一侧为 None → 不拦（旧 run / no_judge run 兼容，与
  criteria_version / agent_model 守卫"单侧缺失放行"同一取舍）。
- 该守卫兜住 criteria_version 守卫的盲区：未声明判准版本时，threshold /
  judge_model / SDK 版本的任何漂移从此可见。

### B. 版本字段贯穿校准链

- `CalibrationSet` / `CalibrationSnapshot` 新增
  `criteria_version: str | None = None`、`judge_config_digest: str | None = None`。
- `calibrate create --source run:<id>`：从 `RunMetadata` 读入两个字段落账；
  `--source production:<id>` → 两字段 None（生产评测记录不含判准版本，
  与 judge_model=None 同一先例：不猜）。旧 set 文件缺字段照常加载（pydantic 缺省）。
- `build_snapshot` 把 set 上的两字段透传进快照；`evaluate` / `show` / `list`
  输出里展示（digest 展示短形式即可）。

### C. 标注机制升级（文章校准方法论对齐）

**C1. CalibrationItem 新字段**（全部带缺省，旧数据兼容）：

- `human_verdict_2: Literal["pass", "fail"] | None = None`——第二标注人
  独立标注（双盲：label 命令以 `--round` 区分，round 2 写此字段，互不覆盖）。
- `human_rater: str = ""`、`human_rater_2: str = ""`——标注人 provenance。
- `disagreement_cause: Literal["input_missing", "rubric_ambiguous",
  "model_misunderstanding"] | None = None`——不一致归因三分法（文章原样），
  词汇封闭，常量 `DISAGREEMENT_CAUSES`。语义：输入缺失（judge 该看到的东西
  不在输入里）/ 标准边界模糊（rubric 对该 case 无法给出唯一判定）/ 模型理解
  偏差（标准清晰但模型读偏）。

**C2. CLI**：

- `calibrate label` 新增 `--round 1|2`（缺省 1）、`--rater NAME`、
  `--cause CAUSE`。
  - `--cause` 校验：目标 item 的 `human_verdict` 未标注 → 拒绝（分歧还不存在，
    归因对象不存在）；已标注且与 judge_verdict 一致 → 拒绝（一致项无归因对象）。
    两种拒绝都是 exit 3 + 指名原因，不静默忽略。
  - round 2 与 round 1 互不覆盖对方字段。
- `calibrate import-labels` 条目支持可选 `round` / `rater` / `cause` 字段，
  校验同上；**全有或全无**语义保持（任何非法条目整批不落盘）。

**C3. 一致性计算**（`calibration/agreement.py`）：

- `MetricAgreement` 新增：`human_kappa: float | None`（rater1 vs rater2 的
  Cohen's κ，只在双标齐备的 item 上算；pe ≥ 1 退化 → None，与 judge κ 同规则）、
  `human_dual_n: int = 0`（参与天花板估计的 item 数）、
  `disagreement_total: int = 0`（labeled 且 human_verdict ≠ judge_verdict 的
  条数）、`disagreement_causes: dict[str, int]`（归因分布，只含有 cause 的）。
  未归因数 = total − sum(causes)，不单独造字段。
- `human_kappa` 的语义是**天花板参照**：judge κ 显著低于人人 κ 才是 judge 的问题；
  人人 κ 本身低说明 rubric 边界模糊（归因里的 rubric_ambiguous 会在分歧子集里
  印证）。CLI `evaluate` 输出对应解读行（有双标数据时）与归因分布行（有分歧时），
  没有数据时整行缺席（不渲染空表）。

### D. 明确不做（本轮裁决）

1. **加权 Kappa 不做**：我们的校准是二值判定（pass/fail，Spec §26.2 封闭词汇），
   2×2 混淆矩阵上加权 κ 与未加权 κ 数值相等——加了就是装饰品（质量规范 §17.6
   同类禁令）。文章的加权 Kappa 针对 0/1/2 有序档位；若未来 judge 引入有序档位，
   随之实现并回写 spec。此裁决写入 Spec §26 新小节。
2. **"0 分样本召回率"不新增代码**：正类 = fail 的 `recall`
   （P(judge FAIL | human FAIL)）就是它在我们口径下的等价物，字段已存在。
   在 spec 里写明映射关系，避免后人重新推导。
3. **生产侧指纹不做**：policy 内容 hash / evaluation 记录落 digest，与 ROADMAP
   缺口 #4（生产 judge 输入清洗/异动归因）同批后续。
4. **judge 版本回归对比**（新旧 judge 配置互评同批 trace）不做——独立后续增量。
5. Web 校准面板 / REST 透传新字段不做（§26.1"Trace Replay 先 API 后 Web"同款
   节奏，随面板批次一起）。

## 验收标准

1. 全量 `uv run pytest` 全绿（现基线 828 passed 不回归）；
   `uv run ruff check .` + `uv run ruff format --check .` 双绿。
2. 测试锚点（扩展 `tests/test_calibration.py`；compare 守卫用例放既有回归测试
   文件；digest 单测独立成文件或并入校准测试均可）：
   - digest 确定性：同输入同 digest；threshold / judge_model / criteria_version /
     deepeval_version 任一变化 → digest 变化；judge_specs 空 → None。
   - compare 守卫三态：双侧不等 → INVALID 且理由指名字段；单侧 None → 放行；
     双侧相等 → 放行。
   - `calibrate create`（run 源）把 meta 的 criteria_version + digest 带进 set
     与快照；production 源两字段为 None。
   - 人人 κ 闭式解：构造已知 2×2（如完全一致 → κ=1；含手算值的混合用例）；
     pe 退化 → None；`human_dual_n` 正确。
   - 归因：分歧子集分布正确；`--cause` 写在未标注/一致项上被拒（exit 3）；
     未归因数 = total − sum(causes)。
   - import-labels round 2 + 新字段全有或全无语义保持；旧 set JSON（无新字段）
     可加载。
3. LF 行尾：全部改动走 Write/Edit，不跑 python 改写脚本（质量规范硬约束）。
4. 注释纪律：只在契约约束处写注释并引用 Spec 小节号；新 spec 小节编号在
   Phase 3.3 回写时确定（建议 §26.8）。

## 关键既有代码坐标（供实现定位）

- `src/agent_eval/runner/runner.py:351`（meta 构建）、`:580-603`（judge_specs
  解析）、`:1380`（_build_meta）。
- `src/agent_eval/models/run.py`（RunMetadata，criteria_version 落账注释在
  criteria_version 字段处）、`src/agent_eval/models/profile.py:35`
  （MetricProfile.criteria_version）。
- `src/agent_eval/regression/compare.py:346-384`（守卫链，criteria_version 守卫
  就在其后，digest 守卫紧随其模式）。
- `src/agent_eval/models/calibration.py`（CalibrationItem / MetricAgreement /
  CalibrationSet / CalibrationSnapshot）。
- `src/agent_eval/calibration/agreement.py`（compute_metric_agreement /
  build_snapshot）、`sample.py`（collect_run_items 已持 meta，
  但 set 级字段在 calibrate_cmd.create 落账更合适——items 不需要逐条带版本，
  run 源的 set 天然单一口径）。
- `src/agent_eval/cli/commands/calibrate_cmd.py`（label / import-labels /
  evaluate / show / list）。
- `tests/test_calibration.py`（既有校准测试锚点）。
