from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ERA5_REQUIRED_CANONICAL_VARIABLES: tuple[str, ...] = (
    "prcp_rate_or_amount",
    "air_temperature_2m",
    "relative_humidity_2m",
    "wind_u_10m",
    "wind_v_10m",
    "pressure_surface",
    "net_radiation",
)


@dataclass(frozen=True)
class HindcastConfig:
    workspace_root: Path
    object_store_root: Path
    object_store_prefix: str = ""
    slurm_gateway_url: str = "http://localhost:8000"
    slurm_client: Any | None = None
    db_session: Any | None = None
    era5_required_variables: tuple[str, ...] = ERA5_REQUIRED_CANONICAL_VARIABLES

    @classmethod
    def from_env(cls) -> HindcastConfig:
        workspace_root = Path(os.getenv("WORKSPACE_ROOT", ".")).expanduser().resolve()
        required_variables = os.getenv(
            "HINDCAST_ERA5_REQUIRED_VARIABLES",
            ",".join(ERA5_REQUIRED_CANONICAL_VARIABLES),
        )
        variables = tuple(
            variable.strip()
            for variable in required_variables.split(",")
            if variable.strip()
        )
        return cls(
            workspace_root=workspace_root,
            object_store_root=Path(os.getenv("OBJECT_STORE_ROOT", str(workspace_root))).expanduser().resolve(),
            object_store_prefix=os.getenv("OBJECT_STORE_PREFIX", ""),
            slurm_gateway_url=os.getenv("SLURM_GATEWAY_URL", "http://localhost:8000"),
            era5_required_variables=variables,
        )
