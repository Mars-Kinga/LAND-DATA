# LAND-Data

LAND 的独立地点与推荐数据基础设施。2B 增加了限量的 OpenStreetMap / Overpass 结构化来源；2C 增加平台无关的社交内容处理层与完整离线验收。社交来源仍仅使用虚构 Mock 数据；不登录真实账号、不调用 LLM，也不修改相邻的 LAND iOS 项目。

## 运行

需要 Python 3.9+；运行和测试只使用标准库。仓库根目录的入口无需安装依赖：

```sh
./land-data auth-status mock-social
./land-data auth mock-social
./land-data pipeline --city mock-tokyo
./land-data review list
```

也可使用 `PYTHONPATH=src python3 -m land_data ...`。`pyproject.toml` 提供标准的 `land-data` 安装入口，供具备构建工具的环境使用。`auth` 仅在 `.local/mock-sessions.json` 记录一个布尔 Mock 状态，没有账号、密码、Cookie 或 Token。未运行 `auth mock-social` 时，包含该来源的 Pipeline 返回 `AUTH_REQUIRED`，退出码为 2。

默认 SQLite 为 `.local/land-data.db`，导出为 `.local/exports/mock-tokyo.json`。`.local/`、数据库、会话和常见密钥文件均被 `.gitignore` 排除。可通过全局参数 `--db`、`--session-file`、`--root` 覆盖路径。

```sh
./land-data collect --city mock-tokyo --source mock-places
./land-data collect --city mock-tokyo --source mock-social
./land-data parse --city mock-tokyo
./land-data validate
./land-data review list --city mock-tokyo
./land-data export --city mock-tokyo --output .local/exports/mock-tokyo.json
```

`pipeline` 顺序执行 collect、parse、基础匹配与验证、评分和审核，再导出。`collect` 单独运行只写 Raw；`parse` 可从仍在保留期内的 Raw 重建 Normalized 与 Derived。`validate` 可重新匹配 Candidate 并计算审核状态和地图可见性。`review` 等同于 `review list`。

## 受限 OSM 集成（第二阶段 2B）

`config/osm-acceptance-scope.json` 是**仅供集成验证**的东京站附近 bbox，不是产品默认城市。它将采集范围的 `name` 与 POI metadata 的 `city` 分开；GeoScope 保存 `scope_type`、国家、bbox，以及可选的中心和半径。当前 OSM Adapter 只查询 bbox，提供中心/半径时会明确报错，避免悄悄忽略范围条件。请求限制为 bbox 每轴至多 0.02°、结果最多 30 条、超时 2–30 秒，仅允许 restaurant/cafe/museum。默认上限为 20 条、15 秒。一次请求、最多一次轻量重试，无并发。

使用单独的数据库，便于比较各层结果：

```sh
./land-data --db .local/osm-acceptance.db collect --source osm --scope config/osm-acceptance-scope.json --limit 20 --timeout 15 --category cafe --category restaurant --category museum
./land-data --db .local/osm-acceptance.db parse --scope config/osm-acceptance-scope.json
./land-data --db .local/osm-acceptance.db validate
./land-data --db .local/osm-acceptance.db review list --city Tokyo
./land-data --db .local/osm-acceptance.db export --scope config/osm-acceptance-scope.json --output .local/exports/osm-acceptance.json
```

也可以使用 `pipeline --source osm --scope ...` 一次运行完整链路。重复执行相同范围可以检查幂等性。OSM 不需要 `auth`。不传 `--source` 与 `--scope` 时，原有 Mock CLI 行为保持不变；一次 scoped collect/pipeline 只允许一个来源。`--scope` 的 `name` 用于采集与 parse 筛选，`city` 用于 POI 元数据。2C 起 `export --scope` 只包含该 scope 的 POI；不指定 scope 时仍按城市导出。

