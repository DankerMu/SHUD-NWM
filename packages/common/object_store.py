from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from packages.common.storage import validate_object_path


class ObjectStoreError(RuntimeError):
    """Raised when an object-store operation fails."""


def sha256_bytes(content: bytes) -> str:
    """Return the SHA-256 hex digest for bytes."""
    return hashlib.sha256(content).hexdigest()


@dataclass(frozen=True)
class LocalObjectStore:
    """Filesystem-backed object store used by M1 workers and tests.

    The store accepts NHMS object keys and S3-style URIs, validates them against
    the shared storage layout, then writes the corresponding bytes under
    ``root``. Production deployments can replace this adapter with a true S3
    implementation without changing worker logic.
    """

    root: Path | str
    object_store_prefix: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).expanduser().resolve())

    def exists(self, key_or_uri: str) -> bool:
        try:
            return self.resolve_path(key_or_uri).exists()
        except OSError as error:
            raise ObjectStoreError(f"Failed to check object existence for {key_or_uri}: {error}") from error

    def read_bytes(self, key_or_uri: str) -> bytes:
        path = self.resolve_path(key_or_uri)
        try:
            return path.read_bytes()
        except OSError as error:
            raise ObjectStoreError(f"Failed to read object {key_or_uri}: {error}") from error

    def write_bytes_atomic(self, key_or_uri: str, content: bytes) -> str:
        path = self.resolve_path(key_or_uri)
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp_path.write_bytes(content)
            os.replace(temp_path, path)
        except OSError as error:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError as cleanup_error:
                raise ObjectStoreError(
                    f"Failed to write object {key_or_uri}: {error}; cleanup also failed: {cleanup_error}"
                ) from cleanup_error
            raise ObjectStoreError(f"Failed to write object {key_or_uri}: {error}") from error
        return self.uri_for_key(self.normalize_key(key_or_uri))

    def delete(self, key_or_uri: str) -> None:
        path = self.resolve_path(key_or_uri)
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            raise ObjectStoreError(f"Failed to delete object {key_or_uri}: {error}") from error

    def checksum(self, key_or_uri: str) -> str:
        return sha256_bytes(self.read_bytes(key_or_uri))

    def size(self, key_or_uri: str) -> int:
        path = self.resolve_path(key_or_uri)
        try:
            return path.stat().st_size
        except OSError as error:
            raise ObjectStoreError(f"Failed to stat object {key_or_uri}: {error}") from error

    def resolve_path(self, key_or_uri: str) -> Path:
        key = self.normalize_key(key_or_uri)
        validation = validate_object_path(key)
        if not validation.valid:
            raise ValueError(validation.error)

        root = self.root
        target = (root / key).resolve()
        try:
            target.relative_to(root)
        except ValueError as error:
            raise ValueError(f"Object key escapes workspace root: {key}") from error
        return target

    def normalize_key(self, key_or_uri: str) -> str:
        candidate = key_or_uri.strip()
        if not candidate:
            raise ValueError("Object key is empty.")

        if self.object_store_prefix and candidate.startswith(self.object_store_prefix.rstrip("/") + "/"):
            candidate = candidate[len(self.object_store_prefix.rstrip("/")) + 1 :]
        elif candidate.startswith("s3://"):
            candidate = urlparse(candidate).path.strip("/")

        candidate = candidate.strip("/")
        if ".." in Path(candidate).parts:
            raise ValueError(f"Object key must not contain '..': {key_or_uri}")
        return candidate

    def uri_for_key(self, key: str) -> str:
        normalized_key = self.normalize_key(key)
        if not self.object_store_prefix:
            return normalized_key
        return f"{self.object_store_prefix.rstrip('/')}/{normalized_key}"
