"""The ERP work context is a fixed view with a strict, bounded response."""

import asyncio
import json

import httpx
import pytest
from pydantic import SecretStr

from kanzlei_ai.connectors.base import ConnectorError
from kanzlei_ai.connectors.contracts import Scope, WorkQuery
from kanzlei_ai.connectors.frappe import FrappeConnector

SCOPE = Scope(kind="client", client_id="fixture")
ITEM = {
    "type": "fibu_package",
    "id": "P-1",
    "client_id": "fixture",
    "client_label": "Mandant",
    "title": "FiBu 11/2026",
    "status": "Collection",
    "due_date": "2026-12-01",
    "due_kind": "document",
    "note": "Wartet auf Mandant",
}


def connector(handler):
    return FrappeConnector(
        "https://erp.example.invalid",
        SecretStr("synthetic-policy-key-" + "s" * 32),
        transport=httpx.MockTransport(handler),
    )


DEADLINES = WorkQuery(kind="deadlines", days=14)


def read(body, query=DEADLINES):
    sent = []

    def handle(request):
        sent.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"message": body})

    result = asyncio.run(connector(handle).read_work_context("g" * 43, SCOPE, query, external=True))
    return result, sent


def test_sends_only_the_fixed_query_and_parses_records():
    result, sent = read({"version": 1, "today": "2026-11-25", "items": [ITEM], "truncated": False})
    assert result.items[0].due_kind == "document"
    assert sent == [
        (
            "/api/method/kanzlei_erp.ai_connector.api.read_work_context",
            {
                "version": 1,
                "access_token": "g" * 43,
                "scope": {"version": 1, "kind": "client", "client_id": "fixture"},
                "query": {"kind": "deadlines", "days": 14},
                "external": True,
            },
        )
    ]


@pytest.mark.parametrize(
    "item",
    [
        {**ITEM, "type": "invoice"},
        {**ITEM, "title": "x" * 501},
        {**ITEM, "note": "line\nbreak"},
        {**ITEM, "due_date": "01.12.2026"},
        {**ITEM, "extra": "field"},
    ],
    ids=["type", "long-title", "control", "date", "extra"],
)
def test_malformed_records_are_rejected(item):
    with pytest.raises(ConnectorError):
        read({"version": 1, "today": "2026-11-25", "items": [item], "truncated": False})


def test_too_many_records_are_rejected():
    with pytest.raises(ConnectorError):
        read({"version": 1, "today": "2026-11-25", "items": [ITEM] * 26, "truncated": True})
