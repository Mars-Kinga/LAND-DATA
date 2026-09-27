# LAND-DATA 局域网审核 Dashboard

本文是当前可用的人工审核 Dashboard 操作说明。Dashboard 只使用本机 LAND-DATA SQLite 数据库，把采集结果、机器处理结果、人工审核结果和发布状态放在一个局域网页面中。打开页面、筛选、查看详情和执行审核都不会调用千问；安装 LAND 还需要显式启动参数和独立的发布操作。

## 1. 一条完整链路

```text
创建城市与 Query Bank
  → 查看城市/来源计划
  → 启动小红书或结构化来源采集
  → 查看作业进度、数据库和 Raw 存储位置
  → 规则处理（默认零 LLM）
  → Dashboard 查看全部对象
  → 人工审核、地点匹配、官方核验
  → QAU `prepare → diff → verify`
  → 显式允许后 `install` 到 LAND
  → 下一轮增量采集、重处理和发布

Dashboard 已覆盖城市建档、来源计划、登录窗口触发、采集与处理作业、作业进度、数据位置、全部对象、人工审核以及 QAU 发布流程。手动增量更新时，在同一城市上重新启动相应作业；长期定时更新继续使用现有 launchd。
```

Dashboard 需要同时显示两类内容：

- 需要人工操作的对象：`NEEDS_REVIEW`、`DRAFT_NEEDS_REVIEW`、冲突、缺坐标、活动和个人经验草稿；
- 不需要人工操作的对象：`AUTO_APPROVED`、已发布、已拒绝、`NOT_NEEDED`、重复、已过期和来源失败记录。

“不需要审核”表示当前状态不要求人工动作，不表示内容必然适合公开发布。公开发布仍受来源策略、字段完整性、有效期、release 验证和 LAND 构建检查限制。

## 2. 启动

启动前先进入克隆后的 LAND-DATA 仓库根目录，并确认要使用的数据库。若当前目录已经在仓库内，可以用 Git 自动定位根目录：

```bash
cd "$(git rev-parse --show-toplevel)"
./land-data --db .local/land-data.db xhs status
```

当前启动命令为：

```bash
./land-data --db .local/land-data.db dashboard
```

启动命令会输出 JSON，包含监听地址、端口、访问 URL、是否要求访问 token 和是否允许 LAND 安装。前台运行时使用 `Ctrl-C` 关闭；端口被占用时命令失败，不会静默换端口。

推荐默认值：

- 监听地址：`127.0.0.1`；
- 端口：明确的固定默认端口，或由 `--port` 指定；
- 数据库：显式的 `--db`，未指定时才使用 `.local/land-data.db`；
- LLM：关闭；Dashboard 页面加载、筛选和审核按钮均不触发 LLM。

## 3. 局域网访问与安全边界

只在需要从同一局域网的另一台设备操作时，才显式绑定局域网地址：

```bash
./land-data --db .local/land-data.db dashboard \
  --host 0.0.0.0 \
  --port 8765
```

使用前应在本机查看实际局域网地址，然后只把该地址发给同一可信网络中的审核者。不要将 Dashboard 端口转发到公网，不要在公共 Wi-Fi、共享办公网络或未受控的 VPN 网段上开启写操作。

局域网模式的实际安全语义如下：

1. 绑定 `0.0.0.0` 时，启动命令生成随机访问 token，并打印一次性入口 URL；
2. 第一次用该 URL 访问时，服务设置 HttpOnly、`SameSite=Lax` 的访问 Cookie，然后重定向到不带 token 的干净路径；后续请求依赖 Cookie，不需要把 token 继续放在 URL 中；
3. 每个浏览器会话还有独立 CSRF nonce；所有 POST 审核和发布操作都必须通过 `X-CSRF-Token` 校验；GET 不改变状态；
4. Host 仅接受 localhost、回环地址、私有 LAN 地址或 `.local` 主机名；非本机/LAN 请求被拒绝；
5. 请求日志关闭，避免把 URL query、正文、作者标识、Cookie、密码或 token 写入日志；详情只在局域网页面显示；
6. 默认不绑定 `0.0.0.0`，必须显式选择；关闭服务后端口释放；审核动作另有 append-only Dashboard 审计记录。

