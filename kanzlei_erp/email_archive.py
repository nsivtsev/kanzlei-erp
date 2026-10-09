"""Read an existing IMAP mailbox into ERPNext's standard Communication timeline."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import html
import re
import sqlite3
from collections import Counter
from contextlib import suppress
from email.header import decode_header, make_header
from email.utils import getaddresses
from pathlib import Path

import frappe
from frappe import _
from frappe.core.api.file import get_max_file_size
from frappe.email.receive import InboundMail
from frappe.utils.html_utils import clean_email_html

_LIST_RE = re.compile(
	rb'^\((?P<flags>[^)]*)\)\s+(?P<delimiter>NIL|"(?:\\.|[^"])*"|[^\s]+)\s+(?P<mailbox>"(?:\\.|[^"])*"|.+)$'
)
_MBOX_SEGMENTS = {
	"drafts",
	"draft",
	"entwuerfe",
	"entwürfe",
	"trash",
	"deleted items",
	"deleted messages",
	"papierkorb",
	"junk",
	"junk email",
	"spam",
	"spamverdacht",
}


def parse_imap_list(response) -> list[dict]:
	"""Decode rows returned by the IMAP LIST command."""
	if isinstance(response, tuple) and len(response) == 2:
		status, rows = response
		if status != "OK":
			frappe.throw(_("The IMAP server could not list mailbox folders."))
	else:
		rows = response

	folders = []
	for row in rows or []:
		if not row:
			continue
		if isinstance(row, str):
			row = row.encode("ascii")
		match = _LIST_RE.match(row)
		if not match:
			continue
		flags = {flag.decode("ascii", "replace").casefold() for flag in match.group("flags").split()}
		mailbox = _unquote_imap_value(match.group("mailbox"))
		folders.append({"name": _decode_imap_utf7(mailbox), "server_name": mailbox, "flags": flags})
	return folders


def should_import_mailbox(folder_name: str, flags: set[str]) -> bool:
	"""Skip system folders by special-use flag or exact path segment."""
	if {"\\noselect", "\\junk", "\\trash", "\\drafts"} & flags:
		return False
	return not any(segment.casefold() in _MBOX_SEGMENTS for segment in re.split(r"[/\\.]", folder_name))


def message_dedupe_key(message_id: str | None, raw_message: bytes) -> str:
	"""Return an account-scoped stable key for mail across IMAP folders."""
	if message_id:
		normalized = message_id.strip().strip("<>").casefold()
		if normalized:
			return f"message-id:{normalized}"
	return f"sha256:{hashlib.sha256(raw_message).hexdigest()}"


def classify_message_direction(sender: str | None, account_email: str, sender_aliases=None) -> str:
	"""Classify archived mail as sent when its From address belongs to the mailbox."""
	aliases = {address.casefold() for _, address in getaddresses([account_email or ""]) if address}
	aliases.update(address.casefold() for address in sender_aliases or [] if address)
	from_addresses = {address.casefold() for _, address in getaddresses([sender or ""]) if address}
	return "Sent" if from_addresses & aliases else "Received"


def _unquote_imap_value(value: bytes) -> str:
	value = value.strip()
	if value.startswith(b'"') and value.endswith(b'"'):
		value = value[1:-1]
	return re.sub(r"\\(.)", r"\1", value.decode("ascii", "replace"))


def _quote_imap_mailbox(value: str) -> str:
	return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _decode_imap_utf7(value: str) -> str:
	def decode_match(match):
		encoded = match.group(1)
		if not encoded:
			return "&"
		padded = encoded.replace(",", "/")
		padded += "=" * (-len(padded) % 4)
		try:
			return base64.b64decode(padded).decode("utf-16-be")
		except (ValueError, UnicodeDecodeError):
			return match.group(0)

	return re.sub(r"&([A-Za-z0-9+,]*)-", decode_match, value)


def _site_archive_dir(account_name: str) -> Path:
	key = hashlib.sha256(account_name.encode("utf-8")).hexdigest()
	directory = Path(frappe.get_site_path("private", "files", "kanzlei_email_archive"))
	directory.mkdir(parents=True, exist_ok=True, mode=0o700)
	with suppress(OSError):
		directory.chmod(0o700)
	return directory / key


class ArchiveJournal:
	"""Private SQLite checkpoint; it stores identifiers and errors, never message bodies."""

	def __init__(self, base_path: Path):
		self.base_path = base_path
		self.lock_file = None
		self.connection = None

	def __enter__(self):
		self.base_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
		lock_path = self.base_path.with_suffix(".lock")
		self.lock_file = open(lock_path, "a+b")
		with suppress(OSError):
			lock_path.chmod(0o600)
		try:
			fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
		except BlockingIOError:
			self.lock_file.close()
			self.lock_file = None
			frappe.throw(_("An archive import is already running for this Email Account."))
		self.connection = sqlite3.connect(self.base_path, timeout=30)
		with suppress(OSError):
			self.base_path.chmod(0o600)
		self.connection.executescript(
			"""
			CREATE TABLE IF NOT EXISTS archive_message (
				dedupe_key TEXT PRIMARY KEY,
				communication TEXT NOT NULL,
				message_id TEXT,
				in_reply_to TEXT,
				direction TEXT NOT NULL
			);
			CREATE TABLE IF NOT EXISTS archive_source (
				folder TEXT NOT NULL,
				uid_validity TEXT NOT NULL,
				uid INTEGER NOT NULL,
				dedupe_key TEXT NOT NULL,
				PRIMARY KEY (folder, uid_validity, uid)
			);
			CREATE TABLE IF NOT EXISTS archive_progress (
				folder TEXT PRIMARY KEY,
				uid_validity TEXT NOT NULL,
				last_uid INTEGER NOT NULL
			);
			CREATE TABLE IF NOT EXISTS archive_failure (
				folder TEXT NOT NULL,
				uid_validity TEXT NOT NULL,
				uid INTEGER NOT NULL,
				message TEXT NOT NULL,
				unsaved_attachments INTEGER NOT NULL DEFAULT 0,
				PRIMARY KEY (folder, uid_validity, uid)
			);
			"""
		)
		columns = {row[1] for row in self.connection.execute("PRAGMA table_info(archive_failure)")}
		if "unsaved_attachments" not in columns:
			self.connection.execute(
				"ALTER TABLE archive_failure ADD COLUMN unsaved_attachments INTEGER NOT NULL DEFAULT 0"
			)
		return self

	def __exit__(self, exc_type, exc_value, traceback):
		if self.connection:
			self.connection.close()
		if self.lock_file:
			with suppress(OSError):
				fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_UN)
			self.lock_file.close()

	def source_communication(self, folder: str, uid_validity: str, uid: int) -> str | None:
		row = self.connection.execute(
			"SELECT dedupe_key FROM archive_source WHERE folder=? AND uid_validity=? AND uid=?",
			(folder, uid_validity, uid),
		).fetchone()
		if not row:
			return None
		communication = self.get_communication_name(row[0])
		if communication and frappe.db.exists("Communication", communication):
			return communication
		self.connection.execute("DELETE FROM archive_source WHERE dedupe_key=?", (row[0],))
		self.connection.execute("DELETE FROM archive_message WHERE dedupe_key=?", (row[0],))
		return None

	def get_communication_name(self, dedupe_key: str) -> str | None:
		row = self.connection.execute(
			"SELECT communication FROM archive_message WHERE dedupe_key=?", (dedupe_key,)
		).fetchone()
		return row[0] if row else None

	def get_message(self, dedupe_key: str):
		return self.connection.execute(
			"SELECT communication, message_id, in_reply_to, direction FROM archive_message WHERE dedupe_key=?",
			(dedupe_key,),
		).fetchone()

	def record_success(
		self,
		folder: str,
		uid_validity: str,
		uid: int,
		dedupe_key: str,
		communication: str,
		message_id: str | None,
		in_reply_to: str | None,
		direction: str,
	):
		self.connection.execute(
			"INSERT OR IGNORE INTO archive_message VALUES (?, ?, ?, ?, ?)",
			(dedupe_key, communication, message_id, in_reply_to, direction),
		)
		self.connection.execute(
			"INSERT OR REPLACE INTO archive_source VALUES (?, ?, ?, ?)",
			(folder, uid_validity, uid, dedupe_key),
		)
		self.connection.execute(
			"DELETE FROM archive_failure WHERE folder=? AND uid_validity=? AND uid=?",
			(folder, uid_validity, uid),
		)

	def clear_failure(self, folder: str, uid_validity: str, uid: int):
		self.connection.execute(
			"DELETE FROM archive_failure WHERE folder=? AND uid_validity=? AND uid=?",
			(folder, uid_validity, uid),
		)

	def record_failure(
		self, folder: str, uid_validity: str, uid: int, reason: str, unsaved_attachments: int = 0
	):
		self.connection.execute(
			"INSERT OR REPLACE INTO archive_failure VALUES (?, ?, ?, ?, ?)",
			(folder, uid_validity, uid, reason[:500], unsaved_attachments),
		)

	def record_progress(self, folder: str, uid_validity: str, uid: int):
		self.connection.execute(
			"INSERT OR REPLACE INTO archive_progress VALUES (?, ?, ?)", (folder, uid_validity, uid)
		)

	def prepare_folder(self, folder: str, uid_validity: str):
		previous = self.connection.execute(
			"SELECT uid_validity FROM archive_progress WHERE folder=?", (folder,)
		).fetchone()
		if previous and previous[0] != uid_validity:
			self.connection.execute(
				"DELETE FROM archive_source WHERE folder=? AND uid_validity!=?", (folder, uid_validity)
			)
			self.connection.execute(
				"DELETE FROM archive_failure WHERE folder=? AND uid_validity!=?", (folder, uid_validity)
			)
		self.record_progress(folder, uid_validity, 0)

	def progress(self):
		return [
			{"folder": folder, "uid_validity": validity, "last_uid": uid}
			for folder, validity, uid in self.connection.execute(
				"SELECT folder, uid_validity, last_uid FROM archive_progress ORDER BY folder"
			)
		]

	def failures(self):
		return [
			{
				"folder": folder,
				"uid_validity": validity,
				"uid": uid,
				"error": error,
				"unsaved_attachments": unsaved_attachments,
			}
			for folder, validity, uid, error, unsaved_attachments in self.connection.execute(
				"SELECT folder, uid_validity, uid, message, unsaved_attachments "
				"FROM archive_failure ORDER BY folder, uid"
			)
		]

	def thread_records(self):
		return list(
			self.connection.execute(
				"SELECT communication, message_id, in_reply_to FROM archive_message WHERE in_reply_to IS NOT NULL"
			)
		)


class ArchiveImportError(Exception):
	"""A mail failure that also reports attachments rejected by configured size limits."""

	def __init__(self, message, unsaved_attachments=0):
		super().__init__(message)
		self.unsaved_attachments = unsaved_attachments


class _ArchiveMail(InboundMail):
	"""Frappe's MIME parser with oversized attachments recorded for the report."""

	def __init__(self, content, email_account, uid, seen_status):
		self.oversized_attachments = []
		super().__init__(content, email_account, uid=uid, seen_status=seen_status)

	def get_attachment(self, part):
		payload = part.get_payload(decode=True)
		if payload:
			file_limit = get_max_file_size()
			account_limit = int(self.email_account.attachment_limit or 0) * 1024 * 1024
			if len(payload) > file_limit or (account_limit and len(payload) > account_limit):
				name = part.get_filename() or "unnamed attachment"
				self.oversized_attachments.append({"name": name, "size": len(payload)})
				return
		super().get_attachment(part)


