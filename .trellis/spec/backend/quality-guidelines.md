# Quality Guidelines

> 质量门槛与编码约束（P0 落地时确立，随任务演进）。

---

## 检查命令（合并前必须全绿）

```bash
uv run pytest        # 375 tests（含 e2e：FakeAgent 全链路 + 真实 TCP mock server + REST API）
uv run ruff check .  # 规则：E/F/I/UP/B/SIM；CLI 文件豁免 B008（Typer 惯用法）
uv run ruff format --check .
cd web && bun run typecheck && bun run build   # 前端改动时
```

测试的 judge 隔离约定：`tests/conftest.py` 的 `deepeval_absent` 夹具默认让能力探针
报告"不可用"，使 native 评测路径的结论不取决于开发机是否装了 deepeval。需要真实
judge 行为的用例标 `@pytest.mark.real_judge` 退出该夹具。装了 deepeval 却没有模型
凭据时 judge 调用会失败并让 run 正确降级为 exit 2（Spec §6.1）——那是另一条语义路径，
由专门用例覆盖，不应把整批 native 用例一起染红。

## 编码约束

1. **Python 3.12+**：`StrEnum`（不用 `class X(str, Enum)`）、`asyncio.timeout`/`TaskGroup`、`X | None`。
2. **Pydantic v2**：可变默认一律 `Field(default_factory=...)`；YAML 宽松面用 `extra="allow"` + 显式 extensions 收集。
3. **异步**：每 iteration 完全隔离（session/fixture 均不共享）；`async for` 消费的生成器内不得让异常静默吞掉协议违约。
4. **禁止**：业务代码直接 import deepeval（只允许 `evaluators/deepeval_adapter.py`）；CLI 之外打印（输出走 rich console 或返回值）。
5. **注释**：只在代码无法自表达"契约约束"时写（引用 PRD/Spec 章节号）；不复述实现。
6. **测试**：网络层用 `httpx.MockTransport`；需要真实 socket 时用 `dev/mock_server.serve(port=0)`；
   非确定场景（FLAKY/错误序列）用 `FakeAgentAdapter.script_queue` + `concurrency=1`。
7. **数值语义**：`None` 与 `0` 是两种事实。PRD §59 规定无定价时 cost 为 `None`
   （不是 `0.0`），报告与 Web UI 都必须让二者显示得不一样——把 null 当 0 会让
   趋势图出现"成本降到零"的假象。
8. **派生层只读消费**：DuckDB 投影可随时 `rebuild()`，所以任何"查询前先建库"的
   便利写法都是错的（REST API 会因此把"未物化"伪装成"没有数据"）。只读消费方用
   `Analytics(read_only=True)`。

## 门禁约束（PRD §108，Spec §15）

"声明了却从不求值"是 Gate 层最严重的缺陷类别：它不让任何功能报错，只在该拦住的
时候放行。守住两条：

1. **Gate YAML 的每个字段都必须有求值点**。`GateRules` 解析出的声明如果没人读，
   就是装饰品——`suites` 曾在 YAML 里躺了整个 P4（`evaluate_gate()` 从不读它），
   导致"只跑 smoke 的 run"套上 release 规则也能 PASS。新增字段时同时给出 rule。
2. **套件覆盖只能读事实源，不能靠反推**。必跑套件的判定读
   `RunMetadata.suites_covered`（套件 → 选中 case 数），不从 case tags 反推，
   也不把 `0` 当成"跑过了"。`0` = 未覆盖 = FAIL：`security.max_failures: 0`
   在零个安全 case 时的平凡通过，与真正的"零失败"是两件不同的事。
3. **选择面与求值面必须成对**。`suites.coverage`（求值）之外还要有
   `resolve_suites()` 用 `benchmark.suites ∪ gate.suites` 扩宽实际选择（选择）；
   只做一半会分别得到"永远 FAIL"或"依然静默通过"。
4. `suites: []` 是"不约束"，不是"任何套件都不许跑"——反向语义会让 PR/Main
   两套 Gate 变成永远 FAIL。

## 安全断言约束（PRD §62/§63，Spec §12/§16）

安全层的规则不落在断言"能算"，而落在"观测面有没有接上"：

1. **观测面必须逐条透传**。`forbidden_mcp` 曾因 runner 不传 `mcp_names` 而**恒 pass**
   （`mcp` 永远是空列表）；`forbidden_command` 也看不到 `command.*` 事件。
   现在 `EvalScope` / `TurnResult` / `CaseRunResult` 都带 `tool_calls` /
   `mcp_calls` / `command_calls` 三个并列观测面，Runner 逐轮收集。
   新增安全规则时必须先确认它的观测面在事件流里有来源。
2. **判定对象是行为，不是文本**。只有 `secret_patterns` 读最终输出，方向是
   "检测泄漏"；路径与提权标记只读工具参数。把两者混在一起会让"解释为什么
   不做 rm -rf"的合规 Agent 被判违规。
