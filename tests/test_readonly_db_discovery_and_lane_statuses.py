"""#2484 D4 discovery plumbing/SQL and D6 lane statuses of the readonly DB validation lane.

The real-PostgreSQL half of D4 is ``tests/test_readonly_db_discovery_integration.py``.
"""

from __future__ import annotations

import itertools
import json
from typing import Any

import pytest

from services.production_closure.readonly_db_merge import _merged_readonly_db_summary
from services.production_closure.readonly_db_probe_adapter import PsycopgReadonlyDbProbeAdapter
from services.production_closure.readonly_db_types import ReadonlyDbMergeSourceEvidence
from services.production_closure.readonly_db_validation import (
    ReadonlyDbValidationConfig,
    RouteHttpResponse,
    _overall_status,
    validate_readonly_db_boundary,
)
from services.production_closure.readonly_db_validation import main as validation_main
from tests.test_readonly_db_validation import (
    _evidence_root,
    _FakeReadonlyAdapter,
    _passing_manual_actions,
    _passing_route_requester,
    _route_name_for_path,
    _run_id,
    _seed_live_readonly_source,
    _write_json,
)

READY_STATUSES = ["succeeded", "parsed", "published"]


# -- D4: configured source / run id reach discovery ---------------------------------


def _config(**overrides: Any) -> ReadonlyDbValidationConfig:
    return ReadonlyDbValidationConfig.from_env(
        evidence_root=_evidence_root(),
        run_id=_run_id("discovery"),
        database_url="postgresql://display_ro:secret@db.example/nhms",
        force=True,
        **overrides,
    )


def _validate(config: ReadonlyDbValidationConfig, adapter: Any, requester: Any = None) -> dict[str, Any]:
    return validate_readonly_db_boundary(
        config,
        adapter=adapter,
        route_requester=requester or _passing_route_requester,
        manual_action_probe_runner=_passing_manual_actions,
    )


def test_configured_source_and_business_run_id_reach_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NHMS_READONLY_DB_VALIDATION_SOURCE", raising=False)
    monkeypatch.delenv("NHMS_READONLY_DB_VALIDATION_RUN_ID", raising=False)
    adapter = _FakeReadonlyAdapter()

    summary = _validate(_config(source="IFS", strict_run_id="fcst_ifs_2026092412_basin"), adapter)

    # A TypeError here would be swallowed into a discovery blocker; the recorded call proves it was not.
    assert adapter.discovery_calls == [{"source": "IFS", "run_id": "fcst_ifs_2026092412_basin"}]
    assert "blockers" not in summary["display_identity"]


def test_unconfigured_discovery_is_called_without_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NHMS_READONLY_DB_VALIDATION_SOURCE", raising=False)
    monkeypatch.delenv("NHMS_READONLY_DB_VALIDATION_RUN_ID", raising=False)
    adapter = _FakeReadonlyAdapter()

    summary = _validate(_config(), adapter)

    assert adapter.discovery_calls == [{"source": None, "run_id": None}]
    assert summary["display_identity"]["basin_id"] == "basin_readonly_validation"


def test_configured_fields_still_override_the_discovered_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NHMS_READONLY_DB_VALIDATION_SOURCE", raising=False)
    monkeypatch.delenv("NHMS_READONLY_DB_VALIDATION_RUN_ID", raising=False)

    summary = _validate(_config(model_id="model_operator", job_id="job_operator"), _FakeReadonlyAdapter())

    assert summary["display_identity"] == {
        "source": "GFS",
        "cycle_time": "2026-05-03T00:00:00+00:00",
        "run_id": "run_readonly_validation",
        "model_id": "model_operator",
        "basin_id": "basin_readonly_validation",
        "job_id": "job_operator",
    }


# -- D4: the adapter's SQL ------------------------------------------------------------


