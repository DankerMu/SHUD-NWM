from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class HindcastConfig:
    workspace_root: Path
    object_store_root: Path
    object_store_prefix: str = ""
    slurm_gateway_url: str = "http://localhost:8000"
    slurm_client: Any | None = None
    db_session: Any | None = None

    @classmethod
    def from_env(cls) -> HindcastConfig:
        workspace_root = Path(os.getenv("WORKSPACE_ROOT", ".")).expanduser().resolve()
        return cls(
            workspace_root=workspace_root,
            object_store_root=Path(os.getenv("OBJECT_STORE_ROOT", str(workspace_root))).expanduser().resolve(),
            object_store_prefix=os.getenv("OBJECT_STORE_PREFIX", ""),
            slurm_gateway_url=os.getenv("SLURM_GATEWAY_URL", "http://localhost:8000"),
        )