3. **命中可红**。每条规则都要有一条"违规行为 → FAIL"的实测用例；
   只有负向 case 时"什么都拒"的 Agent 也能全过，所以正向 case 同样是必需的。
4. **脱敏按 Spec §12.3**：回显前 4 字符 + `***`。推论：**测试源码里不得出现完整
   密钥字面量**（用片段拼接），否则密钥扫描会把测试本身报成泄漏事件。
5. **安全脚本单一实现**：`adapters/fake.py` 的 `SECURITY_RULES` / `turn_events()`
   是唯一脚本源，`dev/mock_server.py` 复用它们——安全用例最不能容忍两份实现漂移。

## Evaluator 插件约束（PRD §43/§44，Spec §17）

1. **加插件 = 只调用 `register_plugin()`**。第三方新增 Evaluator 不得修改
   `runner.py`（PRD §109.4）；`tests/test_evaluator_plugin.py` 断言 Runner 源码里
   不含任何具体插件名。图省事在 Runner 里写 if-else 会让"可扩展"退化成宣传语。
2. **命名空间强制 `harness.*`**。`custom.*` 留给用户 GEval，`native.*` /
   `security.*` 是平台自有语义——后者尤其重要：伪装成 `security.*` 等于绕过
   唯一不可被 judge 覆盖的层。`provider_for()` 对未注册的 `harness.*` id 也必须
   返回 `harness`，落 `native` 兜底会把"插件没注册"当成"有 native 规则在跑"。
3. **平台字段由平台铸造**。插件的 `metric` / `evaluator` / `id` / `case_run_id`
   一律走 `EvaluationContext.result()`；返回值与注册名不一致时判 `error`，
   不静默改名。插件崩溃 = EVALUATION_FAILURE（PRD §46），不得变成 pass。
4. **阻断权在 Profile，不在插件**。`result()` 的 `blocking` 缺省 False，Runner
   以 `spec.blocking` 覆写；阈值走 `MetricSpec.params`
   （`{**plugin.default_params, **spec.params}`）。插件是"观测说明了什么"，
   是否拦门禁是 Gate 策略。
5. **`harness` 是确定性 provider**（`DETERMINISTIC_PROVIDERS`）：插件是过程内
   Python 判定，`--no-judge` 不得把它们一起关掉（PRD §86 只说 judge 与 agent
   并发分离）。
6. **只实现能写清算式的**。PRD §44 的 14 项里 10 项未实现，理由记在 Spec §17.3
   （6 项缺声明侧词汇、3 项缺观测侧通道、1 项属 judge 职责）。恒 PASS 的占位比缺失
   更危险：它会让覆盖统计说谎。新增插件前先补词汇表与观测面，顺序反了就会得到
   "声明了却静默失效"。
7. **必须双向测试**：命中 → FAIL、合规 → PASS。只测一侧会把"恒判 fail 的
   evaluator"当成正确实现。声明侧缺失时的正确判决是 `skipped`（blocking=False），
   不是 pass——没有依据的 pass 是假信号。
8. **"未声明"与"声明为空"必须可区分**：需要 skipped 语义的参数缺省值一律 `None`，
   判定用 `is None`。写成 `[]` 会让"`allowed: []` = 任何 MCP 调用都越权"这类
   **真断言**退化成 skipped——该红的时候不红。
9. **参数三级合并**：插件 `default_params` < Profile `MetricSpec.params` <
   Case `metric_params`。Case 写了 Profile 未运行的 metric → 启动期 fail-fast；
   写了个不生效的期望与"永不失败的断言"同类。
10. **聚合指标不拆名**：同一观测面的两个 PRD 名字（SkillPriority + SkillLoad）
    合成一条 metric。拆开只会得到两条永远同时红/同时绿的指标，让覆盖统计虚高。

## 断言扩展约束（Spec §19）

词汇表 11 个键（`permission` 已移除），处置分三类：已实现 7 个、执行型 3 个
（`pytest` / `build` / `lint`，依赖 PRD §88 沙箱）、依赖其他观测面 1 个（`git_diff`）。
守住六条：

1. **先回答"观测数据从哪来"**。说不清观测来源的键不实现，而是给出明确处置
   （移除 / 标注依赖 / 标注执行型）。处置表在 Spec §19.1，改实现就要同步改它。
2. **`skipped` 是独立结局**。满足 → PASS、不满足 → FAIL、**观测不到 → skipped**
   （`blocking=False`）。缺第三项时"看不到"会被塞进 PASS（假信号）或 FAIL（冤枉），
   两者都污染结论。实现上由 `ObservationUnavailable` 承载，它在
   `evaluate_assertions` 里被转成 skipped + `metadata.skipped_reason`。
3. **观测不足 ≠ 声明非法**。前者 `ObservationUnavailable`（判 skipped，要用例作者
   改 fixture/协议），后者 `UnsupportedAssertionError`（判 error，要用例作者改用例）。
   混用会让"下一步动作"指向错误的文件。形状校验、越界路径、非法表名、未实现的键
   都属后者，且必须在 `unsupported_declarations()` 里也能提前报出来——
   启动期放过、运行期报错是最难查的一类落差。
