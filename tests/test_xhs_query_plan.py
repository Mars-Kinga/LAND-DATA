import json
import tempfile
import unittest
from pathlib import Path

from land_data.cli import _xhs_query_plan


class XHSQueryPlanTests(unittest.TestCase):
    def test_groups_can_run_independently_or_as_full_coverage(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "bank.json"
            path.write_text(json.dumps({
                "city": "san-francisco",
                "query_groups": {
                    "culture": ["旧金山 文化 礼仪", "旧金山 禁忌"],
                    "food": ["旧金山 餐厅"],
                },
                "limits": {"posts_per_query": 30, "scrolls_per_query": 4,
                           "pause_seconds": 1.5},
            }), encoding="utf-8")
            culture = _xhs_query_plan(root, "bank.json", None, ["culture"],
                                      None, None, None, None)
            complete = _xhs_query_plan(root, "bank.json", None, None,
                                       None, None, None, None)
        self.assertEqual(culture["queries"], ["旧金山 文化 礼仪", "旧金山 禁忌"])
        self.assertEqual(culture["query_groups"]["旧金山 禁忌"], "culture")
        self.assertEqual(len(complete["queries"]), 3)
        self.assertEqual(complete["posts"], 30)


if __name__ == "__main__":
    unittest.main()
