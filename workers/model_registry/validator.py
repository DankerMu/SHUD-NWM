from __future__ import annotations

import os
import tarfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

REQUIRED_SUFFIXES = (".mesh", ".para", ".calib")


class ModelPackageValidationError(ValueError):
    """Raised when a SHUD model package is incomplete or inaccessible."""


@dataclass(frozen=True)
class ModelPackageValidationResult:
    package_path: str
    missing_patterns: tuple[str, ...]
    matched_files: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.missing_patterns


def validate_model_package_path(package_path: str | Path) -> ModelPackageValidationResult:
    path = Path(package_path).expanduser()
    if not path.exists():
        raise ModelPackageValidationError(f"Model package does not exist: {path}")

    if path.is_dir():
        names = [str(file.relative_to(path)) for file in path.rglob("*") if file.is_file() and file.stat().st_size > 0]
    elif tarfile.is_tarfile(path):
        with tarfile.open(path) as archive:
            names = [member.name for member in archive.getmembers() if member.isfile() and member.size > 0]
    else:
        names = [path.name] if path.is_file() and path.stat().st_size > 0 else []

    matched = tuple(name for name in names if any(name.endswith(suffix) for suffix in REQUIRED_SUFFIXES))
    missing = tuple(f"*{suffix}" for suffix in REQUIRED_SUFFIXES if not any(name.endswith(suffix) for name in names))
    result = ModelPackageValidationResult(str(path), missing, matched)
    if not result.passed:
        missing_text = ", ".join(f"Missing required file: {pattern}" for pattern in result.missing_patterns)
        raise ModelPackageValidationError(missing_text)
    return result


def validate_model_package_uri(model_package_uri: str) -> ModelPackageValidationResult | None:
    """Validate a model package URI when it is backed by the local object store.

    Remote S3 validation is intentionally skipped unless OBJECT_STORE_ROOT is configured,
    because this M1 implementation uses LocalObjectStore for file operations.
    """
    object_store_root = os.getenv("OBJECT_STORE_ROOT", "").strip()
    if not object_store_root:
        return None

    key = _object_key(model_package_uri, os.getenv("OBJECT_STORE_PREFIX", ""))
    return validate_model_package_path(Path(object_store_root).expanduser() / key)


def _object_key(uri_or_key: str, object_store_prefix: str) -> str:
    candidate = uri_or_key.strip()
    if not candidate:
        raise ModelPackageValidationError("model_package_uri is required")

    prefix = object_store_prefix.rstrip("/")
    if prefix and candidate.startswith(prefix + "/"):
        candidate = candidate[len(prefix) + 1 :]
    elif candidate.startswith("s3://"):
        candidate = urlparse(candidate).path.strip("/")
    return candidate.strip("/")