局域网启动：

```bash
./land-data --db .local/land-data.db dashboard \
  --host 0.0.0.0 \
  --port 8765
```

如果需要让 Dashboard 执行已验证 QAU release 的安装，必须显式启动：

```bash
./land-data --db .local/land-data.db dashboard \
  --host 0.0.0.0 \
  --port 8765 \
  --allow-install \
  --land-root ../LAND
```

`--allow-install` 只打开 Dashboard 的安装入口，不跳过 release 的 `VERIFIED`、blocker、hash、LAND 构建和回滚保护。

当前没有配置 HTTPS、用户系统和公网鉴权时，Dashboard 只能在可信局域网内短时使用。若需要公网访问，应先增加正式身份认证、HTTPS、审计和权限分级，不能仅把监听地址改成公网地址。

## 4. 页面和筛选

首页提供统一列表、详情、审核动作和发布准备面板。列表同时包含需要人工审核和不需要人工审核的对象，不会只留下待办队列。当前支持关键词（标题、摘要、城市、ID）、状态桶和对象类型筛选。

对象类型包括 POI Candidate/Mention/POI、知识候选/草稿、EventCandidate/Event、官方指导快照、来源发现、SourceContent、Raw、来源运行和处理状态。状态桶包括 `REVIEW_REQUIRED`、`AUTO_ACCEPTED`、`REVIEWED_ACCEPTED`、`REVIEWED_REJECTED`、`FAILED_OR_BLOCKED` 和 `PUBLISH_READY`。

页面的运行状态和控制面板使用以下接口：

| 页面 | 接口 | 内容 |
| --- | --- | --- |
| 城市与来源计划 | `/api/workflow/cities`、`/api/workflow/plan` | 已配置城市、可用来源、缺失配置、登录要求和 LLM 默认策略 |
| 启动作业 | `POST /api/workflow` | 创建城市、启动采集/处理/发布准备、取消作业；需要 CSRF、审核人、原因和明确确认值 |
| 作业与进度 | `/api/workflow/jobs`、`/api/workflow/job` | 持久化状态、百分比、计数、错误、模型调用上限和日志位置 |
| 数据位置 | `/api/workflow/storage` | SQLite、本地 Raw、XHS capture、release、LAND Raw 和作业日志目录 |

发布面板显示 `/api/publish/status` 返回的 release 状态、blocker 和可发布包。

详情页必须能区分“来源证据”和“模型/规则推断”。至少显示：

- 对象字段、城市、类别、坐标和候选 POI；
- 来源名称、来源内容 ID、采集时间和来源状态；
- 保留期和 `expires_at`；
- 规则处理结果、LLM 版本、模型、缓存状态和调用时间（如果曾调用）；
- 机器审核原因和解释；
- 官方核验字段和 provenance；
- 既有人工审核历史；
- 当前对象是否能够进入 LAND release，以及阻塞原因。

小红书原始正文只允许在本机审核页面按来源保留策略短期显示。它不能进入发布包、公开导出、前端静态资源或远程错误上报。

## 5. 状态含义

