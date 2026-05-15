from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from packages.common.object_store import LocalObjectStore
from packages.common.test_netcdf4 import encode_test_netcdf4
from tests.integration_helpers import (
    BASIN_VERSION_ID,
    CYCLE_TIME,
    FORECAST_RUN_ID,
    ISSUE_126_PREFIX,
    MODEL_ID,
    RIVER_NETWORK_VERSION_ID,
    SOURCE_ID,
    apply_migrations_from_zero,
    seed_issue_126_data,
    sqlalchemy_engine,
)
from workers.canonical_converter.converter import VARIABLE_MAPPING, CanonicalConverter, CanonicalConverterConfig
from workers.flood_frequency.return_period import compute_return_periods
from workers.forcing_producer import ForcingProducer, ForcingProducerConfig
from workers.output_parser import HydroRunContext, OutputParser, OutputParserConfig, RiverSegmentOrder
from workers.shud_runtime.runtime import SHUDRuntime, SHUDRuntimeConfig

pytestmark = pytest.mark.integration


def test_worker_chain_smoke_uses_real_schema_and_local_object_store(
    integration_database_url: str,
    tmp_path: Path,
) -> None:
    apply_migrations_from_zero(integration_database_url)
    object_root = tmp_path / "object-store"
    seed_issue_126_data(integration_database_url, object_root=object_root)
    store = LocalObjectStore(object_root, "s3://nhms")
    _seed_worker_support_rows(integration_database_url)
    manifest = _write_raw_manifest(store)

    canonical = CanonicalConverter(
        config=CanonicalConverterConfig(workspace_root=tmp_path, object_store_root=object_root, object_store_prefix="s3://nhms"),
        repository=_PsycopgCanonicalRepository(integration_database_url),
        object_store=store,
    )
    conversion = canonical.convert_manifest(manifest)
    assert conversion.status == "canonical_ready"
    assert len(conversion.products) == len(VARIABLE_MAPPING)

    forcing = ForcingProducer(
        config=ForcingProducerConfig(
            workspace_root=tmp_path,
            object_store_root=object_root,
            object_store_prefix="s3://nhms",
            idw_neighbors=1,
        ),
        repository=_PsycopgForcingRepository(integration_database_url),
        object_store=store,
    )
    forcing_result = forcing.produce(source_id=SOURCE_ID, cycle_time=CYCLE_TIME, model_id=MODEL_ID, max_lead_hours=0)
    assert forcing_result.status == "forcing_ready"
    assert forcing_result.station_count == 1
    assert store.exists(forcing_result.file_uris["tsd_forc"])

    _write_runtime_model_package(object_root)
    runtime = SHUDRuntime(
        config=SHUDRuntimeConfig(
            workspace_root=tmp_path / "workspace",
            object_store_root=object_root,
            object_store_prefix="s3://nhms",
            shud_executable=str(Path("tests/mock_shud_omp.py").resolve()),
            output_interval_minutes=60,
            timeout_seconds=30,
        ),
        repository=_PsycopgHydroRepository(integration_database_url),
        object_store=store,
    )
    runtime_result = runtime.execute(
        _runtime_manifest(forcing_result.forcing_version_id, forcing_result.forcing_package_uri)
    )
    assert runtime_result.status == "succeeded"
    assert store.exists(runtime_result.output_uri)

    parser = OutputParser(
        config=OutputParserConfig(object_store_root=object_root, object_store_prefix="s3://nhms", batch_size=10),
        repository=_OutputRepository(integration_database_url),
        object_store=store,
    )
    parsed = parser.parse_run(FORECAST_RUN_ID)
    assert parsed.status == "parsed"
    assert parsed.rows_written == 4

    engine = sqlalchemy_engine(integration_database_url)
    try:
        with Session(engine) as session:
            stats = compute_return_periods(FORECAST_RUN_ID, session)
            rows = session.execute(
                text("SELECT river_segment_id, return_period FROM flood.return_period_result WHERE run_id = :run_id"),
                {"run_id": FORECAST_RUN_ID},
            ).mappings().all()
    finally:
        engine.dispose()

    assert stats.rows_written >= 4
    assert {row["river_segment_id"] for row in rows} >= {
        f"{ISSUE_126_PREFIX}_seg_inside",
        f"{ISSUE_126_PREFIX}_seg_outside",
    }


def _write_raw_manifest(store: LocalObjectStore) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for variable in VARIABLE_MAPPING:
        key = f"raw/gfs/2026050300/{variable}.nc"
        store.write_bytes_atomic(key, encode_test_netcdf4(variable, 0, values=[300.0], cycle_time=CYCLE_TIME))
        entries.append({"local_key": key, "variable": variable, "forecast_hour": 0, "remote_url": f"mock://{variable}"})
    return {"source_id": SOURCE_ID, "cycle_time": CYCLE_TIME.isoformat(), "entries": entries}


