from __future__ import annotations

import json
import math
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from packages.common.flood_quality import backfill_run_product_quality, refresh_run_product_quality
from workers.flood_frequency import return_period
from workers.flood_frequency.frequency import check_sample_size
from workers.flood_frequency.return_period import (
    compute_return_periods,
    extract_max_forecast_q,
    extract_timestep_q,
    interpolate_return_period,
    map_warning_level,
)


def test_normal_interpolation_between_q10_and_q20() -> None:
    thresholds = _thresholds(q2=1200.0, q5=1800.0, q10=2300.0, q20=2900.0, q50=3700.0, q100=4500.0)

    result = interpolate_return_period(2600.0, thresholds)

    expected = 10 ** (math.log10(10) + 0.5 * (math.log10(20) - math.log10(10)))
    assert result == pytest.approx(expected)
    assert map_warning_level(result, _sample_quality(40)) == "warning"


def test_below_q2_returns_one_and_normal_warning() -> None:
    thresholds = _thresholds(q2=1200.0)

    result = interpolate_return_period(800.0, thresholds)

    assert result == 1.0
    assert map_warning_level(result, _sample_quality(40)) == "normal"


def test_above_q100_returns_over_100_and_extreme_warning() -> None:
    thresholds = _thresholds(q100=4500.0)

    result = interpolate_return_period(5000.0, thresholds)

    assert result is not None and result > 100
    assert map_warning_level(result, _sample_quality(40)) == "extreme"


def test_no_frequency_curve_writes_null_result() -> None:
    with _store() as session:
        _insert_forecast_run(session, segment_values={"seg_002": [100.0, 120.0]})

        result = compute_return_periods("forecast_run", session)

        row = _result_row(session, segment_id="seg_002", max_over_window=True)
        quality = _quality_row(session)
        assert result.without_curve == 1
        assert row["return_period"] is None
        assert row["warning_level"] is None
        assert row["quality_flag"] == "no_frequency_curve"
        assert quality["result_rows"] == 3
        assert quality["max_result_rows"] == 1
        assert quality["return_period_rows"] == 0
        assert quality["warning_rows"] == 0
        assert quality["max_return_period_rows"] == 0
        assert quality["max_warning_rows"] == 0
        assert result.quality_state == "unavailable"
        assert result.unavailable_products == ("frequency_curves",)
        assert result.residual_blockers[0]["code"] == "FREQUENCY_CURVE_UNAVAILABLE"
        assert result.residual_blockers[0]["quality_flag"] == "no_frequency_curve"
        assert _tile_layer_count(session) == 0


def test_warning_thresholds_unavailable_keeps_warning_null_with_curve() -> None:
    with _store() as session:
        _insert_curve(session, "seg_001")
        _insert_forecast_run(session, segment_values={"seg_001": [260.0, 300.0]})

        result = compute_return_periods(
            "forecast_run",
            session,
            quality_contract={
                "state": "unavailable",
                "unavailable_products": ["warning_thresholds"],
            },
        )

        row = _result_row(session, segment_id="seg_001", max_over_window=True)
        quality = _quality_row(session)
        assert result.with_curve == 1
        assert row["return_period"] is not None
        assert row["warning_level"] is None
        assert row["quality_flag"] == "warning_thresholds_unavailable"
        assert quality["result_rows"] == 3
        assert quality["max_result_rows"] == 1
        assert quality["return_period_rows"] == 3
        assert quality["warning_rows"] == 0
        assert quality["max_return_period_rows"] == 1
        assert quality["max_warning_rows"] == 0
        assert result.quality_state == "unavailable"
        assert result.unavailable_products == ("warning_thresholds",)
        assert result.residual_blockers[0]["code"] == "WARNING_THRESHOLDS_UNAVAILABLE"
        assert "warning_level remains null" in result.residual_blockers[0]["residual_risk"]
        assert _tile_layer_count(session) == 0


def test_partial_sample_degrades_to_highest_reliable_warning_level() -> None:
    with _store() as session:
        _insert_curve(session, "seg_001", sample_years=25, quality_flag="partial_sample")
        _insert_forecast_run(session, segment_values={"seg_001": [260.0, 300.0]})

        compute_return_periods("forecast_run", session)

        row = _result_row(session, segment_id="seg_001", max_over_window=True)
        assert row["return_period"] > 20
        assert row["warning_level"] == "warning"
        assert row["quality_flag"] == "unreliable_threshold"


