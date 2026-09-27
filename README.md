# LAND-Data

LAND 的独立地点与推荐数据基础设施。2B 增加了限量的 OpenStreetMap / Overpass 结构化来源；2C 增加平台无关的社交内容处理层；2D-2 Part A 增加 Meta 官方 Threads API Adapter。Threads 目前只通过模拟 API 测试，尚未获得 Meta OAuth 授权或进行真实验收。社交内容可选择使用千问进行文本抽取，默认仍走确定性处理；不修改相邻的 LAND iOS 项目。

## 运行

需要 Python 3.9+；运行和测试只使用标准库。仓库根目录的入口无需安装依赖：

```sh
./land-data auth-status mock-social
./land-data auth mock-social
./land-data pipeline --city mock-tokyo
./land-data review list
```

也可使用 `PYTHONPATH=src python3 -m land_data ...`。`pyproject.toml` 提供标准的 `land-data` 安装入口，供具备构建工具的环境使用。`auth` 仅在 `.local/mock-sessions.json` 记录一个布尔 Mock 状态，没有账号、密码、Cookie 或 Token。未运行 `auth mock-social` 时，包含该来源的 Pipeline 返回 `AUTH_REQUIRED`，退出码为 2。

### JFK 官方抵达指引试点

`multi-pipeline` 还可调度 `jfk-arrival-guidance`。它只读取 JFK 官方公共交通与 AirTrain 两条 HTTPS 页面，最多请求两页、每页最多 500KB，并只保存三个白名单章节中的短文本证据、页面 URL、章节位置、采集时间和章节版本哈希。票价、时刻/班次、施工提示、实时航班和动态路线均不采集。

它不进入 POI 或已核验 Guide 链路：首个版本和每个章节版本变化均为 `NEEDS_REVIEW`；未变化计入 `duplicate_count`。网络/页面读取失败会保存 `UNAVAILABLE` 并返回 `RETRY_REQUIRED`；页面可读但白名单章节无法可靠定位或没有安全的有界正文证据时，保存 `availability=AVAILABLE`、`parse_status=PARSE_ERROR`，返回 `SUCCEEDED_WITH_REVIEW`，不冒充网络故障也不自动发布。各章节独立记录状态，不影响其他来源的结果。

```sh
PYTHONPATH=src python3 -m land_data --db .local/jfk-guidance.db multi-pipeline \
  --source-config config/multi-source.json \
  --only-source jfk-arrival-guidance
```

