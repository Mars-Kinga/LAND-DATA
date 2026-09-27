"""Resumeable, read-only bridge from LAND collector snapshots to LAND-DATA."""
import json
import time
from pathlib import Path

from .llm_extraction import LLMExtractionService, qwen_from_env
from .local_classifier import LocalClassifier, persist_classification
from .models import RawSourceRecord
from .pipeline import Pipeline
from .processing import normalize
from .util import safe_source_url, stable_id, utc_now

ROUTER_VERSION = 'land-raw-v7-local-classifier-v4'


def route(content, payload, storage):
    decision = LocalClassifier(storage).classify(content, payload)
    reason = decision.reasons[0] if decision.reasons else 'classifier_decision'
    if decision.primary_label in ('structured_poi', 'known_poi'):
        return decision.primary_label, reason
    if decision.primary_label in ('irrelevant', 'knowledge'):
        return 'skip', reason
    return 'model', reason


def process_land_raw(storage, config, raw_dir, *, use_llm=False, limit=None, retry_errors=False,
                     allow_historical=False):
    root = Path(raw_dir).resolve()
    if not root.is_dir():
        raise ValueError('LAND raw directory does not exist')
    for parent in (root,) + tuple(root.parents):
        manifest = parent / 'history-manifest.json'
        if manifest.is_file():
            value = json.loads(manifest.read_text(encoding='utf-8'))
            if value.get('classification') == 'HISTORICAL_DISCOVERY_INPUT' and not allow_historical:
                raise ValueError('historical LAND Raw is excluded from new collector metrics; use --allow-historical only for archival analysis')
    policy = config.sources['xiaohongshu-land-raw'].policy
    service = LLMExtractionService(qwen_from_env(config.root), max_attempts=1) if use_llm else None
    pipeline = Pipeline(storage, config)
    db = storage.connection
    db.execute('''CREATE TABLE IF NOT EXISTS land_raw_runs (
        snapshot_id TEXT PRIMARY KEY, source_identity TEXT NOT NULL, content_hash TEXT NOT NULL,
        router_version TEXT NOT NULL, route TEXT NOT NULL, reason TEXT NOT NULL,
        status TEXT NOT NULL, calls INTEGER NOT NULL, latency_ms INTEGER NOT NULL,
        cache_hit INTEGER NOT NULL, usage_json TEXT, error TEXT, processed_at TEXT NOT NULL,
        model_latency_ms INTEGER)''')
    if 'model_latency_ms' not in {row[1] for row in db.execute('PRAGMA table_info(land_raw_runs)')}:
        db.execute('ALTER TABLE land_raw_runs ADD COLUMN model_latency_ms INTEGER')
    db.execute('CREATE INDEX IF NOT EXISTS idx_land_raw_identity ON land_raw_runs(source_identity)')
    db.commit()
    files = sorted(root.rglob('*.json'))
    totals = {'files': len(files), 'processed': 0, 'resumed': 0, 'duplicate': 0, 'skip': 0,
              'structured_poi': 0, 'known_poi': 0, 'model': 0, 'pending_model': 0, 'errors': 0,
              'model_calls': 0, 'cache_hits': 0, 'usage': {}}
    for path in files:
        if limit and totals['processed'] >= limit:
            break
        sid = None
        source = {}
        try:
            source = json.loads(path.read_text(encoding='utf-8'))
            if source.get('sourcePlatform') != 'xiaohongshu' or not source.get('id'):
                raise ValueError('invalid LAND Raw snapshot')
            sid = source['id']
            existing = db.execute('SELECT status, router_version FROM land_raw_runs WHERE snapshot_id=?', (sid,)).fetchone()
            if existing and existing['router_version'] == ROUTER_VERSION and not ((existing['status'] == 'ERROR' and retry_errors) or
                                 (existing['status'] == 'PENDING_MODEL' and use_llm)):
                totals['resumed'] += 1
                continue
            identity = source['sourceIdentity']
            duplicate = db.execute('SELECT snapshot_id FROM land_raw_runs WHERE source_identity=? AND content_hash=? AND status != ? LIMIT 1',
                                   (identity, source['contentHash'], 'ERROR')).fetchone()
            tag = source.get('rawMetadata', {}).get('geo_tag')
            if not isinstance(tag, dict):
                tag = None
            tags = [x.get('text', '') if isinstance(x, dict) else x for x in source.get('tags', [])]
            payload = {'content_type': 'POST', 'title': source.get('title', ''),
                       'text': source.get('fullText', ''), 'hashtags': tags,
                       'geo_tag': tag, 'content_status': 'ACTIVE', 'match_known_pois': True}
            payload = policy.retained_payload(payload)
            record = RawSourceRecord(id=stable_id('land-raw', sid), source='xiaohongshu-land-raw',
                                     external_id=str(source.get('sourcePostID') or identity),
                                     source_url=safe_source_url(source.get('normalizedSourceURL') or source.get('sourceURL')),
                                     city=source.get('city', 'new-york'), payload=payload,
                                     collected_at=utc_now(), expires_at=policy.expiry(utc_now()))
            content = normalize(record, policy)
            route_name, reason = route(content, payload, storage)
            if duplicate:
                route_name, reason = 'duplicate', 'same_identity_and_content_hash'
            before_calls = service.calls if service else 0
            before_hits = service.cache_hits if service else 0
            started = time.monotonic()
            status, error = 'DONE', None
            usage = None
            if route_name != 'duplicate':
                storage.put(record)
                storage.put(content)
                decision = LocalClassifier(storage).classify(content, payload)
                persist_classification(storage, content, decision, payload)
            if route_name == 'model' and not use_llm:
                status = 'PENDING_MODEL'
                totals['pending_model'] += 1
            elif route_name in ('model', 'structured_poi', 'known_poi'):
                pipeline.llm_service = service if route_name == 'model' else None
                pipeline._parse_one(record, policy)
                if route_name == 'model' and service and record.external_id in service.errors:
                    status = 'NEEDS_REVIEW'
                    error = service.errors[record.external_id]
                elif route_name == 'model':
                    status = 'NEEDS_REVIEW'
                if service:
                    usage = service.usage_by_content.get(record.external_id)
            if route_name == 'duplicate':
                totals['duplicate'] += 1
            else:
                totals[route_name] += 1
            calls = (service.calls - before_calls) if service else 0
            hits = (service.cache_hits - before_hits) if service else 0
            totals['model_calls'] += calls
            totals['cache_hits'] += hits
            if usage:
                for key, value in usage.items():
                    if isinstance(value, int):
                        totals['usage'][key] = totals['usage'].get(key, 0) + value
            model_latency = service.latencies_by_content.get(record.external_id) if service else None
            db.execute('''INSERT OR REPLACE INTO land_raw_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                       (sid, identity, source['contentHash'], ROUTER_VERSION, route_name, reason,
                        status, calls, round((time.monotonic()-started)*1000), hits,
                        json.dumps(usage) if usage else None, error, utc_now(),
                        round(model_latency*1000) if model_latency is not None else None))
            db.commit()
            totals['processed'] += 1
        except Exception as exc:
            totals['errors'] += 1
            if sid:
                db.execute('INSERT OR REPLACE INTO land_raw_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (sid, source.get('sourceIdentity', ''), source.get('contentHash', ''), ROUTER_VERSION,
                            'error', 'processing_error', 'ERROR', 0, 0, 0, None,
                            type(exc).__name__ + ': ' + str(exc)[:180], utc_now(), None))
                db.commit()
    pipeline.validate()
    eligible = max(1, totals['processed']-totals['duplicate'])
    totals['planned_model_ratio'] = round(totals['model'] / eligible, 4)
    totals['actual_api_call_ratio'] = round(totals['model_calls'] / eligible, 4)
    return totals
