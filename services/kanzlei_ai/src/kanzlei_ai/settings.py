"""Service-local configuration; secrets are excluded from object representations."""

from pydantic import Field, HttpUrl, SecretStr
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
    ingestion_pipeline_version: str = Field(default="metadata-v1", min_length=1, max_length=100)
