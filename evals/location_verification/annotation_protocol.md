# Location Verification Agent 标注规程

本规程用于建立正式的前后对照评测集。目标是至少 **500 个真实地点案例**，并在同一批冻结证据上比较改造前流程与 Agent Harness。标注者只依据已保存的证据快照，不在评测过程中临时打开网页或补查实时信息。

## 1. 数据边界与样本量

正式数据集至少包含 500 个独立地点实体，分成 100 条开发集和 400 条锁定测试集。锁定测试集固定为两部分：

- 200 条日常案件：从待核验队列按预设随机种子抽样，不按模型结果、预期标签或“容易获得正确答案”筛选；
- 200 条困难案件：搬迁/停业、同名分店、旧页或冲突来源、地址/坐标不一致四类各至少 50 条。

困难案件可以同时带多个风险标签，但为保持 200 条的分母不重复计数，`primary_stratum` 在抽样冻结时确定，四类配额按该字段统计；其他风险保存在 `secondary_risk_tags`。100 条开发集从同一候选池独立抽样，用于协议校准、基线和调参，具体构成写入 `sampling_manifest`，不得与锁定测试实体或证据重复。

同一地点实体只能进入一个 `primary_stratum` 和一个 split。一个品牌的不同分店是不同实体，不能因为名称相同而合并。除 500 条正式案例外，可另建不少于 100 条格式错误、工具失败、超时和权限拒绝的 Harness 故障用例；故障用例不计入真实地点数量。

已有 `xhs-verification-a/b/c.json` 与 `xhs-verification-final.json` 中的 51 条记录已经人工审核，只能作为 `historical_replay` 开发回放和 schema/provenance 检查，不能进入锁定测试，也不能计入正式前后准确率分母。它们不能替代新的独立 Gold 案例。

## 2. 抽样、去重与冻结顺序

1. 从真实社交内容、第一方页面、地图/目录和历史地点库建立候选池。每条候选保留原始文本、来源标识、采集时间和城市，不先用模型结果筛选。
2. 用 Unicode NFKC、大小写折叠、空白归一化处理名称和地址，生成 `entity_group_key = normalized_name + normalized_address + city`。坐标只用于发现疑似重复，不单独决定合并。
3. 对同一 URL、同一帖文、同一地点的转发或重复抓取只保留一个 Case；同一实体的多条证据可以进入同一个 Case。名称相同但地址或分店不同的候选必须分组为不同实体。
4. 有第二名标注者时，由两人独立检查自动分组并仲裁分歧；暂时只有一人时，记录单人检查及未独立复核的限制。无法确定是否同一实体时暂时分开，待复核或仲裁后再锁定。确认后的 `entity_group_id` 是 split 隔离的原子单位，同一实体的所有 Case、Claim 和证据不得跨 split。
5. 冻结待核验队列和风险标签后，用固定随机种子抽取 200 条日常锁定案件；再按四类困难案件配额抽取 200 条，并记录不足、补采或多标签归类原因。锁定测试 ID 不因后续调参而重抽。
6. 从未进入锁定测试的候选中独立抽取 100 条开发集。保存原始输入、证据包和清单的 SHA-256；生成 `split_manifest_sha256` 后，任何增删、换源、改层或重分组都必须提升 `protocol_version`，重新冻结并重新测量，不能覆盖原报告。

## 3. 证据时间快照

每个 Case 使用固定 `snapshot_cutoff_at`。网页、地图结果、帖文和 API 响应保存为原始 HTML/JSON/纯文本或截图，记录 URL、来源类型、发布/更新时间（如有）、抓取时间、抓取方式、内容 SHA-256 和许可备注。动态页面必须同时保存可读摘录和截图或原始响应；只保存一个随时变化的 URL 不算快照。

评测准备阶段保存完整的冻结证据包，但 Agent 运行时只挂载 Case、Claim 和初始证据；完整证据包对 Agent 进程不可读。其余证据必须通过同一套受控工具回放接口按 Agent 的 `QUERY_TOOL` 动作返回，工具日志记录请求、返回的 `evidence_id`、结果摘要和成本。禁止联网、禁止读取 Gold、禁止使用 cutoff 之后的内容。若来源后来更新，创建新的 `snapshot_id` 和新版本数据集；不得在原快照上静默替换内容。

