"""Read only current, permission-checked objects created by flat Suite Drive."""

import re
import time
from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps

import frappe

from kanzlei_erp import drive_storage, object_storage
from kanzlei_erp.ai_connector.errors import fail

MAX_BYTES = 20 * 1024 * 1024
CONNECT_TIMEOUT = 0.5
READ_TIMEOUT = 0.75
STORAGE_SECONDS = 3.5

_storage = ContextVar("ai_native_drive_storage", default=None)
_FILE_ID = re.compile(r"[A-Za-z0-9_-]{1,140}")
_ETAG = re.compile(r'"[\x21\x23-\x5b\x5d-\x7e]{1,256}"')
_VERSION = re.compile(r"[\x21-\x7e]{1,1024}")


@dataclass
class StorageRequest:
	deadline: float
	connection: tuple | None = field(default=None, repr=False)

	def remaining(self):
		return self.deadline - time.monotonic()


def bounded(function):
	@wraps(function)
	def within_storage_deadline(*args, **kwargs):
		if _storage.get() is not None:
			return function(*args, **kwargs)
		token = _storage.set(StorageRequest(time.monotonic() + STORAGE_SECONDS))
		try:
			return function(*args, **kwargs)
		finally:
			_storage.reset(token)

	return within_storage_deadline


def page_should_yield():
	request = _storage.get()
	return request.connection is not None and request.remaining() <= CONNECT_TIMEOUT + READ_TIMEOUT


def _check_deadline():
	if _storage.get().remaining() <= 0:
		fail("CONNECTOR_UNAVAILABLE", 503)


def is_native(file):
	return str(file.get("file_url") or "").startswith(object_storage.S3_URL_PREFIX)


def in_drive(file, client_id, *, locked=False):
	"""Require a persisted active ancestry under the Customer's actual root."""
	if file.get("kanzlei_drive_customer") != client_id or file.is_folder:
		return False
	root = frappe.db.get_value(
		"Customer", client_id, "kanzlei_drive_folder", for_update=locked, wait=not locked
	)
	parent = file.folder
	seen = set()
	while root and parent and parent not in seen and len(seen) < 256:
		seen.add(parent)
		row = frappe.db.get_value(
			"File",
			parent,
			["folder", "is_folder", "status", "kanzlei_drive_customer"],
			as_dict=True,
			for_update=locked,
			wait=not locked,
		)
		if not row or not row.is_folder or row.status != "Active" or row.kanzlei_drive_customer != client_id:
			return False
		if parent == root:
			return True
		parent = row.folder
	return False


def _client(config):
	import boto3
	from botocore.config import Config

	_check_deadline()
	secret = config.get_password("aws_secret", raise_exception=False)
	if not config.aws_key or not secret:
		fail("CONNECTOR_UNAVAILABLE", 503)
	parameters = (
		config.endpoint_url or None,
		config.aws_key,
		secret,
		config.get("kanzlei_s3_region") or "us-east-1",
		config.get("signature_version") or "s3v4",
		config.get("kanzlei_s3_addressing_style") or "path",
	)
	request = _storage.get()
	if request.connection is None or request.connection[0] != parameters:
		endpoint, key, secret, region, signature, addressing = parameters
		connection = boto3.client(
			"s3",
			endpoint_url=endpoint,
			aws_access_key_id=key,
			aws_secret_access_key=secret,
			region_name=region,
			config=Config(
				signature_version=signature,
				s3={"addressing_style": addressing},
				connect_timeout=CONNECT_TIMEOUT,
				read_timeout=READ_TIMEOUT,
				retries={"total_max_attempts": 1},
			),
		)
		request.connection = parameters, connection
	return request.connection[1]


@contextmanager
def _storage_errors():
	from botocore.exceptions import BotoCoreError, ClientError

	try:
		yield
	except ClientError as error:
		if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") in {404, 412}:
			fail("STALE_SOURCE", 409)
		fail("CONNECTOR_UNAVAILABLE", 503)
	except BotoCoreError, OSError:
		fail("CONNECTOR_UNAVAILABLE", 503)


def _fingerprint(response):
	etag = response.get("ETag")
	size = response.get("ContentLength")
	version = response.get("VersionId")
	if (
		not isinstance(etag, str)
		or not _ETAG.fullmatch(etag)
		or type(size) is not int
		or size < 0
		or (version is not None and (not isinstance(version, str) or not _VERSION.fullmatch(version)))
	):
		fail("CONNECTOR_UNAVAILABLE", 503)
	if size > MAX_BYTES:
		fail("LIMIT_EXCEEDED", 413)
	return etag, size, version


@dataclass(frozen=True)
class NativeObject:
	bucket: str
	key: str
	fingerprint: tuple[str, int, str | None]
	client: object = field(repr=False, compare=False)

	def read(self):
		etag, size, _ = self.fingerprint
		_check_deadline()
		with _storage_errors():
			response = self.client.get_object(Bucket=self.bucket, Key=self.key, IfMatch=etag)
			with closing(response["Body"]) as body:
				_check_deadline()
				if _fingerprint(response) != self.fingerprint:
					fail("STALE_SOURCE", 409)
				content = bytearray()
				while chunk := body.read(min(65536, size - len(content) + 1)):
					_check_deadline()
					content.extend(chunk)
					if len(content) > size:
						fail("STALE_SOURCE", 409)
				_check_deadline()
				if len(content) != size:
					fail("STALE_SOURCE", 409)
				return bytes(content)


def inspect(file, client_id, *, locked=False):
	from suite.drive.utils.downloads import content_kind
	from suite.drive.utils.files import get_s3_url

	if (
		not file.name
		or not _FILE_ID.fullmatch(file.name)
		or file.status != "Active"
		or content_kind(file) != "stored"
		or not in_drive(file, client_id, locked=locked)
	):
		fail("FORBIDDEN", 403)
	config = object_storage.settings()
	if not config.enabled or not config.flat or not config.bucket:
		fail("STALE_SOURCE", 409)
	try:
		root_key = object_storage.prefix(config) + "/drive"
		object_key = object_storage.managed_key(root_key + "/" + file.name, config)
	except frappe.ValidationError:
		fail("STALE_SOURCE", 409)
	# Frappe marks Suite's application-relative fetch URLs as remote; only
	# the exact server-generated URL identifies this File's native object.
	if config.root_folder != root_key or file.file_url != get_s3_url(object_key):
		fail("STALE_SOURCE", 409)
	root = frappe.db.get_value(
		"File",
		"Drive",
		["file_url", "is_folder", "status"],
		as_dict=True,
		for_update=locked,
		wait=not locked,
	)
	if (
		not root
		or not root.is_folder
		or root.status not in (None, "", "Active")
		or root.file_url != get_s3_url(root_key)
	):
		fail("STALE_SOURCE", 409)
	# Verify persisted identity, alias protection and current Mandant access
	# before constructing a client or requesting any object metadata.
	drive_storage.require_storage_access(file)
	with _storage_errors():
		client = _client(config)
		response = client.head_object(Bucket=config.bucket, Key=object_key)
	_check_deadline()
	return NativeObject(config.bucket, object_key, _fingerprint(response), client)