def test_fit_failed_curve_is_not_used() -> None:
    with _store() as session:
        _insert_curve(session, "seg_001", quality_flag="fit_failed")
        _insert_forecast_run(session, segment_values={"seg_001": [400.0]})

        compute_return_periods("forecast_run", session)

        row = _result_row(session, segment_id="seg_001", max_over_window=True)
        assert row["return_period"] is None
        assert row["warning_level"] is None
        assert row["quality_flag"] == "no_usable_frequency_curve"


def test_timestep_calculation_multiple_timesteps() -> None:
    with _store() as session:
        _insert_curve(session, "seg_001")
        _insert_forecast_run(session, segment_values={"seg_001": [80.0, 150.0, 260.0]})

        timesteps = extract_timestep_q("forecast_run", session)
        result = compute_return_periods("forecast_run", session)

        rows = _result_rows(session, max_over_window=False)
        assert len(timesteps) == 3
        assert result.rows_written == 4
        assert [row["q_value"] for row in rows] == [80.0, 150.0, 260.0]
        assert rows[0]["warning_level"] == "normal"
        assert rows[-1]["warning_level"] == "high_risk"


def test_one_hour_window_keeps_peak_and_timestep_rows() -> None:
    with _store() as session:
        _insert_curve(session, "seg_001")
        _insert_forecast_run(
            session,
            segment_values={"seg_001": [260.0, 150.0]},
            start_time=datetime(2026, 5, 1),
            end_time=datetime(2026, 5, 1, 1),
        )

        result = compute_return_periods("forecast_run", session)

        rows = session.execute(
            text(
                """
                SELECT duration, valid_time, max_over_window, q_value, warning_level
                FROM flood.return_period_result
                WHERE run_id = 'forecast_run'
                  AND river_segment_id = 'seg_001'
                  AND duration = '1h'
                  AND valid_time = :valid_time
                ORDER BY max_over_window DESC
                """
            ),
            {"valid_time": datetime(2026, 5, 1)},
        ).mappings().all()
        assert result.rows_written == 3
        assert len(rows) == 2
        assert [bool(row["max_over_window"]) for row in rows] == [True, False]
        assert [row["q_value"] for row in rows] == [260.0, 260.0]
        assert [row["warning_level"] for row in rows] == ["high_risk", "high_risk"]


def test_ifs_six_day_window_peak_uses_curve_duration_label() -> None:
    # 峰值行的 duration 必须与所用频率曲线的设计历时一致('1h'),不能用预报时窗长度
    # 冒充设计历时(否则展示侧按 duration 过滤峰值层会查不到,且 lineage 自相矛盾)。
    # "峰值跨预报窗"的语义由 max_over_window=True 承载。
    with _store() as session:
        _insert_curve(session, "seg_001")
        _insert_forecast_run(
            session,
            segment_values={"seg_001": [100.0, 420.0]},
            end_time=datetime(2026, 5, 7),
        )

        compute_return_periods("forecast_run", session)

        row = _result_row(session, segment_id="seg_001", max_over_window=True)
        assert row["duration"] == "1h"


def test_state_machine_success_transitions_parsed_to_frequency_done() -> None:
    with _store() as session:
        _insert_curve(session, "seg_001")
        _insert_forecast_run(session, segment_values={"seg_001": [150.0]})

        compute_return_periods("forecast_run", session)

        run = _run_row(session)
        job = _pipeline_job_row(session)
        layer = _tile_layer_row(session)
        quality = _quality_row(session)
    assert run["status"] == "frequency_done"
    assert job["status"] == "succeeded"
    assert layer["published_flag"] in (True, 1)
    assert quality["result_rows"] == 2
    assert quality["max_result_rows"] == 1
    assert quality["return_period_rows"] == 2
    assert quality["warning_rows"] == 2


def test_frequency_pipeline_job_uses_canonical_cycle_id_for_datetime_cycle_time() -> None:
    with _store() as session:
        _insert_curve(session, "seg_001")
        _insert_forecast_run(session, segment_values={"seg_001": [150.0]})

        compute_return_periods("forecast_run", session)

        job = _pipeline_job_row(session)
        assert job["cycle_id"] == "gfs_2026050100"


