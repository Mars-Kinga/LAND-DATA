# LAND-DATA Location Verification Harness：开发期最小闭环

2026-10-28 新增隔离模块 [`src/land_data/location_verification.py`](../../../src/land_data/location_verification.py) 和可选的[千问决策适配器](../../../src/land_data/location_verification_qwen.py)。现已接入 LAND-DATA 主 CLI 的开发期只读命令，仍未进入正式审核／发布路径，也没有在真实地点 Gold 上评测。旧流程的冻结数据库不受影响。

当前闭环保存 Case／Claim／Evidence／Action，决策器提议下一项只读工具；Harness 检查工具白名单、参数和来源组，限制调用与决策次数，把动作和状态写入独立 SQLite checkpoint，并由确定性的 Goal Gate 检查身份、时效、证据独立性及冲突。证据不足或预算用尽时转人工；没有自动修改或发布地点的工具。独立性检查同时要求至少两个来源组和两个不同 `lineage_key`；没有谱系键时以内容哈希代替，仍需人工或来源适配器核实真实转载链。

`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -W ignore::ResourceWarning -m unittest discover -s tests -p 'test*.py' -q`：本次公开提交快照的完整测试为 **313/313 通过**；本地含其他未提交功能的工作区为 365/365，通过数不能混用。Harness 测试覆盖动态下一工具、模型过早停止、冲突、分店身份、权限、预算、未注册来源组、断点恢复、过期证据、转载同谱系、模型输出白名单、坐标容差、冻结清单校验及非 journal 数据库保护。这只是开发期契约测试，不是 120 条故障集全部通过。

## 有界模型烟测

脚本 [`smoke_qwen.py`](smoke_qwen.py) 使用一个**合成地点**和两个模拟只读工具；各次运行的私有 SQLite journal 与摘要保存在 `.local/location-verification-live-check/harness_pilot/`。初次在网络沙箱中请求失败，获准联网后成功；没有把网络错误当作地点不存在。

| 运行 | Prompt／模型行为 | Harness 结果 | 私有摘要 SHA-256 |
|---|---|---|---|
| v2 | 3 次决策：查旧库一次，再连续两次提议停止 | Goal Gate 拒绝单来源结论，预算耗尽后 `HANDOFF_HUMAN` | `245529af24f163e0926bb3ff0d8edcb7ed8093eea527cfea90cb3dbf4cf8dd14` |
| v3 | 提示词明确要求第二个独立来源；2 次决策，依次调用旧库与官方来源模拟工具 | 两组一致证据后 `VERIFIED` | `50b33144441dfcb6b22d6990f02bf904f8ce647606b407716aff75f342b203fd` |

v2/v3 的合成输入和模拟证据相同，仅 case ID、决策记录版本和提示词有差别。这是**一个开发案例的错误定位与修正**，不能写成“准确率从 0% 提升到 100%”，也不能代表真实网站、地图或官方数据库已被实际查询。v2/v3 旧摘要只保存最后一次模型响应的 token usage，不能由此计算整案成本；后续客户端已增加逐次 usage 记录。正式前后比较仍以[配对试验协议](../paired_pilot_protocol.md)和独立人工 Gold 为准。

## 1,004 条真实记录的固定顺序控制层试验

[`coordinate_gate_batch.py`](coordinate_gate_batch.py) 从改前冻结的 LAND-DATA SQLite 输出读取真实 DOHMH 候选，并校验 DOT 当前快照和抓取清单哈希；只对**名称＋地址唯一匹配、双方坐标有效**的 1,004 条执行固定顺序只读回放，每案先读冻结候选、再读 DOT，经独立 Goal Gate 按 10 米阈值判断坐标字段一致或转人工。此批运行**模型调用为 0**，所以不测动态选工具策略。

| 预注册开发组 | 旧流程状态 | Harness 字段级处置 |
|---|---:|---:|
| 两来源坐标相距 >10m | 20/20 `AUTO_APPROVED` | 20/20 `HANDOFF_HUMAN` |
| 两来源坐标相距 ≤10m | 984/984 `AUTO_APPROVED` | 984/984 `VERIFIED`（仅坐标字段一致） |

20 条差异进一步按 borough 核对：**18 条同 borough**，**2 条不同 borough**。Gate 对前 18 条给出 `CLAIM_CONFLICT`，对后 2 条给出 `IDENTITY_CONFLICT`；超过 50 米的 3 条里有 2 条不同 borough。初版回放将 20 条都暂视为同一门店并归为坐标冲突，复核后保留 v1 journal，改为 v2 的身份优先判断，避免把跨 borough 的同名同街道误说成同一地点坐标错误。