| 状态 | 说明 | 人工动作 |
| --- | --- | --- |
| `NEEDS_REVIEW` | 机器无法安全决定，或存在冲突/低置信度 | 查看证据后批准、拒绝、匹配或提升 |
| `DRAFT_NEEDS_REVIEW` | LLM 仅生成编辑草稿，尚未成为 LAND 内容 | 核对原文、改写内容、补官方核验或拒绝 |
| `AUTO_APPROVED` | 规则和来源门槛已满足自动批准 | 可查看；只有纳入 release 时才继续审核 |
| `REJECTED` | 人工明确拒绝，必须保留原因 | 不会被验证流程自动改回批准 |
| `EDITORIAL_ACCEPTED_FOR_BRIDGE` | 人工接受为可编译的 LAND 知识输入 | 等待 `publish prepare` |
| `NOT_PUBLISHED` | 已接受但尚未进入 release | 生成 release 前仍可查看和撤回 |
| `PUBLISHED` | 已进入已验证并安装的 release | 由 release 历史和 App 版本追踪 |
| `DISCOVERED` | 来源发现候选已保存，尚未接受为可用来源 | 可批准或拒绝来源候选；接受来源不等于发布其数据 |
| `SUPPORTED` | 人工接受来源候选作为后续集成输入 | 只读；每条新采集内容仍需单独审核 |
| `NOT_NEEDED` / `DUPLICATE` | 来源检查完成，没有新内容 | 只读展示，不需要审核 |
| `FAILED` / `RETRY_REQUIRED` | 来源或持久化失败，已有成果保留 | 修复来源后重跑，不要手工伪造结果 |
| `EXPIRED` / `UNAVAILABLE` | 内容已过期或来源不可用 | 重新核验或从下一 release 移除 |

## 6. 人工审核操作

### 6.1 Candidate / Mention / POI

Dashboard 的按钮必须与现有审核语义一致：

- `Approve`：仅适用于已有正式 POI 绑定的 Mention、已解析 Candidate 或字段完整的 POI；
- `Resolve to POI`：把 Candidate 绑定到已有 POI，校验城市、国家和类别；
- `Promote`：只在名称、城市、国家、类别和有效坐标齐全时创建新 POI；
- `Reject`：必须输入非空原因；
- `History`：显示追加式审核记录、审核人、时间、旧状态、新状态和原因。

不能通过前端绕过缺字段、坐标冲突、来源不可用或已拒绝对象的校验。每次写操作应返回新的状态和决定 ID，并刷新队列计数。

### 6.2 个人经验

个人经验可以被接受，但只以匿名编辑内容进入 LAND：

- 页面标记为“旅行者经验 · LAND 编辑整理”；
- 发布类型为 `editorial_experience`；
- 不显示原作者、原帖链接、帖子 ID、原始标题或可检索的整段原文；
- 不得标记为官方规则或“已核验”；
- 不能支持官方规则判断；
- 必须显示适用范围、经验性质和可能随时间变化的提示。

人工审核者应确认：内容确实来自可见个人经验，改写没有增加原文不存在的事实，地点/城市范围没有被扩大，且不会把个人体验写成普遍结论。

### 6.3 文化、礼仪和规则

文化观察、礼仪建议和个人建议可以按经验接受。价格、营业时间、入场条件、交通规则、安全规定、航班、施工、活动时间等动态事实必须绑定独立官方或运营方来源，并填写：

- `reference_url`；
- `reference_kind`：`official` 或 `operator`；
- `reference_title`；
- `verified_at`；
- `valid_until`（适用时必须填写）；
- 核验人和核验理由；
- 与答案块对应的 provenance/support binding。

没有这些字段的动态规则不能进入 release。不能用小红书个人经验替代官方核验。

### 6.4 选择少量内容交给模型辅助整理

模型调用默认是 `0`。在审核列表上方的“用千问辅助整理少量候选”区域，可以：

1. 保持“只看需要审核”，或切换到其他列表后确认当前范围；
2. 点击单条记录左侧的复选框，或填写数量 `N` 后点击“选择当前列表前 N 条”；
3. 检查已选数量、模型名称和“预计最多调用”次数；
4. 填写城市区块中的操作者和本次原因，再点击“对已选内容启动模型辅助审核”。

只有类型为 `knowledge_candidate` 且状态为 `NEEDS_REVIEW` 的对象可勾选。地点候选、活动、官方快照、来源记录和已经生成的知识草稿会显示“不可选”及原因。`N` 大于当前可选项时，页面会选择全部可选项，不会制造空的候选 ID。

启动请求仍需 CSRF、审核人、原因、明确模型和硬上限。服务端会再次检查城市、类型和状态；手写请求不能把 POI 或 Event ID 冒充知识候选。调用上限不会超过已选数量，已存在缓存时真实调用数可能更低。

