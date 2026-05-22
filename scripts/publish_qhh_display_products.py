from __future__ import annotations

import json
import os
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from packages.common.source_identity import normalize_source_id
from workers.flood_frequency.return_period import compute_return_periods

ROOT = Path(__file__).resolve().parents[1]
RUN_ROOT = Path(os.getenv("QHH_RUN_ROOT", ROOT / ".nhms-runs" / "qhh-continuous")).resolve()
RUN_ID = os.environ["QHH_RUN_ID"]
MODEL_ID = os.getenv("QHH_MODEL_ID", "basins_qhh_shud")
RIVER_NETWORK_VERSION_ID = os.getenv("QHH_RIVER_NETWORK_VERSION_ID", "basins_qhh_rivnet_vbasins")


def main() -> int:
    database_url = os.environ["DATABASE_URL"]
    engine = create_engine(database_url, future=True)
    with Session(engine) as session:
        _activate_model(session)
        _normalize_run_identity(session)
        frequency_stats = compute_return_periods(RUN_ID, session, graceful_degradation=False)
        session.commit()
        payload = _display_readiness(session, frequency_stats)

    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    (RUN_ROOT / "qhh-display-products.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def _activate_model(session: Session) -> None:
    session.execute(
        text(
            """
            UPDATE core.model_instance
            SET active_flag = (model_id = :model_id),
                lifecycle_state = CASE
                    WHEN model_id = :model_id THEN 'active'
                    ELSE CASE WHEN lifecycle_state = 'active' THEN 'inactive' ELSE lifecycle_state END
                END
            WHERE basin_version_id = (
                SELECT basin_version_id
                FROM core.model_instance
                WHERE model_id = :model_id
            )
            """
        ),
        {"model_id": MODEL_ID},
    )
    session.flush()


def _normalize_run_identity(session: Session) -> None:
    source_id = session.execute(
        text("SELECT source_id FROM hydro.hydro_run WHERE run_id = :run_id"),
        {"run_id": RUN_ID},
    ).scalar_one()
    scenario_id = _scenario_for_source(str(source_id))
    session.execute(
        text(
            """
            UPDATE hydro.hydro_run
            SET scenario_id = :scenario_id,
                updated_at = CURRENT_TIMESTAMP
            WHERE run_id = :run_id
              AND scenario_id <> :scenario_id
            """
        ),
        {"run_id": RUN_ID, "scenario_id": scenario_id},
    )
    session.flush()


def _display_readiness(session: Session, frequency_stats: object) -> dict[str, object]:
    run = session.execute(
        text(
            """
            SELECT run_id, status, scenario_id, source_id, model_id, basin_version_id, cycle_time, start_time, end_time
            FROM hydro.hydro_run
            WHERE run_id = :run_id
            """
        ),
        {"run_id": RUN_ID},
    ).mappings().one()
    hydro = session.execute(
        text(
            """
            SELECT
                COUNT(*) AS rows,
                COUNT(DISTINCT river_segment_id) AS segments,
                MIN(valid_time) AS start_time,
                MAX(valid_time) AS end_time,
                MIN(value) AS min_q,
                AVG(value) AS avg_q,
                MAX(value) AS max_q
            FROM hydro.river_timeseries
            WHERE run_id = :run_id
              AND variable = 'q_down'
            """
        ),
        {"run_id": RUN_ID},
    ).mappings().one()
    segments = session.execute(
        text(
            """
            SELECT
                COUNT(*) FILTER (
                    WHERE COALESCE(properties_json->>'shud_output_river', 'false') = 'true'
                ) AS shud_segments,
                COUNT(*) FILTER (
                    WHERE COALESCE(properties_json->>'shud_output_river', 'false') = 'true'
                      AND geom IS NOT NULL
                ) AS shud_segments_with_geom
            FROM core.river_segment
            WHERE river_network_version_id = :river_network_version_id
            """
        ),
        {"river_network_version_id": RIVER_NETWORK_VERSION_ID},
    ).mappings().one()
    flood = session.execute(
        text(
            """
            SELECT
                COUNT(*) AS rows,
                COUNT(DISTINCT river_segment_id) AS segments,
                COUNT(*) FILTER (WHERE max_over_window = false) AS timestep_rows,
                COUNT(*) FILTER (WHERE max_over_window = true) AS peak_rows,
                COUNT(*) FILTER (WHERE quality_flag = 'no_frequency_curve') AS no_frequency_curve_rows,
                COUNT(*) FILTER (WHERE return_period IS NOT NULL) AS rows_with_return_period
            FROM flood.return_period_result
            WHERE run_id = :run_id
            """
        ),
        {"run_id": RUN_ID},
    ).mappings().one()
    active_model = session.execute(
        text(
            """
            SELECT model_id, active_flag, lifecycle_state
            FROM core.model_instance
            WHERE model_id = :model_id
            """
        ),
        {"model_id": MODEL_ID},
    ).mappings().one()
    return {
        "status": "published_for_display",
        "run": _json_ready(dict(run)),
        "active_model": _json_ready(dict(active_model)),
        "hydro_timeseries": _json_ready(dict(hydro)),
        "river_segments": _json_ready(dict(segments)),
        "return_period": _json_ready(dict(flood)),
        "frequency_stats": {
            "total_segments": frequency_stats.total_segments,
            "with_curve": frequency_stats.with_curve,
            "without_curve": frequency_stats.without_curve,
            "warning_counts": frequency_stats.warning_counts,
            "rows_written": frequency_stats.rows_written,
            "status": frequency_stats.status,
            "error_code": frequency_stats.error_code,
            "error_message": frequency_stats.error_message,
        },
        "quality_note": (
            "qhh 当前没有 flood.flood_frequency_curve 校准频率曲线；"
            "return_period_result 已发布流量和 no_frequency_curve 质量标记，"
            "洪水等级/重现期不做伪造。"
        ),
    }


def _json_ready(value: object) -> object:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_ready(item) for item in value]
    return value


def _scenario_for_source(source_id: str) -> str:
    normalized = normalize_source_id(source_id)
    if normalized == "gfs":
        return "forecast_gfs_deterministic"
    if normalized == "IFS":
        return "forecast_ifs_deterministic"
    return f"forecast_{normalized.lower()}_deterministic"


if __name__ == "__main__":
    raise SystemExit(main())