## 4. Agent 输入与输出契约

Agent 初始输入包只包含 `input/cases.jsonl`、每个 Case 的初始证据和允许工具清单，不包含完整冻结证据包、Gold 标签、仲裁意见、人工最终动作或任何仅在锁定测试后产生的文件。运行容器的文件权限必须阻止 Agent 直接读取隐藏证据目录；只有受控工具回放接口能返回后续证据。每行至少包含：

```json
{
  "schema_version": "lv-input-v1",
  "case": {
    "case_id": "case-...",
    "entity_group_id": "entity-...",
    "city": "...",
    "source_snapshot_id": "snapshot-...",
    "source_text": "原始地点提及或上下文",
    "candidate": {
      "name": "...",
      "address": "...",
      "latitude": null,
      "longitude": null,
      "category": "..."
    }
  },
  "claims": [
    {
      "claim_id": "claim-...",
      "field": "identity|address|coordinates|operational_status|relocation|category",
      "candidate_value": "..."
    }
  ],
  "initial_evidence": [
    {
      "evidence_id": "evidence-...",
      "source_type": "first_party|map|directory|social|other",
      "captured_at": "...",
      "published_at": null,
      "content_sha256": "...",
      "artifact_ref": "snapshot/...",
      "excerpt": "与本 Claim 直接相关的摘录"
    }
  ],
  "tool_policy": {
    "allowed_tools": ["..."],
    "replay_interface": "controlled_tool_replay_v1",
    "max_steps": 8,
    "network": false
  }
}
```

Agent 的每一步 Action 必须记录 `action_id`、`case_id`、动作类型、依据的 `claim_id/evidence_id`、工具名和参数摘要、时间、结果摘要及停止原因。最终动作限定为：

- `KEEP`：维持候选地点及字段；
- `UPDATE`：依据证据更新指定字段，并列出字段级变更；
- `WITHHOLD`：证据不足或冲突，暂不发布；
- `ESCALATE_HUMAN`：需要人工处理（身份无法区分、来源冲突未解、权限/工具失败或达到预算）。

`UPDATE` 不等于“猜测一个新地点”；没有可追溯证据的字段必须保持空值或进入 `ESCALATE_HUMAN`。

每个 Action 使用同一结构，便于重放和逐步审计：

```json
{
  "action_id": "action-...",
  "case_id": "case-...",
  "type": "QUERY_TOOL|KEEP|UPDATE|WITHHOLD|ESCALATE_HUMAN",
  "tool": null,
  "claim_ids": ["claim-..."],
  "evidence_ids": ["evidence-..."],
  "parameter_digest": null,
  "result_digest": null,
  "reason_codes": ["..."],
  "stopped": false,
  "stop_reason": null
}
```

`QUERY_TOOL` 只能调用 `tool_policy.allowed_tools` 中的工具，并通过 `replay_interface` 获取隐藏证据；Agent 不能绕过接口读取完整快照。`KEEP`、`UPDATE`、`WITHHOLD` 和 `ESCALATE_HUMAN` 是终止动作。参数和结果使用摘要或哈希，原始内容放在对应的冻结 artifact 中，避免日志改变输入分布。

## 5. Gold 标注格式

Gold 单独保存在评测运行时不可读的目录中，只通过 `case_id` 与输入关联。每个 Claim 都要标注原子事实，不用一条总评覆盖所有字段：

```json
{
  "schema_version": "lv-gold-v1",
  "case_id": "case-...",
  "split": "dev|locked_test",
  "stratum": "daily|moved_or_closed|same_name_branch|stale_or_conflicting_source|address_coordinate_conflict",
  "entity_group_id": "entity-...",
  "claims": [
    {
      "claim_id": "claim-...",
      "field": "address",
      "gold_value": "...",
      "status": "SUPPORTED|REFUTED|UNKNOWN|CONFLICTING",
      "evidence_ids": ["evidence-..."],
      "confidence": "high|medium|low",
      "reason_codes": ["..."],
      "adjudication_note": "..."
    }
  ],
  "gold_action": {
    "label": "KEEP|UPDATE|WITHHOLD|ESCALATE_HUMAN",
    "fields_to_update": [],
    "target_entity": null,
    "reason_codes": ["..."],
    "required_evidence_ids": ["evidence-..."]
  },
  "review": {
    "annotator_a": "...",
    "annotator_b": null,
    "adjudicator": null,
    "independent_review": false,
    "review_scope": "hard_all|daily_random_20pct|single_annotator",
    "agreement": "agree|adjudicated|single_annotator",
    "limitation": null,
    "gold_locked_at": "..."
  },
  "snapshot_manifest_sha256": "..."
}
```