class _ScriptedConnection:
    """A psycopg2 connection double: records each statement and answers by table."""

    def __init__(self, rows: dict[str, dict[str, Any] | None]) -> None:
        self.rows = rows
        self.statements: list[tuple[str, tuple[Any, ...] | None]] = []

    def cursor(self) -> _ScriptedConnection:
        return self

    def __enter__(self) -> _ScriptedConnection:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, query: str, params: tuple[Any, ...] | None = None) -> None:
        self.statements.append((" ".join(query.split()), params))

    def fetchone(self) -> dict[str, Any] | None:
        sql = self.statements[-1][0]
        for table in ("hydro.hydro_run", "met.forecast_cycle", "ops.pipeline_job"):
            if f"FROM {table}" in sql:
                return self.rows.get(table)
        raise AssertionError(f"unexpected statement: {sql}")

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


def _discover(rows: dict[str, dict[str, Any] | None], **kwargs: Any) -> tuple[dict[str, Any], list]:
    connection = _ScriptedConnection(rows)
    adapter = PsycopgReadonlyDbProbeAdapter(
        "postgresql://readonly:secret@db.example/nhms",
        ddl_suffix="discovery",
        connect_fn=lambda *args, **kw: connection,
    )
    return adapter.discover_display_identity(**kwargs), connection.statements


RUN_ROW = {
    "run_id": "fcst_gfs_2026092412_basin_a",
    "source": "GFS",
    "cycle_time": "2026-09-24T12:00:00+00:00",
    "model_id": "basin_a_shud",
    "basin_id": "basin_a",
}


def test_unfiltered_discovery_gates_on_ready_statuses_and_binds_the_job_to_that_run() -> None:
    identity, statements = _discover({"hydro.hydro_run": RUN_ROW, "ops.pipeline_job": {"job_id": "job_a_1"}})

    assert identity == {**RUN_ROW, "job_id": "job_a_1"}
    run_sql, run_params = statements[0]
    assert "upper(r.source_id) AS source" in run_sql
    assert "LEFT JOIN core.basin_version bv ON bv.basin_version_id = r.basin_version_id" in run_sql
    assert "r.status::text = ANY(%s)" in run_sql
    assert "upper(r.source_id) = upper(%s)" not in run_sql
    assert "r.run_id = %s" not in run_sql
    assert run_params == (READY_STATUSES,)
    job_sql, job_params = statements[1]
    assert "FROM ops.pipeline_job WHERE run_id = %s AND log_uri IS NOT NULL" in job_sql
    assert job_params == ("fcst_gfs_2026092412_basin_a",)
    assert len(statements) == 2


def test_a_configured_source_filters_case_insensitively() -> None:
    _, statements = _discover({"hydro.hydro_run": RUN_ROW, "ops.pipeline_job": None}, source="gfs")

    run_sql, run_params = statements[0]
    assert "r.status::text = ANY(%s)" in run_sql
    assert "upper(r.source_id) = upper(%s)" in run_sql
    assert run_params == (READY_STATUSES, "gfs")


def test_a_configured_run_id_selects_that_run_whatever_its_status() -> None:
    identity, statements = _discover(
        {"hydro.hydro_run": {**RUN_ROW, "run_id": "run_running"}, "ops.pipeline_job": {"job_id": "job_running"}},
        run_id="run_running",
    )

    run_sql, run_params = statements[0]
    assert "r.run_id = %s" in run_sql
    assert "r.status" not in run_sql
    assert run_params == ("run_running",)
    assert statements[1][1] == ("run_running",)
    assert identity["job_id"] == "job_running"


def test_a_run_without_a_logged_job_yields_no_job_id_and_a_run_without_basin_no_basin_id() -> None:
    identity, _ = _discover({"hydro.hydro_run": {**RUN_ROW, "basin_id": None}, "ops.pipeline_job": None})

    assert "job_id" not in identity
    assert "basin_id" not in identity
    assert identity["run_id"] == "fcst_gfs_2026092412_basin_a"