模型结果只写入 `DRAFT_NEEDS_REVIEW`，作业不会自动批准、发布或安装。作业失败会显示在进度区和日志中，不会伪装成成功；完成后要回到审核列表逐条查看原文证据、模型草稿、验证要求和审核历史。模型调用成功后的后续动作仍是人工通过/拒绝，以及必要时补充官方核验来源。

## 7. 生成 release 与安装 LAND

审核完成后，可以在 Dashboard 发布面板执行四个独立 POST 操作，也可以使用下面的终端命令。每一步都要求审核人、理由和显式确认；`prepare` 不等于上传或安装：

```bash
./land-data --db .local/land-data.db publish prepare \
  --city new-york \
  --land-root ../LAND
```

然后在 Dashboard 或终端查看 release 差异：

```bash
./land-data publish diff --release <release-id>
```

必须检查新增、更新、删除、高风险字段和 blocker。Dashboard 的“生成 release”按钮只能调用 prepare，不能隐式 verify 或 install。

明确由审核人核验 release：

```bash
./land-data --db .local/land-data.db publish verify \
  --release <release-id> \
  --reviewer "mars" \
  --reason "已核对答案、适用范围、来源和差异"
```

只有 `VERIFIED` release 才能安装。Dashboard 默认禁用安装；启动时必须显式加入 `--allow-install`，并在页面输入审核人、理由、release 和确认操作：

```bash
./land-data --db .local/land-data.db dashboard \
  --allow-install \
  --land-root ../LAND
```

Dashboard 安装按钮实际调用的 release 安装逻辑等价于：

```bash
./land-data publish install \
  --release <release-id> \
  --land-root ../LAND
```

安装会备份当前 QAU、原子替换、运行 Swift/Xcode 检查；失败自动恢复。Dashboard 不直接写 LAND App 资源，也不绕过构建检查。POI、Place、Guide、Event 仍需各自的发布映射和契约，不能因为 QAU release 成功就宣称所有采集数据已进入 App。

如需回滚：

```bash
./land-data publish rollback \
  --release <release-id> \
  --land-root ../LAND \
  --reason "内容验收失败"
```

### 7.1 地点内容（Place）

Dashboard 第 4 步可以在“知识内容（QAU）”和“地点内容（Place）”之间切换。Place 发布只接受已经人工审核、坐标和来源完整的 `restaurant`、`cafe`、`museum`；Event 和官方 Guide 仍停留在 Dashboard 审核层，不会被误装进 LAND。

Place prepare 需要两类人工输入：明确的 POI ID，以及双语 publication overlay。新手可以直接把 JSON 粘贴到 Dashboard 的文本框，或填写项目内的 overlay 文件路径；“生成填写模板”只生成空字段，不会从帖子或模型自动生成公开文案。

在输入 POI ID 后，Place 模式提供“0. 只读预检”。它可以不填写审核人、原因和 overlay，逐条显示 POI 的 blocker（包括缺失人工 `ReviewDecision`、城市不匹配、来源或字段不完整）；有 overlay 时也会检查中英文文案。预检结果会明确显示 `read_only=true` 和 `release_dir_created=false`，不会生成 release、写数据库审核记录，也不会启用安装按钮。知识内容模式下该按钮保持禁用。

Dashboard 页面会在第 4 步显示当前绑定的 SQLite 数据库绝对路径；审核、预检和采集进度都读取这个数据库。切换数据库需要用新的 `--db` 参数重新启动 Dashboard。

终端也可以使用同一只读预检：

```bash
./land-data --db .local/land-data.db place-publish preflight \
  --city new-york --poi-id <poi-id>
```

```bash
./land-data --db .local/land-data.db place-publish prepare \
  --city new-york --poi-id <poi-id> \
  --overlay overlays/new-york-places.json
./land-data --db .local/land-data.db place-publish diff --release <release-id>
./land-data --db .local/land-data.db place-publish verify \
  --release <release-id> --reviewer "mars" --reason "已核对地点、双语文案和来源"
./land-data --db .local/land-data.db place-publish install \
  --release <release-id> --land-root ../LAND
./land-data --db .local/land-data.db place-publish rollback \
  --release <release-id> --land-root ../LAND --reason "内容验收失败"
```

