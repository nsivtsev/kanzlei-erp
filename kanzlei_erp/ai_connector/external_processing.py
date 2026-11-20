"""Online Mandant policy for sending data to external AI providers."""

from kanzlei_erp.ai_connector import auth, inventory, native_drive, policy
from kanzlei_erp.ai_connector.errors import fail

OPERATIONS = ("ocr", "embed", "generate")


def check(operation, scope, sources, access_token):
	if operation not in OPERATIONS or not isinstance(sources, list) or len(sources) > policy.MAX_BATCH:
		fail()
	references = [policy.reference(source) for source in sources]
	with inventory.current_permission_context():
		if access_token is not None:
			with auth.principal(access_token) as identity:
				inventory.refresh_principal_permissions(identity["subject"])
				_check_current(operation, scope, references)
		else:
			user, clients, _ = inventory.settings()
			inventory.refresh_principal_permissions(user)
			with inventory.principal(user):
				_check_current(operation, scope, references, background_clients=clients)
	return {"version": 1, "allowed": True}


@native_drive.bounded
def _check_current(operation, scope, references, *, background_clients=None):
	scope_check = policy.check_scope(scope)
	if not _version_one(scope_check) or scope_check.get("allowed") is not True:
		fail("FORBIDDEN", 403)
	client_id = scope.get("client_id")
	if background_clients is not None and (scope["kind"] != "client" or client_id not in background_clients):
		fail("FORBIDDEN", 403)
	if not references and (
		background_clients is not None or operation != "embed" or scope["kind"] != "client"
	):
		# A question in general scope has no single Mandant to grant processing.
		fail("FORBIDDEN", 403)
	client_ids = {source["client_id"] for source in references}
	if scope["kind"] == "client":
		if client_ids - {client_id}:
			fail("FORBIDDEN", 403)
		client_ids.add(client_id)
	grants = auth.config().get("external_processing")
	for customer in client_ids:
		operations = grants.get(customer) if isinstance(grants, dict) else None
		if not isinstance(operations, dict) or operations.get(operation) is not True:
			fail("FORBIDDEN", 403)
	if references:
		_require_sources(scope, references)
	if background_clients is not None:
		# Ingestion only reads from the fixed identity's managed Drive projection.
		for reference in references:
			current = inventory.project(reference["source_id"][5:], client_id, background_clients)
			if not current or current.get("revision") != reference["revision"]:
				fail("FORBIDDEN", 403)


def _require_sources(scope, references):
	result = policy.authorize_sources(scope, references)
	decisions = result.get("decisions") if _version_one(result) else None
	if not isinstance(decisions, list) or len(decisions) != len(references):
		fail("FORBIDDEN", 403)
	for decision, reference in zip(decisions, references, strict=True):
		if (
			not isinstance(decision, dict)
			or decision.get("allowed") is not True
			or decision != {**reference, "allowed": True}
		):
			fail("FORBIDDEN", 403)


def _version_one(result):
	return isinstance(result, dict) and type(result.get("version")) is int and result["version"] == 1
