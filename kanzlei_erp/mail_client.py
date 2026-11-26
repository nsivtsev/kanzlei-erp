"""Permission-aware data for the Kanzlei mail client page.

Mail is shown only for the Email Accounts assigned to the current user in
User Email, exactly like the work overview. Sending, replying and forwarding
reuse Frappe's standard composer and ``communication.email.make``.
"""

import re
from functools import partial

import frappe
from frappe import _
from frappe.core.utils import html_to_plain_text
from frappe.query_builder.functions import Count

FOLDERS = ("inbox", "unassigned", "sent", "spam", "trash")
PAGE_LENGTH = 40
BATCH_SIZE = 100
THREAD_LIMIT = 25
PREVIEW_LENGTH = 160
LIST_FIELDS = (
	"name",
	"subject",
	"sender",
	"sender_full_name",
	"recipients",
	"communication_date",
	"sent_or_received",
	"seen",
	"has_attachment",
	"email_account",
	"email_status",
	"reference_doctype",
	"reference_name",
)


def _require_desk_user():
	user = frappe.session.user
	if user == "Guest" or frappe.get_cached_value("User", user, "user_type") != "System User":
		frappe.throw(_("The mail client is available to Desk users only."), frappe.PermissionError)


def _integer(value, minimum, maximum):
	if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value)):
		frappe.throw(_("Invalid mail pagination."), frappe.ValidationError)
	value = int(value)
	if not minimum <= value <= maximum:
		frappe.throw(_("Invalid mail pagination."), frappe.ValidationError)
	return value


def _flag(value):
	return str(value).lower() in {"1", "true", "yes"}


def _accounts():
	rows = frappe.get_all(
		"User Email",
		filters={"parent": frappe.session.user, "parenttype": "User"},
		fields=["email_account", "email_id", "enable_outgoing"],
		order_by="idx asc",
	)
	accounts = {}
	for row in rows:
		if row.email_account and row.email_account not in accounts:
			accounts[row.email_account] = {
				"name": row.email_account,
				"email_id": row.email_id,
				"can_send": bool(row.enable_outgoing),
			}
	return list(accounts.values())


def _context():
	"""Return (state, accounts). Only `ready` allows reading mail."""
	_require_desk_user()
	if not frappe.has_permission("Communication", "read"):
		return "no_access", []
	accounts = _accounts()
	return ("ready" if accounts else "no_account"), accounts


def _query(folder, account_names, search=None, unread=False, attachments=False):
	mail = frappe.qb.DocType("Communication")
	link = frappe.qb.DocType("Communication Link")
	query = (
		frappe.qb.from_(mail)
		.where(mail.communication_type == "Communication")
		.where(mail.communication_medium == "Email")
		.where(mail.email_account.isin(account_names))
	)
	if folder in ("inbox", "unassigned"):
		query = query.where(mail.sent_or_received == "Received").where(
			mail.email_status.isnull() | mail.email_status.notin(["Spam", "Trash"])
		)
	if folder == "unassigned":
		# Same rule as email_history._assigned: no reference and no non-Contact timeline link.
		linked = (
			frappe.qb.from_(link)
			.select(link.parent)
			.where(link.parenttype == "Communication")
			.where(link.parentfield == "timeline_links")
			.where(link.link_doctype != "Contact")
		)
		query = query.where(mail.reference_doctype.isnull() | (mail.reference_doctype == "")).where(
			mail.name.notin(linked)
		)
	elif folder == "sent":
		query = query.where(mail.sent_or_received == "Sent").where(
			mail.email_status.isnull() | mail.email_status.notin(["Spam", "Trash"])
		)
	elif folder == "spam":
		query = query.where(mail.email_status == "Spam")
	elif folder == "trash":
		query = query.where(mail.email_status == "Trash")
	if unread:
		query = query.where(mail.seen == 0)
	if attachments:
		query = query.where(mail.has_attachment == 1)
	search = " ".join(str(search or "").split())[:120]
	if search:
		pattern = "%" + re.sub(r"([\\%_])", r"\\\1", search) + "%"
		query = query.where(
			mail.subject.like(pattern)
			| mail.sender.like(pattern)
			| mail.sender_full_name.like(pattern)
			| mail.recipients.like(pattern)
		)
	return query, mail