def _seed_worker_support_rows(database_url: str) -> None:
    from tests.integration_helpers import psycopg_connection

    with psycopg_connection(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM hydro.river_timeseries WHERE run_id = %s", (FORECAST_RUN_ID,))
            cursor.execute(
                "UPDATE hydro.hydro_run SET status = 'failed', output_uri = NULL, log_uri = NULL WHERE run_id = %s",
                (FORECAST_RUN_ID,),
            )
            cursor.execute(
                """
                INSERT INTO met.met_station (
                    station_id, basin_version_id, station_name, geom, elevation_m, station_role, active_flag
                )
                VALUES (
                    'it126_station', %s, 'Integration Station', ST_SetSRID(ST_MakePoint(110.2, 30.2), 4490),
                    25.0, 'forcing_proxy', true
                )
                ON CONFLICT (station_id) DO UPDATE SET active_flag = EXCLUDED.active_flag
                """,
                (BASIN_VERSION_ID,),
            )


def _write_runtime_model_package(object_root: Path) -> None:
    package = object_root / "models" / "it126" / "package"
    package.mkdir(parents=True, exist_ok=True)
    (package / "it126.mesh").write_text("mesh\n", encoding="utf-8")
    (package / "it126.para").write_text(
        "START_TIME = START_TIME\nEND_TIME = END_TIME\nOUTPUT_DIR = OUTPUT_DIR\nMODEL_OUTPUT_INTERVAL = 60\n",
        encoding="utf-8",
    )
    (package / "it126.calib").write_text("calib\n", encoding="utf-8")


def _runtime_manifest(forcing_version_id: str, forcing_package_uri: str) -> dict[str, Any]:
    return {
        "run_id": FORECAST_RUN_ID,
        "run_type": "forecast",
        "scenario_id": "forecast_gfs_deterministic",
        "source_id": SOURCE_ID,
        "cycle_time": CYCLE_TIME.isoformat(),
        "start_time": datetime(2026, 5, 3, 1, tzinfo=UTC).isoformat(),
        "end_time": datetime(2026, 5, 3, 2, tzinfo=UTC).isoformat(),
        "model": {
            "model_id": MODEL_ID,
            "basin_version_id": BASIN_VERSION_ID,
            "river_network_version_id": RIVER_NETWORK_VERSION_ID,
            "model_package_uri": "s3://nhms/models/it126/package/",
            "project_name": "demo",
            "segment_count": 2,
        },
        "initial_state": {"state_id": None, "ic_file_uri": None},
        "forcing": {
            "forcing_version_id": forcing_version_id,
            "forcing_uri": forcing_package_uri,
        },
        "outputs": {
            "output_uri": f"s3://nhms/runs/{FORECAST_RUN_ID}/output/",
            "log_uri": f"s3://nhms/runs/{FORECAST_RUN_ID}/logs/",
        },
    }


class _PsycopgCanonicalRepository:
    def __init__(self, database_url: str) -> None:
        from packages.common.met_store import PsycopgMetStore

        self.store = PsycopgMetStore(database_url)

    def get_canonical_product(self, *, canonical_product_id: str) -> dict[str, Any] | None:
        return self.store.get_canonical_product(canonical_product_id=canonical_product_id)

    def upsert_canonical_product(self, record: dict[str, Any]) -> dict[str, Any]:
        return self.store.upsert_canonical_product(record)

    def update_forecast_cycle(self, **kwargs: Any) -> dict[str, Any] | None:
        return self.store.update_forecast_cycle(**kwargs)


class _PsycopgForcingRepository:
    def __init__(self, database_url: str) -> None:
        from workers.forcing_producer.store import PsycopgForcingRepository

        self.store = PsycopgForcingRepository(database_url)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.store, name)


class _PsycopgHydroRepository:
    def __init__(self, database_url: str) -> None:
        from workers.shud_runtime.runtime import PsycopgHydroRunRepository

        self.store = PsycopgHydroRunRepository(database_url)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.store, name)


class _OutputRepository:
    def __init__(self, database_url: str) -> None:
        from workers.output_parser.parser import PsycopgOutputParserRepository

        self.store = PsycopgOutputParserRepository(database_url)

    def load_run_context(self, run_id: str) -> HydroRunContext:
        context = self.store.load_run_context(run_id)
        return HydroRunContext(
            run_id=context.run_id,
            model_id=context.model_id,
            basin_version_id=context.basin_version_id,
            river_network_version_id=context.river_network_version_id,
            source_id=context.source_id,
            cycle_id=context.cycle_id,
            cycle_time=context.cycle_time,
            start_time=context.start_time,
            output_uri=context.output_uri,
            run_type=context.run_type,
            scenario_id=context.scenario_id,
        )

    def load_river_segments(self, river_network_version_id: str) -> tuple[RiverSegmentOrder, ...]:
        del river_network_version_id
        return (
            RiverSegmentOrder(f"{ISSUE_126_PREFIX}_seg_inside", RIVER_NETWORK_VERSION_ID, 1),
            RiverSegmentOrder(f"{ISSUE_126_PREFIX}_seg_outside", RIVER_NETWORK_VERSION_ID, 2),
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self.store, name)
