import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from land_data.config import load_config
from land_data.land_raw import process_land_raw
from land_data.sqlite_storage import SQLiteStorage


class EmptyClient:
    model = 'offline-test'
    url = 'offline-test'
    def __init__(self):
        self.calls = 0
        self.last_usage = None
    def complete(self, fields):
        self.calls += 1
        self.last_usage = {'prompt_tokens': 11, 'completion_tokens': 3, 'total_tokens': 14}
        return {'locations': [], 'events': []}


class LandRawTests(unittest.TestCase):
    def test_limit_advances_past_completed_snapshot(self):
        config = load_config(str(Path(__file__).resolve().parents[1]))
        with tempfile.TemporaryDirectory() as temp:
            raw = Path(temp) / 'records'
            raw.mkdir()
            for ident in ('a', 'b'):
                (raw / (ident + '.json')).write_text(json.dumps({
                    'id': ident, 'sourcePlatform': 'xiaohongshu', 'sourceIdentity': ident,
                    'contentHash': ident, 'sourcePostID': ident,
                    'normalizedSourceURL': 'https://www.xiaohongshu.com/explore/' + ident,
                    'city': 'new-york', 'title': '信用卡支付失败', 'fullText': '该怎么解决？',
                    'tags': []}), encoding='utf-8')
            with SQLiteStorage(str(Path(temp) / 'out.db')) as storage:
                self.assertEqual(process_land_raw(storage, config, raw, limit=1)['processed'], 1)
                self.assertEqual(process_land_raw(storage, config, raw, limit=1)['processed'], 1)
                self.assertEqual(storage.connection.execute('SELECT COUNT(*) FROM land_raw_runs').fetchone()[0], 2)

    def test_resume_and_one_model_call_with_usage(self):
        root = Path(__file__).resolve().parents[1]
        config = load_config(str(root))
        with tempfile.TemporaryDirectory() as temp:
            raw = Path(temp) / 'records'
            raw.mkdir()
            for ident, title, body in [('one', '纽约咖啡馆体验', '这家新开的店怎么样？'),
                                       ('two', '信用卡支付失败', '该怎么解决？')]:
                (raw / (ident + '.json')).write_text(json.dumps({
                    'id': ident, 'sourcePlatform': 'xiaohongshu', 'sourceIdentity': ident,
                    'contentHash': ident, 'sourcePostID': ident,
                    'normalizedSourceURL': 'https://www.xiaohongshu.com/explore/' + ident,
                    'city': 'new-york', 'title': title, 'fullText': body, 'tags': []
                }), encoding='utf-8')
            client = EmptyClient()
            with SQLiteStorage(str(Path(temp) / 'out.db')) as storage:
                with patch('land_data.land_raw.qwen_from_env', return_value=client):
                    first = process_land_raw(storage, config, raw, use_llm=True)
                    second = process_land_raw(storage, config, raw, use_llm=True)
                self.assertEqual(first['model_calls'], 1)
                self.assertEqual(first['usage']['total_tokens'], 14)
                self.assertEqual(first['skip'], 1)
                self.assertEqual(second['resumed'], 2)
                self.assertEqual(client.calls, 1)
                rows = storage.connection.execute('SELECT route,status,calls,usage_json FROM land_raw_runs').fetchall()
                self.assertEqual(len(rows), 2)
                self.assertTrue(any(row['status'] == 'NEEDS_REVIEW' for row in rows))
