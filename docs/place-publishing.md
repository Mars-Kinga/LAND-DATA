# POI → LAND Place 发布桥

> 2026-11-03 核对：发布桥已在公开代码中实现。类别、语言和补充字段在本地工作区有未提交扩展，下面分别说明，详见[版本边界](implementation-status.md)。

这个桥只接受 Dashboard/终端明确选择的 POI。它不从小红书正文、推荐摘要、模型输出或卫生等级生成公开 `reason`。

对于 `official-directory`、`nyc-open-data-restaurants` 这两个明确允许的官方/公共结构化来源，如果 POI 同时满足活动状态、地图可见、坐标已核验、结构化字段校验通过、`AUTO_APPROVED`、允许类别、完整 provenance、稳定 HTTPS 来源且没有冲突或待审原因，`prepare` 可以不填 overlay，也不要求额外的 `ReviewDecision`。该路径只冻结证据并生成保守的事实型 fallback 文案，不会修改源数据库。

OSM、小红书、Threads、LLM 结果、来源冲突或缺失字段仍必须经过 Codex 或人工审核，并提供审核后的 publication overlay。

## Overlay

```json
{
  "city": "new-york",
  "destination": {
    "zh-Hans": {"name": "纽约", "localName": "纽约", "summary": "纽约已审核地点"},
    "en": {"name": "New York", "localName": "New York", "summary": "Reviewed places in New York"}
  },
  "places": {
    "<poi-id>": {
      "zh-Hans": {"name": "中文名", "localName": "English name", "address": "地址", "reason": "人工文案", "caution": "人工提示"},
      "en": {"name": "English name", "localName": "English name", "address": "Address", "reason": "Editorial copy", "caution": "Editorial caution"}
    }
  }
}
```

每个提供的 Place 语言对象必须有非空 `name`、`localName`、`address`、`reason`、`caution`；destination 需要 `name`、`localName`、`summary`。

公开代码基线 `50d3438` 映射为 `restaurant/cafe → food`、`museum → sight`，其他类别阻断。本地工作区的 `PLACE_CATEGORY_MAP` 已保留 restaurant、cafe、bar、marketplace、museum、attraction、gallery、theatre、hotel、hostel，并映射 `pub → bar`、`arts_centre → artsCentre`、`place_of_worship → placeOfWorship`、`historic/historic_site → historicSite`、`guest_house → guestHouse`；food/sight 仍兼容。未在映射表中的类别继续阻断。

公开版人工 overlay 校验要求 `en` 与 `zh-Hans` 两份内容。本地工作区的 `REQUIRED_LANGUAGES` 为 `zh-Hans`：destination 和每个 Place 可以只提供中文；如另提供 `en` 对象，其必填字段仍须完整。上面的双语示例两版均适用。自动 fallback 会把同一来源语言的名称、地址和保守模板复制到两种 locale，并在 release `manifest.json` 的 `overlay_mode=deterministic_fallback`、`overlay_metadata.fallback=true` 和 `fallback_locale` 中明确标记；这不是人工翻译。自动 fallback 在两版中仍生成两种 locale；它与人工 overlay 的语言要求不同。

## 终端流程

先使用只读预检查看明确选择的 POI 是否具备发布条件。它不会创建发布目录、写入内容包或修改数据库；`--overlay` 可以省略，用于先看到缺失的人工材料：

```bash
./land-data --db .local/land-data.db place-publish preflight \
  --city new-york --poi-id <poi-id>
```

如果使用城市显示名，只有 `config/cities.json` 中明确配置的 `display_name` 或 `aliases` 会映射到稳定城市 ID；系统不会根据相似拼写猜测城市。预检结果按 POI 返回 `READY`/`BLOCKED`、阻断原因和总数。

预检会返回 `approval_mode`、`overlay_mode`、自动策略版本和逐 POI 的门槛证据。符合官方结构化策略的 POI 会显示 `READY / deterministic_official / deterministic_fallback`；其他来源会显示 blocker。预检不会写审核记录、生成 release 目录或调用 LLM。

```bash
./land-data place-publish prepare \
  --city new-york --poi-id <poi-id>

# 如果希望覆盖自动 fallback，可以提供人工 overlay（公开版双语，本地可仅中文）
./land-data place-publish prepare \
  --city new-york --poi-id <poi-id> --overlay overlay.json

./land-data place-publish diff --release <release-id>

./land-data place-publish verify --release <release-id> \
  --reviewer mars --reason "逐条核对 POI、来源、发布文案"

# deterministic_official release 的机械 verify 可以省略人工参数
./land-data place-publish verify --release <release-id>

./land-data place-publish install --release <release-id> --land-root ../LAND
./land-data place-publish rollback --release <release-id> \
  --land-root ../LAND --reason "恢复上一个本地发布包"
```

`prepare` 会冻结 POI、全部 POI provenance、可用的审核决定、overlay、自动策略版本、门槛证据和输入 hash。`verify` 会重新读取这些输入，任何 POI 状态、来源、审核决定、ContentPack 或 manifest 篡改都会阻断。自动路径不会写入缺失的 `ReviewDecision`。安装写入 LAND 独立的 `LAND/Resources/published-content.json`，不会改写 demo 内容；安装前执行 `tools/validate_content.py --publish`。Swift/Xcode 检查可用 `--run-app-checks` 显式开启。

新 release 会基于准备时已经安装的 `published-content.json` 做确定性增量合并：保留其他城市、既有 Guide 和本次未选择的 Place，只替换本次明确选择且 ID 相同的 Place。已有城市主页文案会保留，并扩展新增地点的来源闭包。基础内容包在 `prepare` 后发生变化会触发 drift，必须重新准备 release；删除、停业和撤回仍需使用单独的显式撤回流程，增量合并不会把“本次未选择”解释成删除。

LAND 只有在构建设置/Info.plist 明确设置 `LANDDataMode=published` 时才读取这个 bundled repository；默认 `demo` 模式保持原行为。


## 本地新增的地点补充字段

本地工作区增加 phone、rating、reviewCount，以及 cuisine、highlights、recommendedFor 等字段。电话、评分与评价数只能来自明确来源字段；社区经验字段需要活动来源和已审核的 XHS → POI Mention 关联，不直接把帖子意见当作已核验事实，也不使用模型输出生成公开 reason。帖子中的推荐项、优缺点和避雷信号仍属于内部经验处理，不能据此声称 ContentPack 已有同名字段。该扩展尚未随本次文档同步发布源码。

## 与 Harness 的关系

`place-publish` 沿用现有 POI 状态和来源检查，不自动运行 Location Verification Agent。显式冲突 hold 可关闭符合条件 POI 的地图可见性；单独生成 Harness 交接包不会改变业务审核状态，已安装的旧内容包也不会自动撤回。