def test_frequency_pipeline_job_uses_canonical_cycle_id_for_string_cycle_time() -> None:
    with _store() as session:
        _insert_curve(session, "seg_001")
        _insert_forecast_run(session, segment_values={"seg_001": [150.0]})
        session.execute(
            text(
                """
                UPDATE hydro.hydro_run
                SET cycle_time = '2026-05-01T00:00:00Z'
                WHERE run_id = 'forecast_run'
                """
            )
        )
        session.commit()

        compute_return_periods("forecast_run", session)

        job = _pipeline_job_row(session)
        assert job["cycle_id"] == "gfs_2026050100"


def test_state_machine_failure_keeps_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    with _store() as session:
        _insert_curve(session, "seg_001")
        _insert_forecast_run(session, segment_values={"seg_001": [150.0]})
        session.execute(
            text(
                """
                INSERT INTO flood.run_product_quality (
                    run_id, result_rows, max_result_rows, return_period_rows, warning_rows,
                    max_return_period_rows, max_warning_rows
                )
                VALUES ('forecast_run', 1, 1, 1, 1, 1, 1)
                """
            )
        )
        session.commit()

        def fail_extract(_run_id: str, _db_session: Session) -> dict[str, tuple[float, datetime]]:
            raise RuntimeError("boom")

        monkeypatch.setattr(return_period, "extract_max_forecast_q", fail_extract)
        result = compute_return_periods("forecast_run", session)

        run = _run_row(session)
        job = _pipeline_job_row(session)
        assert result.status == "failed"
        assert run["status"] == "parsed"
        assert job["status"] == "failed"
        assert job["error_message"] == "boom"
        assert _quality_row(session) is None


def test_graceful_degradation_frequency_failure_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    with _store() as session:
        _insert_forecast_run(session, segment_values={"seg_001": [150.0]})

        def fail_timesteps(_run_id: str, _db_session: Session) -> dict[datetime, dict[str, float]]:
            raise RuntimeError("frequency unavailable")

        monkeypatch.setattr(return_period, "extract_timestep_q", fail_timesteps)

        result = compute_return_periods("forecast_run", session, graceful_degradation=True)

        assert result.status == "failed"
        assert _run_row(session)["status"] == "parsed"
        assert _quality_row(session) is None


def test_upsert_to_return_period_result_updates_existing_row() -> None:
    with _store() as session:
        _insert_curve(session, "seg_001")
        _insert_forecast_run(session, segment_values={"seg_001": [150.0]})
        compute_return_periods("forecast_run", session)

        session.execute(text("UPDATE hydro.river_timeseries SET value = 450.0 WHERE run_id = 'forecast_run'"))
        compute_return_periods("forecast_run", session)

        count = session.execute(
            text("SELECT COUNT(*) AS count FROM flood.return_period_result WHERE max_over_window = 1")
        ).mappings().one()
        row = _result_row(session, segment_id="seg_001", max_over_window=True)
        quality = _quality_row(session)
        assert count["count"] == 1
        assert row["q_value"] == 450.0
        assert row["warning_level"] == "extreme"
        assert quality["result_rows"] == 2
        assert quality["max_result_rows"] == 1
        assert quality["max_warning_rows"] == 1


def test_run_product_quality_backfill_is_idempotent_and_overwrites_stale_counts() -> None:
    with _store() as session:
        _insert_forecast_run(session, segment_values={"seg_001": [150.0]})
        _insert_stale_result_row(
            session,
            segment_id="seg_001",
            valid_time=datetime(2026, 5, 1),
            duration="1h",
            max_over_window=True,
            q_value=150.0,
        )
        session.execute(
            text(
                """
                INSERT INTO flood.run_product_quality (
                    run_id, result_rows, max_result_rows, return_period_rows, warning_rows,
                    max_return_period_rows, max_warning_rows
                )
                VALUES ('forecast_run', 99, 99, 99, 99, 99, 99)
                """
            )
        )
        session.commit()

        first = backfill_run_product_quality(session, ["forecast_run"])
        second = backfill_run_product_quality(session, ["forecast_run"])
        quality = _quality_row(session)

    assert [item.run_id for item in first] == ["forecast_run"]
    assert [item.run_id for item in second] == ["forecast_run"]
    assert quality["result_rows"] == 1
    assert quality["max_result_rows"] == 1
    assert quality["return_period_rows"] == 1
    assert quality["warning_rows"] == 1
    assert quality["max_return_period_rows"] == 1
    assert quality["max_warning_rows"] == 1