def _imap_uid_validity(imap) -> str:
	response = imap.response("UIDVALIDITY")
	parts = response[1] if response and len(response) > 1 else []
	for part in parts or []:
		match = re.search(rb"\d+", part if isinstance(part, bytes) else str(part).encode())
		if match:
			return match.group(0).decode("ascii")
	return "0"


def _message_uids(imap) -> list[int]:
	status, data = imap.uid("SEARCH", None, "ALL")
	if status != "OK":
		frappe.throw(_("The IMAP server failed to list messages."))
	return [int(uid) for uid in (data[0] or b"").split()]


def _batches(items: list, size=100):
	for offset in range(0, len(items), size):
		yield items[offset : offset + size]


def _fetch_message_batch(imap, uids: list[int]) -> dict[int, tuple[bytes, int]]:
	"""Read up to 100 messages without changing IMAP Seen flags."""
	if not uids:
		return {}
	status, data = imap.uid("FETCH", ",".join(str(uid) for uid in uids), "(UID BODY.PEEK[] FLAGS)")
	if status != "OK":
		frappe.throw(_("The IMAP server failed to fetch a message batch."))
	messages = {}
	for item in data or []:
		if not isinstance(item, tuple):
			continue
		metadata, raw_message = item
		uid_match = re.search(rb"\bUID\s+(\d+)", metadata, re.I)
		if not uid_match or not (b"BODY[]" in metadata or b"BODY[" in metadata):
			continue
		uid = int(uid_match.group(1))
		seen_status = int(bool(re.search(rb"\\Seen", metadata, re.I)))
		messages[uid] = (raw_message, seen_status)
	return messages


