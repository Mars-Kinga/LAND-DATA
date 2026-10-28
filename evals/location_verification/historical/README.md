# Historical verification replay

This directory contains an offline, read-only replay of the 51 records in:

- `docs/xhs-verification-a.json`
- `docs/xhs-verification-b.json`
- `docs/xhs-verification-c.json`
- `docs/xhs-verification-final.json`

The records were already reviewed on 2026-09-29. They are therefore a
historical reference set, not an independent gold set and not a valid accuracy
or before/after benchmark. The replay checks schema shape, record counts,
status counts, provenance-source coverage, duplicate collapsing, final-report
consistency, and stable SHA-256 fingerprints. It never accesses the network,
an LLM, or SQLite.

Run it from the repository root:

```bash
python3 evals/location_verification/historical/normalize_and_replay.py \
  --output evals/location_verification/historical/replay-report.json
```

Run the unit tests:

```bash
python3 -m unittest discover -s evals/location_verification/historical -p 'test_*.py'
```

The report's `normalized_records_sha256` and `record_sha256` values make the
input snapshot auditable. A future independent before/after evaluation must
use a separately sampled and adjudicated holdout instead of this reference set.
