# Location Verification Agent：基于 LAND-DATA 的 Harness 更新计划

> 状态：设计与实施计划，初版 2026-10-27，2026-10-28 增补改前／改后评测。本文中的“拟实现”均不代表当前 LAND-DATA 已完成。参考 [Learn Claude Code 当前 s01–s17 主线](https://github.com/shareAI-lab/learn-claude-code/blob/main/README-zh.md)；旧版 `agents/` 和 `docs/` 是另一套章节编号。

本计划的实现、运行与量化对象是 **LAND-DATA Python/SQLite 项目**。相邻的 LAND iOS App 不构成 Location Verification Agent 的部署或真机评测；简历中的 Agent Harness 指标必须来自 LAND-DATA 的可回放运行与同条件前后比较。

## 目标与现有基础

把 LAND-DATA 已有的内容采集、地点提取和人工审核，延伸为一个能处理**单条地点争议**的调查 Agent。输入一条地点记录和待核验的问题，模型在有界工具池中选择下一项证据；Harness 保存状态、执行工具、校验结果、控制权限和预算，并在证据不足时交由人工。

LAND-DATA 当前本地代码已有多来源采集、规则优先的候选分流、可选的通义千问抽取、SQLite 状态、输入哈希缓存、来源记录、人工审核和发布前检查。这些是可复用的基础。具体实现可从 [`local_classifier.py`](../src/land_data/local_classifier.py)、[`xhs_processing.py`](../src/land_data/xhs_processing.py)、[`llm_extraction.py`](../src/land_data/llm_extraction.py)、[`sqlite_storage.py`](../src/land_data/sqlite_storage.py)、[`review.py`](../src/land_data/review.py) 和 [`place_release_bridge.py`](../src/land_data/place_release_bridge.py) 核对。知识抽取路径要求显式选取候选并设上限；不同入口的 `--limit` 含义不同，不能概括为全系统统一 API 预算。**按证据缺口动态选择核验工具、连续更新地点声明、独立判断调查是否完成**是本计划新增的能力。先前 10 篇真实帖文中 17/19 个地点提及被抽取的实测仅评价“提取”，不能作为“地点已核验”的准确率。本地未提交的功能也不能当作公开仓库已有成果介绍。

## 一个可演示的垂直案例

用户报告某咖啡馆疑似搬迁。旧地图记录仍指向 A 地址，另一来源显示同名门店在 B 地址。

1. 系统先拆出声明：`门店身份`、`当前地址`、`坐标`、`营业状态`。每条声明标记为已支持、已反驳、证据不足或冲突。
2. Harness 读取已有 POI 与来源记录，做确定性去重、同名分店初筛。模型看到当前缺口和可用工具，再选择查门店官网、地图候选或地理编码；每个动作写出预期要解决哪条声明。
3. 工具返回结构化结果，附 URL、采集时间、原始页面日期、被支持的声明、门店身份线索和证据位置。模型提出解释；校验器检查证据是否指向同一家店、是否过期、是否源自同一转载链。
4. 当近期官方搬迁公告与新地址身份信息一致时，输出“建议修正”，附地址和坐标的修改提案。若新旧门店身份仍无法区分，或近期可信来源冲突，则输出人工审核包。
5. Agent 没有发布权限。建议修正经现有审核和发布流程处理。

## 最小运行契约

`VerificationCase` 保存目标 POI、待核验字段、预算、状态和当前目标；`Claim` 保存字段、候选值、支持/反驳状态；`Evidence` 保存来源、时间、独立来源组、证据片段或定位、关联声明和内容哈希；`ActionRecord` 保存模型选择的工具、参数、预期价值、实际结果、耗时和调用成本。大型原文存外部文件或已有 SourceContent，消息中只留短摘要和可回读引用。

工具池第一版只需 `lookup_existing_poi`、`search_official_source`、`search_map_candidates`、`geocode_or_reverse`、`fetch_evidence`、`propose_correction`、`handoff_to_human`。每个工具有固定 schema、超时、错误类型和最大输出长度。地图候选或地理编码可以验证身份与位置关系，不能单独证明仍在营业。公共网页内容一律作为待检验的数据，不能指挥 Agent。

运行循环：

```text
读取 case 和可恢复状态
→ 确定性预检与候选声明
→ 模型从可用工具中选择下一步，并说明待解决的缺口
→ 权限与预算门检查工具调用
→ 执行、记录、提取证据并更新声明
→ 独立 Goal Gate 判断：继续 / 核验通过 / 建议修正 / 人工审核
```

模型可以在内部估算“预计减少的不确定性 × 对结论的重要性 − 调用成本 − 延迟”，用来比较动作；Harness 只硬性约束权限、预算和完成条件，不把地点调查写成一棵固定决策树。示例试点可设每案最多 6 轮模型决策、8 次外部读取、2 分钟总时长；这些是**待评测调参的初始上限**，不是当前系统指标。

Goal Gate 与选工具的模型分开。它先用确定性检查确认关键声明有对应证据、证据属于同一门店、来源时间和独立性合格、没有未解决的重大冲突；必要时再用独立评估者解释边界案例。若模型提议停止但缺少关键证据，继续调查；若已无有效动作或预算耗尽，交人工。外部工具不可用单独记为可重试失败，不当成“地点不存在”。

## s01–s17 对应关系

下表覆盖教程的 17 个机制。`核心`表示这个地点核验案例需要；`条件启用`表示已有明确触发场景后再增加。章节含义以仓库根目录当前课程为准。

| 课程 | 在地点核验中的对应实现 | 优先级 |
|---|---|---|
| [s01 Agent Loop](https://github.com/shareAI-lab/learn-claude-code/tree/main/s01_agent_loop) | 一轮模型决策、一轮工具结果，直到提出结论或交接 | 核心 |
| [s02 Tool Use](https://github.com/shareAI-lab/learn-claude-code/tree/main/s02_tool_use) | 统一注册地图、官方页面、地理编码和现有 SQLite 查询工具；新工具只扩展注册表 | 核心 |
| [s03 Permission](https://github.com/shareAI-lab/learn-claude-code/tree/main/s03_permission) | 工具执行前检查来源白名单、读取权限、调用额度；修改和发布须经人工 | 核心 |
| [s04 Hooks](https://github.com/shareAI-lab/learn-claude-code/tree/main/s04_hooks) | 调用前校验 schema 与预算，调用后记录耗时、来源、失败和结果哈希 | 核心 |
| [s05 TodoWrite](https://github.com/shareAI-lab/learn-claude-code/tree/main/s05_todo_write) | 每案生成可更新的声明/待查缺口清单，保持调查方向明确 | 核心 |
| [s06 Subagent](https://github.com/shareAI-lab/learn-claude-code/tree/main/s06_subagent) | 多个独立来源需深查时，让官网核验和地图身份核验在隔离上下文中工作，返回有证据的摘要 | 演示必做，生产按需 |
| [s07 Skill Loading](https://github.com/shareAI-lab/learn-claude-code/tree/main/s07_skill_loading) | 按地点类别和地区加载核验规则，如连锁分店、博物馆或官方园区；只先展示目录 | 增量，演示必做 |
| [s08 Context Compact](https://github.com/shareAI-lab/learn-claude-code/tree/main/s08_context_compact) | 转存冗长工具结果，保留证据引用和近期结论；仅长案触发消息压缩 | 增量，演示必做 |
| [s09 Memory](https://github.com/shareAI-lab/learn-claude-code/tree/main/s09_memory) | 跨案复用稳定的来源规则与已确认别名；动态营业状态带时间戳和失效策略 | 增量，演示必做 |
| [s10 Task System](https://github.com/shareAI-lab/learn-claude-code/tree/main/s10_task_system) | 将案子拆成身份、地址、营业状态等可持久化任务，记录依赖与断点 | 核心 |
| [s11 Background Tasks](https://github.com/shareAI-lab/learn-claude-code/tree/main/s11_background_tasks) | 慢速外部查询并发执行，完成后通知同一案子继续；避免重复启动 | 演示必做，生产按需 |
| [s12 Cron Scheduler](https://github.com/shareAI-lab/learn-claude-code/tree/main/s12_cron_scheduler) | 对过期或高变动地点定期重新核验，沿用原证据并记录新旧差异 | 演示必做，生产按需 |
| [s13 Agent Teams](https://github.com/shareAI-lab/learn-claude-code/tree/main/s13_agent_teams) | 批量、复杂案件才设来源调查队友和统一裁决者；任务认领与结果交接须有协议 | 演示必做，生产按需 |
| [s14 MCP Plugin](https://github.com/shareAI-lab/learn-claude-code/tree/main/s14_mcp_plugin) | 地图/地理编码/官方网页服务经适配器接入同一工具池；按权限发现能力 | 增量，演示必做 |
| [s15 Integrated Harness](https://github.com/shareAI-lab/learn-claude-code/tree/main/s15_integrated_harness) | 把工具、权限、上下文、任务、审计和人工队列接到同一案子的执行循环 | 核心验收 |
| [s16 Workflow Runtime](https://github.com/shareAI-lab/learn-claude-code/tree/main/s16_workflow_runtime) | 对批量入库预检、审核包生成等固定步骤使用有 journal 的可恢复流程；单案下一步仍由模型选择 | 演示必做，生产按需 |
| [s17 Goal Loop](https://github.com/shareAI-lab/learn-claude-code/tree/main/s17_goal_loop) | 模型想停时，独立检查关键声明是否有足够证据、预算与权限是否满足；不满足则继续或转人工 | 核心 |

其中 s05 是**本案当前调查计划**，s10 是**跨会话可恢复的任务状态**；s06 是**单个短时子任务**，s13 是**持续协作的队伍**；s08 管**对话长度**，s09 管**跨案知识**；s16 管**事先确定的编排**，s01 管**取决于新证据的下一步**；s17 检查**目标是否实际完成**。这些边界也是面试时最能体现理解深度的部分。对应概念见[课程总览](https://github.com/shareAI-lab/learn-claude-code/blob/main/README-zh.md)及 [s16](https://github.com/shareAI-lab/learn-claude-code/blob/main/s16_workflow_runtime/README.zh.md)、[s17](https://github.com/shareAI-lab/learn-claude-code/blob/main/s17_goal_loop/README.zh.md)。

为做到“几乎每课都能实际演示”，准备两个互相关联的场景：**单案调查**展示 s01–s10、s14、s15、s17（两条独立线索触发 s06，长页面触发 s08）；**批量复核**展示 s11–s13、s16（后台查询、到期重查、团队认领、固定流程续跑）。生产版只启用有真实收益的模块，但教学演示要保存每个机制的输入、执行记录和可验证结果。

## 改前／改后量化评测：先冻结基线

**开始实现动态调查循环之前，先确定评测协议、收集金标准并运行改前基线。** 已有的 10 篇帖文／19 个地点提及评测和分类器合成样本可作为旧功能回归测试，不能充当地点核验基线，也不能与后续核验准确率相减。

- **样本量与分组。** 目标至少 **500 个不同真实地点的核验案件**，其中 100 个开发集只用于调 Prompt、阈值和预算，400 个锁定测试集不参与调参。测试集包含 200 个按预先记录规则从待核验队列抽取的日常案件，以及 200 个有意补齐的困难案件：搬迁／停业、同名分店、旧页或相互冲突来源、地址／坐标不一致各至少 50 个。两部分分别报告，不能把困难样本的比例当成真实业务发生率；同一门店、同一事件及其重复来源快照不得跨开发集和测试集。若当前可用、合规的真实数据不足，就继续采集与标注，不以 10 篇帖文或合成案例凑数。
- **金标准与证据快照。** 每案人工标注门店身份、地址、坐标、营业状态等字段及 `维持原值／建议修正／证据不足需人工` 结论，允许“不确定”，记录判断所依据的原始来源、页面时间和采集时间。全部困难案件及日常案件的随机 20% 做独立复核，分歧仲裁；若暂时没有第二位标注者，如实标记单人标注，不把它称为双人金标准。将可用网页和工具结果冻结为有哈希的回放快照，使改前、改后面对同一时间点的证据；另设小规模实时试跑检查外部有效性，不混入离线前后分数。
- **两个改前对照。** ① 记录 LAND-DATA 现有流程对这些案件实际能输出什么、多少需人工，以及审核耗时；它尚无动态地点核验能力，缺失的核验结论记为“未覆盖”，不能算正确或错误。② 在 Agent 改造前先实现并冻结一个**规则＋固定工具顺序**的实验对照，使用与 Agent 相同的只读工具、输入快照、模型版本、调用与时间预算、输出格式；主要用它隔离“模型按证据缺口选下一步”的收益。实验对照不是声称现有产品已具备的功能。
- **主指标与安全约束。** 在锁定测试集上报告：自动结论的字段级正确率及错误自动通过数／分母，安全约束下的自动处理覆盖率，必要人工移交的召回率和不必要移交率，每案人工审核分钟数，以及证据链完整率（来源、时间、定位、独立性）。同步报告每案模型／外部工具调用数、平均与 P95 耗时、可归因成本。所有准确率、覆盖率分别列日常集和各困难组的分母及置信区间；不能只汇报有利的总体平均。重大误判逐案复盘，在开发集调整后须换新的锁定测试集重新验证。
- **同条件前后比较。** 同一案件配对运行固定顺序对照和动态 Agent，预先固定工具权限、最大轮数、缓存策略、证据有效期、停止阈值与评测脚本；改后不允许用更新的网页答案或更多调用额度获得优势。对覆盖率和错误率报**百分点差**，对审核时间、调用量、成本报绝对值与相对变化；基线为零时不算“提升百分比”。保留每案轨迹和失败案例，计算配对置信区间，不用少量示例推断整体提升。
- **Harness 专项测试另列。** 至少 100 条可重复的故障／对抗情景，覆盖转载伪独立来源、分店混淆、过期页面、恶意网页指令、工具超时、预算耗尽、进程中断后恢复，以及模型过早声称完成。检查权限拦截、Goal Gate、人工移交和恢复成功率；这组可以含注入样本，但不能混入“真实地点核验准确率”的 400 案分母。比较固定顺序与动态选择之外，再做无独立 Goal Gate 的消融，明确安全提升由哪一层带来。

改前基线的交付物应包括：代码与配置版本、样本来源与抽样规则、标注规范及分歧记录、证据快照哈希、可重跑脚本、逐案输出和汇总报告。**只有这些冻结后才开始能影响核验结果的 Agent 改造。** 500 案目标是计划中的评测规模，不是已完成的样本或成绩。

截至 2026-10-28，已完成[改前资产盘点与预基线](../evals/location-verification-baseline-2026-10-28.md)：45 条合成路由样本测得 41/45；改前 334 项离线测试通过；51 条历史地点审核记录通过结构回放；146 个代码／配置／评测资产文件及两个 SQLite 数据库副本已做本地可校验快照。经污染审计排除历史社交审核记录后，从快照确定性抽取 1,000 条未标注筛查队列；另预注册 120 条合成 Harness 故障用例，尚未对 Agent 运行。对其中 790 个 NYC 官方数据候选完成了[同源时序一致性测试](../evals/location_verification/temporal_official_check/temporal-official-summary-integrity-v2.md)和[NYC DOT 跨来源字段一致性测试](../evals/location_verification/dot_cross_source/dot-cross-source-summary.md)，用于检查证据管线和字段变化。随后在冻结数据库副本上[真实运行 LAND-DATA 旧 CLI](../evals/location_verification/land_data_cli_baseline/land-data-cli-baseline-2026-10-28.md)，并对旧流程实际生成的 12,693 条 DOHMH 候选做了[全量 DOT 跨来源诊断](../evals/location_verification/land_data_output_cross_source/land-data-output-cross-source-summary.md)：1,004 条唯一匹配且坐标可比，其中 20 条相距大于 10 米且全部被旧流程自动通过。开发期进一步发现其中 2 条跨 borough，应按身份冲突交人工；它们都不是已证实的坐标错误。**正式地点核验准确率基线与改后提升仍未运行**；下一步需逐案筛查、封存独立 Gold 与可回放证据，并完成同条件固定顺序对照。

每次继续本计划中的开发、取证或评测时，同步更新本地 `docs/location-verification-evaluation-interview-notes.md`（个人笔记，不随公开仓库发布）：记录本次问题、采用的方法、输入和证据、实际完成的结果、最初的错误及定位修正过程，并给出可用于面试且不超出证据的表述。不能把计划、筛查、同源一致性或合成故障资产写成已完成的地点核验准确率。

在正式 Gold 完成前，已额外建立[隔离的 Harness 开发期最小闭环](../evals/location_verification/harness_pilot/README.md)，只用于验证决策、权限、预算、证据组、checkpoint 和 Goal Gate 的工程契约。它不写入旧流程、不读取锁定 Gold，也不替代本计划要求的固定顺序对照或真实地点准确率评测。

2026-10-28 代码进展：新增 `location-verify-prepare-db`，可直接从 LAND-DATA 现有业务 SQLite 按候选 ID 只读生成哈希锁定的 Case 和三项本地证据工具（候选字段、来源元数据、已关联 POI 上下文）；后两项只给调查上下文，既有自动通过或 POI 关联不会被升格为独立身份事实。Qwen 决策输入现列出各声明的来源组、独立转载链数量及当前证据缺口，Goal Gate 仍独立决定停止。对现有业务库的 100 条有坐标且已关联 POI 候选，准备和固定顺序回放 100/100 成功，100/100 因缺独立身份依据而交人工，业务 DB 文件哈希未变。人工队列可只读关联实际 `POICandidate` 状态；单案显示旧状态仍为 `AUTO_APPROVED`，表明正式审核／发布拦截尚未集成。相同输入的 Qwen 尝试在模型决策前失败，按现有故障门交人工，未得到真实动态轨迹。此结果只说明入口、快照与安全移交可运行；真实模型动态策略、正式业务 review queue 集成及 17 章演示覆盖仍未完成。


冲突到正式审核的可选桥接已实现：`location-verify-review-hold` 默认只预览，显式执行可把仍自动通过的候选及关联 POI 置为待审、关闭地图可见性并追加审核历史。它只接受明确的证据冲突，拒绝预算耗尽和模型故障；后续人工批准优先于旧 journal，普通再处理不取消待审。此入口只在临时数据库测试，尚未作用于实际业务库；已有发布包也不会自动撤回。

标注准备方面，已从 1,000 条冻结筛查队列中生成[500 条空白人工筛查输入包](../evals/location_verification/annotation_packet_500/README.md)。它尚未取得独立外部证据或人工标签，也未分配开发／锁定测试集，不能将“500 条已选”写作“500 条 Gold 已完成”。

对这 500 条的[独立来源覆盖审计](../evals/location_verification/annotation_packet_500/evidence-coverage-summary.json)显示：397 条纽约 DOHMH 中仅 24 条在当前 DOT 唯一匹配、23 条坐标可比，已知 >10 米差异一条也未抽到。因此保留此包作日常筛查入口；困难案例仍须从全量冻结输出定向补足，并按四类各 50 条逐项证明配额，不能把 500 条随机入口直接宣布为正式测试集。

已从冻结全量输出建立[155 条不重叠困难风险短名单](../evals/location_verification/annotation_packet_500/hard-shortlist-summary-v2.json)：坐标差异 50、同名多地址 50、历史来源地址冲突 50、最新官方快照缺失 5。最后 5 条仅为搬迁／停业筛查线索，不是已确认事实；该组至少还缺 45 条合格候选。现已为 155 条建立[私有、哈希可追溯的未标注证据包 v3](../evals/location_verification/annotation_packet_500/hard-evidence-dossier-summary-v3.json)，显式区分来源采集时间、快照文件时间和官方记录时间，并经递归污染审计剔除旧审核字段。v1/v2 因时间语义与字段污染问题被保留作追溯，不能用于标注。开发期 Harness 增设转载链 `lineage_key` 独立性检查，并为 1,004 条固定顺序回放中的 20 条冲突生成私有人工审核包；该阶段完整离线回归 359/359 通过。以上均不改变正式 500 案 Gold 尚未完成的状态。

用户已选择由 Codex 承担标注。当前先完成[500 条日常＋155 条困难的单一 AI 暂定证据分流](../evals/location_verification/annotation_packet_500/ai-provisional-evidence-annotations-summary.json)，逐案保留来源引用和未知项；困难组中 20 条当前坐标冲突、16 条坐标一致、119 条缺可比当前坐标，营业状态均保持未知。由于这是同一 AI 参与项目开发和标注、且大部分案件缺少独立当前事实来源，文件明确 `gold_label=null`、不分 dev／locked test，也不进入正式准确率计算。用户的选择授权我继续取证和标注，**不把 AI 判断改称独立人工 Gold**；要得到简历可用的真实地点前后提升，仍须先补足可冻结的外部证据与符合协议的独立复核或清楚披露单人／AI 标签限制。

日常组现补上[500 份逐案冻结证据包](../evals/location_verification/annotation_packet_500/daily-evidence-dossier-summary-v1.json)及[暂定标注 v2](../evals/location_verification/annotation_packet_500/ai-provisional-evidence-annotations-summary-v2.json)：24 案有当前 DOT 唯一检索行，23 案坐标在 10 米内，1 案缺可比坐标。逐案原始行已通过独立哈希反查，旧 v1 不覆盖。**这只增加证据覆盖，不增加 Gold 数或地点准确率分子。**

开发期 Goal Gate 另针对 `(0,0)` 缺失哨兵值增加失败关闭检查：即使两个独立来源都给出零坐标，也交人工而不自动核验。该回归只证明控制层约束，不计入地点准确率提升。

隔离 Harness 现已接入 `./land-data location-verify` 主命令，并用一条冻结的真实困难地点记录完成固定顺序只读试跑：2 次工具调用后因跨来源坐标冲突交人工，重复运行 journal 不变。主入口可运行，但尚未接入日常候选审核／发布工作流；模型动态策略在真实记录上的同条件对照、隐藏证据进程隔离和正式 Gold 均未完成。不得把这个开发期 CLI 入口称为生产部署。

同一实案的千问策略首次试跑因模型网络错误中断，暴露出决策异常未交接的控制层缺口。现已修为持久化 `DECISION_CLIENT_ERROR` 并交人工，复跑为 1 次失败决策、0 次工具调用；模型在真实地点上的动态选择仍未成功运行，不计入任何前后提升。

主命令现可用 `--handoff-output` 将交人工结果保存为私有、可重跑的审核包；真实冲突案与模型网络故障案均已生成，包内保留证据／动作／待答问题且不自动提出修改。此接口还没有接入正式 `review` 队列，不能把开发期审核包等同于已完成的人工审核。

开发期另有 `location-verify-queue` 只读列出 verification journal 的交人工案件与原因，避免审核包不可发现；它仍是独立工作队列视图，不是业务 `review` 状态迁移或人工裁决。

`location-verify-prepare` 现能按案件 ID 从 500 日常／155 困难的冻结证据包生成受控 Case 与只读工具清单，校验文件和逐行哈希、拒绝旧答案字段；已在 1 条冲突案和 1 条坐标完全一致但身份未确认的日常案上完成主命令回放，两案均安全转人工。该适配器使 Harness 可复用到其他已冻结地点，但当前仍只接入入口与当前 DOT 两种工具、未接入线上采集或正式业务 `review` 状态。

当前适合简历的表述另存于本地 `docs/location-verification-resume-current.md`（个人草稿，不随公开仓库发布）：保留原有“城市内容挖掘与地点信息提取系统”标题，明确区分已运行的 1,004 条固定顺序冲突分流、合成模型烟测和仍待完成的正式准确率。

## 实施顺序与验收

1. **先完成改前测试和数据契约。** 盘点 LAND-DATA 现有状态、审核和 provenance；定义 `Case/Claim/Evidence/Action` schema、来源时间语义和人工交接格式。按上节协议收集、标注并冻结至少 500 个真实案件，在锁定测试集运行现有流程与固定顺序实验对照，保存逐案轨迹和基线报告。样本未齐时可以做开发集试跑，但不能把它写成正式前后提升。
2. **交付最小闭环。** 用“疑似搬迁咖啡馆”跑通 s01–s05、s10、s17：至少三种只读工具、一次动态选择、SQLite 断点、权限与预算、证据链、独立停止检查、人工审核包。每次重跑不重复消耗已完成的工具调用。
3. **处理长案和外部服务。** 按需要加入 s07–s09、s11、s14：按需加载地区知识，压缩长工具结果，召回有有效期的知识，接入有界外部工具及后台结果通知。
4. **完成 17 课演示覆盖。** 用第二个场景加入 s06、s12、s13、s16，和第三步的 s11 一起展示并行调查、后台通知、到期重查、任务认领与固定流程续跑；用 s15 统一运行和观测。定时任务只重新打开过期案子，不自动发布结论。是否在生产路径常开，由实际负载和评测结果决定。
5. **运行改后测试与消融。** 在同一锁定测试集和证据快照上配对比较固定顺序对照与动态 Agent，另跑现有流程的人工成本对照及 100 条 Harness 故障情景。按上节口径报告错误自动通过率、自动覆盖率、人工移交、证据追溯、调用次数／时延／成本和断点恢复；检查模型过早停、转载伪独立来源、恶意工具结果、分店身份未定与预算耗尽等失败案例。若测试集因调参被使用过，重新封存未见案例后再给出最终数字。

完成标准不是把 17 个名词写进代码，而是能展示一条完整轨迹：每轮为什么查、查到了什么、状态怎样改变、为何停止或交人。当前 LAND-DATA 更准确的简历标题是“城市内容提取与审核系统”，项目要点可写其工具约束、状态、来源和人工接管等 Harness 机制。动态选工具和独立停止检查真实落地、经过上述评测后，再将新增模块称为“Location Verification Agent Harness”。
