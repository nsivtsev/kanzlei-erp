"""Store completed turns and render history under current, complete authorization."""

from threading import Lock
from time import monotonic
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError, model_validator
from sqlalchemy import select, tuple_

from kanzlei_ai.chat.assistant import (
    ERP_CLIENT_LIMIT,
    MEMORY_DEPENDENCY_LIMIT,
    STORED_DEPENDENCY_LIMIT,
    AssistantAnswer,
    Memory,
    strip_markers,
)
from kanzlei_ai.chat.citations import VerifiedAnswer, render_answer
from kanzlei_ai.chat.context import (
    CONTEXT_LIMIT,
    Dependency,
    Evidence,
    Session,
    SourceKind,
    checked_evidence,
    configure_query,
    current_evidence,
    database_call,
)
from kanzlei_ai.connectors.base import ConnectorError, ERPConnector
from kanzlei_ai.connectors.contracts import Identifier, Identity, Scope, SourceRef, WorkQuery
from kanzlei_ai.models import chat_conversations, chat_turns
from kanzlei_ai.search import retrieve as search_retrieve

HISTORY_LIMIT = 10
CONVERSATION_LIMIT = 100
MEMORY_TURNS = 6
MEMORY_CHARACTERS = 24_000
MEMORY_ANSWER_CHARACTERS = 8_000
MEMORY_ERP_CLIENTS = 10
ASSISTANT_SCOPE = "assistant"
_dependencies = TypeAdapter(list[Dependency])
_clients = TypeAdapter(list[Identifier])


