"""Opt-in real S3 acceptance fixtures, exclusively for s3-test.localhost.

Run using scripts/verify-s3-erp.py. All secrets remain inside the local container.
"""

import hashlib
import json
import os
import tempfile
import uuid
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import frappe

from kanzlei_erp import object_storage as storage


def _guard():
	if frappe.local.site != "s3-test.localhost" or not frappe.conf.allow_tests:
		raise ValueError("S3 acceptance is restricted to the disposable test site")
	frappe.set_user("Administrator")


def _state_path():
	path = Path(frappe.get_site_path("private", "s3-acceptance"))
	path.mkdir(parents=True, exist_ok=True, mode=0o700)
	return path / "state.json"


def _save(state):
	with open(_state_path(), "w", opener=lambda name, flags: os.open(name, flags, 0o600)) as handle:
		json.dump(state, handle)


def _state():
	return json.loads(_state_path().read_text())


def seed():
	_guard()
	if _state_path().exists():
		return {"state": "seeded", "files": len(_state()["files"])}
	values = dict(
		line.split("=", 1)
		for line in Path("/workspace/.env").read_text().splitlines()
		if "=" in line and not line.startswith("#")
	)
	storage.configure()
	token = uuid.uuid4().hex
	namespace = "tests/s3-test.localhost/acceptance-" + token
	with storage.configuration_change():
		config = storage.settings()
		config.update(
			{
				"enabled": 1,
				"flat": 1,
				"bucket": "stb-golke-test",
				"root_folder": namespace + "/drive",
				"aws_key": values["LOCAL_MINIO_TEST_ACCESS_KEY"],
				"aws_secret": values["LOCAL_MINIO_TEST_SECRET_KEY"],
				"endpoint_url": "http://minio:9000",
				"signature_version": "s3v4",
				"kanzlei_s3_region": "us-east-1",
				"kanzlei_s3_addressing_style": "path",
				"kanzlei_s3_public_endpoint": "http://localhost:9002",
				"kanzlei_s3_prefix": namespace,
			}
		)
		config.save(ignore_permissions=True)
		from suite.drive.utils.files import get_s3_url

		for name in ("Drive", "Users"):
			if frappe.db.exists("File", name):
				frappe.db.set_value(
					"File",
					name,
					"file_url",
					get_s3_url(namespace + "/drive" + ("/Users" if name == "Users" else "")),
				)
	customer = frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": "_S3 Acceptance " + token,
			"customer_type": "Company",
			"customer_group": "Commercial",
			"territory": "All Territories",
		}
	).insert()
	from unittest.mock import patch

	users = {}
	with patch("frappe.core.doctype.user.user.throttle_user_creation"):
		for label in ("reader", "stranger"):
			user = frappe.get_doc(
				{
					"doctype": "User",
					"email": "s3-" + label + "-" + token + "@example.invalid",
					"first_name": "S3 " + label,
					"send_welcome_email": 0,
					"roles": [{"role": "Projects User"}],
				}
			).insert()
			users[label] = user.name
	frappe.share.add("Customer", customer.name, users["reader"], read=1, write=1)
	files = {}
	for label, private, data in (
		("private", 1, b"private S3 original"),
		("public", 0, b"public S3 original"),
		("image", 1, b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>'),
	):
		file = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": label + (".svg" if label == "image" else ".txt"),
				"is_private": private,
				"content": data,
				"attached_to_doctype": "Customer",
				"attached_to_name": customer.name,
			}
		).insert()
		files[label] = {"name": file.name, "url": file.file_url, "sha256": hashlib.sha256(data).hexdigest()}
	from suite.drive.utils import create_drive_file, get_file_type, get_root_folder
	from suite.drive.utils.files import FileManager

	root = get_root_folder()
	manager = FileManager()
	data = b"native S3 original"
	native = create_drive_file(
		"native.txt",
		root.name,
		get_file_type("text/plain"),
		lambda row: get_s3_url(str(manager.get_disk_path(row))),
		"text/plain",
		len(data),
	)
	with tempfile.TemporaryDirectory() as directory:
		source = Path(directory) / "native.txt"
		source.write_bytes(data)
		manager.upload_file(str(source), native, create_thumbnail=False)
	legacy = "/private/files/native-legacy-" + token + ".txt"
	storage.bind(legacy, storage.file_key(native), len(data), hashlib.sha256(data).hexdigest(), "text/plain")
	files["native"] = {
		"name": native.name,
		"url": native.file_url,
		"legacy": legacy,
		"sha256": hashlib.sha256(data).hexdigest(),
	}
	state = {"files": files, "users": users, "customer": customer.name, "prefix": namespace}
	_save(state)
	frappe.db.commit()
	return {"state": "seeded", "files": len(files)}


