# Location Verification Agent：2026-10-28 改前盘点与预基线

> 本文件记录**已测的旧能力**和正式地点核验基线的缺口。它不是 Location Verification Agent 的准确率报告。正式前后评测协议见 [`../docs/location-verification-agent-harness-plan.md`](../docs/location-verification-agent-harness-plan.md)。

## 今天实际运行的测试

- 命令：`PYTHONPATH=src python3 -m land_data classifier evaluate --dataset evals/classifier_regression.jsonl`
- 结果：`local-classifier-v4` 在 45 条人工编写的**合成路由样本**中正确 41 条，route accuracy = 41/45 = 91.11%，macro-F1 = 93.30%，LLM 调用 0 次。
- 评测器给出的语义数据指纹：`sha256:6bf9a8f4bcfb284711021a676abd33b2b084d66350214e34afc66c320b244203`；文件字节 SHA-256：`ab31906330d10bb7e14224e5450c56ace5a063d9f22dbf0faa9a96fab7bc1fa1`。
- `PYTHONPATH=src python3 -m unittest tests.test_classifier_regression_fixture -q`：4 个数据契约测试通过。
- [历史核验离线回放](location_verification/historical/README.md)：51 条源记录与最终报告逐项一致，去重后 47 个实体；38 条 `CONFIRMED`、9 条 `CONFLICT`、4 条 `NOT_FOUND`；5 项回放校验测试通过。规范化记录 SHA-256 为 `0f0dbde40a1efe7e798a3967b9abd96fc95df2994cd813aeb19d7c13a783077e`。它只检查旧审核记录的结构、来源与合并一致性。
- 完整离线套件：`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -p 'test*.py' -q`，**334/334 通过**。首次运行有 6 项因夹具日期随真实时间过期而失败；已在 `tests/test_knowledge_bridge.py`、`tests/test_nyc_parks_events.py` 固定测试参考时钟，生产逻辑未改。修复后相关 24 项及完整套件通过。
- [SQLite 只读规模与性能预基线](location_verification/performance/sqlite-performance-baseline-2026-10-28.md)：本地库 304 个候选、273 个带证据；NYC 大库 12,958 个候选，均有来源和证据，其中 12,874 个有至少两**类**证据，但 0 个有两个不同来源**平台**。大库全候选读取／解析路径单次运行 p50 323.7 ms、p95 662.4 ms，20 次重复结果哈希一致；这些是本机 SQLite 读取数字，不是 Agent 端到端延迟或地点正确率。两个数据库在该只读测试前后文件指纹一致。
- [未标注候选筛查队列](location_verification/sampling/sampling-report-2026-10-28.md)：从已冻结的 SQLite 备份按固定种子抽取 **1,000 个唯一候选**（New York 800、Hanoi 200），私有 JSONL 仅保存在 `.local`，SHA-256 为 `9de97eeae5d4ea9badb5677e727faa6d4659887c913e8b394fdca66f112b1e86`；重复生成哈希一致。独立审计发现初版队列混入 13 条历史已审核社交地点且抽样使用旧审核字段，现已排除整个 XHS 历史来源，并移除旧审核／匹配／坐标验证字段的读取和输出。新队列仍没有 Gold、dev/test split、困难组配额或跨平台独立证据，不能直接变成锁定测试集。
- [Harness 合成故障用例](location_verification/fault_cases/README.md)：预注册 120 条参数化用例，分属 12 个故障族、每族 3 种结构变体；生成和结构校验通过，JSONL SHA-256 为 `e7d58c9437fc244e9858b660f4a72118fc4c1e79ef112df6175bc8898b466ebc`。这组用例**尚未对 Agent 或固定顺序对照运行**，不能报告故障处理成功率，也不进入真实地点准确率分母。
- [NYC 官方数据同源时序一致性测试](location_verification/temporal_official_check/temporal-official-summary-integrity-v2.md)：对冻结队列中 790 个 CAMIS 分 8 批回读同一个 DOHMH 数据集的新快照，查询并集及逐批响应归属校验通过；官方表出现 777 个，13 个未出现；可比较的名称 760/763 相同、门牌和街道 763/763 相同、坐标 753/755 在 10 米内，14 个多行 CAMIS 未强行选行。这些是同源字段一致性和数据漂移的观察值，**不是独立 Gold 或地点核验准确率**。私有逐案结果和原始响应哈希已保存，执行过程见[面试复盘笔记](../docs/location-verification-evaluation-interview-notes.md)。
- [NYC DOT 跨来源字段一致性测试](location_verification/dot_cross_source/dot-cross-source-summary.md)：保存交通局当前 2,516 条许可行和历史 14,428 条申请行，并用官方计数及哈希核验快照完整性。当前表没有 CAMIS，在 790 条餐馆候选中，73 条至少按规范化名称＋地址匹配一行、56 条唯一匹配、17 条多许可行；唯一匹配中的 54 条坐标可比，54/54 与交通局坐标相距不超过 10 米。历史表按 CAMIS 连接 198/790；可比地址 114/174 相同、坐标 162/168 在 10 米内。当前表的匹配条件不能被当作身份正确率，历史表也不能证明当前营业；这仍不是独立人工 Gold。完整分母和限制见报告，过程见[面试复盘笔记](../docs/location-verification-evaluation-interview-notes.md)。
- [LAND-DATA CLI 改前运行基线](location_verification/land_data_cli_baseline/land-data-cli-baseline-2026-10-28.md)：在冻结 NYC 数据库副本上真实执行 `validate` 和 `review list`。New York 的 12,704 个候选中，旧流程有 12,620 条（99.34%）处于 `AUTO_APPROVED`、84 条（0.66%）进入人工队列；84 条均因缺少必需字段。`validate` 重跑约 4.15–7.12 秒，28 张应用表逐行比较逻辑差异为 0。这里测量的是旧流程审核分流和幂等性，**99.34% 不是地点核验准确率**。
- [LAND-DATA 全量 NYC 输出跨来源诊断](location_verification/land_data_output_cross_source/land-data-output-cross-source-summary.md)：只读检查旧流程实际输出的 12,693 条 DOHMH 候选（其中 12,609 条 `AUTO_APPROVED`）。当前 DOT 中有 1,020 条按规范化名称＋完整地址唯一匹配，1,004 条坐标可比；其中 20 条相距大于 10 米、3 条大于 50 米，均被旧流程置为 `AUTO_APPROVED`。这些是值得人工复核的跨来源冲突，不是 20 条已证实的错误或任何 Agent 改后提升。

