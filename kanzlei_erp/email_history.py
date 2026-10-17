"""Add explicit, unambiguous links to stored email without re-saving its contents."""

from email.utils import getaddresses
from functools import partial
from uuid import uuid4

import frappe

BATCH_SIZE = 200
ADDRESS_FIELDS = ("sender", "recipients", "cc", "bcc")
FAILURE_MESSAGE = "Email history linking did not finish. Use Link old correspondence on the contact to retry."


def addresses(values):
	return {address.strip().casefold() for _, address in getaddresses([value for value in values if value]) if address and "@" in address}


def _contact_state(contact):
	return (
		addresses(row.email_id for row in contact.get("email_ids", [])),
		{row.link_name for row in contact.get("links", []) if row.link_doctype == "Customer" and row.link_name},
	)


def _require_staff():
	user = frappe.db.get_value("User", frappe.session.user, ["enabled", "user_type"], as_dict=True)
	if frappe.session.user == "Guest" or not user or not user.enabled or user.user_type != "System User":
		frappe.throw(frappe._("Please log in as a staff user"), frappe.PermissionError)


def _require_contact(name, *, for_update=False):
	_require_staff()
	contact = frappe.get_doc("Contact", name, for_update=for_update)
	contact.check_permission("write")
	return contact


def _notify(actor, message, *, failed=False, after_commit=False):
	try:
		language = frappe.db.get_value("User", actor, "language")
		frappe.publish_realtime("msgprint", {
			"message": frappe._(message, lang=language),
			"title": frappe._("Email history", lang=language),
			"indicator": "red" if failed else "green",
		}, user=actor, after_commit=after_commit)
	except Exception:
		frappe.logger("email_history").exception("Could not deliver email history notification")


def _enqueue(contact, actor, job_id):
	try:
		frappe.enqueue("kanzlei_erp.email_history.link_contact_history", queue="long", timeout=1500,
			contact=contact, actor=actor, job_id=job_id)
	except Exception:
		try:
			frappe.log_error(title="Could not enqueue email history linking", defer_insert=True)
		except Exception:
			frappe.logger("email_history").exception("Could not persist email history queue failure")
		_notify(actor, FAILURE_MESSAGE, failed=True)


def queue_history(contact):
	actor = frappe.session.user
	job_id = "contact-email-history-" + uuid4().hex
	# A distinct job for every save prevents an update during a running job from being lost.
	frappe.db.after_commit.add(partial(_enqueue, contact.name, actor, job_id))
	frappe.msgprint(frappe._("Email history linking will start after the contact is saved."), alert=True)
	return {"job_id": job_id}


def contact_updated(doc, method=None):
	current = _contact_state(doc)
	old = doc.get_doc_before_save()
	if not all(current) or (old and current == _contact_state(old)):
		return
	# Imports or system-created contacts must not grant access on behalf of a non-staff user.
	try:
		_require_contact(doc.name)
	except frappe.PermissionError:
		return
	queue_history(doc)


@frappe.whitelist(methods=["POST"])
def enqueue_contact_history(contact):
	doc = _require_contact(contact)
	if not all(_contact_state(doc)):
		frappe.throw(frappe._("Add an email address and a Mandant link to the contact first."))
	return queue_history(doc)


def _matching_contacts(email_addresses):
	if not email_addresses:
		return []
	placeholders = ",".join(["%s"] * len(email_addresses))
	return frappe.db.sql(f"""
		SELECT DISTINCT c.name AS contact, d.link_name AS customer
		FROM `tabContact Email` e
		JOIN `tabContact` c ON c.name=e.parent AND e.parenttype='Contact' AND e.parentfield='email_ids'
		JOIN `tabDynamic Link` d ON d.parent=c.name AND d.parenttype='Contact'
			AND d.parentfield='links' AND d.link_doctype='Customer'
		JOIN `tabCustomer` customer ON customer.name=d.link_name
		WHERE LOWER(TRIM(e.email_id)) IN ({placeholders})
	""", tuple(sorted(email_addresses)), as_dict=True)