def _open_mailbox(imap, folder_name: str, server_name: str | None = None) -> tuple[str, list[int]]:
	status, _ = imap.select(_quote_imap_mailbox(server_name or folder_name), readonly=True)
	if status != "OK":
		frappe.throw(_("Could not open IMAP folder {0}.").format(folder_name))
	validity = _imap_uid_validity(imap)
	if validity == "0":
		frappe.throw(_("The IMAP server returned no UIDVALIDITY for folder {0}.").format(folder_name))
	return validity, _message_uids(imap)


def _find_existing_communication(account_name: str, message_id: str | None):
	if not message_id:
		return None
	name = frappe.db.get_value(
		"Communication", {"email_account": account_name, "message_id": message_id}, "name"
	)
	return frappe.get_doc("Communication", name) if name else None


def _add_contact_links(communication):
	old_links = {(row.link_doctype, row.link_name) for row in communication.timeline_links}
	communication.flags.in_receive = True
	communication.set_timeline_links()
	communication.deduplicate_timeline_links()
	new_links = {(row.link_doctype, row.link_name) for row in communication.timeline_links}
	if new_links != old_links:
		communication.save(ignore_permissions=True)
	return len(new_links - old_links)


def _decode_address_header(value):
	# Retain Frappe's rejection of malformed encoded addresses.
	if not value or not InboundMail.decode_email(value):
		return ""
	addresses = []
	for name, address in getaddresses([str(value)]):
		name = str(make_header(decode_header(name)))
		if name:
			name = name.replace("\\", "\\\\").replace('"', '\\"')
			addresses.append(f'"{name}" <{address}>')
		else:
			addresses.append(address)
	return ", ".join(addresses)


