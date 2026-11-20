"""Synthetic mail stays separate from quoted, hidden and linked content."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from qdrant_client import QdrantClient

from kanzlei_ai import worker
from kanzlei_ai.connectors.base import ConnectorError
from kanzlei_ai.connectors.contracts import SourceRef
from kanzlei_ai.connectors.frappe import FrappeConnector
from kanzlei_ai.documents import parse
from kanzlei_ai.providers.mistral_embeddings import MistralEmbeddingProvider
from kanzlei_ai.search.index import MISTRAL_INDEX_PROFILE, SearchIndex


def payload(**overrides):
    value = {
        "version": 1,
        "subject": "Synthetic reply",
        "sender": "Ирина Ковалёва <fixture@example.invalid>",
        "recipients": ["Müller & Söhne <de@example.invalid>"],
        "direction": "incoming",
        "sent_at": "2026-11-01T12:30:00+01:00",
        "reply_to": "email:different-client",
        "body": "Approved: AB-2026-001.",
        "body_format": "text",
    }
    return json.dumps({**value, **overrides}, ensure_ascii=False).encode()


MULTILINGUAL = json.loads(
    (Path(__file__).parent / "fixtures/email/multilingual.json").read_text(encoding="utf-8")
)
SUBJECT = "Müller – проверка AB-2026-001"


@pytest.mark.parametrize("fixture", MULTILINGUAL, ids=lambda fixture: fixture["source_id"])
@pytest.mark.parametrize("allowed", [True, False])
def test_native_extraction_and_scoped_search_send_only_current_message_to_provider(
    fixture, monkeypatch, allowed
):
    content = payload(
        subject=SUBJECT,
        direction=fixture["direction"],
        body=fixture["body"],
        body_format=fixture["format"],
    )
    reads = []
    artifacts = []
    publications = []

    class Connector:
        async def read_background_source(self, source):
            reads.append((source.source_id, source.client_id))
            return content

    class Store:
        def put(self, _key, content):
            artifacts.append(json.loads(content))
            return "synthetic-artifact"

    def reject_pdf(*_args, **_kwargs):
        pytest.fail("Email entered the PDF extraction pipeline")

    monkeypatch.setattr(parse, "parse_pdf", reject_pdf)
    monkeypatch.setattr(worker, "_background_connector", lambda _: Connector())
    monkeypatch.setattr(worker, "_artifact_store", lambda _: Store())
    monkeypatch.setattr(worker, "extraction_claim_is_live", lambda *_: True)
    monkeypatch.setattr(
        worker,
        "source_for_claim",
        lambda *_: {
            "connector_id": "fixture-erp",
            "generation": 1,
            "ocr_profile": "native",
            "plan": None,
            "manifest": {
                "source_id": fixture["source_id"],
                "source_type": "email",
                "revision": "a" * 64,
                "client_ids": ["current-client"],
                "mime_type": "application/vnd.kanzlei.email+json",
            },
        },
    )
    monkeypatch.setattr(
        worker, "publish_extraction", lambda *args, **kwargs: publications.append(args[3]) or True
    )
    assert worker._extract_or_stage(
        object(),
        "job",
        "lease",
        SimpleNamespace(ingestion_enabled=True, search_index_enabled=False),
        ocr_provider_factory=reject_pdf,
    )
    assert reads == [(fixture["source_id"], "current-client")]
    assert len(publications) == 1
    assert publications[0]["page_count"] == 0
    assert [(c["page"], c["kind"], c["text"]) for c in publications[0]["chunks"]] == [
        (None, "text", SUBJECT),
        (None, "text", fixture["active"]),
    ]
    wire = json.loads(content)
    assert artifacts[0]["envelope"] == {
        field: wire[field]
        for field in ("subject", "sender", "recipients", "direction", "sent_at", "reply_to")
    }
    assert artifacts[0]["blocks"] == [
        {"page": None, "kind": "text", "text": SUBJECT},
        {"page": None, "kind": "text", "text": fixture["active"]},
    ]
    artifact_json = json.dumps(artifacts[0])
    assert "body" not in artifacts[0]["envelope"]
    assert "SECRET" not in artifact_json and "HIDDEN-CUSTOMER" not in artifact_json

    source = SourceRef(
        source_id=fixture["source_id"], client_id="current-client", revision="a" * 64
    )
    calls = []

    def erp(request):
        assert request.url.path.endswith(".check_processing_policy")
        body = json.loads(request.content)
        assert body["operation"] == "embed"
        assert body["scope"]["client_id"] == "current-client"
        assert body["sources"] == [source.model_dump()]
        calls.append("policy")
        return httpx.Response(200, json={"message": {"version": 1, "allowed": allowed}})

    def embed(request):
        body = json.loads(request.content)
        assert body["input"] in [[SUBJECT, fixture["active"]], [fixture["question"]]]
        calls.append("embed")
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": i, "embedding": [1.0] + [0.0] * 1023}
                    for i in range(len(body["input"]))
                ]
            },
        )

    connector = FrappeConnector(
        "https://erp.example.invalid",
        SecretStr("s" * 43),
        transport=httpx.MockTransport(erp),
    )
    index = SearchIndex(
        client=QdrantClient(":memory:"),
        collection="mail",
        profile=MISTRAL_INDEX_PROFILE,
        dimensions=1024,
        embedding=MistralEmbeddingProvider(
            endpoint="https://api.mistral.ai",
            api_key="synthetic-mistral-key",
            transport=httpx.MockTransport(embed),
        ),
    )

    def authorize():
        if not asyncio.run(worker._allow_processing(connector, "embed", [source])):
            raise ConnectorError("FORBIDDEN", 403)

    def stage():
        index.stage(
            publication_id=str(uuid4()),
            connector_id="fixture-erp",
            source_pk=1,
            revision=source.revision,
            generation=1,
            client_ids=[source.client_id],
            chunks=publications[0]["chunks"],
            before_embed=authorize,
        )

    if not allowed:
        with pytest.raises(ConnectorError, match="FORBIDDEN"):
            stage()
        assert calls == ["policy"]
        return
    stage()
    assert calls == ["policy", "embed"]
    results = index.candidates(
        query=fixture["question"],
        connector_id="fixture-erp",
        client_ids=[source.client_id],
        limit=3,
        before_query_embed=authorize,
    )
    assert results["sparse"][0].chunk_id == publications[0]["chunks"][1]["id"]
    points, _ = index.client.scroll(index.collection, with_payload=True)
    assert all(wire["reply_to"] not in json.dumps(point.payload) for point in points)
    outside_scope = index.candidates(
        query=fixture["question"],
        connector_id="fixture-erp",
        client_ids=["other-client"],
        limit=3,
        before_query_embed=authorize,
    )
    assert not any(outside_scope.values())


@pytest.mark.parametrize(
    "content",
    [
        b"\xff",
        b"[]",
        b'{"version":1,"version":1}',
        payload(version=True),
        payload(direction="sent"),
        payload(body_format="markdown"),
        payload(recipients="x@example.invalid"),
        payload(reply_to="file:wrong"),
        payload(sent_at="2026-11-01"),
        payload(body=None),
        payload(body="active\x00body"),
        payload(thread=["private"]),
    ],
)
def test_malformed_email_payload_is_terminal(content):
    from kanzlei_ai.documents.email import parse_email

    with pytest.raises(parse.TerminalExtractionError, match="INVALID_EMAIL"):
        parse_email(content)


def test_mail_parser_checks_byte_limit_before_decoding(monkeypatch):
    from kanzlei_ai.documents import email

    monkeypatch.setattr(email, "MAX_SOURCE_BYTES", 16)
    with pytest.raises(parse.TerminalExtractionError, match="SOURCE_LIMIT_EXCEEDED"):
        email.parse_email(payload())


@pytest.mark.parametrize(
    ("body", "body_format"),
    [
        ("  > quoted text\n\n-----Ursprüngliche Nachricht-----\nprivate", "text"),
        ("----- Пересылаемое сообщение -----\nprivate", "text"),
        ("Am Montag schrieb ein anderer Kunde:\nprivate", "text"),
        ("<!-- private --><blockquote><p>private</p></blockquote><hr><img src='bad'>", "html"),
        ("<div hidden><div>private</div><p>still private</p></div>", "html"),
        ('<div aria-hidden="TRUE">private</div><div id="divRplyFwdMsg">From</div>private', "html"),
        ('<div style="display:none" style="display:block">private</div>', "html"),
        ('<div type="cite">private</div>', "html"),
        ("\ufeff> quoted only", "text"),
        ("12.11.2026 12:00, Примеров пишет:\nprivate", "text"),
    ],
)
def test_quote_only_or_hidden_body_has_no_searchable_content(body, body_format):
    from kanzlei_ai.documents.email import parse_email

    with pytest.raises(parse.TerminalExtractionError, match="EMAIL_NO_TEXT"):
        parse_email(payload(subject="", body=body, body_format=body_format))


def test_nullable_metadata_and_unicode_identifiers_are_preserved():
    from kanzlei_ai.documents.email import parse_email

    parsed = parse_email(
        payload(subject="", sent_at=None, reply_to=None, body="An Büßer: И-000 27")
    )
    assert parsed.blocks[0].text == "An Büßer: И-000 27"
    envelope = json.loads(parsed.artifact_bytes())["envelope"]
    assert envelope["sent_at"] is None and envelope["reply_to"] is None


@pytest.mark.parametrize(
    "subject",
    [
        "Subject\r\nFrom: foreign",
        "Subject\u2028foreign",
        "Subject\u202eforeign",
        "<blockquote>foreign</blockquote>",
        "&lt;blockquote&gt;foreign&lt;/blockquote&gt;",
        "> foreign",
    ],
)
def test_unsafe_subject_is_omitted_from_searchable_blocks(subject):
    from kanzlei_ai.documents.email import parse_email

    parsed = parse_email(payload(subject=subject, body="Current"))
    assert [block.text for block in parsed.blocks] == ["Current"]


def test_safe_subject_can_stand_alone_without_quoted_body():
    from kanzlei_ai.documents.email import parse_email

    parsed = parse_email(payload(subject=SUBJECT, body="> foreign"))
    assert [block.text for block in parsed.blocks] == [SUBJECT]


@pytest.mark.parametrize(
    "body",
    [
        "<style>.hiddenQuote{display:none}</style><p>Current</p>"
        '<div class="hiddenQuote">foreign quote</div>',
        '<p>Current</p><blockquote id="foreign">foreign quote</blockquote>'
        "<style>#foreign, span { visibility: /* CSS */ hidden !important }</style>",
        "<style>p{color:#333;font:16px sans-serif}</style><p>Current</p>",
        "<style>/* unfinished comment</style>"
        "<style>.hiddenQuote{display:none}</style><p>Current</p>"
        '<div class="hiddenQuote">foreign quote</div>',
    ],
)
def test_local_styles_keep_current_text_and_remove_hidden_body(body):
    from kanzlei_ai.documents.email import parse_email

    parsed = parse_email(payload(subject="", body_format="html", body=body))
    assert [block.text for block in parsed.blocks] == ["Current"]
    assert "foreign quote" not in parsed.artifact_bytes().decode()


@pytest.mark.parametrize(
    "stylesheet",
    [
        "<style>.outer .hiddenQuote {display:none}</style>",
        "<style>@media screen {.hiddenQuote {display:none}}</style>",
        "<style>.hiddenQuote {display:var(--hidden)}</style>",
        "<style>@import 'https://example.invalid/remote.css';</style>",
        '<link rel="stylesheet" href="https://example.invalid/remote.css">',
    ],
)
def test_unresolved_hiding_css_disables_html_body(stylesheet):
    from kanzlei_ai.documents.email import parse_email

    parsed = parse_email(
        payload(
            subject=SUBJECT,
            body_format="html",
            body=stylesheet + '<div class="hiddenQuote">foreign quote</div>',
        )
    )
    assert [block.text for block in parsed.blocks] == [SUBJECT]
    assert "foreign quote" not in parsed.artifact_bytes().decode()


def test_stylesheet_budget_fails_closed(monkeypatch):
    from kanzlei_ai.documents import email

    monkeypatch.setattr(email, "_MAX_STYLESHEET_CHARS", 20)
    with pytest.raises(parse.TerminalExtractionError, match="EMAIL_NO_TEXT"):
        email.parse_email(
            payload(
                subject="",
                body_format="html",
                body="<style>p{font:16px sans-serif}</style><p>Current</p>",
            )
        )
