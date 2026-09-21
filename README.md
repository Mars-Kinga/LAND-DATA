# LAND-Data

LAND 的独立地点与推荐数据基础设施。当前为第一阶段：**仅处理虚构 Mock 数据**。不采集真实城市、不连接社交平台、不登录真实账号、不调用 LLM，也不修改相邻的 LAND iOS 项目。

## 运行

需要 Python 3.9+；运行和测试只使用标准库。仓库根目录的入口无需安装依赖：

```sh
./land-data auth-status mock-social
./land-data auth mock-social
./land-data pipeline --city mock-tokyo
./land-data review
```

也可使用 `PYTHONPATH=src python3 -m land_data ...`。`pyproject.toml` 提供标准的 `land-data` 安装入口，供具备构建工具的环境使用。`auth` 仅在 `.local/mock-sessions.json` 记录一个布尔 Mock 状态，没有账号、密码、Cookie 或 Token。未运行 `auth mock-social` 时，包含该来源的 Pipeline 返回 `AUTH_REQUIRED`，退出码为 2。

默认 SQLite 为 `.local/land-data.db`，导出为 `.local/exports/mock-tokyo.json`。`.local/`、数据库、会话和常见密钥文件均被 `.gitignore` 排除。可通过全局参数 `--db`、`--session-file`、`--root` 覆盖路径。

```sh
./land-data collect --city mock-tokyo --source mock-places
./land-data collect --city mock-tokyo --source mock-social
./land-data parse --city mock-tokyo
./land-data validate
./land-data review
./land-data export --city mock-tokyo --output .local/exports/mock-tokyo.json
```

`pipeline` 顺序执行 collect、parse、基础匹配与验证、评分和审核，再导出。`collect` 单独运行只写 Raw；`parse` 可从仍在保留期内的 Raw 重建 Normalized 与 Derived。`validate` 可重新匹配 Candidate 并计算审核状态和地图可见性。`review` 只列待审核对象，尚无人工裁决命令。

测试：

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 数据流与模型

```text
SourceAdapter（PUBLIC 或 AUTHENTICATED）
  → RawSourceRecord（来源策略筛选后的快照，保留期限）
  → SourceContent（规范化内容与来源身份）
  → LocationEvidence / POICandidate / POIMention / RecommendationSignal / EventCandidate
  → Entity Resolution → POI / Event
  → Validation / Score / Review → Export
```

- **POI**：正式地点；多语言名称、别名、坐标、类别、字段来源、质量、审核和地图可见性分开存储。
- **SourceContent**：来源内容。`platform + external_id` 为稳定身份；Raw 快照可因内容变化而增加。
- **POICandidate**：待匹配地点；证据不够时保留并进入审核，不直接发布 Marker。
- **POIMention**：内容与地点的关系，可先只关联 `poi_candidate_id`，解析后同时绑定 `poi_id`；数据库约束保证两者至少一个存在。
- **RecommendationSignal**：属于 Mention 的轻量 `type/value` 信号。SQLite 按已批准 Mention 的不同 SourceContent 聚合，无需查看时调用 LLM。
- **LocationEvidence**：记录 Geo Tag、正文或其他证据的原值、类型、置信度及匹配 POI。
- **EventCandidate / Event**：临时活动及其场地关系；不作为永久 POI 发布，第一阶段一律待人工审核。
- **FieldProvenance**：POI 重要字段的来源、内容引用、采集和验证时间。

当前 Mock 输入含一个已验证的结构化地点、两篇可匹配的推荐帖、一篇未解析的推荐帖和一个活动候选。未解析帖的 Mention 与 Cocktail 信号仍保存在 SQLite，但不会进入地图导出。已匹配的两篇帖子产生 `sunset: 2` 与 `date: 1` 的聚合计数。

## 匹配、审核与导出

基础 Entity Resolution 只用规范化名称或别名、城市、国家、类别和 250 米坐标距离。唯一精确匹配可绑定现有 POI；多个匹配、坐标冲突或证据不足进入 `NEEDS_REVIEW`。只有配置明确授权坐标验证的结构化 Mock 来源，才能自动创建 POI。没有模糊 NLP 或真实来源适配器。

POI 的 `map_visibility` 只有在坐标有效且已验证、验证通过、审核为 `AUTO_APPROVED`、地点有效且置信度达到配置阈值时为真。`REJECTED` 不会被重复验证自动改回批准。导出只包含可见 POI 和已批准 Mention；每个来源的 `export_fields` 决定来源链接、推荐摘要、信号能否进入最终数据。导出 schema 与 LAND iOS 现有 `Place` v1 并不相同，尚未接入 App。

## 来源策略与认证边界

`config/sources.json` 声明 Adapter capability、访问模式、原始字段 allowlist、是否保存来源 URL、规范化字段 allowlist、临时字段、导出字段和 Raw 保留天数。收集时递归剔除明显的敏感键，并清除来源 URL 中明显的认证参数。到期 Raw 在下一次 collect/parse 时删除；允许保留的 SourceContent 和 Derived 数据分开存储。临时字段只可在同一进程的 `pipeline` 调用中参与处理，不会落盘，之后无法从 Raw 回放该字段。

真实认证会话、平台权限判断、内容保存许可和删除传播仍未实现。未来 Adapter 必须先确定平台允许的访问与保存方式；会话仅交给 Source Access 层，不能进入模型、SQLite 数据集、日志或导出。Mock 代码不提供验证码、2FA 或访问控制绕过能力。

## 结构

```text
config/                 城市与 Mock 来源策略
fixtures/               完全虚构的来源数据
src/land_data/
  adapters.py            Adapter 接口、能力声明及 Mock Adapter
  auth.py                Mock Session / AUTH_REQUIRED
  models.py              核心数据模型
  policy.py              字段 allowlist、保留期限和敏感字段过滤
  storage.py             Pipeline 使用的 Storage 接口
  sqlite_storage.py      SQLite 表与唯一 SQL 实现
  processing.py          规范化、提取、匹配、验证、评分、审核
  pipeline.py            阶段编排
  export.py              可部署 Mock 数据集
  cli.py                 命令行入口
tests/                  自动化测试
```

## 当前边界

- Mock 提取使用 fixture 提供的地点和推荐标注；没有自然语言地点提取、语义分类或 LLM。
- Raw 字段保留期已实现；规范化与衍生数据的跨层删除传播、平台许可审计和长期修订策略尚未实现。
- 匹配算法仅适合基础精确场景；同名地点、多语言变体和复杂坐标冲突需要后续审核与算法改进。
- 审核队列可查看，人工批准或拒绝流程及审核历史尚未提供 CLI。
- 当前导出是 LAND-Data 的独立 schema；LAND iOS 现有 `Place` 契约没有 Mention/Signal 列表，接入需要单独设计并经确认。
