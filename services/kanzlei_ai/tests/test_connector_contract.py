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


def _file_manifest():
    return {
        "version": 1,
        "source_id": "file:managed-pdf",
        "source_type": "file",
        "revision": "a" * 64,
        "client_ids": ["A"],
        "title": "source.pdf",
        "mime_type": "application/pdf",
        "source_modified_at": "2026-11-15T00:00:00",
        "locator": "/desk/customer/A",
        "byte_size": 20,
    }


def test_mixed_email_and_legacy_pdf_inventory_with_explicit_attachment():
    legacy_pdf = _file_manifest()
    email = {
        **legacy_pdf,
        "source_id": "email:mail-ä",
        "source_type": "email",
        "mime_type": "application/vnd.kanzlei.email+json",
    }
    attachment = {
        **legacy_pdf,
        "source_id": "file:attachment",
        "parent_message_id": "email:mail-ä",
    }
    page = contracts.InventoryPage.model_validate(
        {"items": [legacy_pdf, email, attachment], "next_cursor": None, "changes_cursor": "w"}
    )
    assert [manifest.parent_message_id for manifest in page.items] == [None, None, "email:mail-ä"]
    assert contracts.SourceRef(source_id=email["source_id"], revision="a" * 64, client_id="A")
    assert contracts.ChangeItem(
        operation="delete", source_id=email["source_id"], client_id="A", manifest=None
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"source_type": "email"},
        {"source_id": "email:file-masquerade"},
        {"mime_type": "application/vnd.kanzlei.email+json"},
        {"source_id": "email:bad-type", "source_type": "email"},
        {"parent_message_id": "file:not-a-parent"},
        {"parent_message_id": "email:"},
        {
            "source_id": "email:bad-parent",
            "source_type": "email",
            "mime_type": "application/vnd.kanzlei.email+json",
            "parent_message_id": "email:other",
        },
    ],
)
def test_manifest_rejects_mismatched_source_kind_or_attachment_parent(overrides):
    with pytest.raises(ValidationError):
        contracts.SourceManifest.model_validate({**_file_manifest(), **overrides})