Place 的安装和回滚同样要求 Dashboard 启动时显式加入 `--allow-install`。Dashboard 会把 `content_type` 写入审计记录；刷新页面后安装按钮仍保持关闭，必须重新验证当前 release。Place bridge 会冻结 POI、provenance 和审核决定，任何 blocker、输入漂移或 ContentPack hash 不一致都会阻断验证。

## 8. LLM 用量边界

Dashboard 默认零 LLM：打开页面、刷新列表、查看详情、执行普通审核、准备/查看/验证 release 都不会调用千问。规则处理、结构化来源采集、去重和状态统计也默认不调用模型。

只有人工明确选出候选，并同时提供模型与调用上限时，才允许调用千问。例如：

```bash
./land-data --db .local/land-data.db xhs knowledge extract \
  --id <candidate-id-1> \
  --id <candidate-id-2> \
  --llm auto \
  --limit 2
```

模型来自本机 `LAND_LLM_MODEL`（示例为 `qwen3.7-flash`），当前工具关闭 thinking，没有额外推理强度档位；`--limit` 是硬上限。Dashboard 不提供“自动抽取全部候选”按钮，也不会因为列表中存在 `PENDING_LLM` 就自行调用模型。实际 API 返回的 usage 才会记录 token；没有 usage 时不猜成本。

## 9. 不调用模型的内容

Dashboard 只展示处理结果，不自动扩大 LLM 范围。以下内容应能在页面看到，即使没有人工审核按钮：

- 规则已自动批准的结构化 POI；
- `AUTO_APPROVED` Mention；
- 来源重复和 `NOT_NEEDED` 运行；
- OSM、官方开放数据和活动来源的采集结果；
- 被规则跳过的无地点帖子及跳过原因；
- LLM 缓存命中、零调用处理结果；
- 失败来源、checkpoint 和 `RETRY_REQUIRED`；
- 已发布、已撤回、已过期和被拒绝的对象。

页面必须提供只读历史和导出摘要，避免审核者只能看到待办项目而无法解释“为什么没有进入待审队列”。

## 10. 关闭服务

前台启动时使用 `Ctrl-C` 关闭。关闭前应等待当前写操作完成，再确认终端显示服务已停止。后台启动时使用当前实现输出的 PID/服务命令，不要直接删除 SQLite 或锁文件。

关闭后检查端口已释放：

```bash
lsof -nP -iTCP:<port> -sTCP:LISTEN
```

退出 Dashboard 不会删除采集数据、审核历史或 release。需要清理数据时，应使用单独的保留期/数据库维护命令，并先备份。

## 11. 验收清单

实现合入前，至少手工验收以下场景：

1. 默认启动只监听 `127.0.0.1`；
2. 局域网设备可访问显式绑定的地址，未授权写请求被拒绝；
3. 页面同时显示待审、自动批准、拒绝、重复、失败和已发布对象；
4. Candidate 可查看原因、来源、可能 POI 和历史；
5. Mention 可绑定 POI 后批准，未绑定时按钮失败且原因可读；
6. Candidate 可安全 resolve/promote/reject；
7. 个人经验可匿名接受，但原始小红书字段不会出现在 release；
8. 动态规则没有官方核验字段时无法接受为可发布规则；
9. 仅选择的候选才触发千问，打开页面和浏览列表的 LLM 调用数为 0；
10. `publish prepare` 只读取已接受输入，未审核对象不能混入 release；
11. `publish verify` 缺审核人、理由、来源闭包、有效期或存在 blocker 时失败；
12. `publish install` 的 Swift/Xcode 检查失败会自动回滚；
13. 关闭服务后端口释放，SQLite 与审核历史保持完整；
14. 同一审核操作刷新或重复提交不会产生两次决定；
15. 所有错误页和日志不泄露 Cookie、密码、Token、作者凭据或未授权原文。

## 12. 采集、增量更新与当前边界

在页面顶部依次操作：