4. **`null ≠ 0`（PRD §59）**。`max_cost` 在无定价时是 `None`，判 skipped 而不是
   `0 <= max_cost → pass`；`exit_code` 未被协议上报时保持 `None`，不猜成 0。
   把"没观测到"归一化成 0 会让最需要拦的假象过闸。
5. **三者一致**：`EXTENSION_KEYS`（词汇表）== `IMPLEMENTED_EXTENSIONS`
   ∪ `SANDBOX_DEPENDENT_KEYS` ∪ `DEFERRED_EXTENSION_KEYS`，且
   `unsupported_declarations()` 对每个子集给出各自的下一步动作。漂移会让
   `case validate` 的输出与实际能力脱节。三个子集的分工固定：执行型 = 依赖
   PRD §88 沙箱；`DEFERRED_EXTENSION_KEYS` = **已裁决不做**的键（`git_diff`，
   Spec §20.4）。"不做"同样必须给出替代手段：笼统的"尚未实现"会让下一个人
   重新做一遍这个判断，所以 `git_diff` 的提示指向 `file_state`。
   形状非法的扩展键要报出**哪个键**非法（`tool_arguments_problem()` 把 pydantic
   拒绝的字段名带出来）——`extra="forbid"` 的词表里最常见的失败是拼错的匹配器键。
6. **别让陪跑组污染分母**。`native.status` 只在声明了 `status` 或出现未实现键时产出；
   让它在每个有扩展断言的 case 上都产出，会得到一条永远 pass 的指标——
   与"恒 PASS 的占位"同类（§17 第 6 条）。同理，已实现的扩展各有自己的 metric id，
   **空块（`sql_result: {}`）与未声明同口径、不产出 metric**；形状非法则必须产出
   `error`（与"没声明"是两件事）。`exit_code: {}` 是例外：缺省语义"全部被观测命令
   以 0 结束"是真断言。

## 语义 diff 约束（PRD §57，Spec §20）

归一化是"让差异变少"的手段，而**漏判比误报危险**（误报让人多点一次确认，漏判让
真实回归静默通过）。守住五条：

1. **每条规则成对测试**：判相同的用例 + **不该被归一化掉的对照用例**。只有前半
   会得到一个"把什么都判成相同"的实现。规则表在 Spec §20.1，它是**封闭**的，
   加规则要连反例一起加。
2. **两种差异要分开存**：该报的进 `argument_diffs`，语义相同的进
   `TraceDiff.semantic_equal`。同一路径只能出现在一边（两处都报会让报告自相矛盾），
   而"参数差异为空"必须能与"数据丢了"区分开——所以 `semantic_equal` 非空或带
   `diff_notes` 的 case 即使 `changed=False` 也要在报告与 Web 上展示。
3. **降级必须可见且去重**：sqlglot 缺失 / SQL 解析失败 / 方言未声明三种成因，
   逐处详情留 `degraded`，**按分类去重的摘要**进 `TraceDiff.diff_notes`
   （几十个参数各带一行同样说明是噪声）。绝不在"没能力判断"时静默判相同或不同。
   推论：文本层判相同**不附带降级**——它只折叠关键字大小写与空白，是完整结论。
4. **方言来自 case 声明**：`Case.environment.database` → `dialect_for()` →
   `EvalScope.database` / `CaseRunResult.environment_database`（反范式化副本，
   因为比对时不一定持有 case 定义）。未声明时按 sqlite 解析**并标注**
   `dialect_defaulted`，不在任何地方硬编码方言。
5. **`semantic: true` 是显式开关，默认关闭**，且只影响字符串值：`exact` 的字面
   语义不变（"把大小写差异判成不同"在某些用例里正是要断言的事），非字符串值仍走
   原有相等性。文件类 diff 用 `file_state` 快照比对，不引 git（Spec §20.4）。

## Case 级产物约束（PRD §90，Spec §21）

产物是"现场留存"，与判定无关：采不到不该让跑完的执行变 ERROR，采到了也不参与
Gate。守住五条：

1. **采集时机固定在 cleanup 之前的 `finally` 里**。cleanup 删掉 workspace 与库文件
   （`SQLiteFixture` 连 `-wal`/`-shm` 一起删），之后就没有现场；放 `finally` 而不是
   成功路径，是为了让 infra error 的现场同样可复原。
2. **索引只有一份，挂在宿主上**。`CaseRunResult.id` 就是 `case_run_id`，
   `CaseRunResult.artifacts` 就是索引——不要建 `case_run_id → 产物` 的映射表
   （多一份映射多一个漂移点，漂移的表现是"文件在磁盘上、索引里查不到"）。
   `ArtifactRecord` 里不放 `case_id`/`iteration`：那是宿主给的。
