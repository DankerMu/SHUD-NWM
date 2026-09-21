"""Package-version derivation and the required-field accessors it rests on.

Split out of ``scripts/publish_scheduler_file_registry.py`` by #1100.
``package_version_for_model`` is the per-model content_sha256/source_sha256
rule; the four ``_required_*`` / ``_slug_id`` helpers are its refusal surface
and are shared with the staging and publish paths.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from packages.scheduler.registry_audit import SchedulerRegistryPublishError
from scripts.publish_registry.constants import _SAFE_KEY_RE, DEFAULT_PACKAGE_VERSION_TEMPLATE


def package_version_for_model(
    model: Mapping[str, Any],
    template: str = DEFAULT_PACKAGE_VERSION_TEMPLATE,
    *,
    source_identity: Mapping[str, Any],
) -> str:
    basin_slug = str(model.get("basin_slug") or "")
    model_id = _required_model_str(model, "model_id")
    slug_id = _slug_id(basin_slug)
    content_hash = _required_source_identity_hash(source_identity, "content_sha256", model_id)[:12]
    source_hash = _required_source_identity_hash(source_identity, "source_sha256", model_id)[:8]
    try:
        version = template.format(
            slug=basin_slug.replace("/", "_"),
            slug_id=slug_id,
            model_id=model_id,
            content_hash=content_hash,
            source_hash=source_hash,
        )
    except KeyError as error:
        raise SchedulerRegistryPublishError(
            "SCHEDULER_REGISTRY_VERSION_TEMPLATE_INVALID",
            "Package version template contains an unsupported placeholder.",
            details={"placeholder": str(error), "template": template},
        ) from error
    if not _SAFE_KEY_RE.fullmatch(version) or version in {".", ".."}:
        raise SchedulerRegistryPublishError(
            "SCHEDULER_REGISTRY_PACKAGE_VERSION_UNSAFE",
            "Package version must be a safe object-store path segment.",
            details={"model_id": model_id, "version": version},
        )
    return version

def _required_source_identity_hash(identity: Mapping[str, Any], field: str, model_id: str) -> str:
    value = identity.get(field)
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise SchedulerRegistryPublishError(
            "SCHEDULER_REGISTRY_SOURCE_IDENTITY_INVALID",
            "Package source identity is missing a canonical SHA-256 digest.",
            details={"model_id": model_id, "field": field},
        )
    return value

def _slug_id(value: str) -> str:
    normalized = re.sub(r"[^0-9a-zA-Z]+", "_", value).strip("_").lower()
    return normalized or "unknown"

def _required_model_str(model: Mapping[str, Any], field: str) -> str:
    value = model.get(field)
    if value in (None, ""):
        raise SchedulerRegistryPublishError(
            "SCHEDULER_REGISTRY_MODEL_FIELD_MISSING",
            "Basins model is missing a required field.",
            details={"field": field, "model": dict(model)},
        )
    return str(value)

def _required_path(value: str | Path | None, env_name: str) -> str:
    if value in (None, ""):
        raise SchedulerRegistryPublishError(
            "SCHEDULER_REGISTRY_REQUIRED_PATH_MISSING",
            f"{env_name} or the matching CLI option is required.",
            details={"env": env_name},
        )
    return str(value)
