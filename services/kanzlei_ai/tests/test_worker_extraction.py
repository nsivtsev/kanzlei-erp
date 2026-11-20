"""Worker extraction failure classification."""

from types import SimpleNamespace

from kanzlei_ai import worker
from kanzlei_ai.connectors.base import ConnectorError


def test_worker_marks_connector_source_limit_as_terminal(monkeypatch):
    class Connector:
        async def read_background_source(self, _source):
            raise ConnectorError("LIMIT_EXCEEDED", 413)

    calls = []
    monkeypatch.setattr(worker, "_background_connector", lambda _settings: Connector())
    monkeypatch.setattr(
        worker,
        "source_for_claim",
        lambda *_: {
            "connector_id": "erp",
            "generation": 1,
            "ocr_profile": "native",
            "plan": None,
            "manifest": {
                "source_id": "file:limit",
                "revision": "a" * 64,
                "client_ids": ["client"],
                "mime_type": "application/pdf",
            },
        },
    )
    monkeypatch.setattr(worker, "fail_terminal", lambda *args: calls.append(args))

    engine = object()
    assert not worker._extract_or_stage(
        engine, "job-id", "lease-token", SimpleNamespace(ingestion_enabled=True)
    )
    assert calls == [(engine, "job-id", "lease-token", "SOURCE_LIMIT_EXCEEDED")]
