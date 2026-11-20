import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from kanzlei_ai.app import create_app
from kanzlei_ai.settings import Settings


def test_default_app_does_not_expose_chat_routes():
    with TestClient(create_app(settings=Settings(_env_file=None))) as client:
        assert client.get("/v1/auth/browser/config").status_code == 404
        response = client.post("/v1/chat/conversations", json={"client_id": "fixture"})
        assert response.status_code == 404
        assert client.get("/health/live").status_code == 200


@pytest.mark.parametrize(
    "origin",
    [
        "https://erp.example.invalid/route",
        "https://erp.example.invalid/?state=123",
        "https://erp.example.invalid/#page",
        "https://name@erp.example.invalid/",
        "http://erp.example.invalid/",
        "https://*.example.invalid",
        "https://erp.example.invalid/./",
        "https://erp.example.invalid/docs/..",
    ],
)
def test_browser_origin_is_exact_and_requires_tls_outside_loopback(origin):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, chat_browser_erp_origin=origin)


def test_chat_rejects_incomplete_opt_in_configuration():
    with pytest.raises(ValueError, match="Chat requires"):
        create_app(settings=Settings(_env_file=None, chat_enabled=True))


def test_configured_chat_mounts_routes_without_contacting_dependencies():
    settings = Settings(
        _env_file=None,
        chat_enabled=True,
        search_index_enabled=True,
        chat_browser_erp_origin="http://erp.test.localhost:8089/",
        erp_base_url="https://erp.example.invalid",
        erp_service_key="s" * 43,
    )
    with TestClient(create_app(settings=settings)) as client:
        config = client.get("/v1/auth/browser/config")
        assert config.status_code == 200
        assert config.json() == {"version": 1, "erp_origin": "http://erp.test.localhost:8089"}
        assert config.headers["cache-control"] == "private, no-store"
        response = client.post("/v1/chat/conversations", json={"client_id": "fixture"})
        assert response.status_code == 401