def test_no_run_row_falls_back_to_the_source_filtered_cycle_and_queries_no_job() -> None:
    identity, statements = _discover(
        {"hydro.hydro_run": None, "met.forecast_cycle": {"source": "IFS", "cycle_time": "2026-09-24T12:00:00+00:00"}},
        source="IFS",
    )

    assert identity == {"source": "IFS", "cycle_time": "2026-09-24T12:00:00+00:00"}
    cycle_sql, cycle_params = statements[1]
    assert "FROM met.forecast_cycle" in cycle_sql
    assert "upper(source_id) = upper(%s)" in cycle_sql
    assert cycle_params == ("IFS",)
    assert not any("ops.pipeline_job" in sql for sql, _ in statements)


# -- D6: lane statuses ----------------------------------------------------------------


def _master_overall_status(
    *,
    role_evidence: dict[str, Any],
    permission_probes: list[dict[str, Any]],
    route_smoke: list[dict[str, Any]],
    manual_actions: list[dict[str, Any]],
) -> str:
    """``readonly_db_validation._overall_status`` as on master ``09acbd5cb``, the I3 oracle."""
    if role_evidence.get("role_type") == "writer_or_mutating":
        return "FAIL"
    all_items = [*permission_probes, *route_smoke, *manual_actions]
    if any(item.get("status") == "FAIL" for item in all_items):
        return "FAIL"
    if any(item.get("status") == "BLOCKED" for item in all_items):
        return "BLOCKED"
    return "PASS"


ITEM_SHAPES: tuple[list[dict[str, Any]], ...] = (
    [],
    [{"status": "PASS"}],
    [{"status": "BLOCKED"}],
    [{"status": "FAIL"}],
    [{"status": "PASS"}, {"status": "BLOCKED"}],
    [{}],
)


def test_overall_status_equals_master_for_every_role_probe_manual_route_combination() -> None:
    cases = 0
    for role_type, probes, routes, manual in itertools.product(
        ("readonly_candidate", "writer_or_mutating"), ITEM_SHAPES, ITEM_SHAPES, ITEM_SHAPES
    ):
        kwargs = {
            "role_evidence": {"role_type": role_type},
            "permission_probes": probes,
            "route_smoke": routes,
            "manual_actions": manual,
        }
        assert _overall_status(**kwargs) == _master_overall_status(**kwargs), kwargs
        cases += 1
    assert cases == 2 * len(ITEM_SHAPES) ** 3


def _blocked_latest_product_requester(method: str, path: str) -> RouteHttpResponse:
    if _route_name_for_path(path) == "latest_product":
        return RouteHttpResponse(
            status_code=404,
            body={"error": {"code": "QHH_LATEST_PRODUCT_UNAVAILABLE", "message": "fixture unavailable"}},
        )
    return _passing_route_requester(method, path)


@pytest.mark.parametrize(
    ("adapter", "requester", "expected_lanes", "expected_status"),
    [
        (
            _FakeReadonlyAdapter(),
            _blocked_latest_product_requester,
            {"deny_write": "PASS", "read_routes": "BLOCKED"},
            "BLOCKED",
        ),
        (
            _FakeReadonlyAdapter(successful_operations={("hydro.hydro_run", "INSERT")}),
            _passing_route_requester,
            {"deny_write": "FAIL", "read_routes": "PASS"},
            "FAIL",
        ),
        (
            _FakeReadonlyAdapter(privileges={"hydro.hydro_run": {"insert": True}}),
            _passing_route_requester,
            {"deny_write": "FAIL", "read_routes": "PASS"},
            "FAIL",
        ),
        # Both lanes PASS, yet a simulated (injected) run is never PASS.
        (_FakeReadonlyAdapter(), _passing_route_requester, {"deny_write": "PASS", "read_routes": "PASS"}, "BLOCKED"),
    ],
    ids=["deny-write-pass-read-blocked", "deny-write-fail-read-pass", "writer-role", "both-pass-simulated"],
)
def test_the_summary_reports_deny_write_and_read_route_lanes_separately(
    adapter: _FakeReadonlyAdapter, requester: Any, expected_lanes: dict[str, str], expected_status: str
) -> None:
    config = _config()

    summary = _validate(config, adapter, requester)

    assert summary["lane_statuses"] == expected_lanes
    assert summary["status"] == expected_status
    on_disk = json.loads((config.lane_dir / "summary.json").read_text(encoding="utf-8"))
    assert on_disk["lane_statuses"] == expected_lanes


