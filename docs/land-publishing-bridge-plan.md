# LAND 数据采集、审核、发布与更新闭环计划

> 2026-11-03 状态校正：QAU 与 Place bundled 发布桥均已有实现。本文同时保留后续目标；公开代码与本地新增功能的区别以[实现状态](implementation-status.md)为准，构建检查不等于 App 实机验收。

## 目标

最终形成一条可重复运行的本地生产线：

```text
新城市建档
→ 多来源采集
→ 零模型规则分流
→ 有预算的 LLM 抽取
→ 人工审核与事实核验
→ 发布编译
→ LAND App 读取并运行
→ 定期增量更新、撤回与回滚
```

采集成功只代表取得了输入。只有通过事实验证、审核、授权和发布编译的数据，才能进入 LAND App。

## 已经具备

- 小红书本人登录后的有界搜索、可见正文采集、checkpoint、去重和更新识别。
- 新城市第一次采集时，按城市名称与 ISO 国家代码执行一次结构化地理查询，自动创建城市配置和默认 Query Bank。
- NYC Open Data、OSM、NYC Parks Events 等结构化来源的独立运行与来源级记录。
- Raw、SourceContent、Candidate、Mention、Signal、Event、POI、provenance 和审核状态。
- 确定性规则优先，千问只处理 `PENDING_LLM`，支持数量预算、缓存、usage 和失败恢复。
- Candidate、Mention、POI 的终端审核命令。
- 覆盖 LAND 15 个旅行场景的 45 个社区 Query；非 POI 的文化、礼仪和实用信息进入 `xhs_knowledge_candidates`，一篇帖子可以同时进入知识审核与地点解析。
- 独立知识草稿链：只有明确选中的候选才可限量调用千问；证据必须来自可见原文；支持 content gap、失败冷却、草稿缓存和追加式人工审核。人工批准只到 `EDITORIAL_ACCEPTED_FOR_BRIDGE / NOT_PUBLISHED`。
- 只导出 `map_visibility=true` 的 POI 和符合来源策略的已批准 Mention。
- LAND App 已有 bundled demo repository 与只读 HTTP repository；已有 DTO 和 OpenAPI 雏形。
- 本地 QAU 发布桥接器：冻结已接受草稿、合并完整 LAND 索引、生成 manifest/diff、独立人工 verify、原子安装、默认 Swift 测试、可选 Xcode 构建和失败回滚。
- 小红书策略继续禁止发布原帖、链接和作者身份；人工接受的个人经验可被改写为匿名 `editorial_experience`，并且不能支持规则。只有已被独立官方/运营方来源核验、并带有效期的规则块才能作为规则进入 QAU release。

## 建设状态

### 1. 城市来源档案

每个城市需要一份 `CitySourceProfile`，包括：

- 城市 ID、双语名称、国家、时区、中心点和允许范围；
- 小红书 Query Bank；
- OSM 采集范围与分片；
- 官方开放数据目录和允许使用的字段；
- 来源访问条件、许可、署名和更新频率；
- 每类数据的 freshness、撤回和保留策略。

自动建档只创建城市中心和 Query Bank。它不能自动证明某个开放数据集适合该城市，也不能用城市中心替代 POI 坐标。

### 2. 多城市结构化采集

当前官方来源主要针对纽约，OSM 仍是小范围 bbox。需要：

- 把 OSM 扩展为有界网格或行政区分片，带 checkpoint、限流、去重和失败恢复；
- 将官方数据集发现从 NYC-only 改成 provider registry；
- 为每个城市启用经过审核的官方 Adapter；
- 记录删除、停业、迁址和上游撤回；
- 为每个来源设置不同更新周期。

### 3. 处理与审核闭环

处理顺序固定为：

1. 结构化字段与已知 POI 精确匹配；
2. 本地规则跳过无地点内容；
3. 只将无法结构化的内容放入 `PENDING_LLM`；
4. LLM 只提取候选和短证据；
5. Entity Resolution 解析分店、父场所和类别；
6. OSM 或官方来源验证坐标与身份；
7. 人工处理歧义、冲突、活动和推荐语义；
8. 再计算审核状态与 `map_visibility`。

