#!/usr/bin/env python3
"""Validate AP-01 files and declarative expectations, not ERP runtime behavior."""

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from collections import Counter
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from pypdf import PdfReader
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "docs/plans/ai-posteingang/schemas"
FIXTURES = ROOT / "kanzlei_erp/tests/fixtures/posteingang"
WORKFLOW = ROOT / "docs/plans/ai-posteingang/workflow.json"


class ContractError(ValueError):
    """A malformed file or inconsistent cross-file expectation."""


def require(condition, message):
    if not condition:
        raise ContractError(message)


def read_json(path):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, f"{path}: duplicate JSON key {key}")
            result[key] = value
        return result

    try:
        return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique_pairs)
    except (OSError, ValueError) as exc:
        raise ContractError(f"{path}: {exc}") from exc


def safe_path(root, relative):
    path = Path(relative)
    target = (root / path).resolve()
    require(not path.is_absolute() and ".." not in path.parts and target.is_relative_to(root.resolve()), f"unsafe path: {relative}")
    require(target.is_file(), f"missing fixture: {relative}")
    return target


@lru_cache(maxsize=1)
def schema_registry():
    schemas = {}
    for path in sorted(SCHEMAS.glob("*.schema.json")):
        value = read_json(path)
        Draft202012Validator.check_schema(value)
        value["$id"] = path.resolve().as_uri()
        schemas[path.name] = value
    # No retrieve callback: references outside this local registry cannot trigger network I/O.
    registry = Registry().with_resources((s["$id"], Resource.from_contents(s)) for s in schemas.values())
    return schemas, registry


def validate_document(value, schema_name, definition=None):
    schemas, registry = schema_registry()
    schema = schemas[schema_name]
    if definition:
        schema = {"$ref": schema["$id"] + "#/$defs/" + definition}
    validator = Draft202012Validator(schema, registry=registry, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(value), key=lambda e: str(list(e.absolute_path)))
    if errors:
        error = errors[0]
        if error.validator == "anyOf":
            error = next((child for child in error.context if child.validator != "type"), error)
        raise ContractError(f"{schema_name} {list(error.absolute_path)}: {error.message} [{error.validator}]")


def normalized(text):
    return " ".join(unicodedata.normalize("NFKC", text).split())


def identifier_key(identifier):
    """Validate exact-match provenance in gold fixtures; this is not AP-06 matching."""
    kind = identifier["type"]
    value = unicodedata.normalize("NFKC", identifier["value"]).strip()
    context = tuple(unicodedata.normalize("NFKC", identifier[k]).strip() if identifier.get(k) else None for k in ("jurisdiction", "authority", "other_type"))
    if kind == "de_steuernummer":
        value = "".join(value.split()).replace("/", "")
        if not re.fullmatch(r"[0-9]+", value) or not all(context[:2]):
            return None
        return (kind, value, *context[:2], None)
    if kind == "de_tax_id":
        value = "".join(value.split())
        return (kind, value, None, None, None) if re.fullmatch(r"[0-9]+", value) else None
    if kind == "de_vat_id":
        value = "".join(value.split()).upper()
        return (kind, value, None, None, None) if re.fullmatch(r"[A-Z0-9]+", value) else None
    if kind == "other" and all(context[1:]):
        return (kind, value, *context)
    return None


def validate_annotations(gold, case, source_pages, customers):
    validate_document(gold, "annotations.schema.json")
    require(gold["case_id"] == case["case_id"], "semantic gold case_id mismatch")

    def check_quotes(value):
        if isinstance(value, dict):
            if {"page", "quote"} <= value.keys():
                require(1 <= value["page"] <= len(source_pages), "semantic gold page outside PDF")
                require(normalized(value["quote"]) in normalized(source_pages[value["page"] - 1]["text"]), "semantic gold quote absent from source")
            for child in value.values():
                check_quotes(child)
        elif isinstance(value, list):
            for child in value:
                check_quotes(child)

    check_quotes(gold)
    ids = [a["gold_action_id"] for a in gold["actions"]]
    require(len(ids) == len(set(ids)), "duplicate semantic gold action id")
    proof_customers = [p["customer"] for p in gold["candidate_evidence"]]
    require(proof_customers == gold["customer_candidates"] and len(proof_customers) == len(set(proof_customers)), "semantic gold candidates need unique provenance")
    for proof in gold["candidate_evidence"]:
        require(proof["customer"] in case["allowed_customers"], "inaccessible semantic gold customer")
        customer = customers[proof["customer"]]
        require(proof["recipient_index"] < len(gold["recipients"]), "candidate provenance: invalid recipient_index")
        recipient = gold["recipients"][proof["recipient_index"]]
        if proof["kind"] == "exact_identifier":
            index = proof["identifier_index"]
            require(index is not None and index < len(recipient["identifiers"]), "candidate provenance: missing identifier_index")
            identifier = recipient["identifiers"][index]
            key = identifier_key(identifier)
            require(identifier["holder_role"] == "addressee" and key is not None and key in [identifier_key(i) for i in customer["identifiers"]], "candidate provenance: identifier does not identify this addressee")
        else:
            require(proof["identifier_index"] is None and recipient["name"]["value"] and recipient["address"]["value"], "candidate provenance: name/address required")
            require(normalized(recipient["name"]["value"]).casefold() == normalized(customer["name"]).casefold() and normalized(recipient["address"]["value"]).casefold() == normalized(customer["address"]).casefold(), "candidate provenance: name/address differ from catalog")


