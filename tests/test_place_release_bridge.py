import json
import tempfile
import unittest
from pathlib import Path

from land_data.models import FieldProvenance, POI, RawSourceRecord, SourceContent
from land_data.place_release_bridge import (
    _merge_published_pack,
    _run_place_checks,
    diff_place_release,
    install_place_release,
    prepare_place_release,
    preflight_place_release,
    resolve_city_id,
    rollback_place_release,
    verify_place_release,
)
from land_data.cli import build_parser
from land_data.review import ReviewService
from land_data.sqlite_storage import SQLiteStorage
from land_data.util import stable_id, utc_now


class FailingRunner:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []
        self.kwargs = []

    def __call__(self, command, **kwargs):
        from types import SimpleNamespace
        self.calls.append(command)
        self.kwargs.append(kwargs)
        return SimpleNamespace(returncode=1 if self.fail else 0, stdout="", stderr="failed" if self.fail else "")


class PlaceReleaseBridgeTests(unittest.TestCase):
    def test_multi_city_merge_preserves_other_cities_and_unselected_places(self):
        def metadata(locale, source):
            return {"locale": locale, "isDemo": False, "updatedAt": "2026-01-01T00:00:00Z",
                    "verifiedAt": "2026-01-01T00:00:00Z",
                    "sources": [{"id": source, "url": "https://example.gov/" + source}]}

        base = {"schemaVersion": 1, "batchID": "old", "status": "published",
                "destinations": [
                    {"id": "new-york", "name": "New York", "metadata": metadata("en", "ny-old")},
                    {"id": "hanoi", "name": "Hà Nội", "metadata": metadata("en", "hn-old")},
                ],
                "guides": [{"id": "hanoi-guide", "destinationID": "hanoi",
                            "metadata": metadata("en", "guide-source")}],
                "places": [
                    {"id": "ny-existing", "destinationID": "new-york",
                     "metadata": metadata("en", "ny-place-old")},
                    {"id": "hn-place", "destinationID": "hanoi",
                     "metadata": metadata("en", "hn-place-old")},
                ]}
        fragment = {"schemaVersion": 1, "batchID": "new", "status": "published",
                    "destinations": [
                        {"id": "new-york", "name": "automatic fallback",
                         "metadata": metadata("en", "ny-new")},
                    ], "guides": [], "places": [
                        {"id": "ny-new", "destinationID": "new-york",
                         "metadata": metadata("en", "ny-place-new")},
                    ]}

        merged = _merge_published_pack(base, fragment, "new-york")
        self.assertEqual({item["id"] for item in merged["destinations"]}, {"new-york", "hanoi"})
        self.assertEqual({item["id"] for item in merged["places"]},
                         {"ny-existing", "ny-new", "hn-place"})
        self.assertEqual([item["id"] for item in merged["guides"]], ["hanoi-guide"])
        ny = next(item for item in merged["destinations"] if item["id"] == "new-york")
        self.assertEqual(ny["name"], "New York")
        self.assertEqual({item["id"] for item in ny["metadata"]["sources"]},
                         {"ny-old", "ny-new"})

    def test_app_checks_use_xcode_toolchain_and_isolated_build_paths(self):
        runner = FailingRunner(False)
        ok, records = _run_place_checks(
            Path("/tmp/LAND-test"), Path("/tmp/content-pack.json"),
            "place-release", runner=runner, run_app_checks=True)
        self.assertTrue(ok)
        self.assertEqual(len(records), 3)
        self.assertIn("--scratch-path", runner.calls[1])
        self.assertIn("-derivedDataPath", runner.calls[2])
        self.assertEqual(
            runner.kwargs[1]["env"].get("DEVELOPER_DIR"),
            "/Applications/Xcode.app/Contents/Developer")

    def test_cli_exposes_explicit_place_release_steps(self):
        parsed = build_parser().parse_args([
            "place-publish", "prepare", "--city", "new-york",
            "--poi-id", "poi-1", "--overlay", "overlay.json"])
        self.assertEqual(parsed.place_publish_command, "prepare")
        self.assertEqual(parsed.poi_id, ["poi-1"])
        parsed = build_parser().parse_args([
            "place-publish", "preflight", "--city", "New York",
            "--poi-id", "poi-1"])
        self.assertEqual(parsed.place_publish_command, "preflight")
        self.assertIsNone(parsed.overlay)

    def _storage(self, root):
        storage = SQLiteStorage(str(Path(root) / "data.sqlite"))
        now = utc_now()
        raw = RawSourceRecord("raw-1", "official-directory", "place-1", "https://example.gov/places/1",
                              "new-york", {"title": "directory", "text": "source"}, now)
        storage.put(raw)
        content = SourceContent("source-1", raw.id, "official-directory", raw.external_id,
                               raw.source_url, "DIRECTORY", "source", "source", now,
                               canonical_url=raw.source_url)
        storage.put(content)
        poi = POI("poi-1", {"en": "Museum"}, 40.7, -74.0, "new-york", "US", "museum",
                  now, now, address="1 Main St", confidence_score=0.95)
        storage.put(poi)
        storage.put(FieldProvenance("prov-1", "POI", poi.id, "names", "official-directory", now,
                                    source_content_id=content.id, verified_at=now,
                                    source_reference_url="https://example.gov/places/1"))
        ReviewService(storage, 0.8).approve(poi.id, "confirmed by reviewer", reviewer="editor")
        return storage

    @staticmethod
    def _overlay():
        return {
            "city": "new-york",
            "destination": {
                "zh-Hans": {"name": "纽约", "localName": "纽约", "summary": "纽约公开地点"},
                "en": {"name": "New York", "localName": "New York", "summary": "Published places in New York"},
            },
            "places": {"poi-1": {
                "zh-Hans": {"name": "博物馆", "localName": "Museum", "address": "1 Main St",
                            "reason": "适合第一次到访时安排。", "caution": "开放安排可能变化，请出发前查看官方页面。"},
                "en": {"name": "Museum", "localName": "Museum", "address": "1 Main St",
                       "reason": "A useful first visit stop.", "caution": "Hours can change; check the official page before going."},
            }},
        }

    def _prepare(self, storage, root):
        base = Path(root).parent / "published-content-base.json"
        if not base.exists():
            base.write_text(
                '{"schemaVersion":1,"batchID":"old","status":"published",'
                '"destinations":[],"guides":[],"places":[]}\n', encoding="utf-8")
        return prepare_place_release(storage, city="new-york", poi_ids=["poi-1"],
                                     publication_overlay=self._overlay(), releases_root=str(root),
                                     base_content_path=str(base))

    def _official_auto_storage(self, root, *, source="official-directory", conflict=False):
        """Build a complete structured source fixture without ReviewDecision rows."""
        storage = SQLiteStorage(str(Path(root) / "auto.sqlite"))
        now = utc_now()
        raw = RawSourceRecord("raw-auto", source, "place-auto", "https://example.gov/places/auto",
                              "new-york", {"title": "Auto Museum", "text": "structured record"}, now)
        storage.put(raw)
        content = SourceContent(
            "source-auto", raw.id, source, raw.external_id, raw.source_url,
            "PLACE_RECORD", "Auto Museum", "", now,
            canonical_url=raw.source_url,
            raw_metadata={"source_kind": "structured", **({"conflict_fields": ["address"]} if conflict else {})})
        storage.put(content)
        poi = POI("poi-auto", {"en": "Auto Museum"}, 40.7, -74.0, "new-york", "US", "museum",
                  now, now, address="1 Main St", confidence_score=0.95,
                  last_verified_at=now,
                  review_status="AUTO_APPROVED", coordinates_verified=True,
                  validation_passed=True, map_visibility=True)
        storage.put(poi)
        for field in ("names", "address", "latitude", "longitude", "category"):
            storage.put(FieldProvenance(
                "prov-auto-" + field, "POI", poi.id, field, source, now,
                source_content_id=content.id, verified_at=now,
                source_field=field, source_reference_url=raw.source_url))
        return storage

    def test_preflight_is_read_only_and_reports_missing_overlay_per_poi(self):
        with tempfile.TemporaryDirectory() as temp:
            releases = Path(temp) / "not-created"
            with self._storage(temp) as storage:
                before = storage.get(POI, "poi-1")
                result = preflight_place_release(
                    storage, city="new-york", poi_ids=["poi-1"],
                    publication_overlay=None, releases_root=str(releases))
                after = storage.get(POI, "poi-1")
                self.assertEqual(result["status"], "BLOCKED")
                self.assertFalse(result["items"][0]["ready"])
                codes = {item["code"] for item in result["items"][0]["blockers"]}
                self.assertIn("OVERLAY_MISSING", codes)
                self.assertTrue(result["read_only"])
                self.assertFalse(releases.exists())
                self.assertEqual(before, after)

    def test_city_display_name_maps_only_with_explicit_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            with self._storage(temp) as storage:
                poi = storage.get(POI, "poi-1")
                poi.city = "New York"
                storage.put(poi)
                profile = {"display_name": "New York", "aliases": ["NYC"]}
                self.assertEqual(resolve_city_id("NYC", {"new-york": profile})[0], "new-york")
                ready = preflight_place_release(
                    storage, city="new-york", poi_ids=["poi-1"],
                    publication_overlay=self._overlay(), city_profile=profile,
                    releases_root=str(Path(temp) / "releases"))
                self.assertEqual(ready["status"], "READY")
                poi.city = "New Jersey"
                storage.put(poi)
                blocked = preflight_place_release(
                    storage, city="new-york", poi_ids=["poi-1"],
                    publication_overlay=self._overlay(), city_profile=profile)
                self.assertIn("CITY_MISMATCH", {item["code"] for item in blocked["items"][0]["blockers"]})

    def test_preflight_requires_frozen_review_decision_even_when_field_is_auto_approved(self):
        with tempfile.TemporaryDirectory() as temp:
            with self._storage(temp) as storage:
                now = utc_now()
                poi = POI("poi-2", {"en": "Second Museum"}, 40.71, -74.01, "new-york", "US", "museum",
                          now, now, address="2 Main St", confidence_score=0.95,
                          review_status="AUTO_APPROVED", coordinates_verified=True,
                          validation_passed=True, map_visibility=True)
                storage.put(poi)
                storage.put(FieldProvenance("prov-2", "POI", poi.id, "names", "official-directory", now,
                                            source_content_id="source-1", verified_at=now,
                                            source_reference_url="https://example.gov/places/2"))
                overlay = self._overlay()
                overlay["places"]["poi-2"] = overlay["places"]["poi-1"]
                result = preflight_place_release(
                    storage, city="new-york", poi_ids=["poi-2"],
                    publication_overlay=overlay)
                self.assertIn("REVIEW_DECISION_MISSING",
                              {item["code"] for item in result["items"][0]["blockers"]})

    def test_official_structured_poi_auto_path_needs_no_decision_or_overlay(self):
        with tempfile.TemporaryDirectory() as temp:
            with self._official_auto_storage(temp) as storage:
                before = storage.get(POI, "poi-auto")
                result = prepare_place_release(
                    storage, city="new-york", poi_ids=["poi-auto"],
                    releases_root=str(Path(temp) / "releases"))
                after = storage.get(POI, "poi-auto")
                self.assertEqual(result["blockers"], [])
                self.assertEqual(result["approval_mode"], "deterministic_official")
                self.assertEqual(result["overlay_mode"], "deterministic_fallback")
                self.assertEqual(before, after)
                self.assertEqual(storage.review_history("POI", "poi-auto"), [])
                manifest = json.loads((Path(result["release_dir"]) / "manifest.json").read_text())
                self.assertEqual(manifest["approval_mode"], "deterministic_official")
                self.assertEqual(manifest["auto_approval"]["policy_version"],
                                 "deterministic_official_v1")
                self.assertTrue(manifest["overlay_metadata"]["fallback"])
                pack = json.loads((Path(result["release_dir"]) / "content-pack.json").read_text())
                places = pack["places"]
                published_ids = {stable_id("land-place", "new-york", "poi-auto")}
                auto_places = [item for item in places if item["id"] in published_ids]
                self.assertEqual(len(auto_places), 2)
                self.assertEqual(places[0]["address"], "1 Main St")
                self.assertNotIn("grade", places[0]["reason"].lower())
                self.assertIn("verified", places[0]["reason"].lower())
                checked = verify_place_release(result["release_dir"], storage=storage)
                self.assertTrue(checked["ok"])

    def test_community_structured_source_cannot_use_auto_path(self):
        with tempfile.TemporaryDirectory() as temp:
            with self._official_auto_storage(temp, source="osm") as storage:
                result = preflight_place_release(
                    storage, city="new-york", poi_ids=["poi-auto"],
                    releases_root=str(Path(temp) / "releases"))
                codes = {item["code"] for item in result["items"][0]["blockers"]}
                self.assertIn("OVERLAY_MISSING", codes)
                self.assertIn("REVIEW_DECISION_MISSING", codes)
                self.assertEqual(result["approval_mode"], "human_reviewed")

    def test_source_conflict_cannot_use_auto_path(self):
        with tempfile.TemporaryDirectory() as temp:
            with self._official_auto_storage(temp, conflict=True) as storage:
                result = preflight_place_release(
                    storage, city="new-york", poi_ids=["poi-auto"],
                    releases_root=str(Path(temp) / "releases"))
                codes = {item["code"] for item in result["items"][0]["blockers"]}
                evidence = result["items"][0]["approval_evidence"]
                self.assertIn("SOURCE_CONFLICT_MARKER", evidence["reasons"])
                self.assertIn("REVIEW_DECISION_MISSING", codes)

    def test_success_idempotency_and_bilingual_pack(self):
        with tempfile.TemporaryDirectory() as temp:
            with self._storage(temp) as storage:
                first = self._prepare(storage, Path(temp) / "releases")
                second = self._prepare(storage, Path(temp) / "releases")
                self.assertEqual(first["release_id"], second["release_id"])
                self.assertTrue(second["idempotent"])
                pack = json.loads((Path(first["release_dir"]) / "content-pack.json").read_text())
                self.assertEqual(pack["status"], "published")
                self.assertEqual({item["locale"] for item in [p["metadata"] for p in pack["places"]]}, {"en", "zh-Hans"})
                checked = verify_place_release(first["release_dir"], storage=storage,
                                               reviewer="editor", reason="reviewed bilingual package")
                self.assertTrue(checked["ok"])

    def test_missing_overlay_source_unknown_category_and_unsafe_status_block(self):
        with tempfile.TemporaryDirectory() as temp:
            with self._storage(temp) as storage:
                bad_overlay = self._overlay()
                bad_overlay["places"]["poi-1"]["en"]["reason"] = "Grade A hygiene"
                result = self._prepare(storage, Path(temp) / "health")
                # Replace the input with the health-grade overlay for the actual assertion.
                result = prepare_place_release(storage, city="new-york", poi_ids=["poi-1"],
                                               publication_overlay=bad_overlay,
                                               releases_root=str(Path(temp) / "health"))
                self.assertIn("HEALTH_GRADE_IN_PUBLICATION_COPY", {b["code"] for b in result["blockers"]})
                poi = storage.get(POI, "poi-1")
                poi.category = "bar"
                storage.put(poi)
                result = self._prepare(storage, Path(temp) / "unknown")
                self.assertIn("UNKNOWN_CATEGORY", {b["code"] for b in result["blockers"]})
                poi.category = "museum"
                poi.active = False
                storage.put(poi)
                result = self._prepare(storage, Path(temp) / "unsafe")
                self.assertIn("UNSAFE_POI_STATUS", {b["code"] for b in result["blockers"]})

                missing = self._overlay()
                missing["places"]["poi-1"]["en"].pop("address")
                result = prepare_place_release(storage, city="new-york", poi_ids=["poi-1"],
                                               publication_overlay=missing,
                                               releases_root=str(Path(temp) / "missing-copy"))
                self.assertIn("PLACE_FIELD_MISSING", {b["code"] for b in result["blockers"]})

                # A POI with no provenance URL cannot be turned into a
                # published source reference even if the POI itself is valid.
                poi = storage.get(POI, "poi-1")
                poi.active = True
                poi.website = ""
                storage.connection.execute("DELETE FROM field_provenance WHERE entity_id=?", (poi.id,))
                storage.connection.commit()
                result = self._prepare(storage, Path(temp) / "missing-source")
                self.assertIn("PROVENANCE_MISSING", {b["code"] for b in result["blockers"]})
                self.assertIn("STABLE_HTTPS_SOURCE_MISSING", {b["code"] for b in result["blockers"]})

    def test_tamper_and_poi_drift_block_verify(self):
        with tempfile.TemporaryDirectory() as temp:
            with self._storage(Path(temp) / "first") as storage:
                result = self._prepare(storage, Path(temp) / "releases")
                pack_path = Path(result["release_dir"]) / "content-pack.json"
                pack_path.write_text(pack_path.read_text() + "\n", encoding="utf-8")
                checked = verify_place_release(result["release_dir"], storage=storage,
                                               reviewer="editor", reason="review")
                self.assertFalse(checked["ok"])
                self.assertIn("ContentPack hash does not match manifest", checked["errors"])
            with self._storage(Path(temp) / "pack-tamper") as storage:
                result = self._prepare(storage, Path(temp) / "pack-tamper-release")
                release_dir = Path(result["release_dir"])
                pack_path = release_dir / "content-pack.json"
                pack = json.loads(pack_path.read_text())
                pack["places"][0]["reason"] = "tampered"
                pack_path.write_text(json.dumps(pack, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
                manifest_path = release_dir / "manifest.json"
                manifest = json.loads(manifest_path.read_text())
                import hashlib
                manifest["files"]["content-pack.json"]["sha256"] = hashlib.sha256(pack_path.read_bytes()).hexdigest()
                body = {key: value for key, value in manifest.items() if key != "manifest_integrity"}
                manifest["manifest_integrity"] = hashlib.sha256(
                    json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8")
                checked = verify_place_release(result["release_dir"], storage=storage,
                                               reviewer="editor", reason="review")
                self.assertFalse(checked["ok"])
                self.assertIn("CONTENT_PACK_DRIFT", checked["errors"])
            # Recreate an untouched release to test source/POI drift.
            with self._storage(Path(temp) / "second") as storage:
                result = self._prepare(storage, Path(temp) / "drift")
                poi = storage.get(POI, "poi-1")
                poi.address = "changed"
                storage.put(poi)
                checked = verify_place_release(result["release_dir"], storage=storage,
                                               reviewer="editor", reason="review")
                self.assertFalse(checked["ok"])
                self.assertIn("INPUT_DRIFT", checked["errors"])

    def test_install_failure_rolls_back_and_success_can_rollback(self):
        with tempfile.TemporaryDirectory() as temp:
            land = Path(temp) / "LAND"
            target = land / "LAND" / "Resources" / "published-content.json"
            target.parent.mkdir(parents=True)
            old = b'{"schemaVersion":1,"batchID":"old","status":"published","destinations":[],"guides":[],"places":[]}\n'
            target.write_bytes(old)
            with self._storage(temp) as storage:
                result = self._prepare(storage, Path(temp) / "releases")
                # The test release was prepared against the real checkout base,
                # so let the temporary install use an absent base by updating its
                # manifest through a fresh prepared fixture is unnecessary here:
                # install accepts the target when its bytes are the release base.
                manifest_path = Path(result["release_dir"]) / "manifest.json"
                manifest = json.loads(manifest_path.read_text())
                manifest["base_resource"]["sha256"] = __import__("hashlib").sha256(old).hexdigest()
                body = {k: v for k, v in manifest.items() if k != "manifest_integrity"}
                manifest["manifest_integrity"] = __import__("hashlib").sha256(
                    json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8")
                verify_place_release(result["release_dir"], storage=storage, reviewer="editor", reason="review")
                failed = install_place_release(result["release_dir"], str(land), runner=FailingRunner(True))
                self.assertEqual(failed["status"], "INSTALL_FAILED_ROLLED_BACK")
                self.assertEqual(target.read_bytes(), old)
                installed = install_place_release(result["release_dir"], str(land), runner=FailingRunner(False))
                self.assertEqual(installed["status"], "INSTALLED")
                rolled = rollback_place_release(result["release_id"], str(land), reason="restore", runner=FailingRunner(False))
                self.assertEqual(rolled["status"], "ROLLED_BACK")
                self.assertEqual(target.read_bytes(), old)


if __name__ == "__main__":
    unittest.main()