2026-09-21 的受限真实验收：同一 bbox 返回 10 个 OSM 对象。第一次完成 collect、parse、validate、export 后有 10 Raw、10 SourceContent、10 Candidate、10 POI、47 条字段来源，10 个 POI 均为 `AUTO_APPROVED` 且可见，导出 10 个。第二次用同样的 bbox、类别、20 条上限和 15 秒超时执行完整 `pipeline` 后，四层数量、来源数量与导出 POI ID 集合均未变化。本机 Python 3.9 的默认 CA 路径不存在，实际请求使用 `SSL_CERT_FILE=/etc/ssl/cert.pem` 指向系统 CA，TLS 校验始终开启；若系统 Python 已有可信 CA，无需设置该变量。一次服务端读取超时由 Adapter 的单次轻量重试处理。

OSM 对象身份由 `node/way/relation` 和对象 ID 组成；SourceContent 稳定去重，未变化的 Raw 快照也不会重复存储。Raw 保留对象类型、ID、必要 tags、坐标及采集时间，不存整份 Overpass 响应。仅在名称、有效坐标、受支持类别和可信结构化证据齐全时才自动创建 POI；缺少必要字段或类别未知的 Candidate 进入审核并保留原 tags。字段级 FieldProvenance 保存 OSM 来源内容引用及 `tags.name`、`tags.amenity`、`tags.website`、`lat` 或 `center.lat` 等路径。可见 OSM POI 的导出附带 OpenStreetMap / ODbL 署名信息；对外使用该数据时仍须遵守 [OSM 署名与许可要求](https://www.openstreetmap.org/copyright)。

同一 OSM 对象的 tags 更新后，仍使用同一个 SourceContent、Candidate 和 POI ID；原 POI 的名称、坐标、类别、地址、网站和营业时间按新来源值同步，变动字段的 FieldProvenance、`updated_at` 和 `last_verified_at` 同步更新。仅同步该 OSM Candidate 自己创建的 POI，避免覆盖从其他来源匹配到的地点。没有引入通用历史版本系统。

## Social Content Foundation（第二阶段 2C）

`MockSocialAdapter` 只读取虚构 fixture，交给通用的 SourceItem / RawSourceRecord 链路；它不判断地点、推荐或审核状态。`SourceContent` 支持规范 URL、作者、标题、正文、发布时间、Geo Tag、hashtag、语言和仅含结构信息的媒体 metadata；这些字段可缺失。`config/sources.json` 为社交来源分别声明 Raw、Normalized、临时与 Export 字段及保留期。`ACTIVE`、`UPDATED`、`UNAVAILABLE` 是最小内容状态，同一 `platform + external_id` 始终保留一个 SourceContent 身份；不可用内容的 Mention 不再导出。相同 Raw 快照再次采集时只刷新最近采集与到期时间，不增加 Raw 行数；内容恢复可复用原身份。帖子更新后不再支持的 Mention 标为 inactive，保留记录但不参与聚合或导出。

`social_processing.py` 定义 LocationExtractor 和 RecommendationExtractor 接口，目前只实现 `GeoTagExtractor`、fixture 标注的文本地点提取与少量规则推荐词。证据强度统一放在 `EVIDENCE_STRENGTH`；AREA Geo Tag 只能作为低强度区域证据，不能直接变成 POI。每个地点的推荐规则只读取与该地点关联的文本片段，避免一帖多地时把 `study` 和 `cocktail` 同时挂到 Cafe A、Bar B。提取后复用既有 Candidate、Evidence、Mention、Signal、Event、Entity Resolution、Review 和 Export 流程。

完整离线验收不会请求 OSM 网络：

```sh
PYTHONPATH=src python3 scripts/social_acceptance.py --db .local/social-acceptance.db --output .local/exports/social-acceptance.json
```

脚本先用模拟 OSM 响应创建 Example Rooftop、Example Matcha Cafe、Cafe A、Bar B、Example Gallery，再导入六篇虚构帖子。预期 11 Raw / SourceContent / Candidate、5 POI、4 Mention、5 Signal、1 EventCandidate 和 1 Event；导出 5 POI 与 4 Mention。每个 POI 的 Mention 数、信号聚合、来源和审核状态会打印出来。也可在种子 POI 建好后，单独用 `./land-data --db .local/social-acceptance.db pipeline --source mock-social-2c --scope config/social-acceptance-scope.json` 重放 Mock 社交输入。

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
- **SourceContent**：来源内容。`platform + external_id` 为稳定身份；Raw 快照可因内容变化而增加。保留采集 scope 以支持 Raw 过期后的隔离导出。
- **POICandidate**：待匹配地点；证据不够时保留并进入审核，不直接发布 Marker。
- **POIMention**：内容与地点的关系，可先只关联 `poi_candidate_id`，解析后同时绑定 `poi_id`；数据库约束保证两者至少一个存在。
- **RecommendationSignal**：属于 Mention 的轻量 `type/value` 信号。SQLite 按已批准 Mention 的不同 SourceContent 聚合，无需查看时调用 LLM。
- **LocationEvidence**：记录 Geo Tag、正文或其他证据的原值、类型、置信度及匹配 POI。
- **EventCandidate / Event**：临时活动及其场地关系；不作为永久 POI 发布，第一阶段一律待人工审核。
- **FieldProvenance**：POI 重要字段的来源、内容引用、采集和验证时间。
- **ReviewDecision**：针对 Candidate、Mention 或 POI 的人工操作、前后状态、原因、审核人、时间与可选元数据；每次操作写入 SQLite 追加式审核历史。

当前 Mock 输入含一个已验证的结构化地点、两篇可匹配的推荐帖、一篇未解析的推荐帖和一个活动候选。未解析帖的 Mention 与 Cocktail 信号仍保存在 SQLite，但不会进入地图导出。已匹配的两篇帖子产生 `sunset: 2` 与 `date: 1` 的聚合计数。

## 匹配、审核与导出

基础 Entity Resolution 只用规范化名称或别名、城市、国家、类别和 250 米坐标距离。唯一精确匹配可绑定现有 POI；多个匹配、坐标冲突或证据不足进入 `NEEDS_REVIEW`，并留下 `AMBIGUOUS_POI_MATCH`、`CONFLICTING_COORDINATES`、`INSUFFICIENT_LOCATION_EVIDENCE` 等机器可读原因。只有配置明确授权坐标验证的结构化来源（Mock Places、OSM），才能自动创建 POI。社交内容仅匹配既有 POI 或进入审核；没有模糊 NLP 或真实社交 Adapter。

POI 的 `map_visibility` 只有在坐标有效且已验证、验证通过、审核为 `AUTO_APPROVED`、地点有效且置信度达到配置阈值时为真。`REJECTED` 不会被重复验证自动改回批准。导出只包含可见 POI 和已批准 Mention；每个来源的 `export_fields` 决定来源链接、推荐摘要、信号能否进入最终数据。导出 schema 与 LAND iOS 现有 `Place` v1 并不相同，尚未接入 App。

## 人工审核 CLI（第二阶段 2A）

统一队列列出状态为 `NEEDS_REVIEW` 的 Candidate、Mention 和 POI，并可按类型或城市过滤：

```sh
./land-data review list
./land-data review list --type candidate --city mock-review-city
./land-data review list --type mention
./land-data review show <id>
./land-data review history <id>
./land-data review approve <id>
./land-data review reject <id> --reason "原因"
./land-data review resolve <candidate_id> --poi <poi_id>
./land-data review promote <candidate_id>
```

`show` 展示审核原因说明、完整对象字段、来源 URL/ID、保留中的 Raw 记录身份、地点证据、Candidate 的可能 POI 匹配以及 Mention 的推荐信号。Raw 正文不会作为 Raw payload 打印；来源允许保留的规范化标题与文本会显示。常见 credential 模式在输出时再次遮盖。Raw 过期删除后，保留的 SourceContent 和来源身份仍可追溯。`reviewer` 暂固定为 `local-user`。

`reject` 必须提供非空原因。`approve` 适用于已绑定正式 POI 的 Mention、已解析 Candidate、或字段与坐标有效的 POI；未解析 Candidate 请使用 `resolve` 或 `promote`。`resolve` 校验 POI 存在、未被拒绝，且城市、国家、类别一致；原 Candidate 与证据不删除，关联 Mention 绑定正式 POI，RecommendationSignal 的 Mention 引用保持不变。`promote` 仅在 Candidate 已有名称、城市、国家、类别和有效坐标时创建正式 POI，不猜测缺失字段。两者在同一 SQLite 事务内更新对象、追加 Candidate 和 Mention 的 ReviewDecision，并补记来源；然后重新计算验证、评分与地图可见性。原机器审核原因保存在 Decision 的 `metadata.previous_review_reasons`，审核历史表禁止 UPDATE/DELETE。

### 可复现的 Mock 歧义场景

`mock-review-city` 是虚构城市（国家代码 `ZZ`），含 East/West 两个同别名的 Rooftop 和一篇虚构帖子：“这家 rooftop 晚上 sunset 很漂亮，很适合 date。”以下命令使用独立 Mock 会话与数据库：

```sh
./land-data --session-file .local/review-demo-session.json auth mock-social
./land-data --db .local/review-demo.db --session-file .local/review-demo-session.json pipeline --city mock-review-city --output .local/exports/review-before.json
./land-data --db .local/review-demo.db review list --city mock-review-city
./land-data --db .local/review-demo.db review show cad453b6b6e20f38543e7a8c
./land-data --db .local/review-demo.db review show f2cd3bc4f6ba33dbe7e18dc4
./land-data --db .local/review-demo.db review resolve cad453b6b6e20f38543e7a8c --poi e85038c757bc17ed503a9f4b --reason "Mock post matches East venue"
./land-data --db .local/review-demo.db review history cad453b6b6e20f38543e7a8c
./land-data --db .local/review-demo.db export --city mock-review-city --output .local/exports/review-after.json
```

在全新数据库上，审核前导出 2 个 POI、0 条 Mention，两者推荐聚合都为空。审核后导出 2 个 POI、1 条 Mention；East 的 `sunset` 与 `date` 聚合各为 1，West 仍为空。再次运行示例前请使用新的数据库文件名。

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
  geoscope.py            采集范围与 bbox 校验
  osm.py                 单一受限 Overpass Adapter、响应解析与类别映射
  social_processing.py   通用社交地点与推荐提取接口和确定性实现
  policy.py              字段 allowlist、保留期限和敏感字段过滤
  storage.py             Pipeline 使用的 Storage 接口
  sqlite_storage.py      SQLite 表与唯一 SQL 实现
  processing.py          规范化、提取、匹配、验证、评分、审核
  review.py              审核队列、上下文、裁决与 Candidate resolve/promote
  pipeline.py            阶段编排
  export.py              可部署 Mock 数据集
  cli.py                 命令行入口
tests/                  自动化测试
scripts/social_acceptance.py  完全离线的 2C 验收脚本
```

## 当前边界

- Mock 提取使用 fixture 提供的地点和推荐标注；没有自然语言地点提取、语义分类或 LLM。
- Raw 字段保留期已实现；规范化与衍生数据的跨层删除传播、平台许可审计和长期修订策略尚未实现。
- 匹配算法仅适合基础精确场景；同名地点、多语言变体和复杂坐标冲突需要后续审核与算法改进。
- 当前仅有本机 `local-user`，没有多用户并发锁、权限系统或撤销/合并审核工作流。
- 机器原因码覆盖当前基础规则；跨来源冲突检测与复杂同名实体匹配仍待后续阶段。
- OSM 只支持少数确定性类别、bbox 查询和小样本采集；Way/Relation 使用 Overpass 返回的几何中心，中心可能不在多边形内。scope 隔离根据 POI FieldProvenance 追溯 SourceContent 的采集 scope；没有复杂地理层级。OSM 更新同步只覆盖它原始创建的 POI，不提供完整版本历史。
- 社交地点识别目前依赖 fixture 标注，规则推荐仅覆盖少数示例词；没有真实平台 Adapter。`UNAVAILABLE` 内容的旧 Derived 行保留用于追溯但不发布；删除地点后旧 Mention 失活，但旧 Candidate、Event 和审核历史没有通用版本修订系统。
- 当前导出是 LAND-Data 的独立 schema；LAND iOS 现有 `Place` 契约没有 Mention/Signal 列表，接入需要单独设计并经确认。
