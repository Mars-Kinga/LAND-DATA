# 城市主页生成任务

Dashboard 在创建城市和查看采集计划时，会检查 LAND 的：

```text
LAND/LAND/Resources/city-home-registry.json
```

只有 registry 中同一个 `cityID` 的 `reviewState` 为 `ready`，才会显示“已有可用主页”。`scaffold` 会显示为“主页预览，等待验收”，不会被当成完整主页。`published-content.json` 只表示城市数据内容是否已经发布，不能单独证明主页已经存在。

## Dashboard 操作

1. 在“选择城市”填写城市 ID、城市名称、国家代码、操作者和原因。
2. 点击“创建 / 补全城市”，再点击“查看采集计划”。
3. 如果 LAND 没有这个城市主页，Dashboard 会显示：`确定让 Codex 生成一个吗？`
4. 点击“确定，让 Codex 生成主页”。这是唯一的启动确认。
5. Dashboard 会保存一个 JSON 任务文件，并尝试启动本机 Codex CLI。任务状态会在页面和任务文件中持续显示。

## 固定的 Codex 边界

Dashboard 不接受页面输入的 shell 命令、模型、工作目录或执行策略。确认后的作业固定为：

```text
模型：gpt-5.6-sol
强度：high
沙箱：workspace-write
审批：--approve-for-me
最多 Codex 作业：1
Qwen 调用：0
```

Codex 的工作目录固定为 LAND，LAND-DATA 以受控的附加目录提供采集任务规范和发布桥接。Dashboard 使用参数数组启动进程，不经过 shell；城市名称和原因只作为 JSON 数据传入。

任务文件保存到：

```text
.local/dashboard/homepage-tasks/homepage-<city>-<random>.json
```

同一城市已有 `TASK_READY` 或 `IN_PROGRESS` 任务时，重复点击不会创建第二个任务。找不到受信任的本机 Codex CLI 时，任务保持 `TASK_READY`，文件中的 `execution.command` 仍保存可执行参数；这不代表主页已生成。

## 任务会要求什么

生成任务规范会要求 Codex：

- 阅读现有 `CityArtwork.swift`、`TravelStyle.swift`、`Models.swift` 和城市主页 registry，保持 LAND 的现有风格；
- 使用当地文字或可验证的本地字形特征设计城市大字，并提供文字替代；
- 加入一个有明确文化关联的动态元素；
- 遵守 `accessibilityReduceMotion`、`scenePhase` 和 VoiceOver，减少动效时保留完整静态画面；
- 把城市注册为 `reviewState=scaffold`，通过视觉和测试验收后再改为 `ready`；
- 运行 LAND 测试和构建验收，记录变更文件和结果；
- 保留后续城市更新、审核、发布、撤回和回滚入口。

Codex 退出后，Dashboard 会再次读取 registry：

| 状态 | 含义 |
| --- | --- |
| `TASK_READY` | 任务规范已保存，等待可用的 Codex CLI 或再次启动 |
| `IN_PROGRESS` | Codex 正在执行 |
| `NEEDS_ACCEPTANCE` | Codex 已生成 scaffold，需要人工验收并设为 `ready` |
| `COMPLETED` | registry 已为 `ready`，可继续导入数据 |
| `FAILED` | Codex 出错，或退出后没有检测到主页 |

## 数据什么时候进入 LAND

主页生成和数据采集是两个状态。主页完成后仍需运行城市采集、零模型分流、审核和发布桥接：

```text
主页任务 → reviewState=ready
城市采集 → 规则分流 / 必要时 Codex 或人工审核
审核通过 → publish prepare → verify → install
```

未审核的 Raw、社区原文和待审候选不会因为主页任务完成而直接进入 App。后续更新继续从 LAND-DATA Dashboard 运行，发布包保留版本、差异和回滚记录。

任务规范本身只在本机保存；Dashboard 不调用千问，也不会把 `TASK_READY`、`IN_PROGRESS` 或 `NEEDS_ACCEPTANCE` 显示成已经上线。
