"""Explicit AP10 synthetic collection only. Never imported by application runtime.

All database writes use existing services. Output journals contain synthetic OCR
and are private acceptance artifacts, never technical logs or provider input.
"""

import hashlib
import io
import json
import multiprocessing
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import frappe
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request


def _tools():
	root = Path(frappe.get_app_path("kanzlei_erp")).resolve().parent
	sys.path.insert(0, str(root / "scripts"))
	import posteingang_evaluation as evaluation

	return root, evaluation


def _write(path, value):
	path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
	temporary = path.with_suffix(".tmp")
	descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
	with os.fdopen(descriptor, "w") as stream:
		json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
		stream.write("\n")
	os.replace(temporary, path)


def _post(method, *args, request=None, **kwargs):
	request = request or Request(EnvironBuilder(method="POST").get_environ())
	with patch.object(frappe.local, "request", request, create=True):
		result = method(*args, **kwargs)
		frappe.db.commit()
		return result


def _worker(site, sites_path, actor, queue_name):
	from kanzlei_erp.tests.test_posteingang_jobs_concurrency import _rq_worker

	# RQ invokes the production job with only run_name. It never receives OCR,
	# prompts, PDF bytes, output paths or credentials as job args/results.
	_rq_worker(site, sites_path, actor, queue_name)


def _catalog(actor, catalog, journal, path):
	from run_posteingang_evaluation import check_actor

	from kanzlei_erp.mandant_identifiers import verify_identifier

	frappe.set_user("Administrator")
	for doctype, field, name, extra in (
		("Customer Group", "customer_group_name", "All Customer Groups", {"is_group": 1}),
		(
			"Customer Group",
			"customer_group_name",
			"Commercial",
			{"parent_customer_group": "All Customer Groups"},
		),
		("Territory", "territory_name", "All Territories", {"is_group": 1}),
	):
		if not frappe.db.exists(doctype, name):
			frappe.get_doc({"doctype": doctype, field: name, **extra}).insert()
	frappe.db.commit()
	if not frappe.db.exists("Address Template", "Germany"):
		frappe.get_doc(
			{
				"doctype": "Address Template",
				"country": "Germany",
				"template": "{{ address_line1 }} {{ pincode }} {{ city }}",
			}
		).insert()
		frappe.db.commit()
	if frappe.db.exists("User", actor) and not journal.get("actor_created"):
		raise ValueError("Use a new synthetic actor for each measurement")
	if not frappe.db.exists("User", actor):
		with patch("frappe.core.doctype.user.user.throttle_user_creation"):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": actor,
					"first_name": "SYNTHETIC AP10 collector",
					"send_welcome_email": 0,
					"roles": [{"role": "Projects User"}, {"role": "Posteingang Operator"}],
				}
			).insert()
		frappe.db.commit()
		journal["actor_created"] = True
		_write(path, journal)
	user = frappe.get_doc("User", actor)
	if not user.enabled or user.user_type != "System User":
		raise ValueError("Synthetic collector actor configuration differs")
	check_actor(journal.get("actor_created"), frappe.get_roles(actor))
	actual_shares = set(
		frappe.get_all("DocShare", filters={"share_doctype": "Customer", "user": actor}, pluck="share_name")
	)
	if actual_shares - set(journal.get("customers", {}).values()):
		raise ValueError("Unexpected Customer scope for collector actor")
	journal.setdefault("customers", {})
	journal.setdefault("addresses", {})
	for row in catalog["customers"]:
		key = row["customer"]
		if key not in journal["customers"]:
			name = f"AP10-{journal['run_id'][:12]}-{key}"
			if frappe.db.exists("Customer", name):
				raise ValueError("Unexpected existing synthetic Customer")
			doc = frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": row["name"],
					"customer_type": "Company",
					"customer_group": "Commercial",
					"territory": "All Territories",
				}
			)
			for identifier in row["identifiers"]:
				doc.append(
					"kanzlei_identifiers",
					{
						"identifier_type": identifier["type"],
						"raw_value": identifier["value"],
						"jurisdiction": identifier.get("jurisdiction"),
						"authority": identifier.get("authority"),
						"other_type": identifier.get("other_type"),
					},
				)
			doc.insert(set_name=name)
			frappe.db.commit()
			journal["customers"][key] = name
			_write(path, journal)
		doc = frappe.get_doc("Customer", journal["customers"][key])
		if doc.customer_name != row["name"] or len(doc.kanzlei_identifiers) != len(row["identifiers"]):
			raise ValueError("Synthetic catalog changed")
		for identifier, expected in zip(doc.kanzlei_identifiers, row["identifiers"], strict=True):
			if (
				identifier.identifier_type != expected["type"]
				or identifier.raw_value != expected["value"]
				or any(
					(identifier.get(k) or None) != (expected.get(k) or None)
					for k in ("jurisdiction", "authority", "other_type")
				)
			):
				raise ValueError("Synthetic identifier changed")
			if not identifier.verified_by:
				doc.reload()
				_post(
					verify_identifier,
					doc.name,
					identifier.name,
					str(doc.modified),
					"Synthetic AP10 catalog specification",
				)
		street, city = row["address"].split(", ", 1)
		zipcode, city = city.split(" ", 1)
		if key not in journal["addresses"]:
			own_links = frappe.get_all(
				"Dynamic Link",
				filters={"parenttype": "Address", "link_doctype": "Customer", "link_name": doc.name},
				pluck="parent",
			)
			if own_links:
				address = frappe.get_doc("Address", own_links[0])
			else:
				address = frappe.get_doc(
					{
						"doctype": "Address",
						"address_title": doc.name,
						"address_type": "Billing",
						"address_line1": street,
						"pincode": zipcode,
						"city": city,
						"country": "Germany",
						"links": [{"link_doctype": "Customer", "link_name": doc.name}],
					}
				).insert()
				frappe.db.commit()
			journal["addresses"][key] = address.name
			_write(path, journal)
		address = frappe.get_doc("Address", journal["addresses"][key])
		if (address.address_line1, address.pincode, address.city) != (street, zipcode, city) or not any(
			link.link_doctype == "Customer" and link.link_name == doc.name for link in address.links
		):
			raise ValueError("Synthetic address changed")
	return journal["customers"]


