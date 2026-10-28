# LAND-DATA CLI 改前地点流程基线（2026-10-28）

这份报告来自真实执行的 LAND-DATA `validate` 和 `review list`，输入为改前冻结
的 NYC 增量 SQLite 快照，所有写入发生在私有副本。它度量旧流程的运行覆盖、审核
分流、耗时和幂等性，不提供独立 Gold，也不声称地点核验准确率。

| 指标 | 结果 |
|---|---:|
| 全库地点候选 | 12,958 |
| New York 候选 | 12,704 |
| New York `AUTO_APPROVED` | 12,620（99.34%） |
| New York `NEEDS_REVIEW` | 84（0.66%） |
| 人工队列原因 | 84 条均为 `MISSING_REQUIRED_FIELD` |
| `LocationEvidence` | 25,832 行，覆盖 12,958 个候选 |
| `validate` 耗时 | 4.15–7.12 秒（重复运行） |
| `review list` 耗时 | 约 0.30 秒 |
| 应用表逻辑行差异 | 0 |
| 网络 / LLM 调用 | 0 / 0 |
| SQLite `quick_check` / `integrity_check` | 输入与副本均 `ok` |

输入快照 SHA-256：
`6e4169a739776b3e48e8cb504f27ea8b808e424e04ccc1fac6db59763e5f7a11`。

## 口径

`AUTO_APPROVED` 是 LAND-DATA 旧工作流的内部状态，不是外部事实标签。99.34% 只能
说明旧流程把这些候选留在自动通过状态，不能写成“地点核验准确率”。正式前后测试
仍需要独立人工 Gold、锁定证据快照、固定顺序对照和动态 Agent 的配对轨迹。

## 复现

```sh
PYTHONPATH=src python3 \
  evals/location_verification/land_data_cli_baseline/run_land_data_cli_baseline.py \
  --input-db .local/baselines/location-verification-pre-agent-final-2026-10-28/nyc-incremental-acceptance-v2.db \
  --run-dir .local/location-verification-live-check/land_data_cli_run/reproducible-run
```
