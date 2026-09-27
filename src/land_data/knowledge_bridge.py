"""Compile editorially accepted XHS knowledge drafts into a LAND QAU sidecar.

This module is deliberately a one-way bridge.  It never changes extraction or
editorial records, and it never writes into the LAND application.  The output
is a small, self-contained ``QuestionContentIndex`` compatible package which
can later be merged or installed by a separate publishing step.
"""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import secrets
from urllib.parse import urlsplit

from .knowledge_processing import ensure_knowledge_schema
from .util import normalize_url, stable_id, utc_now
from .xhs_processing import ensure_xhs_schema


ACCEPTED_STATUS = "EDITORIAL_ACCEPTED_FOR_BRIDGE"
PREPARED = "PREPARED"
VERIFIED = "VERIFIED"
SCHEMA_VERSION = 1
GENERATOR_VERSION = "knowledge-bridge-v1"

# These are intentionally narrow.  A city can be added only when the product
# has an explicit QAU surface for it; transport, airports, routes and country
# rules need product mapping rather than a best-effort guess here.
TOPIC_MAP = {
    ("money", "payment"): ("money", "explanation"),
    ("money", "money"): ("money", "explanation"),
    ("food", "food"): ("food", "explanation"),
    ("food", "dining"): ("food", "etiquette"),
    ("food", "restaurant"): ("food", "interaction"),
    ("stay", "stay"): ("stay", "explanation"),
    ("stay", "hotel"): ("stay", "operation"),
    ("stay", "accommodation"): ("stay", "operation"),
    ("sightseeing", "attraction"): ("attractions", "operation"),
    ("sightseeing", "sightseeing"): ("attractions", "operation"),
    ("sightseeing", "activity"): ("attractions", "operation"),
    ("culture", "culture"): ("culture", "etiquette"),
    ("culture", "etiquette"): ("culture", "etiquette"),
}
KNOWN_OPERATOR_HOSTS = {
    "jfkairport.com", "mta.info", "cityexperiences.com", "lirr.com",
    "amtrak.com", "nps.gov",
}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value):
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        raw = value.encode("utf-8")
    else:
        raw = _canonical(value).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _read_json(path):
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp-" + secrets.token_hex(6))
    temporary.write_text(_canonical(value) + "\n", encoding="utf-8")
    os.replace(str(temporary), str(path))


def _as_dict(value):
    try:
        return json.loads(value) if isinstance(value, str) else dict(value)
    except (TypeError, ValueError):
        return {}


def _iso(value):
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _source_kind(url):
    """Return a source kind only when the review provides enough provenance."""
    host = urlsplit(url).hostname or ""
    host = host.lower()
    if host.endswith(".gov") or ".gov." in host:
        return "official"
    if any(host == known or host.endswith("." + known) for known in KNOWN_OPERATOR_HOSTS):
        return "operator"
    return None


def _current_input_hash(row, content):
    """Match the immutable input envelope used by knowledge_processing."""
    title = content.get("title", "")
    text = content.get("text", "")
    hashtags = content.get("hashtags", [])
    if not isinstance(title, str) or not isinstance(text, str):
        return None
    if not isinstance(hashtags, list) or not all(isinstance(x, str) for x in hashtags):
        hashtags = []
    candidate = {key: row[key] for key in
                 ("source_content_id", "city", "scenario", "query", "reason")}
    envelope = {"candidate": candidate,
                "visible": {"title": title[:500], "text": text[:10000], "hashtags": hashtags[:30]}}
    return _sha(envelope)


def _blocker(draft_id, code, message, **extra):
    item = {"draft_id": draft_id, "code": code, "message": message}
    item.update(extra)
    return item


def _country_for(city, city_countries):
    if not isinstance(city_countries, dict) or not isinstance(city_countries.get(city), str):
        return None
    return city_countries[city]


def _review_row(connection, draft_id):
    return connection.execute("""
      SELECT id,draft_id,previous_status,new_status,action,reviewer,reason,
             reference_url,verified_at,created_at,data
      FROM xhs_knowledge_review_log WHERE draft_id=?
      ORDER BY created_at DESC,id DESC LIMIT 1
    """, (draft_id,)).fetchone()


def _source_detail(question_key, content, role):
    # The LAND contract intentionally has a separate anonymous editorial
    # experience kind.  Do not copy the post URL, title, author, external ID,
    # or collection metadata into the public QAU package.  The immutable
    # source_content_id/input/review hashes remain in manifest.inputs.
    if role != "experience":
        return None, "INVALID_EDITORIAL_DETAIL_ROLE"
    return {
        "detailID": "editorial-experience-" + question_key,
        "kind": "editorial_experience", "title": "匿名旅行者经验", "url": None,
        "author": "LAND 编辑整理", "platform": "LAND",
        "role": role, "publishedAt": None,
        "collectedAt": None,
    }, None


