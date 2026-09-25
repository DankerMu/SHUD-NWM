"""#2484 D1/D2/D5/D7: the readonly DB route smoke judges each identity echo per field.

Every body here is a real success envelope (``tests/test_readonly_db_validation.py``
``_real_route_body``), validated against ``apps/api/response_models`` below. The
requested identity uses discovery's ``+00:00`` spelling and every echo uses the
server's ``Z`` spelling, which is the live pairing (proposal "Why").
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from apps.api.response_models.forecast import QhhLatestProductEnvelope
from apps.api.response_models.pipeline import (
    JobLogsEnvelope,
    OpsIdentity,
    OpsLogIdentity,
    PipelineJobPageEnvelope,
    PipelineStageListEnvelope,
    PipelineStatusEnvelope,
)
from services.production_closure.readonly_db_validation import (
    ReadonlyDbValidationConfig,
    RouteHttpResponse,
    run_display_route_smoke,
)
from tests.test_readonly_db_validation import (
    IDENTITY_BOUND_ROUTES,
    _evidence_root,
    _passing_route_requester,
    _real_route_body,
    _route_name_for_path,
    _run_id,
)

REQUESTED_IDENTITY = {
    "source": "GFS",
    "cycle_time": "2026-09-24T12:00:00+00:00",
    "run_id": "fcst_gfs_2026092412_basins_sw_ylzb",
    "model_id": "basins_sw_ylzb_shud",
    "basin_id": "basins_sw_ylzb",
    "job_id": "job_fcst_gfs_2026092412_basins_sw_ylzb_forecast_0",
}
# The server's echo of REQUESTED_IDENTITY, spelled independently (UTC ``Z``).
ECHOED_IDENTITY = {
    "source": "GFS",
    "cycle_time": "2026-09-24T12:00:00Z",
    "run_id": "fcst_gfs_2026092412_basins_sw_ylzb",
    "model_id": "basins_sw_ylzb_shud",
}
FOUR_FIELDS = ("source", "cycle_time", "run_id", "model_id")


def _config() -> ReadonlyDbValidationConfig:
    return ReadonlyDbValidationConfig.from_env(
        evidence_root=_evidence_root(),
        run_id=_run_id("route-identity"),
        database_url="postgresql://readonly:secret@db.example/nhms",
    )


def _smoke(requester: Callable[[str, str], RouteHttpResponse], identity: dict[str, Any] | None = None) -> dict:
    results = run_display_route_smoke(_config(), identity or REQUESTED_IDENTITY, route_requester=requester)
    return {item["name"]: item for item in results}


def _echo_mutating_requester(route: str, mutate: Callable[[dict[str, Any]], None]) -> Callable:
    """The real envelope for every route, with ``mutate`` applied to ``route``'s echo mapping."""

    def requester(method: str, path: str) -> RouteHttpResponse:
        response = _passing_route_requester(method, path)
        if _route_name_for_path(path) == route:
            body = copy.deepcopy(response.body)
            mutate(body["data"] if route == "latest_product" else body["identity"])
            return RouteHttpResponse(status_code=response.status_code, body=body)
        return response

    return requester


def _error_requester(route: str, status_code: int, code: str) -> Callable:
    def requester(method: str, path: str) -> RouteHttpResponse:
        if _route_name_for_path(path) == route:
            return RouteHttpResponse(status_code=status_code, body={"error": {"code": code, "message": "fixture"}})
        return _passing_route_requester(method, path)

    return requester


def _source_key(route: str) -> str:
    return "source_id" if route == "latest_product" else "source"


def test_every_identity_bound_route_passes_on_a_z_echo_of_a_plus_zero_request() -> None:
    by_name = _smoke(_passing_route_requester)

    assert {name: item["status"] for name, item in by_name.items()} == dict.fromkeys(by_name, "PASS")
    for route in IDENTITY_BOUND_ROUTES:
        record = by_name[route]
        assert record["reason"] == "display_read_route_succeeded"
        assert "identity_blockers" not in record
        assert parse_qs(urlsplit(record["path"]).query)["cycle_time"] == ["2026-09-24T12:00:00+00:00"]
        assert record["response_identity"]["cycle_time"] == "2026-09-24T12:00:00Z"
    assert by_name["job_logs"]["response_identity"]["job_id"] == REQUESTED_IDENTITY["job_id"]


