# NYC DOT 跨来源比较器

`dot_cross_source_check.py` 是一个只读离线比较器，用已冻结的 1,000 条筛查队列和两份已经保存的 NYC DOT JSON 快照做字段级诊断。它不会访问网络、模型或数据库，也不会把匹配条件本身包装成地点准确率。

当前 DOT 快照（数据集 `fpeh-f7ci`）没有 CAMIS。脚本只在候选名称和完整地址都存在、且规范化后的 `(name, address)` 在 DOT 行中恰好对应一行时建立保守匹配；这个数字是跨来源覆盖/检索结果。匹配后只报告坐标距离和 DOT 状态字段是否存在，不能把它解释为人工 Gold 正确率。名称/地址规范化只做 NFKC、大小写、标点、空白、常见街道缩写和数字序数后缀处理，不做模糊匹配或地理编码。

历史 DOT 快照（数据集 `pitm-atqc`）用队列中 `platform=nyc-open-data-restaurants` 的 `source.external_id` 连接 `food_service_establishment`/CAMIS。一个 CAMIS 的多条申请全部保留；只有每个重复行都具有同一个非空字段时，该字段才进入对应比较分母，冲突或缺失会进入私有诊断但不会被静默选一行。历史地址和坐标结果只代表历史字段一致性，不能证明地点当前营业。

默认命令（两份快照和抓取清单由父任务保存后运行）：

```sh
python3 evals/location_verification/dot_cross_source/dot_cross_source_check.py \
  --run-id 20261028T000000Z
```

输入默认位置：

```text
.local/location-verification-live-check/dot_batches/current.json
.local/location-verification-live-check/dot_batches/historical.json
.local/location-verification-live-check/dot_batches/capture-manifest.json
.local/baselines/location-verification-pre-agent-final-2026-10-28/screening/location-verification-screening-2026-10-28.jsonl
```

脚本在比较前校验：冻结队列和三个输入文件的字节哈希、抓取清单中的数据集行数与两份 JSON 哈希是否一致；输入在读取前后变化会终止运行。私有逐案结果、运行清单和原始输入清单的摘要写入 `.local/location-verification-live-check/dot_cross_source_results/`，目录权限为 `0700`，逐案和清单文件为 `0600`。公开 JSON/Markdown 只写计数、明确分母、筛选规则、声明边界和哈希。

## 2026-10-28 实际运行

本次运行使用 `--run-id 20261028T000000Z`，输入哈希已由 `capture-manifest.json` 校验：当前快照 2,516 行，SHA-256 `083f896643e6623f2ebf02f2505010b73ae9f5553416ed537f2fc2fb0136e12c`；历史快照 14,428 行，SHA-256 `f45c8fe037a737c1e42e8749da2036da156f282d6f9ab7d393f85ffc03aa883d`。抓取清单 SHA-256 为 `5cae01939ee4bf76535b5aa2d6e3544368f0d8d34b12302237778f6f6d2de5c6`；脚本 SHA-256 为 `2823775092cdefcfe3612c21de9e98b91c431df19faefed56f06a16903490b4c`。

当前快照的 800 个纽约队列行中，790 个具备名称和地址，至少匹配一条 DOT 许可行为 73/790，其中 56 个唯一、17 个对应多条许可行；多许可行没有被自动选一行。56 个唯一匹配中，坐标可比较覆盖为 54/56；可比较者坐标 ≤10m 为 54/54，≤50m 也是 54/54；另 2 个候选的坐标缺失或无效。DOT 状态字段存在 56/56。这些都是覆盖和字段级一致性分母，不是地点准确率。

历史快照对 790 个 CAMIS 候选连接到 198 个 CAMIS（141 个单行、57 个重复 CAMIS 组）。地址字段可比较时相同为 114/174；坐标 ≤10m 为 162/168，≤50m 为 165/168。重复组的冲突/缺失保留在私有逐案结果中；历史快照不能证明当前营业。

公开结果：`dot-cross-source-summary.json`（SHA-256 `69b8e2adc1a8d80d369e97ec1a2554a7d2f2550d49fb4ff8651b3703c5e8f76b`）和 `dot-cross-source-summary.md`。私有逐案结果 SHA-256 为 `f4f8e03f46f8b710708273e3f0b819d1eb4bcdbb35df35e40349fafa3a0f10f3`，私有运行清单 SHA-256 为 `3486bf23e833d695c6101b78ec905ff9a0fd26f547af4702f7bee910b58eb757`。运行脚本、输入、过滤器和分母均可通过清单复现。

## 最小回归测试

```sh
python3 -m unittest discover \
  -s evals/location_verification/dot_cross_source \
  -p 'test_*.py' -v
```

测试覆盖规范化、当前 DOT 名称＋地址重复键、历史 CAMIS 重复冲突保留，以及抓取清单哈希/行数校验。它们不替代正式的独立 Gold。