def _validate_evidence(draft, content):
    evidence = draft.get("evidence")
    sources = {"title": content.get("title", ""), "text": content.get("text", ""),
               "hashtag": "\n".join(content.get("hashtags", []) if isinstance(content.get("hashtags"), list) else [])}
    if not isinstance(evidence, list) or not evidence:
        return "EVIDENCE_MISSING"
    for item in evidence:
        if not isinstance(item, dict) or item.get("field") not in sources:
            return "EVIDENCE_INVALID"
        quote = item.get("quote")
        if not isinstance(quote, str) or not quote.strip() or quote not in sources[item["field"]]:
            return "EVIDENCE_STALE_OR_MISSING"
    return None


def _derived_output_text(draft):
    """Return only fields that can reach a LAND answer/reference package."""
    return _canonical({
        "question": draft.get("question"),
        "direct_answer": draft.get("direct_answer"),
        "actions": draft.get("actions", []),
        "success_signals": draft.get("success_signals", []),
        "what_ifs": draft.get("what_ifs", []),
        "applicability": draft.get("applicability", {}),
        "risk": draft.get("risk", []),
    })


def _source_leak_error(draft, row, content):
    """Reject verbatim source identifiers/body material in derived answers.

    A reviewed derivative may paraphrase the source, but the bridge must never
    turn a copied post body, title, author, URL, evidence quote, or external
    identifier into public LAND content.  Provenance hashes stay in the
    private manifest instead.
    """
    raw_record = _as_dict(row.get("raw_data"))
    raw_values = [
        (content.get("title"), 3), (content.get("text"), 3),
        (content.get("author_identifier"), 3),
        (content.get("canonical_url"), 3), (content.get("source_url"), 3),
        (row.get("source_url"), 3), (row.get("external_id"), 3),
        (raw_record.get("source_url"), 3), (raw_record.get("external_id"), 3),
    ]
    raw_values.extend(
        (item.get("quote"), 8) for item in draft.get("evidence", [])
        if isinstance(item, dict))
    output = _derived_output_text(draft)
    for value, minimum_length in raw_values:
        if not isinstance(value, str):
            continue
        value = value.strip()
        if len(value) >= minimum_length and value in output:
            return "SOURCE_CONTENT_LEAK"
    return None


def _needs_verification(draft):
    if draft.get("verification_required") is True or draft.get("facts_requiring_verification"):
        return True
    if any(isinstance(item, dict) and item.get("kind") == "RULE_OR_DYNAMIC_FACT"
           for item in draft.get("evidence", [])):
        return True
    for key in ("direct_answer",):
        item = draft.get(key)
        if isinstance(item, dict) and item.get("basis") == "NEEDS_OFFICIAL_VERIFICATION":
            return True
    for key in ("actions", "success_signals"):
        if any(isinstance(item, dict) and item.get("basis") == "NEEDS_OFFICIAL_VERIFICATION"
               for item in draft.get(key, [])):
            return True
    return any(isinstance(item, dict) and item.get("basis") == "NEEDS_OFFICIAL_VERIFICATION"
               for item in draft.get("what_ifs", []))


def _policy_value(source_policies, source, name, default=False):
    """Read an explicit publishing policy from Config sources or a small dict."""
    policy = source_policies.get(source) if isinstance(source_policies, dict) else None
    if isinstance(policy, bool):
        return policy if name == "export_enabled" else default
    if isinstance(policy, dict):
        return policy.get(name, default) is True
    config_policy = getattr(policy, "policy", policy)
    return getattr(config_policy, name, default) is True