def _count(folder, account_names, unread=False):
	query, mail = _query(folder, account_names, unread=unread)
	return query.select(Count(mail.name)).run()[0][0] or 0


def _preview(content):
	text = " ".join(html_to_plain_text(content or "").split())
	return text if len(text) <= PREVIEW_LENGTH else text[: PREVIEW_LENGTH - 1] + "…"


def _mandants(names):
	"""Map Communication name -> readable Customer rows, without exposing unreadable names."""
	if not names:
		return {}
	pairs = {}
	for row in frappe.get_all(
		"Communication Link",
		filters={
			"parent": ["in", names],
			"parenttype": "Communication",
			"parentfield": "timeline_links",
			"link_doctype": "Customer",
		},
		fields=["parent", "link_name"],
	):
		pairs.setdefault(row.parent, []).append(row.link_name)
	for row in frappe.get_all(
		"Communication",
		filters={"name": ["in", names], "reference_doctype": "Customer"},
		fields=["name", "reference_name"],
	):
		if row.reference_name:
			pairs.setdefault(row.name, []).insert(0, row.reference_name)
	customers = {customer for values in pairs.values() for customer in values}
	labels = {
		row.name: row.customer_name
		for row in frappe.get_all(
			"Customer", filters={"name": ["in", list(customers)]}, fields=["name", "customer_name"]
		)
		if frappe.has_permission("Customer", "read", doc=row.name)
	}
	return {
		name: [
			{"name": customer, "customer_name": labels[customer]}
			for customer in dict.fromkeys(values)
			if customer in labels
		]
		for name, values in pairs.items()
	}


def _row(doc, mandants):
	return {
		"name": doc.name,
		"subject": doc.subject,
		"sender": doc.sender,
		"sender_full_name": doc.sender_full_name,
		"recipients": doc.recipients,
		"communication_date": doc.communication_date,
		"sent_or_received": doc.sent_or_received,
		"seen": bool(doc.seen),
		"has_attachment": bool(doc.has_attachment),
		"email_account": doc.email_account,
		"preview": _preview(doc.content),
		"mandants": mandants.get(doc.name, []),
	}


@frappe.whitelist()
def get_mailboxes():
	state, accounts = _context()
	if state != "ready":
		return {"state": state, "accounts": [], "counts": {}}
	names = [account["name"] for account in accounts]
	return {
		"state": state,
		"accounts": accounts,
		"counts": {
			"inbox": _count("inbox", names, unread=True),
			"unassigned": _count("unassigned", names),
			"spam": _count("spam", names, unread=True),
		},
	}