def _scope(actor, customer_map, allowed):
	frappe.set_user("Administrator")
	for key, name in customer_map.items():
		frappe.share.remove("Customer", name, actor)
		if key in allowed:
			frappe.share.add("Customer", name, actor, read=1, write=1)
	frappe.db.commit()
	frappe.clear_cache(user=actor)
	frappe.set_user(actor)


def collect(args):
	from frappe.utils.background_jobs import generate_qname, get_redis_conn
	from rq import Queue

	from kanzlei_erp import posteingang
	from kanzlei_erp import posteingang_ai as ai
	from kanzlei_erp import posteingang_jobs as jobs
	from kanzlei_erp import posteingang_matching as matching
	from kanzlei_erp.posteingang_setup import DEFAULTS

	root, evaluation = _tools()
	from run_posteingang_evaluation import (
		check_configuration,
		check_invocation,
		check_receipt,
		check_run,
		check_source,
		code_hash,
		resume_action,
	)

	check_invocation(args, frappe.conf)
	if args.site != frappe.local.site:
		raise ValueError("Collector site context differs")
	if (
		not frappe.conf.get("posteingang_acceptance_site")
		or not frappe.local.site.startswith("posteingang-ap10")
		or not args.actor.startswith("ap10-")
		or not args.actor.endswith("@example.invalid")
	):
		raise ValueError("Marked disposable synthetic site required")
	manifest_path = root / "kanzlei_erp/tests/fixtures/posteingang/manifest.json"
	manifest, _gold, _facts, dataset_hash, gold_hash = evaluation.load_dataset(manifest_path)
	cases = [c for c in manifest["cases"] if args.split == "all" or c["split"] == args.split]
	if len(cases) > args.max_documents:
		raise ValueError("Explicit document budget insufficient")
	frappe.set_user("Administrator")
	settings = frappe.get_single("Posteingang Settings")
	fields = [key for key in DEFAULTS if key not in ("analysis_configuration_epoch", "recovery_cursor")]
	baseline = {key: settings.get(key) for key in fields}
	check_configuration(args, settings.as_dict(), len(cases))
	if args.provider == "mistral" and not ai._valid_configuration(settings):
		raise ValueError("Invalid live configuration")
	config = {
		key: settings.get(key)
		for key in (
			"provider",
			"ocr_model",
			"extraction_model",
			"max_file_bytes",
			"max_pages",
			"max_actions",
			"request_timeout_seconds",
		)
	}
	if args.provider == "fake":
		config.update(provider="fake")
	configuration_hash = evaluation.digest(config)
	revision = subprocess.check_output(
		["git", "-c", f"safe.directory={root}", "rev-parse", "HEAD"], cwd=root, text=True
	).strip()
	execution_hash = code_hash(root)
	journal_path = args.output / "journal.json"
	if journal_path.exists():
		if not args.resume:
			raise ValueError("Existing collection requires explicit resume")
		journal = evaluation.read(journal_path)
		expected = {
			"site": frappe.local.site,
			"actor": args.actor,
			"provider": args.provider,
			"dataset_hash": dataset_hash,
			"gold_hash": gold_hash,
			"configuration_hash": configuration_hash,
			"case_ids": [c["case_id"] for c in cases],
			"code_revision": revision[:12] + "+local." + execution_hash[:16],
		}
		if (
			journal.get("return_epoch", journal.get("execution_epoch", journal.get("settings_epoch")))
			!= settings.analysis_configuration_epoch
		):
			raise ValueError("Settings epoch changed before resume")
		if any(journal.get(k) != v for k, v in expected.items()):
			raise ValueError("Resume fingerprint changed")
	else:
		args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
		journal = {
			"run_id": frappe.generate_hash(length=24),
			"site": frappe.local.site,
			"actor": args.actor,
			"provider": args.provider,
			"dataset_hash": dataset_hash,
			"gold_hash": gold_hash,
			"configuration_hash": configuration_hash,
			"case_ids": [c["case_id"] for c in cases],
			"code_revision": revision[:12] + "+local." + execution_hash[:16],
			"started_at": datetime.now(UTC).isoformat(),
			"settings_epoch": settings.analysis_configuration_epoch,
			"entries": {},
			"customers": {},
		}
		_write(journal_path, journal)
	queue_name = "ap10-collect-" + journal["run_id"]
	queue = Queue(generate_qname(queue_name), connection=get_redis_conn())
	try:
		with patch("kanzlei_erp.drive_realtime._deliver"):
			catalog = evaluation.read(manifest_path.parent / manifest["catalog"])
			customer_map = _catalog(args.actor, catalog, journal, journal_path)
		if args.provider == "fake":
			frappe.set_user("Administrator")
			settings = frappe.get_single("Posteingang Settings")
			settings.update(
				{
					"enabled": 1,
					"provider": "fake",
					"external_analysis_enabled": 0,
					"auto_analysis_enabled": 0,
					"max_retries": 0,
				}
			)
			settings.save()
			frappe.db.commit()
		journal["execution_epoch"] = frappe.get_single("Posteingang Settings").analysis_configuration_epoch
		_write(journal_path, journal)
		for case in cases:
			cid = case["case_id"]
			_scope(args.actor, customer_map, set(case["allowed_customers"]))
			entry = journal["entries"].get(cid)
			if entry:
				existing_letter = frappe.get_doc("Incoming Letter", entry["letter"])
				check_source(jobs._snapshot(existing_letter), case)
				if entry.get("observation"):
					continue
			start = time.monotonic()
			if not entry:
				request = Request(
					EnvironBuilder(
						method="POST",
						data={
							"file": (
								io.BytesIO((manifest_path.parent / case["pdf"]).read_bytes()),
								f"{cid}.pdf",
							)
						},
					).get_environ()
				)
				receipt = _post(
					posteingang.receive,
					f"ap10-{journal['run_id']}-{cid}",
					"true" if case["received_date_known"] else "false",
					case["received_date"],
					request=request,
				)
				check_receipt(receipt)
				entry = {"letter": receipt["letter"], "run": None}
				journal["entries"][cid] = entry
				_write(journal_path, journal)
			letter = frappe.get_doc("Incoming Letter", entry["letter"])
			if not entry["run"]:
				if letter.current_run:
					entry["run"] = letter.current_run
				else:
					with patch("frappe.utils.background_jobs.get_queue", return_value=queue):
						queued = _post(
							jobs.queue_analysis,
							letter.name,
							letter.revision,
							f"ap10-{journal['run_id']}-{cid}-analysis",
						)
					entry["run"] = queued["run"]
				_write(journal_path, journal)
			run = frappe.get_doc("Incoming Letter Run", entry["run"])
			entry.setdefault("configuration_epoch", run.configuration_epoch)
			_write(journal_path, journal)
			actual_letter = frappe.get_doc("Incoming Letter", entry["letter"])
			run_data = run.as_dict()
			run_data["configuration"] = evaluation.loads(run.configuration_json)
			run_data["snapshot"] = evaluation.loads(run.source_snapshot_json)
			letter_data = jobs._snapshot(actual_letter) | {
				"name": actual_letter.name,
				"analysis_generation": actual_letter.analysis_generation,
				"received_by": actual_letter.received_by,
			}
			check_source(letter_data, case)
			check_run(run_data, letter_data, args.actor, args.provider, config)
			if (
				actual_letter.current_run != run.name
				or run.configuration_epoch != entry["configuration_epoch"]
			):
				raise ValueError("Run no longer belongs to this measurement")
			action = resume_action(
				run.status, run.attempt_count, run.configuration_epoch, journal["execution_epoch"]
			)
			if action == "dispatch":
				# Resume may recover a durable pre-claim dispatch without creating
				# a new logical run. Running/attempted requests are never resent.
				with patch("frappe.utils.background_jobs.get_queue", return_value=queue):
					jobs._dispatch(run.name)
				worker = multiprocessing.get_context("spawn").Process(
					target=_worker,
					args=(
						frappe.local.site,
						str(Path(frappe.local.sites_path).resolve()),
						args.actor,
						queue_name,
					),
				)
				worker.start()
				worker.join(timeout=settings.worker_timeout_seconds + 30)
				if worker.is_alive():
					worker.kill()
					worker.join(timeout=10)
				frappe.db.rollback()
				run = frappe.get_doc("Incoming Letter Run", entry["run"])
			actual_letter = frappe.get_doc("Incoming Letter", entry["letter"])
			run_data = run.as_dict()
			run_data["configuration"] = evaluation.loads(run.configuration_json)
			run_data["snapshot"] = evaluation.loads(run.source_snapshot_json)
			letter_data = jobs._snapshot(actual_letter) | {
				"name": actual_letter.name,
				"analysis_generation": actual_letter.analysis_generation,
				"received_by": actual_letter.received_by,
			}
			check_source(letter_data, case)
			check_run(run_data, letter_data, args.actor, args.provider, config)
			if (
				actual_letter.current_run != run.name
				or run.configuration_epoch != entry["configuration_epoch"]
			):
				raise ValueError("Run no longer belongs to this measurement")
			if run.status not in ("Succeeded", "Failed", "Cancelled", "Superseded"):
				raise ValueError("Unknown worker outcome: preserve lease/journal; explicit recovery required")
			successful = run.status == "Succeeded"
			match = matching.get_matching_state(entry["letter"])["matching"] if successful else None
			reverse = {v: k for k, v in customer_map.items()}
			observation = {
				"case_id": cid,
				"sha256": actual_letter.content_sha256,
				"letter": entry["letter"],
				"run": run.name,
				"status": "succeeded" if successful else "failed",
				"error_code": None if successful else run.error_code or "ANALYSIS_STOPPED",
				"ocr": {"pages": evaluation.loads(run.ocr_pages_json)} if run.ocr_pages_json else None,
				"extraction": evaluation.loads(run.result_json) if successful else None,
				"matching": {
					"candidates": [reverse[c["customer"]] for c in match["candidates"]],
					"ambiguity": match["ambiguity"],
				}
				if match
				else None,
				"duration_seconds": round(time.monotonic() - start, 6),
				"attempt_count": run.attempt_count,
				"raw_relative_date_violations": 0 if successful else None,
			}
			# OCR checkpoint is a pages list in v1; normalize only its envelope.
			if observation["ocr"] and isinstance(observation["ocr"]["pages"], dict):
				observation["ocr"] = observation["ocr"]["pages"]
			if observation["ocr"]:
				observation["ocr"]["schema_version"] = "1.0"
			entry["observation"] = observation
			_write(journal_path, journal)
		observations = {
			"schema_version": "1.0",
			"run_id": journal["run_id"],
			"dataset_hash": dataset_hash,
			"gold_hash": gold_hash,
			"provider": args.provider,
			"prompt_version": ai.PROMPT_VERSION if args.provider == "mistral" else "fake-v1",
			"code_revision": journal["code_revision"],
			"configuration_hash": configuration_hash,
			"models": {
				"ocr": config["ocr_model"] if args.provider == "mistral" else None,
				"extraction": config["extraction_model"] if args.provider == "mistral" else None,
			},
			"started_at": journal["started_at"],
			"cases": [journal["entries"][c["case_id"]]["observation"] for c in cases],
			"safety": None,
		}
		evaluation.validate(observations, "observations")
		_write(args.output / "observations.json", observations)
		judgment_path = args.output / "judgments.json"
		if not judgment_path.exists():
			judgments = {
				"schema_version": "1.0",
				"run_id": journal["run_id"],
				"observations_hash": evaluation.digest(observations),
				"gold_hash": gold_hash,
				"cases": [
					{
						"case_id": c["case_id"],
						"status": "pending",
						"reviewer": None,
						"adjudicator": None,
						"disputed": False,
						"reason": None,
						"matches": [],
						"facts": [],
						"field_edits": None,
						"actions_edited": None,
					}
					for c in cases
				],
			}
			_write(judgment_path, judgments)
	finally:
		# Never commit a partially failed fixture or mutation during restoration.
		frappe.db.rollback()
		# Remove only our queue's delivery objects; never clear shared Redis.
		queue.delete(delete_jobs=True)
		frappe.set_user("Administrator")
		if args.provider == "fake":
			settings = frappe.get_single("Posteingang Settings")
			settings.update(baseline)
			settings.save()
			frappe.db.commit()
		journal["return_epoch"] = frappe.get_single("Posteingang Settings").analysis_configuration_epoch
		_write(journal_path, journal)
