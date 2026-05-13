import re
from pathlib import Path

from db.seeds import seed_demo


def test_seed_module_importable() -> None:
    from db.seeds.seed_demo import main

    assert callable(main)


def test_seed_sql_strings_contain_expected_tables() -> None:
    source = Path(seed_demo.__file__).read_text(encoding="utf-8")
    expected_tables = {
        "core.basin",
        "core.basin_version",
        "core.river_network_version",
        "core.river_segment",
        "core.mesh_version",
        "core.model_instance",
        "met.data_source",
        "met.forecast_cycle",
        "met.met_station",
        "met.forcing_version",
        "met.forcing_station_timeseries",
        "hydro.hydro_run",
        "hydro.river_timeseries",
        "flood.flood_frequency_curve",
        "flood.return_period_result",
        "map.tile_layer",
        "ops.pipeline_job",
        "ops.qc_result",
    }

    missing_tables = sorted(table for table in expected_tables if table not in source)

    assert missing_tables == []


def test_seed_ids_follow_convention() -> None:
    assert seed_demo.BASIN_ID == "yangtze"
    assert re.fullmatch(r"yangtze_v\d{4}_\d{2}", seed_demo.BASIN_VERSION_ID)
    assert re.fullmatch(r"yangtze_rivnet_v\d{2}", seed_demo.RIVER_NETWORK_VERSION_ID)
    assert re.fullmatch(r"yangtze_shud_v\d{2}", seed_demo.MODEL_ID)
    assert re.fullmatch(r"yangtze_mesh_v\d{2}", seed_demo.MESH_VERSION_ID)
    assert seed_demo.SOURCE_ID == "gfs"
    assert seed_demo.SOURCE_ID_IN_IDS == "gfs"
    assert re.fullmatch(r"forc_gfs_\d{10}_yangtze_shud_v\d{2}", seed_demo.FORCING_VERSION_ID)
    assert re.fullmatch(r"fcst_gfs_\d{10}_yangtze_shud_v\d{2}", seed_demo.RUN_ID)
    assert re.fullmatch(r"gfs_\d{10}", seed_demo.CYCLE_ID)

    segment_ids = [segment.river_segment_id for segment in seed_demo.build_river_segments()]
    assert segment_ids == [f"{seed_demo.RIVER_NETWORK_VERSION_ID}_riv_{index:04d}" for index in range(1, 16)]
    assert all(5000 <= segment.length_m <= 50000 for segment in seed_demo.build_river_segments())

    station_ids = [station.station_id for station in seed_demo.build_met_stations()]
    assert station_ids == [f"{seed_demo.BASIN_VERSION_ID}_stn_{index:04d}" for index in range(1, 6)]

    curve_ids = [seed_demo.build_curve_id(index) for index in range(1, 6)]
    assert curve_ids == [f"freq_piii_1h_{seed_demo.MODEL_ID}_riv{index:04d}" for index in range(1, 6)]