@frappe.whitelist()
def get_messages(folder="inbox", account=None, search=None, unread=0, attachments=0, start=0, page_length=PAGE_LENGTH):
	if folder not in FOLDERS:
		frappe.throw(_("Unknown mail folder."), frappe.ValidationError)
	start = _integer(start, 0, 100000)
	page_length = _integer(page_length, 1, PAGE_LENGTH)
	state, accounts = _context()
	if state != "ready":
		return {"state": state, "rows": [], "has_more": False}
	names = [row["name"] for row in accounts]
	if account:
		if account not in names:
			frappe.throw(_("This mailbox is not assigned to you."), frappe.PermissionError)
		names = [account]
	query, mail = _query(folder, names, search, _flag(unread), _flag(attachments))
	query = query.select(mail.name).orderby(mail.communication_date, order=frappe.qb.desc).orderby(
		mail.name, order=frappe.qb.desc
	)
	# One extra readable row tells whether another page exists; next_start resumes after the last shown row.
	docs, positions, offset = [], [], start
	while len(docs) <= page_length:
		batch = query.offset(offset).limit(BATCH_SIZE).run(pluck=True)
		for name in batch:
			offset += 1
			if frappe.has_permission("Communication", "read", doc=name):
				docs.append(name)
				positions.append(offset)
				if len(docs) > page_length:
					break
		if len(batch) < BATCH_SIZE:
			break
	has_more = len(docs) > page_length
	docs = docs[:page_length]
	offset = positions[page_length - 1] if has_more else offset
	rows = {
		row.name: row
		for row in frappe.get_all(
			"Communication", filters={"name": ["in", docs]}, fields=[*LIST_FIELDS, "content"]
		)
	} if docs else {}
	mandants = _mandants(docs)
	return {
		"state": state,
		"rows": [_row(rows[name], mandants) for name in docs if name in rows],
		"has_more": has_more,
		"next_start": offset if has_more else None,
	}


def _readable(name):
	state, accounts = _context()
	if state != "ready":
		frappe.throw(_("No mailbox is assigned to you."), frappe.PermissionError)
	doc = frappe.get_doc("Communication", name)
	if (
		doc.communication_type != "Communication"
		or doc.communication_medium != "Email"
		or doc.email_account not in {account["name"] for account in accounts}
		or not frappe.has_permission("Communication", "read", doc=doc)
	):
		frappe.throw(_("This message is not available in your mailboxes."), frappe.PermissionError)
	return doc, accounts


def _thread(doc, account_names):
	"""Messages joined by In Reply To, oldest first, within the user's mailboxes."""
	names, frontier = {doc.name}, [doc.name]
	parent = doc.in_reply_to
	while parent and parent not in names and len(names) < THREAD_LIMIT:
		names.add(parent)
		frontier.append(parent)
		parent = frappe.db.get_value("Communication", parent, "in_reply_to")
	while frontier and len(names) < THREAD_LIMIT:
		children = frappe.get_all(
			"Communication", filters={"in_reply_to": ["in", frontier]}, pluck="name", limit=THREAD_LIMIT
		)
		frontier = [child for child in children if child not in names]
		names.update(frontier)
	if len(names) == 1:
		return []
	rows = frappe.get_all(
		"Communication",
		filters={
			"name": ["in", list(names)],
			"communication_medium": "Email",
			"email_account": ["in", account_names],
		},
		fields=["name", "subject", "sender", "sender_full_name", "communication_date", "sent_or_received", "seen", "content", "email_status"],
		order_by="communication_date asc, creation asc",
	)
	return [
		{
			"name": row.name,
			"subject": row.subject,
			"sender": row.sender,
			"sender_full_name": row.sender_full_name,
			"communication_date": row.communication_date,
			"sent_or_received": row.sent_or_received,
			"seen": bool(row.seen),
			"preview": _preview(row.content),
			"current": row.name == doc.name,
		}
		for row in rows
		if row.email_status != "Trash" or row.name == doc.name
		if frappe.has_permission("Communication", "read", doc=row.name)
	]


def _links(doc):
	links = []
	if doc.reference_doctype and doc.reference_name and doc.reference_doctype != "Customer":
		links.append((doc.reference_doctype, doc.reference_name))
	links += [
		(row.link_doctype, row.link_name)
		for row in doc.timeline_links
		if row.link_doctype not in ("Customer", "Contact")
	]
	result = []
	for doctype, name in dict.fromkeys(links):
		if frappe.has_permission(doctype, "read", doc=name):
			result.append({"doctype": doctype, "name": name, "label": _(doctype)})
	return result


