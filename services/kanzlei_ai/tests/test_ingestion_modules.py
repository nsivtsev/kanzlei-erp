"""The independent coordinator is packaged with the service."""


def test_worker_uses_late_ack_and_bounded_runtime():
    from kanzlei_ai.worker import app

    assert app.conf.task_acks_late is True
    assert app.conf.task_reject_on_worker_lost is True
    assert app.conf.worker_prefetch_multiplier == 1
    assert app.conf.task_time_limit < 120


def test_background_feed_contract_rejects_mismatched_delete():
    import pytest
    from pydantic import ValidationError

    from kanzlei_ai.connectors.contracts import ChangePage

    with pytest.raises(ValidationError):
        ChangePage.model_validate(
            {
                "version": 1,
                "next_cursor": "next",
                "items": [
                    {
                        "source_id": "file:x",
                        "client_id": "c",
                        "operation": "delete",
                        "manifest": {"bad": True},
                    }
                ],
            }
        )


def test_background_sync_is_disabled_by_default():
    from kanzlei_ai.settings import Settings

    assert Settings(_env_file=None).ingestion_enabled is False


def test_background_credential_must_differ_from_browser_key():
    import pytest

    from kanzlei_ai.coordinator import sync_once
    from kanzlei_ai.settings import Settings

    settings = Settings(
        _env_file=None,
        ingestion_enabled=True,
        erp_base_url="https://erp.example",
        ingestion_key="a" * 40,
        erp_service_key="a" * 40,
    )
    with pytest.raises(ValueError):
        sync_once(None, settings)


def test_background_feed_uses_private_key_and_fixed_rpc_contract():
    import asyncio
    import json

    import httpx
    from pydantic import SecretStr

    from kanzlei_ai.connectors.frappe import FrappeConnector

    calls = []

    def handler(request):
        operation = request.url.path.rsplit(".", 1)[-1]
        calls.append((operation, json.loads(request.content)))
        assert request.url.path == "/api/method/kanzlei_erp.ai_connector.api." + operation
        assert request.headers["X-Kanzlei-AI-Service-Key"] == "private-ingestion-key-" + "i" * 32
        assert "cookie" not in request.headers
        if operation == "inventory":
            return httpx.Response(
                200,
                json={
                    "message": {
                        "version": 1,
                        "items": [],
                        "next_cursor": "page-2",
                        "changes_cursor": "watermark",
                    }
                },
            )
        assert operation == "changes"
        return httpx.Response(
            200, json={"message": {"version": 1, "items": [], "next_cursor": "change-2"}}
        )

    client = FrappeConnector(
        "https://erp.example.invalid",
        SecretStr("private-ingestion-key-" + "i" * 32),
        transport=httpx.MockTransport(handler),
    )
    inventory = asyncio.run(client.inventory("page-1", 20))
    changes = asyncio.run(client.changes("change-1", 30))
    assert inventory.changes_cursor == "watermark" and inventory.next_cursor == "page-2"
    assert changes.next_cursor == "change-2"
    assert calls == [
        ("inventory", {"version": 1, "cursor": "page-1", "limit": 20}),
        ("changes", {"version": 1, "cursor": "change-1", "limit": 30}),
    ]


def test_background_feed_rejects_invalid_wire_response():
    import asyncio

    import httpx
    import pytest
    from pydantic import SecretStr

    from kanzlei_ai.connectors.base import ConnectorError
    from kanzlei_ai.connectors.frappe import FrappeConnector

    client = FrappeConnector(
        "https://erp.example.invalid",
        SecretStr("i" * 40),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"message": {"version": 1, "items": [], "next_cursor": None}}
            )
        ),
    )
    with pytest.raises(ConnectorError):
        asyncio.run(client.changes("cursor"))


def test_coordinator_recovers_and_dispatches_when_polling_disabled(monkeypatch):
    from kanzlei_ai import coordinator
    from kanzlei_ai.settings import Settings

    events = []
    monkeypatch.setattr(
        coordinator.outbox, "recover", lambda engine: events.append(("recover", engine))
    )

    def dispatch(engine, publish):
        events.append(("dispatch", engine))
        publish("job-id")

    monkeypatch.setattr(coordinator.outbox, "dispatch", dispatch)
    monkeypatch.setattr(
        coordinator.app, "send_task", lambda name, args: events.append((name, args))
    )
    monkeypatch.setattr(coordinator, "sync_once", lambda *_: events.append(("unexpected sync",)))
    coordinator.run_cycle("test-engine", Settings(_env_file=None, ingestion_enabled=False))
    assert events == [
        ("recover", "test-engine"),
        ("dispatch", "test-engine"),
        ("kanzlei_ai.stage_metadata", ["job-id"]),
    ]


def test_erp_failure_occurs_after_recovery_and_dispatch(monkeypatch):
    import pytest

    from kanzlei_ai import coordinator
    from kanzlei_ai.settings import Settings

    events = []
    monkeypatch.setattr(coordinator.outbox, "recover", lambda engine: events.append("recover"))
    monkeypatch.setattr(
        coordinator.outbox, "dispatch", lambda engine, publish: events.append("dispatch")
    )

    def fail(*_):
        events.append("sync")
        raise RuntimeError("ERP unavailable")

    monkeypatch.setattr(coordinator, "sync_once", fail)
    with pytest.raises(RuntimeError, match="ERP unavailable"):
        coordinator.run_cycle(None, Settings(_env_file=None, ingestion_enabled=True))
    assert events == ["recover", "dispatch", "sync"]