def _compile_one(row, content, country, source_policies):
    """Return (question, reference, frozen_input) or a machine-readable blocker."""
    draft_id = row["id"]
    envelope = _as_dict(row["data"])
    draft = envelope.get("qau_draft")
    if not isinstance(draft, dict):
        return None, _blocker(draft_id, "DRAFT_PAYLOAD_INVALID", "qau_draft is missing")
    if _current_input_hash(row, content) != row["input_hash"]:
        return None, _blocker(draft_id, "STALE_INPUT_HASH", "candidate/source content no longer matches draft input")
    if draft.get("scenario") != row["scenario"]:
        return None, _blocker(draft_id, "SCENARIO_MISMATCH", "draft scenario differs from candidate")
    evidence_error = _validate_evidence(draft, content)
    if evidence_error:
        return None, _blocker(draft_id, evidence_error, "source evidence is absent or no longer exact")
    source_leak_error = _source_leak_error(draft, row, content)
    if source_leak_error:
        return None, _blocker(draft_id, source_leak_error,
                              "derived QAU contains verbatim source material or an identifier")
    if draft.get("content_gaps"):
        return None, _blocker(draft_id, "CONTENT_GAP", "published QAU cannot retain content gaps")
    if draft.get("direct_answer") is None:
        return None, _blocker(draft_id, "MISSING_DIRECT_ANSWER", "published QAU needs a direct answer")
    if any(item.get("state") != "supported" for item in draft.get("what_ifs", []) if isinstance(item, dict)):
        return None, _blocker(draft_id, "UNSUPPORTED_OR_UNKNOWN_BRANCH", "published QAU cannot carry unknown branches")
    topic_key = (row["scenario"], str(draft.get("topic", "")).strip().lower())
    mapped = TOPIC_MAP.get(topic_key)
    if mapped is None:
        return None, _blocker(draft_id, "UNMAPPED_SCENARIO_TOPIC", "no safe LAND QAU topic mapping", scenario=row["scenario"], topic=draft.get("topic"))
    requirements = draft.get("applicability", {}).get("requirements")
    if not isinstance(requirements, dict):
        return None, _blocker(draft_id, "APPLICABILITY_INVALID", "draft applicability requirements are invalid")
    if set(requirements).difference({"city", "country"}):
        return None, _blocker(draft_id, "UNMAPPED_APPLICABILITY", "airport, route, service or other scope requires product mapping", dimensions=sorted(set(requirements).difference({"city", "country"})))
    if requirements.get("city") not in (None, [row["city"]]):
        return None, _blocker(draft_id, "CITY_SCOPE_MISMATCH", "draft city scope differs from candidate")
    if requirements.get("country") not in (None, [country]):
        return None, _blocker(draft_id, "COUNTRY_SCOPE_MISMATCH", "country scope cannot be inferred or differs from city profile")
    review = _review_row(row["connection"], draft_id)
    if review is None or review["action"] != "approve" or review["new_status"] != ACCEPTED_STATUS:
        return None, _blocker(draft_id, "MISSING_ACCEPTANCE_REVIEW", "accepted draft lacks its append-only approval decision")
    review_data = _as_dict(review["data"])
    dynamic = _needs_verification(draft)
    rule_detail = None
    if dynamic:
        if len(draft.get("facts_requiring_verification", [])) > 1:
            return None, _blocker(
                draft_id, "MULTIPLE_RULE_FACTS_NEED_SEPARATE_REFERENCES",
                "split unrelated dynamic facts so each published QAU has one explicit verification basis")
        if not review["reference_url"] or not _iso(review["verified_at"]):
            return None, _blocker(draft_id, "OFFICIAL_VERIFICATION_MISSING", "dynamic facts need URL and verified_at")
        try:
            verification_url = normalize_url(review["reference_url"])
        except ValueError:
            return None, _blocker(draft_id, "OFFICIAL_VERIFICATION_INVALID", "verification URL is invalid")
        # New review records carry this explicit classification.  Never guess
        # that an arbitrary community URL is an operator source.
        kind = review_data.get("reference_kind")
        if kind not in ("official", "operator") or _source_kind(verification_url) != kind:
            return None, _blocker(draft_id, "OFFICIAL_OR_OPERATOR_REQUIRED", "community sources cannot support a rule", reference_url=verification_url)
        valid_until = review_data.get("valid_until")
        if not _iso(valid_until) or not _iso(review["verified_at"]):
            return None, _blocker(draft_id, "RULE_VALIDITY_MISSING", "rule verification needs verified_at and valid_until")
        valid_until_date = datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
        if valid_until_date <= datetime.fromisoformat(review["verified_at"].replace("Z", "+00:00")):
            return None, _blocker(draft_id, "RULE_VALIDITY_INVALID", "valid_until must follow verified_at")
        if valid_until_date <= datetime.now(timezone.utc):
            return None, _blocker(draft_id, "RULE_VERIFICATION_EXPIRED", "official/operator verification has expired")
        title = review_data.get("reference_title")
        if not isinstance(title, str) or not title.strip():
            return None, _blocker(draft_id, "RULE_REFERENCE_TITLE_MISSING", "official/operator verification needs a title")
        rule_detail = {"detailID": "rule-" + stable_id(draft_id, verification_url), "kind": kind,
                       "title": title.strip(), "url": verification_url,
                       "author": urlsplit(verification_url).hostname, "platform": "editorial_verification",
                       "role": "rule", "publishedAt": None, "collectedAt": review["verified_at"]}
    blocks = []
    bindings = []
    needs_experience = False
    needs_rule = False
    question_key = stable_id("knowledge-qau", draft_id, row["input_hash"])
    def add_block(source, label):
        nonlocal needs_experience, needs_rule
        if not isinstance(source, dict) or not isinstance(source.get("text"), str) or not source["text"].strip():
            raise ValueError("invalid answer block")
        basis = source.get("basis")
        if basis not in ("PERSONAL_EXPERIENCE", "NEEDS_OFFICIAL_VERIFICATION"):
            raise ValueError("invalid answer basis")
        block_id = "block-" + stable_id(question_key, label)
        role = "experience" if basis == "PERSONAL_EXPERIENCE" else "rule"
        if role == "experience":
            needs_experience = True
            answer = {"blockID": block_id, "text": source["text"].strip(), "determinesAction": True,
                      "attribution": "个体旅行者经验，经 LAND 编辑整理；实际情况可能因时间和个人情况不同。"}
        else:
            needs_rule = True
            answer = {"blockID": block_id, "text": source["text"].strip(), "determinesAction": True}
        blocks.append((label, answer))
        return block_id, role
    try:
        direct_id, direct_role = add_block(draft["direct_answer"], "direct")
        actions = []
        for position, item in enumerate(draft.get("actions", [])):
            block_id, role = add_block(item, "action-{}".format(position))
            actions.append(blocks[-1][1])
        successes = []
        for position, item in enumerate(draft.get("success_signals", [])):
            block_id, role = add_block(item, "success-{}".format(position))
            successes.append(blocks[-1][1])
        branches = []
        for position, item in enumerate(draft.get("what_ifs", [])):
            if not isinstance(item, dict) or item.get("state") != "supported":
                raise ValueError("invalid what-if")
            block_id, role = add_block({"text": item.get("answer"), "basis": item.get("basis")}, "whatif-{}".format(position))
            branches.append({"branchID": "branch-" + stable_id(question_key, position),
                             "trigger": item.get("trigger"), "state": "supported", "critical": True,
                             "answer": blocks[-1][1], "relatedQuestionID": None,
                             "boundary": item.get("boundary")})
    except (KeyError, ValueError):
        return None, _blocker(draft_id, "ANSWER_BLOCK_INVALID", "draft answer block cannot be compiled safely")
    if dynamic and not needs_rule:
        return None, _blocker(draft_id, "UNBOUND_DYNAMIC_FACT", "dynamic fact has no rule answer block to bind")
    export_enabled = _policy_value(source_policies, row["source"], "export_enabled")
    allow_source_republication = _policy_value(
        source_policies, row["source"], "allow_source_republication")
    allow_experience_derivation = _policy_value(
        source_policies, row["source"], "allow_experience_derivation")
    allow_verified_rules = _policy_value(source_policies, row["source"], "allow_verified_rules")
    if needs_experience and not allow_experience_derivation:
        return None, _blocker(
            draft_id, "EXPERIENCE_DERIVATION_DISABLED",
            "source policy does not allow an editorially accepted traveler experience derivative",
            source=row["source"])
    if needs_rule and not allow_verified_rules:
        return None, _blocker(draft_id, "DERIVED_RULE_EXPORT_DISABLED", "source policy has not allowed public rule derivation after verification", source=row["source"])
    traveler_detail = None
    if needs_experience:
        traveler_detail, source_error = _source_detail(question_key, content, "experience")
        if source_error:
            return None, _blocker(draft_id, source_error, "source content lacks publishable attribution")
    details = [] if traveler_detail is None else [traveler_detail]
    if rule_detail:
        details.append(rule_detail)
    applicability = {"requirements": {"country": [country], "city": [row["city"]]},
                     "validFrom": None, "validUntil": None,
                     "summary": draft.get("applicability", {}).get("summary", ""),
                     "unconfirmed": draft.get("applicability", {}).get("unconfirmed", [])}
    for _, block in blocks:
        role = "experience" if "attribution" in block else "rule"
        detail_id = traveler_detail["detailID"] if role == "experience" else rule_detail["detailID"]
        bindings.append({"blockID": block["blockID"], "detailIDs": [detail_id], "role": role,
                         "applicability": applicability,
                         "verifiedAt": None if role == "experience" else review["verified_at"],
                         "validUntil": None if role == "experience" else review_data["valid_until"], "needsReview": False})
    topic, intent = mapped
    question_id = "knowledge-{}-{}".format(row["city"], question_key[:16])
    reference_id = "knowledge-reference-" + question_key[:16]
    question = {"questionID": question_id, "revision": 1, "locale": "zh-Hans",
                "situationID": "knowledge-situation-" + question_key[:16], "intent": intent,
                "question": draft.get("question"), "searchAliases": [],
                "directAnswer": blocks[0][1], "actions": actions, "successSignals": successes,
                "whatIfs": branches, "relatedQuestionIDs": [],
                "ownerScope": {"kind": "city", "id": row["city"], "countryID": country},
                "applicability": applicability, "placements": [],
                "countryPlacements": [{"scopeKind": "city", "scopeID": row["city"], "topic": topic,
                                       "groupID": "knowledge-" + question_key[:12], "rank": 10}],
                "relation": None, "supportBindings": bindings,
                "referenceRef": {"referenceID": reference_id, "revision": 1}, "legacyRefs": [],
                "editorialState": "published", "productReviewState": "accepted",
                "updatedAt": review["created_at"], "contentGaps": []}
    limitations = list(draft.get("risk", []))
    if needs_experience:
        limitations.extend([
            "个体旅行者经验，不代表官方规则或普遍情况。",
            "原始身份与帖子未进入 LAND 公开内容包。",
        ])
    reference = {"ref": {"referenceID": reference_id, "revision": 1}, "details": details,
                 "limitations": list(dict.fromkeys(limitations)), "frozenTipRefs": [], "artifactPaths": []}
    frozen = {"draft_id": draft_id, "candidate_id": row["candidate_id"],
              "source_content_id": row["source_content_id"], "input_hash": row["input_hash"],
              "extractor_version": row["extractor_version"], "model": row["model"],
              "review_id": review["id"], "reviewer": review["reviewer"],
              "review_decision": review["action"], "reviewed_at": review["created_at"],
              "verification_url": review["reference_url"], "verified_at": review["verified_at"]}
    frozen["review_decision_hash"] = _sha({key: review[key] for key in review.keys()})
    frozen["source_policy"] = {
        "export_enabled": export_enabled,
        "allow_source_republication": allow_source_republication,
        "allow_experience_derivation": allow_experience_derivation,
        "allow_verified_rules": allow_verified_rules,
    }
    frozen["question_id"] = question_id
    frozen["reference_id"] = reference_id
    return (question, reference, frozen), None


