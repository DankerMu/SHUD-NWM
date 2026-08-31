from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from packages.common.object_store import MAX_OBJECT_MANIFEST_BYTES

DEFAULT_BASINS_MIGRATION_SOURCE_URI = "/volume/data/nwm/Basins"
DEFAULT_OBJECT_STORE_TARGET = "local-production-like"
DEFAULT_CLEANUP_POLICY = "quarantine"
FORBIDDEN_RUNTIME_SOURCE_FRAGMENTS = ("data/Basins", "/volume/")
SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:_-]{0,127}$")
MAX_PERCENT_DECODE_ROUNDS = 4
ENCODED_SEPARATOR_RE = re.compile(r"%(?:2f|5c)", re.IGNORECASE)
MAX_STORED_MANIFEST_BYTES = MAX_OBJECT_MANIFEST_BYTES
MAX_RAW_INTERMEDIATE_BYTES = 64 * 1024 * 1024
MAX_RUNTIME_STAGING_OBJECT_BYTES = 8 * 1024 * 1024
MAX_RUNTIME_STAGING_FILE_COUNT = 128
MAX_RUNTIME_STAGING_NODE_COUNT = 128
MAX_RUNTIME_STAGING_DIRECTORY_DEPTH = 16
MAX_RUNTIME_STAGING_TOTAL_BYTES = 64 * 1024 * 1024
MAX_DESCENDANT_SYMLINK_SCAN_NODES = 8192
RUNTIME_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
RUNTIME_READ_FLAGS = (
    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
)
SENSITIVE_PREFIX_ASSIGNMENT_RE = re.compile(
    r"(?:^|[;?#&])[^=/?#;&]*(?:token|password|passwd|pwd|secret|credential|api[_-]?key|access[_-]?key|"
    r"session[_-]?key|signature|x-amz-signature)[^=/?#;&]*=",
    re.IGNORECASE,
)
SENSITIVE_PREFIX_SEPARATOR_RE = re.compile(r"[/;?#&]")


class ProductionObjectStoreValidationError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


@dataclass(frozen=True)
class PackageChecksumReconstruction:
    checksum: str | None
    status: str
    identity_basis: str
    limitation: str | None = None


@dataclass
class RuntimeStagingBudget:
    max_file_count: int
    max_directory_depth: int
    max_total_bytes: int
    max_object_bytes: int
    max_node_count: int | None = None
    file_count: int = 0
    total_bytes: int = 0
    node_count: int = 0

    def __post_init__(self) -> None:
        if self.max_node_count is None:
            self.max_node_count = self.max_file_count

    def reserve_node(self, *, relative_path: str) -> None:
        if self.node_count + 1 > int(self.max_node_count):
            raise ProductionObjectStoreValidationError(
                "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
                (
                    "Runtime staging prefix exceeds configured traversal node "
                    f"limit of {self.max_node_count}: {relative_path}"
                ),
            )
        self.node_count += 1

    def reserve(self, *, relative_path: str, size_bytes: int) -> None:
        depth = len(PurePosixPath(relative_path).parts)
        if depth > self.max_directory_depth:
            raise ProductionObjectStoreValidationError(
                "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
                (
                    "Runtime staging object exceeds configured directory depth "
                    f"limit of {self.max_directory_depth}: {relative_path}"
                ),
            )
        if size_bytes > self.max_object_bytes:
            raise ProductionObjectStoreValidationError(
                "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
                (
                    "Runtime staging object exceeds configured per-object byte "
                    f"limit of {self.max_object_bytes}: {relative_path}"
                ),
            )
        if self.file_count + 1 > self.max_file_count:
            raise ProductionObjectStoreValidationError(
                "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
                (f"Runtime staging exceeds configured staged file count limit of {self.max_file_count}."),
            )
        if self.total_bytes + size_bytes > self.max_total_bytes:
            raise ProductionObjectStoreValidationError(
                "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
                (f"Runtime staging exceeds configured total byte limit of {self.max_total_bytes}."),
            )
        self.file_count += 1
        self.total_bytes += size_bytes

    def to_payload(self) -> dict[str, int]:
        return {
            "max_file_count": self.max_file_count,
            "max_node_count": int(self.max_node_count),
            "max_directory_depth": self.max_directory_depth,
            "max_total_bytes": self.max_total_bytes,
            "max_object_bytes": self.max_object_bytes,
            "staged_file_count": self.file_count,
            "traversed_node_count": self.node_count,
            "staged_total_bytes": self.total_bytes,
        }


@dataclass(frozen=True)
class RuntimeStagedObject:
    target: Path
    content: bytes
    receipt: dict[str, Any]


@dataclass(frozen=True)
class RuntimeStagingPreparation:
    cfg_path: Path
    package_receipts: list[dict[str, Any]]
    forcing_receipts: list[dict[str, Any]]
    forcing_prefix_receipt: dict[str, Any] | None
    staged_files: list[str]
    budgets: dict[str, int]


@dataclass(frozen=True)
class RuntimePrefixCollection:
    objects: list[RuntimeStagedObject]
    prefix_receipt: dict[str, Any] | None = None


def _deterministic_manifest_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _sha256_json(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