Port Authority 的 [网站免责声明](https://www.jfkairport.com/website-disclaimer) 将内容定位为一般信息、可不经通知变动、且不应在未独立核验下依赖；因此该试点仅作内部待审来源快照，不作为公开再发布或自动化旅行建议的授权依据。

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

## 小红书终端采集

`xhs` 命令使用一个独立 Chrome 资料目录，由本人正常扫码登录后，对小红书网页执行有界 Query 搜索、有限滚动和可见详情 DOM 读取。采集阶段不调用 LLM，不读取或导出 Cookie、密码、localStorage 或验证码，不调用私有接口，也不会绕过登录和访问限制。

```sh
# 首次登录；需要验证时也运行这条命令
./land-data xhs login

# 检查浏览器、采集和待处理队列
./land-data --db .local/land-data.db xhs status

# 默认读取 config/xiaohongshu-queries.json，零 LLM
./land-data --db .local/land-data.db xhs collect

# 只对 PENDING_LLM 队列调用千问
./land-data --db .local/land-data.db xhs process --city new-york --llm auto --limit 20
```

来源保持 `export_enabled=false`。千问只做未结构化文字抽取，不负责事实验证、坐标确认或自动发布。日常运行先看[操作说明](/Users/mars/Downloads/LAND-DATA/操作说明.md)；完整状态机、checkpoint 和数据边界见[小红书终端工具说明](/Users/mars/Downloads/LAND-DATA/docs/xiaohongshu-terminal-tool.md)。

## 局域网人工审核 Dashboard

LAND-DATA 提供实际可运行的本地 Dashboard。它把需要人工处理和已经自动完成的对象放在同一列表中，包括 POI、知识候选/草稿、Event、官方指导快照、来源发现、SourceContent、Raw、运行记录和处理状态。页面支持搜索、状态桶/类型筛选、详情、审核历史、approve/reject/resolve/promote，以及 QAU 发布准备。

社区内容在进入审核前会运行版本化的本地 classifier。它在本机完成地点、活动、文化礼仪与无关内容分流，保存可解释信号和置信度，不调用 LLM，也不会把分类结果当成事实核验或发布批准。使用 `./land-data classifier status --city <city-id>` 查看当前分类。

本机启动（默认只监听回环地址）：

```sh
./land-data --db .local/land-data.db dashboard
```

从同一局域网的其他设备访问时，显式绑定局域网接口：

```sh
./land-data --db .local/land-data.db dashboard \
  --host 0.0.0.0 \
  --port 8765
```

此模式启动时打印带随机 token 的 URL。第一次使用该 URL 后，服务通过 HttpOnly、`SameSite=Lax` Cookie 建立访问会话，并重定向到不含 token 的地址；后续请求使用 Cookie。状态变更还必须携带 CSRF nonce，GET 不会改变状态。请求日志不打印正文、Cookie、token 或密钥。不要把端口暴露到公网。

Dashboard 底部还显示城市/来源计划、采集与处理作业、数据库/Raw/release/LAND 存储位置。完整流程是：创建城市与 Query Bank → 查看计划 → 运行小红书或结构化采集 → 查看进度与存储 → 零模型规则处理 → Dashboard 全量查看与人工审核 → QAU `prepare → diff → verify` → 显式安装 → 下次增量更新。

Dashboard 默认零 LLM。只有人工明确选择候选 ID、指定 `--llm auto`、配置模型并提供 `--limit`，才会调用千问；页面加载、筛选、审核和发布准备均不会调用模型。个人经验可在人工审核后编译为匿名 `editorial_experience`，原帖正文、作者、链接和 ID 不进入公开 QAU 包。

默认不允许 Dashboard 安装 LAND。需要显式打开安装入口：

```sh
./land-data --db .local/land-data.db dashboard \
  --allow-install \
  --land-root ../LAND
```

当前自动发布只覆盖 LAND QAU 知识内容。POI/Place、Event、官方 Guide/指导快照仍缺少到 LAND App 的发布映射和安装契约；它们可以采集、查看和审核，但不能因审核通过或 QAU release 成功就宣称已经进入 App。详细操作见[Dashboard 说明](/Users/mars/Downloads/LAND-DATA/docs/review-dashboard.md)。

## LAND QAU 本地发布

已审核的文化、礼仪和实用知识可以编译到 LAND 当前的完整 QAU 索引。发布链不调用 LLM，并且与采集命令分开：

```sh
./land-data --db .local/land-data.db publish prepare --city new-york --land-root ../LAND
./land-data publish diff --release <release-id>
./land-data --db .local/land-data.db publish verify --release <release-id> --reviewer "mars" --reason "已核对差异与来源"
./land-data publish install --release <release-id> --land-root ../LAND
```

`install` 会备份当前 bundled QAU，原子替换后运行 Swift 测试和 Xcode iOS Simulator 构建；失败会自动恢复原文件。通过 Dashboard 安装必须使用 `--allow-install`；终端 `publish install` 仍然只接受明确 `VERIFIED` 的 release。显式回滚：

```sh
./land-data publish rollback --release <release-id> --land-root ../LAND --reason "回滚原因"
```

小红书原帖、链接和作者身份仍不会发布。通过人工审核的个人经验可被改写为匿名的 `editorial_experience`，在 LAND 中明确显示为“旅行者经验 · LAND 编辑整理”，并且不能作为官方规则依据。动态规则只有经官方/运营方来源独立验证、带有效期且通过人工 release 核验后才能安装。详细操作见[操作说明](/Users/mars/Downloads/LAND-DATA/操作说明.md#11-将已审核知识发布到-land)。

## 多来源无人值守入口（NYC Open Data、OSM 与 NYC Parks Events）

`multi-pipeline` 读取 `config/multi-source.json`，按配置运行 NYC Open Data、OSM、NYC Parks Events 和已配置的 JFK 内部试点。每个来源使用自己的开始/结束时间、成功或失败状态、新增数、更新数、重复数、失败原因、下次重跑状态、HTTP 请求数、重试数、endpoint 和耗时；这些字段兼容迁移后持久化在同一 SQLite 的 `source_runs` 表中，旧记录缺失的指标保持 `NULL`。来源之间独立，单个来源失败不会回滚已经完成的其他来源；命令在任一来源失败时返回退出码 1，并且标准输出始终是 JSON，适合普通系统定时器或 cron：

```sh
./land-data --db .local/multi-source.db multi-pipeline
```

### macOS launchd（NYC、OSM 与 Events 分开调度）

入口脚本是 [land_data_unattended.sh](/Users/mars/Downloads/LAND-DATA/scripts/land_data_unattended.sh)，参数为 `nyc`、`osm`、`events` 或 `both`；锁按来源隔离，同一来源不重叠。NYC、OSM 和 Events 均可独立每日运行；每次来源失败会留在 `source_runs`，外层尝试最多重跑 2 次，仍失败则保留非零退出码。

```sh
mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$HOME/Library/Application Support/LAND-DATA"
cp scripts/land_data_unattended.sh "$HOME/Library/Application Support/LAND-DATA/land_data_unattended.sh"
cp config/launchd/com.land-data.nyc-osm.plist "$HOME/Library/LaunchAgents/com.land-data.nyc.plist"
cp config/launchd/com.land-data.osm.plist "$HOME/Library/LaunchAgents/"
cp config/launchd/com.land-data.nyc-parks-events.plist "$HOME/Library/LaunchAgents/"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.land-data.nyc.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.land-data.osm.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.land-data.nyc-parks-events.plist"
launchctl print "gui/$(id -u)/com.land-data.nyc"
launchctl print "gui/$(id -u)/com.land-data.osm"
launchctl print "gui/$(id -u)/com.land-data.nyc-parks-events"
tail -f .local/launchd/nyc.runs.jsonl
# Events uses its own source log; OSM uses .local/launchd/osm.runs.jsonl.
tail -f .local/launchd/events.runs.jsonl
```

手动重跑：

```sh
./scripts/land_data_unattended.sh nyc
./scripts/land_data_unattended.sh osm
./scripts/land_data_unattended.sh events
```

停止并移除：

```sh
launchctl bootout "gui/$(id -u)/com.land-data.nyc"
launchctl bootout "gui/$(id -u)/com.land-data.osm"
launchctl bootout "gui/$(id -u)/com.land-data.nyc-parks-events"
rm "$HOME/Library/LaunchAgents/com.land-data.nyc.plist" "$HOME/Library/LaunchAgents/com.land-data.osm.plist" "$HOME/Library/LaunchAgents/com.land-data.nyc-parks-events.plist"
```

重复运行相同配置不会重复创建 Raw、SourceContent 或 Derived 数据；结果中的 `duplicate_count` 会反映已见来源身份。`--only-source osm` 或 `--only-source nyc-open-data-restaurants` 可用于单来源重跑和故障恢复验证。该入口不接受 `--llm-extract`，采集、去重和结构化字段解析均为现有确定性代码路径，不调用 Codex 或千问。

### 零 LLM 官方数据源发现与统一入口

`discover-sources` 使用 Socrata 官方 Catalog API 在配置的旅行主题下有界搜索公开数据集。第一版仅接受 `data.cityofnewyork.us`，随后对少量候选调用官方 metadata API 检查 asset type、机器接口和字段 schema。结果写入 SQLite 的 `source_discovery_candidates`，默认是 `DISCOVERED` 或 `NEEDS_REVIEW`；新发现的未知 schema 不会自动进入 POI、Event、现有采集配置或公开导出。候选按 provider + dataset id 去重，查询进度、失败、HTTP 请求数、重试数和最近运行写入独立表。整个发现路径不调用 Codex、千问或其他 LLM。

```sh
# 运行一次有界发现，再运行 new-york 已配置来源（默认零 LLM）
./land-data --db .local/land-data.db auto-collect --city new-york
# 查看已发现候选
./land-data --db .local/land-data.db discover-sources --list-candidates
# 查看最近发现运行
./land-data --db .local/land-data.db discover-sources --list-runs
```

需要只发现或只运行现有来源时，可使用 `auto-collect --discovery-only` 或 `auto-collect --collection-only`。独立的 [source_discovery_unattended.sh](/Users/mars/Downloads/LAND-DATA/scripts/source_discovery_unattended.sh) 与 [launchd 配置](/Users/mars/Downloads/LAND-DATA/config/launchd/com.land-data.source-discovery.plist) 默认每周运行一次；它只更新发现候选，不启用任何未知 dataset。若 Catalog 或 schema 探测部分失败，成功查询和已保存候选会保留，并以机器可读的 `PARTIAL_FAILURE` / `RETRY_REQUIRED` 返回。

安装独立发现定时入口：

```sh
mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$HOME/Library/Application Support/LAND-DATA"
cp scripts/source_discovery_unattended.sh "$HOME/Library/Application Support/LAND-DATA/"
cp config/launchd/com.land-data.source-discovery.plist "$HOME/Library/LaunchAgents/"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.land-data.source-discovery.plist"
launchctl print "gui/$(id -u)/com.land-data.source-discovery"
```

### NYC Open Data 有界增量

NYC 餐饮检查数据集的行是检查/违规引用，不是一店一行；`CAMIS` 才是餐厅稳定标识，同一检查可能有多行，评分也可能在裁决后变化。`record_date` 是生成整份数据抽取的日期，而不是单店修改时间。采集器不使用 `grade='A'` 过滤，卫生等级不参与餐厅推荐或可见性判断。

每轮先用一个轻量 `$limit=1` 聚合查询锁定当前 `record_date` 的**日期窗口**。若抽取日未变化且上一轮已完成，7 天安全重扫窗口内直接返回 `NOT_NEEDED`，不重新扫描 CAMIS；抽取日变化后才按页扫描。SQLite checkpoint 保存固定快照日与末尾 CAMIS：中断时继续同一快照，新的抽取日不会插入并打乱当前轮；每页由 Pipeline 持久化后才推进 checkpoint，失败页会被安全重读并去重。

默认范围是 Manhattan，最早允许快照为 `2026-09-01T00:00:00.000`，每个分组请求最多 100 个 CAMIS、每次最多 20 页；另有一个 `$limit=1` 快照锚点请求。基于 2026-09-21 的官方聚合结果（109,639 条引用行、12,518 个 CAMIS），一轮需要 `ceil(12,518 / 100) = 126` 个分组页，分为 7 次定时运行（前 6 次各 20 页，最后一次 6 页），共 127 个 HTTP 请求。请求超时上限 15 秒，串行最坏网络等待为 31 分 45 秒；实际调度周期另计。

每个内容变更保留新的 Raw 快照、复用相同 `SourceContent` 身份，并由运行 JSON 的 `new_count`、`updated_count`、`duplicate_count` 区分首次发现、变化和未变；NYC 输出还包含快照锚点、请求页数、产出餐厅数和 checkpoint。字段 provenance 继续通过结构化 `field_sources` 写入 POI。

这是当前公开抽取的有界餐厅快照，不是引用级历史镜像。仍无法发现源端未发布的更改、关闭/删除后不再出现在当前抽取中的 CAMIS、未改变 `record_date` 的回填，以及同一 CAMIS 内彼此不一致的历史违规、评分或等级详情；聚合字段也不能声称与同一条“最新检查引用行”严格同源。

NYC 餐厅与 OSM 的运行验收仍按上文范围执行；NYC Parks Events 使用独立的结构化适配器和每日入口，真实网络验收需在可访问 NYC Open Data 的宿主环境完成。NYC 的配置容量为 Manhattan 每页 100 个 CAMIS、每次 20 页，但本轮只执行了官方聚合与 2 个 CAMIS 的查询验证，未用该容量扩展实际采集；也没有处理历史 403 或修改 LAND App。离“无人值守多来源采集器”仍缺少：可靠生产网络/凭据与权限管理、来源级退避和限流策略、跨进程锁与并发调度、失败告警和指标、配置版本/变更审计、长期运行记录归档与清理、数据源删除/撤回传播，以及更多来源的契约和恢复验收。

### NYC Parks 未来 14 天活动

`nyc-parks-events` 使用 NYC Parks Public Events – Upcoming 14 Days 官方 Socrata 数据集（`w3wp-dpdi`）。`guid` 是活动稳定身份，分页按 `guid` 续跑；标题、纽约本地时间、园区 ID/名称、园内位置、类别、描述、报名链接和来源坐标作为 Event 字段及字段 provenance 保存。时间明确按 `America/New_York` 解析后存为 UTC ISO 时间，非法时间或坐标进入待审，不猜测、不地理编码。

活动不会创建 POI 或 RecommendationSignal，也不会混入现有 POI 导出。只有一次完整分页成功后，才会根据当前完整快照处理消失活动：结束时间已过的记录标记 `EXPIRED`，结束前消失的记录标记 `UNAVAILABLE` 并待核对；分页失败或只完成部分页面不会触发撤回。活动重新出现时复用原 `guid` 身份并进入待审。

```sh
./land-data --db .local/nyc-events.db multi-pipeline \
  --source-config config/multi-source.json \
  --only-source nyc-parks-events
./scripts/land_data_unattended.sh events
```

该来源当前只写入内部 EventCandidate/Event 与 provenance，保持 `export_enabled=false`，待单独定义活动导出契约后再开放公开输出。

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

## POI Enrichment（小范围验证）

`Pipeline.validate(enrichment_plan)` 在现有 Entity Resolution 后，对指定 GeoScope 内仍未匹配的社交 Candidate 做受限补全。先读取计划中明确列出的官方场地方/品牌目录页，再对尚未匹配的名称用 OSM Adapter 做 bbox 内精确名称查询；每次最多处理 5 个 Candidate，OSM 每次最多 20 个对象，超时为 2–20 秒。补全来源只产生 `SourceItem`，后续仍走 Raw → SourceContent → Candidate → Resolution → Validation / Review / Export。没有配置计划时，原 Pipeline 行为不变。网络或来源失败会保留 Candidate 供审核，并在结果中返回错误。

CLI 可在已有的社交 SourceContent 解析后运行 `validate --scope <scope.json> --enrichment-references <references.json>`。References JSON 是最多 5 项的数组，每项包含 `kind` 和 `url`；当前只支持经过明确允许的 Marunouchi tenant 页、KITTE food index/access 页与品牌门店目录页。示例格式：`[{"kind":"marunouchi_tenant","url":"https://www.marunouchi.com/en/tenants/12345/"}]`。`pipeline` 也支持相同参数。官方目录来源不接受独立 `--source official-directory`，以免无界发现或误用。

店铺坐标必须来自有店铺身份的地图链接；设施地图点只能建父 POI，不能作为子店精确坐标。`POI.parent_poi_id` 表示父子关系，`coordinate_precision` 标出 `VENUE_PIN`、`BUILDING` 或 `OSM_OBJECT`，两者随 POI 导出。字段级 Provenance 保留坐标来源页和原字段位置。酒店被文本匹配时，如果同篇内容提取出餐饮信号，Mention 保留酒店 POI 绑定，同时以 `POSSIBLE_INTERNAL_VENUE` 进入审核；不会自动创建内部餐厅、酒吧或咖啡店。人工确认可沿用 Review CLI。

2026-09-21 用先前已观察的三篇小红书样本在内存数据库验证，没有重新采集小红书或保存帖子正文文件：THE FRONT ROOM 由 Marunouchi 官方 tenant 页建立 cafe POI；SAZA COFFEE 由 KITTE 目录和品牌门店目录交叉确认，使用品牌门店地图点，父 POI 为 KITTE；AMAN Tokyo 由 OSM `node/13585773984` 匹配酒店，餐饮信号进入 `POSSIBLE_INTERNAL_VENUE` 审核。第一次共有 7 Raw、7 SourceContent、7 Candidate、4 POI、3 Mention、5 Signal；再次运行相同输入后各层数量不变。该验证依赖预先指定的可信目录引用，尚不包含通用网站搜索或自动发现任意品牌目录。

## Social Content Foundation（第二阶段 2C）

`MockSocialAdapter` 只读取虚构 fixture，交给通用的 SourceItem / RawSourceRecord 链路；它不判断地点、推荐或审核状态。`SourceContent` 支持规范 URL、作者、标题、正文、发布时间、Geo Tag、hashtag、语言和仅含结构信息的媒体 metadata；这些字段可缺失。`config/sources.json` 为社交来源分别声明 Raw、Normalized、临时与 Export 字段及保留期。`ACTIVE`、`UPDATED`、`UNAVAILABLE` 是最小内容状态，同一 `platform + external_id` 始终保留一个 SourceContent 身份；不可用内容的 Mention 不再导出。相同 Raw 快照再次采集时只刷新最近采集与到期时间，不增加 Raw 行数；内容恢复可复用原身份。帖子更新后不再支持的 Mention 标为 inactive，保留记录但不参与聚合或导出。

`social_processing.py` 定义 LocationExtractor 和 RecommendationExtractor 接口，目前只实现 `GeoTagExtractor`、fixture 标注的文本地点提取与少量规则推荐词。证据强度统一放在 `EVIDENCE_STRENGTH`；AREA Geo Tag 只能作为低强度区域证据，不能直接变成 POI。每个地点的推荐规则只读取与该地点关联的文本片段，避免一帖多地时把 `study` 和 `cocktail` 同时挂到 Cafe A、Bar B。提取后复用既有 Candidate、Evidence、Mention、Signal、Event、Entity Resolution、Review 和 Export 流程。

完整离线验收不会请求 OSM 网络：

```sh
PYTHONPATH=src python3 scripts/social_acceptance.py --db .local/social-acceptance.db --output .local/exports/social-acceptance.json
```

脚本先用模拟 OSM 响应创建 Example Rooftop、Example Matcha Cafe、Cafe A、Bar B、Example Gallery，再导入六篇虚构帖子。预期 11 Raw / SourceContent / Candidate、5 POI、4 Mention、5 Signal、1 EventCandidate 和 1 Event；导出 5 POI 与 4 Mention。每个 POI 的 Mention 数、信号聚合、来源和审核状态会打印出来。也可在种子 POI 建好后，单独用 `./land-data --db .local/social-acceptance.db pipeline --source mock-social-2c --scope config/social-acceptance-scope.json` 重放 Mock 社交输入。

## Threads 官方 API（第二阶段 2D-2 Part A）

`ThreadsAdapter` 只调用 Meta 官方 Threads Graph API：`GET /keyword_search` 发现最多 5 篇帖子；`GET /{post_id}` 取得帖子详情及 `location_id`；若有地点 ID，再调用 `GET /{location_id}` 读取 `id,name,address,city,country,latitude,longitude,postal_code`。搜索最多 2 页、单次超时 2–30 秒、502/503/504 与网络超时最多重试一次；401/403 会提示 OAuth/权限错误，429 会提示稍后重试。请求使用 User-Agent 与 Authorization header，不把 token 放进 URL、SQLite、导出或普通日志。重定向禁用，避免认证 header 被转发到其他主机。官方 API 端点与字段参考 [Meta 官方 Threads API Postman collection](https://www.postman.com/meta/threads/collection/dht3nzz/threads-api)、[keyword search](https://www.postman.com/meta/threads/request/34203612-b3b2c12a-7ce6-4d86-a3c6-6d31e3b66ea1)、[post details](https://www.postman.com/meta/threads/request/34203612-416ad6d2-214e-415a-bfe4-98c959fe25ad) 与 [location retrieval](https://www.postman.com/meta/threads/request/34203612-5753b234-2a84-4b3f-844d-9f47940e718e)。

需要用户在 Meta Developer 创建 Meta App、加入 Threads use case、配置官方 OAuth 回调并授权。官方 collection 列有 `threads_basic`、`threads_keyword_search` 和 `threads_location_tagging`；其中地点读取所需的精确授权组合、是否要求 App Review / Advanced Access、以及普通公开帖搜索的应用访问条件，**NEEDS MANUAL VERIFICATION**：应以 App Dashboard 及实际授权后的官方 API 响应为准。当前 CLI 不自动运行浏览器 OAuth redirect。`auth threads` 只给出官方授权说明；用户自行完成授权后，在本地环境设置 `LAND_THREADS_ACCESS_TOKEN`，`auth-status threads` 通过 `GET /me?fields=id,username` 验证 token 基本可用性，不宣称已验证 keyword/location 权限。不要把 token 发到聊天、命令行参数或仓库文件。

授权后使用独立数据库进行不超过 5 篇的小样本验收：

```sh
./land-data auth threads
./land-data auth-status threads
./land-data --db .local/threads-acceptance.db pipeline --source threads --city Tokyo --query "用户指定关键词" --search-type RECENT --limit 5 --pages 1 --timeout 15 --output .local/exports/threads-acceptance.json
./land-data --db .local/threads-acceptance.db pipeline --source threads --city Tokyo --query "用户指定关键词" --search-type RECENT --limit 5 --pages 1 --timeout 15 --output .local/exports/threads-acceptance-second.json
./land-data --db .local/threads-acceptance.db review list --city Tokyo
```

`--city` 是本地采集/导出分组键，**不会为 Threads keyword search 添加地理过滤**；应人工核对搜索结果的地点与城市。`--post-id <id>` 可代替 `--query`，通过官方帖子详情端点重新检查一篇已知帖子。如果该 ID 返回 404/410，现有 SourceContent 转为 `UNAVAILABLE`；旧 Derived 记录保留审计，但不贡献内部推荐聚合或公开导出。相同 post ID 重复采集复用 SourceContent、Candidate、Mention、Signal 身份；文本变化通过 hash 进入 `UPDATED`。

来源策略位于 `config/sources.json`。Threads Raw 只保存帖子 ID/URL、正文、作者标识、时间、主题标签、媒体类型、地点元数据等必要字段，保留 1 天；不保存媒体文件、缩略图 URL 或整份 API 响应。Normalized 只保留稳定来源身份、时间、地点标签、状态及正文 hash，不长期保留正文或作者标识。Derived 层可形成 Evidence、Candidate、Mention、Signal 和候选地点的 `location_id` FieldProvenance。已知 POI 名称在无 geo tag 的帖子正文中出现时，保守地生成 `TEXT_EXTRACTED`；不会推断未认识的地点。Threads category 未知时只尝试精确名称和坐标匹配现有 POI，不能自动创建新 POI；区域地点进入审核。**NEEDS POLICY VERIFICATION**：Meta 对字段长期存储、衍生数据保留、展示、删除传播与署名的具体要求尚未确认；因此 Threads 的 `export_enabled=false`，其 Mention 与 Signal 不进入公开 Dataset，也不改变公开推荐聚合。真实验收阶段须先核对政策，再决定是否开放导出。

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

## 局域网人工审核 Dashboard

Dashboard 已覆盖城市创建、来源计划、采集/处理作业、进度与存储位置、全部状态浏览、人工审核、显式候选千问抽取以及 QAU prepare/diff/verify/install。审核范围、局域网安全边界、个人经验匿名发布、官方核验字段和关闭服务说明见[局域网采集与审核 Dashboard 操作说明](/Users/mars/Downloads/LAND-DATA/docs/review-dashboard.md)。终端命令继续作为等价的备用入口；两者复用同一套审核、知识处理和发布服务。

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

### 可选千问文本抽取

Social Processing 可选择使用千问，从 **已获准处理的可见标题、正文、hashtags 与结构化可见 location** 提取地点、父场所/分店/区域上下文、逐地点推荐信号和活动。默认快速路径为 `qwen3.7-flash` 且关闭思考模式。模型输出必须符合 JSON Schema，且每个地点、信号和活动都要带可在输入文本中找到的证据及置信度。地点区分具体 POI、品牌、父场所、区域、车站、城市和方向上下文；方向/区域上下文通常不生成 Mention。短的 RecommendationSignal value 会移除常见评价后缀，无法拆清的并列主题和纯情绪词会丢弃。模型不验证 POI、不提供坐标、不创建 POI，也不决定审核状态；这些工作仍由已有 Pipeline 完成。网络失败重试一次后回退到原确定性提取；明确活动标题可生成低置信度 EventCandidate 待审。

先在本机复制 `.env.example` 为 `.env`，仅由你本人填写 `DASHSCOPE_API_KEY`、`LAND_LLM_BASE_URL`（从千问控制台复制所属区域的 OpenAI 兼容 endpoint）和 `LAND_LLM_MODEL`。`LAND_LLM_TIMEOUT` 建议 60 秒；真实样本中 20 秒发生过超时。`.env` 已被 Git 忽略，CLI 仅在显式添加 `--llm-extract` 时由程序在本机加载；不要把 Key 发到聊天或命令参数中。[千问结构化输出文档](https://help.aliyun.com/en/model-studio/qwen-structured-output)列出支持 `json_schema` 的模型，请选用支持该模式的型号。

```sh
./land-data --db .local/social.db parse --city mock-tokyo --llm-extract
# 也可在已有 social source 的 pipeline 命令末尾加 --llm-extract
```

结果中的 `llm_extraction.calls/cache_hits/fallbacks` 用于验收。缓存键包含 SourceContent ID、可见文本哈希、模型及提示词/Schema 版本；结果缓存只保存结构化结果及短证据，保留期不超过该来源 Raw policy 的天数。文本变化或模型/Schema 版本变化会重新抽取；保留期为 0 的来源不落盘缓存。不会把 HTML、Cookie、session、作者凭据、媒体内容或原始页面响应发送给模型。是否允许将某平台正文提交第三方模型及保留派生证据，仍需按具体来源许可确认。

2026-09-21 使用此前已正常浏览的同一 10 篇小红书内容（其中 3 篇复用旧样本、2 篇仅有标题）复测；只把必要的可见标题/正文摘录通过 `scripts/llm_social_acceptance.py` 的 stdin 输入，脚本不保存帖子输入文件。人工标注的目标地点有 19 条，最终首跑提取 17 条，17 条均正确：Mention precision 100%（17/17）、recall 89.5%（17/19），误报 0、漏报 2。漏掉的是茑屋内的星巴克和仅有标题的 TOFROM YAESU；长帖中的 11 个咖啡店均保留。得到 21 条短 RecommendationSignal，未保存整句或纯情绪词；活动标题生成 1 条宝可梦展 EventCandidate，尚无已证实场地/日期，因此待审。首跑 11 次模型请求（1 次网络重试），平均每帖 9.71 秒；第二遍 10 次缓存命中、0 次新请求。此次仅测 Extraction 与原有解析/审核，未调用 Enrichment；基线 OSM POI 无直接匹配，17 条 Mention 均待审。相同文本的多次独立模型请求仍会发生地点和信号输出波动，此处数据只代表最终这次实测。

`config/sources.json` 声明 Adapter capability、访问模式、原始字段 allowlist、是否保存来源 URL、规范化字段 allowlist、临时字段、导出字段和 Raw 保留天数。收集时递归剔除明显的敏感键，并清除来源 URL 中明显的认证参数。到期 Raw 在下一次 collect/parse 时删除；允许保留的 SourceContent 和 Derived 数据分开存储。临时字段只可在同一进程的 `pipeline` 调用中参与处理，不会落盘，之后无法从 Raw 回放该字段。

Threads 已提供环境变量 Token 接口和 `/me` 基础验证，但没有内置 OAuth redirect、权限枚举或自动刷新。平台许可、Normalized/Derived 删除传播仍待确认；会话仅交给 Source Access 层，不能进入模型、SQLite 数据集、日志或导出。没有验证码、2FA 或访问控制绕过能力。

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
  threads.py             Meta 官方 Threads API 受限 Adapter
  social_processing.py   通用社交地点与推荐提取接口和确定性实现
  llm_extraction.py       可选千问 JSON Schema 抽取、可见文本过滤、证据校验与缓存
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

- 默认仍使用确定性 Social Processing；LLM 地点抽取仅在 `--llm-extract` 明确启用且本机已配置凭据时运行。LLM 抽取结果需要真实小样本验收，不能把 Schema 合格等同于地点真实或推荐归属正确。
- Raw 字段保留期已实现；规范化与衍生数据的跨层删除传播、平台许可审计和长期修订策略尚未实现。
- 匹配算法仅适合基础精确场景；同名地点、多语言变体和复杂坐标冲突需要后续审核与算法改进。
- 当前仅有本机 `local-user`，没有多用户并发锁、权限系统或撤销/合并审核工作流。
- 机器原因码覆盖当前基础规则；跨来源冲突检测与复杂同名实体匹配仍待后续阶段。
- OSM 只支持少数确定性类别、bbox 查询和小样本采集；Way/Relation 使用 Overpass 返回的几何中心，中心可能不在多边形内。scope 隔离根据 POI FieldProvenance 追溯 SourceContent 的采集 scope；没有复杂地理层级。OSM 更新同步只覆盖它原始创建的 POI，不提供完整版本历史。
- Threads Adapter 已实现但未获得官方授权进行真实验收；`location_id` 覆盖率及官方字段/权限的实际可用性未知。规则推荐仅覆盖少数示例词。`UNAVAILABLE` 内容的旧 Derived 行保留用于追溯但不发布；删除地点后旧 Mention 失活，但旧 Candidate、Event 和审核历史没有通用版本修订系统。
- 当前导出是 LAND-Data 的独立 schema；LAND iOS 现有 `Place` 契约没有 Mention/Signal 列表，接入需要单独设计并经确认。
