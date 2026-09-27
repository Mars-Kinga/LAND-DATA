# 纽约旅客场景与独立来源计划

`LAND/data/acquisition-pilot/nyc-wave-1` 的 403 篇小红书 Raw 已由
[`history-manifest.json`](/Users/mars/Downloads/LAND/data/acquisition-pilot/nyc-wave-1/history-manifest.json)
标记为 `HISTORICAL_DISCOVERY_INPUT`。它不能计入新采集器吞吐、来源覆盖或 unseen 质量基线。

| 场景与用户问题 | 需要核验的字段 | 可用来源与处理边界 |
| --- | --- | --- |
| 第一次从 JFK/LGA 进城，如何找到乘车点、购票或付款、携带行李换乘？ | 交通方式、乘车点、付款方式、换乘步骤、行李与无障碍条件、适用机场和核验日期 | 应以机场和交通运营方的公开指南核验；当前尚未接入这些指南，不能从机场附近的 OSM 餐饮 POI 推出换乘建议。路线选择和实时状态交给地图或交通应用。 |
| JFK 周边有哪些可定位的餐饮地点？ | 地点名称、类别、坐标、来源对象 ID、采集时间 | OpenStreetMap / Overpass 提供结构化地点候选；只证明来源记录中的地点属性，不提供体验推荐。 |
| 抵达或游览时，哪些餐饮地点有官方可核验身份和最近卫生检查记录？ | CAMIS、餐厅名称、地址、borough、菜系、最近检查日期、grade、坐标、采集时间 | NYC Open Data DOHMH `43nn-pn8j`：公开 Socrata JSON API，官方数据，每日更新。只解析结构化字段；检查等级是卫生检查字段，不是推荐评分。OSM 用于补充地点属性与独立交叉来源。 |

## 已接入的独立来源

| 来源 | 访问与更新 | 使用条件 | 进入 LAND-DATA 的字段 |
| --- | --- | --- | --- |
| [NYC Open Data DOHMH Restaurant Inspection Results](https://data.cityofnewyork.us/Health/DOHMH-New-York-City-Restaurant-Inspection-Results/43nn-pn8j/about_data) | 无认证 HTTPS JSON（Socrata），数据页声明 daily | 数据含重复的 violation 行、缺失与数据录入问题；只用 CAMIS 去重后的当前小批量记录，保留原检查日期/等级和审核状态 | CAMIS、DBA、地址、borough、菜系、检查日期、grade、坐标、采集时间、来源 URL |
| [OpenStreetMap / Overpass](https://www.openstreetmap.org/copyright) | 无认证 HTTPS Overpass，按需查询 | ODbL 署名和许可要求；限制纽约小 bbox、20 条以内、一次轻量重试 | OSM 对象 ID、名称、类别、坐标、地址、网站、营业时间、采集时间、字段 provenance |

两者都由普通程序直接读取；不使用千问。社区帖若以后获得访问和处理许可，只能成为单独的 `community` 来源，且其个人经验保持为来源陈述/待审信号，不能自动变成地点事实、运营规则或推荐结论。

## 独立运行

餐饮小批量：

```sh
./land-data --db .local/nyc-public.db pipeline --source nyc-open-data-restaurants \
  --city new-york --borough Manhattan --limit 5 --timeout 15 \
  --output .local/exports/nyc-restaurants.json
```

JFK 周边的结构化餐饮地点：

```sh
./land-data --db .local/nyc-public.db pipeline --source osm \
  --scope config/nyc-jfk-arrival-scope.json --category cafe --category restaurant \
  --limit 10 --timeout 15 --output .local/exports/nyc-jfk.json
```

命令本身不依赖 Codex，重复运行会按稳定来源 ID 去重。定时器可直接运行以上命令并依据退出码告警。它仍依赖来源网络、可用性和许可。当前机场场景真正未覆盖的是首次抵达所需的乘车点、付款和换乘步骤；应从机场与交通运营方的稳定指南核验。实时航班、到站、中断和动态最优路线不属于本采集计划。