def _ensure_attachment_bytes(mail, communication, repair=True):
	if mail.oversized_attachments:
		raise ArchiveImportError(
			_("Attachment size limits prevent verifying original email attachments."),
			unsaved_attachments=len(mail.oversized_attachments),
		)
	expected = Counter(hashlib.sha256(part["fcontent"]).hexdigest() for part in mail.attachments)

	def stored_hashes():
		actual = Counter()
		for row in frappe.get_all("File", filters={
			"attached_to_doctype": "Communication", "attached_to_name": communication.name, "is_private": 1,
		}, fields=["name", "file_url"]):
			if not row.file_url or not row.file_url.startswith("/private/files/"):
				continue
			try:
				data = Path(frappe.get_doc("File", row.name).get_full_path()).read_bytes()
			except OSError:
				continue
			actual[hashlib.sha256(data).hexdigest()] += 1
		return actual

	missing = expected - stored_hashes()
	if missing and repair:
		original_parts = mail.attachments
		parts = []
		for part in original_parts:
			digest = hashlib.sha256(part["fcontent"]).hexdigest()
			if missing[digest]:
				parts.append(part)
				missing[digest] -= 1
		try:
			mail.attachments = parts
			mail.save_attachments_in_doc(communication)
		finally:
			mail.attachments = original_parts
		missing = expected - stored_hashes()
	if missing:
		raise ArchiveImportError(
			_("Original attachment bytes are missing or changed; review file processing settings before retrying."),
			unsaved_attachments=sum(missing.values()),
		)