def _merged_index(base, city, questions, references):
    """Append safely compiled records while preserving the base frozen source."""
    index = json.loads(_canonical(base))
    required = {"schemaVersion", "indexID", "revision", "frozenSource", "situations", "questions", "references", "legacyMappings", "transport"}
    if not isinstance(index, dict) or set(index) != required or index.get("schemaVersion") != SCHEMA_VERSION:
        raise ValueError("base_index_path is not a QuestionContentIndex v1")
    if not isinstance(index.get("frozenSource"), dict):
        raise ValueError("base index frozenSource is invalid")
    existing_questions = {item.get("questionID") for item in index["questions"] if isinstance(item, dict)}
    existing_refs = {(item.get("ref", {}).get("referenceID"), item.get("ref", {}).get("revision"))
                     for item in index["references"] if isinstance(item, dict)}
    if existing_questions.intersection(question["questionID"] for question in questions):
        raise ValueError("compiled question ID collides with base index")
    if existing_refs.intersection((reference["ref"]["referenceID"], reference["ref"]["revision"]) for reference in references):
        raise ValueError("compiled reference ID collides with base index")
    index["situations"].extend({"situationID": question["situationID"], "locale": "zh-Hans", "title": question["question"]}
                               for question in questions)
    index["questions"].extend(questions)
    index["references"].extend(references)
    # The base frozenSource describes the editorial source document, and must
    # survive byte-for-byte.  Manifest pins both it and the resulting package.
    index["revision"] = int(base["revision"]) + 1
    return index


