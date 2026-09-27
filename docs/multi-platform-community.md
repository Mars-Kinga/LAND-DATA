# 多平台社区采集

LAND-DATA 把社区平台当作可插拔 provider。采集器负责搜索或接收本人可见的授权输入，统一写入 Raw、SourceContent、规则处理、provenance 和审核链；Codex 与千问都不在默认采集路径中。

## 当前入口

| 平台 | 自动搜索 | 认证 | Dashboard | 默认 LLM |
| --- | --- | --- | --- | --- |
| 小红书 | 已实现，登录浏览器可见内容 | 本机 Chrome 登录 | 可启动 | 0 |
| Threads | 已实现，Meta 官方关键词搜索 | 官方 OAuth token | 可启动 | 0 |
| 其他平台 | provider contract 与 JSON 导入已实现 | 取决于来源 | 显示真实配置状态 | 0 |

Threads 未配置 token 时，Dashboard 返回 `AUTH_REQUIRED`。token 不写入 URL、SQLite、任务参数或普通日志。关键词结果不等同于城市过滤，地点要继续经过解析和审核。

## 通用 provider contract

实现 `PagedSocialProvider.status()` 与 `fetch_page()` 后，交给 `SocialConnectorAdapter`。连接器提供：

- 平台 ID 或规范 URL hash 稳定身份；
- provider / city / query checkpoint；
- 内容 hash 去重和更新识别；
- 完整页面提交后才推进游标；
- `PUBLIC`、`AUTHENTICATED`、`AUTH_REQUIRED`、`UNAVAILABLE` 状态；
- 统一 `SourceItem` 输出和零模型默认值。

连接器不处理 Cookie、token 或授权头，不绕过登录、访问限制和验证码，也不会为平台编造不存在的搜索接口。

## 通用导入

```bash
./land-data social list

./land-data --db .local/land-data.db social import \
  --provider <平台小写 ID> --city <城市 ID> \
  --mode browser --input <records.json>
```

`browser` 表示内容来自本人能看到并获准保存的浏览器页面；`public` 只用于平台明确提供的公开 JSON feed。两者都会使用社区来源的短期 Raw 保留和禁止公开导出策略。

## 接入下一个平台

1. 先确认官方 API、OAuth 权限、字段存储和删除传播要求。
2. 实现 provider 的有界分页与状态检查。
3. 在 `config/sources.json` 声明保留和导出策略。
4. 加入 Dashboard 来源计划；未接通执行器时必须显示“尚未接入”。
5. 用离线 fixture 验证去重、更新、中断恢复、授权失败和日志脱敏。
6. 做小规模真实验收，再决定是否允许进入公开发布桥接。
