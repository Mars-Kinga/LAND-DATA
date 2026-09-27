# 小红书终端采集工具

本文说明如何用本人正常登录的小红书网页，从终端执行有界搜索、保存可见帖子正文、断点续跑，并按需处理地点候选。

## 能力边界

工具操作一个独立的 Chrome 资料目录，通过页面上已经渲染的可见 DOM 完成搜索和详情读取。它不会调用小红书私有接口，不会重放页面请求，不会读取或导出 Cookie、密码、localStorage、验证码，不会下载图片或视频，也不会绕过登录、验证码、访问频率限制。

目前小红书开放平台的 [`read_notes` scope](https://openaccount.xiaohongshu.com/docs/scope) 仍是规划中，[公开 API 目录](https://openaccount.xiaohongshu.com/docs/api-reference)也没有任意公开笔记的关键词搜索与全文接口。因此这里使用的是本人登录后的正常网页，而不是官方内容 API。页面结构改变后可能需要更新选择器。

小红书数据默认只用于本机内部候选和审核：`export_enabled=false`。采集成功不代表取得公开再发布许可。

## 模型策略

采集阶段始终为零 LLM：

- 搜索、滚动、打开详情和保存正文不用模型。
- 去重、内容更新识别、checkpoint 和审计不用模型。
- 已有 POI 的唯一精确名称和明确无地点内容走确定性规则；未来取得可靠的页面结构化地点标签时也走确定性规则。
- 无法用规则确定地点、分店、父场所或活动的旅行内容进入 `PENDING_LLM`。
- 文化、礼仪、入境、支付、安全、通信、健康等没有具体 POI 的内容进入独立 `KNOWLEDGE_REVIEW`，不会因为没有地点而被丢弃，也不会伪装成 POI。

默认 `--llm never`，不会调用千问。只有显式执行 `--llm auto`，才处理 `PENDING_LLM`：

```bash
./land-data --db .local/land-data.db xhs process \
  --city new-york \
  --llm auto \
  --limit 20
```

模型只做结构化抽取，不验证地点事实，不创建地图可见 POI，也不决定公开发布。真实地点验证继续依赖已有 POI、官方来源、OSM、Entity Resolution 和审核门。模型结果默认进入审核；缓存键包含正文、模型、Prompt 和 Schema 版本，同一输入重复处理不会再次请求。

模型网络或 Schema 失败会记录为 `ERROR`，不会伪装成已经完成的审核项。确认服务恢复后使用 `--retry-errors` 重试；`xhs run --llm auto` 会在本次 `--llm-limit` 预算内自动重试旧的 `ERROR`。每篇每次处理最多发起一次模型请求。

文化、礼仪和实用知识使用独立的显式流程，绝不会被 `xhs run --llm auto` 自动送入模型：

```bash
./land-data --db .local/land-data.db xhs knowledge candidates \
  --city new-york --scenario culture --limit 50

./land-data --db .local/land-data.db xhs knowledge extract \
  --id <candidate-id> --llm auto --limit 1

./land-data --db .local/land-data.db xhs knowledge drafts \
  --status DRAFT_NEEDS_REVIEW --limit 50
```

知识抽取同样读取 `LAND_LLM_MODEL`，关闭 thinking。它要求逐字原文证据，允许在证据不足时保留空答案和 content gap；产物只能是 `DRAFT_NEEDS_REVIEW`。失败有 5 分钟冷却，只有显式 `--retry-errors` 才立即重试。人工批准最多推进到 `EDITORIAL_ACCEPTED_FOR_BRIDGE / NOT_PUBLISHED`；涉及规定、价格、资格、安全步骤等动态事实时，批准命令强制提供 `--reference-url`、`--reference-kind official|operator`、`--reference-title`、`--verified-at` 和晚于核验时间的 `--valid-until`。

已接受草稿不会自动进入 App。本地 QAU 桥接器要求单独执行 `publish prepare → diff → verify → install`。小红书保持 `export_enabled=false`，所以原帖、链接和作者身份不会进入公开内容包；另行启用的 `allow_experience_derivation=true` 只允许把人工接受的个人经验改写为匿名 `editorial_experience`。它在 LAND 中明确标为旅行者经验，不能支持规则。规则块仍必须由独立官方/运营方来源验证。发布命令不调用 LLM。

## 第一次使用

在 LAND-DATA 根目录运行：

```bash
cd /Users/mars/Downloads/LAND-DATA

./land-data xhs login
```

命令会打开一个专用于 LAND-DATA 的 Chrome 窗口。请在窗口内正常扫码或完成平台要求的验证。登录状态由 Chrome 留在：

```text
.local/xiaohongshu/chrome-profile/
```

不要复制、上传或提交这个目录。工具不会读取其中的账号凭据。

使用独立目录也符合 Chrome 136 之后对 remote debugging 的要求：调试端口必须配合非默认 `--user-data-dir`，不能附着到日常 Chrome 默认资料目录。

连接前还会检查本机调试端口是否属于这个专用资料目录；如果端口被其他 Chrome 占用，工具会拒绝连接，不会控制日常浏览器。

登录后检查：

```bash
./land-data xhs status
```

## 单个 Query

```bash
./land-data --db .local/land-data.db xhs collect \
  --city new-york \
  --query "纽约 咖啡馆" \
  --posts 20 \
  --scrolls 3
```

采集完成后，帖子会写入 Raw 与 SourceContent；默认确定性处理，不调用模型。无法确定地点的帖子进入 `PENDING_LLM`。

## Query Bank

仓库提供一个可编辑的示例：

```text
config/xiaohongshu-queries.json
```

运行：

```bash
./land-data --db .local/land-data.db xhs collect \
  --query-file config/xiaohongshu-queries.json
```

文件格式支持按 LAND 旅行场景分组：

```json
{
  "city": "new-york",
  "coverage_version": 2,
  "query_groups": {
    "food": ["纽约 餐厅", "纽约 咖啡馆"],
    "culture": ["纽约 文化 礼仪", "纽约 当地 禁忌"],
    "safety": ["纽约 安全 防骗"]
  },
  "limits": {
    "posts_per_query": 30,
    "scrolls_per_query": 4,
    "pause_seconds": 1.5
  }
}
```

当前默认覆盖 entry、arrival、packing、money、food、transport、stay、culture、safety、connectivity、sightseeing、shopping、health、departure 和 special-needs，共 45 个 Query，理论候选上限约 1,350。使用 `--group culture` 可只采文化与礼仪；`--group` 可重复。不传时运行全部场景。

`--query` 可以重复；一旦给出 `--query`，它会覆盖 query file 中的查询词。单个 Query 的帖子数和滚动次数都有硬上限，避免无限滚动。

完成的 Query 在下一次命令中会重新执行一次有界搜索，以发现新帖子；相同内容计为重复，不新增 Raw。未完成的 Query 默认从已保存的帖子位置继续。只有需要放弃未完成的搜索快照并重新搜索时才使用 `--refresh`。

## 一条命令完成采集与按需抽取

零模型模式：

```bash
./land-data --db .local/land-data.db xhs run \
  --query-file config/xiaohongshu-queries.json \
  --llm never
```

只对待模型队列调用千问：

```bash
./land-data --db .local/land-data.db xhs run \
  --query-file config/xiaohongshu-queries.json \
  --llm auto \
  --llm-limit 20
```

千问配置只从本机 `.env` 或当前进程环境变量读取。不要把 API Key 放进命令行、query file、数据库或聊天消息。

## 登录失效和恢复

出现以下情况时，采集器会停止当前运行：

- 未登录或登录过期；
- 验证码或账号验证；
- 页面显示访问限制；
- 搜索或详情 DOM 无法可靠定位；
- 正文在两次读取之间仍然变化，或仍有展开/截断状态。

状态为 `AUTH_REQUIRED` 时，工具会打开专用 Chrome 窗口。你完成正常登录或验证后，重新执行原命令即可。已经成功保存的帖子不会重复写入；未完成 Query 从 checkpoint 继续。

采集器不改 IP、不切换账号、不高频重试，也不会把登录页、错误页或不完整正文保存成正常帖子。

## 状态和审计

查看浏览器、采集和处理状态：

```bash
./land-data --db .local/land-data.db xhs status
```

查看最近运行：

```bash
./land-data --db .local/land-data.db xhs runs --limit 20
```

每篇帖子记录：

- Query 与帖子 ID；
- 新增、更新或重复；
- 页面读取状态与失败原因；
- Raw ID 和内容 hash；
- 确定性路由原因；
- `PENDING_LLM`、`NEEDS_REVIEW`、`DONE`、`SKIPPED` 或 `ERROR`；
- LLM 调用数、缓存命中、模型耗时与 API 实际返回的 usage；
- 每次处理 attempt，历史为追加式记录。

短 capture 位于：

```text
.local/xiaohongshu/captures/
```

这里只保存处理所需的可见标题、正文、作者显示名、标签、时间原标签、媒体类型/数量和 Query provenance。不保存正文 HTML、图片 URL、评论、Cookie 或请求响应。

短 capture 与该来源 Raw 的默认保留期都是 7 天；后续采集会删除过期 capture 文件。帖子 ID、内容 hash、Query 关系、处理状态和追加式审计记录继续保留，以便去重和解释结果。当前 `SourceContent` 和已生成的候选/证据仍按 LAND-DATA 的现有长期数据层保留；跨层删除传播尚未完成，因此这台机器上的数据库本身仍应视为含社区内容的私有数据。

## 退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | 本次命令完成，或没有需要处理的新内容 |
| `1` | 配置、页面结构、持久化或其他运行错误 |
| `2` | 需要本人登录或完成平台验证 |

`xhs status` 在成功报告 `STOPPED` 或 `auth_required` 时仍返回 `0`；状态本身不是采集动作。`xhs login` 返回 `0` 只表示登录窗口已经打开，不表示登录已经完成。

正常业务结果的标准输出为 JSON，适合被其他本地脚本读取；参数或配置错误会以普通错误写到 stderr。正文和账号信息不会写到标准输出或运行日志。

## 与其他来源一起使用

小红书是社区体验来源，官方目录和 OSM 是事实与地点验证来源。两者使用同一个 SQLite 数据库：

```bash
./land-data --db .local/land-data.db auto-collect --city new-york
./land-data --db .local/land-data.db xhs run --llm never
```

建议先运行零 LLM 的官方来源，让更多 POI 名称进入数据库；随后处理小红书时，唯一精确命中的帖子可以跳过模型。

## 不建议直接无人值守定时运行

终端命令可以脚本化，但登录续期、验证码和页面改版仍需要本人处理。只有在实际连续验收证明账号和页面稳定后，再考虑低频定时。即使定时运行，遇到 `AUTH_REQUIRED` 也应停止并等待本人操作，而不是自动反复尝试。