3. **采集失败一律记账，不改判定，且要说出原因**。provider `snapshot()` 声明
   采不到（抛 `SnapshotUnavailable`，如库被 agent 删掉 / dump 失败）/ 抛其他异常 /
   返回畸形值 / 元素类型不对 / name 非法 / 写盘失败，都进
   `CaseRunResult.artifact_notes`。静默返回 `[]` 会把"该采的拿不到"伪装成
   "本来就没有"；两种异常的前缀也不同（`snapshot unavailable:` 指向 agent 对
   环境做了什么，`fixture snapshot failed:` 指向 provider），排查方向相反。
4. **采不到的观测面不造空文件占位**（PRD §90 原文），在能力表
   （`models/artifacts.py` 的 `UNAVAILABLE_KINDS`）里如实写明原因与替代手段，
   并让 API/Web 把它渲染出来。`files.changes.txt` 的"无变更"是采集到的结论，
   不属于占位。上限（文件个数 / 单文件字节 / 清单行数 / dump 字节）必须配
   `truncated=True` + `note`，不许静默截断；省略个数挂在 `files.changes.txt`
   的 `note` 上（它必然存在），不挂在"最后一条内容产物"上。
5. **比对与预览都不做无界读**。变更清单的签名用**全文件**流式摘要
   （只摘头部会把头之后的就地改写漏判成"无变更"——漏判比误报危险）；
   预览端点只读上限 +1 字节，`bytes` 用 `stat` 报真实大小，先整文件
   `read_bytes()` 再截断等于上限名存实亡。

路径解析只有一条规则：**请求里的 `name` 必须与索引中某条记录全等**，再用该记录的
`path` 去解析文件，解析后再判"在 run 目录内"。`ArtifactRecord.path` 出自磁盘上的
`case_runs/*.json`，那是数据不是可信输入，所以第二道判断不能省。落盘侧另有
`safe_artifact_name()`（白名单 + 逐段 `..` 检查，两层缺一不可）。

## 用例集覆盖约束（PRD §103，Spec §18）

"用例数量够"与"维度真被覆盖"是两件事。守住三条：

1. **标签不算覆盖，断言才算**。维度 case 必须声明可判定的期望（断言或
   `metric_params`）；只打 `skill` 标签而没有 `harness.skill_load` 期望的 case
   是假覆盖。`tests/test_case_coverage.py` 逐条检查这一点。
2. **负向 case 必须真的红**。只有正向用例时，"断言写错了（正则拼错、字段名错）"
   与"agent 合规"在报告里长得一模一样——两者都是全绿。每个维度至少一条负向。
3. **golden 只收正向**。把负向 case 放进 golden 会让
   `golden.required_pass_rate: 1.0` 永远 FAIL（Release Gate 不可用）。
4. **fixture 边界**：新 case 只能落在已实现的 fixture 能力（`filesystem` /
   `sqlite`）内；`postgres` / `git` 在 `get_provider()` 里显式 raise planned，
   用了会以 infra error 收场，不是"暂时没数据"。
5. **维度扩展的顺序**：先想清楚"拿什么断言"，再动手加 case。写不出真断言的维度
   应标注为**受限覆盖**并记录缺口，不要造只会"输出里出现某个词"就算过的假覆盖。
6. **负向 case 的断言落在哪个 metric 上，就该用哪个 profile**（2026-09-30，C 类实测）。
   `CaseStatus.FAIL` 的判据是 `blocking_failed`，而 `blocking` 归 Profile 决定
   （Spec §17.2）：一条负向 case 若把期望声明在 `harness.*` 上、却挂在
   `harness.*` 全部 `blocking=false` 的档里，那条 metric 判 fail 而 **case 仍是
   PASS**——恒绿的负向用例与"断言写对了、agent 也合规"在报告里完全同形，
   是本条要拦的假绿换了个发生位置（从插件实现搬到了档位选择）。
   先例：`database-core` 的 `skill.priority.wrong_order` 用 `strict` 档、
   `chatbot-core` 的负向用 `chatbot-strict`。机械护栏：case 用 `metric_params`
   声明了对某插件指标的期望，该指标就必须在该 case 的 profile 里 `blocking=true`
   （`tests/test_chatbot_dataset.py::TestAssertionsCanActuallyFail`），不留例外——
   有例外就没法机检。`native.*` 不在此列：它们的阻断权由平台固定。
7. **"故意不可满足"要真的不可满足**（同上实测）。需要"理想值"的连续型断言
   （`step_efficiency` 的 `baseline_steps`）只要理论值与实测值能相等，用例随时
   失去判别力：ChatBot 的步数 canary 原写 `baseline_steps: 1`，真机恰好
   `tools=1` → `1 <= 1` → **恒绿**。改法是让声明与被测方必须做的事互相矛盾
   （`baseline_steps: 0` 配"必须用 bash 执行"的题面），而不是期待 agent 表现差。
