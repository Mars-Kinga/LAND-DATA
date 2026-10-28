# LAND-DATA 全量 NYC 输出跨来源诊断

`land_data_output_cross_source.py` 只读读取冻结的 LAND-DATA NYC SQLite 输出，并与已保存的 NYC DOT 当前/历史快照做全量字段级诊断。它回答三个可复现的问题：

1. 冻结数据库里 DOHMH `source_contents`、`poi_candidates` 和解析后的 `pois` 输出分别有多少，旧流程的 `AUTO_APPROVED` 与 `NEEDS_REVIEW` 如何分布；
2. 当前 NYC DOT 快照按唯一规范化名称＋完整地址检索时，覆盖多少候选、坐标可比较分母是多少，以及 `>10m`/`>50m` 坐标冲突落在旧流程的哪种状态；
3. 历史 NYC DOT 快照按 CAMIS 连接时，地址/坐标字段的一致性如何，以及重复申请和时间差带来的限制。

这不是地点核验准确率测试。当前快照没有与全部候选共享的独立人工标签，名称＋地址匹配是检索/覆盖条件；历史 DOT 快照是 2023 年数据，只能做历史字段一致性诊断，不能证明当前营业。它也不测动态 Agent，因此不能作为 Agent 前后提升数字。

## 输入和只读边界

默认输入：

```text
.local/baselines/location-verification-pre-agent-final-2026-10-28/nyc-incremental-acceptance-v2.db
.local/baselines/location-verification-pre-agent-final-2026-10-28/manifest.json
.local/location-verification-live-check/dot_batches/current.json
.local/location-verification-live-check/dot_batches/historical.json
.local/location-verification-live-check/dot_batches/capture-manifest.json
```

数据库在读取前会对照冻结清单校验字节数和 SHA-256，并使用 SQLite `mode=ro&immutable=1` 打开；随后执行 `PRAGMA quick_check`。查询只读以下实际输出链路：

```text
source_contents (platform=nyc-open-data-restaurants)
    -> poi_candidates (city=New York)
    -> pois (resolved_poi_id)
```

脚本会拒绝 DOHMH `source_contents` 与候选数量不一致的快照，也会拒绝 DOT 原始文件或抓取清单哈希变化。逐案结果没有写入数据库；它们只写到私有 `.local` 目录，公开文件只写聚合计数、分母、输入哈希和声明边界。

## 运行

在仓库根目录执行：

```sh
python3 evals/location_verification/land_data_output_cross_source/land_data_output_cross_source.py \
  --run-id 20261028T000000Z --force
```

输出：

```text
evals/location_verification/land_data_output_cross_source/
  land-data-output-cross-source-summary.json
  land-data-output-cross-source-summary.md
.local/location-verification-live-check/land_data_output_cross_source/
  land-data-output-cross-source-cases.jsonl
  land-data-output-cross-source-manifest.json
```

`--force` 只覆盖本次评测生成的四个输出文件；不会覆盖冻结数据库、DOT 快照或旧评测目录。私有目录为 `0700`，逐案与清单文件为 `0600`。

## 匹配和分母

当前 DOT 比较复用 `dot_cross_source` 的确定性规范化：Unicode NFKC、大小写、标点/空白、常见街道缩写和数字序数。只有候选名称和完整地址都非空，且规范化 `(name, address)` 在当前 DOT 行中恰好对应一行，才记为 `unique_name_address_match`；零行是 `no_unique_name_address_match`，多行是 `ambiguous_name_address_match`，不会静默选一行。坐标阈值只在唯一匹配且双方有合法坐标时进入分母。`>10m` 与 `>50m` 表示跨来源坐标字段冲突，并按冻结候选 `review_status`（`AUTO_APPROVED`/`NEEDS_REVIEW`）分组。

历史 DOT 比较以 `source_contents.external_id` 作为 CAMIS。一个 CAMIS 的所有历史行都会保留；只有重复行每一行都存在且值相同的字段才进入该字段分母，冲突/缺失会留在私有逐案结果中。历史连接覆盖率本身不是当前营业率。

## 2026-10-28 实际运行

当前公开汇总见 [land-data-output-cross-source-summary.md](land-data-output-cross-source-summary.md)。本次输入和脚本哈希写在同名 JSON、Markdown 和私有 manifest 中；下列数字是该次冻结运行的可复现输出：

- DOHMH `source_contents` 和 `poi_candidates` 各 `12,693` 条；候选状态为 `AUTO_APPROVED=12,609`、`NEEDS_REVIEW=84`。已解析候选 `12,609` 条，对应去重后的 `pois` 为 `12,580` 个。
- 当前 DOT 快照 `2,516` 行。唯一规范化名称＋地址匹配 `1,020/12,693`（`8.0359%`）；唯一匹配中坐标可比较 `1,004/1,020`，其中 `>10m` 为 `20/1,004`，`>50m` 为 `3/1,004`。
- 当前唯一匹配按旧状态分层：`AUTO_APPROVED` 为 `1,019/12,609`，`NEEDS_REVIEW` 为 `1/84`。`>10m` 与 `>50m` 的 20/3 个坐标冲突均来自 `AUTO_APPROVED`，但这只表示冲突在本快照中的状态分布，不能反推真实错误率。
- 历史 DOT 快照 `14,428` 行，按 CAMIS 连接 `3,164/12,693`；匹配后地址相同 `1,848/2,758`，坐标 ≤10m `2,641/2,751`，≤50m `2,682/2,751`。重复 CAMIS 匹配案例 `831`，全部保留为字段缺失/冲突诊断；历史快照不作为当前营业 Gold。

## 回归测试

```sh
python3 -m unittest discover \
  -s evals/location_verification/land_data_output_cross_source \
  -p 'test_*.py' -v
```

测试覆盖：状态/坐标分母计算、`>10m`/`>50m` 冲突分组、历史 CAMIS 资格和重复字段规则、冻结清单/immutable 数据库的失败关闭行为。它们不替代正式的独立人工 Gold。

## 面试复盘笔记（本轮）

一开始容易把已有的 1,000 条筛查队列当成唯一评测入口；用户明确要求直接用 LAND-DATA 项目本身来测后，范围改为冻结 SQLite 中的全量 NYC DOHMH 输出。实现时先校验冻结清单和数据库 SHA-256，再用 immutable read-only SQLite 查询 `source_contents → poi_candidates → pois`，并把同城 11 条 OSM 候选排除，避免把不同来源混进 DOHMH 分母。

当前 DOT 没有 CAMIS，因此没有把“名称＋地址能匹配”写成正确率，而是保守地要求唯一规范化名称＋完整地址，并把坐标可比、`>10m`/`>50m` 冲突按旧 `review_status` 分层。历史 DOT 能按 CAMIS 连接，但存在重复申请和时间差；修正后的做法是保留所有重复行，只有字段非空且完全一致时才进入分母，并在报告中明确它不能证明当前营业。

这轮最终留下可复现脚本、公开汇总、回归测试、逐案私有 JSONL 和哈希清单。面试时可以把它表述为“先冻结输入和证据边界，再用只读查询做全量诊断；发现匹配键不等于 Gold 后，把指标改成覆盖/一致性指标，并显式保留冲突和人工审核边界”。