`SUPPORTED` 要求证据直接支持该字段；`REFUTED` 要求证据直接否定候选值；`UNKNOWN` 表示当前快照没有足够信息；`CONFLICTING` 表示有两个或更多无法按规程消解的可信来源。无法确定实体身份时，Gold Action 通常为 `ESCALATE_HUMAN` 或 `WITHHOLD`，不得为了提高自动化率强行选择 `KEEP`。

## 6. 双人复核与仲裁

标注开始前，所有标注者用开发集完成一次校准，统一字段定义、来源强度、地址/坐标差异和停止条件。200 条困难锁定案件全部由标注者 A、B 独立完成；200 条日常锁定案件中按固定随机种子抽取 20%（40 条）由 A、B 独立完成，其余日常案件由 A 完成并标记为单人标注。双方看不到对方标签、模型输出和运行日志。每人必须填写 Claim 状态、证据 ID、Gold Action、理由码和实际用时。

对有独立复核的案件，以下情况自动进入第三名高级标注者仲裁：实体分组不同、任何 Claim 状态不同、最终 Action 不同、来源时效或权威性判断不同、地址/坐标冲突无法解释。仲裁者保留 A/B 原始标签，只新增仲裁结果和理由。报告至少包含困难全量和日常 20% 子集的 Claim 状态一致率、最终 Action 一致率和仲裁率；只有确实存在第二名独立标注者时才计算或宣称双人一致率/Cohen's κ。

如果实际没有第二名标注者，`annotator_b` 必须为 `null`，`independent_review` 为 `false`，并填写 `limitation`（例如“无第二标注者，本案为单人 Gold”）。此类案例可以继续用于开发或锁定评测，但报告必须明确其单人限制，不能称为“双人金标准”或“全量双标”。

人工审核必须选择 `ESCALATE_HUMAN` 的条件包括：无法唯一匹配实体、关键字段只有互相冲突的来源、所有来源均过期或不可追溯、工具权限/预算导致关键证据缺失、以及安全或隐私规则禁止继续取证。

## 7. Split lock 与防泄漏检查

正式运行前依次执行并记录以下检查：

1. `case_id`、`entity_group_id`、规范化名称/地址哈希、来源 URL 哈希在两个 split 间没有交集；近重复转发和同一地点的别名也只能保留在一个 split。
2. `locked_test` 的初始输入包不含 `status`、`gold_action`、`adjudication_note`、标注者姓名、最终发布结论、完整隐藏证据包或 Gold 文件路径；运行容器不能读取 Gold 目录和隐藏证据目录。
3. 隐藏证据只通过 `controlled_tool_replay_v1` 返回，返回内容与 `snapshot_cutoff_at` 之前的快照哈希相符；工具回放不联网，不能接受 Agent 自行指定的未登记 artifact 路径。
4. 固定模型、Prompt、工具版本、缓存策略、最大步数、超时和费用预算；改动任一项都生成新 run，不修改旧结果。
5. 先在开发集冻结基线和调参，再一次性运行锁定测试。锁定测试结果不得反馈给 Prompt、路由、阈值或样本分布；需要修复时创建新协议版本和新锁定集。
6. 比较改前/改后时使用完全相同的 Case、Claim、Evidence、工具预算和评价分母，并单独报告 `KEEP/UPDATE/WITHHOLD/ESCALATE_HUMAN` 各类结果。历史 51 条回放记录和故障用例只能单独列示，不能并入真实地点准确率。

最终提交物应包含 `sampling_manifest`、`split_manifest`、证据快照哈希清单、输入包、隔离的 Gold 包、独立复核与仲裁日志（以及单人标注限制记录）、运行配置和不可变的评测报告。任何缺失清单或哈希的案例视为不可用于锁定测试，先回到开发集处理。
