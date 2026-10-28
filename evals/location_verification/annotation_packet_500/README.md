# 500 条 LAND-DATA 人工筛查输入包

从已冻结的 1,000 条未标注候选队列，以固定哈希顺序选出纽约 400、河内 100 条不同实体；私有逐案文件为 `.local/location-verification-live-check/annotation_packet_500/cases.jsonl`，权限 `0600`。输入队列 SHA-256 为 `9de97eeae5d4ea9badb5677e727faa6d4659887c913e8b394fdca66f112b1e86`，输出 SHA-256 为 `90a35f98b3d4e21762906ae2da7c4d50c2018bceae374df247747a7bc396b121`。公开汇总见 [`packet-summary.json`](packet-summary.json)。

每案保留候选、来源和原有证据，新增**空白**人工标注字段。脚本禁止旧 `review_status`、匹配 POI、坐标验证、Gold 和仲裁字段流入输入；用实体摘要检查唯一性。两项测试检查确定性、500 条配额、污染和重复实体失败关闭。脚本默认拒绝覆盖既有输出。

这些案例尚未经过资格审查、补充独立证据、人工 Gold、困难类别配额和 dev／locked-test 隔离。不能把本包的 500 当作主计划中的 500 条已标注评测集，也不能报告地点核验准确率。

后续用已冻结 DOT 快照做[证据覆盖审计](evidence-coverage-summary.json)：500 条中有 397 条纽约 DOHMH 记录；其中当前 DOT 唯一名称＋地址匹配仅 **24/397**、坐标可比较 **23/24**，超过 10 米的差异 **0 条**。历史 DOT 按 CAMIS 连接 **90/397**（单行 68、多行 22），不代表当前营业。该包的随机筛查规则几乎没有抽到已知坐标困难案例，因此它只能作为**日常候选入口**；正式 200 条困难锁定案例必须另从全量来源池定向建立，不能把本包的 500 直接分割为正式 100＋400。

另从冻结的 LAND-DATA 全量 DOHMH 输出构建了[困难风险短名单 v2](hard-shortlist-summary-v2.json)，私有逐案文件为 `.local/location-verification-live-check/annotation_packet_500/hard-case-shortlist-v2.jsonl`，SHA-256 为 `fd2adfee9ba45a62c98df40a26c9817f053458d652254d8a08f6972f747e1629`。它与日常 500 条规范化地点键无重叠，155 条的候选 ID 和地点键也各不重复：坐标差异 50 条（当前 DOT 20、历史 DOT 30）、同名多地址 50、历史 DOT 地址不一致 50、最新 DOHMH 快照缺失 5。最后一组只是“搬迁／停业待筛查”信号，**不等于**已有 5 个已证实搬迁或停业案例；距离 50 条困难组目标仍差 45 条，其他组也须逐案审查资格、补独立证据和人工标签。首版短名单没有完整写出所有风险信号，保留原始结果并修正为 v2；3 项输入包和短名单契约测试通过。

为这 155 条生成了[私有、未标注的审核证据包 v3](hard-evidence-dossier-summary-v3.json)，逐案保留冻结 DOHMH 来源、可检索的 DOT 当前／历史原始行，以及存在时的最新 DOHMH 记录；每条附快照哈希、行定位、行内容哈希、来源 URL、时间值及其语义。**仅 v3 可用于后续人工筛查**：文件为 `.local/location-verification-live-check/annotation_packet_500/hard-evidence-dossiers-v3.jsonl`，权限 `0600`，SHA-256 为 `2d6719b4568fa55382a22121a406649dfb49493b3af10fd6623188ed9b698ac5`。覆盖 155 条冻结 DOHMH、37 条当前 DOT、129 条历史 DOT、3 条最新 DOHMH；DOHMH 新旧属于同一来源体系，DOT 新旧也不自动算两条独立来源。v1 把官方 `record_date` 错写为采集时间；v2 又被递归污染审计发现冻结来源的嵌套元数据带有旧系统 `coordinate_verified`（155 条）及 `review_reasons`（1 条）。v3 删除这些旧判断字段，[独立审计脚本](audit_evidence_dossiers.py)按冻结数据库与快照反查 324 条证据行、行内容哈希和 0 个禁用字段，结果为 `VERIFIED`。v1/v2 保留作错误追溯，**不得作为标注输入或 Agent 测试输入**。v3 不含旧流程审核状态、风险选样信号或 Gold，只供人核验，不交给 Agent 作为锁定测试答案。当前本目录 7 项契约测试通过。