以上测试分别评价路由、旧 CLI 的审核分流，以及独立来源字段诊断。它们仍不衡量地址／营业状态是否核验正确，也不能用于计算动态选工具的改前／改后准确率提升。

## 可复用但尚不能作为盲测成绩的资产

- `docs/xhs-verification-a.json`、`b.json`、`c.json` 共 51 条历史核验记录：38 条 `CONFIRMED`、9 条 `CONFLICT`、4 条 `NOT_FOUND`，合并后 47 个实体。它们含审核结论与来源线索，可用于定义案例 schema、回放和人工标注规范，但已被人工看过，不能直接称为独立未见金标准。三个文件的字节 SHA-256 依次为 `e773342d8e12b050ac5a80f813738d0627510f9c194e64df2e3756d271de726a`、`06cf60d3b2db7ae3d7761bc15fe2eff640932c4db197364b04a4fd95d53ba921`、`8ada2ad8addfd806b358bd8bab7a8dc0cb9828e131a4b4add5646b44866dec5b`。
- 本地 LAND-DATA 数据库已有较多来源、POI、证据和审核记录；另一个 NYC 数据库有上万地点。它们可供抽样与性能测试，但没有满足本计划各困难场景的独立核验标签，而且大库没有跨平台独立证据，不能直接把记录数当作评测样本数。
- 旧的 10 篇真实帖文／19 个地点提及结果只评价地点**提取**，不评价地点**核验**。

## 正式改前基线的状态

**正式准确率对照未运行；旧流程运行基线已完成。** 当前没有达到计划中至少 500 个不同真实地点、其中 400 个锁定测试案例的独立标注集，也没有供两种策略在同一证据快照上运行的固定顺序对照。已经在冻结数据库副本上实际运行旧 CLI，并对全量 DOHMH 输出做跨来源诊断；这提供了真实的审核分流、耗时和潜在冲突分母，但不提供正确率。下一步需逐案资格审查、收集并封存外部证据、建立人工 Gold，然后在 Agent 改造前运行固定顺序对照。现有历史 51 条可做开发期回放试跑，但不得混入最终锁定测试集。

## 可复现快照已冻结

使用 [`location_verification/freeze_baseline.py`](location_verification/freeze_baseline.py) 将 **146 个**当前代码、配置、测试和评测资产文件归档，并用 SQLite backup API 为两个本地数据库生成独立副本。最终快照位于 Git 忽略的 `.local/baselines/location-verification-pre-agent-final-2026-10-28/`，目录权限为 `0700`，归档、清单和数据库文件为 `0600`。创建后运行 `verify`，逐文件哈希与两份数据库 `PRAGMA quick_check` 均通过，且未生成 SQLite `-wal/-shm` 旁车文件。`manifest.json` SHA-256：`922931df7d3a57bda867131c7b8890dc7442067c7b29b1a9c0d9f49e4a4230f2`；源码归档 SHA-256：`c48eeffde4fbea64873d50fde4dd40d073958a9a2d558b50111e9a67f0ac9715`；两份**备份副本** SHA-256 依次为 `b1508851cbb0eec6047a64b15daca955526e032fa07e27e69b3685f632e97580`、`6e4169a739776b3e48e8cb504f27ea8b808e424e04ccc1fac6db59763e5f7a11`。备份副本与源数据库字节哈希不同，不能混用；源库哈希见[只读性能报告](location_verification/performance/sqlite-performance-baseline-2026-10-28.md)。

这份快照保存的是**改前可用材料**，包括故障用例、抽样脚本、标注规程与测试修复；它不是 500 案独立 Gold，也不含未来工具回放所需的全部外部网页内容。1,000 条私有筛查队列在最终快照建成后，从其中的数据库副本重新生成，哈希与修正后的队列一致；队列另行校验，未计入 146 文件源码归档。快照生成时 Git 工作区仍有未提交更改；归档以当时文件字节为准，不以 `HEAD` 代替实际版本。报告和抽样公开清单在快照后补记最终哈希，当前文字不声称与归档内的旧版报告逐字相同。

当前 Git `HEAD` 为 `c05bb3694790dc7b52f4ef2d9b63a8af3e3ffc9a`，工作区有多处未提交更改，单独记录 `HEAD` **不足以复现**完整基线。今天实际跑过的分类器实现文件 `src/land_data/local_classifier.py` 和评测器 `src/land_data/classifier_evaluation.py` 字节 SHA-256 分别为 `851fbe41a84051f5e9bcab9ac1c6832c66cce216f816a600a5f4194c6961d591`、`5d93a9afc2790dc07a99a44c8a78530aa4d47c00710d1a57e857c0524695b37e`。现有代码、配置和数据库副本已在最终快照冻结；正式核验基线仍需封存独立案例的 Gold、外部证据回放快照、对照实现及运行配置，再生成新的完整评测清单。