def test_run_product_quality_refresh_deletes_stale_row_when_source_rows_are_missing() -> None:
    with _store() as session:
        _insert_forecast_run(session, segment_values={"seg_001": [150.0]})
        session.execute(
            text(
                """
                INSERT INTO flood.run_product_quality (
                    run_id, result_rows, max_result_rows, return_period_rows, warning_rows,
                    max_return_period_rows, max_warning_rows
                )
                VALUES ('forecast_run', 1, 1, 1, 1, 1, 1)
                """
            )
        )
        session.commit()

        result = refresh_run_product_quality(session, "forecast_run")
        quality = _quality_row(session)

    assert result is None
    assert quality is None


def test_recompute_replaces_peak_when_peak_valid_time_moves() -> None:
    with _store() as session:
        _insert_curve(session, "seg_001")
        _insert_forecast_run(session, segment_values={"seg_001": [150.0, 260.0]})
        compute_return_periods("forecast_run", session)

        first_peak = _result_row(session, segment_id="seg_001", max_over_window=True)
        assert str(first_peak["valid_time"]) == "2026-05-01 01:00:00"
        assert first_peak["q_value"] == 260.0

        session.execute(
            text(
                """
                UPDATE hydro.river_timeseries
                SET value = CASE
                    WHEN valid_time = :first_time THEN 360.0
                    WHEN valid_time = :second_time THEN 120.0
                    ELSE value
                END
                WHERE run_id = 'forecast_run'
                  AND river_segment_id = 'seg_001'
                """
            ),
            {
                "first_time": datetime(2026, 5, 1),
                "second_time": datetime(2026, 5, 1, 1),
            },
        )
        compute_return_periods("forecast_run", session)

        peak_rows = _result_rows(session, max_over_window=True)
        assert len(peak_rows) == 1
        assert str(peak_rows[0]["valid_time"]) == "2026-05-01 00:00:00"
        assert peak_rows[0]["q_value"] == 360.0
        assert peak_rows[0]["warning_level"] == "severe"


def test_upsert_to_return_period_result_preserves_same_segment_in_different_networks() -> None:
    valid_time = datetime(2026, 5, 1, 1)
    base_context = {
        "run_id": "forecast_run",
        "scenario_id": "scenario_v1",
        "basin_version_id": "basin_v1",
        "model_id": "model_v1",
        "source_id": "GFS",
        "cycle_time": datetime(2026, 5, 1),
    }
    base_result = {"return_period": 20.0, "warning_level": "warning", "quality_flag": "ok"}
    with _store() as session:
        return_period._upsert_return_period_result(
            session,
            {**base_context, "river_network_version_id": "rnv_v1"},
            "seg_001",
            valid_time,
            "1h",
            200.0,
            max_over_window=False,
            result=base_result,
        )
        return_period._upsert_return_period_result(
            session,
            {**base_context, "river_network_version_id": "rnv_v2"},
            "seg_001",
            valid_time,
            "1h",
            300.0,
            max_over_window=False,
            result={**base_result, "return_period": 50.0, "warning_level": "severe"},
        )

        rows = session.execute(
            text(
                """
                SELECT river_network_version_id, q_value, return_period, warning_level
                FROM flood.return_period_result
                WHERE run_id = 'forecast_run'
                  AND river_segment_id = 'seg_001'
                  AND duration = '1h'
                  AND valid_time = :valid_time
                ORDER BY river_network_version_id
                """
            ),
            {"valid_time": valid_time},
        ).mappings().all()

        assert [row["river_network_version_id"] for row in rows] == ["rnv_v1", "rnv_v2"]
        assert [row["q_value"] for row in rows] == [200.0, 300.0]
        assert [row["warning_level"] for row in rows] == ["warning", "severe"]


def test_extract_max_forecast_q_returns_peak_time_per_segment() -> None:
    with _store() as session:
        _insert_forecast_run(
            session,
            segment_values={
                "seg_001": [100.0, 250.0, 200.0],
                "seg_002": [90.0, 95.0, 120.0],
            },
        )

        result = extract_max_forecast_q("forecast_run", session)

        assert result["seg_001"][0] == 250.0
        assert result["seg_002"][0] == 120.0


