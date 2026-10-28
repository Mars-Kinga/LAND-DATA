# Location Verification Harness fault cases

这是 Location Verification Agent 的预注册合成对抗评测资产。它只描述
`input`、模拟 `tool_events`、期望处置和安全不变量，不调用 Agent、LLM 或
网络，也不代表真实地点或真实用户内容。

冻结文件是 [`fault_cases.jsonl`](fault_cases.jsonl)，由
[`generate_fault_cases.py`](generate_fault_cases.py) 生成并校验。生成器的
输入全部是仓库内固定模板，来源 URL 使用 `synthetic.invalid` 保留域名。

## 冻结规模

- 记录数：120（12 类 × 每类 10 个参数化案例）
- 参数化语义唯一数（只去除身份字段后）：120
- 每个故障族的实际结构变体数：3（共 36 个结构组合）
- 工具事件：110 条记录含 3 个事件，10 条恢复场景含 4 个事件
- JSONL SHA-256：`e7d58c9437fc244e9858b660f4a72118fc4c1e79ef112df6175bc8898b466ebc`

| 故障族 | 参数化案例 | 结构变体 | 结构变体示例 | 主要安全不变量 |
| --- | ---: | ---: | --- | --- |
| `repost_non_independent` | 10 | 3 | same-text / canonical-URL / quote-chain | `INDEPENDENCE_REQUIRED`, `NO_DOUBLE_COUNT` |
| `branch_confusion` | 10 | 3 | address / coordinate / category split | `BRANCH_IDENTITY_MATCH`, `NO_CROSS_BRANCH_MERGE` |
| `expired_page` | 10 | 3 | stale-404 / old-200 / missing timestamp | `FRESHNESS_REQUIRED`, `NO_STALE_AUTO_APPROVAL` |
| `malicious_web_instruction` | 10 | 3 | prompt text / exfiltration link / tool-call text | `UNTRUSTED_CONTENT_NOT_INSTRUCTIONS`, `NO_SIDE_EFFECT_FROM_WEB_TEXT` |
| `tool_timeout_or_error` | 10 | 3 | timeout / malformed-or-rate-limited / circuit breaker | `BOUNDED_RETRIES`, `NO_ERROR_AS_EVIDENCE` |
| `permission_denied` | 10 | 3 | forbidden / expired session / consent required | `NO_AUTH_BYPASS`, `PERMISSION_BOUNDARY_PRESERVED` |
| `budget_exhausted` | 10 | 3 | call / token / deadline budget | `BUDGET_HARD_STOP`, `NO_POST_BUDGET_CALL` |
| `interrupted_recovery` | 10 | 3 | durable checkpoint / before checkpoint / duplicate resume | `CHECKPOINT_IS_DURABLE`, `RESUME_IS_IDEMPOTENT` |
| `premature_stop` | 10 | 3 | address+status / status / identity gap | `GOAL_GATE_MUST_HOLD`, `NO_PREMATURE_STOP` |
| `conflicting_evidence` | 10 | 3 | address+status / hours / temporal conflict | `CONFLICT_CANNOT_AUTO_RESOLVE`, `NO_SILENT_OVERWRITE` |
| `identity_collision` | 10 | 3 | same-name category / alias / nearby coordinates | `NAME_ONLY_MATCH_INSUFFICIENT`, `NO_GUESSING_IDENTITY` |
| `evidence_gap` | 10 | 3 | status / address / provenance gap | `CLAIM_NEEDS_SUPPORT`, `NO_INVENTED_FACTS` |

每条记录至少包含：

- `input`：地点声明、城市、分店、地址、坐标和合成来源标记；
- `tool_events`：按顺序排列的工具请求和返回状态，包括超时、拒绝、冲突、
  检查点和恢复事件；
- `expected`：终止状态、下一动作、停止原因、人工审核要求和禁止动作；
- `required_evidence`：允许结束或恢复前仍需取得的证据；
- `safety_invariants`：可由 Harness 断言的安全不变量。

`terminal_state` 是评测预期，不是已经运行出的 Agent 结果。当前资产只用于
在实现 Agent Harness 后进行离线回放和回归；不能据此宣称真实地点准确率、
自动化覆盖率或改造后的提升。

## 重新生成和校验

在本目录执行：

```sh
PYTHONDONTWRITEBYTECODE=1 python3 generate_fault_cases.py generate
PYTHONDONTWRITEBYTECODE=1 python3 generate_fault_cases.py check
```

`check` 会验证至少 100 条记录、12 个故障族覆盖、字段完整性、工具事件结构、
合成来源标记，以及去除身份字段后的参数化去重。它还会递归去除地点、城市、
地址、claim、时间、URL、ID/hash 等表面字段，按 family 计算结构指纹，并强制
每个 family 至少有 3 种结构变体。若 JSONL 内容变化，应把新的 SHA-256、族分布
和结构变体计数一起记录在评测报告中。
