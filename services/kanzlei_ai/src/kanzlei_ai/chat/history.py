"""Store completed turns and render history under current, complete authorization."""

from threading import Lock
from time import monotonic
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError
from sqlalchemy import select, tuple_

from kanzlei_ai.chat.citations import VerifiedAnswer
from kanzlei_ai.chat.context import (
    CONTEXT_LIMIT,
    Dependency,
    Evidence,
    Session,
    checked_evidence,
    configure_query,
    current_evidence,
    database_call,
)
from kanzlei_ai.connectors.base import ConnectorError
from kanzlei_ai.models import chat_conversations, chat_turns

HISTORY_LIMIT = 10
CONVERSATION_LIMIT = 100
_dependencies = TypeAdapter(list[Dependency])


class StoredCitation(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    id: str
    chunk_id: str
    page: int
    title: str


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


def lookup(engine, connector_id, subject, conversation_id):
    with engine.begin() as connection:
        configure_query(connection)
        row = connection.execute(
            select(chat_conversations.c.client_id).where(
                chat_conversations.c.id == conversation_id, *_owner_filter(connector_id, subject)
            )
        ).one_or_none()
    if row is None:
        raise ConnectorError("NOT_FOUND", 404)
    return row.client_id


def _conversation_view(row):
    return {"id": row.id, "client_id": row.client_id, "created_at": row.created_at.isoformat()}


def create_conversation(engine, session: Session):
    with engine.begin() as connection:
        configure_query(connection)
        row = connection.execute(
            chat_conversations.insert()
            .values(
                id=str(uuid4()),
                connector_id=session.connector_id,
                subject=session.subject,
                client_id=session.scope.client_id,
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
                chat_conversations.c.client_id == session.scope.client_id,
            )
            .order_by(chat_conversations.c.created_at.desc(), chat_conversations.c.id.desc())
            .limit(CONVERSATION_LIMIT)
        )
        return [_conversation_view(row) for row in rows]


def store_turn(
    engine,
    session: Session,
    conversation_id,
    question,
    context: tuple[Evidence, ...],
    answer: VerifiedAnswer | None,
    commit: TurnCommit,
):
    commit.checkpoint()
    dependencies = [evidence.dependency for evidence in context]
    citations = []
    if answer is not None:
        citations = [
            StoredCitation(
                id=str(uuid4()),
                chunk_id=answer.evidence.dependency.chunk_id,
                page=answer.evidence.page,
                title=answer.evidence.title,
            )
        ]
    statement = (
        chat_turns.insert()
        .values(
            id=str(uuid4()),
            conversation_id=conversation_id,
            status="answered" if answer is not None else "no_evidence",
            question=question,
            answer=f"Der Betrag beträgt {answer.amount}." if answer is not None else None,
            citations=[citation.model_dump() for citation in citations],
            dependencies=_dependencies.dump_python(dependencies, mode="json"),
        )
        .returning(chat_turns)
    )
    with engine.begin() as connection:
        configure_query(connection)
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


def _stored_proof(row) -> tuple[list[Dependency], list[StoredCitation]]:
    try:
        if (
            not isinstance(row.dependencies, list)
            or len(row.dependencies) > CONTEXT_LIMIT
            or not isinstance(row.citations, list)
            or len(row.citations) != (1 if row.status == "answered" else 0)
        ):
            raise ValueError
        dependencies = _dependencies.validate_python(row.dependencies)
        citations = [StoredCitation.model_validate(value) for value in row.citations]
        chunk_ids = {dependency.chunk_id for dependency in dependencies}
        if len(chunk_ids) != len(dependencies) or any(
            citation.chunk_id not in chunk_ids for citation in citations
        ):
            raise ValueError
    except (ValidationError, ValueError):
        raise ConnectorError("CHAT_UNAVAILABLE", 503) from None
    return dependencies, citations


def _history_view(row, citations, *, available=True):
    return {
        "id": row.id,
        "created_at": row.created_at.isoformat(),
        "status": row.status if available else "unavailable",
        "question": row.question if available else None,
        "answer": row.answer if available else None,
        "citations": (
            [citation.model_dump(exclude={"chunk_id"}) for citation in citations]
            if available
            else []
        ),
    }


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


async def read_history(engine, session, conversation_id, cursor):
    rows = await database_call(_history_rows, engine, conversation_id, cursor)
    page_rows = rows[:HISTORY_LIMIT]
    proofs = [_stored_proof(row) for row in page_rows]
    all_dependencies = list(dict.fromkeys(d for proof, _ in proofs for d in proof))
    current = await checked_evidence(engine, session, all_dependencies)
    return {
        "items": [
            _history_view(row, citations, available=all(d in current for d in deps))
            for row, (deps, citations) in zip(page_rows, proofs, strict=True)
        ],
        "next_cursor": page_rows[-1].id if len(rows) > HISTORY_LIMIT else None,
    }


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
        dependencies, citations = _stored_proof(row)
        for citation in citations:
            if citation.id == citation_id:
                return dependencies, citation
    raise ConnectorError("NOT_FOUND", 404)


async def check_citation(engine, session, dependencies, citation) -> Evidence:
    current = await checked_evidence(engine, session, dependencies)
    if len(current) == len(dependencies):
        for dependency, evidence in current.items():
            if (
                dependency.chunk_id == citation.chunk_id
                and evidence.page == citation.page
                and evidence.mime_type == "application/pdf"
            ):
                return evidence
    raise ConnectorError("NOT_FOUND", 404)


async def find_citation(engine, session, conversation_id, turn_id, citation_id):
    deps, citation = await database_call(
        _citation_proof, engine, conversation_id, turn_id, citation_id
    )
    return deps, citation, await check_citation(engine, session, deps, citation)