def _insert_stale_result_row(
    session: Session,
    *,
    segment_id: str,
    valid_time: datetime,
    duration: str,
    max_over_window: bool,
    q_value: float = 999.0,
) -> None:
    session.execute(
        text(
            """
            INSERT INTO flood.return_period_result (
                run_id, scenario_id, basin_version_id, river_network_version_id,
                model_id, river_segment_id, valid_time, duration, q_value, q_unit,
                return_period, warning_level, source_id, cycle_time, max_over_window,
                quality_flag
            )
            VALUES (
                'forecast_run', 'scenario_v1', 'basin_v1', 'rnv_v1',
                'model_v1', :segment_id, :valid_time, :duration, :q_value, 'm3/s',
                50.0, 'severe', 'GFS', :cycle_time, :max_over_window, 'ok'
            )
            """
        ),
        {
            "segment_id": segment_id,
            "valid_time": valid_time,
            "duration": duration,
            "q_value": q_value,
            "cycle_time": datetime(2026, 5, 1),
            "max_over_window": max_over_window,
        },
    )
    session.commit()


def _stale_label_count(session: Session, *, duration: str, max_over_window: bool) -> int:
    return int(
        session.execute(
            text(
                """
                SELECT COUNT(*)
                FROM flood.return_period_result
                WHERE run_id = 'forecast_run'
                  AND duration = :duration
                  AND max_over_window = :max_over_window
                """
            ),
            {"duration": duration, "max_over_window": max_over_window},
        ).scalar_one()
    )


def test_recompute_clears_stale_label_peak_orphans() -> None:
    # 核心场景：旧标签('167h')峰值孤儿在按新标签('1h')重跑后必须被清除，不留跨标签孤儿。
    with _store() as session:
        _insert_curve(session, "seg_001")
        _insert_forecast_run(session, segment_values={"seg_001": [260.0, 150.0]})
        _insert_stale_result_row(
            session,
            segment_id="seg_001",
            valid_time=datetime(2026, 4, 1),
            duration="167h",
            max_over_window=True,
        )

        result = compute_return_periods("forecast_run", session)

        assert _stale_label_count(session, duration="167h", max_over_window=True) == 0
        peak_rows = _result_rows(session, max_over_window=True)
        assert len(peak_rows) == 1
        assert peak_rows[0]["duration"] == "1h"
        # 全表不翻倍：1 peak(seg_001 max=260) + 2 timestep(260,150) = 3 行；陈旧 167h 行已删。
        timestep_rows = _result_rows(session, max_over_window=False)
        total_rows = int(
            session.execute(text("SELECT COUNT(*) FROM flood.return_period_result")).scalar_one()
        )
        assert len(timestep_rows) == 2
        assert total_rows == len(peak_rows) + len(timestep_rows) == 3
        assert result.rows_written == 3


def test_recompute_clears_stale_label_timestep_orphans() -> None:
    # timestep 对称回归：旧标签 timestep 行重跑后同样被清除，只剩 '1h'。
    with _store() as session:
        _insert_curve(session, "seg_001")
        _insert_forecast_run(session, segment_values={"seg_001": [80.0, 150.0]})
        _insert_stale_result_row(
            session,
            segment_id="seg_001",
            valid_time=datetime(2026, 4, 1),
            duration="167h",
            max_over_window=False,
        )

        compute_return_periods("forecast_run", session)

        # 与 peak 测试对称的显式行数断言：新 '1h' timestep 恰为 2 行(80,150)，陈旧 '167h' 清零。
        assert _stale_label_count(session, duration="1h", max_over_window=False) == 2
        assert _stale_label_count(session, duration="167h", max_over_window=False) == 0
        timestep_rows = _result_rows(session, max_over_window=False)
        assert {row["duration"] for row in timestep_rows} == {"1h"}


