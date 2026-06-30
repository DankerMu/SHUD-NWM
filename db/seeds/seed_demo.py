"""Seed deterministic Yangtze demo data.

Database records, composite IDs, and object-storage key paths use canonical
source_id='gfs' for GFS.

Timeseries records use hourly half-open interval semantics: [START_TIME, END_TIME).
"""

from __future__ import annotations

import json
import math
import os
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

BASIN_ID = "yangtze"
BASIN_VERSION_ID = "yangtze_v2026_01"
RIVER_NETWORK_VERSION_ID = "yangtze_rivnet_v01"
MODEL_ID = "yangtze_shud_v12"
MESH_VERSION_ID = "yangtze_mesh_v01"
SOURCE_ID = "gfs"
SOURCE_ID_IN_IDS = "gfs"
CYCLE_ID = "gfs_2026050100"
FORCING_VERSION_ID = "forc_gfs_2026050100_yangtze_shud_v12"
RUN_ID = "fcst_gfs_2026050100_yangtze_shud_v12"
SCENARIO_ID = "forecast_gfs_deterministic"
IFS_SOURCE_ID = "IFS"
IFS_SOURCE_ID_IN_IDS = "ifs"
IFS_CYCLE_ID = "ifs_2026050100"
IFS_FORCING_VERSION_ID = "forc_ifs_2026050100_yangtze_shud_v12"
IFS_RUN_ID = "fcst_ifs_2026050100_yangtze_shud_v12"
IFS_SCENARIO_ID = "forecast_ifs_deterministic"
IFS_06Z_CYCLE_ID = "ifs_2026050106"
IFS_06Z_FORCING_VERSION_ID = "forc_ifs_2026050106_yangtze_shud_v12"
IFS_06Z_RUN_ID = "fcst_ifs_2026050106_yangtze_shud_v12"
TILE_LAYER_ID = "river_network_yangtze"
PIPELINE_JOB_ID = "job_download_gfs_2026050100"
QC_CHECKPOINT = "forcing_completeness"

