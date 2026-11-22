"""Scoped, identity-checked operations on private rebuildable projections."""

import re
from hashlib import sha256
from uuid import UUID

from qdrant_client import models

from kanzlei_ai.search.index import point_id

_POINT_PROVENANCE = ["profile", "connector_id", "publication_id", "chunk_id"]


class StorageError(Exception):
    """The backend could not confirm a safe operation."""


class UnownedObject(StorageError):
    """The private key or metadata could not establish ownership."""


def scope_id(*parts: str) -> str:
    return sha256("\0".join(parts).encode()).hexdigest()


class ArtifactInventory:
    def __init__(self, store):
        self.store = store
        self._key_pattern = re.compile(
            rf"{re.escape(store.prefix)}/([a-f0-9]{{2}})/([a-f0-9]{{64}})/([a-f0-9]{{64}})"
        )

    def contains(self, key):
        match = self._key_pattern.fullmatch(key)
        return bool(match and match[2].startswith(match[1]))

    def objects(self, referenced):
        paginator = self.store.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.store.bucket, Prefix=f"{self.store.prefix}/"):
            for row in page.get("Contents", []):
                key = row["Key"]
                if not self.contains(key) or key in referenced:
                    continue
                try:
                    if identity := self.identity(key):
                        yield key, identity
                except UnownedObject:
                    continue

    def identity(self, key):
        from botocore.exceptions import ClientError

        if not self.contains(key):
            raise UnownedObject("Artifact key is outside the private namespace")
        try:
            obj = self.store.client.head_object(Bucket=self.store.bucket, Key=key)
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code in {"404", "NoSuchKey", "NotFound"}:
                return None
            status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status == 403 or code in {"403", "AccessDenied", "Forbidden"}:
                # Prefix-scoped listing may establish absence when HEAD is denied.
                listing = self.store.client.list_objects_v2(
                    Bucket=self.store.bucket, Prefix=key, MaxKeys=1
                )
                if listing.get("IsTruncated") is False and all(
                    row["Key"] != key for row in listing.get("Contents", [])
                ):
                    return None
            raise
        namespace, digest = key.rsplit("/", 2)[1:]
        metadata = obj.get("Metadata", {})
        if metadata.get("sha256") != digest or metadata.get("namespace") != namespace:
            raise UnownedObject("Artifact metadata does not match its immutable key")
        return scope_id(obj["ETag"], obj["LastModified"].isoformat(), str(obj["ContentLength"]))

    def delete(self, key, expected=None):
        versioning = self.store.client.get_bucket_versioning(Bucket=self.store.bucket)
        if versioning.get("Status"):
            raise StorageError("Artifact deletion requires an unversioned bucket")
        identity = self.identity(key)
        if identity is None:
            return True
        if expected is not None and identity != expected:
            return False
        self.store.client.delete_object(Bucket=self.store.bucket, Key=key)
        if self.identity(key) is not None:
            raise StorageError("Artifact object deletion was not confirmed")
        return True


class VectorInventory:
    def __init__(self, client, collection, profile, connector):
        self.client = client
        self.collection = collection
        self.profile = profile
        self.connector = connector

    def exists(self):
        return self.client.collection_exists(self.collection)

    def _filter(self, **fields):
        return models.Filter(
            must=[
                models.FieldCondition(key=key, match=models.MatchValue(value=value))
                for key, value in {
                    "profile": self.profile,
                    "connector_id": self.connector,
                    **fields,
                }.items()
            ]
        )

    def _publication(self, point):
        payload = point.payload or {}
        if payload.get("profile") != self.profile or payload.get("connector_id") != self.connector:
            return None
        publication, chunk = payload.get("publication_id"), payload.get("chunk_id")
        if (
            not canonical_uuid(publication)
            or not isinstance(chunk, str)
            or not 1 <= len(chunk) <= 64
        ):
            return None
        if str(point.id) == point_id(publication, chunk):
            return publication
        return None

    def points(self):
        if not self.exists():
            return
        offset = None
        while True:
            points, offset = self.client.scroll(
                self.collection,
                scroll_filter=self._filter(),
                offset=offset,
                limit=250,
                with_payload=_POINT_PROVENANCE,
                with_vectors=False,
            )
            for point in points:
                if publication := self._publication(point):
                    yield str(point.id), publication
            if offset is None:
                return

    def _retrieve(self, target):
        return self.client.retrieve(
            self.collection, ids=[target], with_payload=_POINT_PROVENANCE, with_vectors=False
        )

    def delete(self, target, publication):
        if not canonical_uuid(target) or not canonical_uuid(publication):
            return False
        if not self.exists():
            return True
        point = self._retrieve(target)
        if not point:
            return True
        if self._publication(point[0]) != publication:
            return False
        result = self.client.delete(
            self.collection, points_selector=models.PointIdsList(points=[target]), wait=True
        )
        if result.status != models.UpdateStatus.COMPLETED or self._retrieve(target):
            raise StorageError("Vector deletion was not confirmed")
        return True

    def publication_empty(self, publication):
        if not self.exists():
            return True
        return (
            self.client.count(
                self.collection, count_filter=self._filter(publication_id=publication), exact=True
            ).count
            == 0
        )


def canonical_uuid(value):
    if not isinstance(value, str) or len(value) != 36:
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False