def test_fresh_run_writes_without_overdelete() -> None:
    # 保留语义不回退：一开始就是 1h 的 fresh 跑正确写入，无误删。
    with _store() as session:
        _insert_curve(session, "seg_001")
        _insert_curve(session, "seg_002")
        _insert_forecast_run(
            session,
            segment_values={"seg_001": [260.0], "seg_002": [150.0]},
        )

        compute_return_periods("forecast_run", session)

        peak_rows = _result_rows(session, max_over_window=True)
        assert {row["river_segment_id"] for row in peak_rows} == {"seg_001", "seg_002"}
        assert all(row["duration"] == "1h" for row in peak_rows)


def test_idempotent_recompute_keeps_single_peak_per_segment() -> None:
    # 幂等重跑（连续两次 1h）不产生重复行：峰值行数 = segment 数。
    with _store() as session:
        _insert_curve(session, "seg_001")
        _insert_curve(session, "seg_002")
        _insert_forecast_run(
            session,
            segment_values={"seg_001": [260.0, 150.0], "seg_002": [120.0, 90.0]},
        )
        compute_return_periods("forecast_run", session)
        compute_return_periods("forecast_run", session)

        peak_rows = _result_rows(session, max_over_window=True)
        assert len(peak_rows) == 2
        assert {row["river_segment_id"] for row in peak_rows} == {"seg_001", "seg_002"}


def test_upsert_return_period_result_clears_stale_segment_peak_label() -> None:
    # per-segment 路径：_upsert_return_period_result 写新峰值时清除该 segment 旧标签峰值孤儿。
    valid_time = datetime(2026, 5, 1, 1)
    context = {
        "run_id": "forecast_run",
        "scenario_id": "scenario_v1",
        "basin_version_id": "basin_v1",
        "river_network_version_id": "rnv_v1",
        "model_id": "model_v1",
        "source_id": "GFS",
        "cycle_time": datetime(2026, 5, 1),
    }
    result = {"return_period": 20.0, "warning_level": "warning", "quality_flag": "ok"}
    with _store() as session:
        _insert_stale_result_row(
            session,
            segment_id="seg_001",
            valid_time=datetime(2026, 4, 1),
            duration="167h",
            max_over_window=True,
        )

        # 前置断言：陈旧 167h 峰值行确已写入，杜绝"删了个不存在的行也绿"的假绿。
        assert _stale_label_count(session, duration="167h", max_over_window=True) == 1

        return_period._upsert_return_period_result(
            session, context, "seg_001", valid_time, "1h", 300.0,
            max_over_window=True, result=result,
        )

        assert _stale_label_count(session, duration="167h", max_over_window=True) == 0
        peak_rows = _result_rows(session, max_over_window=True)
        assert len(peak_rows) == 1
        assert peak_rows[0]["duration"] == "1h"
        assert peak_rows[0]["q_value"] == 300.0


@contextmanager
def _store() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _attach_schemas(engine)
    with engine.begin() as connection:
        _create_tables(connection)
        _seed_model(connection)
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()


def _attach_schemas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _attach(dbapi_connection: Any, _connection_record: Any) -> None:
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS core")
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS hydro")
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS flood")
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS ops")
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS map")