def smoke():
	_guard()
	state = _state()
	from werkzeug.exceptions import Forbidden

	from kanzlei_erp.mandant_documents import download_document, download_documents_zip
	from kanzlei_erp.s3_http import authorize

	for item in state["files"].values():
		file = frappe.get_doc("File", item["name"])
		assert hashlib.sha256(file.get_content(encodings=[])).hexdigest() == item["sha256"]
		with storage.local_path(file) as path:
			assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]
		assert not path.exists()
	private = frappe.get_doc("File", state["files"]["private"]["name"])
	key = storage.file_key(private)
	for user in ("Guest", state["users"]["stranger"]):
		frappe.set_user(user)
		try:
			authorize(private.file_url, key)
		except Forbidden:
			pass
		else:
			raise AssertionError("Unauthorized private URL was readable")
	frappe.set_user(state["users"]["reader"])
	assert authorize(private.file_url, key).name == private.name
	download_document(state["customer"], private.name)
	assert (
		hashlib.sha256(frappe.local.response.filecontent).hexdigest() == state["files"]["private"]["sha256"]
	)
	download_documents_zip(state["customer"], [private.name])
	with ZipFile(BytesIO(frappe.local.response.filecontent)) as archive:
		assert archive.read(archive.namelist()[0]) == b"private S3 original"
	frappe.set_user("Administrator")
	from frappe.email.email_body import get_filecontent_from_path

	assert (
		hashlib.sha256(get_filecontent_from_path(state["files"]["image"]["url"])).hexdigest()
		== state["files"]["image"]["sha256"]
	)
	# Exercise the real WebDAV generation transaction for an adopted attachment.
	from suite.drive.utils.files import FileManager
	from suite.drive.webdav.put import _stage_blob_swap

	manager = FileManager()
	data = b"updated mapped WebDAV original"
	with tempfile.TemporaryDirectory() as directory:
		scratch = Path(directory) / "body.txt"
		scratch.write_bytes(data)
		assert _stage_blob_swap(manager, private, scratch, None) == private.file_url
		private.db_set("file_size", len(data))
		frappe.db.commit()
	assert frappe.get_doc("File", private.name).get_content(encodings=[]) == data
	state["files"]["private"]["sha256"] = hashlib.sha256(data).hexdigest()
	# A native generation must update retained legacy URL mappings too.
	native = frappe.get_doc("File", state["files"]["native"]["name"])
	data = b"updated native WebDAV original"
	with tempfile.TemporaryDirectory() as directory:
		scratch = Path(directory) / "body.txt"
		scratch.write_bytes(data)
		url = _stage_blob_swap(manager, native, scratch, None)
		with manager.storage_binding(native, url):
			native.db_set({"file_url": url, "file_size": len(data)})
		frappe.db.commit()
	assert frappe.get_doc("File", native.name).get_content(encodings=[]) == data
	assert storage.get_binding(state["files"]["native"]["legacy"]).object_key == storage.file_key(native)
	state["files"]["native"].update(url=url, sha256=hashlib.sha256(data).hexdigest())
	_save(state)
	frappe.db.commit()
	return {
		"checks": [
			"bytes",
			"temporary cleanup",
			"Guest/stranger denial",
			"reader access",
			"document/ZIP",
			"email inline",
			"WebDAV mapped/native",
			"legacy aliases",
		]
	}


def verify_restored():
	_guard()
	state = _state()
	for item in state["files"].values():
		file = frappe.get_doc("File", item["name"])
		assert hashlib.sha256(file.get_content(encodings=[])).hexdigest() == item["sha256"]
	assert frappe.db.exists("Customer", state["customer"])
	assert frappe.db.get_value("Customer", state["customer"], "customer_name").startswith("_S3 Acceptance ")
	return {"restored_files": len(state["files"]), "prefix": state["prefix"]}


def damage():
	_guard()
	state = _state()
	config = storage.settings()
	for item in state["files"].values():
		file = frappe.get_doc("File", item["name"])
		storage.client().delete_object(Bucket=config.bucket, Key=storage.file_key(file))
	frappe.db.set_value("Customer", state["customer"], "customer_name", "INTENTIONALLY DAMAGED TEST SNAPSHOT")
	frappe.db.commit()
	return {"damaged_test_files": len(state["files"])}


def thumbnail():
	_guard()
	from PIL import Image
	from suite.drive.utils import create_drive_file, get_file_type, get_root_folder
	from suite.drive.utils.files import FileManager, get_s3_url

	manager = FileManager()
	with tempfile.TemporaryDirectory() as directory:
		path = Path(directory) / "image.png"
		Image.new("RGB", (32, 32), "blue").save(path)
		file = create_drive_file("thumbnail.png", get_root_folder().name, get_file_type("image/png"),
			lambda row: get_s3_url(str(manager.get_disk_path(row))), "image/png", path.stat().st_size)
		manager.upload_file(str(path), file, create_thumbnail=True)
		meta = storage.client().head_object(Bucket=storage.settings().bucket, Key=str(manager.get_thumbnail_path(file.name)))
		assert meta["ContentLength"] > 0
	frappe.db.commit()
	return {"s3_thumbnail": "verified", "bytes": meta["ContentLength"]}
