# Codex 辅助审核

这个模块用于处理“系统标记为需要审核，但这次不想人工打开，也不想调用千问”的记录。
它只记录一次由 Codex 在当前对话中完成的审核决定，不会自己调用任何 LLM、访问网络或读取远程账号。

## 工作边界

Codex 先读取本地审核证据（来源、字段 provenance、坐标、当前状态和已有审核历史），确认证据后调用本地函数：

```python
from land_data.assisted_review import codex_assisted_review

decision = codex_assisted_review(
    storage,
    target_id="poi-id",
    decision="approve",              # approve 或 reject
    reviewer_model="codex:gpt-5",
    reason="官方结构化来源确认了名称、地址和坐标，字段没有冲突。",
    evidence_digest="sha256:...",   # Codex 实际审阅输入的摘要或输入包 ID
    evidence_metadata={
        "input_id": "local-review-bundle/poi-id",
        "source_ids": ["source-content-id"],
        "checked_fields": ["names", "address", "latitude", "longitude"],
    },
)
```

Codex 也可以通过终端写入同一份审核记录：

```bash
./land-data --db .local/land-data.db codex-review approve <目标ID> \
  --reviewer-model codex:gpt-5 \
  --reason "已核对本地来源、字段 provenance、坐标和冲突标记" \
  --evidence-digest "sha256:<本次证据摘要>" \
  --evidence-metadata .local/review-evidence/<目标ID>.json
```

这个终端命令只保存 Codex 已完成的决定，不会在后台调用 Codex、千问或其他模型。

`reviewer_model` 必须以 `codex:` 开头，例如 `codex:gpt-5` 或
`codex:gpt-5.6-luna`。`reason`、`evidence_digest` 和审核对象 ID 都必须有值。
模块会把以下信息写入不可变的 `ReviewDecision.metadata`：

- `review_mode=CODEX_ASSISTED`
- `model` 与 `reviewer_model`
- `evidence_digest` 和 `evidence_metadata`
- `policy_version`

## 安全校验

批准沿用现有 `ReviewService` 的对象查找、地点/活动/官方指引/来源校验、坐标校验、
状态更新和 `validate_score_review`。缺少坐标、必填字段、地点绑定、活动时间、来源条件，
或者对象已经拒绝，都会失败；不会因为审核者是 Codex 而放宽条件。

审核决定通过现有 `Storage.apply_review` 原子保存，模块不直接改 SQLite。重复批准或拒绝会安全失败，
不会追加重复决定。拒绝只记录拒绝，不会把内容发布到 LAND。

## 发布边界

审核批准只表示该对象通过审核。真正写入 LAND 内容包仍要经过：

1. Place bridge `preflight`
2. `prepare`
3. `verify`
4. `install`

因此 Codex 可以在当前对话中完成需要人看的审核，但发布仍保留独立的来源、字段、城市、内容和安装校验。
这条路径完全不调用千问，千问调用数为 0。

## 版本

默认策略版本是 `codex-assisted-review/v1`。如果审核规则发生变化，应显式传入新的
`policy_version`，方便在审核历史中区分不同规则。
