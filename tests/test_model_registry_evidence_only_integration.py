"""Evidence-only basins stay out of public discovery (#1729) -- real SQL.

The fake-cursor suite (``tests/test_model_registry_evidence_only_boundary.py``)
pins the SQL shape; this one runs it. Four basins in a per-test throwaway
database: an ``evidence-only`` fixture that sorts FIRST (so a filter applied
after ``LIMIT/OFFSET`` would short a page), a NULL-group basin, a ``Basins``
basin and one whose group merely resembles the excluded value. Every basin but
the last has a display-ready forecast run, so both ``list_basins`` paths see the
evidence row as a candidate.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from packages.common.model_registry import MissingResourceError, PsycopgModelRegistryStore
from tests.integration_helpers import apply_migrations_from_zero, psycopg_connection

pytestmark = pytest.mark.integration

_EVIDENCE = "basin__evidence_cmfd_p02_synth"
_NULL_GROUP = "it1729_null_group"
_BASINS_GROUP = "it1729_basins_group"
_LOOKALIKE = "it1729_lookalike_group"
# (basin_id, basin_name, basin_group, active model, ready forecast run)
_SEED: tuple[tuple[str, str, str | None, bool, bool], ...] = (
    (_EVIDENCE, "A Evidence", "evidence-only", False, True),
    (_NULL_GROUP, "B Null Group", None, True, True),
    (_BASINS_GROUP, "C Basins Group", "Basins", False, True),
    (_LOOKALIKE, "D Lookalike", "evidence", True, False),
)
_CYCLE = datetime(2026, 5, 14, 0, tzinfo=UTC)


def _seed(database_url: str) -> None:
    apply_migrations_from_zero(database_url)
    with psycopg_connection(database_url) as connection, connection.cursor() as cursor:
        for basin_id, name, group, active, ready_run in _SEED:
            cursor.execute(
                "INSERT INTO core.basin (basin_id, basin_name, basin_group, description) VALUES (%s, %s, %s, %s)",
                (basin_id, name, group, "#1729 integration"),
            )
            cursor.execute(
                """
                INSERT INTO core.basin_version (basin_version_id, basin_id, version_label, geom, active_flag)
                VALUES (%s, %s, 'v1', ST_Multi(ST_MakeEnvelope(99.9, 29.9, 100.6, 30.6, 4490)), true)
                """,
                (f"{basin_id}_v1", basin_id),
            )
            cursor.execute(
                """
                INSERT INTO core.river_network_version (
                    river_network_version_id, basin_version_id, version_label, segment_count
                )
                VALUES (%s, %s, 'v1', 0)
                """,
                (f"{basin_id}_rnv", f"{basin_id}_v1"),
            )
            cursor.execute(
                "INSERT INTO core.mesh_version (mesh_version_id, basin_version_id, version_label, mesh_uri) "
                "VALUES (%s, %s, 'v1', 'integration://mesh')",
                (f"{basin_id}_mesh", f"{basin_id}_v1"),
            )
            cursor.execute(
                """
                INSERT INTO core.model_instance (
                    model_id, basin_version_id, river_network_version_id, mesh_version_id,
                    calibration_version_id, shud_code_version, model_package_uri, active_flag, lifecycle_state
                )
                VALUES (%s, %s, %s, %s, 'calib-v1', 'shud-v1', 'integration://package/', %s, %s)
                """,
                (
                    f"{basin_id}_model",
                    f"{basin_id}_v1",
                    f"{basin_id}_rnv",
                    f"{basin_id}_mesh",
                    active,
                    "active" if active else "inactive",
                ),
            )
            if ready_run:
                cursor.execute(
                    """
                    INSERT INTO hydro.hydro_run (
                        run_id, run_type, scenario_id, model_id, basin_version_id,
                        cycle_time, start_time, end_time, status, run_manifest_uri
                    )
                    VALUES (%s, 'forecast', 'forecast_gfs_deterministic', %s, %s, %s, %s, %s, 'parsed',
                            'integration://manifest.json')
                    """,
                    (f"{basin_id}_run", f"{basin_id}_model", f"{basin_id}_v1", _CYCLE, _CYCLE, _CYCLE),
                )


def _ids(rows: list[dict[str, Any]], key: str = "basin_id") -> list[str]:
    return [row[key] for row in rows]


def test_list_basins_excludes_evidence_only_on_both_paths_and_pages_after_the_filter(
    throwaway_database_url: str,
) -> None:
    _seed(throwaway_database_url)
    store = PsycopgModelRegistryStore(throwaway_database_url)

    assert _ids(store.list_basins(limit=100, offset=0)) == [_NULL_GROUP, _BASINS_GROUP, _LOOKALIKE]
    assert _ids(store.list_basins(limit=100, offset=0, has_display_product=True)) == [_NULL_GROUP, _BASINS_GROUP]
    # Pages are cut from the filtered set: the evidence row, first by name, never
    # occupies a slot.
    pages = [_ids(store.list_basins(limit=1, offset=offset)) for offset in range(4)]
    assert pages == [[_NULL_GROUP], [_BASINS_GROUP], [_LOOKALIKE], []]
    display_pages = [_ids(store.list_basins(limit=1, offset=offset, has_display_product=True)) for offset in range(3)]
    assert display_pages == [[_NULL_GROUP], [_BASINS_GROUP], []]


def test_versions_of_an_evidence_only_basin_are_missing_and_others_are_kept(throwaway_database_url: str) -> None:
    _seed(throwaway_database_url)
    store = PsycopgModelRegistryStore(throwaway_database_url)

    with pytest.raises(MissingResourceError, match=f"basin_id not found: {_EVIDENCE}"):
        store.list_basin_versions(basin_id=_EVIDENCE, limit=10, offset=0)
    for basin_id in (_NULL_GROUP, _BASINS_GROUP, _LOOKALIKE):
        assert _ids(store.list_basin_versions(basin_id=basin_id, limit=10, offset=0), "basin_version_id") == [
            f"{basin_id}_v1"
        ]


def test_public_model_reads_exclude_evidence_only_basins(throwaway_database_url: str) -> None:
    _seed(throwaway_database_url)
    store = PsycopgModelRegistryStore(throwaway_database_url)

    everything = store.list_models(basin_version_id=None, active=None, limit=100, offset=0)
    assert everything["total"] == 3
    assert sorted(_ids(everything["items"])) == [_BASINS_GROUP, _LOOKALIKE, _NULL_GROUP]
    inactive = store.list_models(basin_version_id=None, active=False, limit=100, offset=0)
    assert (inactive["total"], _ids(inactive["items"])) == (1, [_BASINS_GROUP])
    scoped = store.list_models(basin_version_id=f"{_EVIDENCE}_v1", active=None, limit=100, offset=0)
    assert (scoped["total"], scoped["items"]) == (0, [])

    with pytest.raises(MissingResourceError, match="model_id not found"):
        store.get_model(f"{_EVIDENCE}_model")
    assert store.get_model(f"{_NULL_GROUP}_model")["basin_id"] == _NULL_GROUP
    # The scheduler / lifecycle read is not a public surface and stays unfiltered.
    assert store.get_model_internal(f"{_EVIDENCE}_model")["basin_id"] == _EVIDENCE