8. **会撞安全硬门的 canary 单独一跑**（同上）。`security.max_failures: 0` 在每档
   gate 里都是 Hard Gate；一条故意违规的安全 case 留在默认 run 里会让那条硬门
   **永远红**，报告上"安全规则被真实触发"与"这是一条撞线 canary"不可分辨，而
   后者只能靠人工记忆解释——硬门就退化成装饰品。同类 case 摘掉默认套件的标签，
   由 `--suite security` 单独执行（`database-core` 的既有分法），并在套件选中侧的
   测试里逐条登记例外 + 校验它真的被目标套件选中（区分"刻意排除"与"忘了挂标签"）。
9. **断言不得把被测方的自由度写进去**（2026-09-30，C 类第九轮实测）。当被断言的
   值由**模型**（而不是平台或配置）决定时，同一条 case 的两次运行会给出不同结果，
   报告上呈现为被测对象失败——实际是用例把模型的自由度当成了确定性事实。
   先例：`subagent.started` 的 `name` 来自 `data-sub-open.agentType`，即"模型填不填
   那个可选参数"的后果（不指定：9 次里 6 次 `auto`、3 次 `fullstack-engineer`；
   题面显式要求后：8 次全一致）。`chatbot.subagent.delegation` 首版提示词没钉它，
   第二次全量跑里同一条 case 给出不同 name，判成 `harness.subagent_routing` 失败。
   **修法是让题面钉死被断言的值**（并顺势把 `allow_extra` 收成集合恰好相等），
   不是放宽断言、也不是靠 `allow_extra` 兜住随机性——放宽只是把"不确定"藏进阈值，
   判别力跟着一起没了。机械护栏：
   `tests/test_chatbot_dataset.py::TestExpectationsArePinnedByThePrompt` 要求每个
   非 `auto` 的期望值都**字面出现**在题面里。推论：期望里出现 `auto` 这类"未指定"
   哨兵本身就是缺陷信号——它断言的是模型的沉默，不是平台的行为。
10. **judge 指标进档要同时成立三件事**（2026-09-30，C 类第二版实测）。加一条
   `agent.*` 到 profile 不是"多一个信号"，它会改变三处行为：
   **(a) 同一条 case 的其它档必须还是纯确定性基线**——judge 的分数只有在"确定
   性结论已知"时才能被解读（是 agent 真没做完，还是判分器口味问题）；基线档里
   混进 judge，参照系本身就随模型漂移。机械护栏：基线档**不得**含 judge 指标
   （`test_first_version_profiles_declare_no_judge_metrics`），且 judge 档
   **必须**真的含（否则"第二版已交付"只是名分，跑出来与基线档一模一样）。
   **(b) 阈值抄先例值，不自造第二套数字**：同一指标在两档里阈值不同，跨档比对
   （"这次分数掉了"）就没法区分是阈值差还是质量差。先例取 `nightly`。
   **(c) 输入缺口要落 skipped，不能落 error**：判分器要的分量不在这次观测里
   （`tools_called` / `expected_tools` / 非空 `actual_output`）是 Spec §19.1.1 的
   第三结局。按 error 处理会把整条 case 变成 EVALUATION_FAILURE、把 run 拉到
   exit 2，而 agent 什么都没做错——**一次已经由 case 自己声明的超时**（收尾输
   出为空）尤其容易踩到。先例：`chatbot-core` 16 条里 6 条不声明 `tools.required`、
   7 条没有 `expected.output`；判据集中在 `deepeval_adapter._is_missing_input`，
   结局在 `runner._run_judge_metrics`，反方向（判分器真坏仍是 error → exit 2）
   也必须有断言。
11. **"永不失败的断言"与"永远判不出的指标"是两种假覆盖，各自要有护栏**
    （2026-09-30，C 类全覆盖勘察）。两者都不报错、都不变红，只是占着"已覆盖"的名分。

    **(a) 声明了却永远绿的断言**——比缺这条断言更糟。实例（真实测到）：
    `chatbot.security.compliant.baseline` 声明过 `security.forbidden_sql`，
    而该规则只读 `call.arguments["sql"]` / `["query"]`，本 SUT 的 SQL 走
    `bash {command: …}` → 命中集**恒空** → 这条安全规则永远是 pass。
    同一缺口此前已对 `native.sql_result` 下过禁令（"观测面不存在 → 不声明"），
    `forbidden_sql` 是漏网的第二处。**判据**：一条断言的生命周期里，有没有一个
    输入会让它变红？没有就别写，护栏要按"同一观测面缺口的所有下游断言"一起查
    （`test_forbidden_sql_is_not_declared` 与 `test_sql_result_is_not_declared`
    是同一条纪律的两个落点）。

    **(b) 恒 skipped 的 profile 条目**——它不是假绿（没判过就是没判过，
    Spec §19.1.1），但会让"这档覆盖了 N 个指标"的 N 虚高。两个来源：
    **观测面缺口**（SUT 在 `/health` 把事件声明为 false）与**声明缺口**
    （事件面可用，但区分性参数从未被任何 case/profile 声明，插件按
    "未声明即不判"自跳过）。第二个来源尤其阴——既有护栏只覆盖第一个，
    实测 `harness.mcp_permission` 在 `chatbot-core` 16 条 × 3 档里恒 skipped
    而三套护栏全碰不到它。**处置**：维护一份"恒 skipped 指标 → 理由"登记表，
    并要求它**双向**成立（未登记的空转 → 红；登记了却其实可判 → 红，防登记过期；
    `TestEveryProfileMetricCanActuallyJudge`）。

