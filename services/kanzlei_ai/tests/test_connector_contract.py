"""Strict contracts prevent privilege-shaped input and incompatible ERP responses."""

import pytest
from pydantic import ValidationError

from kanzlei_ai.connectors import contracts


def model(name):
    assert hasattr(contracts, name), f"{name} contract not implemented"
    return getattr(contracts, name)


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 2, "kind": "client", "client_id": "A"},
        {"kind": "general", "client_id": "A"},
        {"kind": "client"},
        {"kind": "client", "client_id": "A", "role": "System Manager"},
    ],
)
def test_scope_rejects_invalid_and_privilege_shaped_input(payload):
    cls = model("Scope")
    with pytest.raises(ValidationError):
        cls.model_validate(payload)


def test_source_manifest_preserves_multiple_client_relationships():
    cls = model("SourceManifest")
    result = cls(
        source_id="file:abc",
        revision="a" * 64,
        source_type="file",
        client_ids=["A", "B"],
        title="source.pdf",
        mime_type="application/pdf",
        source_modified_at="2026-11-15T00:00:00",
        locator="/desk/customer/A",
        byte_size=20,
    )
    assert result.client_ids == ["A", "B"]
    with pytest.raises(ValidationError):
        cls.model_validate({**result.model_dump(), "locator": "https://external.invalid/secret"})


def test_source_reference_rejects_unbounded_or_ambiguous_versions():
    cls = model("SourceRef")
    with pytest.raises(ValidationError):
        cls(source_id="file:abc", revision="latest", client_id="A")