def _import_communication(account, uid, seen_status, raw_message, sender_aliases, in_inbox):
	mail = _ArchiveMail(raw_message, account, uid=uid if in_inbox else -1, seen_status=seen_status)
	if mail.oversized_attachments:
		files = ", ".join(f"{item['name']} ({item['size']} bytes)" for item in mail.oversized_attachments)
		raise ArchiveImportError(
			_("Attachment size limit prevents importing this message: {0}").format(files),
			unsaved_attachments=len(mail.oversized_attachments),
		)

	actual_sender = _decode_address_header(mail.mail.get("X-Original-From") or mail.mail.get("From"))
	sender = _decode_address_header(mail.mail.get("From")) or mail.from_email
	direction = classify_message_direction(actual_sender, account.email_id, sender_aliases)
	dedupe_key = message_dedupe_key(mail.message_id, raw_message)
	content = (
		clean_email_html(mail.content)
		if mail.content_type == "text/html"
		else "<br>".join(html.escape(line) for line in mail.content.splitlines())
	)
	communication = frappe.get_doc(
		{
			"doctype": "Communication",
			"communication_type": "Communication",
			"communication_medium": "Email",
			"sent_or_received": direction,
			"email_account": account.name,
			"message_id": mail.message_id or "",
			"subject": mail.subject,
			"content": content,
			"text_content": mail.text_content,
			"sender": sender,
			"sender_full_name": next(iter(getaddresses([sender or ""])), ("", ""))[0] or mail.from_real_name,
			"recipients": _decode_address_header(mail.mail.get("To")),
			"cc": _decode_address_header(mail.mail.get("CC")),
			"bcc": _decode_address_header(mail.mail.get("BCC")),
			"communication_date": mail.date,
			"has_attachment": int(bool(mail.attachments)),
			"seen": seen_status if direction == "Received" else 1,
			"uid": uid if in_inbox else -1,
			"status": "Open",
			"email_status": "Open",
			"delivery_status": "Sent" if direction == "Sent" else None,
			"unread_notification_sent": 1,
		}
	)
	communication.flags.in_receive = True
	communication.flags.skip_add_signature = True
	communication.insert(ignore_permissions=True)
	files = mail.save_attachments_in_doc(communication)
	for file in files:
		content_id = mail.cid_map.get(file.file_name)
		if content_id:
			communication.content = (communication.content or "").replace(
				f"cid:{content_id}", file.unique_url
			)
	if len(files) != len(mail.attachments):
		raise ArchiveImportError(
			_("Some attachments were not saved for archived message {0}.").format(mail.message_id or uid),
			unsaved_attachments=len(mail.attachments) - len(files),
		)
	_ensure_attachment_bytes(mail, communication, repair=False)
	communication.save(ignore_permissions=True)
	return communication, dedupe_key, direction, mail.in_reply_to


def _link_pending_replies(journal, account_name: str) -> int:
	linked = 0
	for communication_name, _message_id, in_reply_to in journal.thread_records():
		if not in_reply_to:
			continue
		parent_message_id = in_reply_to.strip().strip("<>")
		parent_name = frappe.db.get_value(
			"Communication", {"email_account": account_name, "message_id": parent_message_id}, "name"
		)
		if not parent_name or parent_name == communication_name:
			continue
		current = frappe.db.get_value("Communication", communication_name, "in_reply_to")
		if current != parent_name:
			frappe.db.set_value("Communication", communication_name, "in_reply_to", parent_name, update_modified=False)
			linked += 1
	return linked


def _connect(email_account):
	# Use the standard account configuration, but avoid receive-mode error handling,
	# which can disable the account and send an automatic admin notice on failure.
	server = email_account.get_incoming_server(in_receive=False, email_sync_rule="ALL")
	if not server or not server.settings.use_imap:
		frappe.throw(_("The configured Email Account must use IMAP."))
	server.connect()
	return server