## 断言有效性不变式（2026-09-23 review #I01–#I04 的教训）

评测平台最严重的缺陷类别是"声明了却不算数的断言"。守住三条：

1. **全部挂载点参与判决**：case 级 `expected`、session 级 `expected.final`、turn 级
   `expect` 的判定都必须进入 `CaseRunResult.all_metric_results`，由 `blocking_failed`
   统一消费。新增挂载点时必须同时接进这个聚合视图。
2. **没有观测来源就不得评测**：观测切片 `EvalScope` 只放真正能观测到的量。
   尚未实现的声明必须在启动期 `scan_unsupported_assertions` fail-fast（exit 3），
   绝不允许落进"默认值恒 pass / 恒 fail"的分支——两者都会污染 Gate 结论。
   **已实现但本次观测不到的**走第三条路：判 `skipped`（blocking=False），
   见下面的"断言扩展约束"第 2 条。
3. **新增断言词汇的步骤**：先在 `native.py` 实现 checker + 加入
   `IMPLEMENTED_EXTENSIONS`/约束实现面，再补 `unsupported_declarations` 的放行，
   最后补正向+边界测试。顺序反了就会复现"声明了却静默失效"。
4. **不要发明隐式基线**：需要"理想值"的连续型断言（如 `step_efficiency`）必须由
   Case 显式声明基线。从 `tools.required` / `constraints.max_tool_calls` 反推理想步数
   会把"必须调用"读成"只应调用"，误伤多轮 case。判据与算式在 Spec §11。

## 源文件行尾（Windows 环境硬约束）

源码必须是 **LF**。本机 ruff 与文件读取工具都会拒绝 CRLF 的 `.py` 文件
（报 `E902 stream did not contain valid UTF-8`，且 Read 工具无法打开）。
凡是经过 python `write_text()` 重写的文件会变成 CRLF——因此：
- **禁止**用 Bash 里的 python 脚本改写源码文件，一律走 Write/Edit 工具；
- 写入后若 ruff 报 E902，先查行尾（`file` 或统计 `\r\n`），删文件用 Write 重建；
- 提交前 `uv run ruff format --check . && uv run ruff check .` 必须双绿
  （`docs/` 已从 ruff 范围排除：内嵌 Python 片段是契约文本，不是待格式化代码）。
- 同一条规则适用于 `web/`：`.tsx` 源码同样必须 LF。用 Bash 的 python 脚本改写
  前端文件会复现同一个损坏（Read/tsc 都会失败），改前端也走 Write/Edit。

## 本机构建怪癖（重要）

uv 的通用 PEP 517 桥接子进程在本机损坏（`stream did not contain valid UTF-8`）。
pyproject 已配置 `uv_build` 原生构建后端绕开。**不要**把 build-backend 改回 hatchling/setuptools。

**个别源文件的读取器分叉（2026-09-29 发现）**：`api/routers/runs.py`、
`evaluators/harness.py`、`reports/aggregate.py` 三个文件在部分读取器（ruff 直读、
Read 工具、certutil）眼里是坏字节（`E902 stream did not contain valid UTF-8` /
"Unsupported or binary text encoding"），而 python / git / cat 读到的字节完好
（与 HEAD 全同、UTF-8、LF、经 stdin 喂给 ruff 全绿）。certutil 两次哈希不一致，
说明是**读取期**非稳定损坏（磁盘/过滤驱动层），不是内容问题，`git checkout`
与删除重建都修不掉。处置：这类文件的内容校验走
`cat <file> | ruff check --stdin-filename <name> -`（cat 阵营读到的字节可信）；
`ruff check .` 的 E902 若只落在内容已验证的文件上，不视为回归。若范围扩大，
先跑 `chkdsk` 再怀疑代码。

**2026-09-30 补记（同一现象的第二种成因，两者要分开）**：走 Write/Edit 工具改过的
`runner.py` / `harness.py` / `test_runner.py` 等文件**真的变成了 CRLF**（
`b.count(b"\r\n") == b.count(b"\n")`，全文件每一行都是 CRLF），与上面的"读取器
分叉"不同——这次是内容问题，`git diff` 会显示"整文件重写"。修法是把字节里的
`\r\n` 换成 `\n`（只对全 CRLF 文件做，断言无裸 LF）而不是用 Write 重建（重建会
再走一遍同一个转换）。判别口诀：**全 CRLF 且有 `\r\n` = 内容问题，可修；
python/git 读到 LF 而 ruff 报 E902 = 读取期问题，别动它。**

## 外部接入约束（2026-09-29，change-plan A2/A3/B2 的固化）

