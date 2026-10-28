# LAND-DATA 全量 NYC 输出跨来源诊断

本报告只读读取冻结的 LAND-DATA NYC SQLite 输出，再与已保存的 NYC DOT 当前快照和历史快照做字段级诊断。它用于说明旧流程的输出分布、跨来源覆盖和坐标冲突，不是人工 Gold，也不能直接写成地点核验准确率或 Agent 提升。

- 运行标识：`20261028T000000Z`；脚本 SHA-256：`6ed88ecda814e292dd539a799ab3f4fcd160581487168707a81280173071ed82`。
- 冻结 SQLite：`nyc-incremental-acceptance-v2.db`；SHA-256：`6e4169a739776b3e48e8cb504f27ea8b808e424e04ccc1fac6db59763e5f7a11`；字节数：`212193280`。
- 冻结清单 SHA-256：`922931df7d3a57bda867131c7b8890dc7442067c7b29b1a9c0d9f49e4a4230f2`；SQLite `quick_check`：`ok`。
- DOHMH `source_contents`：`12693`；对应 `poi_candidates`：`12693`；去重后的已解析 POI：`12580`。同城数据库另有 11 条 OSM 候选，本诊断不纳入。

## 改前旧系统输出分布

- `AUTO_APPROVED`：`12609`；`NEEDS_REVIEW`：`84`；其他/缺失状态：`0`。
- 已解析到 POI 的候选：`12609`；未解析：`84`。
- 候选行关联的 POI 状态分布：`{"AUTO_APPROVED": 12609}`。
- 去重 POI 输出状态分布：`{"AUTO_APPROVED": 12580}`；POI 状态冲突 ID：`0`。

## 当前 NYC DOT（独立来源，但不是 Gold）

- 当前快照行数：`2516`；名称＋地址可比较候选：`12693`。
- 唯一规范化名称＋完整地址匹配：`1020/12693`（覆盖率 `0.080359`）；任意匹配（含多行）：`1310/12693`。
- 唯一匹配坐标可比较：`1004/1020`；坐标 `>10m`：`20/1004`；`>50m`：`3/1004`。
- 全体候选旧状态分层：`AUTO_APPROVED` 唯一匹配 `1019/12609 (0.080815)`；`NEEDS_REVIEW` 唯一匹配 `1/84 (0.011905)`。
- 全体候选旧状态下坐标冲突：`AUTO_APPROVED` >10m `20/1004 (0.01992)`、>50m `3/1004 (0.002988)`；`NEEDS_REVIEW` >10m `0/0 (None)`、>50m `0/0 (None)`。
- 解释：当前 DOT 没有 CAMIS，名称＋地址只做保守检索键；唯一匹配和坐标阈值是跨来源一致性/冲突诊断，不是地点身份正确率。

## 历史 NYC DOT（CAMIS 连接）

- 历史快照行数：`14428`；CAMIS 连接：`3164/12693`（覆盖率 `0.249271`）。
- 匹配后地址相同：`1848/2758 (0.670051)`；坐标 ≤10m：`2641/2751 (0.960015)`；≤50m：`2682/2751 (0.974918)`。
- 重复 CAMIS 匹配行：`831`；字段缺失或冲突：`831`。
- 历史 DOT 数据按 CAMIS 连接但保留重复申请；它只支持历史字段一致性诊断，不能证明当前营业，也不作为当前营业 Gold。

## 复现与证据边界

脚本通过冻结清单校验数据库字节数和 SHA-256，再以 SQLite immutable read-only 模式读取；DOT 快照和抓取清单也会校验哈希。逐案结果只放在 `.local/location-verification-live-check/land_data_output_cross_source/` 并单独哈希，公开文件只保留聚合计数和分母。正式 Agent 前后提升仍需独立人工 Gold、相同锁定案例和可审计运行轨迹。

- 当前全体候选唯一匹配汇总（便于查阅）：`1020/12693 (0.080359)`。
- 历史全体候选 CAMIS/字段汇总（便于查阅）：`3164/12693 (0.249271)`。

复现命令：

```sh
python3 evals/location_verification/land_data_output_cross_source/land_data_output_cross_source.py \
  --run-id 20261028T000000Z --force
```
