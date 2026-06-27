from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from services.orchestrator.chain import PsycopgOrchestratorRepository


class _NoRowsRepository(PsycopgOrchestratorRepository):
    def __init__(self) -> None:
        super().__init__("postgresql://example")

    def _fetch_optional(self, statement: str, parameters: tuple[Any, ...]) -> dict[str, Any] | None:
        del statement, parameters
        return None

    def _fetch_all(self, statement: str, parameters: tuple[Any, ...]) -> list[dict[str, Any]]:
        del statement, parameters
        return []


def _write_nfs_manifest(root: Path) -> None:
    local_key = "raw/gfs/2026062612/gfs.t12z.f000.bundle.grib2"
    raw_file = root / local_key
    raw_file.parent.mkdir(parents=True, exist_ok=True)
    raw_file.write_bytes(b"grib-bytes")
    manifest = {
        "source_id": "gfs",
        "cycle_time": "2026-06-26T12:00:00+00:00",
        "manifest_uri": "s3://nhms/raw/gfs/2026062612/manifest.json",
        "entries": [
            {
                "remote_url": "https://example.invalid/gfs",
                "local_key": local_key,
                "variable": "prcp_rate_or_amount",
                "forecast_hour": 0,
            }
        ],
    }
    (root / "raw/gfs/2026062612/manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_candidate_state_materializes_nfs_raw_manifest_without_local_db_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_nfs_manifest(tmp_path)
    monkeypatch.setenv("NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST", "true")
    monkeypatch.setenv("NHMS_SCHEDULER_NFS_RAW_MANIFEST_ROOT", str(tmp_path))
    monkeypatch.setenv("NHMS_SCHEDULER_NFS_RAW_MANIFEST_PREFIX", "s3://nhms")

    state = _NoRowsRepository().candidate_state(
        source_id="gfs",
        cycle_time=datetime(2026, 6, 26, 12, tzinfo=UTC),
        model_id="basins_qhh_shud",
        run_id="fcst_gfs_2026062612_basins_qhh_shud",
        forcing_version_id="forc_gfs_2026062612_basins_qhh_shud",
        candidate_id="gfs:2026-06-26T12:00:00Z:basins_qhh_shud:forecast_gfs_deterministic",
    )

    assert state is not None
    assert state["forecast_cycle"]["status"] == "raw_complete"
    assert state["forecast_cycle"]["manifest_uri"] == "s3://nhms/raw/gfs/2026062612/manifest.json"
    assert state["forecast_cycle"]["source_cycle_truth"] == "node27_nfs_raw_manifest"
    assert state["nfs_raw_manifest"]["status"] == "ready"


def test_candidate_state_reports_required_nfs_manifest_gap_without_fallback_db_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST", "true")
    monkeypatch.setenv("NHMS_SCHEDULER_NFS_RAW_MANIFEST_ROOT", str(tmp_path))

    state = _NoRowsRepository().candidate_state(
        source_id="gfs",
        cycle_time=datetime(2026, 6, 26, 12, tzinfo=UTC),
        model_id="basins_qhh_shud",
        run_id="fcst_gfs_2026062612_basins_qhh_shud",
        forcing_version_id="forc_gfs_2026062612_basins_qhh_shud",
        candidate_id="gfs:2026-06-26T12:00:00Z:basins_qhh_shud:forecast_gfs_deterministic",
    )

    assert state is not None
    assert state["forecast_cycle"] is None
    assert state["nfs_raw_manifest"]["status"] == "missing"
    assert state["nfs_raw_manifest"]["required"] is True