def _get_folders(imap):
	return [folder for folder in parse_imap_list(imap.list()) if should_import_mailbox(folder["name"], folder["flags"])]


def _inbox_first(folder):
	is_inbox = "\\inbox" in folder["flags"] or folder["name"].casefold() == "inbox"
	return (not is_inbox, folder["name"].casefold())


def _safe_close(server):
	if server:
		with suppress(Exception):
			server.logout()


def _preview_message(mail_account, raw_message):
	mail = _ArchiveMail(raw_message, mail_account, uid=-1, seen_status=0)
	attachments = [len(item["fcontent"]) for item in mail.attachments]
	oversized = [item["size"] for item in mail.oversized_attachments]
	return {
		"message_bytes": len(raw_message),
		"attachment_count": len(attachments) + len(oversized),
		"attachment_bytes": sum(attachments) + sum(oversized),
		"oversized_attachments": mail.oversized_attachments,
	}


def _require_archive_account(email_account_name: str, require_incoming=False, require_safe_linking=False):
	if not ({"System Manager", "Super Email User"} & set(frappe.get_roles())):
		frappe.throw(_("Only System Managers may read or import a complete email archive."))
	account = frappe.get_doc("Email Account", email_account_name)
	if not account.use_imap or account.service == "Frappe Mail":
		frappe.throw(_("The archive utility requires an IMAP Email Account."))
	if not (account.use_ssl or account.use_starttls):
		frappe.throw(_("Enable SSL or STARTTLS for the IMAP connection before using the archive utility."))
	if account.create_contact:
		frappe.throw(_("Disable automatic Contact creation before using the email archive utility."))
	if require_safe_linking and frappe.db.exists("Email Account", {"enable_automatic_linking": 1}):
		frappe.throw(_("Disable Enable Automatic Linking in Documents before importing the email archive."))
	if require_incoming and not account.enable_incoming:
		frappe.throw(_("Enable Incoming on this Email Account before previewing its archive."))
	return account


def preview(email_account_name: str) -> dict:
	"""Read all selected mailbox counts and file sizes without writing mail."""
	account = _require_archive_account(email_account_name, require_incoming=True)
	server = _connect(account)
	result = {
		"email_account": account.name,
		"max_file_bytes": get_max_file_size(),
		"account_attachment_limit_mb": int(account.attachment_limit or 0),
		"folders": [],
		"messages": 0,
		"attachments": 0,
		"attachment_bytes": 0,
		"oversized_attachments": 0,
	}
	try:
		for folder in sorted(_get_folders(server.imap), key=_inbox_first):
			validity, uids = _open_mailbox(server.imap, folder["name"], folder["server_name"])
			folder_result = {
				"name": folder["name"],
				"flags": sorted(folder["flags"]),
				"uid_validity": validity,
				"messages": len(uids),
				"messages_bytes": 0,
				"attachments": 0,
				"attachment_bytes": 0,
				"oversized_attachments": 0,
				"oversized_files": [],
			}
			for batch in _batches(uids):
				messages = _fetch_message_batch(server.imap, batch)
				for uid in batch:
					raw_message = messages.get(uid, (b"", 0))[0]
					if not raw_message:
						continue
					stats = _preview_message(account, raw_message)
					folder_result["messages_bytes"] += stats["message_bytes"]
					folder_result["attachments"] += stats["attachment_count"]
					folder_result["attachment_bytes"] += stats["attachment_bytes"]
					folder_result["oversized_attachments"] += len(stats["oversized_attachments"])
					folder_result["oversized_files"].extend(
						{
							"uid": uid,
							"name": attachment["name"],
							"bytes": attachment["size"],
						}
						for attachment in stats["oversized_attachments"]
					)
			result["messages"] += len(uids)
			result["attachments"] += folder_result["attachments"]
			result["attachment_bytes"] += folder_result["attachment_bytes"]
			result["oversized_attachments"] += folder_result["oversized_attachments"]
			result["folders"].append(folder_result)
	finally:
		_safe_close(server)
	return result


