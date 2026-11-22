"""Strict, versioned wire contracts with no Frappe runtime dependency."""

from typing import Annotated, Literal, Self
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Identifier = Annotated[str, Field(min_length=1, max_length=200)]
Revision = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Token = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{43}$")]
SourceId = Annotated[
    str, Field(pattern=r"^(file:.{1,195}|email:[^\x00-\x1f\x7f]+)$", max_length=200)
]
EmailId = Annotated[str, Field(pattern=r"^email:[^\x00-\x1f\x7f]+$", max_length=200)]
Locator = Annotated[str, Field(min_length=1, max_length=2048)]

EMAIL_MIME_TYPE = "application/vnd.kanzlei.email+json"
MAX_SOURCE_BYTES = 20 * 1024 * 1024


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: Literal[1] = 1

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("Unsupported contract version")
        return value


class Scope(Contract):
    kind: Literal["client", "general"]
    client_id: Identifier | None = None

    @model_validator(mode="after")
    def validate_client(self) -> Self:
        if (self.kind == "client") != bool(self.client_id):
            raise ValueError("Client scope requires exactly one client")
        return self


class SourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    source_id: SourceId
    revision: Revision
    client_id: Identifier


class ScopeCheck(Contract):
    allowed: bool


ProcessingOperation = Literal["ocr", "embed", "generate"]


class ProcessingPolicyCheck(ScopeCheck):
    version: Literal[1]


class SourceManifest(Contract):
    source_id: SourceId
    source_type: Literal["file", "email"]
    revision: Revision
    client_ids: Annotated[list[Identifier], Field(min_length=1, max_length=100)]
    title: Annotated[str, Field(min_length=1, max_length=500)]
    mime_type: Annotated[str, Field(min_length=1, max_length=200)]
    source_modified_at: Annotated[str, Field(min_length=1, max_length=50)]
    locator: Locator
    byte_size: Annotated[int, Field(ge=0, le=MAX_SOURCE_BYTES)]
    active: Literal[True] = True
    parent_message_id: EmailId | None = None

    @model_validator(mode="after")
    def validate_source_type(self) -> Self:
        if not self.source_id.startswith(self.source_type + ":"):
            raise ValueError("Source ID must match the source type")
        is_email = self.source_type == "email"
        if is_email != (self.mime_type == EMAIL_MIME_TYPE) or (is_email and self.parent_message_id):
            raise ValueError("Invalid email media type or attachment parent")
        return self

    @field_validator("locator")
    @classmethod
    def relative_erp_locator(cls, value: str) -> str:
        if (
            not value.startswith("/")
            or value.startswith("//")
            or "\\" in value
            or any(ord(c) < 32 for c in value)
        ):
            raise ValueError("Expected an ERP-relative locator")
        return value


class SourceLocator(Contract):
    version: Literal[1]
    locator: Locator

    @field_validator("locator")
    @classmethod
    def safe_erp_path(cls, value: str) -> str:
        if not value.startswith("/") or value.startswith("//") or value != value.strip():
            raise ValueError("Expected an ERP-relative locator")
        decoded = unquote(value, errors="strict")
        if "\\" in decoded or any(ord(char) < 32 or ord(char) == 127 for char in decoded):
            raise ValueError("Invalid ERP locator characters")
        parsed = urlsplit(value)
        path = unquote(parsed.path, errors="strict")
        if (
            parsed.netloc
            or "#" in value
            or path.startswith("//")
            or any(part in {".", ".."} for part in path.split("/"))
        ):
            raise ValueError("Unsafe ERP locator")
        return value


class Identity(Contract):
    subject: Identifier
    connector_id: Identifier
    expires_at: Annotated[int, Field(gt=0)]
    general_access: bool


class Grant(Contract):
    access_token: Token = Field(repr=False)
    token_type: Literal["bearer"]
    identity: Identity


class ExchangeRequest(Contract):
    ticket: Token = Field(repr=False)


class BrowserExchangeRequest(ExchangeRequest):
    version: Literal[1]
    state: Token = Field(repr=False)
    verifier: Token = Field(repr=False)


class ClientRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: Identifier
    label: Annotated[str, Field(min_length=1, max_length=500)]


class ClientPage(Contract):
    items: Annotated[list[ClientRecord], Field(max_length=100)]
    next_cursor: Annotated[str, Field(max_length=4096)] | None


class DocumentPage(Contract):
    items: Annotated[list[SourceManifest], Field(max_length=100)]
    next_cursor: Annotated[str, Field(max_length=4096)] | None


class PageRequest(Contract):
    limit: Annotated[int, Field(ge=1, le=100)] = 50
    cursor: Annotated[str, Field(max_length=4096)] | None = None


class DocumentRequest(PageRequest):
    client_id: Identifier


class AuthorizationRequest(Contract):
    scope: Scope
    sources: Annotated[list[SourceRef], Field(min_length=1, max_length=50)]


class ReadRequest(Contract):
    scope: Scope
    source: SourceRef


class Decision(SourceRef):
    allowed: bool


class AuthorizationResult(Contract):
    decisions: Annotated[list[Decision], Field(min_length=1, max_length=50)]


class Revoked(Contract):
    revoked: Literal[True]


class InventoryPage(Contract):
    items: Annotated[list[SourceManifest], Field(max_length=100)]
    next_cursor: Annotated[str, Field(min_length=1, max_length=4096)] | None
    changes_cursor: Annotated[str, Field(min_length=1, max_length=4096)]


class ChangeItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    source_id: SourceId
    client_id: Identifier
    operation: Literal["upsert", "delete"]
    manifest: SourceManifest | None

    @model_validator(mode="after")
    def validate_projection(self):
        if self.operation == "delete":
            if self.manifest is not None:
                raise ValueError("Delete must not include a manifest")
        elif (
            self.manifest is None
            or self.manifest.source_id != self.source_id
            or self.client_id not in self.manifest.client_ids
        ):
            raise ValueError("Upsert must match its source and client")
        return self


class ChangePage(Contract):
    items: Annotated[list[ChangeItem], Field(max_length=100)]
    next_cursor: Annotated[str, Field(min_length=1, max_length=4096)]
