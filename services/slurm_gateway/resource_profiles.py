"""Resource profile file loading shared by the Slurm gateway and the scheduler.

The gateway resolves an array's cpus/mem/walltime from ``default`` plus
``overrides[model_id]`` of task 0 only.  The scheduler therefore splits cohorts
by the same override key set (#2543) and must read the exact same file with the
exact same fail-closed semantics; both sides go through this module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from services.slurm_gateway.gateway import ConfigurationError


def load_resource_profiles(path: str | Path) -> dict[str, Any]:
    resolved_path = Path(path).expanduser()
    if not resolved_path.exists():
        raise ConfigurationError(
            "Resource profile configuration file does not exist.",
            {"resource_profiles_path": str(resolved_path)},
        )
    try:
        data = yaml.safe_load(resolved_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigurationError(
            "Resource profile configuration is not valid YAML.",
            {"resource_profiles_path": str(resolved_path)},
        ) from exc
    if not isinstance(data, dict):
        raise ConfigurationError("Resource profile configuration must include a default section.")
    profiles = data.get("resource_profiles", data)
    if not isinstance(profiles, dict) or not isinstance(profiles.get("default"), dict):
        raise ConfigurationError("Resource profile configuration must include a default section.")
    overrides = profiles.get("overrides", {})
    if overrides is None:
        profiles["overrides"] = {}
    elif not isinstance(overrides, dict):
        raise ConfigurationError("Resource profile overrides must be a mapping.")
    return profiles


def resource_profile_override_model_ids(path: str | Path) -> frozenset[str]:
    """Model ids whose gateway profile differs from ``default``.

    Mirrors ``RealSlurmBackend.resolve_resource_profile``: only mapping-valued
    override entries are applied by the gateway, so only those count.
    """

    overrides = load_resource_profiles(path).get("overrides") or {}
    return frozenset(str(model_id) for model_id, profile in overrides.items() if isinstance(profile, dict))
