from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    ensure_directory_no_follow,
    open_file_no_follow,
    read_bytes_limited_no_follow,
    read_bytes_no_follow,
    stat_no_follow,
    unlink_no_follow,
)
from packages.common.storage import validate_object_path

MAX_OBJECT_MANIFEST_BYTES = 16 * 1024 * 1024

#: The four states :meth:`LocalObjectStore.object_kind` can report.  Deliberately
#: four-valued rather than a boolean ``is_regular_file``: a boolean answers False
#: for an ABSENT target as well as for a directory, and the failure-state probe's
#: file-kind check must be a positive determination only (#1394).
OBJECT_KIND_FILE = "file"
OBJECT_KIND_DIRECTORY = "directory"
OBJECT_KIND_OTHER = "other"
OBJECT_KIND_ABSENT = "absent"


class ObjectStoreError(RuntimeError):
    """Raised when an object-store operation fails."""


def sha256_bytes(content: bytes) -> str:
    """Return the SHA-256 hex digest for bytes."""
    return hashlib.sha256(content).hexdigest()


def normalize_object_key(key_or_uri: str, object_store_prefix: str = "") -> str:
    """Normalize a recorded reference into a store key -- pure string work only.

    THE single normalization derivation (#1397).  ``LocalObjectStore.normalize_key``
    delegates here, and the failure-state shape classifier calls it directly so the
    classifier and the probe cannot frame the same reference differently: the
    classifier used to hand the RAW recorded value to the closed-world validator
    while the probe asked the store about the NORMALIZED key, which fabricated a
    witness beneath a physically present file key whenever the deployment prefix
    carried a path segment or the reference carried percent-encoding.

    Touching the filesystem is deliberately impossible here.  The classifier runs
    OUTSIDE the probe's own containment, and ``LocalObjectStore.__post_init__``
    would ``ensure_directory_no_follow`` the root and raise ``ObjectStoreError``
    (a ``RuntimeError``) that no ``except ValueError`` can fold -- one escape
    aborts the whole scheduler pass.  ``ValueError`` is this function's ONLY throw
    face, which is exactly what the classifier already contains.
    """

    candidate = key_or_uri.strip()
    if not candidate:
        raise ValueError("Object key is empty.")

    if candidate.startswith("s3://"):
        candidate = _normalize_s3_uri_key(candidate, object_store_prefix)
    elif object_store_prefix and candidate.startswith(object_store_prefix.rstrip("/") + "/"):
        candidate = candidate[len(object_store_prefix.rstrip("/")) + 1 :]

    candidate = candidate.strip("/")
    if ".." in Path(candidate).parts:
        raise ValueError(f"Object key must not contain '..': {key_or_uri}")
    return candidate


