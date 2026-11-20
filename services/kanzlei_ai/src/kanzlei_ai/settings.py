"""Service-local configuration; secrets are excluded from object representations."""

from urllib.parse import urlsplit

from pydantic import Field, HttpUrl, SecretStr, field_validator
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
    chat_enabled: bool = False
    chat_browser_erp_origin: HttpUrl | None = None
    redis_url: SecretStr = SecretStr("redis://127.0.0.1:56379/0")
    health_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    erp_base_url: HttpUrl | None = None
    erp_service_key: SecretStr | None = None
    erp_site_name: str | None = None
    erp_allow_insecure_http: bool = False
    erp_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    ingestion_enabled: bool = False
    ingestion_key: SecretStr | None = None
    ingestion_connector_id: str = "kanzlei-erp"
    ingestion_poll_seconds: int = Field(default=30, ge=5, le=3600)
    ingestion_reconcile_seconds: int = Field(default=86400, ge=60, le=2592000)
    ingestion_pipeline_version: str = Field(default="pdf-native-v1", min_length=1, max_length=100)
    artifact_s3_endpoint: HttpUrl | None = None
    artifact_s3_bucket: str | None = None
    artifact_s3_access_key: SecretStr | None = None
    artifact_s3_secret_key: SecretStr | None = None
    artifact_s3_region: str = Field(default="us-east-1", min_length=1, max_length=100)
    artifact_s3_prefix: str = Field(default="kanzlei-ai", min_length=1, max_length=60)
    artifact_max_bytes: int = Field(default=4 * 1024 * 1024, ge=1024, le=20 * 1024 * 1024)

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