全部 1,004 案共记录 2,008 条只读工具动作，v2 journal 的 SQLite `quick_check=ok`。第二次运行不增加动作，v2 journal SHA-256 仍为 `403bc8acce3a74c296c5fdb43ae38c785e31d4cf920c4b439b41439053bf299c`。20 条冲突另生成私有逐案人工审核包，含声明、时间、证据哈希、工具动作和待答问题，SHA-256 为 `2731593e2deca69336f7e1504026062e0ca8a96e85388aa3b934e98995bc8391`，没有自动修正建议。聚合输出见 [`coordinate-gate-summary.json`](coordinate-gate-summary.json)；逐案轨迹和审核包只留在 `.local/location-verification-live-check/harness_pilot/`。

**解释边界**：这是由同一坐标阈值定义、再用该阈值路由的工程契约检查，20/20 表示已知跨来源差异被拦截，不是独立真值准确率或模型预测能力。名称＋地址匹配仍可能混淆分店；DOHMH 与 DOT 属不同数据集，不保证底层信息链真正独立。`VERIFIED` 只表示这个试验中的坐标字段相差不超过 10 米，不证明地点身份、营业状态或全部地址正确。正式前后提升仍需人工 Gold 与同条件动态策略对照。

## 同证据固定顺序／模型策略回放入口

[`src/land_data/location_verification_replay.py`](../../../src/land_data/location_verification_replay.py) 提供独立的开发期命令行入口。`synthetic-case.json` 锁定 `synthetic-tools.json` 的 SHA-256；模型只接收 Case、已返回证据和可用工具名，工具结果由注册表回放，动作写入独立 journal。入口拒绝清单篡改、未注册来源组和把非 journal 数据库当作状态库。正式盲测还需要将隐藏证据放在 Agent 进程不可读的隔离环境中；当前同进程回放**不满足**那一层隔离。

现已把相同执行器接入 LAND-DATA 主命令 `./land-data location-verify`，要求显式指定 Case、哈希锁定工具清单和独立 journal；可选 `fixed`／`qwen` 决策策略，入口不打开业务数据库，也不修改 POI。真实记录开发期烟测由 `build_real_replay_case.py` 从已冻结的 155 案证据包确定性挑选一条已知坐标差异案，生成私有 `real-conflict-case-v1.json` 与 `real-conflict-tools-v1.json`。Case SHA-256 `3c01f01a70832e3e2fd91ae398b39d2cafae88a9380de81855b3757eb0924eab`，工具清单 SHA-256 `c9eb93b8ca7c927d5e6e9565ced9b3344712906bb790223f3cf97bc52eed6493`。用主命令、`fixed` 策略运行后，两次只读工具调用得到 `HANDOFF_HUMAN / CLAIM_CONFLICT`；重复运行 journal SHA-256 保持 `98e7f4863ac1154d61ae45fc6fae2573ed03964caebf43c478cd046586ffd71e`，没有重复工具调用。该样本按已知差异选择，属于开发试跑，不是盲测或地点准确率。

真实记录主命令复现：

```sh
python3 evals/location_verification/harness_pilot/build_real_replay_case.py
PYTHONPATH=src ./land-data location-verify \
  --case .local/location-verification-live-check/harness_pilot/real-conflict-case-v1.json \
  --tool-manifest .local/location-verification-live-check/harness_pilot/real-conflict-tools-v1.json \
  --journal .local/location-verification-live-check/harness_pilot/real-conflict-cli-v1.db \
  --policy fixed
```

同一真实 Case 改用 `--policy qwen` 时，本机模型请求返回网络错误；首次实现让异常直接穿透 CLI。现已在 Harness 中将普通决策客户端异常持久化为 `HANDOFF_HUMAN / DECISION_CLIENT_ERROR`，只记录异常类型；复跑结果为 1 次失败决策、0 次工具调用、无模型用量记录。进程级中断仍可从 checkpoint 恢复。**真实记录上的动态模型决策仍未验证成功**，不得把固定顺序结果当成模型策略结果。

主命令现可加 `--handoff-output .local/location-verification-live-check/harness_pilot/real-conflict-review-packet-v1.json`，在交人工时保存 `0600` 私有审核包；同一 journal 重跑只接受字节相同的原包。真实冲突案审核包 SHA-256 `b44b5fd7378447a65e7bbbc297bbf1b65f43fbd031a5e009191a6209dc0caeaa`，包含 2 条坐标证据、2 条动作和空修改提案。模型故障案也能从已保存 checkpoint 生成无虚构证据的审核包。该接口尚未把结果写入正式 LAND-DATA 审核队列。

