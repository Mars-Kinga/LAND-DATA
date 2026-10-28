# NYC DOHMH 同源时序一致性检查

`temporal_official_check.py` 是一个只读离线比较器：它把冻结筛查队列中
`nyc-open-data-restaurants` 的 CAMIS，与本地保存的 NYC DOHMH `43nn-pn8j`
API 响应做字段级比对。脚本不访问网络、不调用模型、不调用地图服务。

这项检查比较的是两个时间快照，而不是独立 Gold，也不是地点核验准确率：

- 名称只做 NFKC、大小写折叠、标点和空白规范化后的精确比较；
- 地址按 `building + street` 拼接后做相同的规范化比较，并只将常见街道缩写（如 `ST/ STREET`、`AVE/ AVENUE`、`E/ EAST`、`PL/ PLACE`）和数字序数后缀归一化；
- 两边都有合法坐标时计算球面距离，并分别报告 `≤10m` 和 `≤50m`；
- 当前官方批次中没有出现的 CAMIS 记为 `missing_in_latest_official_snapshot`，不能解释为停业；
- 同一个 CAMIS 出现多行时记为 `official_duplicate_conflict`（多行待逐案处理），逐案保留所有行，绝不任选一行；所选名称、地址、坐标字段是否有分歧另行统计。

默认输入是：

```text
.local/baselines/location-verification-pre-agent-final-2026-10-28/
  screening/location-verification-screening-2026-10-28.jsonl
.local/location-verification-live-check/official_batches/*.json
.local/location-verification-live-check/official_queries/*.where
```

官方 JSON 可以是原始数组，也可以是包含 `records`、`results`、`rows`、
`items` 或 `data` 数组的捕获封装。每个批次文件对应同名的 `.where` 查询文件；
脚本在读入任何响应前执行 fail-closed 完整性检查：

- 每个官方批次必须有且只有一个同名 `.where` 文件；孤立查询文件也会报错；
- 每个查询文件不能含重复 CAMIS，查询文件之间不能重复；全部查询 ID 的并集必须与冻结队列完全相等，因此查询遗漏或额外 ID 都会终止运行；
- 每个响应中的 CAMIS 必须属于该批次自己的请求集合，跨批次或超出请求的返回会终止运行；
- 不能映射到 CAMIS 的响应行也不会被静默纳入比较。

通过完整性检查后，脚本才会把批次结果哈希、查询哈希、字段选择、覆盖数量写入私有
`official_snapshot_manifest.json`。

运行（数据批次全部就位后）：

```bash
python3 evals/location_verification/temporal_official_check/temporal_official_check.py \
  --captured-at 2026-10-28T00:00:00Z \
  --run-id 20261028T000000Z
```

若要求官方批次覆盖冻结队列中的每个 CAMIS，可增加 `--require-complete`；
当前缺失案例会使命令退出而不生成“完整”汇总。逐案 JSONL 和 API manifest
写入 `.local/location-verification-live-check/`，权限为目录 `0700`、文件
`0600`；公开 JSON/Markdown 汇总只含计数、分母和哈希，写入本目录。

公开汇总中的字段比率明确列出分母：缺失官方记录和重复 CAMIS 冲突不进入名称、
地址或坐标的可比较分母。因而即使出现率或字段一致率较高，也只能说明这两个
同一官方来源快照之间的时序一致性，不能作为 Agent 的独立核验准确率或跨来源
真实性证明。

完整性检查的临时目录反例测试：

```bash
python3 -m unittest discover \
  -s evals/location_verification/temporal_official_check \
  -p 'test_*.py' -v
```

测试会用临时目录覆盖缺失查询文件、查询 ID 重复/并集不一致以及响应跨批次三类失败路径；不会改写既有私有逐案结果、manifest 或公开汇总。