def validate_evidence(value, ocr):
    if isinstance(value, dict):
        if {"page", "quote", "verification"} <= value.keys():
            require(1 <= value["page"] <= len(ocr), "evidence page outside PDF")
            if value["verification"] == "matched":
                require(normalized(value["quote"]) in normalized(ocr[value["page"] - 1]["text"]), "matched quote absent from OCR page")
        for child in value.values():
            validate_evidence(child, ocr)
    elif isinstance(value, list):
        for child in value:
            validate_evidence(child, ocr)


def validate_review(review, extraction):
    actions = review["actions"]
    ids = [a["action_id"] for a in actions]
    require(len(ids) == len(set(ids)), "duplicate action_id")
    indices = [a["source_index"] for a in actions if a["source_index"] is not None]
    require(len(indices) == len(set(indices)), "duplicate source_index")
    require(all(i < len(extraction["actions"]) for i in indices), "source_index outside extraction actions")
    for action in actions:
        if action["decision"] == "reject":
            require(action["rejection_reason"], "rejection_reason required")
    if review["mode"] == "clarification":
        require(review["clarification_reason"], "clarification_reason required")
    if review["mode"] != "confirm":
        return
    require(review["human_verified"], "human_verified required for confirmation")
    require(review["selected_customer"], "selected_customer required for confirmation")
    require(review["customer_reason"], "customer_reason required for confirmation")
    require(len(extraction["recipients"]) <= 1, "multiple recipients require clarification in v1")
    require(all(a["decision"] != "pending" for a in actions), "pending action cannot be confirmed")
    accepted = [a for a in actions if a["decision"] == "accept"]
    if not accepted:
        require(review["no_actions_reason"], "no_actions_reason required")
    else:
        require(review["no_actions_reason"] is None, "no_actions_reason contradicts accepted actions")
    require(set(indices) == set(range(len(extraction["actions"]))), "every source action needs an explicit decision")
    for action in accepted:
        if action["assigned_to"] is None:
            require(action["assignment_decision"] == "unassigned" and action["assignment_reason"], "assignment_reason and explicit unassigned decision required")
        else:
            require(action["assignment_decision"] == "assigned", "assigned user requires assigned decision")
        if action["internal_date"] is None:
            require(action["scheduling_decision"] == "unscheduled" and action["scheduling_reason"], "scheduling_reason and explicit unscheduled decision required")
        else:
            require(action["scheduling_decision"] == "scheduled", "internal_date requires scheduled decision")
        if action["external_date"]:
            require(action["external_date_basis"], "external_date_basis required")
        require(action["needs_planning"] == (action["assigned_to"] is None or action["internal_date"] is None), "needs_planning inconsistent with assignment/date")