def test_the_merged_summary_keeps_a_source_route_fail_visible_next_to_its_blocked_status() -> None:
    evidence_root = _evidence_root()
    run_id = _run_id("merge-lanes")
    gfs = _seed_live_readonly_source(evidence_root=evidence_root, run_id=f"{run_id}-gfs", source="GFS")
    ifs = _seed_live_readonly_source(evidence_root=evidence_root, run_id=f"{run_id}-ifs", source="IFS")
    sources = []
    for lane in (gfs.lane_dir, ifs.lane_dir):
        payload = json.loads((lane / "summary.json").read_text(encoding="utf-8"))
        sources.append(
            ReadonlyDbMergeSourceEvidence(
                source_dir=lane, summary=payload, artifacts={}, parent_binding_field="run_id_prefix"
            )
        )
    ifs_payload = sources[1].summary
    next(route for route in ifs_payload["route_smoke"] if route["name"] == "jobs")["status"] = "FAIL"
    ifs_payload["status"] = "FAIL"

    merged = _merged_readonly_db_summary(
        _config(), source_evidence=sources, declared_sources=("GFS", "IFS"), reduced_scope=False
    )

    assert merged["status"] == "BLOCKED"
    assert {"code": "READONLY_DB_MERGE_SOURCE_NOT_PASS", "source_index": 1, "status": "FAIL"} in merged["blockers"]
    assert merged["lane_statuses"] == {"deny_write": "PASS", "read_routes": "FAIL"}


def test_the_merge_cli_still_refuses_a_non_pass_source_bundle(capsys: pytest.CaptureFixture[str]) -> None:
    """Merge rules are unchanged: a non-PASS source is refused on read (exit 1), before any summary."""
    evidence_root = _evidence_root()
    run_id = _run_id("merge-lanes-cli")
    gfs = _seed_live_readonly_source(evidence_root=evidence_root, run_id=f"{run_id}-gfs", source="GFS")
    ifs = _seed_live_readonly_source(evidence_root=evidence_root, run_id=f"{run_id}-ifs", source="IFS")
    ifs_summary = json.loads((ifs.lane_dir / "summary.json").read_text(encoding="utf-8"))
    ifs_summary["status"] = "FAIL"
    _write_json(ifs.lane_dir / "summary.json", ifs_summary)

    exit_code = validation_main(
        [
            "--evidence-root",
            str(evidence_root),
            "--run-id",
            run_id,
            "--merge-source-dir",
            str(gfs.lane_dir),
            "--merge-source-dir",
            str(ifs.lane_dir),
            "--force",
        ]
    )

    assert exit_code == 1
    assert "READONLY_DB_MERGE_SOURCE_NOT_PASS" in capsys.readouterr().err


def test_a_clean_merged_summary_reports_both_lanes_pass() -> None:
    from services.production_closure.readonly_db_validation import merge_readonly_db_source_evidence

    evidence_root = _evidence_root()
    run_id = _run_id("merge-lanes-pass")
    gfs = _seed_live_readonly_source(evidence_root=evidence_root, run_id=f"{run_id}-gfs", source="GFS")
    ifs = _seed_live_readonly_source(evidence_root=evidence_root, run_id=f"{run_id}-ifs", source="IFS")

    merged = merge_readonly_db_source_evidence(
        evidence_root=evidence_root,
        run_id=run_id,
        source_dirs=(gfs.lane_dir, ifs.lane_dir),
        force=True,
    )

    assert merged["status"] == "PASS"
    assert merged["lane_statuses"] == {"deny_write": "PASS", "read_routes": "PASS"}