1. 输入城市 ID、搜索名称、国家代码、操作者和原因，点击“创建/补全城市”；
2. 点击“查看采集计划”，确认小红书 Query Bank、已配置结构化来源及缺失范围；
3. 在“作业步骤”选择小红书采集、零模型规则处理、结构化来源、LAND Raw、QAU 发布准备，或仅选中候选的千问抽取；
4. 点击“启动所选作业”，在“作业、进度与数据位置”查看持久化状态、计数、错误、LLM 调用上限和具体路径；
5. 采集结束后到审核列表处理变化对象，再执行 release 的 prepare/diff/verify/install。

新城市另有一个主页门槛。Dashboard 读取 `LAND/Resources/city-home-registry.json`；缺失时显示“确定让 Codex 生成一个吗？”。只有用户点击确认才会启动固定的本地 Codex 作业：`gpt-5.6-sol`、`high`、最多 1 次、Qwen 0 次、`workspace-write`。作业生成当地文字大字和一个本地文化动态元素，并强制 Reduce Motion、VoiceOver、无远程素材、Swift 测试和 iOS Simulator 构建。生成的 `scaffold` 会显示 `PREVIEW_NEEDS_ACCEPTANCE`，`ready` 显示 `PRESENT`；CLI 不可用时保留 `TASK_READY` 和可追踪任务文件，不会误报完成。

小红书采集发现浏览器未运行或登录失效时，会打开登录窗口并显示 `AUTH_REQUIRED`；完成登录后重新启动同一作业。终端等价的城市建档命令为：

```bash
./land-data --db .local/land-data.db xhs collect \
  --city san-francisco \
  --city-name "San Francisco" \
  --country US
```

工具会创建城市配置和默认 Query Bank；之后可在 `config/xiaohongshu-queries-san-francisco.json` 中查看计划。小红书首次登录：

```bash
./land-data xhs login
```

零模型运行小红书：

```bash
./land-data --db .local/land-data.db xhs collect --city san-francisco
./land-data --db .local/land-data.db xhs process --city san-francisco --llm never
```

结构化来源和增量运行：

```bash
./land-data --db .local/land-data.db auto-collect --city san-francisco
./land-data --db .local/land-data.db multi-pipeline
```

Dashboard 的 `/api/workflow/jobs`、`/api/workflow/storage` 和 `/api/workflow/plan` 会显示这些运行的状态、计数、checkpoint 相关来源记录和存储路径。后续更新按同一城市、同一数据库重跑；来源适配器按稳定 ID 去重并从 checkpoint 续跑，成功后再由 Dashboard 重新审核变化对象并生成下一版 QAU release。

当前自动发布只覆盖 LAND QAU 知识内容。POI/Place、Event、官方 Guide/指导快照还没有完成到 LAND App 的发布映射、schema 契约和安装验收；它们可以在 Dashboard 查看和审核，但不能因为审核通过或 QAU release 成功就宣称已经进入 App。

## 13. 当前 CLI 对照

以下终端命令与 Dashboard 使用同一套服务，可作为脚本化或故障恢复入口。Dashboard 已覆盖城市创建、采集与处理作业、显式候选模型抽取、对象查看、普通审核、知识草稿审核、来源/活动/官方快照审核以及 QAU prepare/diff/verify/install（安装需 `--allow-install`）：

```bash
./land-data review list
./land-data review show <id>
./land-data review approve <id>
./land-data review reject <id> --reason "原因"
./land-data review resolve <candidate-id> --poi <poi-id>
./land-data review promote <candidate-id>
./land-data xhs knowledge candidates --city new-york
./land-data xhs knowledge drafts --status DRAFT_NEEDS_REVIEW
./land-data xhs knowledge approve <draft-id> \
  --reviewer mars \
  --reason "已核对" \
  --reference-url https://example.gov/policy \
  --reference-kind official \
  --reference-title "官方说明" \
  --verified-at 2026-09-26T00:00:00Z \
  --valid-until 2026-10-26T00:00:00Z
```

Dashboard 与 CLI 应共享同一 ReviewService、knowledge review 和 publish 函数，不能各自实现一套状态转换规则。