@frappe.whitelist()
def get_message(name):
	doc, accounts = _readable(name)
	attachments = frappe.get_all(
		"File",
		filters={"attached_to_doctype": "Communication", "attached_to_name": doc.name, "is_folder": 0},
		fields=["name", "file_name", "file_url", "file_size", "is_private"],
		order_by="creation asc",
	)
	account = next(row for row in accounts if row["name"] == doc.email_account)
	return {
		"name": doc.name,
		"subject": doc.subject,
		"sender": doc.sender,
		"sender_full_name": doc.sender_full_name,
		"recipients": doc.recipients,
		"cc": doc.cc,
		"bcc": doc.bcc if doc.sent_or_received == "Sent" else None,
		"communication_date": doc.communication_date,
		"sent_or_received": doc.sent_or_received,
		"email_account": doc.email_account,
		"account_email_id": account["email_id"],
		"email_status": doc.email_status or "Open",
		"status": doc.status,
		"seen": bool(doc.seen),
		"content": doc.content or "",
		"attachments": attachments,
		"mandants": _mandants([doc.name]).get(doc.name, []),
		"links": _links(doc),
		"thread": _thread(doc, [row["name"] for row in accounts]),
		"can_write": bool(frappe.has_permission("Communication", "write", doc=doc)),
	}


@frappe.whitelist(methods=["POST"])
def set_seen(names, seen=1):
	"""Mark messages read/unread; IMAP messages also queue the server-side flag."""
	from frappe.email.inbox import create_email_flag_queue

	names = frappe.parse_json(names) if isinstance(names, str) else names
	if not isinstance(names, list) or not 0 < len(names) <= PAGE_LENGTH:
		frappe.throw(_("Choose up to {0} messages.").format(PAGE_LENGTH), frappe.ValidationError)
	seen = 1 if _flag(seen) else 0
	changed = []
	for name in dict.fromkeys(str(value) for value in names):
		doc = _readable(name)[0]
		if int(doc.seen or 0) == seen:
			continue
		if doc.uid and doc.uid != -1:
			create_email_flag_queue(frappe.as_json([doc.name]), "Read" if seen else "Unread")
		else:
			doc.db_set("seen", seen, update_modified=False)
		changed.append(doc.name)
	return {"changed": changed, "seen": bool(seen)}


@frappe.whitelist(methods=["POST"])
def move_to(name, folder):
	"""Move one message to Spam or Trash, or restore it to its regular folder."""
	targets = {"spam": "Spam", "trash": "Trash", "restore": "Open"}
	if folder not in targets:
		frappe.throw(_("Unknown mail folder."), frappe.ValidationError)
	doc = _readable(name)[0]
	frappe.has_permission("Communication", "write", doc=doc, throw=True)
	if folder == "spam" and doc.sent_or_received == "Received":
		from frappe.email.inbox import mark_as_spam

		mark_as_spam(doc.name, doc.sender)
	else:
		doc.db_set("email_status", targets[folder])
	return {"name": doc.name, "email_status": targets[folder]}


@frappe.whitelist(methods=["POST"])
def assign_mandant(name, customer):
	"""Link one message to a Mandant through its timeline, like the contact history import."""
	from kanzlei_erp.ai_connector.events import communication_linked

	doc = _readable(name)[0]
	frappe.has_permission("Communication", "write", doc=doc, throw=True)
	if not frappe.db.exists("Customer", customer):
		frappe.throw(_("Mandant {0} does not exist.").format(customer), frappe.DoesNotExistError)
	frappe.has_permission("Customer", "write", doc=customer, throw=True)
	mail = frappe.get_doc("Communication", doc.name, for_update=True)
	existing = {(row.link_doctype, row.link_name) for row in mail.timeline_links}
	if ("Customer", customer) not in existing:
		mail.add_link("Customer", customer)
		mail.timeline_links[-1].db_insert()
		communication_linked(mail)
		frappe.clear_document_cache("Communication", mail.name)
		frappe.db.after_commit.add(partial(frappe.clear_document_cache, "Communication", mail.name))
	return {"name": mail.name, "mandants": _mandants([mail.name]).get(mail.name, [])}
