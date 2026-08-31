"""Public facade for the one node-27 cold-tablespace installer state machine.

The implementation is intentionally partitioned so no source file approaches
this repository's size guard.  Production CLI and imported disposable-oracle
APIs both reach the same ``run_install`` owner in ``node27_cold_tablespace_engine``.
"""

from __future__ import annotations

from packages.common.node27_cold_tablespace_engine import run_install
from packages.common.node27_cold_tablespace_identity import (
    COLD_TABLESPACE,
    PRODUCTION_HOST_PATH,
)
from packages.common.node27_cold_tablespace_identity import (
    CONTAINER_COLD_PATH as COLD_CONTAINER_PATH,
)
from packages.common.node27_cold_tablespace_topology import WRITER_TIMER_UNITS
from packages.common.node27_cold_tablespace_types import (
    InstallConfig,
    InstallDependencies,
    InstallInterrupted,
    InstallResult,
)

COLD_HOST_PATH = str(PRODUCTION_HOST_PATH)

# Compatibility seam for existing callers/tests that explicitly exercise an
# authority unlink failure.  The engine delegates terminal closure to recovery;
# new tests use dependency injection rather than monkeypatching this alias.

__all__ = [
    "COLD_CONTAINER_PATH",
    "COLD_HOST_PATH",
    "COLD_TABLESPACE",
    "InstallConfig",
    "InstallDependencies",
    "InstallInterrupted",
    "InstallResult",
    "WRITER_TIMER_UNITS",
    "run_install",
]
