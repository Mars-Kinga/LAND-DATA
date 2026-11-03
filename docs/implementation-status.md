# 实现状态与版本边界

核对日期：2026-11-03。公开代码基线为 `50d3438`；本次同步仅更新文档和 `doc/` 忽略约定，不将本地未提交的业务代码一起发布。下表中的“本地工作区”是该日期的实现快照，不能作为 clone 后已经具备的能力。

## 公开版本与本地工作区

| 功能 | 公开代码基线 | 本地工作区新增实现 |
| --- | --- | --- |
| 地点发布 | 已有 `place-publish preflight/prepare/diff/verify/install/rollback`；符合门槛的官方结构化来源有确定性发布路径 | 扩展类别、中文单语 overlay，以及受审核来源约束的地点补充字段 |
| Place 类别 | `restaurant/cafe → food`，`museum → sight` | 保留 restaurant、cafe、bar、museum、hotel 等类别；具体列表见 Place 说明 |
| Place 语言 | 人工 overlay 要求 `zh-Hans`、`en` | `zh-Hans` 必需，`en` 可选；提供的英文对象仍逐字段检查 |
| QAU 知识发布 | 已接受草稿 → 完整索引合并 → 人工 verify → 显式 install/rollback | 新增 reviewed overlay、更多场景、河内覆盖门槛、草稿 retract 与对应内容移除 |
| 社区经验 | 基础地点提及和推荐信号；默认规则处理，可选模型抽取 | `EXPERIENCE` 关系、菜系／推荐项／优缺点／避雷原因、证据片段及抽取方式 |
| 知识草稿导入 | 已有明确选中候选后的模型抽取与人工审核 | 零模型 `xhs knowledge import-draft` |
| 城市主页 | registry 的 `ready` / `scaffold` 判定与受控生成任务 | 分别返回 `visual_shell_ready`、内容数量和 `content_readiness`；发布就绪要求界面与内容均满足条件 |
| QAU 构建检查 | 安装默认 Swift 测试；Xcode 构建显式启用 | 模拟器环境不可用时，可回退到不签名的 iOS device SDK 构建；不等于真机运行 |
| Location Verification Harness | 开发期单案循环、固定顺序／千问策略、journal、Goal Gate、人工队列及显式冲突 hold | 本次核对未发现与公开基线不同的 Harness 实现；下面的未完成项仍成立 |

依据：[Place 发布代码](../src/land_data/place_release_bridge.py)、[QAU 桥](../src/land_data/knowledge_bridge.py)、[知识处理](../src/land_data/knowledge_processing.py)、[主页状态](../src/land_data/city_homepage.py)、[安装检查](../src/land_data/release_install.py)。GitHub 上这些链接显示公开实现；本地打开则显示工作区实现。

## 已有发布路径的准确边界

- QAU 和 Place 均已有发布桥，不能再写“只有 QAU 能发布”。采集、审核、准备包、验证、安装是不同动作。
- 普通 `export` 仍输出 LAND-DATA 自有 schema；App 接入通过 `publish` 或 `place-publish`。
- Event、官方指引快照的通用发布桥仍未完成；Place 合并时保留已有 Guide，不等于能从任意采集结果生成 Guide。
- Place 官方自动路径只允许 `official-directory` 和 `nyc-open-data-restaurants`，并检查状态、字段、坐标与来源门槛；OSM 和社区来源不能仅因结构化就使用该路径。
- QAU 安装默认执行 Swift 测试；Place 安装默认执行内容校验，Swift/Xcode 检查通过 `--run-app-checks` 开启。测试、构建和真实 App 使用分别记录。
- 知识草稿撤回与发布包回滚是局部能力；跨来源、跨对象的通用删除传播和远程 HTTP 发布仍未完成。

## Harness 已实现的核心