def _normalize_s3_uri_key(uri: str, object_store_prefix: str) -> str:
    """The ``s3://`` arm of :func:`normalize_object_key` (percent-decoding included).

    Unquoting happens on this arm ONLY; the bare-key arm above has never decoded
    and is not changed here.
    """

    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise ValueError(f"Invalid S3 URI: {uri}")

    decoded_path = unquote(parsed.path).strip("/")
    if object_store_prefix:
        prefix = urlparse(object_store_prefix.rstrip("/"))
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
        # ``Path.expanduser()`` raises an errno-less ``RuntimeError`` when the home
        # directory cannot be determined ("~nosuchuser/store", or "~/store" with no
        # HOME and no passwd entry).  The family primitive used elsewhere for this
        # throw face is not applicable here: keeping the literal would anchor the
        # store at the cwd and really create a "~nosuchuser" directory.  Convert it
        # into the domain error instead, so the callers that already handle
        # ``ObjectStoreError`` keep their fail-closed attributions (#1441).  Kept
        # separate from the ``ensure_directory_no_follow`` boundary below because
        # ``ObjectStoreError`` is itself a ``RuntimeError``.
        try:
            root = Path(self.root).expanduser()
        except RuntimeError as error:
            raise ObjectStoreError(f"Local object store root is not expandable: {self.root!r}: {error}") from error
        root = root if root.is_absolute() else Path.cwd() / root
        try:
            ensure_directory_no_follow(root)
        except SafeFilesystemError as error:
            raise ObjectStoreError(f"Local object store root is unsafe: {error}") from error
        object.__setattr__(self, "root", root)

    def exists(self, key_or_uri: str) -> bool:
        path = self.resolve_path(key_or_uri)
        if not self.root.exists():
            return False
        try:
            stat_no_follow(path, containment_root=self.root)
            return True
        except FileNotFoundError:
            return False
        except SafeFilesystemError as error:
            raise ObjectStoreError(f"Failed to check object existence for {key_or_uri}: {error}") from error

    def object_kind(self, key_or_uri: str) -> str:
        """Classify what stands at ``key_or_uri``: file / directory / other / absent.

        Pure addition alongside :meth:`exists`, whose semantics are unchanged: the
        20+ construction sites of this store keep the existence answer they have
        always had, and only the failure-state probe asks the sharper question
        (#1394).  ``stat_no_follow`` refuses to follow a symlinked leaf or
        ancestor but does NOT reject a directory (only ``open_file_no_follow``
        carries an ``S_ISREG`` check), which is why ``exists`` reports a directory
        squatting on a file key as present.

        Error containment is the same as ``exists`` -- ``FileNotFoundError``
        becomes ``absent`` and ``SafeFilesystemError`` becomes ``ObjectStoreError``
        -- so a caller that already handles the existence probe's faults handles
        this one with the same arm and no new reason vocabulary is introduced.
        """

        path = self.resolve_path(key_or_uri)
        if not self.root.exists():
            return OBJECT_KIND_ABSENT
        try:
            entry_stat = stat_no_follow(path, containment_root=self.root)
        except FileNotFoundError:
            return OBJECT_KIND_ABSENT
        except SafeFilesystemError as error:
            raise ObjectStoreError(f"Failed to classify object kind for {key_or_uri}: {error}") from error
        if stat.S_ISREG(entry_stat.st_mode):
            return OBJECT_KIND_FILE
        if stat.S_ISDIR(entry_stat.st_mode):
            return OBJECT_KIND_DIRECTORY
        return OBJECT_KIND_OTHER

    def read_bytes(self, key_or_uri: str) -> bytes:
        path = self.resolve_path(key_or_uri)
        try:
            return read_bytes_no_follow(path, containment_root=self.root)
        except (OSError, SafeFilesystemError) as error:
            raise ObjectStoreError(f"Failed to read object {key_or_uri}: {error}") from error

    def read_bytes_limited(self, key_or_uri: str, *, max_bytes: int) -> bytes:
        if max_bytes < 0:
            raise ValueError("max_bytes must be non-negative.")
        path = self.resolve_path(key_or_uri)
        try:
            content = read_bytes_limited_no_follow(path, max_bytes=max_bytes, containment_root=self.root)
            if len(content) > max_bytes:
                raise ObjectStoreError(
                    f"Object {key_or_uri} exceeds read limit: observed more than {max_bytes} bytes"
                )
            return content
        except ObjectStoreError:
            raise
        except (OSError, SafeFilesystemError) as error:
            raise ObjectStoreError(f"Failed to read object {key_or_uri}: {error}") from error

    def write_bytes_atomic(self, key_or_uri: str, content: bytes) -> str:
        path = self.resolve_path(key_or_uri)
        try:
            ensure_directory_no_follow(self.root)
            atomic_write_bytes_no_follow(path, content, containment_root=self.root, temp_suffix="part")
        except (OSError, SafeFilesystemError) as error:
            raise ObjectStoreError(f"Failed to write object {key_or_uri}: {error}") from error
        return self.uri_for_key(self.normalize_key(key_or_uri))

    def delete(self, key_or_uri: str) -> None:
        path = self.resolve_path(key_or_uri)
        if not self.root.exists():
            return
        try:
            unlink_no_follow(path, containment_root=self.root, missing_ok=True)
        except (OSError, SafeFilesystemError) as error:
            raise ObjectStoreError(f"Failed to delete object {key_or_uri}: {error}") from error

    def iter_bytes(self, key_or_uri: str, *, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        if chunk_size < 1:
            raise ValueError("chunk_size must be positive.")
        path = self.resolve_path(key_or_uri)
        try:
            file_fd = open_file_no_follow(path, containment_root=self.root)
        except (OSError, SafeFilesystemError) as error:
            raise ObjectStoreError(f"Failed to stream object {key_or_uri}: {error}") from error
        try:
            while chunk := os.read(file_fd, chunk_size):
                yield chunk
        except OSError as error:
            raise ObjectStoreError(f"Failed to stream object {key_or_uri}: {error}") from error
        finally:
            os.close(file_fd)

    def checksum(self, key_or_uri: str) -> str:
        return self.size_and_checksum(key_or_uri)[1]

    def checksum_limited(self, key_or_uri: str, *, max_bytes: int) -> str:
        return self.size_and_checksum_limited(key_or_uri, max_bytes=max_bytes)[1]

    def size_and_checksum(self, key_or_uri: str, *, chunk_size: int = 1024 * 1024) -> tuple[int, str]:
        digest = hashlib.sha256()
        size_bytes = 0
        for chunk in self.iter_bytes(key_or_uri, chunk_size=chunk_size):
            digest.update(chunk)
            size_bytes += len(chunk)
        return size_bytes, digest.hexdigest()

    def size_and_checksum_limited(
        self,
        key_or_uri: str,
        *,
        max_bytes: int,
    ) -> tuple[int, str]:
        if max_bytes < 0:
            raise ValueError("max_bytes must be non-negative.")
        content = self.read_bytes_limited(key_or_uri, max_bytes=max_bytes)
        return len(content), sha256_bytes(content)

    def size(self, key_or_uri: str) -> int:
        path = self.resolve_path(key_or_uri)
        try:
            return stat_no_follow(path, containment_root=self.root).st_size
        except (OSError, SafeFilesystemError) as error:
            raise ObjectStoreError(f"Failed to stat object {key_or_uri}: {error}") from error

    def resolve_path(self, key_or_uri: str) -> Path:
        key = self.normalize_key(key_or_uri)
        validation = validate_object_path(key)
        if not validation.valid:
            raise ValueError(validation.error)

        root = self.root
        target = root / key
        try:
            target.relative_to(root)
        except ValueError as error:
            raise ValueError(f"Object key escapes workspace root: {key}") from error
        return target

    def normalize_key(self, key_or_uri: str) -> str:
        return normalize_object_key(key_or_uri, self.object_store_prefix)

    def uri_for_key(self, key: str) -> str:
        normalized_key = self.normalize_key(key)
        if not self.object_store_prefix:
            return normalized_key
        return f"{self.object_store_prefix.rstrip('/')}/{normalized_key}"