def _create_tables(connection: Any) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE core.model_instance (
                model_id TEXT PRIMARY KEY,
                basin_version_id TEXT NOT NULL,
                river_network_version_id TEXT NOT NULL
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE hydro.hydro_run (
                run_id TEXT PRIMARY KEY,
                run_type TEXT NOT NULL,
                scenario_id TEXT NOT NULL,
                model_id TEXT NOT NULL,
                basin_version_id TEXT NOT NULL,
                source_id TEXT,
                cycle_time DATETIME,
                start_time DATETIME NOT NULL,
                end_time DATETIME NOT NULL,
                status TEXT NOT NULL,
                error_code TEXT,
                error_message TEXT,
                updated_at DATETIME
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE hydro.river_timeseries (
                run_id TEXT NOT NULL,
                basin_version_id TEXT NOT NULL,
                river_network_version_id TEXT NOT NULL,
                river_segment_id TEXT NOT NULL,
                valid_time DATETIME NOT NULL,
                variable TEXT NOT NULL,
                value REAL NOT NULL,
                unit TEXT NOT NULL,
                quality_flag TEXT DEFAULT 'ok'
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE flood.flood_frequency_curve (
                curve_id TEXT PRIMARY KEY,
                model_id TEXT NOT NULL,
                river_network_version_id TEXT NOT NULL,
                basin_version_id TEXT NOT NULL,
                river_segment_id TEXT NOT NULL,
                duration TEXT NOT NULL,
                method TEXT NOT NULL,
                sample_period_start DATE NOT NULL,
                sample_period_end DATE NOT NULL,
                sample_size INTEGER NOT NULL,
                parameters_json TEXT NOT NULL,
                q2 REAL,
                q5 REAL,
                q10 REAL,
                q20 REAL,
                q50 REAL,
                q100 REAL,
                unit TEXT NOT NULL,
                quality_flag TEXT NOT NULL
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE flood.return_period_result (
                run_id TEXT NOT NULL,
                scenario_id TEXT NOT NULL,
                basin_version_id TEXT NOT NULL,
                river_network_version_id TEXT NOT NULL,
                model_id TEXT NOT NULL,
                river_segment_id TEXT NOT NULL,
                valid_time DATETIME NOT NULL,
                duration TEXT NOT NULL,
                q_value REAL NOT NULL,
                q_unit TEXT NOT NULL DEFAULT 'm3/s',
                return_period REAL,
                warning_level TEXT,
                source_id TEXT,
                cycle_time DATETIME,
                max_over_window BOOLEAN NOT NULL DEFAULT 0,
                quality_flag TEXT NOT NULL DEFAULT 'ok',
                PRIMARY KEY (run_id, river_network_version_id, river_segment_id, duration, valid_time, max_over_window)
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE flood.run_product_quality (
                run_id TEXT PRIMARY KEY,
                result_rows INTEGER NOT NULL DEFAULT 0,
                max_result_rows INTEGER NOT NULL DEFAULT 0,
                return_period_rows INTEGER NOT NULL DEFAULT 0,
                warning_rows INTEGER NOT NULL DEFAULT 0,
                max_return_period_rows INTEGER NOT NULL DEFAULT 0,
                max_warning_rows INTEGER NOT NULL DEFAULT 0,
                refreshed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE ops.pipeline_job (
                job_id TEXT PRIMARY KEY,
                run_id TEXT,
                cycle_id TEXT,
                job_type TEXT NOT NULL,
                model_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                stage TEXT,
                submitted_at DATETIME,
                started_at DATETIME,
                finished_at DATETIME,
                error_code TEXT,
                error_message TEXT,
                created_at DATETIME,
                updated_at DATETIME
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE map.tile_layer (
                layer_id TEXT PRIMARY KEY,
                layer_type TEXT NOT NULL,
                source_run_id TEXT,
                source_product_id TEXT,
                variable TEXT,
                valid_time DATETIME,
                tile_format TEXT NOT NULL,
                tile_uri_template TEXT NOT NULL,
                min_zoom INTEGER NOT NULL DEFAULT 0,
                max_zoom INTEGER NOT NULL DEFAULT 14,
                style_json TEXT,
                published_flag BOOLEAN NOT NULL DEFAULT 0,
                publish_time DATETIME,
                created_at DATETIME
            )
            """
        )
    )


def _seed_model(connection: Any) -> None:
    connection.execute(
        text(
            """
            INSERT INTO core.model_instance (model_id, basin_version_id, river_network_version_id)
            VALUES ('model_v1', 'basin_v1', 'rnv_v1')
            """
        )
    )


def _insert_forecast_run(
    session: Session,
    *,
    segment_values: dict[str, list[float]],
    start_time: datetime = datetime(2026, 5, 1),
    end_time: datetime = datetime(2026, 5, 8),
) -> None:
    session.execute(
        text(
            """
            INSERT INTO hydro.hydro_run (
                run_id, run_type, scenario_id, model_id, basin_version_id, source_id,
                cycle_time, start_time, end_time, status
            )
            VALUES (
                'forecast_run', 'forecast', 'scenario_v1', 'model_v1', 'basin_v1', 'GFS',
                :cycle_time, :start_time, :end_time, 'parsed'
            )
            """
        ),
        {"cycle_time": start_time, "start_time": start_time, "end_time": end_time},
    )
    rows = []
    for segment_id, values in segment_values.items():
        for index, value in enumerate(values):
            rows.append(
                {
                    "run_id": "forecast_run",
                    "basin_version_id": "basin_v1",
                    "river_network_version_id": "rnv_v1",
                    "river_segment_id": segment_id,
                    "valid_time": start_time + timedelta(hours=index),
                    "variable": "q_down",
                    "value": value,
                    "unit": "m3/s",
                }
            )
    session.execute(
        text(
            """
            INSERT INTO hydro.river_timeseries (
                run_id, basin_version_id, river_network_version_id, river_segment_id,
                valid_time, variable, value, unit
            )
            VALUES (
                :run_id, :basin_version_id, :river_network_version_id, :river_segment_id,
                :valid_time, :variable, :value, :unit
            )
            """
        ),
        rows,
    )
    session.commit()


def _insert_curve(
    session: Session,
    segment_id: str,
    *,
    sample_years: int = 40,
    quality_flag: str = "ok",
) -> None:
    quantiles = _thresholds()
    if quality_flag in {"fit_failed", "no_valid_sample"}:
        quantiles = {key: None for key in quantiles}
    session.execute(
        text(
            """
            INSERT INTO flood.flood_frequency_curve (
                curve_id, model_id, river_network_version_id, basin_version_id,
                river_segment_id, duration, method, sample_period_start, sample_period_end,
                sample_size, parameters_json, q2, q5, q10, q20, q50, q100, unit, quality_flag
            )
            VALUES (
                :curve_id, 'model_v1', 'rnv_v1', 'basin_v1',
                :river_segment_id, '1h', 'P-III', '1980-01-01', '2019-12-31',
                :sample_size, :parameters_json, :q2, :q5, :q10, :q20, :q50, :q100, 'm3/s', :quality_flag
            )
            """
        ),
        {
            "curve_id": f"curve_{segment_id}_{quality_flag}",
            "river_segment_id": segment_id,
            "sample_size": sample_years,
            "parameters_json": json.dumps({"sample_quality": _sample_quality(sample_years)}, sort_keys=True),
            "q2": quantiles["Q2"],
            "q5": quantiles["Q5"],
            "q10": quantiles["Q10"],
            "q20": quantiles["Q20"],
            "q50": quantiles["Q50"],
            "q100": quantiles["Q100"],
            "quality_flag": quality_flag,
        },
    )
    session.commit()


def _thresholds(
    *,
    q2: float = 100.0,
    q5: float = 150.0,
    q10: float = 200.0,
    q20: float = 250.0,
    q50: float = 350.0,
    q100: float = 400.0,
) -> dict[str, float]:
    return {"Q2": q2, "Q5": q5, "Q10": q10, "Q20": q20, "Q50": q50, "Q100": q100}


def _sample_quality(sample_years: int) -> dict[str, dict[str, Any]]:
    return check_sample_size(sample_years).thresholds


def _result_rows(session: Session, *, max_over_window: bool) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            """
            SELECT *
            FROM flood.return_period_result
            WHERE max_over_window = :max_over_window
            ORDER BY valid_time, river_segment_id
            """
        ),
        {"max_over_window": max_over_window},
    ).mappings()
    return [dict(row) for row in rows]


def _result_row(session: Session, *, segment_id: str, max_over_window: bool) -> dict[str, Any]:
    return dict(
        session.execute(
            text(
                """
                SELECT *
                FROM flood.return_period_result
                WHERE river_segment_id = :segment_id
                  AND max_over_window = :max_over_window
                ORDER BY valid_time DESC
                LIMIT 1
                """
            ),
            {"segment_id": segment_id, "max_over_window": max_over_window},
        ).mappings().one()
    )


def _run_row(session: Session) -> dict[str, Any]:
    return dict(session.execute(text("SELECT * FROM hydro.hydro_run WHERE run_id = 'forecast_run'")).mappings().one())


def _pipeline_job_row(session: Session) -> dict[str, Any]:
    return dict(session.execute(text("SELECT * FROM ops.pipeline_job")).mappings().one())


def _tile_layer_count(session: Session) -> int:
    return int(session.execute(text("SELECT COUNT(*) FROM map.tile_layer")).scalar_one())


def _tile_layer_row(session: Session) -> dict[str, Any]:
    return dict(session.execute(text("SELECT * FROM map.tile_layer")).mappings().one())


def _quality_row(session: Session) -> dict[str, Any] | None:
    row = session.execute(
        text("SELECT * FROM flood.run_product_quality WHERE run_id = 'forecast_run'")
    ).mappings().first()
    return dict(row) if row is not None else None
