import pytest
from pydantic import ValidationError

from kanzlei_ai.search.publication import configured_index_profile
from kanzlei_ai.settings import Settings


def test_coordinator_can_route_mistral_jobs_without_provider_credentials(monkeypatch):
    monkeypatch.setenv("AI_COORDINATOR_ONLY", "true")
    monkeypatch.setenv("AI_MODEL_PROFILE", "mistral")
    monkeypatch.setenv("AI_MISTRAL_ENABLED", "true")
    monkeypatch.setenv("AI_MISTRAL_ENDPOINT", "https://api.eu.mistral.ai")
    monkeypatch.delenv("AI_MISTRAL_API_KEY", raising=False)

    settings = Settings(_env_file=None)

    assert settings.mistral_api_key is None
    assert configured_index_profile(settings) == "mistral-embed-v1"
    with pytest.raises(ValidationError, match="Coordinator cannot enable chat"):
        Settings(_env_file=None, chat_enabled=True)

    monkeypatch.delenv("AI_COORDINATOR_ONLY")
    with pytest.raises(ValidationError, match="Mistral requires"):
        Settings(_env_file=None)
