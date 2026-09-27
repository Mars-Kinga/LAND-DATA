# Classifier regression fixture

`classifier_regression.jsonl` 是本地 classifier 的冻结回归/评测 fixture。它由人工编写的可核对合成样本组成，目的是固定分类器的路由契约、覆盖边界和回归行为。

这些样本不是线上抓取结果，也不是新的 unseen evaluation set；不能用它们宣称真实平台上的 precision、recall 或城市覆盖率。真实 unseen evaluation 需要另行抽样、冻结并记录采集来源和人工金标准。

每行一个 JSON 对象，字段含义如下：

- `id`：唯一稳定样本 ID。
- `title`、`text`：供 classifier 读取的标题和正文；允许标题或正文为空，但不能同时为空以外的字段缺失契约仍需保持。
- `city`：样本所属城市 slug。
- `source`：来源类型标签，例如 `xiaohongshu`、`threads`、`official-web` 或 `manual-import`。
- `content_type`：默认 `POST`，显式活动使用 `EVENT`。
- `geo_tag`：可选的结构化输入；`level=POI` 代表已给定 POI 标签。
- `payload`：可选的路由输入，例如显式活动或已知 POI 命中提示。
- `gold_primary_label`：人工期望的主分类。
- `gold_route`：人工期望的下一步路由；当前 fixture 中与主分类一致，单独保留以固定公共契约。
- `expected_model_recommended`：是否应该建议后续使用模型/人工补充地点身份。`true` 不表示自动调用模型。
- `notes`：人工核对说明和覆盖意图。

当前允许的主分类/路由值为：

`structured_poi`、`known_poi`、`knowledge`、`event`、`mixed`、`unresolved_place`、`ambiguous`、`irrelevant`。

运行数据契约测试：

```sh
PYTHONPATH=src python3 -m unittest tests.test_classifier_regression_fixture -q
```

新增或修改样本时必须保持 `id` 不重复，并更新 `notes`；若分类语义改变，应提升 classifier 版本并明确记录 fixture 变化原因。