def _parent_release_for_base(releases_root, city, base_sha256):
    """Find a verified release only when its output is the current LAND base."""
    candidates = []
    for path in Path(releases_root).glob("knowledge-{}-*".format(city)):
        if not (path / "manifest.json").is_file():
            continue
        try:
            manifest = _read_json(path / "manifest.json")
            output = manifest.get("files", {}).get("question-content-index-v1.json")
            output = output.get("sha256") if isinstance(output, dict) else output
            if manifest.get("status") == VERIFIED and output == base_sha256:
                candidates.append((manifest.get("verified_at", manifest.get("created_at", "")), path))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return max(candidates, default=("", None))[1]


def _make_diff(index, previous, *, base_release_id=None):
    if previous is None:
        before = {}
    else:
        source = previous if previous.is_file() else previous / "question-content-index-v1.json"
        before = {q["questionID"]: q for q in _read_json(source).get("questions", [])}
    after = {q["questionID"]: q for q in index["questions"]}
    added = sorted(set(after).difference(before))
    removed = sorted(set(before).difference(after))
    updated = sorted(key for key in set(after).intersection(before)
                     if _canonical(after[key]) != _canonical(before[key]))
    return {"base_release_id": base_release_id if base_release_id is not None else (None if previous is None else previous.name),
            "added": added, "removed": removed, "updated": updated,
            "high_risk": {"added": added, "removed": removed, "updated": updated}}