class StoredCitation(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    id: str
    chunk_id: str
    page: int | None
    title: str
    source_kind: SourceKind = "pdf"

    @model_validator(mode="after")
    def check_page(self):
        if self.source_kind == "pdf":
            if self.page is None or self.page < 1:
                raise ValueError("PDF citations require a positive page")
        elif self.page is not None:
            raise ValueError("Text citations have no page")
        return self


class TurnCommit:
    """The final checkpoint starts commit. Later cancellation leaves commit to finish."""

    def __init__(self, timeout: float):
        self._deadline = monotonic() + timeout
        self._lock = Lock()
        self._cancelled = False

    def remaining(self) -> float:
        return self._deadline - monotonic()

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True

    def checkpoint(self) -> None:
        with self._lock:
            if self._cancelled or self.remaining() <= 0:
                raise ConnectorError("CHAT_UNAVAILABLE", 503)


def _owner_filter(connector_id, subject):
    return (
        chat_conversations.c.connector_id == connector_id,
        chat_conversations.c.subject == subject,
    )


async def current_identity(connector: ERPConnector, token: str) -> Identity:
    identity = await connector.identity(token)
    if not isinstance(identity, Identity):
        raise ConnectorError()
    return identity


async def verify_session(session: Session) -> None:
    identity = await current_identity(session.connector, session.token)
    if (identity.connector_id, identity.subject) != (session.connector_id, session.subject):
        raise ConnectorError("UNAUTHENTICATED", 401)
    if session.scope is not None:
        await session.connector.check_scope(session.token, session.scope)


def _conversation_filter(connector_id, subject, conversation_id):
    return (*_owner_filter(connector_id, subject), chat_conversations.c.id == conversation_id)


def _require_conversation(connection, connector_id, subject, conversation_id, *, lock=False):
    statement = select(chat_conversations.c.scope_kind, chat_conversations.c.client_id).where(
        *_conversation_filter(connector_id, subject, conversation_id)
    )
    if lock:
        statement = statement.with_for_update(read=True, key_share=True)
    row = connection.execute(statement).one_or_none()
    if row is None:
        raise ConnectorError("NOT_FOUND", 404)
    return row


def lookup(engine, connector_id, subject, conversation_id) -> Scope | None:
    """Return the conversation's document scope; None for an assistant without documents."""
    with engine.begin() as connection:
        configure_query(connection)
        row = _require_conversation(connection, connector_id, subject, conversation_id)
        if row.scope_kind == ASSISTANT_SCOPE:
            return None
        return Scope(kind=row.scope_kind, client_id=row.client_id)


def _conversation_view(row):
    view = {"id": row.id, "client_id": row.client_id, "created_at": row.created_at.isoformat()}
    if row.scope_kind in ("general", ASSISTANT_SCOPE):
        view["scope"] = {"kind": row.scope_kind}
    return view


def create_conversation(engine, session: Session):
    with engine.begin() as connection:
        configure_query(connection)
        row = connection.execute(
            chat_conversations.insert()
            .values(
                id=str(uuid4()),
                connector_id=session.connector_id,
                subject=session.subject,
                client_id=session.scope.client_id if session.scope else None,
                scope_kind=session.scope.kind if session.scope else ASSISTANT_SCOPE,
            )
            .returning(chat_conversations)
        ).one()
    return _conversation_view(row)


def list_conversations(engine, session: Session):
    with engine.begin() as connection:
        configure_query(connection)
        rows = connection.execute(
            select(chat_conversations)
            .where(
                *_owner_filter(session.connector_id, session.subject),
                chat_conversations.c.scope_kind
                == (session.scope.kind if session.scope else ASSISTANT_SCOPE),
                chat_conversations.c.client_id
                == (session.scope.client_id if session.scope else None),
            )
            .order_by(chat_conversations.c.created_at.desc(), chat_conversations.c.id.desc())
            .limit(CONVERSATION_LIMIT)
        )
        return [_conversation_view(row) for row in rows]


def list_for_management(engine, connector_id, subject, cursor):
    table = chat_conversations.c
    owner = _owner_filter(connector_id, subject)
    with engine.begin() as connection:
        configure_query(connection)
        statement = select(table.id, table.created_at, table.scope_kind).where(*owner)
        if cursor is not None:
            anchor = connection.execute(
                select(table.created_at).where(*owner, table.id == cursor)
            ).scalar_one_or_none()
            if anchor is None:
                raise ConnectorError("INVALID_REQUEST", 422)
            statement = statement.where(tuple_(table.created_at, table.id) < (anchor, cursor))
        rows = connection.execute(
            statement.order_by(table.created_at.desc(), table.id.desc()).limit(
                CONVERSATION_LIMIT + 1
            )
        ).all()
    page = rows[:CONVERSATION_LIMIT]
    return {
        "items": [
            {"id": row.id, "created_at": row.created_at.isoformat(), "scope_kind": row.scope_kind}
            for row in page
        ],
        "next_cursor": page[-1].id if len(rows) > CONVERSATION_LIMIT else None,
    }


def delete_conversation(engine, connector_id, subject, conversation_id):
    with engine.begin() as connection:
        configure_query(connection)
        deleted = connection.execute(
            chat_conversations.delete().where(
                *_conversation_filter(connector_id, subject, conversation_id)
            )
        )
        if not deleted.rowcount:
            raise ConnectorError("NOT_FOUND", 404)


def store_turn(
    engine,
    session: Session,
    conversation_id,
    question,
    context: tuple[Evidence, ...],
    answer: VerifiedAnswer | AssistantAnswer | None,
    commit: TurnCommit,
):
    commit.checkpoint()
    dependencies = [evidence.dependency for evidence in context]
    assistant = isinstance(answer, AssistantAnswer)
    erp_clients = sorted(answer.erp_clients) if assistant else []
    cited = answer.cited if assistant else (answer.evidence,) if answer is not None else ()
    citations = [
        StoredCitation(
            id=str(uuid4()),
            chunk_id=evidence.dependency.chunk_id,
            page=evidence.page,
            title=evidence.title,
            source_kind=evidence.source_kind,
        )
        for evidence in cited
    ]
    statement = (
        chat_turns.insert()
        .values(
            id=str(uuid4()),
            conversation_id=conversation_id,
            status="answered" if answer is not None else "no_evidence",
            question=question,
            answer=(
                answer.text
                if assistant
                else render_answer(question, answer)
                if answer is not None
                else None
            ),
            citations=[citation.model_dump() for citation in citations],
            dependencies=_dependencies.dump_python(dependencies, mode="json"),
            mode="assistant" if assistant else "strict",
            erp_clients=erp_clients,
        )
        .returning(chat_turns)
    )
    with engine.begin() as connection:
        configure_query(connection)
        _require_conversation(
            connection, session.connector_id, session.subject, conversation_id, lock=True
        )
        # Hold the SQL fences through commit so no local invalidation can
        # race the write against a new revision, artifact, or membership.
        current = current_evidence(connection, session, dependencies, lock=True)
        if any(current.get(evidence.dependency) != evidence for evidence in context):
            raise ConnectorError("STALE_SOURCE", 409)
        commit.checkpoint()
        row = connection.execute(statement).one()
        commit.checkpoint()
        connection.commit()
    return _history_view(row, citations)


def _stored_proof(row) -> tuple[list[Dependency], list[StoredCitation], set[str]]:
    assistant = row.mode == "assistant"
    try:
        if (
            not isinstance(row.dependencies, list)
            or len(row.dependencies) > (STORED_DEPENDENCY_LIMIT if assistant else CONTEXT_LIMIT)
            or not isinstance(row.erp_clients, list)
            or len(row.erp_clients) > (ERP_CLIENT_LIMIT if assistant else 0)
            or not isinstance(row.citations, list)
            or (
                (row.status != "answered" or len(row.citations) > len(row.dependencies))
                if assistant
                else len(row.citations) != (1 if row.status == "answered" else 0)
            )
        ):
            raise ValueError
        dependencies = _dependencies.validate_python(row.dependencies)
        citations = [StoredCitation.model_validate(value) for value in row.citations]
        erp_clients = set(_clients.validate_python(row.erp_clients))
        if len(erp_clients) != len(row.erp_clients):
            raise ValueError
        chunk_ids = {dependency.chunk_id for dependency in dependencies}
        if (
            len(chunk_ids) != len(dependencies)
            or any(citation.chunk_id not in chunk_ids for citation in citations)
            or len({citation.chunk_id for citation in citations}) != len(citations)
        ):
            raise ValueError
    except (ValidationError, ValueError):
        raise ConnectorError("CHAT_UNAVAILABLE", 503) from None
    return dependencies, citations, erp_clients


def _history_view(row, citations, *, available=True):
    view = {
        "id": row.id,
        "created_at": row.created_at.isoformat(),
        "mode": row.mode,
        "status": row.status if available else "unavailable",
        "question": row.question if available else None,
        "answer": (
            (render_answer(row.question, None) if row.status == "no_evidence" else row.answer)
            if available
            else None
        ),
        "citations": (
            [citation.model_dump(exclude={"chunk_id"}) for citation in citations]
            if available
            else []
        ),
    }
    if available and row.erp_clients:
        view["erp_data"] = True
    return view


def _history_rows(engine, conversation_id, cursor):
    with engine.begin() as connection:
        configure_query(connection)
        table = chat_turns.c
        stmt = select(chat_turns).where(table.conversation_id == conversation_id)
        if cursor is not None:
            anchor = connection.execute(
                select(table.created_at, table.id).where(
                    table.conversation_id == conversation_id, table.id == cursor
                )
            ).one_or_none()
            if anchor is None:
                raise ConnectorError("INVALID_REQUEST", 422)
            stmt = stmt.where(tuple_(table.created_at, table.id) > (anchor.created_at, anchor.id))
        return connection.execute(
            stmt.order_by(table.created_at, table.id).limit(HISTORY_LIMIT + 1)
        ).all()


async def accessible_clients(session: Session, clients, *, external=False) -> set[str]:
    """Mandanten whose ERP data the user may still read (and, if external, send out)."""
    allowed = set()
    if clients and session.scope is None:
        raise ConnectorError("CHAT_UNAVAILABLE", 503)
    for client in sorted(clients):
        scope = Scope(kind="client", client_id=client)
        if session.scope.kind == "client" and client != session.scope.client_id:
            continue
        try:
            if external:
                await session.connector.read_work_context(
                    session.token, scope, WorkQuery(kind="client"), external=True
                )
            else:
                await session.connector.check_scope(session.token, scope)
        except ConnectorError as error:
            if error.status not in (403, 404):
                raise
            continue
        allowed.add(client)
    return allowed


async def read_history(engine, session, conversation_id, cursor):
    rows = await database_call(_history_rows, engine, conversation_id, cursor)
    page_rows = rows[:HISTORY_LIMIT]
    proofs = [_stored_proof(row) for row in page_rows]
    all_dependencies = list(dict.fromkeys(d for proof, _, _ in proofs for d in proof))
    current, labels = await checked_dependencies(engine, session, all_dependencies)
    clients = await accessible_clients(session, {c for _, _, erp in proofs for c in erp})
    items = []
    for row, (deps, citations, erp) in zip(page_rows, proofs, strict=True):
        available = all(dep in current for dep in deps) and erp <= clients
        item = _history_view(row, citations, available=available)
        if available and labels is not None:
            by_chunk = {dep.chunk_id: dep for dep in deps}
            sources = [
                current[by_chunk[citation.chunk_id]].dependency.source for citation in citations
            ]
            label_citations(item, sources, labels)
        items.append(item)
    return {
        "items": items,
        "next_cursor": page_rows[-1].id if len(rows) > HISTORY_LIMIT else None,
    }


def _recent_rows(engine, conversation_id, before_limit):
    with engine.begin() as connection:
        configure_query(connection)
        table = chat_turns.c
        return connection.execute(
            select(chat_turns)
            .where(table.conversation_id == conversation_id, table.status == "answered")
            .order_by(table.created_at.desc(), table.id.desc())
            .limit(before_limit)
        ).all()


async def recent_memory(engine, session, conversation_id, *, external=False) -> Memory:
    """The latest answered turns that are still fully available, within fixed budgets.

    A turn joins memory only while every source and ERP Mandant it depended on
    is still current and permitted, so a revoked source can never reach the
    model through an earlier answer. The new turn inherits these dependencies.
    """
    rows = await database_call(_recent_rows, engine, conversation_id, MEMORY_TURNS)
    chosen = []
    dependencies: dict[Dependency, None] = {}
    clients: set[str] = set()
    characters = 0
    for row in rows:
        deps, _, erp = _stored_proof(row)
        size = len(row.question) + min(len(row.answer), MEMORY_ANSWER_CHARACTERS)
        merged = dependencies | dict.fromkeys(deps)
        if (
            characters + size > MEMORY_CHARACTERS
            or len(merged) > MEMORY_DEPENDENCY_LIMIT
            or len(clients | erp) > MEMORY_ERP_CLIENTS
        ):
            break
        chosen.append((row, deps, erp))
        dependencies, characters = merged, characters + size
        clients |= erp
    if not chosen:
        return Memory()
    current, _ = await checked_dependencies(engine, session, list(dependencies))
    allowed = await accessible_clients(session, clients, external=external)
    turns, evidence, used = [], {}, set()
    for row, deps, erp in reversed(chosen):
        if all(dep in current for dep in deps) and erp <= allowed:
            turns.append((row.question, strip_markers(row.answer[:MEMORY_ANSWER_CHARACTERS])))
            evidence.update(dict.fromkeys(current[dep] for dep in deps))
            used |= erp
    return Memory(tuple(turns), tuple(evidence), frozenset(used))


async def checked_dependencies(engine, session, dependencies):
    if session.scope is None:
        if dependencies:
            raise ConnectorError("CHAT_UNAVAILABLE", 503)
        await verify_session(session)
        return {}, None
    if session.scope.kind == "client":
        return await checked_evidence(engine, session, dependencies), None
    labels = (
        await search_retrieve.list_authorized_clients(session.connector, session.token)
        if dependencies
        else {}
    )
    eligible = [dep for dep in dependencies if dep.source.client_id in labels]
    # Scope is required even when no refs remain. For eligible refs the ERP
    # confirms general scope and exact ACLs together, before the SQL fence.
    await verify_session(session)
    current = await checked_evidence(engine, session, eligible)
    return current, labels


def label_citations(item, sources: list[SourceRef], labels: dict[str, str]):
    for citation, source in zip(item["citations"], sources, strict=True):
        citation["client"] = {"id": source.client_id, "label": labels[source.client_id]}


def _citation_proof(engine, conversation_id, turn_id, citation_id):
    with engine.begin() as connection:
        configure_query(connection)
        row = connection.execute(
            select(chat_turns).where(
                chat_turns.c.conversation_id == conversation_id,
                chat_turns.c.id == turn_id,
                chat_turns.c.status == "answered",
            )
        ).one_or_none()
    if row is not None:
        dependencies, citations, erp = _stored_proof(row)
        for citation in citations:
            if citation.id == citation_id:
                return dependencies, citation, erp
    raise ConnectorError("NOT_FOUND", 404)


async def check_citation(engine, session, dependencies, citation) -> Evidence:
    current, _ = await checked_dependencies(engine, session, dependencies)
    if len(current) == len(dependencies):
        for dependency, evidence in current.items():
            if (
                dependency.chunk_id == citation.chunk_id
                and evidence.page == citation.page
                and evidence.source_kind == citation.source_kind
            ):
                return evidence
    raise ConnectorError("NOT_FOUND", 404)


async def find_citation(engine, session, conversation_id, turn_id, citation_id):
    deps, citation, erp = await database_call(
        _citation_proof, engine, conversation_id, turn_id, citation_id
    )
    if not erp <= await accessible_clients(session, erp):
        raise ConnectorError("NOT_FOUND", 404)
    return deps, citation, await check_citation(engine, session, deps, citation)