def _assigned(mail):
	if mail.reference_doctype or mail.reference_name:
		return True
	if any(link.link_doctype != "Contact" for link in mail.timeline_links):
		return True
	# FiBu/Task references can exist without a Communication timeline link.
	file_names = frappe.get_all("File", filters={"attached_to_doctype": "Communication",
		"attached_to_name": mail.name}, pluck="name")
	for doctype, mail_field, file_field in (
		("FiBu Message Link", "communication", None),
		("FiBu File Link", "source_communication", "file"),
		("FiBu Checklist Evidence", "communication", "file"),
		("FiBu Transfer Entry", "evidence_communication", "evidence_file"),
		("FiBu Question Event", "communication", None),
	):
		if frappe.db.exists(doctype, {mail_field: mail.name}):
			return True
		if file_field and file_names and frappe.db.exists(doctype, {file_field: ["in", file_names]}):
			return True
	return False


def link_communication(name, contact=None):
	"""Link one stored message in the caller's transaction; never commit or send mail."""
	_require_staff()
	current_contact = _require_contact(contact, for_update=True) if contact else None
	mail = frappe.get_doc("Communication", name, for_update=True)
	if mail.communication_type != "Communication" or mail.communication_medium != "Email":
		return "skipped", 0
	if not all(frappe.has_permission("Communication", ptype=ptype, doc=mail) for ptype in ("read", "write")):
		return "skipped", 0
	if _assigned(mail):
		return "skipped", 0
	participants = addresses(mail.get(field) for field in ADDRESS_FIELDS)
	if current_contact:
		contact_addresses, contact_customers = _contact_state(current_contact)
		if not participants.intersection(contact_addresses) or not contact_customers:
			return "skipped", 0
	matches = _matching_contacts(participants)
	customers = {row.customer for row in matches}
	# Never report ambiguity for a message whose potential target the actor cannot edit.
	eligible = [customer for customer in customers
		if (not current_contact or customer in contact_customers)
		and frappe.has_permission("Customer", ptype="write", doc=customer)]
	if not eligible:
		return "skipped", 0
	if len(customers) != 1:
		return "ambiguous", 0
	customer = eligible[0]
	links = {("Contact", row.contact) for row in matches if row.customer == customer
		and frappe.has_permission("Contact", ptype="read", doc=row.contact)}
	links.add(("Customer", customer))
	existing = {(row.link_doctype, row.link_name) for row in mail.timeline_links}
	for doctype, docname in sorted(links - existing):
		mail.add_link(doctype, docname)
		mail.timeline_links[-1].db_insert()
	frappe.clear_document_cache("Communication", name)
	frappe.db.after_commit.add(partial(frappe.clear_document_cache, "Communication", name))
	return "linked", len(links - existing)


def _candidates(email_addresses, after, snapshot):
	conditions = []
	params = {"after": after, "snapshot": snapshot, "limit": BATCH_SIZE}
	for index, address in enumerate(sorted(email_addresses)):
		key = f"email{index}"
		params[key] = "%" + address.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
		conditions.extend(f"LOWER(`{field}`) LIKE %({key})s" for field in ADDRESS_FIELDS)
	if not conditions:
		return []
	return frappe.db.sql(f"""SELECT name FROM `tabCommunication`
		WHERE communication_type='Communication' AND communication_medium='Email'
		AND name > %(after)s AND creation <= %(snapshot)s AND ({' OR '.join(conditions)})
		ORDER BY name LIMIT %(limit)s""", params, pluck=True)


def link_contact_history(contact, actor):
	"""Worker: each completed batch survives a later failure; the caller identity is rechecked."""
	previous_user = frappe.session.user
	counts = {"linked": 0, "ambiguous": 0}
	try:
		frappe.set_user(actor)
		doc = _require_contact(contact)
		email_addresses, _ = _contact_state(doc)
		snapshot = frappe.utils.now_datetime()
		after = ""
		while names := _candidates(email_addresses, after, snapshot):
			for attempt in range(3):
				batch_counts = {"linked": 0, "ambiguous": 0}
				try:
					frappe.set_user(actor)
					for name in names:
						status, _ = link_communication(name, contact=contact)
						if status in batch_counts:
							batch_counts[status] += 1
					frappe.db.commit()
					break
				except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
					frappe.db.rollback()
					if attempt == 2:
						raise
			for key, count in batch_counts.items():
				counts[key] += count
			after = names[-1]
		language = frappe.db.get_value("User", actor, "language")
		message = frappe._("Email history: {0} messages linked; {1} ambiguous messages skipped.", lang=language).format(
			counts["linked"], counts["ambiguous"])
		frappe.db.commit()
		_notify(actor, message)
		return counts
	except Exception:
		frappe.db.rollback()
		_notify(actor, FAILURE_MESSAGE, failed=True)
		raise
	finally:
		frappe.set_user(previous_user)