def prepare_release(storage, *, city, base_index_path, releases_root=".local/releases", city_countries=None,
                    source_policies=None):
    """Prepare an immutable, mergeable QAU sidecar for one city.

    ``base_index_path`` must name the complete current LAND QAU index.  The
    bridge copies it and appends records; it never emits a partial replacement.
    ``city_countries`` is intentionally required for the city being released;
    deriving a country from prose would create a country-rule publication path.
    It accepts the ``Config.cities`` shape as well as ``{city: "US"}``.
    """
    if city_countries and isinstance(city_countries.get(city), dict):
        city_countries = {key: value.get("country") for key, value in city_countries.items()}
    country = _country_for(city, city_countries)
    if not country:
        raise ValueError("prepare_release requires an explicit city_countries entry for the city")
    base_path = Path(base_index_path)
    if not base_path.is_file():
        raise ValueError("base_index_path must be an existing complete LAND QAU index")
    base_bytes = base_path.read_bytes()
    base = _read_json(base_path)
    base_sha256 = _sha(base_bytes)
    connection = storage.connection
    ensure_xhs_schema(connection)
    ensure_knowledge_schema(connection)
    rows = connection.execute("""
      SELECT d.id,d.candidate_id,d.source_content_id,d.input_hash,d.extractor_version,d.model,d.status,
             d.verification_required,d.data,k.city,k.scenario,k.query,k.reason,k.data AS candidate_data,
             s.data AS content_data,raw.source AS source,raw.data AS raw_data
      FROM xhs_knowledge_drafts d
      JOIN xhs_knowledge_candidates k ON k.id=d.candidate_id
      JOIN source_contents s ON s.id=d.source_content_id
      JOIN raw_source_records raw ON raw.id=s.raw_record_id
      WHERE d.status=? AND k.city=? ORDER BY d.id
    """, (ACCEPTED_STATUS, city)).fetchall()
    compiled, blockers = [], []
    for sqlite_row in rows:
        row = dict(sqlite_row)
        row["connection"] = connection
        result, blocker = _compile_one(row, _as_dict(row["content_data"]), country, source_policies or {})
        if blocker:
            blockers.append(blocker)
        else:
            compiled.append(result)
    # Accepted drafts remain in SQLite after installation.  A later release
    # must skip records already present byte-for-byte in the current LAND base,
    # while refusing a same-ID record whose published representation changed.
    base_questions = {item.get("questionID"): item for item in base.get("questions", [])
                      if isinstance(item, dict)}
    base_references = {(item.get("ref", {}).get("referenceID"), item.get("ref", {}).get("revision")): item
                       for item in base.get("references", []) if isinstance(item, dict)}
    pending, already_present = [], []
    for question, reference, frozen in compiled:
        existing_question = base_questions.get(question["questionID"])
        reference_key = (reference["ref"]["referenceID"], reference["ref"]["revision"])
        existing_reference = base_references.get(reference_key)
        if existing_question is None and existing_reference is None:
            pending.append((question, reference, frozen))
            continue
        if (existing_question is not None and existing_reference is not None and
                _canonical(existing_question) == _canonical(question) and
                _canonical(existing_reference) == _canonical(reference)):
            already_present.append({"draft_id": frozen["draft_id"],
                                    "question_id": question["questionID"],
                                    "reference_id": reference_key[0]})
            continue
        blockers.append(_blocker(
            frozen["draft_id"], "PUBLISHED_ID_CONFLICT",
            "current LAND base contains the stable ID with different content",
            question_id=question["questionID"], reference_id=reference_key[0]))
    compiled = pending
    frozen_inputs = [item[2] for item in compiled]
    fingerprint = _sha({"city": city, "country": country, "base_index_sha256": base_sha256,
                        "inputs": frozen_inputs, "already_present": already_present,
                        "blockers": blockers})
    release_id = "knowledge-{}-{}".format(city, fingerprint[:16])
    root = Path(releases_root)
    release_dir = root / release_id
    if release_dir.exists():
        manifest = _read_json(release_dir / "manifest.json")
        if manifest.get("input_fingerprint") != fingerprint:
            raise ValueError("existing release id has different immutable inputs")
        return {"release_id": release_id, "release_dir": str(release_dir), "status": manifest.get("status"),
                "idempotent": True, "compiled": len(manifest.get("inputs", [])),
                "already_present": len(manifest.get("already_present", [])),
                "blockers": manifest.get("blockers", [])}
    questions, references = [item[0] for item in compiled], [item[1] for item in compiled]
    index = _merged_index(base, city, questions, references)
    root.mkdir(parents=True, exist_ok=True)
    parent = _parent_release_for_base(root, city, base_sha256)
    diff = _make_diff(index, base_path,
                      base_release_id=(parent.name if parent else "base-index"))
    # Build under the same parent, then atomically publish the complete release.
    staging = root / (release_id + ".tmp-" + secrets.token_hex(6))
    staging.mkdir()
    index_path = staging / "question-content-index-v1.json"
    diff_path = staging / "diff.json"
    _write_json(index_path, index)
    _write_json(diff_path, diff)
    manifest = {"manifest_version": 1, "generator_version": GENERATOR_VERSION,
                "release_id": release_id, "parent_release_id": parent.name if parent else None,
                "city": city, "city_ids": [city], "country": country,
                "locales": ["zh-Hans"], "status": PREPARED,
                "created_at": utc_now(), "input_fingerprint": fingerprint, "inputs": frozen_inputs,
                "already_present": already_present,
                "blockers": blockers, "release_question_ids": [item[0]["questionID"] for item in compiled],
                "base_index": {"path": str(base_path), "sha256": base_sha256,
                "revision": base.get("revision"), "question_count": len(base.get("questions", [])),
                "frozen_source": base.get("frozenSource")}, "files": {index_path.name: _sha(index_path.read_bytes()),
                diff_path.name: _sha(diff_path.read_bytes())},
                "file_metadata": {
                    index_path.name: {"sha256": _sha(index_path.read_bytes()), "bytes": index_path.stat().st_size},
                    diff_path.name: {"sha256": _sha(diff_path.read_bytes()), "bytes": diff_path.stat().st_size}},
                "counts": {"questions_added": len(compiled), "references_added": len(references),
                           "already_present": len(already_present),
                           "questions_total": len(index.get("questions", [])),
                           "references_total": len(index.get("references", [])),
                           "blocked": len(blockers)},
                "limitations": ["This release is a merged QAU candidate. Installation into LAND is deliberately handled by a separate publishing step."]}
    _write_json(staging / "manifest.json", manifest)
    os.replace(str(staging), str(release_dir))
    return {"release_id": release_id, "release_dir": str(release_dir), "status": PREPARED,
            "idempotent": False, "compiled": len(compiled),
            "already_present": len(already_present), "blockers": blockers}