@pytest.mark.parametrize(
    ("field", "tampered"),
    [
        ("run_id", "fcst_ifs_2026092412_basins_sw_ylzb"),
        ("model_id", "basins_qhh_shud"),
        # One cycle hour off: the same instant rule must not blur neighbouring cycles.
        ("cycle_time", "2026-09-24T13:00:00Z"),
    ],
)
@pytest.mark.parametrize("route", IDENTITY_BOUND_ROUTES)
def test_a_tampered_echo_field_fails_naming_only_that_field(route: str, field: str, tampered: str) -> None:
    by_name = _smoke(_echo_mutating_requester(route, lambda echo: echo.__setitem__(field, tampered)))

    record = by_name[route]
    assert record["status"] == "FAIL"
    assert record["reason"] == "display_read_route_response_identity_mismatch"
    assert record["identity_blockers"] == [
        {
            "code": "READONLY_DB_ROUTE_RESPONSE_IDENTITY_MISMATCH",
            "field": field,
            "expected": REQUESTED_IDENTITY[field],
            "observed": tampered,
        }
    ]
    assert all(item["status"] == "PASS" for name, item in by_name.items() if name != route)


def test_a_tampered_job_id_echo_fails_the_job_log_route() -> None:
    by_name = _smoke(_echo_mutating_requester("job_logs", lambda echo: echo.__setitem__("job_id", "job_other_run")))

    record = by_name["job_logs"]
    assert record["status"] == "FAIL"
    assert record["identity_blockers"] == [
        {
            "code": "READONLY_DB_ROUTE_RESPONSE_IDENTITY_MISMATCH",
            "field": "job_id",
            "expected": REQUESTED_IDENTITY["job_id"],
            "observed": "job_other_run",
        }
    ]


@pytest.mark.parametrize("field", FOUR_FIELDS)
@pytest.mark.parametrize("route", IDENTITY_BOUND_ROUTES)
def test_an_echo_missing_one_field_blocks_and_keeps_the_partial_identity(route: str, field: str) -> None:
    echo_key = _source_key(route) if field == "source" else field
    by_name = _smoke(_echo_mutating_requester(route, lambda echo: echo.pop(echo_key)))

    record = by_name[route]
    assert record["status"] == "BLOCKED"
    assert record["reason"] == "display_read_route_response_identity_invalid"
    assert record["identity_blockers"] == [
        {
            "code": "READONLY_DB_ROUTE_RESPONSE_IDENTITY_MISSING",
            "field": field,
            "expected": REQUESTED_IDENTITY[field],
        }
    ]
    kept = {name: value for name, value in ECHOED_IDENTITY.items() if name != field}
    assert {name: record["response_identity"][name] for name in kept} == kept
    assert field not in record["response_identity"]


def test_a_job_log_echo_missing_its_job_id_blocks_on_that_field_only() -> None:
    by_name = _smoke(_echo_mutating_requester("job_logs", lambda echo: echo.pop("job_id")))

    record = by_name["job_logs"]
    assert record["status"] == "BLOCKED"
    assert record["identity_blockers"] == [
        {
            "code": "READONLY_DB_ROUTE_RESPONSE_IDENTITY_MISSING",
            "field": "job_id",
            "expected": REQUESTED_IDENTITY["job_id"],
        }
    ]
    assert record["response_identity"] == ECHOED_IDENTITY


def test_a_missing_field_plus_a_contradicting_field_fails() -> None:
    def mutate(echo: dict[str, Any]) -> None:
        echo.pop("model_id")
        echo["run_id"] = "fcst_gfs_2026092400_basins_sw_ylzb"

    by_name = _smoke(_echo_mutating_requester("pipeline_status", mutate))

    record = by_name["pipeline_status"]
    assert record["status"] == "FAIL"
    assert record["reason"] == "display_read_route_response_identity_mismatch"
    assert [(item["code"], item["field"]) for item in record["identity_blockers"]] == [
        ("READONLY_DB_ROUTE_RESPONSE_IDENTITY_MISMATCH", "run_id"),
        ("READONLY_DB_ROUTE_RESPONSE_IDENTITY_MISSING", "model_id"),
    ]


def test_an_unparseable_cycle_time_echo_only_matches_byte_for_byte() -> None:
    by_name = _smoke(_echo_mutating_requester("jobs", lambda echo: echo.__setitem__("cycle_time", "latest")))

    assert by_name["jobs"]["status"] == "FAIL"
    assert by_name["jobs"]["identity_blockers"] == [
        {
            "code": "READONLY_DB_ROUTE_RESPONSE_IDENTITY_MISMATCH",
            "field": "cycle_time",
            "expected": "2026-09-24T12:00:00+00:00",
            "observed": "latest",
        }
    ]

    def verbatim_requester(method: str, path: str) -> RouteHttpResponse:
        del method
        name = _route_name_for_path(path)
        if name not in IDENTITY_BOUND_ROUTES:
            return RouteHttpResponse(status_code=200, body={"request_id": "r", "status": "ok", "data": {}})
        query = {key: values[0] for key, values in parse_qs(urlsplit(path).query).items()}
        echo = {field: query[field] for field in FOUR_FIELDS}
        if name == "job_logs":
            echo["job_id"] = urlsplit(path).path.split("/")[-2]
        return RouteHttpResponse(status_code=200, body=_real_route_body(name, echo))

    verbatim = _smoke(verbatim_requester, {**REQUESTED_IDENTITY, "cycle_time": "cycle-alpha"})
    assert all(verbatim[route]["status"] == "PASS" for route in IDENTITY_BOUND_ROUTES)


