from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

# #1813: forcing CSV payload evidence left package identity here.  The bump is
# itself the named identity migration -- BASINS_PACKAGE_SCHEMA_VERSION is inside
# the content material, so a republished package re-mints its identity under a
# declared packaging schema change rather than under a silent content change.
BASINS_PACKAGE_SCHEMA_VERSION_V1 = "basins.package.v1"
BASINS_PACKAGE_SCHEMA_VERSION = "basins.package.v2"
SUPPORTED_BASINS_PACKAGE_SCHEMA_VERSIONS: tuple[str, ...] = (
    BASINS_PACKAGE_SCHEMA_VERSION_V1,
    BASINS_PACKAGE_SCHEMA_VERSION,
)
BASINS_PACKAGE_SOURCE_IDENTITY_SCHEMA_VERSION = "basins.package.source_identity.v1"
BASINS_MIGRATION_REPORT_SCHEMA_VERSION = "basins.migration.v1"
FORCING_SAMPLE_BYTE_LIMIT = 64 * 1024
FORCING_SAMPLE_LINE_LIMIT = 1000


class BasinsPackageError(RuntimeError):
    """Raised when Basins package publication or migration evidence fails."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        model_id: str | None = None,
        version: str | None = None,
        path: str | None = None,
        manifest_uri: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.model_id = model_id
        self.version = version
        self.path = path
        self.manifest_uri = manifest_uri
        self.details = dict(details or {})

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error_code": self.error_code, "message": str(self)}
        if self.model_id is not None:
            payload["model_id"] = self.model_id
        if self.version is not None:
            payload["version"] = self.version
        if self.path is not None:
            payload["path"] = self.path
        if self.manifest_uri is not None:
            payload["manifest_uri"] = self.manifest_uri
        payload.update(self.details)
        return payload

@dataclass(frozen=True)
class SourceFile:
    source_path: Path
    source_root: Path
    relative_path: str
    object_key: str
    object_uri: str
    role: str

@dataclass(frozen=True)
class ObjectStoreParent:
    path: Path
    name: str
    parent_fd: int | None = None

def forcing_checksum_material_for_schema_version(
    forcing: Mapping[str, Any],
    schema_version: str,
) -> dict[str, Any]:
    """Return the forcing contribution to ``package_checksum`` for one package schema generation.

    #1813: a package that declares ``forcing.policy = "excluded_by_default"``
    must not fold forcing CSV payload evidence into its identity.  From
    ``BASINS_PACKAGE_SCHEMA_VERSION`` on, the contribution is the declaration
    itself -- for both policies, because copied payloads already enter identity
    as ``included_files`` entries with ``role="forcing"``.

    The pre-migration shape is retained (not deleted) because published
    manifests are immutable evidence: anything reconstructing a stored
    manifest's ``package_checksum`` must use the shape declared by that
    manifest's own ``schema_version``.  Callers must reject unsupported
    versions before calling.
    """

    if schema_version == BASINS_PACKAGE_SCHEMA_VERSION_V1:
        return {
            "policy": forcing.get("policy"),
            "csv_count": forcing.get("csv_count"),
            "byte_count": forcing.get("byte_count"),
            "aggregate_checksum": forcing.get("aggregate_checksum"),
            "payload_copied": forcing.get("payload_copied"),
            "copied_file_count": forcing.get("copied_file_count"),
            "copied_byte_count": forcing.get("copied_byte_count"),
        }
    if schema_version == BASINS_PACKAGE_SCHEMA_VERSION:
        return {
            "policy": forcing.get("policy"),
            "payload_copied": forcing.get("payload_copied"),
        }
    raise ValueError(f"Unsupported Basins package schema_version: {schema_version!r}")

def _forcing_checksum_material(forcing: Mapping[str, Any]) -> dict[str, Any]:
    return forcing_checksum_material_for_schema_version(forcing, BASINS_PACKAGE_SCHEMA_VERSION)

def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")

def _sha256_json(payload: Any) -> str:
    return _sha256_bytes(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))

def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()

def _sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        return _sha256_handle(handle)

def _sha256_handle(handle: BinaryIO) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()
