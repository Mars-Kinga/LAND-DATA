# LAND-DATA 改前 validate/review 基线

这个目录记录当前 LAND-DATA CLI 在 Location Verification Agent 改造前的
真实运行结果。它是流程覆盖、审核分流和幂等性的基线，不是地点核验准确率。

## 运行方式

使用冻结的 NYC 数据库作为只读输入，并把改动写进私有副本：

```sh
PYTHONPATH=src python3 \
  evals/location_verification/land_data_cli_baseline/run_land_data_cli_baseline.py \
  --input-db .local/baselines/location-verification-pre-agent-final-2026-10-28/nyc-incremental-acceptance-v2.db \
  --run-dir .local/location-verification-live-check/land_data_cli_run/reproducible-run \
  --output evals/location_verification/land_data_cli_baseline/summary.json
```

脚本会先以 SQLite `mode=ro` 读取输入快照，然后复制数据库并执行：

```text
python3 -m land_data --root . --db <private-copy> validate
python3 -m land_data --root . --db <private-copy> review list --type candidate --city "New York"
```

`validate` 和 `review` 都不带 `--llm-extract`，也没有 enrichment 参数；这条路径不
发起网络请求或模型调用。脚本会检查输入和输出的 `quick_check`/`integrity_check`，
并逐行比较应用表，区分文件哈希变化与逻辑数据变化。

## 当前运行摘要

2026-10-28 在冻结的 `nyc-incremental-acceptance-v2.db` 上运行：

| 指标 | 结果 | 解释 |
|---|---:|---|
| 全库候选 | 12,958 | New York 12,704；Hanoi 254 |
| New York `AUTO_APPROVED` | 12,620 / 12,704 = 99.34% | 现有工作流状态，不是准确率 |
| New York `NEEDS_REVIEW` | 84 / 12,704 = 0.66% | CLI 审核队列分母 |
| 审核原因 | 84 `MISSING_REQUIRED_FIELD` | 当前流程要求人工补齐字段 |
| `LocationEvidence` | 25,832 行 / 12,958 候选 | 证据行覆盖，不代表独立真值 |
| `validate` 耗时 | 4.15–7.12 s（3 次运行） | 本机离线运行时间 |
| `review list` 耗时 | 0.31 s | New York candidate 队列 |
| 逻辑行差异 | 0 | validate 对冻结输入幂等 |
| SQLite 检查 | input/output `quick_check` 与 `integrity_check` 均 `ok` | 数据库完整性 |
| 网络 / LLM | 0 / 0 | CLI 入口没有外部工具或模型参数 |

原始冻结输入 SHA-256 为
`6e4169a739776b3e48e8cb504f27ea8b808e424e04ccc1fac6db59763e5f7a11`。
输出副本的文件 SHA-256 会因 SQLite 从 `delete` journal 切换为 `wal` 而变化，
但应用表逻辑行差异为 0；因此不能把文件哈希变化解释成地点状态变化。

## 面试中的正确表述

可以说：“我先在 12,704 个纽约候选上跑通现有 LAND-DATA 的 validate 和 review
路径，得到 84 条明确进入人工审核的记录，离线 validate 约 4–6 秒，并用逐表 diff
确认重跑没有改变冻结数据。这个结果冻结了改前的审核覆盖和幂等性基线。”

不要说：“现有 LAND-DATA 对 12,704 个地点达到了 99.34% 准确率。”当前库没有
独立人工 Gold，也没有动态地点核验 Agent；`AUTO_APPROVED` 只能说明旧规则通过了
内部状态门。后续 Agent 需要在同一案件和证据快照上比较独立 Gold、人工移交召回率、
字段正确率、证据链完整率、调用次数和时延。
