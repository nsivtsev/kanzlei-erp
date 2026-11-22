"""Private, content-addressed derived-artifact storage.

The storage root is service-owned and intentionally never derived from an ERP
locator or document title.  Keys contain only hashes and stable pipeline IDs.
"""

from __future__ import annotations

import base64
import hashlib


class LimitExceeded(Exception):
    """A bounded artifact write was rejected."""


class S3ArtifactStore:
    """Private immutable artifact objects in a service-specific S3 namespace.

    An S3 PUT is atomic for readers.  Conditional create protects an existing
    key from a distinct/corrupt writer; duplicate deliveries may only reuse an
    object whose immutable metadata matches the requested bytes.
    """

    def __init__(self, client, *, bucket: str, prefix: str, max_bytes: int):
        clean = prefix.strip("/")
        if not bucket or not clean or any(part in ("", ".", "..") for part in clean.split("/")):
            raise ValueError("Invalid artifact namespace")
        self.client = client
        self.bucket = bucket
        self.prefix = clean
        self.max_bytes = max_bytes

    @classmethod
    def from_settings(cls, settings):
        from boto3 import client
        from botocore.config import Config

        if not all(
            (
                settings.artifact_s3_endpoint,
                settings.artifact_s3_bucket,
                settings.artifact_s3_access_key,
                settings.artifact_s3_secret_key,
            )
        ):
            raise RuntimeError("Private artifact storage is not configured")
        return cls(
            client(
                "s3",
                endpoint_url=str(settings.artifact_s3_endpoint),
                aws_access_key_id=settings.artifact_s3_access_key.get_secret_value(),
                aws_secret_access_key=settings.artifact_s3_secret_key.get_secret_value(),
                region_name=settings.artifact_s3_region,
                config=Config(
                    signature_version="s3v4",
                    s3={"addressing_style": "path"},
                    connect_timeout=3,
                    read_timeout=10,
                    retries={"max_attempts": 2, "mode": "standard"},
                ),
            ),
            bucket=settings.artifact_s3_bucket,
            prefix=settings.artifact_s3_prefix,
            max_bytes=settings.artifact_max_bytes,
        )

    def put(self, namespace: str, content: bytes) -> str:
        if len(content) > self.max_bytes:
            raise LimitExceeded
        namespace_hash = hashlib.sha256(namespace.encode("utf-8")).hexdigest()
        digest = hashlib.sha256(content).hexdigest()
        key = f"{self.prefix}/{namespace_hash[:2]}/{namespace_hash}/{digest}"
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=content,
                ContentType="application/json",
                ChecksumSHA256=base64.b64encode(bytes.fromhex(digest)).decode(),
                Metadata={"sha256": digest, "namespace": namespace_hash},
                IfNoneMatch="*",
            )
        except Exception as error:
            # S3-compatible APIs return a precondition error for a duplicate
            # delivery.  Trust only a full metadata/length match, never an
            # arbitrary object at the final key.
            response = getattr(error, "response", {})
            status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            code = response.get("Error", {}).get("Code")
            if status not in (409, 412) and code not in {
                "PreconditionFailed",
                "ConditionalRequestConflict",
            }:
                raise
            existing = self.client.head_object(Bucket=self.bucket, Key=key)
            if (
                existing.get("ContentLength") != len(content)
                or existing.get("Metadata", {}).get("sha256") != digest
                or existing.get("Metadata", {}).get("namespace") != namespace_hash
            ):
                raise RuntimeError("Artifact key collision") from None
        return key
