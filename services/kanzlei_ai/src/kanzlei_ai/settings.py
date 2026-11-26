"""Service-local configuration; secrets are excluded from object representations."""

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, HttpUrl, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AI_",
        env_file=".env",
        extra="ignore",
        hide_input_in_errors=True,
        env_ignore_empty=True,
    )

    database_url: SecretStr = SecretStr("postgresql://kanzlei_ai@127.0.0.1:55432/kanzlei_ai")
    database_password: SecretStr | None = None
    qdrant_url: HttpUrl = "http://127.0.0.1:56333"
    qdrant_api_key: SecretStr | None = None
    search_index_enabled: bool = False
    search_collection: str = Field(default="kanzlei_pdf_synthetic_v1", min_length=1, max_length=255)
    mistral_search_collection: str = Field(
        default="kanzlei_pdf_mistral_embed_v1", min_length=1, max_length=255
    )
    model_profile: Literal["synthetic", "mistral"] = "synthetic"
    mistral_enabled: bool = False
    mistral_endpoint: Literal["https://api.eu.mistral.ai", "https://api.mistral.ai"] | None = None
    mistral_api_key: SecretStr | None = None
    # Simultaneous Mistral requests across all AI processes; background work
    # (OCR, indexing) may use only part of them so chat answers keep the rest.
    mistral_max_concurrent_requests: int = Field(default=4, ge=1, le=32)
    mistral_background_concurrent_requests: int = Field(default=2, ge=1, le=32)
    mistral_request_wait_seconds: float = Field(default=10.0, ge=0, le=30)
    coordinator_only: bool = False
    chat_enabled: bool = False
    chat_browser_erp_origin: HttpUrl | None = None
    # Generative assistant model; compare candidates on synthetic questions before a change.
    chat_assistant_model: str = Field(
        default="mistral-small-2603", min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9.-]*$"
    )
    redis_url: SecretStr = SecretStr("redis://127.0.0.1:56379/0")
    health_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    erp_base_url: HttpUrl | None = None
    erp_service_key: SecretStr | None = None
    erp_site_name: str | None = None
    erp_allow_insecure_http: bool = False
    erp_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    ingestion_enabled: bool = False
    ingestion_docx_enabled: bool = False
    ingestion_key: SecretStr | None = None
    ingestion_connector_id: str = "kanzlei-erp"
    ingestion_poll_seconds: int = Field(default=30, ge=5, le=3600)
    ingestion_reconcile_seconds: int = Field(default=86400, ge=60, le=2592000)
    ingestion_pipeline_version: str = Field(default="pdf-native-v1", min_length=1, max_length=100)
    ingestion_ocr_profile: Literal["native", "paddle"] = "native"
    paddle_model_dir: Path | None = None
    paddle_docling_artifacts_dir: Path | None = None
    artifact_s3_endpoint: HttpUrl | None = None
    artifact_s3_bucket: str | None = None
    artifact_s3_access_key: SecretStr | None = None
    artifact_s3_secret_key: SecretStr | None = None
    artifact_s3_region: str = Field(default="us-east-1", min_length=1, max_length=100)
    artifact_s3_prefix: str = Field(default="kanzlei-ai", min_length=1, max_length=60)
    artifact_max_bytes: int = Field(default=4 * 1024 * 1024, ge=1024, le=20 * 1024 * 1024)

    @model_validator(mode="after")
    def require_mistral_configuration(self):
        if self.coordinator_only and self.chat_enabled:
            raise ValueError("Coordinator cannot enable chat")
        if self.model_profile == "mistral" and not self.mistral_enabled:
            raise ValueError("Mistral model profile requires explicit external processing opt-in")
        if self.mistral_enabled and (
            not self.mistral_endpoint or (not self.coordinator_only and not self.mistral_api_key)
        ):
            raise ValueError("Mistral requires an approved endpoint and API key")
        if self.mistral_background_concurrent_requests > self.mistral_max_concurrent_requests:
            raise ValueError("Mistral background requests cannot exceed the total limit")
        if self.mistral_search_collection == self.search_collection:
            raise ValueError("Mistral and synthetic indexes require separate collections")
        return self

    @field_validator("chat_browser_erp_origin", mode="before")
    @classmethod
    def reject_non_origin_input(cls, value: object) -> object:
        if isinstance(value, str) and (
            any(character.isspace() or ord(character) < 32 for character in value)
            or any(character in value for character in "*\\?#%")
            or urlsplit(value).path not in ("", "/")
        ):
            raise ValueError("Expected an exact browser ERP origin")
        return value

    @field_validator("chat_browser_erp_origin")
    @classmethod
    def exact_browser_origin(cls, origin: HttpUrl | None) -> HttpUrl | None:
        if origin is None:
            return None
        host = origin.host or ""
        loopback = host in {"localhost", "127.0.0.1", "[::1]"} or host.endswith(".localhost")
        if (
            origin.path not in (None, "/")
            or origin.username is not None
            or origin.password is not None
            or origin.query is not None
            or origin.fragment is not None
            or (origin.scheme != "https" and not loopback)
        ):
            raise ValueError("Expected an exact browser ERP origin with HTTPS outside loopback")
        return origin