- `Case / Claim / Evidence / Action` 数据模型，固定顺序和千问决策入口。
- 工具白名单、只读约束、参数键校验、来源组校验、工具调用次数与决策次数限制。
- 独立 Goal Gate 检查字段值、身份、时效、来源组及 `lineage_key`；证据不足、冲突或模型失败有明确处理结果。
- 独立 SQLite journal、已保存动作的恢复、私有人工审核包与只读人工队列。
- 从冻结证据包或业务候选库只读准备 Case；可选 `location-verify-review-hold` 仅在显式 `--apply` 时将符合条件的冲突对象转为待审。

## Harness 尚未完成

| 原计划项 | 当前差距 |
| --- | --- |
| s01–s02 真实动态调查与工具 | 主要执行冻结证据回放；官网搜索、地图候选、地理编码及网页读取尚未接成真实调查工具。合成千问烟测成功，已有真实案件千问试跑因客户端错误交人工 |
| s03–s04 预算与动作审计 | 已有次数限制与校验；尚无统一整案 deadline、累计 token/成本硬预算、通用工具超时和输出长度控制，ActionRecord 不含逐次耗时／成本 |
| s05、s10 调查计划与任务 | 有按声明计算的证据缺口和案件级 checkpoint；尚无完整的可更新调查计划、子任务依赖与任务级恢复 |
| s06–s09 | 子代理、按需 Skill 加载、上下文压缩、跨案且有有效期的记忆尚未实现 |
| s11–s14、s16 | 后台查询通知、定时重查、团队认领、MCP 接入及批量可恢复工作流尚未实现 |
| s15 统一接入 | 单案核心可运行，尚未完成上述机制及正式业务审核界面的统一接入 |
| 修正建议 | 当前终态为 `VERIFIED` 或 `HANDOFF_HUMAN`，审核包的 `proposed_correction` 固定为空；“建议搬迁后新地址”仍是目标场景 |
| s17 完成条件的验证 | 确定性 Gate 已实现；真实独立证据覆盖、完整故障集运行及无 Gate 消融尚未完成 |
| 业务闭环 | 显式冲突 hold 仅在临时库验证，尚未成为默认发布拦截；未自动撤回已安装内容，人工裁决与案件状态的完整闭环待接入 |
| 盲测隔离 | 回放工具结果和 Agent 仍在同一进程，正式盲测要求的隐藏证据进程／权限隔离尚未完成 |

核心依据：[Harness](../src/land_data/location_verification.py)、[千问决策](../src/land_data/location_verification_qwen.py)、[回放工具](../src/land_data/location_verification_replay.py)、[冲突 hold](../src/land_data/location_verification_review.py)。完整目标保留在[原计划](location-verification-agent-harness-plan.md)。

## 评测证据与未完成项

已有运行记录包括 1,004 条固定顺序坐标一致性试验：20 条差异转人工、984 条在 10 米阈值内，模型调用为 0；以及 100 条业务候选只读准备与固定顺序回放，均因身份证据不足交人工。这些是工程和证据路由检查，不是地点核验准确率或动态 Agent 的前后提升。

500 条日常候选和 155 条困难候选已准备证据材料及 AI 暂定标注，尚未成为独立 Gold，也未完成正式开发／锁定测试分组。搬迁／停业组仅有 5 条筛查线索，候选配额至少还缺 45 条；已有线索仍需资格核实。120 条故障用例已生成并做结构校验，尚未全部对 Agent 运行。

仍需补足证据及标签，冻结正式基线，完成同条件动态策略配对、人工审核时间／成本记录、置信区间和无 Goal Gate 消融。历史测试数字只对应其代码快照；本次文档同步不把它们改写成新的效果评测。

依据：[开发期报告](../evals/location_verification/harness_pilot/README.md)、[标注材料](../evals/location_verification/annotation_packet_500/README.md)、[故障资产](../evals/location_verification/fault_cases/README.md)、[配对协议](../evals/location_verification/paired_pilot_protocol.md)。

## 本次核对与验证

2026-11-03 对 Harness、Place 发布、QAU 桥、安装与城市主页相关测试进行离线复核：本地工作区 80 项通过，公开代码基线 `50d3438` 的相同测试文件范围 65 项通过。两者数量差异来自尚未发布的功能与测试；这不是全量测试或真实来源、模型服务、App 实机验收。本次同步不修改业务代码。
