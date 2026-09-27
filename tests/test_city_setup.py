import io
import json
import tempfile
import unittest
from pathlib import Path

from land_data.city_setup import ensure_city, resolve_city


class Response:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def read(self, _limit):
        return self.value


class CitySetupTests(unittest.TestCase):
    def test_resolve_city_is_one_bounded_country_filtered_request(self):
        seen = []
        payload = json.dumps([{
            "lat": "35.6762", "lon": "139.6503", "display_name": "Tokyo, Japan",
            "osm_type": "relation", "osm_id": 175805,
            "address": {"country_code": "jp"},
        }]).encode()

        def opener(request, timeout):
            seen.append((request.full_url, request.headers, timeout))
            return Response(payload)

        value = resolve_city("东京", "JP", opener=opener)
        self.assertEqual(value["country"], "JP")
        self.assertEqual(value["latitude"], 35.6762)
        self.assertEqual(len(seen), 1)
        self.assertIn("countrycodes=jp", seen[0][0])
        self.assertIn("featuretype=city", seen[0][0])
        self.assertIn("User-agent", seen[0][1])

    def test_ensure_city_creates_config_and_query_bank_once(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "config").mkdir()
            (root / "config" / "cities.json").write_text("{}\n", encoding="utf-8")
            calls = []

            def resolver(name, country):
                calls.append((name, country))
                return {"display_name": name, "country": country,
                        "latitude": 35.6, "longitude": 139.6}

            first = ensure_city(root, "tokyo", "东京", "JP", resolver=resolver)
            second = ensure_city(root, "tokyo", resolver=resolver)
            cities = json.loads((root / "config" / "cities.json").read_text())
            bank = json.loads((root / first["query_file"]).read_text())
        self.assertTrue(first["city_created"])
        self.assertTrue(first["query_file_created"])
        self.assertFalse(second["city_created"])
        self.assertFalse(second["query_file_created"])
        self.assertEqual(calls, [("东京", "JP")])
        self.assertEqual(cities["tokyo"]["country"], "JP")
        self.assertEqual(bank["city"], "tokyo")
        self.assertEqual(bank["query_groups"]["food"][0], "东京 餐厅")
        self.assertIn("东京 文化 礼仪", bank["query_groups"]["culture"])
        self.assertGreaterEqual(
            sum(len(values) for values in bank["query_groups"].values()), 40)


if __name__ == "__main__":
    unittest.main()