@pytest.mark.parametrize("route", IDENTITY_BOUND_ROUTES)
def test_the_echoed_source_agrees_case_insensitively(route: str) -> None:
    by_name = _smoke(_echo_mutating_requester(route, lambda echo: echo.__setitem__(_source_key(route), "gfs")))

    assert by_name[route]["status"] == "PASS"
    assert "identity_blockers" not in by_name[route]


@pytest.mark.parametrize(
    ("route", "status_code", "code", "expected_status"),
    [
        ("latest_product", 404, "QHH_LATEST_PRODUCT_UNAVAILABLE", "BLOCKED"),
        ("pipeline_status", 404, "PIPELINE_STRICT_IDENTITY_NOT_FOUND", "BLOCKED"),
        ("job_logs", 409, "PIPELINE_STRICT_IDENTITY_MISMATCH", "FAIL"),
        ("jobs", 500, "INTERNAL_ERROR", "FAIL"),
    ],
)
def test_an_error_response_is_judged_by_its_error_code_alone(
    route: str, status_code: int, code: str, expected_status: str
) -> None:
    by_name = _smoke(_error_requester(route, status_code, code))

    record = by_name[route]
    assert record["status"] == expected_status
    assert record["http_status"] == status_code
    assert record["error_code"] == code
    assert "identity_blockers" not in record
    assert "response_identity" not in record


def test_latest_product_requests_the_discovered_basin_and_compares_four_fields() -> None:
    paths: dict[str, str] = {}

    def recording_requester(method: str, path: str) -> RouteHttpResponse:
        paths[str(_route_name_for_path(path))] = path
        return _passing_route_requester(method, path)

    by_name = _smoke(recording_requester)

    assert parse_qs(urlsplit(paths["latest_product"]).query) == {
        "source": ["GFS"],
        "cycle_time": ["2026-09-24T12:00:00+00:00"],
        "run_id": ["fcst_gfs_2026092412_basins_sw_ylzb"],
        "model_id": ["basins_sw_ylzb_shud"],
        "basin_id": ["basins_sw_ylzb"],
    }
    assert by_name["latest_product"]["status"] == "PASS"
    for route in ("pipeline_status", "pipeline_stages", "jobs", "job_logs"):
        assert "basin_id" not in parse_qs(urlsplit(paths[route]).query)

    # basin_id selects the product; it is not one of the four compared echo fields.
    other_basin = _smoke(_echo_mutating_requester("latest_product", lambda echo: echo.__setitem__("basin_id", "x")))
    assert other_basin["latest_product"]["status"] == "PASS"


def test_latest_product_without_a_basin_keeps_the_qhh_default_path() -> None:
    paths: dict[str, str] = {}

    def recording_requester(method: str, path: str) -> RouteHttpResponse:
        paths[str(_route_name_for_path(path))] = path
        return _passing_route_requester(method, path)

    identity = {key: value for key, value in REQUESTED_IDENTITY.items() if key != "basin_id"}
    _smoke(recording_requester, identity)

    assert "basin_id" not in parse_qs(urlsplit(paths["latest_product"]).query)


@pytest.mark.parametrize(
    ("route", "envelope"),
    [
        ("pipeline_status", PipelineStatusEnvelope),
        ("pipeline_stages", PipelineStageListEnvelope),
        ("jobs", PipelineJobPageEnvelope),
        ("job_logs", JobLogsEnvelope),
        ("latest_product", QhhLatestProductEnvelope),
    ],
)
def test_every_fixture_success_body_validates_against_its_response_model(route: str, envelope: type) -> None:
    path = next(
        path
        for path in _recorded_route_paths()
        if _route_name_for_path(path) == route
    )
    body = _passing_route_requester("GET", path).body

    envelope.model_validate(body)
    if route == "job_logs":
        assert OpsLogIdentity.model_validate(body["identity"]).job_id == REQUESTED_IDENTITY["job_id"]
    elif route != "latest_product":
        assert OpsIdentity.model_validate(body["identity"]).cycle_time == "2026-09-24T12:00:00Z"
    else:
        assert "identity" not in body
        assert "identity" not in body["data"]


def test_an_empty_job_page_fixture_validates_against_its_response_model() -> None:
    body = _real_route_body("jobs", dict(ECHOED_IDENTITY))
    body["data"] = {**body["data"], "items": [], "total": 0}

    PipelineJobPageEnvelope.model_validate(body)
    OpsIdentity.model_validate(body["identity"])


def _recorded_route_paths() -> list[str]:
    paths: list[str] = []

    def recording_requester(method: str, path: str) -> RouteHttpResponse:
        paths.append(path)
        return _passing_route_requester(method, path)

    _smoke(recording_requester)
    return paths