def release_diff(release):
    """Read the frozen diff generated during prepare; it never recomputes history."""
    return _read_json(Path(release) / "diff.json")


def _verify_index(index, manifest):
    errors = []
    required = {"schemaVersion", "indexID", "revision", "frozenSource", "situations", "questions", "references", "legacyMappings", "transport"}
    if not isinstance(index, dict) or set(index) != required or index.get("schemaVersion") != SCHEMA_VERSION:
        return ["INDEX_SCHEMA"]
    questions, references = index["questions"], index["references"]
    if not isinstance(questions, list) or not isinstance(references, list):
        return ["INDEX_COLLECTIONS"]
    ids = [q.get("questionID") for q in questions if isinstance(q, dict)]
    refs = [(r.get("ref", {}).get("referenceID"), r.get("ref", {}).get("revision")) for r in references if isinstance(r, dict)]
    if len(ids) != len(set(ids)) or any(not value for value in ids): errors.append("QUESTION_ID_UNIQUENESS")
    if len(refs) != len(set(refs)) or any(not value[0] for value in refs): errors.append("REFERENCE_ID_UNIQUENESS")
    ref_map = {key: record for key, record in zip(refs, references)}
    release_questions = set(manifest.get("release_question_ids", []))
    for q in questions:
        if not isinstance(q, dict):
            errors.append("QUESTION_INVALID"); continue
        is_release_question = q.get("questionID") in release_questions
        if is_release_question and (q.get("editorialState") != "published" or q.get("productReviewState") != "accepted"):
            errors.append("PUBLISHED_ACCEPTED_GATE")
        if is_release_question and (not q.get("directAnswer") or q.get("contentGaps") or any(b.get("state") != "supported" for b in q.get("whatIfs", []) if isinstance(b, dict))):
            errors.append("PUBLISHED_CONTENT_GATE")
        owner, applicability = q.get("ownerScope", {}), q.get("applicability", {})
        if is_release_question and (owner.get("kind") != "city" or owner.get("id") != manifest.get("city") or applicability.get("requirements", {}).get("country") != [manifest.get("country")] or applicability.get("requirements", {}).get("city") != [manifest.get("city")]):
            errors.append("OWNER_APPLICABILITY")
        ref_value = q.get("referenceRef", {})
        reference = ref_map.get((ref_value.get("referenceID"), ref_value.get("revision")))
        if reference is None: errors.append("REFERENCE_CLOSURE"); continue
        detail_map = {item.get("detailID"): item for item in reference.get("details", []) if isinstance(item, dict)}
        blocks = [q.get("directAnswer")] + q.get("actions", []) + q.get("successSignals", []) + [b.get("answer") for b in q.get("whatIfs", []) if isinstance(b, dict)]
        blocks = [b for b in blocks if isinstance(b, dict)]
        if len(detail_map) != len(reference.get("details", [])):
            errors.append("DETAIL_ID_UNIQUENESS")
        if len({b.get("blockID") for b in blocks}) != len(blocks):
            errors.append("BLOCK_ID_UNIQUENESS")
        binding_map = {b.get("blockID"): b for b in q.get("supportBindings", []) if isinstance(b, dict)}
        decisive = [b.get("blockID") for b in blocks if b.get("determinesAction")]
        for block_id in decisive:
            binding = binding_map.get(block_id)
            if not binding: errors.append("DECISIVE_SUPPORT"); continue
            role = binding.get("role"); detail_ids = binding.get("detailIDs", [])
            details = [detail_map.get(detail_id) for detail_id in detail_ids]
            if not details or any(detail is None or detail.get("role") != role for detail in details): errors.append("SUPPORT_CLOSURE")
            block = next((b for b in blocks if b.get("blockID") == block_id), {})
            if role == "experience":
                if (not block.get("attribution") or binding.get("verifiedAt") is not None or
                        (is_release_question and any(d.get("kind") != "editorial_experience" for d in details if d))):
                    errors.append("EXPERIENCE_ATTRIBUTION")
            elif role == "rule":
                dates_invalid = (is_release_question and
                    (not _iso(binding.get("verifiedAt")) or not _iso(binding.get("validUntil")) or
                     datetime.fromisoformat(binding["validUntil"].replace("Z", "+00:00")) <= datetime.fromisoformat(binding["verifiedAt"].replace("Z", "+00:00"))))
                unsafe_kind = any(
                    d.get("kind") not in (("official", "operator") if is_release_question
                                          else ("official", "operator", "industry"))
                    for d in details if d)
                if dates_invalid or unsafe_kind:
                    errors.append("RULE_VERIFICATION")
            elif is_release_question:
                errors.append("UNSAFE_SUPPORT_ROLE")
    return sorted(set(errors))


