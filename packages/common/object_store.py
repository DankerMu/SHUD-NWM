from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

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

    def read_bytes_limited(self, key_or_uri: str, *, max_bytes: int) -> bytes:
        if max_bytes < 0:
            raise ValueError("max_bytes must be non-negative.")
        path = self.resolve_path(key_or_uri)
        try:
            size_bytes = path.stat().st_size
            if size_bytes > max_bytes:
                raise ObjectStoreError(
                    f"Object {key_or_uri} exceeds read limit: {size_bytes} bytes > {max_bytes} bytes"
                )
            return path.read_bytes()
        except ObjectStoreError:
            raise
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

    def iter_bytes(self, key_or_uri: str, *, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        if chunk_size < 1:
            raise ValueError("chunk_size must be positive.")
        path = self.resolve_path(key_or_uri)
        try:
            with path.open("rb") as handle:
                while chunk := handle.read(chunk_size):
                    yield chunk
        except OSError as error:
            raise ObjectStoreError(f"Failed to stream object {key_or_uri}: {error}") from error

    def checksum(self, key_or_uri: str) -> str:
        return self.size_and_checksum(key_or_uri)[1]

    def size_and_checksum(self, key_or_uri: str, *, chunk_size: int = 1024 * 1024) -> tuple[int, str]:
        digest = hashlib.sha256()
        size_bytes = 0
        for chunk in self.iter_bytes(key_or_uri, chunk_size=chunk_size):
            digest.update(chunk)
            size_bytes += len(chunk)
        return size_bytes, digest.hexdigest()

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

        if candidate.startswith("s3://"):
            candidate = self._normalize_s3_uri(candidate)
        elif self.object_store_prefix and candidate.startswith(self.object_store_prefix.rstrip("/") + "/"):
            candidate = candidate[len(self.object_store_prefix.rstrip("/")) + 1 :]

        candidate = candidate.strip("/")
        if ".." in Path(candidate).parts:
            raise ValueError(f"Object key must not contain '..': {key_or_uri}")
        return candidate

    def uri_for_key(self, key: str) -> str:
        normalized_key = self.normalize_key(key)
        if not self.object_store_prefix:
            return normalized_key
        return f"{self.object_store_prefix.rstrip('/')}/{normalized_key}"

    def _normalize_s3_uri(self, uri: str) -> str:
        parsed = urlparse(uri)
        if parsed.scheme != "s3" or not parsed.netloc:
            raise ValueError(f"Invalid S3 URI: {uri}")

        decoded_path = unquote(parsed.path).strip("/")
        if self.object_store_prefix:
            prefix = urlparse(self.object_store_prefix.rstrip("/"))
            if prefix.scheme != "s3" or not prefix.netloc:
                raise ValueError(f"OBJECT_STORE_PREFIX must be an S3 URI when normalizing S3 URI inputs: {uri}")
            expected_path = unquote(prefix.path).strip("/")
            if parsed.netloc != prefix.netloc:
                raise ValueError(f"S3 URI bucket does not match configured object store prefix: {uri}")
            if expected_path:
                if decoded_path == expected_path:
                    raise ValueError(f"S3 URI must include an object key below configured prefix: {uri}")
                if not decoded_path.startswith(f"{expected_path}/"):
                    raise ValueError(f"S3 URI is outside configured object store prefix: {uri}")
                return decoded_path[len(expected_path) + 1 :]
        return decoded_path