`./land-data location-verify-queue --journal <journal.db>` 可列出该 journal 中待人工案件的 ID、原因、决策数和工具调用数，不输出证据正文。真实固定顺序和模型故障的两个独立 journal 各列出 1 个运行；它们使用同一个地点 Case ID，不能把队列项相加当作不同地点数。审核包仍由 `--handoff-output` 提供；队列没有自动人工裁决或 POI 写入能力。

可复用的 `./land-data location-verify-prepare` 接受冻结 reviewer dossier 的路径、预期 SHA-256、案件 ID 和输出目录，逐案生成 Case 与只读工具清单；日常 dossier 还需显式 `--as-of`。它验证嵌套来源行哈希、剔除旧答案字段，并把实体身份列为必需但尚未获得独立证据的声明。当前仅把入口与当前 DOT 坐标作为可执行工具；历史 DOT 不提供当前营业／地址证明。真实困难案 `0f97e4588148c1606d832a52` 完成“准备→运行→审核包”，日常案 `screen-new-york-0094` 两来源坐标相距 0.0 米仍因身份未证实而交人工。该入口只处理已冻结的本地证据包，不是在线抓取或正式 Gold 评测。

`./land-data --db .local/land-data.db location-verify-prepare-db --candidate-id <id> --output-dir <private-dir> --as-of <UTC时间>` 可直接从现有业务候选库只读冻结一个 Case。工具名为 `read_candidate`、`read_source_metadata`，若已关联 POI 再加 `read_linked_poi`。后两者是调查上下文，不把旧自动通过状态、POI 关联或同一原始记录重复算成独立身份事实。100 条有坐标且关联 POI 的真实业务候选已完成固定顺序回放，100/100 因独立身份依据缺失交人工，0 次准备／执行异常，业务数据库文件哈希前后不变；私有报告在 `.local/location-verification-live-check/db-batch-100-v2/report.json`。这验证业务入口和保守移交，不是地点准确率或真实模型动态策略效果。候选行没有更新时间字段，历史时点比较必须改用冻结数据库副本。

`./land-data --db .local/land-data.db location-verify-queue --journal <journal.db> --with-business-candidates` 可把独立 journal 的案件 ID 只读映射到现有候选及其当前审核状态。实案 `012a438f9eae4e391e4ffb1d` 的 Harness 交人工后，业务状态仍为 `AUTO_APPROVED`；当前命令帮助人工定位案件，但**没有**把它放入正式审核状态或阻止发布。对同一 Case 的 Qwen 试跑在第一次决策时进入 `DECISION_CLIENT_ERROR`，0 次工具调用、无模型用量记录，故不能计为真实动态选工具成功。

用同一个合成 Case 分别运行固定顺序和千问策略，输入文件哈希、两次工具结果哈希均一致；两者都是 2 次决策／2 次工具调用，最后 `VERIFIED`。千问两次响应共 **512 tokens**（199＋313），固定顺序模型调用为 0。因此这个单案没有证明动态策略更准、更快或更省。私有输出 SHA-256 分别为 `44f349966d7073b29358653330357825d35fa460c6e2cf7f4cc65850a05f0f53`、`de483703d2dc33026fad19231cf472e7907695000cea269530c4ab91a5118e94`。新增的两项回放测试验证清单哈希锁与恢复幂等。

从仓库根目录复现固定顺序回放时，使用一个**新的** journal 路径；原有路径会按 checkpoint 续跑：

```sh
PYTHONPATH=src python3 -m land_data.location_verification_replay \
  --case evals/location_verification/harness_pilot/synthetic-case.json \
  --tool-manifest evals/location_verification/harness_pilot/synthetic-tools.json \
  --journal .local/location-verification-live-check/harness_pilot/replay-fixed-new.db \
  --policy fixed
```

把 `--policy fixed` 换为 `--policy qwen` 时，客户端只从本地 `.env` 读取千问配置并会产生实际模型调用；两种策略必须使用不同 journal 路径，才能保留完整的逐次决策与调用量。

明确的 `CLAIM_CONFLICT`／`IDENTITY_CONFLICT` 等冲突可用 `./land-data --db <business.db> location-verify-review-hold --case <case.json> --tool-manifest <tools.json> --journal <journal.db>` 预览；加 `--apply` 才将仍为 `AUTO_APPROVED` 的候选及关联 POI 置为 `NEEDS_REVIEW`，关闭 POI 地图可见性并追加 `VERIFY_HOLD` 审核决定。此命令拒绝 `BUDGET_EXHAUSTED` 和 `DECISION_CLIENT_ERROR`，也拒绝案件与当前候选坐标／地址不一致；后续人工批准优先于旧 journal，普通再处理不会静默取消 hold。仅在临时数据库完成行为测试，**未对真实业务库应用**；过去已生成或安装的发布包不会自动回收。
