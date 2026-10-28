# Location Verification Agent 改前评测入口

此目录是[主计划](../../docs/location-verification-agent-harness-plan.md)的评测工作区。**当前没有正式的地点核验准确率或改前／改后提升数字**：已有开发期 CLI 和固定顺序真实记录回放，但尚无真实地点动态策略对照或至少 500 个真实地点的独立人工 Gold。已有 10 帖提取指标、45 条合成路由样本和 51 条历史审核记录分别衡量不同问题，不合并为一个分数。

现阶段已具备：

- [人工标注与数据隔离规程](annotation_protocol.md)：100 个开发案例、200 个日常锁定案例、200 个困难锁定案例；未调用工具前，Agent 不能读取全部冻结证据。
- [历史审核离线回放](historical/README.md)：核对 51 条旧记录的结构、去重、来源与最终报告一致性；只作为开发参考。
- [未标注候选抽样](sampling/sampling-report-2026-10-28.md)：基于冻结的数据库备份，生成 1,000 条本地私有筛查队列；仍需逐条核验和人工 Gold。
- [Harness 合成故障用例](fault_cases/README.md)：120 条预注册案例、12 个故障族；当前仅完成生成与结构校验，尚未运行 Agent。
- [SQLite 只读性能预基线](performance/sqlite-performance-baseline-2026-10-28.md)：记录当前数据量、来源覆盖和存储读取耗时，不代表 Agent 端到端表现。
- [冻结脚本](freeze_baseline.py)：在本地 `.local/baselines/` 保存当前代码/配置归档及 SQLite 一致性备份，并校验逐文件 SHA-256。数据库副本不入 Git；最终快照为 `location-verification-pre-agent-final-2026-10-28`。
- [2026-10-28 预基线报告](../location-verification-baseline-2026-10-28.md)：记录已经实际运行的测试及尚未具备的正式基线条件。
- [DOHMH 同源时序一致性检查](temporal_official_check/temporal-official-summary-integrity-v2.md)：790 个冻结候选的官方数据回读、查询完整性和字段漂移；不是独立准确率。
- [NYC DOT 跨来源字段一致性检查](dot_cross_source/dot-cross-source-summary.md)：对交通局当前与历史快照做独立来源覆盖和字段诊断；不是人工 Gold。
- [LAND-DATA CLI 改前运行基线](land_data_cli_baseline/land-data-cli-baseline-2026-10-28.md)：在冻结数据库的私有副本上实际运行 `validate` 和 `review list`，记录审核分流、耗时和幂等性；内部 `AUTO_APPROVED` 状态不是准确率。
- [LAND-DATA 全量 NYC 输出跨来源诊断](land_data_output_cross_source/land-data-output-cross-source-summary.md)：对旧流程实际生成的 12,693 条 DOHMH 候选与 DOT 快照逐案比较，保留跨来源匹配和坐标冲突的分母；不是独立人工 Gold。
- [开发期配对试验协议](paired_pilot_protocol.md)：预先固定 1,004 条真实可比记录与 120 条合成故障的比较口径；尚无同条件真实地点动态策略结果。
- [Harness 开发期最小闭环](harness_pilot/README.md)：已接入开发期 CLI，包含决策／权限／预算／checkpoint／Goal Gate、同证据回放、合成千问烟测、1,004 条固定顺序坐标试验和 100 条业务候选只读回放；无真实地点独立 Gold。
- [500 条日常筛查输入、155 条困难风险证据包及 655 条 AI 暂定标注](annotation_packet_500/README.md)：从冻结数据确定性选样并保存逐案来源与哈希；Codex 已完成保守的来源关系和未知项分流，Gold 与 split 仍为空，不能计算真实地点准确率。
- 评测执行与面试复盘笔记保存在本地 `docs/location-verification-evaluation-interview-notes.md`；每次记录方法、实际结果、初始错误与修正、可用于面试的表述边界。此个人笔记不随公开仓库发布。

校验已存在的最终快照（仓库根目录执行）：

```sh
python3 evals/location_verification/freeze_baseline.py verify \
  .local/baselines/location-verification-pre-agent-final-2026-10-28
```

如需重新冻结当前工作区，使用 `create` 并指定一个尚不存在的新目录，不覆盖上述快照。

正式基线需要另外完成：锁定候选抽样与真实证据快照、隔离人工 Gold、固定顺序工具对照、逐案运行轨迹和事先声明的指标。只有这些条件具备并运行后，才能将后续 Agent 结果与改前结果计算为提升。
