"""Shared fakes of the replay lineage suite (#3210).

Split out of ``tests/test_replay_lineage.py``; its partitions
(``tests/test_replay_lineage*.py``) import from here. Not a test
module: pytest does not collect it.

Holds only what both partitions use: the ``BASIN_VERSION_ID`` and
``CUTOVER_VALID_TIME`` fixture constants and the in-memory
``_FakeAuditReader`` / ``_FakeCloneReader`` read protocols the resolver
takes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from services.orchestrator.replay_lineage import ReplayLineageAmbiguityError

# ---------------------------------------------------------------------------
# Fixture constants
# ---------------------------------------------------------------------------


BASIN_VERSION_ID = "basin_v01"
CUTOVER_VALID_TIME = datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# In-memory fakes for the resolver's read protocols
# ---------------------------------------------------------------------------


@dataclass
class _FakeAuditReader:
    """In-memory :class:`ReplayLineageAuditReader` for resolver tests.

    Records the ``basin_version_id`` on every call so tests can assert
    the resolver reads exactly once.
    """

    activations_by_basin: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def get_latest_direct_grid_activation(
        self, basin_version_id: str
    ) -> Mapping[str, Any] | None:
        self.calls.append(basin_version_id)
        return self.activations_by_basin.get(basin_version_id)


@dataclass
class _FakeCloneReader:
    """In-memory :class:`ReplayLineageCloneReader` for resolver tests.

    ``valid_times_by_model`` maps ``model_id`` -> the set of distinct
    ``valid_time`` values across the clone rows (mirrors the ``SELECT
    DISTINCT valid_time`` shape the production reader emits). Empty
    set = no clone rows for that model; >1 = raises
    :class:`ReplayLineageAmbiguityError`.
    """

    valid_times_by_model: dict[str, set[datetime]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def get_cutover_valid_time_for_model(
        self, model_id: str
    ) -> datetime | None:
        self.calls.append(model_id)
        distinct = self.valid_times_by_model.get(model_id, set())
        if not distinct:
            return None
        if len(distinct) > 1:
            raise ReplayLineageAmbiguityError(
                f"clone rows for model_id={model_id!r} disagree on valid_time: "
                f"{sorted(str(vt) for vt in distinct)}"
            )
        (only,) = distinct
        return only
