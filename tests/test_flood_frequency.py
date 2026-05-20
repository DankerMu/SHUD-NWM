from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.api.auth import AuthContext, evaluate_policy
from workers.flood_frequency import cli as flood_cli
from workers.flood_frequency import frequency
from workers.flood_frequency.frequency import (
    AnnualMaximaResult,
    FrequencyFitError,
    check_monotonicity,
    check_sample_size,
    extract_annual_maxima,
    fit_curves,
    fit_frequency_curve,
    fit_pearson3,
    fit_segment_duration,
    save_frequency_curve,
)


def test_pearson3_normal_fit_known_data_reasonable_quantiles() -> None:
    samples = [100 + year * 4 + (year % 5) * 3 for year in range(40)]

    result = fit_pearson3(samples)

    assert result.method == "P-III"
    assert result.quality_flag == "ok"
    assert 100 < result.quantiles["Q2"] < result.quantiles["Q100"] < 400
    assert result.params["scale"] > 0


def test_gev_fallback_when_pearson3_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_fit(_data: list[float]) -> tuple[float, float, float]:
        raise RuntimeError("p3 failed")

    monkeypatch.setattr(frequency.stats.pearson3, "fit", fail_fit)

    result = fit_frequency_curve([100 + index for index in range(40)], method="auto")

    assert result.method == "GEV"
    assert result.quality_flag == "p3_fallback_gev"
    assert result.quantiles["Q100"] is not None


def test_double_failure_writes_fit_failed_record(monkeypatch: pytest.MonkeyPatch) -> None:
    with _store() as session:
        _patch_annual(
            monkeypatch,
            AnnualMaximaResult(
                samples=[(1980 + index, float(100 + index)) for index in range(40)],
                excluded_years=[],
                observed_years=list(range(1980, 2020)),
            ),
        )
        monkeypatch.setattr(frequency.stats.pearson3, "fit", _raise_fit("p3 failed"))
        monkeypatch.setattr(frequency.stats.genextreme, "fit", _raise_fit("gev failed"))

        result = fit_segment_duration("model_v1", "seg_001", "1h", session)

        row = _curve_row(session)
        assert result["quality_flag"] == "fit_failed"
        assert row["quality_flag"] == "fit_failed"
        assert row["q2"] is None
        assert _qc_row(session)["severity"] == "error"


def test_zero_valid_samples_writes_no_valid_sample(monkeypatch: pytest.MonkeyPatch) -> None:
    with _store() as session:
        _patch_annual(
            monkeypatch,
            AnnualMaximaResult(samples=[], excluded_years=[2000, 2001], observed_years=[2000, 2001]),
        )

        result = fit_segment_duration("model_v1", "seg_001", "24h", session)

        row = _curve_row(session)
        parameters = json.loads(row["parameters_json"])
        assert result["quality_flag"] == "no_valid_sample"
        assert row["sample_size"] == 0
        assert row["q100"] is None
        assert parameters["excluded_years"] == [2000, 2001]


def test_sample_size_quality_flags() -> None:
    assert check_sample_size(40).quality_flag == "ok"
    assert check_sample_size(25).quality_flag == "partial_sample"
    assert check_sample_size(8).quality_flag == "insufficient_sample"


def test_per_threshold_sample_quality_structure() -> None:
    result = check_sample_size(25)

    assert result.thresholds["Q2"] == {"min_required": 10, "met": True, "quality_flag": "ok"}
    assert result.thresholds["Q50"] == {
        "min_required": 30,
        "met": False,
        "quality_flag": "insufficient_sample",
    }
    assert result.thresholds["Q100"]["min_required"] == 40


def test_monotonicity_violation_detection_and_correction() -> None:
    result = check_monotonicity({"Q2": 1200, "Q5": 1800, "Q10": 2500, "Q20": 2400, "Q50": 3700, "Q100": 4500})

    assert result.quality_flag == "monotonicity_corrected"
    assert result.corrected_quantiles["Q20"] == pytest.approx((2500 + 3700) / 2)
    assert result.corrections[0]["quantile"] == "Q20"


def test_save_frequency_curve_upsert_idempotent() -> None:
    with _store() as session:
        curve_data = _curve_data(q2=100.0)
        save_frequency_curve(curve_data, session)
        save_frequency_curve({**curve_data, "quantiles": {**curve_data["quantiles"], "Q2": 111.0}}, session)

        count = session.execute(text("SELECT COUNT(*) AS count FROM flood.flood_frequency_curve")).mappings().one()
        row = _curve_row(session)
        assert count["count"] == 1
        assert row["q2"] == pytest.approx(111.0)