还需要补充：

- 按城市和批次冻结审核队列；
- 审核前后 diff；
- 批量批准只允许同一种、同证据条件的项目；
- LLM Prompt/Schema 版本变化后的重处理策略；
- 模型调用的城市级、批次级和月度预算；
- 双语翻译与独立人工审校；
- QAU 已具备稳定身份、逐答案块 support binding、官方 reference、placement 和 product review；Guide 与多语言尚需独立映射和审校；
- 撤回和删除传播到 Candidate、Mention、POI、导出和 App。

### 4. 发布桥接器（QAU 与 Place 均已有实现）

已新增独立 `publish` 层。采集器不会因为采集成功就修改 App 资源；公开版 QAU 桥接器读取 SQLite 中状态为 `EDITORIAL_ACCEPTED_FOR_BRIDGE` 的知识草稿；本地另支持带审核回执与哈希校验的 `--reviewed-overlay`。Place 使用独立的 POI 和 publication overlay 契约。

实际命令：

```bash
./land-data publish prepare --city new-york
./land-data publish diff --release <release-id>
./land-data publish verify --release <release-id> --reviewer "mars" --reason "已核对差异与来源"
./land-data publish install --release <release-id> --land-root ../LAND
./land-data publish rollback --release <release-id> --land-root ../LAND --reason "产品验收回滚"
```

`prepare` 已完成：

- 冻结精确输入 ID、hash、审核决策和 provenance；
- 拒绝 content gap、无直接答案、未支持分支、未映射场景/适用范围、缺少官方核验或超出来源发布策略的内容；
- 公开版将 culture、food、money、stay 和 sightseeing 知识映射到 LAND QAU topic/intent；本地已扩展到入境、到达、交通、通讯、安全、健康等场景，并对河内合并后的索引实施逐主题覆盖门槛；
- 生成 `zh-Hans` QAU；当前不伪造未经人工审校的英文翻译；
- 生成稳定 ID、来源信息、更新时间和验证时间；
- 下一次 prepare 对当前 LAND base 中已有且完全一致的草稿计为 `already_present`；同 ID 内容冲突会阻止发布；
- 生成不可变 release manifest 与内容 hash；
- 保留上一版，不覆盖历史 release。

`verify` 已完成：

- JSON 结构、文件 hash 和 base index hash；
- ID 唯一性与跨引用检查；
- 当前 SQLite 输入与审核决策没有在 prepare 后改变；
- 每个决定性答案块都有闭合的 support binding；规则只接受 official/operator 并有 `verifiedAt/validUntil`；
- 与上一版的新增、更新、删除和高风险字段 diff；
- 空 release、有 blocker 的 release 以及没有独立审核人/理由的 release 不能进入 `VERIFIED`。

`install` 只安装已经明确审核的 `VERIFIED` release。它核对 LAND 当前资源与 release base 一致，备份原字节，原子替换 bundled QAU，然后默认运行 Swift 测试。需要额外运行 Xcode iOS Simulator 构建时，使用 `publish install --run-app-checks`。启用的检查失败时自动恢复上一版；`rollback` 可显式恢复已成功安装前的精确字节，并再次执行默认 Swift 验证。

POI → Place 桥已经提供 preflight/prepare/diff/verify/install/rollback，见 [Place 发布说明](place-publishing.md)。Event、官方指引的通用发布映射、多语言独立审校、完整撤回传播和 HTTP release 仍需建设。本地新增 `xhs knowledge retract` 及对应 QAU 内容移除，不等于全数据层的删除传播。

### 5. LAND App 接入（bundled QAU 与 Place）

QAU 安装目标为 `LAND/Resources/question-content-index-v1.json`；Place 安装目标为 `LAND/Resources/published-content.json`，不改写 demo 包。Place 发布说明约定 LAND 以 `LANDDataMode=published` 读取发布内容；相邻 App 的实际加载、收藏保留和展示需要在对应 App 版本中验收。