def import_archive(email_account_name: str, sender_aliases=None) -> dict:
	"""Import all mail from selected IMAP folders, resuming from a private checkpoint."""
	account = _require_archive_account(email_account_name, require_safe_linking=True)
	if account.enable_incoming:
		frappe.throw(_("Pause automatic email receiving on the Email Account before importing its archive."))
	aliases = [alias.strip().casefold() for alias in sender_aliases or [] if alias and alias.strip()]
	base_path = _site_archive_dir(account.name)
	server = None
	counts = {"imported": 0, "existing": 0, "new_contact_links": 0, "thread_links": 0}
	unlinked = set()
	try:
		with ArchiveJournal(base_path) as journal:
			server = _connect(account)
			for folder in sorted(_get_folders(server.imap), key=_inbox_first):
				validity, uids = _open_mailbox(server.imap, folder["name"], folder["server_name"])
				journal.prepare_folder(folder["name"], validity)
				is_inbox = "\\inbox" in folder["flags"] or folder["name"].casefold() == "inbox"
				for batch in _batches(uids):
					try:
						messages = _fetch_message_batch(server.imap, batch)
					except Exception as error:
						for uid in batch:
							journal.record_failure(folder["name"], validity, uid, str(error))
						journal.connection.commit()
						continue
					for uid in batch:
						frappe.db.savepoint("kanzlei_email_archive_message")
						try:
							raw_message, seen_status = messages.get(uid, (None, 0))
							if raw_message is None:
								raise ArchiveImportError(_("The IMAP server returned no content for UID {0}.").format(uid))
							parsed = _ArchiveMail(raw_message, account, uid=uid if is_inbox else -1, seen_status=seen_status)
							known_communication = journal.source_communication(folder["name"], validity, uid)
							if known_communication:
								communication = frappe.get_doc("Communication", known_communication)
								_ensure_attachment_bytes(parsed, communication)
								counts["existing"] += 1
								counts["new_contact_links"] += _add_contact_links(communication)
								journal.clear_failure(folder["name"], validity, uid)
							else:
								key = message_dedupe_key(parsed.message_id, raw_message)
								existing = journal.get_message(key)
								communication = (
									frappe.get_doc("Communication", existing[0])
									if existing and frappe.db.exists("Communication", existing[0])
									else _find_existing_communication(account.name, parsed.message_id)
								)
								if communication:
									_ensure_attachment_bytes(parsed, communication)
									counts["existing"] += 1
									counts["new_contact_links"] += _add_contact_links(communication)
									message_id = communication.message_id or parsed.message_id
									in_reply_to = parsed.in_reply_to
									direction = communication.sent_or_received
								else:
									communication, key, direction, in_reply_to = _import_communication(
										account, uid, seen_status, raw_message, aliases, is_inbox
									)
									counts["imported"] += 1
									message_id = communication.message_id
								journal.record_success(
									folder["name"],
									validity,
									uid,
									key,
									communication.name,
									message_id,
									in_reply_to,
									direction,
								)
							if not frappe.db.exists(
								"Communication Link", {"parent": communication.name, "link_doctype": "Customer"}
							):
								unlinked.add(communication.name)
							journal.record_progress(folder["name"], validity, uid)
						except Exception as error:
							frappe.db.rollback(save_point="kanzlei_email_archive_message")
							journal.record_failure(
								folder["name"],
								validity,
								uid,
								str(error),
								getattr(error, "unsaved_attachments", 0),
							)
							frappe.log_error(
								title=_("Email archive import failed"),
								message=f"Email Account: {account.name}\nFolder: {folder['name']}\nUID: {uid}\n{error}",
							)
					if batch:
						journal.record_progress(folder["name"], validity, batch[-1])
						# Checkpoint first: a crash before the DB commit is detected by the next run.
						journal.connection.commit()
						frappe.db.commit()
			counts["thread_links"] = _link_pending_replies(journal, account.name)
			frappe.db.commit()
			journal.connection.commit()
			counts["unlinked"] = len(unlinked)
			counts["progress"] = journal.progress()
			counts["failures"] = journal.failures()
			counts["unsaved_attachments"] = sum(
				failure["unsaved_attachments"] for failure in counts["failures"]
			)
			counts["complete"] = not counts["failures"]
	finally:
		_safe_close(server)
	return counts
