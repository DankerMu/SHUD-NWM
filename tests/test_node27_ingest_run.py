from __future__ import annotations

from typing import Any

from scripts.node27_ingest_run import upsert_hydro_run


class RecordingCursor:
    def __init__(self) -> None:
        self.statement = ""
        self.parameters: tuple[Any, ...] = ()

    def execute(self, statement: str, parameters: tuple[Any, ...]) -> None:
        self.statement = statement
        self.parameters = parameters

    def fetchone(self) -> dict[str, str]:
        return {
            "run_id": "fcst_gfs_2026062700_basins_qhh_shud",
            "status": "succeeded",
            "output_uri": "s3://nhms/runs/fcst_gfs_2026062700_basins_qhh_shud/output/",
        }


def _manifest() -> dict[str, Any]:
    return {
        "identity": {
            "run_id": "fcst_gfs_2026062700_basins_qhh_shud",
            "scenario_id": "forecast_gfs_deterministic",
            "model_id": "basins_qhh_shud",
            "basin_version_id": "basins_qhh_vbasins",
            "forcing_version_id": "forc_gfs_2026062700_basins_qhh_shud",
        },
        "forcing": {
            "forcing_version_id": "forc_gfs_2026062700_basins_qhh_shud",
        },
        "initial_state": {
            "state_id": "state_gfs_basins_qhh_shud_2026062700_gfs_2026062612_f012",
        },
        "cycle_time": "2026-06-27T00:00:00Z",
        "start_time": "2026-06-27T00:00:00Z",
        "end_time": "2026-07-04T00:00:00Z",
        "run_type": "forecast",
        "scenario_id": "forecast_gfs_deterministic",
    }


def test_upsert_hydro_run_revives_superseded_cold_start_placeholder() -> None:
    cursor = RecordingCursor()

    result = upsert_hydro_run(cursor, _manifest(), "gfs")

    assert result["status"] == "succeeded"
    assert "WHEN hydro.hydro_run.status = 'superseded' THEN EXCLUDED.status" in cursor.statement
    assert "ELSE hydro.hydro_run.status" in cursor.statement
    assert cursor.parameters[6] == "state_gfs_basins_qhh_shud_2026062700_gfs_2026062612_f012"


def test_register_upsert_never_writes_parsed_at() -> None:
    """#1789: the register must not touch ``hydro_run.parsed_at``.

    Same shape as the existing ``updated_at`` reasoning, inverted. This
    statement runs for EVERY run on EVERY tick and deliberately bumps
    ``updated_at``; that is exactly why ``updated_at`` cannot serve as a parse
    timestamp. ``parsed_at`` is read by the autopipe completeness criterion as
    "when did a parse last succeed", so a register bump would freeze recompute
    detection into always claiming the ingested data is current.
    """
    cursor = RecordingCursor()

    upsert_hydro_run(cursor, _manifest(), "gfs")

    assert "parsed_at" not in cursor.statement