def test_qc_result_records_required_checks() -> None:
    with _store() as session:
        save_frequency_curve(_curve_data(quality_flag="partial_sample"), session)

        qc = _qc_row(session)
        checks = json.loads(qc["checks_json"])
        assert qc["qc_checkpoint"] == "flood_frequency"
        assert qc["severity"] == "warning"
        assert set(checks) == {"sample_size_check", "monotonicity_check", "fit_validity_check"}


def test_cli_dry_run_no_db_writes() -> None:
    with _store() as session:
        result = fit_curves("model_v1", session, dry_run=True)

        count = session.execute(text("SELECT COUNT(*) AS count FROM flood.flood_frequency_curve")).mappings().one()
        assert result.total_segments == 2
        assert result.skipped == 12
        assert count["count"] == 0


def test_fit_curves_supersedes_old_model_curves(monkeypatch: pytest.MonkeyPatch) -> None:
    with _store() as session:
        save_frequency_curve(_curve_data(quality_flag="ok"), session)
        session.execute(
            text(
                """
                INSERT INTO core.model_instance (model_id, basin_version_id, river_network_version_id)
                VALUES ('model_v2', 'basin_v1', 'rnv_v1')
                """
            )
        )
        _patch_annual(
            monkeypatch,
            AnnualMaximaResult(
                samples=[(1980 + index, float(100 + index * 4 + (index % 5) * 3)) for index in range(40)],
                excluded_years=[],
                observed_years=list(range(1980, 2020)),
            ),
        )

        result = fit_curves(
            "model_v2",
            session,
            segment_id="seg_001",
            duration="1h",
            supersede_model_id="model_v1",
            trusted_internal=True,
        )

        rows = session.execute(
            text("SELECT model_id, quality_flag FROM flood.flood_frequency_curve ORDER BY model_id")
        ).mappings()
        flags = {str(row["model_id"]): str(row["quality_flag"]) for row in rows}
        assert result.succeeded == 1
        assert flags == {
            "model_v1": "superseded_by_model_upgrade",
            "model_v2": "ok",
        }


def test_fit_curves_supersede_without_policy_evidence_rejects_and_does_not_mutate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _store() as session:
        save_frequency_curve(_curve_data(quality_flag="ok"), session)
        _insert_model_v2(session)
        _patch_fit_samples(monkeypatch)

        with pytest.raises(FrequencyFitError) as exc_info:
            fit_curves(
                "model_v2",
                session,
                segment_id="seg_001",
                duration="1h",
                supersede_model_id="model_v1",
            )

        assert exc_info.value.error_code == "AUTH_REQUIRED"
        assert exc_info.value.details["no_mutation_expected"] is True
        assert exc_info.value.details["policy_decision"]["action_id"] == "models.supersede"
        assert exc_info.value.details["policy_decision"]["target_type"] == "model_instance"
        assert exc_info.value.details["policy_decision"]["target_id"] == "model_v1"
        assert _curve_flags(session) == {"model_v1": "ok"}


def test_fit_curves_supersede_accepts_model_admin_policy_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _store() as session:
        save_frequency_curve(_curve_data(quality_flag="ok"), session)
        _insert_model_v2(session)
        _patch_fit_samples(monkeypatch)
        decision = evaluate_policy(
            AuthContext(
                actor_id="dev-test:model-admin",
                roles=("model_admin",),
                auth_mode="dev_test",
                live_backend_auth_executed=False,
            ),
            "models.supersede",
            target_type="model_instance",
            target_id="model_v1",
        )

        result = fit_curves(
            "model_v2",
            session,
            segment_id="seg_001",
            duration="1h",
            supersede_model_id="model_v1",
            policy_decision=decision,
        )

        assert result.succeeded == 1
        assert _curve_flags(session) == {
            "model_v1": "superseded_by_model_upgrade",
            "model_v2": "ok",
        }


def test_fit_curves_supersede_dry_run_does_not_require_policy_or_mutate() -> None:
    with _store() as session:
        save_frequency_curve(_curve_data(quality_flag="ok"), session)
        _insert_model_v2(session)

        result = fit_curves(
            "model_v2",
            session,
            segment_id="seg_001",
            duration="1h",
            dry_run=True,
            supersede_model_id="model_v1",
        )

        assert result.skipped == 1
        assert result.items == [{"river_segment_id": "seg_001", "duration": "1h", "status": "dry_run"}]
        assert _curve_flags(session) == {"model_v1": "ok"}