公开版 Place 桥将 restaurant/cafe 映射为 food、museum 映射为 sight。本地桥已扩展餐厅、酒吧、住宿、博物馆等类别；此处不再用旧 food/sight 映射概括所有版本。未映射类别继续阻断。已发布内容的稳定 ID、来源展示和更新后的 App 行为仍是端到端验收项。

第二阶段再接 HTTP：

- 实现现有 OpenAPI 的只读服务或静态 API/CDN；
- LAND 使用已有 `HTTPContentRepository`；
- release manifest 带版本、hash、发布时间和撤回列表；
- App 缓存最近一次有效 release；
- 网络或新 release 校验失败时继续使用旧版本；
- 支持服务端回滚，不要求每次数据更新都重新发布 App。

## 用户以后如何更新

### 本地 App 数据包模式

适合个人测试和早期验收：

```text
Dashboard 或现有采集命令 → review → publish/place-publish prepare → verify → install → 构建 App
```

每次数据更新需要重新构建或安装 App 内容包。

### HTTP 发布模式

适合已经部署的 App：

```text
目标流程：采集更新 → review → prepare → verify → 上传 release → 切换 current
```

App 下次刷新读取新 release，不需要因数据变化重新提交客户端版本。

以下是尚未实现的统一命令设计，不是当前可直接运行的 CLI：

```bash
./land-data city update --city new-york --llm never
```

目标是依次运行已启用来源、保存 checkpoint、做零模型处理并输出待审数量。该目标命令中，只有显式增加 `--llm auto --llm-limit N` 才调用千问。当前应使用已有 Dashboard 工作流、`multi-pipeline` 和 `xhs` 子命令。更新入口绝不自动执行 publish；审核完成后由独立发布命令推进 release。

## 更新频率

频率应按来源而不是整座城市统一：

- 官方营业状态、活动：按来源更新周期；
- OSM POI：低频增量重扫；
- 社区体验：本人需要时手动运行；
- 已发布 POI：定期做 freshness 检查；
- 发生撤回、停业或坐标冲突：进入高优先级审核并从下一 release 移除。

工具需保存 `last_checked_at`、`last_changed_at`、`verified_at`、`expires_at` 和 `withdrawn_at`，才能区分“没有变化”“没有检查”和“已经失效”。

## 发布门槛

一个 release 必须同时满足：

- 所有输入和处理版本可追溯；
- 没有未解析或待审对象混入；
- POI 坐标已验证且 `map_visibility=true`；
- 来源允许相应字段公开；
- 满足相应版本及内容类型的语言要求，已提供文案通过审核；
- 完成对应发布桥默认检查；启用应用检查时还需通过相应测试／构建，实机使用另行验收；
- 更新 diff 已审阅；
- 可以一条命令回滚到上一 release。

## 推荐实施顺序

1. **城市自动建档**：已有实现，后续完善来源适配与范围配置。
2. **QAU 发布契约与本地桥接器**：已有实现；本地扩展需要独立提交和对应验证。
3. **Place 后续验收与 Event/Guide 发布契约**：Place 桥已有实现，继续验证 App 使用、撤回与更新；Event/Guide 通用生成仍需单独设计。
4. **更新与撤回传播**：增量 diff、tombstone、freshness 和跨对象失效。QAU 安装回滚已具备。
5. **多城市 OSM 与官方来源**：从第二个真实城市验证通用性。
6. **统一 `city update` 入口**：采集、处理和状态汇总。
7. **HTTP 发布服务**：需要无需重新发 App 的内容更新时再建设。

下一条端到端验收应选择一个城市、少量已验证 POI，并且全程不使用社区原文公开导出。证明：采集 → 审核 → Place bundled release → LAND 真机运行 → 相同输入零变化 → 一个 POI 更新 → 一个 POI 撤回 → 回滚，随后再扩大城市和来源。QAU 链的 prepare/diff/verify/install/rollback 已有自动化验收。
