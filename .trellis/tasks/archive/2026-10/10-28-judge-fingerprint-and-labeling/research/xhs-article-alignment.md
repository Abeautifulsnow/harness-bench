# 研究：小红书评测文章与 harness-bench 的对齐分析

来源：小红书 REDtech 微信公众号《小红书推荐系统评测 Agent：上下文驱动的
LLM-as-Judge 实践》（2026-10 分析，原文链接
https://mp.weixin.qq.com/s/333qmTTiNsFp9m8h49Slgg ）。

## 文章七个论点 → 本仓映射

| 文章论点 | 本仓现状 | 结论 |
|---|---|---|
| ① 上下文驱动：结果绑定输入快照+评测版本；缺证据单独记录、不补全 | run 产物落盘 + replay `meta.origin` 回指；judge 无输入落 `skipped(judge_input_unavailable)`；replay"没有 policy 不打分" | 已对齐 |
| ② 维度=基础体验+目标符合度；0/1/2 三档；分数+理由+执行状态；分维度保留 | evaluator 维度化 + 五值 verdict（含 skipped/error）+ reason | 已对齐（档位制不适用：我们是阈值+二值判定） |
| ③ 代码/统计特征+LLM/限定范围 LLM 三分法 | `native.py` / `harness.py` / `deepeval_adapter.py` 分层 | 已对齐（逐条对应） |
| ④ 校准：人机双评、加权 Kappa、0 分样本召回、人人一致性上限、不一致归因三分法 | `calibration/`（κ、near-threshold、台账）已有；**双人天花板、不一致归因缺位** | **本任务交付 C 部分** |
| ⑤ 可比性/可重放：结果记录生效配置版本；重放前校验；显式换配置走新旧对比 | criteria_version 手工声明进守卫（§26.5）；Trace Replay 同 policy 双侧 | **本任务交付 A/B 部分**（自动指纹补手工声明的盲区） |
| ⑥ 分布分析 + 五级下钻 | Wilson/bootstrap/McNemar 已有；Web 缺维度分布视图 | 后续（Web 校准面板批次） |
| ⑦ LLM-as-User 连续模拟（三层验证法） | 无 | 远期参考，记 ROADMAP |

## 关键设计裁决（本任务依据）

1. **加权 Kappa 不适用**：文章的 judge 按 0/1/2 有序档位打分，加权 κ（二次权重）
   惩罚"跨档分歧"；我们的 judge 输出 float score + 阈值二值化，校准比较的是
   pass/fail 判定（Spec §26.2 封闭词汇）。2×2 混淆矩阵上任何加权方案与未加权
   Cohen's κ 数值相等——实现它属于"声明了却不算数"的装饰品。触发条件：judge
   引入有序档位时随之实现。
2. **"0 分样本召回率"已有等价物**：正类 = judge FAIL 的 `recall`
   = P(judge FAIL | human FAIL) = "人工判坏的样本里 judge 抓住了多少"。
   字段已存在（`MetricAgreement.recall`），不新增。
3. **人人一致性上限需要双标机制**：单人标注永远算不出天花板。本轮补字段
   （`human_verdict_2` + rater provenance）与计算（`human_kappa` + `human_dual_n`），
   数据等真标注流程产出。
4. **自动指纹与手工声明的关系**：criteria_version（§26.5）语义是"判准（rubric
   形状）变更要递增"，靠作者自觉；judge_config_digest 是从生效配置
   （judge_model / SDK 版本 / 判准版本 / 各 metric 阈值）自动算的指纹，兜住
   "作者忘了递增/没声明"的盲区。两者并存：digest 相等 ⇒ criteria_version 相等
   （digest 含它）；criteria_version 相等 ⇏ digest 相等（threshold 等漂移只有
   digest 能抓）。compare 守卫两者都留，单侧缺失都放行。
5. **不一致归因三分法原样采纳**：input_missing（输入缺失）/ rubric_ambiguous
   （标准边界模糊）/ model_misunderstanding（模型理解偏差）。封闭词汇 + 常量，
   校验"归因只能写在分歧项上"。这直接服务 ROADMAP 缺口 #4 的"异动归因"。
6. **run 级粒度足够**：一个 run 内 judge 配置恒定（模型/阈值/SDK 版本随 run 固定，
   逐 metric 阈值进 digest 输入），逐结果重复同一戳没有信息增量——文章的"每条
   结果记录版本"在我们以 run 级落账 + 校准 set 级继承实现。

## 生产侧缺口（本任务不做，登记后续）

- 生产 evaluation 记录只存 policy id 不存内容 hash——同一 id 改 YAML 后历史
  结论的口径不可追溯。与 ROADMAP 缺口 #4（生产 judge 输入清洗/异动归因）同批。
- judge 版本回归对比（同批 trace 上新旧 judge 配置互评，improved/same/regressed
  分布）——文章④的版本回归，独立增量，可复用 compare + calibration 基建。