def validate_scenarios(scenarios):
    validate_document(scenarios, "support.schema.json", "scenarios")
    workflow = read_json(WORKFLOW)
    validate_document(workflow, "support.schema.json", "workflow")
    for transition in workflow["transitions"]:
        if transition["operation"] in ("start_run", "stage_result"):
            require({"initiator_authorized", "feature_enabled", "analysis_enabled", "current_generation"} <= set(transition["requires"]), "worker transition missing authorization/switch/generation guard")
    allowed = {(t["from"], t["to"], t["operation"]) for t in workflow["transitions"]}
    seen_ids = set()
    pairs = set()
    for transition in scenarios["transitions"]:
        require(transition["id"] not in seen_ids, "duplicate scenario id")
        seen_ids.add(transition["id"])
        pairs.add((transition["from"], transition["to"]))
        actual = (transition["from"], transition["to"], transition["operation"]) in allowed
        require(actual == transition["allowed"], f"transition expectation: {transition['id']}")
    require(pairs == {(a, b) for a in workflow["states"] for b in workflow["states"]}, "all 64 state pairs must be specified")
    expected_codes = {"revision_conflict": "REVISION_CONFLICT", "idempotency_conflict": "IDEMPOTENCY_CONFLICT", "late_run": "STALE_RUN", "permission_revoked": "PERMISSION_DENIED", "assignment_rollback": "ASSIGNMENT_FAILED", "exact_duplicate": "DUPLICATE"}
    kinds = set()
    for case in scenarios["operations"]:
        require(case["id"] not in seen_ids, "duplicate scenario id")
        seen_ids.add(case["id"])
        kind, given, expected, requests = case["kind"], case["given"], case["expected"], case["requests"]
        kinds.add(kind)
        def check(condition):
            require(condition, f"scenario expectation: {case['id']}")
        check(expected["code"] == expected_codes.get(kind, "OK"))
        check(not expected["data_disclosed"])
        check(expected["tasks_delta"] == (1 if kind == "confirmation_retry" else 0))
        check(expected["letters_delta"] == (1 if kind in ("upload_retry", "correction") else 0))
        if kind in ("upload_retry", "confirmation_retry", "correction"):
            check(len(requests) == 2 and requests[0] == requests[1] and expected["same_result"])
        if kind in expected_codes:
            check(expected["draft_unchanged"])
        if kind == "revision_conflict":
            check(requests[0]["expected_revision"] != given["revision"])
        if kind == "idempotency_conflict":
            check(len(requests) == 2 and requests[0]["request_key"] == requests[1]["request_key"] and requests[0]["payload_fingerprint"] != requests[1]["payload_fingerprint"])
        if kind == "late_run":
            check(given["response_generation"] != given["current_generation"])
        if kind == "permission_revoked":
            check(not given["authorized"])
        if kind == "assignment_rollback":
            check(given["assignment_fails"])
        if kind == "completed_retry":
            check(given["status"] == "Completed" and given["existing_tasks"] > 0 and expected["same_result"])
        if kind == "correction":
            check(given["existing_tasks"] > 0 and expected["final_status"] == "Cancelled")
            details = case["correction"]
            require(details["original_task_before"]["status"] == "Completed" and details["original_task_before"] == details["original_task_after"], "correction must preserve completed Task and provenance")
            require(details["quarantined"] and not details["normal_reader_can_read_old_file"] and not details["normal_assignee_can_read_old_task"], "correction quarantine must close File and Task access")
            require(details["replacement_file_is_distinct"] and not details["action_ids_reused"] and details["dispositions_required_before_confirmation"], "correction replacement requires distinct provenance and explicit dispositions")
    require(kinds == set(expected_codes) | {"upload_retry", "confirmation_retry", "completed_retry", "correction", "manual_during_run"}, "missing operation scenario")