def _verify_frozen_inputs(storage, manifest):
    """Optionally prove the database has not changed since preparation."""
    errors = []
    connection = storage.connection
    for frozen in manifest.get("inputs", []):
        row = connection.execute("""
          SELECT d.status,d.input_hash,k.source_content_id,k.city,k.scenario,k.query,k.reason,s.data AS content_data
          FROM xhs_knowledge_drafts d JOIN xhs_knowledge_candidates k ON k.id=d.candidate_id
          JOIN source_contents s ON s.id=k.source_content_id WHERE d.id=?
        """, (frozen.get("draft_id"),)).fetchone()
        if row is None or row["status"] != ACCEPTED_STATUS or row["input_hash"] != frozen.get("input_hash"):
            errors.append("FROZEN_DRAFT_CHANGED:" + str(frozen.get("draft_id")))
            continue
        current = dict(row)
        content = _as_dict(row["content_data"])
        if content.get("content_status", "ACTIVE") != "ACTIVE":
            errors.append("FROZEN_SOURCE_NOT_ACTIVE:" + str(frozen.get("draft_id")))
        if _current_input_hash(current, content) != frozen.get("input_hash"):
            errors.append("FROZEN_CANDIDATE_HASH_CHANGED:" + str(frozen.get("draft_id")))
        review = _review_row(connection, frozen.get("draft_id"))
        if (review is None or review["id"] != frozen.get("review_id") or
                _sha({key: review[key] for key in review.keys()}) != frozen.get("review_decision_hash")):
            errors.append("FROZEN_REVIEW_CHANGED:" + str(frozen.get("draft_id")))
    return errors


def verify_release(release, *, storage=None, reviewer=None, reason=None):
    """Verify hashes and the LAND reader's publication invariants.

    A failed verification leaves the manifest's status unchanged.  Successful
    verification is the sole state transition from PREPARED to VERIFIED.
    """
    directory = Path(release)
    try:
        manifest = _read_json(directory / "manifest.json")
        errors = []
        if manifest.get("status") not in (PREPARED, VERIFIED):
            errors.append("RELEASE_STATUS")
        if manifest.get("blockers"):
            errors.append("UNRESOLVED_BLOCKERS")
        if not manifest.get("release_question_ids"):
            errors.append("EMPTY_RELEASE")
        for name, expected in manifest.get("files", {}).items():
            path = directory / name
            if not path.is_file() or _sha(path.read_bytes()) != expected:
                errors.append("FILE_SHA256:" + name)
        index = _read_json(directory / "question-content-index-v1.json")
        errors.extend(_verify_index(index, manifest))
        base = manifest.get("base_index", {})
        if index.get("frozenSource") != base.get("frozen_source"):
            errors.append("FROZEN_SOURCE_MISMATCH")
        base_path = base.get("path")
        if not isinstance(base_path, str) or not Path(base_path).is_file() or _sha(Path(base_path).read_bytes()) != base.get("sha256"):
            errors.append("BASE_INDEX_SHA256")
        if storage is not None:
            errors.extend(_verify_frozen_inputs(storage, manifest))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"ok": False, "status": None, "errors": ["RELEASE_UNREADABLE:" + type(exc).__name__]}
    errors = sorted(set(errors))
    if errors:
        return {"ok": False, "status": manifest.get("status"), "errors": errors}
    if manifest.get("status") == PREPARED:
        if not isinstance(reviewer, str) or not reviewer.strip() or not isinstance(reason, str) or not reason.strip():
            return {"ok": False, "status": PREPARED, "errors": ["VERIFICATION_REVIEW_REQUIRED"]}
        manifest["verification"] = {"reviewer": reviewer.strip(), "reason": reason.strip(), "verified_at": utc_now()}
    manifest["status"] = VERIFIED
    manifest["verified_at"] = manifest.get("verification", {}).get("verified_at", utc_now())
    _write_json(directory / "manifest.json", manifest)
    return {"ok": True, "status": VERIFIED, "errors": []}
