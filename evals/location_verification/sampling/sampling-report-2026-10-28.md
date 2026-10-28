# Location Verification 未标注候选筛查抽样

本报告只公开计数、分布、筛选规则和哈希。原始地点字段、来源文本、稳定 ID 和证据摘要只保存在冻结目录下的 `.local/.../screening/`。该队列不是 500 条 Gold，也没有标记 dev/test split，不保证困难案件配额。公共导入里的 `AUTO_APPROVED` 只是既有流水线状态，不等于独立 Gold。

- 生成时间：`2026-10-27T17:06:43.723915+00:00`
- 固定种子：`20261028`
- 队列总量：`1000`
- 队列 SHA-256：`9de97eeae5d4ea9badb5677e727faa6d4659887c913e8b394fdca66f112b1e86`
- 脚本 SHA-256：`e4173a1db422caf5ad7146f48cb1727126f3e81ea46232f835236c7a5e65f4db`
- 冻结清单 SHA-256：`922931df7d3a57bda867131c7b8890dc7442067c7b29b1a9c0d9f49e4a4230f2`

## 抽样分布

```json
{
  "city": {
    "Hanoi": 200,
    "New York": 800
  },
  "platform": {
    "nyc-open-data-restaurants": 790,
    "osm": 210
  },
  "category_top20": {
    "restaurant": 825,
    "hotel": 39,
    "cafe": 35,
    "place_of_worship": 25,
    "bar": 15,
    "theatre": 13,
    "museum": 11,
    "attraction": 10,
    "hostel": 8,
    "guest_house": 7,
    "marketplace": 7,
    "gallery": 5
  },
  "evidence_count_distribution": {
    "1": 8,
    "2": 992
  },
  "evidence_type_counts": {
    "GOVERNMENT_COORDINATES": 782,
    "GOVERNMENT_RECORD": 790,
    "OSM_COORDINATES": 210,
    "OSM_OBJECT": 210
  },
  "rows_with_coordinates": 992,
  "collected_at_min": "2026-09-23T01:03:37.308983Z",
  "collected_at_max": "2026-10-18T15:17:04.402639Z"
}
```

## 来源池和去重

| 城市 | 快照 | 输入候选行 | 排除 mock | 排除 XHS/历史核验 | 排除缺少来源 | 去重后实体数 | 去重后来源数 | 抽取数 |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| New York | `nyc-incremental-acceptance-v2.db` | 12704 | 0 | 0 | 0 | 12485 | 12704 | 800 |
| Hanoi | `land-data.db` | 300 | 0 | 31 | 0 | 231 | 269 | 200 |

## 固定规则

1. 只读取 pre-agent freeze 中的 `land-data.db`（Hanoi）和 `nyc-incremental-acceptance-v2.db`（New York），按 manifest SHA-256 校验；不读取 live DB。
2. 固定种子 `20261028`，目标配额为 New York 800、Hanoi 200，共 1000 条；不创建或写入 split 标签。
3. 排除 mock 平台、整个 `xiaohongshu-browser` 平台、`source_kind=xhs-verification` 及历史 XHS 核验导入，另外排除缺少稳定公开 source ID 的行。
4. 只用公开来源 ID（platform + external_id）和规范化名称/地址/城市/类别去重；不同地址的分店保留为不同实体。抽样不读取或使用历史 POI 匹配、review 状态或坐标验证元数据。
5. 每个可用来源平台最多先保留 10 条作为覆盖 guard，再按哈希排序填满城市配额；这不是困难案件或 Gold 配额。
6. 队列不携带历史 review_status、review_reasons、resolved/matched POI、坐标验证元数据、Gold 或标注信息；来源文本和证据摘要仅在 `.local` 私有队列中。

公共导入中的 `AUTO_APPROVED` 只是既有流水线状态，不等于独立 Gold。队列仍需逐案资格审查、独立标注和外部证据，不能直接形成锁定集。