用户指定由 Codex 承担标注后，先完成[655 条单一 AI 标注者的保守证据分流](ai-provisional-evidence-annotations-summary.json)，私有逐案输出为 `.local/location-verification-live-check/annotation_packet_500/ai-provisional-evidence-annotations-v1.jsonl`（`0600`，SHA-256 `2f0af953a75c89224dbca78f592fae5acc907e34c6f6aa3696bb7c37cdb3619b`）。其中 500 条日常输入仅检查已有包的字段完整度与证据缺口，缺地址 54、缺名称 8、缺有效坐标 10，四类事实字段都保持 `UNKNOWN`；155 条困难输入基于 v3 证据包逐案标注**来源间关系**：当前 DOT 坐标可比 36 条，20 条超过 10 米、16 条在 10 米内，其余 119 条未知；身份字段 2 条跨 borough 冲突、35 条仅为可能匹配、118 条未知。逐案保存来源引用、原因、下一步动作与限制，7 项本目录测试通过。输出中 `gold_label=null`、`split=null`、`eligible_for_accuracy=false`：这是 AI 辅助筛查，**不是人工 Gold，也不能计算地点准确率或改前／改后提升**。历史 DOT 许可不证明当前营业，营业状态全部保持未知；相同名称＋地址检索到的 37 条不能当作实体身份已证实。

标注后的证据充分性复核显示：155 条冻结 DOHMH 原始元数据均无 `ACTION`／当前营业状态字段，不能用在线数据集字段说明反向补进旧快照。655 条建议动作中，2 条优先人工辨别分店、18 条优先人工核查坐标、567 条继续收集独立现时证据、68 条先修复输入后再取证。**20 条优先人工不是 20 条已确认地点错误。**

日常组的跨来源证据此前仅有覆盖统计，没有逐案原始行。现从同一冻结输入与 DOT 快照生成[500 份日常私有证据包 v1](daily-evidence-dossier-summary-v1.json)，文件 `.local/location-verification-live-check/annotation_packet_500/daily-evidence-dossiers-v1.jsonl`（`0600`，SHA-256 `3d20fc51b262fbcf789f9a48f0dfb0debe9a29f98dc55725d10c24a32b8bd4bc`）。逐案关联 397 条 DOHMH 入口、103 条 OSM 入口、24 条当前 DOT 原始行和 117 条历史 DOT 原始行（历史覆盖 90 案）。[独立反查脚本](audit_daily_evidence_dossiers.py)重读冻结输入和 DOT 原始快照，核验全部行、定位和哈希，结果 `VERIFIED`、禁用旧答案字段 0。它们是供审阅的证据，不是地点真值。

在不改动 v1 的前提下，另生成[655 条暂定标注 v2](ai-provisional-evidence-annotations-summary-v2.json)，私有逐案文件 `.local/location-verification-live-check/annotation_packet_500/ai-provisional-evidence-annotations-v2.jsonl`（`0600`，SHA-256 `e21612d0bdfe78e29ba6875e2947c426cbb7b1e72649158f26fe6ec98e57a310`）。日常组 24 条有当前 DOT 同名同地址检索行，其中 23 条坐标在 10 米内、1 条缺可比坐标；其余 476 条没有当前 DOT 唯一检索行。23 条只是跨来源坐标一致，身份仍是“可能匹配、未确认”，不能报为 23 条正确地点或用作准确率分子。营业状态、Gold 和锁定测试 split 继续保持未知／空值。

本目录当前 **11/11** 项契约测试通过；新增证据包的独立逐行审计为 `VERIFIED`。