def validate_dataset(directory=FIXTURES):
    root = Path(directory).resolve()
    manifest = read_json(root / "manifest.json")
    validate_document(manifest, "manifest.schema.json")
    catalog = read_json(safe_path(root, manifest["catalog"]))
    validate_document(catalog, "support.schema.json", "catalog")
    customers = {c["customer"]: c for c in catalog["customers"]}
    users = {u["user"]: u for u in catalog["users"]}
    require(len(customers) == len(catalog["customers"]), "duplicate customer id")
    require(len(users) == len(catalog["users"]), "duplicate user id")
    sources = read_json(safe_path(root, manifest["sources"]))
    validate_document(sources, "support.schema.json", "sources")
    by_id = {s["case_id"]: s for s in sources["cases"]}
    require(len(by_id) == len(sources["cases"]), "duplicate source case_id")
    ids, hashes, families, action_ids = set(), {}, {}, set()
    for case in manifest["cases"]:
        cid = case["case_id"]
        try:
            require(cid not in ids, "duplicate case_id")
            ids.add(cid)
            require(cid in by_id, "missing source specification")
            source = by_id[cid]
            require(case["source_family"] == source["source_family"], "source family mismatch")
            family = case["source_family"]
            require(family not in families or families[family] == case["split"], "source family crosses evaluation split")
            families[family] = case["split"]
            path = safe_path(root, case["pdf"])
            data = path.read_bytes()
            require(len(data) <= 20 * 1024 * 1024, "PDF exceeds size limit")
            digest = hashlib.sha256(data).hexdigest()
            require(digest == case["sha256"], "SHA-256 mismatch")
            require(digest not in hashes, "duplicate PDF; variants must not cross evaluation split")
            hashes[digest] = case["split"]
            pdf = PdfReader(path, strict=True)
            require(not pdf.is_encrypted, "encrypted PDF")
            require(len(pdf.pages) == case["page_count"], "PDF page count mismatch")
            require(case["generation"] == source["generation"], "generation mismatch")
            require(len(source["pages"]) == case["page_count"], "source page count mismatch")
            source_text = read_json(safe_path(root, case["source_text"]))
            ocr = read_json(safe_path(root, case["ocr"]))
            for pages in (source_text, ocr):
                validate_document(pages, "support.schema.json", "pages")
                require([p["page"] for p in pages["pages"]] == list(range(1, case["page_count"] + 1)), "pages must cover PDF consecutively from 1")
                require(sum(len(p["text"]) for p in pages["pages"]) <= 2000000, "OCR total size limit exceeded")
            require([p["text"] for p in source_text["pages"]] == source["pages"], "source text differs from generation specification")
            if case["generation"] == "raster":
                require(all(not p.extract_text().strip() for p in pdf.pages), "raster scan contains hidden text")
                require(all(not p["text"] for p in ocr["pages"]), "poor scan OCR must not expose clean ground truth")
            else:
                require(all("SYNTHETISCH" in p.extract_text() for p in pdf.pages), "synthetic PDF marking missing")
            require(case["received_date_known"] == (case["received_date"] is not None), "received_date must agree with received_date_known")
            extraction = read_json(safe_path(root, case["extraction"]))
            review = read_json(safe_path(root, case["review"]))
            validate_document(extraction, "extraction.schema.json")
            validate_document(review, "review.schema.json")
            validate_evidence(extraction, ocr["pages"])
            validate_evidence(review, ocr["pages"])
            validate_review(review, extraction)
            annotations = read_json(safe_path(root, case["annotations"]))
            validate_annotations(annotations, case, source_text["pages"], customers)
            require(not (action_ids & {a["action_id"] for a in review["actions"]}), "action_id reused across letters")
            action_ids.update(a["action_id"] for a in review["actions"])
            allowed = set(case["allowed_customers"])
            require(allowed <= customers.keys(), "unknown allowed customer")
            candidates = case["expected"]["candidate_customers"]
            require(len(candidates) == len(set(candidates)), "duplicate candidate")
            require(set(candidates) <= allowed, "inaccessible customer in candidates")
            gold_candidates = annotations["customer_candidates"] if extraction["readability"] != "unreadable" else []
            require(candidates == gold_candidates, "expected candidates differ from semantic gold candidates")
            if review["selected_customer"]:
                require(review["selected_customer"] in allowed, "inaccessible customer selected")
            status = {"confirm": "Completed", "clarification": "Needs Clarification", "draft": "Needs Review"}[review["mode"]]
            require(status == case["expected"]["status"], "expected status differs from review")
            expected_tasks = []
            for action in review["actions"]:
                if review["mode"] != "confirm" or action["decision"] != "accept":
                    continue
                if action["assigned_to"]:
                    user = users.get(action["assigned_to"])
                    require(user and user["enabled"] and user["user_type"] == "System User" and review["selected_customer"] in user["customers"], "invalid assignee or missing Customer access")
                expected_tasks.append({"action_id": action["action_id"], "customer": review["selected_customer"], "assigned_to": action["assigned_to"], "exp_start_date": action["internal_date"], "exp_end_date": action["internal_date"], "external_date": action["external_date"], "needs_planning": action["needs_planning"]})
            require(expected_tasks == case["expected"]["tasks"], "expected tasks differ from confirmed review")
        except (ContractError, OSError, ValueError) as exc:
            raise ContractError(f"{cid}: {exc}") from exc
    require(ids == by_id.keys(), "manifest and sources must contain the same cases")
    for case in manifest["cases"]:
        original = case["expected"]["replaces_case"]
        if original:
            require(original in ids and original != case["case_id"], "invalid replacement link")
            require(by_id[original]["source_family"] == case["source_family"], "replacement must share source family")
    splits = dict(Counter(c["split"] for c in manifest["cases"]))
    require(splits.get("development") == 12 and splits.get("holdout", 0) >= 4, "expected 12 development and at least 4 holdout letters")
    require(16 <= len(ids) <= 50, "dataset must contain 16 to 50 letters")
    validate_scenarios(read_json(safe_path(root, manifest["scenarios"])))
    return {"cases": len(ids), "pages": sum(c["page_count"] for c in manifest["cases"]), "splits": splits}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, default=FIXTURES)
    args = parser.parse_args()
    try:
        result = validate_dataset(args.fixtures)
    except (ContractError, OSError, ValueError) as exc:
        print(f"AP-01 validation failed: {exc}", file=sys.stderr)
        return 1
    print(f"Validated {result['cases']} synthetic letters / {result['pages']} pages; splits={result['splits']}. Runtime acceptance is pending AP-02…AP-10.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
