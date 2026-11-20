"""Feature gate and stage-specific task routing."""

from types import SimpleNamespace

from kanzlei_ai import coordinator, worker


def test_disabled_index_task_does_not_claim(monkeypatch):
    monkeypatch.setattr(worker, "Settings", lambda: SimpleNamespace(search_index_enabled=False))

    def reject(*_):
        raise AssertionError("Disabled index task opened database")

    monkeypatch.setattr(worker, "engine_from_settings", reject)
    assert worker.index_document("synthetic") is None


def test_coordinator_exposes_index_route_only_when_enabled(monkeypatch):
    sent = []
    recovered = []

    def dispatch(engine, publish, *, publish_index=None, publish_ocr, index_profile=None):
        publish("extract-id")
        if publish_index:
            publish_index("index-id")

    monkeypatch.setattr(coordinator.outbox, "dispatch", dispatch)
    monkeypatch.setattr(
        coordinator.outbox, "recover", lambda _engine, **kwargs: recovered.append(kwargs)
    )
    monkeypatch.setattr(coordinator.app, "send_task", lambda task, args: sent.append((task, args)))
    engine = object()
    coordinator.run_cycle(
        engine, SimpleNamespace(ingestion_enabled=False, search_index_enabled=False)
    )
    assert sent == [("kanzlei_ai.stage_metadata", ["extract-id"])]
    coordinator.run_cycle(
        engine, SimpleNamespace(ingestion_enabled=False, search_index_enabled=True)
    )
    assert sent[-2:] == [
        ("kanzlei_ai.stage_metadata", ["extract-id"]),
        ("kanzlei_ai.index_document", ["index-id"]),
    ]
    assert recovered[-1] == {"include_index": True, "index_profile": "synthetic-v1"}