外部 SUT 经转译 shim 接入后，"观测面透传"的漏传来源从内部 runner 变成了接入方。
三条硬约束把评审结论固化成条款：

1. **外部映射必须逐观测面拆分**。MCP 调用必须转成 `mcp.call` / `mcp.result`，
   独立命令必须有 `command.*` 事件且带 `exit_code`——把 MCP/command 折叠进
   `tool.*`，`forbidden_mcp` / `forbidden_command` / `harness.mcp_permission`
   全部恒 pass（`security/evaluator.py` 要求三路逐条透传的注释里写着教训）。
2. **未被 SUT 观测能力覆盖的 metric 必须显式处置**：能力经 health 上报
   （`observation_surface`，事件名 → bool）并在 `metric_capability_snapshot`
   以 `event:` 前缀留痕；依赖缺失观测面的插件以 `required_events` 声明、由
   `run_plugin` 判 skipped（`observation_unavailable`）。**不得静默 pass，
   也不得靠删 profile 条目掩盖**——删了之后报告分不清"不打算测"与"忘了配"。
3. **"外部数据缺失"的判定必须与 `max_cost` 同向**：依赖的分量未观测即
   skipped（`ObservationUnavailable`），禁止坍缩为 0 后参与比较。`tokens: int`
   缺省 0 让 `max_tokens` 的 `0 > limit` 恒假 → 恒 pass，与 `cost: float | None`
   的不对称不是设计选择，是遗漏（修复见 `native.py::_check_constraints` 的
   分量级观测标志）。同向的推论：口径（哪侧被观测）要进 `RunMetadata` 并参与
   基线守卫，否则口径漂移会被伪装成回归。
4. **closing 必须挂在 opening 的 `event_id` 上，且不得替平台编造终局**（2026-09-30，
   C 类第九轮）。§8 的配对规则是 `parent_span_id == opening.event_id`
   （`trace/builder.py::_close_span`），不是"挂 root 就行"：挂错等于孤立 closing，
   被静默忽略，span 永不闭合，`run.finished` 统一收口判 `error` /
   `span never closed (stream ended)`——**一次成功的调用在报告里呈现为失败**。
   先例：`subagent.finished` 原先挂 root，同步路径一直这么错着，只在真机 dump 重放
   时才暴露（下游 metric 只读 `span.name`、用例又不断言 span 状态，两层都看不见）。
   同源的第二条：**父流没有观测到的事实不许合成**——`data-sub-async`（异步委派受理）
   只登记、在 `run.finished` 之前关闭并如实写 `status="submitted"`，不合成
   `data-sub-done`；合成等于替平台宣布一个尚未发生的结果，而异步终态
   （`status:"completed"`）要另查 `GET /api/chat/subagent-status`。终态查得到但
   **不进 §8 流**：词汇表里没有承载它的位置，硬塞就是撑大框架词汇表（E5 反模式）。
5. **配置缺口不得静默降级**（同上）。shim 侧每新增一个"可选配置"（如
   `ask_user_question` 的答案注入 `--answers-file`）都要先回答"配置没覆盖到会怎样"：
   默认必须**带自述原因失败**（`shim answers config incomplete: …`）且不发起第二次
   POST。缺口放过去之后红的是 agent 的行为（它没收到答案，自然不会照答案做），
   报告里看不到真实原因——与"超时归属必须唯一"同源。探索性运行提供显式降级档
   （`--answers-policy partial`：省略该答案 + stderr 告警），**降级必须是选择，
   不能是默认**。附带判定：平台**不校验**注入载荷的键名（写错的键被原样回显、
   静默退化成"没收到答案"），所以归一化是 shim 的责任——三种写法（问题全文 /
   header / 序号）统一成实测可用的**问题全文**再发。
6. **"丢进 §8 流"与"可以不留档"是两件事**（2026-09-30，第十一轮真机实测）。
   归一化纪律容易读成"方言一律丢弃 = 可以不看"，但有一类方言的用途不是**报告**，
   而是**回话**：续跑要重建 assistant 消息时缺了它，模型侧直接拒收整条消息。
   先例：`reasoning-*` 被当方言整族丢弃后，`ask_user_question` 的续跑 POST 恒定以
   `The reasoning_content in the thinking mode must be passed back to the API` 收场
   （四次独立运行全中），形态是**一次成功的提问在报告里变成 agent 崩溃**。
   判据：丢一个事件类型之前先问"它是不是**平台侧协议**的一部分（请求体里要回传的
   东西）"，而不是只看"§8 词汇表里有没有它的位置"。两者都否才可以丢。
   同类推论：改这类"留档"代码要顺带核对**同一批状态量的粒度**——同一次改动暴露了
   part 文本取整步累加值（`_step_text`）的既有 bug，一个 step 里有第二个 part 时
   会被记成"前一个 + 自己"，续跑重复回传文本。实测 dump 里每 step 恰好一个 part，
   所以它从未显形；**"没显形过"不等于"没有这个分支"**。
