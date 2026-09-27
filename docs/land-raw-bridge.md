# LAND Raw → LAND-DATA 独立处理

在 LAND-DATA 目录运行，不需要 Codex 参与：

```sh
./land-data --db .local/land-raw.db process-land-raw \
  --raw-dir /Users/mars/Downloads/LAND/data/acquisition-pilot/nyc-wave-1/content/raw/records
```

`nyc-wave-1` 已被标记为历史 discovery 输入；默认命令会拒绝它，避免混入新采集器指标。只有存档分析才可显式加入 `--allow-historical`。

默认只做离线分流。`land_raw_runs` 在同一个 SQLite 文件中逐篇保存 Raw snapshot ID、来源身份、内容哈希、规则版本、路由原因、处理状态、API 调用次数、总处理耗时、模型耗时、缓存命中、API 原样返回的 usage 和错误类型。`RawSourceRecord` 与 `SourceContent` 对每篇记录落库；重新运行会跳过已完成快照，`--limit N` 每次处理最多 N 篇新快照，`--retry-errors` 重试错误。规则版本变化时重新分流。只把确定性无地点的短帖与限定的通用主题跳过；作者 POI geo tag、已有 POI 唯一名称精确命中走规则；其他进入模型待处理。

完成离线审阅并确认来源许可后，可加 `--llm-extract` 处理待模型记录。千问每篇最多请求一次；网络失败、校验失败进入待审，不会把失败当成功。来源的 `export_enabled=false`，真实小红书帖的衍生结果不会自动进公开数据集。密钥仍由本机 `.env` 提供。`usage_json` 只记录 API 响应确实提供的 usage；离线或失败前无响应时为 NULL。

## 现有数据离线验收（2026-09-22）

输入为 LAND `data/acquisition-pilot/nyc-wave-1/content/raw/records` 的 403 篇真实 Raw，不含新增 30 篇盲测。规则 v6：403 Raw/SourceContent、66 篇跳过、337 篇待模型、0 个现有 POI 精确命中、0 个结构化 POI geo tag、0 错误。预计模型路由占 83.62%；离线实际 API 调用 0，实际 token usage 不存在。用返回空抽取的离线替身对 403 篇跑过处理全链路：337 次替身调用、0 错误；它只验证调度和持久化，不证明真实抽取质量。重复运行全部 403 篇命中续跑跳过。

从跳过的 66 篇按固定随机种子 211 抽查 20 篇标题和正文，发现 1 篇“给领事馆点赞”的潜在地点服务信号，但没有足以确定具体分馆的线索；明确可定位的纽约 POI 推荐或活动漏报为 0/20，潜在地点信号漏报为 1/20。样本包含通用地铁、支付、护照经验。抽样观察不能当作总体漏报率或质量通过证明；扩大过滤前须继续复核。

## 独立定时入口设计

1. 获得可定时使用的数据来源及内容处理许可后，独立采集任务把每批授权的完整输入归档到固定目录，再调用 LAND `tools/collect_content.py --input ... --query ... --city new-york --data-dir ...`。Collector 已支持输入指纹、精确去重、进程锁和续跑。
2. 采集成功后，调度器调用上面的 `process-land-raw` 命令。SQLite 文件和 Raw 目录使用绝对路径；失败退出码触发告警，下一次重跑仍可续跑。按来源授权决定是否开启模型处理；待审队列由人审核后再发布。
3. 可用系统 `launchd` 或服务器定时器调度这个入口，但目前不要安装无人值守的小红书抓取任务：LAND 现有真实采集是登录浏览器里的搜索/详情 DOM 与人工监督的小批量流程，LAND Collector 的正式命令只消费本地 JSON。没有经验证可长期无人值守的官方公开笔记搜索/全文接口、稳定登录续期、访问限制处理和内容复用许可。

官方开放平台当前公开的 scope 中 `read_notes` 仍标为规划中，首期只有 `basic_info`；它也不等于任意公开笔记搜索授权。现阶段能独立定时运行的是**已获准输入的导入、分流和处理**，不能声称小红书端已实现合法可靠的无人值守采集。