def test_argparse_cli_dry_run(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    with _store() as session:
        monkeypatch.setattr(flood_cli, "_session_from_env", lambda: session)

        exit_code = flood_cli._argparse_main(["fit-curves", "--model-id", "model_v1", "--dry-run"])

        output = json.loads(capsys.readouterr().out)
        assert exit_code == 0
        assert output["total_segments"] == 2
        assert output["skipped"] == 12


def test_argparse_cli_supersede_without_policy_evidence_rejects_and_does_not_mutate(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _store() as session:
        save_frequency_curve(_curve_data(quality_flag="ok"), session)
        _insert_model_v2(session)
        session.commit()
        _patch_fit_samples(monkeypatch)
        monkeypatch.setattr(flood_cli, "_session_from_env", lambda: session)

        exit_code = flood_cli._argparse_main(
            [
                "fit-curves",
                "--model-id",
                "model_v2",
                "--segment-id",
                "seg_001",
                "--duration",
                "1h",
                "--supersede-model-id",
                "model_v1",
            ]
        )

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "AUTH_REQUIRED" in captured.err
        assert _curve_flags(session) == {"model_v1": "ok"}


def test_argparse_cli_supersede_without_policy_evidence_preflights_before_segment_discovery(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_segments(_model_id: str, _session: Session) -> list[str]:
        raise AssertionError("_segments_for_model should not be called before supersede auth")

    with _store() as session:
        save_frequency_curve(_curve_data(quality_flag="ok"), session)
        _insert_model_v2(session)
        session.commit()
        _patch_fit_samples(monkeypatch)
        monkeypatch.setattr(frequency, "_segments_for_model", fail_segments)
        monkeypatch.setattr(flood_cli, "_session_from_env", lambda: session)

        exit_code = flood_cli._argparse_main(
            [
                "fit-curves",
                "--model-id",
                "model_v2",
                "--duration",
                "1h",
                "--supersede-model-id",
                "model_v1",
            ]
        )

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "AUTH_REQUIRED" in captured.err
        assert _curve_flags(session) == {"model_v1": "ok"}


def test_main_cli_supersede_without_policy_evidence_rejects_and_does_not_mutate(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _store() as session:
        save_frequency_curve(_curve_data(quality_flag="ok"), session)
        _insert_model_v2(session)
        session.commit()
        _patch_fit_samples(monkeypatch)
        monkeypatch.setattr(flood_cli, "_session_from_env", lambda: session)

        with pytest.raises(SystemExit) as exc_info:
            flood_cli.main(
                [
                    "fit-curves",
                    "--model-id",
                    "model_v2",
                    "--segment-id",
                    "seg_001",
                    "--duration",
                    "1h",
                    "--supersede-model-id",
                    "model_v1",
                ]
            )

        captured = capsys.readouterr()
        assert exc_info.value.code == 1
        assert "AUTH_REQUIRED" in captured.err
        assert _curve_flags(session) == {"model_v1": "ok"}


def test_argparse_cli_supersede_with_cli_model_admin_policy_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _store() as session:
        save_frequency_curve(_curve_data(quality_flag="ok"), session)
        _insert_model_v2(session)
        session.commit()
        _patch_fit_samples(monkeypatch)
        monkeypatch.setattr(flood_cli, "_session_from_env", lambda: session)

        exit_code = flood_cli._argparse_main(
            [
                "fit-curves",
                "--model-id",
                "model_v2",
                "--segment-id",
                "seg_001",
                "--duration",
                "1h",
                "--supersede-model-id",
                "model_v1",
                "--auth-actor-id",
                "cli-model-admin",
                "--auth-role",
                "model_admin",
            ]
        )

        output = json.loads(capsys.readouterr().out)
        decision = output["auth_policy_decision"]
        assert exit_code == 0
        assert output["succeeded"] == 1
        assert decision["action_id"] == "models.supersede"
        assert decision["actor_id"] == "cli-model-admin"
        assert decision["roles"] == ["model_admin"]
        assert decision["target_type"] == "model_instance"
        assert decision["target_id"] == "model_v1"
        assert decision["decision"] == "allow"
        assert decision["execution_mode"] == "backend_route_executed"
        assert decision["auth_mode"] == "cli_dev_test"
        assert _curve_flags(session) == {
            "model_v1": "superseded_by_model_upgrade",
            "model_v2": "ok",
        }


def test_argparse_cli_supersede_live_backend_blocks_cli_flag_auth_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AUTH_BACKEND", "oidc")
    with _store() as session:
        save_frequency_curve(_curve_data(quality_flag="ok"), session)
        _insert_model_v2(session)
        session.commit()
        _patch_fit_samples(monkeypatch)
        monkeypatch.setattr(flood_cli, "_session_from_env", lambda: session)

        exit_code = flood_cli._argparse_main(
            [
                "fit-curves",
                "--model-id",
                "model_v2",
                "--segment-id",
                "seg_001",
                "--duration",
                "1h",
                "--supersede-model-id",
                "model_v1",
                "--auth-actor-id",
                "cli-model-admin",
                "--auth-role",
                "model_admin",
            ]
        )

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "RELEASE_BLOCKED" in captured.err
        assert _curve_flags(session) == {"model_v1": "ok"}


def test_argparse_cli_supersede_saml_blocks_cli_flag_auth_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AUTH_BACKEND", "saml")
    with _store() as session:
        save_frequency_curve(_curve_data(quality_flag="ok"), session)
        _insert_model_v2(session)
        session.commit()
        _patch_fit_samples(monkeypatch)
        monkeypatch.setattr(flood_cli, "_session_from_env", lambda: session)

        exit_code = flood_cli._argparse_main(
            [
                "fit-curves",
                "--model-id",
                "model_v2",
                "--segment-id",
                "seg_001",
                "--duration",
                "1h",
                "--supersede-model-id",
                "model_v1",
                "--auth-actor-id",
                "cli-model-admin",
                "--auth-role",
                "model_admin",
            ]
        )

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "RELEASE_BLOCKED" in captured.err
        assert _curve_flags(session) == {"model_v1": "ok"}


def test_argparse_cli_supersede_saml_preflights_before_segment_discovery(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_segments(_model_id: str, _session: Session) -> list[str]:
        raise AssertionError("_segments_for_model should not be called before supersede auth")

    monkeypatch.setenv("AUTH_BACKEND", "saml")
    with _store() as session:
        save_frequency_curve(_curve_data(quality_flag="ok"), session)
        _insert_model_v2(session)
        session.commit()
        _patch_fit_samples(monkeypatch)
        monkeypatch.setattr(frequency, "_segments_for_model", fail_segments)
        monkeypatch.setattr(flood_cli, "_session_from_env", lambda: session)

        exit_code = flood_cli._argparse_main(
            [
                "fit-curves",
                "--model-id",
                "model_v2",
                "--duration",
                "1h",
                "--supersede-model-id",
                "model_v1",
                "--auth-actor-id",
                "cli-model-admin",
                "--auth-role",
                "model_admin",
            ]
        )

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "RELEASE_BLOCKED" in captured.err
        assert _curve_flags(session) == {"model_v1": "ok"}


def test_argparse_cli_supersede_production_mode_blocks_env_auth_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NHMS_AUTH_MODE", "production")
    monkeypatch.setenv("NHMS_CLI_AUTH_ACTOR_ID", "cli-model-admin")
    monkeypatch.setenv("NHMS_CLI_AUTH_ROLES", "model_admin")
    with _store() as session:
        save_frequency_curve(_curve_data(quality_flag="ok"), session)
        _insert_model_v2(session)
        session.commit()
        _patch_fit_samples(monkeypatch)
        monkeypatch.setattr(flood_cli, "_session_from_env", lambda: session)

        exit_code = flood_cli._argparse_main(
            [
                "fit-curves",
                "--model-id",
                "model_v2",
                "--segment-id",
                "seg_001",
                "--duration",
                "1h",
                "--supersede-model-id",
                "model_v1",
            ]
        )

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "RELEASE_BLOCKED" in captured.err
        assert _curve_flags(session) == {"model_v1": "ok"}


def test_duration_1h_direct_extraction_vs_24h_sliding_window() -> None:
    with _store() as session:
        _insert_hindcast_hourly_year(session, year=2001)

        one_hour = extract_annual_maxima("model_v1", "seg_001", "1h", session)
        daily = extract_annual_maxima("model_v1", "seg_001", "24h", session)

        assert one_hour == [(2001, 1000.0)]
        assert daily == [(2001, 100.0)]


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
            CREATE TABLE core.river_segment (
                river_segment_id TEXT NOT NULL,
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
                start_time DATETIME NOT NULL,
                end_time DATETIME NOT NULL,
                status TEXT NOT NULL,
                run_manifest_uri TEXT NOT NULL
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
                quality_flag TEXT NOT NULL,
                UNIQUE (
                    model_id,
                    river_network_version_id,
                    river_segment_id,
                    duration,
                    method,
                    sample_period_start,
                    sample_period_end
                )
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE ops.qc_result (
                qc_id INTEGER PRIMARY KEY,
                qc_checkpoint TEXT NOT NULL,
                target_type TEXT NOT NULL,
                target_id TEXT NOT NULL,
                run_id TEXT,
                passed BOOLEAN NOT NULL,
                severity TEXT NOT NULL,
                checks_json TEXT NOT NULL,
                message TEXT
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
    connection.execute(
        text(
            """
            INSERT INTO core.river_segment (river_segment_id, river_network_version_id)
            VALUES ('seg_001', 'rnv_v1'), ('seg_002', 'rnv_v1')
            """
        )
    )


def _insert_model_v2(session: Session) -> None:
    session.execute(
        text(
            """
            INSERT INTO core.model_instance (model_id, basin_version_id, river_network_version_id)
            VALUES ('model_v2', 'basin_v1', 'rnv_v1')
            """
        )
    )


def _patch_fit_samples(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_annual(
        monkeypatch,
        AnnualMaximaResult(
            samples=[(1980 + index, float(100 + index * 4 + (index % 5) * 3)) for index in range(40)],
            excluded_years=[],
            observed_years=list(range(1980, 2020)),
        ),
    )


def _curve_flags(session: Session) -> dict[str, str]:
    rows = session.execute(text("SELECT model_id, quality_flag FROM flood.flood_frequency_curve ORDER BY model_id"))
    return {str(row["model_id"]): str(row["quality_flag"]) for row in rows.mappings()}


def _insert_hindcast_hourly_year(session: Session, year: int) -> None:
    run_id = f"hindcast_model_v1_{year}"
    start = datetime(year, 1, 1)
    session.execute(
        text(
            """
            INSERT INTO hydro.hydro_run (
                run_id, run_type, scenario_id, model_id, basin_version_id,
                start_time, end_time, status, run_manifest_uri
            )
            VALUES (
                :run_id, 'hindcast', 'hindcast_replay', 'model_v1', 'basin_v1',
                :start_time, :end_time, 'parsed', :run_manifest_uri
            )
            """
        ),
        {
            "run_id": run_id,
            "start_time": start,
            "end_time": datetime(year + 1, 1, 1),
            "run_manifest_uri": f"runs/{run_id}/manifest.json",
        },
    )
    rows = []
    for hour in range(8760):
        value = 1.0
        if hour == 1000:
            value = 1000.0
        if 2000 <= hour < 2024:
            value = 100.0
        rows.append(
            {
                "run_id": run_id,
                "basin_version_id": "basin_v1",
                "river_network_version_id": "rnv_v1",
                "river_segment_id": "seg_001",
                "valid_time": start + timedelta(hours=hour),
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


def _curve_data(q2: float = 100.0, quality_flag: str = "ok") -> dict[str, Any]:
    return {
        "model_id": "model_v1",
        "river_network_version_id": "rnv_v1",
        "basin_version_id": "basin_v1",
        "river_segment_id": "seg_001",
        "duration": "1h",
        "method": "P-III",
        "sample_period_start": "1980-01-01",
        "sample_period_end": "2019-12-31",
        "sample_size": 40,
        "parameters_json": {"sample_quality": check_sample_size(40).thresholds},
        "quantiles": {"Q2": q2, "Q5": 150.0, "Q10": 200.0, "Q20": 250.0, "Q50": 300.0, "Q100": 350.0},
        "quality_flag": quality_flag,
        "qc_checks": {
            "sample_size_check": {},
            "monotonicity_check": {},
            "fit_validity_check": {"quality_flag": quality_flag},
        },
    }


def _curve_row(session: Session) -> dict[str, Any]:
    return dict(session.execute(text("SELECT * FROM flood.flood_frequency_curve")).mappings().one())


def _qc_row(session: Session) -> dict[str, Any]:
    return dict(session.execute(text("SELECT * FROM ops.qc_result ORDER BY qc_id DESC LIMIT 1")).mappings().one())


def _patch_annual(monkeypatch: pytest.MonkeyPatch, result: AnnualMaximaResult) -> None:
    monkeypatch.setattr(frequency, "extract_annual_maxima_with_metadata", lambda *_args, **_kwargs: result)


def _raise_fit(message: str) -> Any:
    def fail(_data: list[float]) -> tuple[float, float, float]:
        raise RuntimeError(message)

    return fail
