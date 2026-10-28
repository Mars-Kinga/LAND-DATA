# SQLite 地点数据只读性能基线

本报告只测 LAND-DATA 的 SQLite 读取/反序列化路径，不代表 Location Verification Agent 端到端表现；没有网络、LLM、动态工具选择或人工审核。连接使用 `mode=ro` 和只读事务。

- 生成时间：`2026-10-27T16:51:13.423295+00:00`
- 运行参数：warmups=3，iterations=20，page limit=1000

## `land-data.db`

- 主库 SHA-256：`12e291a29f88be90f9d71176cd089c992cd7fffddc8cc284f404c66632fb820a`
- 只读运行前后文件指纹一致：`True`
- SQLite 快照：`{"data_version": 1, "freelist_count": 0, "journal_mode": "delete", "page_count": 1645, "page_size": 4096, "schema_version": 63}`
- 候选总量：`304`；有 source_content：`304`；有 location_evidence：`273`；证据行：`543`
- 至少 500 条原始候选可抽样：`False`；至少 500 条同时具备来源和证据：`False`；至少 500 条有两类证据：`False`；至少 500 条跨两个来源平台：`False`
- 独立金标准状态：no dedicated gold/label/eval table detected; existing review_status is not an independent gold label

| 查询路径 | cases | queries | p50 ms | p95 ms | mean rows/s | digest mismatch |
|---|---:|---:|---:|---:|---:|---:|
| `candidate_storage_scan_and_pending_filter` | 1 | 20 | 8.505 | 11.620 | 33822.138 | 0 |
| `candidate_city_page` | 2 | 40 | 2.209 | 6.203 | 55869.795 | 0 |
| `poi_city_page` | 2 | 40 | 1.679 | 6.283 | 45593.552 | 0 |
| `candidate_evidence_source_lookup` | 12 | 240 | 0.244 | 0.545 | 6710.659 | 0 |
| `evidence_rollup_by_city` | 1 | 20 | 2.045 | 7.057 | 524.198 | 0 |
| `review_history_lookup` | 8 | 160 | 0.025 | 0.097 | 28425.529 | 0 |

### 来源与时间范围

```json
[
  {
    "city": "Hanoi",
    "platform": "osm",
    "candidates": 269,
    "distinct_external_ids": 269,
    "candidates_with_evidence": 269,
    "source_collected_min": "2026-09-28T11:23:54.477295Z",
    "source_collected_max": "2026-09-29T17:57:20.622524Z"
  },
  {
    "city": "Hanoi",
    "platform": "xiaohongshu-browser",
    "candidates": 31,
    "distinct_external_ids": 14,
    "candidates_with_evidence": 0,
    "source_collected_min": "2026-09-28T11:37:03.973136Z",
    "source_collected_max": "2026-09-28T11:42:42.453475Z"
  },
  {
    "city": "mock-tokyo",
    "platform": "mock-social",
    "candidates": 3,
    "distinct_external_ids": 3,
    "candidates_with_evidence": 3,
    "source_collected_min": "2026-09-21T05:40:45.537499Z",
    "source_collected_max": "2026-09-21T05:40:45.540413Z"
  },
  {
    "city": "mock-tokyo",
    "platform": "mock-places",
    "candidates": 1,
    "distinct_external_ids": 1,
    "candidates_with_evidence": 1,
    "source_collected_min": "2026-09-21T05:40:31.011194Z",
    "source_collected_max": "2026-09-21T05:40:31.011194Z"
  }
]
```

## `nyc-incremental-acceptance-v2.db`

- 主库 SHA-256：`86cebe5886ca0f81e2239417b65a5b8632b21741d35fc9655634668a21343b1c`
- 只读运行前后文件指纹一致：`True`
- SQLite 快照：`{"data_version": 1, "freelist_count": 2635, "journal_mode": "delete", "page_count": 51805, "page_size": 4096, "schema_version": 62}`
- 候选总量：`12958`；有 source_content：`12958`；有 location_evidence：`12958`；证据行：`25832`
- 至少 500 条原始候选可抽样：`True`；至少 500 条同时具备来源和证据：`True`；至少 500 条有两类证据：`True`；至少 500 条跨两个来源平台：`False`
- 独立金标准状态：no dedicated gold/label/eval table detected; existing review_status is not an independent gold label

| 查询路径 | cases | queries | p50 ms | p95 ms | mean rows/s | digest mismatch |
|---|---:|---:|---:|---:|---:|---:|
| `candidate_storage_scan_and_pending_filter` | 1 | 20 | 323.665 | 662.403 | 32360.226 | 0 |
| `candidate_city_page` | 2 | 40 | 35.672 | 104.831 | 14026.443 | 0 |
| `poi_city_page` | 2 | 40 | 12.930 | 30.517 | 42513.975 | 0 |
| `candidate_evidence_source_lookup` | 12 | 240 | 54.934 | 89.004 | 33.614 | 0 |
| `evidence_rollup_by_city` | 1 | 20 | 72.584 | 113.149 | 25.070 | 0 |

### 来源与时间范围

```json
[
  {
    "city": "New York",
    "platform": "nyc-open-data-restaurants",
    "candidates": 12693,
    "distinct_external_ids": 12693,
    "candidates_with_evidence": 12693,
    "source_collected_min": "2026-09-22T20:31:10.144083Z",
    "source_collected_max": "2026-10-18T14:23:10.438146Z"
  },
  {
    "city": "Hanoi",
    "platform": "osm",
    "candidates": 254,
    "distinct_external_ids": 254,
    "candidates_with_evidence": 254,
    "source_collected_min": "2026-10-10T20:56:02.479311Z",
    "source_collected_max": "2026-10-21T02:58:01.956908Z"
  },
  {
    "city": "New York",
    "platform": "osm",
    "candidates": 11,
    "distinct_external_ids": 11,
    "candidates_with_evidence": 11,
    "source_collected_min": "2026-09-28T08:14:16.496880Z",
    "source_collected_max": "2026-10-18T15:17:04.402639Z"
  }
]
```

## 解释边界

数据库记录数、稳定候选/POI ID、source platform/external_id、来源时间和 evidence 行数可以用于构造后续 500+ 案例的抽样框。这里的‘两类证据/两个来源平台’只是结构性覆盖信号，不能自动证明来源独立或结论正确；已有 `review_status` 只表示历史流程状态，不能直接当作地点核验真值。正式改前/改后评测仍需冻结证据快照并完成独立标注。