7. **`--timeout` 一类的全局预算覆盖必须是下限**（2026-09-30，第十二轮）。覆盖开关的
   用途只有一种：真实 SUT 比确定性脚本慢一个数量级，整体放宽。若它能**压小**预算，
   它就成了一个顺手改掉整份数据集声明的开关，而"这条 case 该给多少预算"是用例作者的
   判断。先例：`--timeout 5` 把 `error.recovery.timeout`（`timeout: 1` + `[slow]`
   脚本 sleep 3s，**这个矛盾就是断言本身**）从红转绿。修复连带两条纪律：
   (a) 语义只在**一处**规定，不在两层各写一套优先级（轮级=替换、case 级=下限这种
   组合会让读代码的人无法判断该信哪条）；
   (b) 覆盖的落点要**覆盖到每一层**——此前只改 session 总额、`_run_turn` 又自己写一遍
   `case.execution.timeout`，单轮 case 的覆盖是空头承诺。
   下限**挡不住**的情形要如实登记，不要假装解决了：预算本身作为断言的那些 case
   （超时 canary）在更大的覆盖值下必然被抬过去；处置与安全 canary 同法（不进默认套件）。

8. **全局换档开关不得改写用例作者写下的阻断意图**（2026-09-30，C 类第二版）。
   `RunConfig.profile`（CLI `--profile`）的优先级是
   `cfg.profile or case.evaluation_profile or benchmark.default_profile`——
   运行期开关**压过** case 自己的声明。而 `blocking` 归 Profile（Spec §17.2），
   于是"负向用例必须真的红"这条约束可以被一个命令行参数整份解开：实测
   `--profile chatbot-judge` 跑 `chatbot.skill.load.negative` 时，
   `harness.skill_load` 判 FAIL 而 blocking=false → case 状态 **PASS**，
   恒绿的负向用例又回来了（与首跑 `run_d487591757ca` 同一类，入口从"默认档
   选错"换成"运行期整份覆盖"）。两层处置：**(a)** 判据档按 case 选
   （`evaluation_profile`），不用 `--profile` 全局压；**(b)** 默认档必须是纯
   确定性基线，且负向用例的 harness 期望必须落在 blocking=true 的档上——
   两条都做成机械护栏，因为"记得别这么跑"不是约束。
   与第 7 条同源：**一个能整份覆盖用例声明的开关，都要问"它会不会顺手把某条
   断言消解掉"**，并把挡不住的情形如实登记（不进默认套件 / 按 case 选档），
   不要假装语义能修掉它。

9. **接入侧的观测面声明只写事实，不写愿望**（2026-09-30，C 类合规基线修正）。
    `observation_surface` 是"这个 SUT 能不能观测到"的**实测结论**，它的消费者是
    判定逻辑（`run_plugin` 据此判 skipped）。因此两类错都禁止：
    **为了让某条指标有输入而把 False 改成 True**（那就是伪造观测面），以及
    **用一句过于笼统的话覆盖掉已知例外**。"provider 重试只写日志不上协议流"就是
    后者：模型层重试确实不上流，但连接器工具有一条 `connector.call.retrying`
    （经 `data-connector-event` 落到父流）。声明值不变（那条是**工具级**重试，
    `harness.retry` 判的是模型层），但口径必须写准，并说明"要覆盖它得新增一条
    工具级指标，不能靠改这条声明"。同理：**shim 丢弃的每一种流上方言都要逐条
    登记**（`translator.feed` 的 fallback 注释是那份登记表）——静默落在"…"里的
    方言，是下一个人唯一无法从代码看出来它会丢什么的地方。

框架通用性的机制保障（change-plan E 类）落在 `tests/test_framework_boundary.py`：
专有方言 deny-list 扫 `src/agent_eval/`、`SessionContext.extra` 保持不透明。
往框架里加平台相关分支/字段/事件名之前先过那条测试与它的判断标准：
**这个标识在换一个 SUT 之后还成立吗？**

扫描面口径（2026-09-30 复核，四处收口）：**大小写不敏感**（`AI-Chatbot` /
`conversation_id` 这类写法差异原本能溜过去，而写 Python 的人更可能写下划线形式）、
**覆盖包内全部文本文件**（含 `reports/templates/*.j2`，此前只扫 `*.py`）、
**清单按"同族成组"登记**（`data-sub-` 与 `data-connector-event` /
`data-permission-mode` / `data-queue-status` 同族，只登记一半等于留后门）、
**并自检扫描面非空且含非 .py 文件**（防止 glob 写错后"零命中"是假绿）。
注释里曾写"dev/ 豁免"而代码从未豁免——按**更严的一侧**收口（继续扫 dev/），
并在测试里补了一条"变体写法必须命中"的对照用例，防止匹配逻辑被改回大小写敏感。

## 示例数据集约定

`evals/` 下的 smoke suite 必须对 fake:// 与 mock server 保持确定性 PASS（作为回归基线）；
负向用例（`negative` tag）只允许进入 core suite，用于演示 exit 1 路径。