START_TIME = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
END_TIME = datetime(2026, 5, 8, 0, 0, tzinfo=UTC)
FORECAST_HOURS = int((END_TIME - START_TIME).total_seconds() // 3600)
IFS_CYCLE_TIME = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
IFS_END_TIME = IFS_CYCLE_TIME + timedelta(hours=168)
IFS_FORECAST_HOURS = int((IFS_END_TIME - IFS_CYCLE_TIME).total_seconds() // 3600)
IFS_06Z_CYCLE_TIME = datetime(2026, 5, 1, 6, 0, tzinfo=UTC)
IFS_06Z_END_TIME = IFS_06Z_CYCLE_TIME + timedelta(hours=144)
IFS_06Z_FORECAST_HOURS = int((IFS_06Z_END_TIME - IFS_06Z_CYCLE_TIME).total_seconds() // 3600)

BASIN_GEOM_WKT = "MULTIPOLYGON(((90 25, 122 25, 122 35, 90 35, 90 25)))"
MODEL_PACKAGE_URI = f"s3://nhms/models/{MODEL_ID}/model_package.tar.gz"
FORCING_PACKAGE_URI = (
    f"s3://nhms/forcing/{SOURCE_ID_IN_IDS}/2026050100/{BASIN_VERSION_ID}/{MODEL_ID}/forcing_package.tar.gz"
)
RUN_MANIFEST_URI = f"s3://nhms/runs/{RUN_ID}/input/manifest.json"
RUN_OUTPUT_URI = f"s3://nhms/runs/{RUN_ID}/output/"
IFS_FORCING_PACKAGE_URI = (
    f"s3://nhms/forcing/{IFS_SOURCE_ID_IN_IDS}/2026050100/{BASIN_VERSION_ID}/{MODEL_ID}/forcing_package.tar.gz"
)
IFS_RUN_MANIFEST_URI = f"s3://nhms/runs/{IFS_RUN_ID}/input/manifest.json"
IFS_RUN_OUTPUT_URI = f"s3://nhms/runs/{IFS_RUN_ID}/output/"
IFS_06Z_FORCING_PACKAGE_URI = (
    f"s3://nhms/forcing/{IFS_SOURCE_ID_IN_IDS}/2026050106/{BASIN_VERSION_ID}/{MODEL_ID}/forcing_package.tar.gz"
)
IFS_06Z_RUN_MANIFEST_URI = f"s3://nhms/runs/{IFS_06Z_RUN_ID}/input/manifest.json"
IFS_06Z_RUN_OUTPUT_URI = f"s3://nhms/runs/{IFS_06Z_RUN_ID}/output/"

FORCING_VARIABLES = ("t2m", "rh2m", "wind_u", "wind_v", "precip", "srad")
RIVER_VARIABLES = ("q_down", "y_stage")


@dataclass(frozen=True)
class RiverSegment:
    river_segment_id: str
    segment_order: int
    downstream_segment_id: str | None
    length_m: float
    wkt: str


@dataclass(frozen=True)
class MetStation:
    station_id: str
    station_name: str
    lon: float
    lat: float
    elevation_m: float


S3_PLACEHOLDER_OBJECTS: dict[str, bytes] = {
    MODEL_PACKAGE_URI: b"NHMS demo model package placeholder\n",
    FORCING_PACKAGE_URI: b"NHMS demo forcing package placeholder\n",
    RUN_MANIFEST_URI: json.dumps(
        {
            "run_id": RUN_ID,
            "model_id": MODEL_ID,
            "forcing_version_id": FORCING_VERSION_ID,
            "start_time": START_TIME.isoformat(),
            "end_time": END_TIME.isoformat(),
        },
        indent=2,
    ).encode("utf-8"),
    IFS_FORCING_PACKAGE_URI: b"NHMS demo IFS forcing package placeholder\n",
    IFS_RUN_MANIFEST_URI: json.dumps(
        {
            "run_id": IFS_RUN_ID,
            "model_id": MODEL_ID,
            "forcing_version_id": IFS_FORCING_VERSION_ID,
            "source_id": IFS_SOURCE_ID,
            "scenario_id": IFS_SCENARIO_ID,
            "start_time": IFS_CYCLE_TIME.isoformat(),
            "end_time": IFS_END_TIME.isoformat(),
            "forecast_horizon_hours": 168,
        },
        indent=2,
    ).encode("utf-8"),
    IFS_06Z_FORCING_PACKAGE_URI: b"NHMS demo IFS 06Z forcing package placeholder\n",
    IFS_06Z_RUN_MANIFEST_URI: json.dumps(
        {
            "run_id": IFS_06Z_RUN_ID,
            "model_id": MODEL_ID,
            "forcing_version_id": IFS_06Z_FORCING_VERSION_ID,
            "source_id": IFS_SOURCE_ID,
            "scenario_id": IFS_SCENARIO_ID,
            "start_time": IFS_06Z_CYCLE_TIME.isoformat(),
            "end_time": IFS_06Z_END_TIME.isoformat(),
            "forecast_horizon_hours": 144,
        },
        indent=2,
    ).encode("utf-8"),
    f"s3://nhms/runs/{RUN_ID}/output/rivqdown.csv": b"river_segment_id,valid_time,q_down\n"
    b"yangtze_rivnet_v01_riv_0001,2026-05-01T00:00:00Z,820.5\n",
    f"s3://nhms/runs/{RUN_ID}/logs/run.log": b"Demo SHUD run completed successfully.\n",
    f"s3://nhms/runs/{IFS_RUN_ID}/output/rivqdown.csv": b"river_segment_id,valid_time,q_down\n"
    b"yangtze_rivnet_v01_riv_0001,2026-05-01T00:00:00Z,801.5\n",
    f"s3://nhms/runs/{IFS_RUN_ID}/logs/run.log": b"Demo IFS SHUD run completed successfully.\n",
    f"s3://nhms/runs/{IFS_06Z_RUN_ID}/output/rivqdown.csv": b"river_segment_id,valid_time,q_down\n"
    b"yangtze_rivnet_v01_riv_0001,2026-05-01T06:00:00Z,797.5\n",
    f"s3://nhms/runs/{IFS_06Z_RUN_ID}/logs/run.log": b"Demo IFS 06Z SHUD run completed successfully.\n",
    f"s3://nhms/states/{MODEL_ID}/2026050100/yangtze_v12.cfg.ic": b"NHMS demo initial state placeholder\n",
    "s3://nhms/raw/gfs/2026050100/gfs_t2m.grib2": b"NHMS demo raw GFS t2m placeholder\n",
    "s3://nhms/raw/ifs/2026050100/ifs_t2m.grib2": b"NHMS demo raw IFS t2m placeholder\n",
    "s3://nhms/raw/ifs/2026050106/ifs_t2m.grib2": b"NHMS demo raw IFS 06Z t2m placeholder\n",
    "s3://nhms/canonical/gfs/2026050100/t2m/data.nc": b"NHMS demo canonical t2m placeholder\n",
    "s3://nhms/canonical/ifs/2026050100/2t/data.nc": b"NHMS demo canonical IFS 2t placeholder\n",
}


def build_river_segment_id(index: int) -> str:
    return f"{RIVER_NETWORK_VERSION_ID}_riv_{index:04d}"


def build_station_id(index: int) -> str:
    return f"{BASIN_VERSION_ID}_stn_{index:04d}"


def build_river_segments() -> list[RiverSegment]:
    downstream_ids = {
        1: build_river_segment_id(2),
        2: build_river_segment_id(3),
        3: build_river_segment_id(4),
        4: build_river_segment_id(5),
        5: build_river_segment_id(6),
        6: build_river_segment_id(7),
        7: build_river_segment_id(8),
        8: build_river_segment_id(9),
        9: build_river_segment_id(10),
        10: None,
        11: build_river_segment_id(4),
        12: build_river_segment_id(6),
        13: build_river_segment_id(8),
        14: build_river_segment_id(9),
        15: build_river_segment_id(10),
    }
    lines = {
        1: "LINESTRING(91 31, 94 31.2)",
        2: "LINESTRING(94 31.2, 97 31)",
        3: "LINESTRING(97 31, 100 30.8)",
        4: "LINESTRING(100 30.8, 103 30.6)",
        5: "LINESTRING(103 30.6, 106 30.7)",
        6: "LINESTRING(106 30.7, 109 30.9)",
        7: "LINESTRING(109 30.9, 112 31.1)",
        8: "LINESTRING(112 31.1, 115 31)",
        9: "LINESTRING(115 31, 118 31.2)",
        10: "LINESTRING(118 31.2, 121 31)",
        11: "LINESTRING(98 33.5, 100 30.8)",
        12: "LINESTRING(104 34, 106 30.7)",
        13: "LINESTRING(110 33.8, 112 31.1)",
        14: "LINESTRING(114 28, 115 31)",
        15: "LINESTRING(119 29, 121 31)",
    }
    lengths = {
        1: 42000.0,
        2: 38000.0,
        3: 35000.0,
        4: 33000.0,
        5: 31000.0,
        6: 36000.0,
        7: 39000.0,
        8: 41000.0,
        9: 45000.0,
        10: 47000.0,
        11: 29000.0,
        12: 34000.0,
        13: 30000.0,
        14: 27000.0,
        15: 26000.0,
    }
    return [
        RiverSegment(
            river_segment_id=build_river_segment_id(index),
            segment_order=index,
            downstream_segment_id=downstream_ids[index],
            length_m=lengths[index],
            wkt=lines[index],
        )
        for index in range(1, 16)
    ]


def build_met_stations() -> list[MetStation]:
    return [
        MetStation(build_station_id(1), "Yangtze Proxy Station 0001", 92.5, 31.5, 421.0),
        MetStation(build_station_id(2), "Yangtze Proxy Station 0002", 99.0, 30.0, 315.0),
        MetStation(build_station_id(3), "Yangtze Proxy Station 0003", 106.0, 32.0, 185.0),
        MetStation(build_station_id(4), "Yangtze Proxy Station 0004", 113.0, 30.5, 72.0),
        MetStation(build_station_id(5), "Yangtze Proxy Station 0005", 119.0, 32.5, 22.0),
    ]


def hourly_times(start_time: datetime = START_TIME, forecast_hours: int = FORECAST_HOURS) -> list[datetime]:
    return [start_time + timedelta(hours=hour) for hour in range(forecast_hours)]


def forcing_unit(variable: str) -> str:
    return {
        "t2m": "degC",
        "rh2m": "%",
        "wind_u": "m/s",
        "wind_v": "m/s",
        "precip": "mm/h",
        "srad": "W/m2",
    }[variable]


def forcing_value(rng: random.Random, variable: str, valid_time: datetime) -> float:
    if variable == "t2m":
        return round(rng.uniform(15.0, 30.0), 3)
    if variable == "rh2m":
        return round(rng.uniform(40.0, 90.0), 3)
    if variable in {"wind_u", "wind_v"}:
        return round(rng.uniform(-5.0, 5.0), 3)
    if variable == "precip":
        return round(0.0 if rng.random() < 0.7 else rng.uniform(0.1, 5.0), 3)
    if variable == "srad":
        daylight = max(0.0, math.sin(math.pi * ((valid_time.hour % 24) - 6) / 12))
        return round(daylight * rng.uniform(450.0, 800.0), 3)
    raise ValueError(f"Unsupported forcing variable: {variable}")


def river_value(
    rng: random.Random,
    segment_order: int,
    variable: str,
    lead_time_hours: int,
    *,
    forecast_hours: int = FORECAST_HOURS,
    flow_offset: float = 0.0,
) -> float:
    daily_wave = math.sin(2 * math.pi * (lead_time_hours % 24) / 24)
    forecast_wave = math.sin(2 * math.pi * lead_time_hours / forecast_hours)
    base_q = 650.0 + segment_order * 95.0 + flow_offset
    q_down = max(80.0, base_q * (1.0 + 0.18 * daily_wave + 0.28 * forecast_wave) + rng.uniform(-55.0, 85.0))

    if variable == "q_down":
        return round(q_down, 3)
    if variable == "y_stage":
        return round(max(0.5, 1.8 + segment_order * 0.18 + q_down / 900.0 + rng.uniform(-0.15, 0.2)), 3)
    raise ValueError(f"Unsupported river variable: {variable}")


def load_database_url() -> str:
    from dotenv import load_dotenv

    load_dotenv()
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required to seed demo data.")
    return database_url


def connect_database(database_url: str) -> Any:
    import psycopg2

    connection = psycopg2.connect(database_url)
    connection.autocommit = True
    return connection


def seed_core(cursor: Any, json_adapter: Any, execute_values: Any) -> None:
    cursor.execute(
        """
        INSERT INTO core.basin (basin_id, basin_name, basin_group, description)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (BASIN_ID, "长江流域", "yangtze", "Demo Yangtze River basin."),
    )
    cursor.execute(
        """
        INSERT INTO core.basin_version (basin_version_id, basin_id, version_label, geom, active_flag, source_uri)
        VALUES (%s, %s, %s, ST_GeomFromText(%s, 4490), %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (BASIN_VERSION_ID, BASIN_ID, "v2026.01", BASIN_GEOM_WKT, True, "s3://nhms/basins/yangtze/v2026_01.geojson"),
    )
    cursor.execute(
        """
        INSERT INTO core.river_network_version (
            river_network_version_id, basin_version_id, version_label, segment_count
        )
        VALUES (%s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (RIVER_NETWORK_VERSION_ID, BASIN_VERSION_ID, "v01", 15),
    )

    river_segment_rows = [
        (
            segment.river_segment_id,
            RIVER_NETWORK_VERSION_ID,
            segment.segment_order,
            segment.downstream_segment_id,
            segment.length_m,
            segment.wkt,
            json_adapter({"demo_network": True, "segment_order": segment.segment_order}),
        )
        for segment in build_river_segments()
    ]
    execute_values(
        cursor,
        """
        INSERT INTO core.river_segment (
            river_segment_id,
            river_network_version_id,
            segment_order,
            downstream_segment_id,
            length_m,
            geom,
            properties_json
        )
        VALUES %s
        ON CONFLICT DO NOTHING
        """,
        river_segment_rows,
        # geom is geometry(MultiLineString, 4490) (000036); ST_Multi wraps the demo
        # LineString WKT so the seed satisfies the column type.
        template="(%s, %s, %s, %s, %s, ST_Multi(ST_GeomFromText(%s, 4490)), %s)",
        page_size=1000,
    )
    cursor.execute(
        """
        INSERT INTO core.mesh_version (mesh_version_id, basin_version_id, version_label, mesh_uri, properties_json)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (
            MESH_VERSION_ID,
            BASIN_VERSION_ID,
            "v01",
            f"s3://nhms/models/{MODEL_ID}/mesh/",
            json_adapter({"demo_mesh": True}),
        ),
    )
    cursor.execute(
        """
        INSERT INTO core.model_instance (
            model_id,
            basin_version_id,
            river_network_version_id,
            mesh_version_id,
            calibration_version_id,
            shud_code_version,
            model_package_uri,
            active_flag,
            lifecycle_state,
            resource_profile
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'active', %s)
        ON CONFLICT DO NOTHING
        """,
        (
            MODEL_ID,
            BASIN_VERSION_ID,
            RIVER_NETWORK_VERSION_ID,
            MESH_VERSION_ID,
            "yangtze_cal_v01",
            "2.0",
            MODEL_PACKAGE_URI,
            True,
            json_adapter({"cpu": 16, "memory_gb": 64, "walltime_hours": 3}),
        ),
    )


def seed_met(cursor: Any, json_adapter: Any, execute_values: Any, rng: random.Random) -> None:
    cursor.execute(
        """
        INSERT INTO met.data_source (source_id, source_name, source_type, status, adapter_name, config_json)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (
            SOURCE_ID,
            "GFS Mock",
            "global_forecast",
            "mock",
            "gfs_adapter",
            json_adapter({"mode": "demo", "cycle_hours": [0, 6, 12, 18]}),
        ),
    )
    cursor.execute(
        """
        INSERT INTO met.data_source (source_id, source_name, source_type, status, native_format, adapter_name)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (source_id) DO NOTHING
        """,
        (IFS_SOURCE_ID, "IFS Open Data", "forecast", "enabled", "GRIB2", "ifs_adapter"),
    )
    cursor.execute(
        """
        INSERT INTO met.forecast_cycle (cycle_id, source_id, cycle_time, issue_time, status, manifest_uri)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (CYCLE_ID, SOURCE_ID, START_TIME, START_TIME + timedelta(minutes=45), "complete", RUN_MANIFEST_URI),
    )
    execute_values(
        cursor,
        """
        INSERT INTO met.forecast_cycle (cycle_id, source_id, cycle_time, issue_time, status, manifest_uri)
        VALUES %s
        ON CONFLICT DO NOTHING
        """,
        [
            (
                IFS_CYCLE_ID,
                IFS_SOURCE_ID,
                IFS_CYCLE_TIME,
                IFS_CYCLE_TIME + timedelta(minutes=90),
                "complete",
                IFS_RUN_MANIFEST_URI,
            ),
            (
                IFS_06Z_CYCLE_ID,
                IFS_SOURCE_ID,
                IFS_06Z_CYCLE_TIME,
                IFS_06Z_CYCLE_TIME + timedelta(minutes=90),
                "complete",
                IFS_06Z_RUN_MANIFEST_URI,
            ),
        ],
        page_size=1000,
    )

    station_rows = [
        (
            station.station_id,
            BASIN_VERSION_ID,
            station.station_name,
            f"POINT({station.lon} {station.lat})",
            station.elevation_m,
            "forcing_proxy",
            True,
            json_adapter({"demo_station": True, "source": "mock_gfs"}),
        )
        for station in build_met_stations()
    ]
    execute_values(
        cursor,
        """
        INSERT INTO met.met_station (
            station_id,
            basin_version_id,
            station_name,
            geom,
            elevation_m,
            station_role,
            active_flag,
            properties_json
        )
        VALUES %s
        ON CONFLICT DO NOTHING
        """,
        station_rows,
        template="(%s, %s, %s, ST_GeomFromText(%s, 4490), %s, %s, %s, %s)",
        page_size=1000,
    )
    forcing_version_rows = [
        (
            FORCING_VERSION_ID,
            MODEL_ID,
            SOURCE_ID,
            START_TIME,
            START_TIME,
            END_TIME,
            5,
            FORCING_PACKAGE_URI,
            json_adapter({"forecast_cycle_id": CYCLE_ID, "source_id": SOURCE_ID, "max_lead_hours": 168}),
        ),
        (
            IFS_FORCING_VERSION_ID,
            MODEL_ID,
            IFS_SOURCE_ID,
            IFS_CYCLE_TIME,
            IFS_CYCLE_TIME,
            IFS_END_TIME,
            5,
            IFS_FORCING_PACKAGE_URI,
            json_adapter({"forecast_cycle_id": IFS_CYCLE_ID, "source_id": IFS_SOURCE_ID, "max_lead_hours": 168}),
        ),
        (
            IFS_06Z_FORCING_VERSION_ID,
            MODEL_ID,
            IFS_SOURCE_ID,
            IFS_06Z_CYCLE_TIME,
            IFS_06Z_CYCLE_TIME,
            IFS_06Z_END_TIME,
            5,
            IFS_06Z_FORCING_PACKAGE_URI,
            json_adapter({"forecast_cycle_id": IFS_06Z_CYCLE_ID, "source_id": IFS_SOURCE_ID, "max_lead_hours": 144}),
        ),
    ]
    execute_values(
        cursor,
        """
        INSERT INTO met.forcing_version (
            forcing_version_id,
            model_id,
            source_id,
            cycle_time,
            start_time,
            end_time,
            station_count,
            forcing_package_uri,
            lineage_json
        )
        VALUES %s
        ON CONFLICT DO NOTHING
        """,
        forcing_version_rows,
        page_size=1000,
    )

    forcing_rows = [
        (
            FORCING_VERSION_ID,
            BASIN_VERSION_ID,
            station.station_id,
            valid_time,
            SOURCE_ID,
            variable,
            forcing_value(rng, variable, valid_time),
            forcing_unit(variable),
            "1h",
            "ok",
        )
        for station in build_met_stations()
        for variable in FORCING_VARIABLES
        for valid_time in hourly_times()
    ]
    execute_values(
        cursor,
        """
        INSERT INTO met.forcing_station_timeseries (
            forcing_version_id,
            basin_version_id,
            station_id,
            valid_time,
            source_id,
            variable,
            value,
            unit,
            native_resolution,
            quality_flag
        )
        VALUES %s
        ON CONFLICT DO NOTHING
        """,
        forcing_rows,
        page_size=1000,
    )


def seed_hydro(cursor: Any, execute_values: Any, rng: random.Random) -> None:
    hydro_run_rows = [
        (
            RUN_ID,
            "forecast",
            SCENARIO_ID,
            MODEL_ID,
            BASIN_VERSION_ID,
            FORCING_VERSION_ID,
            SOURCE_ID,
            START_TIME,
            START_TIME,
            END_TIME,
            "published",
            RUN_MANIFEST_URI,
            RUN_OUTPUT_URI,
            f"s3://nhms/runs/{RUN_ID}/logs/run.log",
        ),
        (
            IFS_RUN_ID,
            "forecast",
            IFS_SCENARIO_ID,
            MODEL_ID,
            BASIN_VERSION_ID,
            IFS_FORCING_VERSION_ID,
            IFS_SOURCE_ID,
            IFS_CYCLE_TIME,
            IFS_CYCLE_TIME,
            IFS_END_TIME,
            "published",
            IFS_RUN_MANIFEST_URI,
            IFS_RUN_OUTPUT_URI,
            f"s3://nhms/runs/{IFS_RUN_ID}/logs/run.log",
        ),
        (
            IFS_06Z_RUN_ID,
            "forecast",
            IFS_SCENARIO_ID,
            MODEL_ID,
            BASIN_VERSION_ID,
            IFS_06Z_FORCING_VERSION_ID,
            IFS_SOURCE_ID,
            IFS_06Z_CYCLE_TIME,
            IFS_06Z_CYCLE_TIME,
            IFS_06Z_END_TIME,
            "published",
            IFS_06Z_RUN_MANIFEST_URI,
            IFS_06Z_RUN_OUTPUT_URI,
            f"s3://nhms/runs/{IFS_06Z_RUN_ID}/logs/run.log",
        ),
    ]
    execute_values(
        cursor,
        """
        INSERT INTO hydro.hydro_run (
            run_id,
            run_type,
            scenario_id,
            model_id,
            basin_version_id,
            forcing_version_id,
            source_id,
            cycle_time,
            start_time,
            end_time,
            status,
            run_manifest_uri,
            output_uri,
            log_uri
        )
        VALUES %s
        ON CONFLICT DO NOTHING
        """,
        hydro_run_rows,
        page_size=1000,
    )

    river_rows = [
        *_build_river_timeseries_rows(
            rng,
            run_id=RUN_ID,
            start_time=START_TIME,
            forecast_hours=FORECAST_HOURS,
        ),
        *_build_river_timeseries_rows(
            rng,
            run_id=IFS_RUN_ID,
            start_time=IFS_CYCLE_TIME,
            forecast_hours=IFS_FORECAST_HOURS,
            flow_offset=-20.0,
        ),
        *_build_river_timeseries_rows(
            rng,
            run_id=IFS_06Z_RUN_ID,
            start_time=IFS_06Z_CYCLE_TIME,
            forecast_hours=IFS_06Z_FORECAST_HOURS,
            flow_offset=-25.0,
        ),
    ]
    execute_values(
        cursor,
        """
        INSERT INTO hydro.river_timeseries (
            run_id,
            basin_version_id,
            river_network_version_id,
            river_segment_id,
            valid_time,
            lead_time_hours,
            variable,
            value,
            unit,
            quality_flag
        )
        VALUES %s
        ON CONFLICT DO NOTHING
        """,
        river_rows,
        page_size=1000,
    )


def _build_river_timeseries_rows(
    rng: random.Random,
    *,
    run_id: str,
    start_time: datetime,
    forecast_hours: int,
    flow_offset: float = 0.0,
) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for segment in build_river_segments():
        for variable in RIVER_VARIABLES:
            unit = "m3/s" if variable == "q_down" else "m"
            for lead_time_hours, valid_time in enumerate(hourly_times(start_time, forecast_hours)):
                rows.append(
                    (
                        run_id,
                        BASIN_VERSION_ID,
                        RIVER_NETWORK_VERSION_ID,
                        segment.river_segment_id,
                        valid_time,
                        lead_time_hours,
                        variable,
                        river_value(
                            rng,
                            segment.segment_order,
                            variable,
                            lead_time_hours,
                            forecast_hours=forecast_hours,
                            flow_offset=flow_offset,
                        ),
                        unit,
                        "ok",
                    )
                )
    return rows


def seed_map(cursor: Any, json_adapter: Any) -> None:
    cursor.execute(
        """
        INSERT INTO map.tile_layer (
            layer_id,
            layer_type,
            source_run_id,
            variable,
            tile_format,
            tile_uri_template,
            min_zoom,
            max_zoom,
            style_json,
            published_flag,
            publish_time
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (
            TILE_LAYER_ID,
            "vector",
            RUN_ID,
            "river_network",
            "pbf",
            "s3://nhms/tiles/hydro/{z}/{x}/{y}.pbf",
            3,
            12,
            json_adapter({"lineColor": "#2563eb", "lineWidth": 2, "source": RIVER_NETWORK_VERSION_ID}),
            True,
            START_TIME + timedelta(hours=6),
        ),
    )


def seed_ops(cursor: Any, json_adapter: Any) -> None:
    cursor.execute(
        """
        INSERT INTO ops.pipeline_job (
            job_id,
            run_id,
            cycle_id,
            job_type,
            status,
            stage,
            submitted_at,
            started_at,
            finished_at,
            exit_code,
            log_uri
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (
            PIPELINE_JOB_ID,
            RUN_ID,
            CYCLE_ID,
            "download",
            "succeeded",
            "raw_download",
            START_TIME + timedelta(minutes=5),
            START_TIME + timedelta(minutes=6),
            START_TIME + timedelta(minutes=32),
            0,
            f"s3://nhms/runs/{RUN_ID}/logs/run.log",
        ),
    )
    cursor.execute(
        """
        INSERT INTO ops.qc_result (
            qc_checkpoint,
            target_type,
            target_id,
            run_id,
            cycle_id,
            passed,
            severity,
            checks_json,
            message
        )
        SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s
        WHERE NOT EXISTS (
            SELECT 1
            FROM ops.qc_result
            WHERE qc_checkpoint = %s
              AND target_type = %s
              AND target_id = %s
        )
        ON CONFLICT DO NOTHING
        """,
        (
            QC_CHECKPOINT,
            "forcing_version",
            FORCING_VERSION_ID,
            RUN_ID,
            CYCLE_ID,
            True,
            "info",
            json_adapter(
                {
                    "checks": [
                        {"name": "station_count", "expected": 5, "actual": 5, "passed": True},
                        {"name": "hourly_records", "expected": 5040, "actual": 5040, "passed": True},
                        {"name": "time_range", "expected": "[2026-05-01, 2026-05-08)", "passed": True},
                    ]
                }
            ),
            "Demo forcing package contains complete hourly station forcings.",
            QC_CHECKPOINT,
            "forcing_version",
            FORCING_VERSION_ID,
        ),
    )


def collect_counts(cursor: Any) -> dict[str, int]:
    station_ids = [station.station_id for station in build_met_stations()]
    queries = [
        ("core.basin", "SELECT COUNT(*) FROM core.basin WHERE basin_id = %s", (BASIN_ID,)),
        (
            "core.basin_version",
            "SELECT COUNT(*) FROM core.basin_version WHERE basin_version_id = %s",
            (BASIN_VERSION_ID,),
        ),
        (
            "core.river_network_version",
            "SELECT COUNT(*) FROM core.river_network_version WHERE river_network_version_id = %s",
            (RIVER_NETWORK_VERSION_ID,),
        ),
        (
            "core.river_segment",
            "SELECT COUNT(*) FROM core.river_segment WHERE river_network_version_id = %s",
            (RIVER_NETWORK_VERSION_ID,),
        ),
        ("core.mesh_version", "SELECT COUNT(*) FROM core.mesh_version WHERE mesh_version_id = %s", (MESH_VERSION_ID,)),
        ("core.model_instance", "SELECT COUNT(*) FROM core.model_instance WHERE model_id = %s", (MODEL_ID,)),
        ("met.data_source", "SELECT COUNT(*) FROM met.data_source WHERE source_id = %s", (SOURCE_ID,)),
        ("met.data_source.ifs", "SELECT COUNT(*) FROM met.data_source WHERE source_id = %s", (IFS_SOURCE_ID,)),
        ("met.forecast_cycle", "SELECT COUNT(*) FROM met.forecast_cycle WHERE cycle_id = %s", (CYCLE_ID,)),
        (
            "met.forecast_cycle.ifs",
            "SELECT COUNT(*) FROM met.forecast_cycle WHERE cycle_id = ANY(%s)",
            ([IFS_CYCLE_ID, IFS_06Z_CYCLE_ID],),
        ),
        ("met.met_station", "SELECT COUNT(*) FROM met.met_station WHERE station_id = ANY(%s)", (station_ids,)),
        (
            "met.forcing_version",
            "SELECT COUNT(*) FROM met.forcing_version WHERE forcing_version_id = %s",
            (FORCING_VERSION_ID,),
        ),
        (
            "met.forcing_version.ifs",
            "SELECT COUNT(*) FROM met.forcing_version WHERE forcing_version_id = ANY(%s)",
            ([IFS_FORCING_VERSION_ID, IFS_06Z_FORCING_VERSION_ID],),
        ),
        (
            "met.forcing_station_timeseries",
            "SELECT COUNT(*) FROM met.forcing_station_timeseries WHERE forcing_version_id = %s",
            (FORCING_VERSION_ID,),
        ),
        ("hydro.hydro_run", "SELECT COUNT(*) FROM hydro.hydro_run WHERE run_id = %s", (RUN_ID,)),
        (
            "hydro.hydro_run.ifs",
            "SELECT COUNT(*) FROM hydro.hydro_run WHERE run_id = ANY(%s)",
            ([IFS_RUN_ID, IFS_06Z_RUN_ID],),
        ),
        ("hydro.river_timeseries", "SELECT COUNT(*) FROM hydro.river_timeseries WHERE run_id = %s", (RUN_ID,)),
        (
            "hydro.river_timeseries.ifs",
            "SELECT COUNT(*) FROM hydro.river_timeseries WHERE run_id = ANY(%s)",
            ([IFS_RUN_ID, IFS_06Z_RUN_ID],),
        ),
        ("map.tile_layer", "SELECT COUNT(*) FROM map.tile_layer WHERE layer_id = %s", (TILE_LAYER_ID,)),
        ("ops.pipeline_job", "SELECT COUNT(*) FROM ops.pipeline_job WHERE job_id = %s", (PIPELINE_JOB_ID,)),
        (
            "ops.qc_result",
            """
            SELECT COUNT(*)
            FROM ops.qc_result
            WHERE qc_checkpoint = %s
              AND target_type = %s
              AND target_id = %s
            """,
            (QC_CHECKPOINT, "forcing_version", FORCING_VERSION_ID),
        ),
    ]

    counts: dict[str, int] = {}
    for table_name, sql, params in queries:
        cursor.execute(sql, params)
        counts[table_name] = int(cursor.fetchone()[0])
    return counts


def seed_database(connection: Any) -> dict[str, int]:
    from psycopg2.extras import Json, execute_values

    rng = random.Random(42)
    with connection.cursor() as cursor:
        seed_core(cursor, Json, execute_values)
        seed_met(cursor, Json, execute_values, rng)
        seed_hydro(cursor, execute_values, rng)
        seed_map(cursor, Json)
        seed_ops(cursor, Json)
        return collect_counts(cursor)


def seed_s3_objects() -> int:
    endpoint_url = os.getenv("S3_ENDPOINT_URL")
    if not endpoint_url:
        print("Warning: S3 seed skipped: S3_ENDPOINT_URL is not set.")
        return 0

    try:
        import boto3

        access_key = os.getenv("AWS_ACCESS_KEY_ID")
        secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
        client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
        )

        for bucket in sorted({parse_s3_uri(uri)[0] for uri in S3_PLACEHOLDER_OBJECTS}):
            ensure_bucket(client, bucket)

        for uri, body in S3_PLACEHOLDER_OBJECTS.items():
            bucket, key = parse_s3_uri(uri)
            client.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type_for_key(key))
    except Exception as error:
        print(f"Warning: S3 seed skipped: {error}")
        return 0

    print(f"S3 seed complete: {len(S3_PLACEHOLDER_OBJECTS)} placeholder objects.")
    return len(S3_PLACEHOLDER_OBJECTS)


def parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.strip("/"):
        raise ValueError(f"Invalid S3 URI: {uri}")
    return parsed.netloc, parsed.path.lstrip("/")


def ensure_bucket(client: Any, bucket: str) -> None:
    try:
        client.head_bucket(Bucket=bucket)
    except Exception:
        client.create_bucket(Bucket=bucket)


def content_type_for_key(key: str) -> str:
    if key.endswith(".json"):
        return "application/json"
    if key.endswith(".csv"):
        return "text/csv"
    if key.endswith(".log") or key.endswith(".ic"):
        return "text/plain"
    if key.endswith(".tar.gz"):
        return "application/gzip"
    if key.endswith(".nc"):
        return "application/netcdf"
    return "application/octet-stream"


def print_summary(counts: dict[str, int]) -> None:
    print("Demo seed table counts:")
    for table_name, count in counts.items():
        print(f"  {table_name}: {count}")


def main() -> None:
    database_url = load_database_url()
    connection = connect_database(database_url)
    try:
        counts = seed_database(connection)
    finally:
        connection.close()

    print_summary(counts)
    seed_s3_objects()


if __name__ == "__main__":
    main()
