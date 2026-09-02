"""Every API error response leaves a redacted, grep-able server-side line.

Issue #1704: `apps/api/errors.py::error_response()` serialised `ApiError` and
validation errors for the client but wrote nothing server-side, so a
`STATION_FORCING_FILE_MALFORMED` 500 carrying
`parse_reason: "concurrent-replace: ..."` could not be found afterwards in
`/tmp/display-api.log` -- the operator had the client's `X-Request-ID` and
nothing to grep it against. The repo had no logging configuration at all, and
the production unit passes no `--log-config`, so the handler install in
`apps/api/main.py` is part of the contract, not a convenience.

`details` is client-influenced and carries absolute paths, so the line is
redacted before it is written: a key-level pass over `rejected_value` /
`rejected_values`, whose values are raw client input of any shape, then
`redact_audit_payload` for value-shaped secrets. That redaction is NOT total,
and the residual is frozen here (see
`test_client_identifiers_under_other_keys_stay_verbatim`): identifiers under
any other key stay verbatim by design.

Two more bounds on the line, added after round-1 review of this PR: the
rendered `details` is cut to a fixed byte budget (the validation arm renders
one entry per invalid item, so an authorised bulk POST used to write megabytes
into an unrotated unit log), and the inbound `X-Request-ID` is echoed only when
it matches `[A-Za-z0-9._-]{1,64}` -- it is rendered bare into a
space-separated line, so a forged header could otherwise inject a second
`code=`/`path=` pair into the very line an operator greps.
"""

from __future__ import annotations

import io
import json
import logging
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from apps.api import errors, main
from apps.api.errors import ApiError, error_response, register_error_handlers
from apps.api.routes.hydro_display import get_hydro_display_session
from packages.common.object_store_forcing import (
    CONCURRENT_REPLACE_REASON_PREFIX,
    _station_csv_failure_reason,
)
from packages.common.safe_fs import SafeFilesystemError

REQUEST_ID = "req-1704-abc"
MALFORMED_DETAILS: dict[str, Any] = {
    "station_id": "STA-0001",
    "expected_path": "/ghdc/data/nwm/forcing/STA-0001.csv",
    "parse_reason": "concurrent-replace: row count shrank mid-read",
}


def _request(
    path: str = "/api/v1/met/stations/STA-0001/series",
    *,
    state_request_id: str | None = REQUEST_ID,
    header_request_id: str | None = None,
) -> Any:
    """The minimal Request surface `error_response()` actually touches.

    `headers` is part of that surface now that `error_response()` resolves the
    id through `resolve_request_id`, which falls back to the inbound header
    when the state value is not of the accepted shape. A real `Request` always
    carries headers, so this is not a widening of the fake.
    """
    return SimpleNamespace(
        state=SimpleNamespace(request_id=state_request_id),
        url=SimpleNamespace(path=path),
        headers={"X-Request-ID": header_request_id} if header_request_id is not None else {},
    )


@pytest.fixture
def api_error_logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.DEBUG, logger="apps.api.errors")
    return caplog


# --------------------------------------------------------------------------- #
# The line itself
# --------------------------------------------------------------------------- #
def test_api_error_line_carries_the_request_id_code_status_and_path(
    api_error_logs: pytest.LogCaptureFixture,
) -> None:
    response = error_response(
        _request(),
        status_code=500,
        code="STATION_FORCING_FILE_MALFORMED",
        message="Station forcing file is malformed.",
        details=MALFORMED_DETAILS,
    )

    text = api_error_logs.text
    assert "api_error " in text
    # The header the client is told to quote is the key the operator greps.
    assert response.headers["X-Request-ID"] == REQUEST_ID
    assert f"request_id={REQUEST_ID}" in text
    assert "code=STATION_FORCING_FILE_MALFORMED" in text
    assert "status=500" in text
    assert "path=/api/v1/met/stations/STA-0001/series" in text


def test_absolute_path_is_redacted_while_the_diagnosis_survives(
    api_error_logs: pytest.LogCaptureFixture,
) -> None:
    """The reason is why the line exists; the path is what must not be in it."""
    error_response(
        _request(),
        status_code=500,
        code="STATION_FORCING_FILE_MALFORMED",
        message="Station forcing file is malformed.",
        details=MALFORMED_DETAILS,
    )

    text = api_error_logs.text
    assert "'expected_path': '[redacted]'" in text
    assert "/ghdc/data/nwm/forcing/STA-0001.csv" not in text
    assert "concurrent-replace: row count shrank mid-read" in text
    assert "'station_id': 'STA-0001'" in text


def test_the_real_production_parse_reason_survives_redaction_verbatim(
    api_error_logs: pytest.LogCaptureFixture,
) -> None:
    """The spec scenario's oracle is production code, not a literal typed here.

    `parse_reason` must stay readable or the line cannot answer "why did this
    500 happen". It only survives because `_public_error_reason()` already
    scrubs absolute paths to `<path>` upstream: had the raw path stayed in the
    text, `redact_audit_payload` would flatten the WHOLE string to `[redacted]`.
    """
    target = Path("/ghdc/data/nwm/forcing/basins_heihe_shud/ifs/2026062012/shud/heihe_forc_001.csv")
    reason = _station_csv_failure_reason(
        SafeFilesystemError(f"Target file changed while being opened: {target}", kind="identity_changed")
    )
    assert reason.startswith(CONCURRENT_REPLACE_REASON_PREFIX)

    error_response(
        _request(),
        status_code=500,
        code="STATION_FORCING_FILE_MALFORMED",
        message="Station forcing file is malformed.",
        details={"station_id": "heihe_forc_001", "expected_path": str(target), "parse_reason": reason},
    )

    text = api_error_logs.text
    assert f"'parse_reason': '{reason}'" in text
    assert str(target) not in text
    assert "'expected_path': '[redacted]'" in text


def test_response_body_is_not_redacted_by_the_logging_path(
    api_error_logs: pytest.LogCaptureFixture,
) -> None:
    """Redaction is for the log only; the client's payload is unchanged."""
    response = error_response(
        _request(),
        status_code=500,
        code="STATION_FORCING_FILE_MALFORMED",
        message="Station forcing file is malformed.",
        details=MALFORMED_DETAILS,
    )

    assert b"/ghdc/data/nwm/forcing/STA-0001.csv" in bytes(response.body)
    assert MALFORMED_DETAILS["expected_path"] == "/ghdc/data/nwm/forcing/STA-0001.csv"


@pytest.mark.parametrize(
    ("status_code", "expected_level"),
    [(500, "ERROR"), (503, "ERROR"), (422, "WARNING"), (404, "WARNING"), (401, "WARNING")],
)
def test_level_is_error_for_server_faults_and_warning_for_client_faults(
    api_error_logs: pytest.LogCaptureFixture, status_code: int, expected_level: str
) -> None:
    error_response(_request(), status_code=status_code, code="ANY_CODE", message="m", details=None)

    assert [record.levelname for record in api_error_logs.records] == [expected_level]


def test_every_error_response_logs_exactly_one_line(api_error_logs: pytest.LogCaptureFixture) -> None:
    for index in range(3):
        error_response(_request(), status_code=404, code=f"CODE_{index}", message="m", details=None)

    assert len(api_error_logs.records) == 3
    assert [record.name for record in api_error_logs.records] == ["apps.api.errors"] * 3


# --------------------------------------------------------------------------- #
# Raw client input: `rejected_value` regardless of shape
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "rejected",
    ["sk-live-ABCDEF0123456789", "operator@example.com", "1 OR 1=1; DROP TABLE core.basin", 17, None],
    ids=["api-key", "email", "sql-fragment", "int", "none"],
)
def test_rejected_value_is_redacted_whatever_its_shape(
    api_error_logs: pytest.LogCaptureFixture, rejected: Any
) -> None:
    """`redact_audit_payload` alone lets non-path client input through verbatim."""
    error_response(
        _request(),
        status_code=422,
        code="VALIDATION_ERROR",
        message="Request validation failed.",
        details=[{"field": "query.limit", "rejected_value": rejected, "reason": "Invalid value"}],
    )

    text = api_error_logs.text
    assert "'rejected_value': '[redacted]'" in text
    if isinstance(rejected, str):
        assert rejected not in text
    # The surrounding diagnosis must survive, or the line is useless.
    assert "'field': 'query.limit'" in text
    assert "'reason': 'Invalid value'" in text


def test_every_rejected_value_in_a_multi_error_list_is_redacted(
    api_error_logs: pytest.LogCaptureFixture,
) -> None:
    error_response(
        _request(),
        status_code=422,
        code="VALIDATION_ERROR",
        message="Request validation failed.",
        details=[
            {"field": "query.a", "rejected_value": "sk-live-AAA", "reason": "Invalid value"},
            {"field": "query.b", "rejected_value": "sk-live-BBB", "reason": "Invalid value"},
        ],
    )

    text = api_error_logs.text
    assert text.count("'rejected_value': '[redacted]'") == 2
    assert "sk-live-" not in text


def test_nested_rejected_value_is_redacted(api_error_logs: pytest.LogCaptureFixture) -> None:
    error_response(
        _request(),
        status_code=422,
        code="VALIDATION_ERROR",
        message="Request validation failed.",
        details={"errors": [{"nested": {"rejected_value": "sk-live-CCC"}}]},
    )

    assert "sk-live-CCC" not in api_error_logs.text
    assert "'rejected_value': '[redacted]'" in api_error_logs.text


def test_redact_error_details_leaves_the_input_object_untouched() -> None:
    details = {"rejected_value": "sk-live-DDD", "expected_path": "/ghdc/x.csv"}

    redacted = errors._redact_error_details(details)

    assert redacted == {"rejected_value": "[redacted]", "expected_path": "[redacted]"}
    assert details == {"rejected_value": "sk-live-DDD", "expected_path": "/ghdc/x.csv"}


# --------------------------------------------------------------------------- #
# Logging may never change the response
# --------------------------------------------------------------------------- #
def test_unprocessable_details_still_returns_the_response_and_logs_no_payload(
    api_error_logs: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A redactor failure must degrade to a marker, never to the raw payload.

    Fault-injected at the redaction seam: the response contract already requires
    `details` to be JSON-encodable, so the only reachable way for redaction to
    fail is the redactor itself raising on some future payload shape.
    """

    def _boom(_value: Any) -> Any:
        raise ValueError("cannot traverse")

    monkeypatch.setattr(errors, "redact_audit_payload", _boom)

    response = error_response(
        _request(),
        status_code=500,
        code="DATABASE_ERROR",
        message="boom",
        details={"secret_token": "sk-live-EEE"},
    )

    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == REQUEST_ID
    assert json.loads(bytes(response.body))["error"]["details"] == {"secret_token": "sk-live-EEE"}
    text = api_error_logs.text
    assert "details=<redaction-failed:ValueError>" in text
    assert "sk-live-EEE" not in text


@pytest.mark.parametrize("details", [None, "plain reason text", 42, []], ids=["none", "str", "int", "empty"])
def test_non_mapping_details_render_without_raising(
    api_error_logs: pytest.LogCaptureFixture, details: Any
) -> None:
    response = error_response(_request(), status_code=500, code="DATABASE_ERROR", message="m", details=details)

    assert response.status_code == 500
    assert len(api_error_logs.records) == 1
    assert f"details={details}" in api_error_logs.text


def test_a_broken_logger_does_not_break_the_error_response(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("logging backend is down")

    monkeypatch.setattr(errors.logger, "log", _boom)

    response = error_response(
        _request(), status_code=500, code="DATABASE_ERROR", message="boom", details=MALFORMED_DETAILS
    )

    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == REQUEST_ID


# --------------------------------------------------------------------------- #
# Through the real handlers, over HTTP
# --------------------------------------------------------------------------- #
class _Payload(BaseModel):
    limit: int


_PARAM_ROUTE = "/api/v1/met/stations/{station_id}/series"
_TILE_ROUTE = "/api/v1/tiles/hydro/{run_id}/{variable}/{valid_time}/{z}/{x}/{y}.pbf"
_STR_DETAILS = "line one\ncode=OK status=200"


def _probe_app() -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/api/v1/probe-api-error")
    def _raise_api_error() -> None:
        raise ApiError(
            status_code=500,
            code="STATION_FORCING_FILE_MALFORMED",
            message="Station forcing file is malformed.",
            details=MALFORMED_DETAILS,
        )

    @app.post("/api/v1/probe-validation")
    def _validated(payload: _Payload) -> dict[str, int]:
        return {"limit": payload.limit}

    # A matched path-parameter route: the segment is client-controlled, so this
    # is where a forged `path=` reaches the line. `details=None` keeps the
    # assertions in the forgery block about the PATH segment only.
    @app.get(_PARAM_ROUTE)
    def _raise_for_station(station_id: str) -> None:
        raise ApiError(status_code=404, code="STATION_NOT_FOUND", message="Unknown station.", details=None)

    # The DATETIME-shaped path parameter the tile routes declare
    # (apps/api/routes/hydro_display.py:289,296,347,353). Declared with the same
    # annotation on the same template so the `%3A` pin below is about a shape
    # this app really serves, not one typed into a test.
    @app.get(_TILE_ROUTE)
    def _raise_for_tile(run_id: str, variable: str, valid_time: datetime, z: int, x: int, y: int) -> None:
        raise ApiError(status_code=404, code="TILE_NOT_FOUND", message="Unknown tile.", details=None)

    # A NON-mapping `details`: `_render_details` calls `str()`, not `repr()`, so
    # nothing quotes this value on its way into the line.
    @app.get("/api/v1/probe-str-details")
    def _raise_with_str_details() -> None:
        raise ApiError(status_code=500, code="DATABASE_ERROR", message="boom", details=_STR_DETAILS)

    return app


def test_api_error_handler_logs_the_header_request_id(api_error_logs: pytest.LogCaptureFixture) -> None:
    client = TestClient(_probe_app(), raise_server_exceptions=False)

    response = client.get("/api/v1/probe-api-error")

    assert response.status_code == 500
    request_id = response.headers["X-Request-ID"]
    assert response.json()["request_id"] == request_id
    text = api_error_logs.text
    assert f"request_id={request_id}" in text
    assert "code=STATION_FORCING_FILE_MALFORMED" in text
    assert "path=/api/v1/probe-api-error" in text
    assert "'expected_path': '[redacted]'" in text


def test_validation_handler_logs_a_redacted_line(api_error_logs: pytest.LogCaptureFixture) -> None:
    client = TestClient(_probe_app(), raise_server_exceptions=False)

    response = client.post("/api/v1/probe-validation", json={"limit": "sk-live-FFF"})

    assert response.status_code == 422
    request_id = response.headers["X-Request-ID"]
    text = api_error_logs.text
    assert f"request_id={request_id}" in text
    assert "code=VALIDATION_ERROR" in text
    assert "status=422" in text
    assert "'rejected_value': '[redacted]'" in text
    assert "sk-live-FFF" not in text
    # The client still sees its own input; only the server log is redacted.
    assert response.json()["error"]["details"][0]["rejected_value"] == "sk-live-FFF"


def test_slurm_validation_errors_stay_unlogged_and_unchanged(
    api_error_logs: pytest.LogCaptureFixture,
) -> None:
    """Documented blind spot: /api/v1/slurm has its own handler (#1704 non-goal)."""
    app = FastAPI()
    register_error_handlers(app)

    @app.post("/api/v1/slurm/probe")
    def _validated(payload: _Payload) -> dict[str, int]:
        return {"limit": payload.limit}

    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/api/v1/slurm/probe", json={"limit": "nope"})

    assert response.status_code == 422
    assert api_error_logs.records == []


# --------------------------------------------------------------------------- #
# The handler install in main.py
# --------------------------------------------------------------------------- #
def test_installing_the_handler_twice_leaves_exactly_one() -> None:
    """The second install must be a no-op, not a second handler (duplicate lines).

    `main.py` already ran the install at import time, so the list has to be
    cleared first or "exactly one" would be satisfied by that pre-existing
    handler no matter what a second install did. Handler IDENTITY is asserted
    too: replacing the handler in place would also leave a count of one while
    silently dropping any stream an operator had swapped in.
    """
    api_logger = logging.getLogger(main.API_LOGGER_NAME)
    before = list(api_logger.handlers)
    try:
        api_logger.handlers = []

        main._install_api_log_handler()
        assert len(api_logger.handlers) == 1
        installed = api_logger.handlers[0]
        assert isinstance(installed, logging.StreamHandler)

        main._install_api_log_handler()

        assert api_logger.handlers == [installed]
    finally:
        api_logger.handlers = before


def test_installed_handler_renders_timestamp_level_and_logger_name() -> None:
    api_logger = logging.getLogger(main.API_LOGGER_NAME)
    handler = next(h for h in api_logger.handlers if isinstance(h, logging.StreamHandler))
    buffer = io.StringIO()
    previous = handler.setStream(buffer)
    try:
        error_response(
            _request(), status_code=500, code="DATABASE_ERROR", message="boom", details=MALFORMED_DETAILS
        )
    finally:
        handler.setStream(previous)

    rendered = buffer.getvalue().strip()
    assert rendered, "the installed handler emitted nothing"
    # "<asctime> ERROR apps.api.errors api_error request_id=..."
    assert " ERROR apps.api.errors api_error " in rendered
    assert f"request_id={REQUEST_ID}" in rendered
    assert rendered.startswith("20")
    assert "/ghdc/data/nwm/forcing/STA-0001.csv" not in rendered


def test_api_logger_propagates_so_one_record_reaches_both_ends() -> None:
    """Propagation on: uvicorn's root has no handler, so no duplicate line."""
    assert logging.getLogger(main.API_LOGGER_NAME).propagate is True


def test_models_route_logger_renders_through_the_installed_handler() -> None:
    """Must-preserve: the pre-existing `apps.api.routes.models` logger is a child."""
    api_logger = logging.getLogger(main.API_LOGGER_NAME)
    handler = next(h for h in api_logger.handlers if isinstance(h, logging.StreamHandler))
    buffer = io.StringIO()
    previous = handler.setStream(buffer)
    try:
        logging.getLogger("apps.api.routes.models").error(
            "Model registry operation failed.", extra={"error_type": "ModelRegistryError"}
        )
    finally:
        handler.setStream(previous)

    rendered = buffer.getvalue()
    assert " ERROR apps.api.routes.models Model registry operation failed." in rendered
    assert rendered.count("Model registry operation failed.") == 1


# --------------------------------------------------------------------------- #
# Client input under the plural key, and the residual under every other key
#
# `rejected_values` is the sibling shape the stores raise. It reaches this
# chokepoint through `ApiError.details` in two forms: a LIST (an unknown
# `variables` token, packages/common/forecast_store.py:239) and a MAPPING of
# field -> reflected value (apps/api/routes/forecast.py:297,
# apps/api/routes/pipeline.py:1360). Both are raw client input of any shape.
# --------------------------------------------------------------------------- #
def test_rejected_values_list_is_redacted(api_error_logs: pytest.LogCaptureFixture) -> None:
    """The store-raised form: `{"field": "variables", "rejected_values": [...]}`."""
    error_response(
        _request(),
        status_code=422,
        code="VALIDATION_ERROR",
        message="Invalid station forcing variable.",
        details={
            "field": "variables",
            "rejected_values": ["sk-live-GGG", "1 OR 1=1; DROP TABLE core.basin"],
            "allowed_values": ["prcp", "temp"],
        },
    )

    text = api_error_logs.text
    assert "'rejected_values': '[redacted]'" in text
    assert "sk-live-GGG" not in text
    assert "DROP TABLE" not in text
    # The diagnosis either side of it must survive.
    assert "'field': 'variables'" in text
    assert "'allowed_values': ['prcp', 'temp']" in text


def test_rejected_values_mapping_is_redacted(api_error_logs: pytest.LogCaptureFixture) -> None:
    """The route-built form: a mapping of rejected field -> reflected value."""
    error_response(
        _request(),
        status_code=422,
        code="VALIDATION_ERROR",
        message="Request validation failed.",
        details={
            "reason": "unsupported_filter",
            "rejected_values": {"run_id": "sk-live-HHH", "source": "operator@example.com"},
        },
    )

    text = api_error_logs.text
    assert "'rejected_values': '[redacted]'" in text
    assert "sk-live-HHH" not in text
    assert "operator@example.com" not in text
    assert "'reason': 'unsupported_filter'" in text


def test_client_identifiers_under_other_keys_stay_verbatim(
    api_error_logs: pytest.LogCaptureFixture,
) -> None:
    """Freezes the recorded residual (design D4), so nobody reads the line as fully redacted.

    Only `_CLIENT_INPUT_KEYS` are redacted by key. A client-controlled
    identifier under any other key survives unless it is value-shaped
    (path/URI/checksum) or under a sensitive key. That is deliberate -- the
    line exists to say WHICH station/run failed -- and it is why the runbook
    must not promise that client input never lands in the log.
    """
    error_response(
        _request(),
        status_code=404,
        code="STATION_NOT_FOUND",
        message="Unknown station.",
        details={"station_id": "operator@example.com", "layer_id": "sk-live-III"},
    )

    text = api_error_logs.text
    assert "'station_id': 'operator@example.com'" in text
    assert "'layer_id': 'sk-live-III'" in text


# --------------------------------------------------------------------------- #
# The line is bounded
#
# The validation arm renders one entry PER invalid item, so before the budget a
# single authorised POST wrote a multi-megabyte line into the unrotated unit
# log (measured 8.5 MB from a 20 000-item body). The RESPONSE stays whole; only
# what is written server-side is cut.
# --------------------------------------------------------------------------- #
_BULK_ITEM_COUNT = 5_000


class _BulkPayload(BaseModel):
    items: list[int]


def _bulk_probe_app() -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)

    @app.post("/api/v1/probe-bulk")
    def _validated(payload: _BulkPayload) -> dict[str, int]:
        return {"count": len(payload.items)}

    return app


def _bulk_invalid_body() -> dict[str, list[str]]:
    return {"items": [f"not-an-int-{index:05d}" for index in range(_BULK_ITEM_COUNT)]}


def test_oversized_details_are_truncated_to_the_render_budget(
    api_error_logs: pytest.LogCaptureFixture,
) -> None:
    client = TestClient(_bulk_probe_app(), raise_server_exceptions=False)

    response = client.post("/api/v1/probe-bulk", json=_bulk_invalid_body())

    assert response.status_code == 422
    assert len(api_error_logs.records) == 1
    message = api_error_logs.records[0].getMessage()
    prefix, separator, rendered = message.partition(" details=")
    assert separator, message[:200]

    kept, marker, tail = rendered.rpartition("…[truncated ")
    assert marker, "the rendered details were not truncated"
    assert tail.endswith(" bytes]")
    assert int(tail.removesuffix(" bytes]")) > 0
    # The kept text is inside the budget, and the whole line is the budget plus
    # the fixed prefix and marker -- computed from this record, not hard-coded.
    assert len(kept.encode("utf-8")) <= errors._DETAILS_RENDER_BUDGET_BYTES
    # "budget + a FIXED overhead" is only a claim if the overhead is bounded
    # independently of this record: the prefix is the request id, code, status
    # and path, the tail is the marker.
    overhead = len(prefix.encode("utf-8")) + len((separator + marker + tail).encode("utf-8"))
    # 256 is this route's short synthetic path, not a general ceiling: the real
    # ceiling is budget + rendered path length, and `path=` carries the SAME
    # budget (see `test_an_oversized_path_is_truncated_to_the_render_budget`),
    # so the whole line is bounded by twice the budget plus two markers.
    assert overhead < 256, prefix
    assert len(message.encode("utf-8")) <= errors._DETAILS_RENDER_BUDGET_BYTES + overhead
    # Vacuity guard: without the budget this line would have been ~100x longer.
    assert len(message.encode("utf-8")) < len(response.content) // 10
    # Truncated or not, the redaction still ran.
    assert "not-an-int-0" not in message


def test_oversized_response_body_is_not_truncated_by_the_logging_path(
    api_error_logs: pytest.LogCaptureFixture,
) -> None:
    """The budget is a LOG bound. The client still gets one entry per bad item."""
    client = TestClient(_bulk_probe_app(), raise_server_exceptions=False)

    response = client.post("/api/v1/probe-bulk", json=_bulk_invalid_body())

    details = response.json()["error"]["details"]
    assert len(details) == _BULK_ITEM_COUNT
    assert details[0]["rejected_value"] == "not-an-int-00000"
    assert details[-1]["rejected_value"] == f"not-an-int-{_BULK_ITEM_COUNT - 1:05d}"
    assert details[0]["field"] == "body.items.0"
    assert len(response.content) > errors._DETAILS_RENDER_BUDGET_BYTES


def _contains_value(container: Any, needle: str) -> bool:
    if isinstance(container, str):
        return container == needle
    if isinstance(container, Mapping):
        return any(_contains_value(item, needle) for item in container.values())
    if isinstance(container, (list, tuple)):
        return any(_contains_value(item, needle) for item in container)
    return False


def test_client_input_keys_are_redacted_before_the_audit_walk(
    api_error_logs: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`redact_audit_payload` must never receive the raw client-input subtree.

    Recorded rather than raised: `_log_error_response` swallows every redaction
    exception into `<redaction-failed:...>`, so a raising probe would be
    invisible to `pytest.raises` and would prove nothing.
    """
    sentinel = "sk-live-BEFORE-THE-AUDIT-WALK"
    saw_sentinel: list[bool] = []
    real_redact = errors.redact_audit_payload

    def _recording(value: Any) -> Any:
        saw_sentinel.append(_contains_value(value, sentinel))
        return real_redact(value)

    monkeypatch.setattr(errors, "redact_audit_payload", _recording)

    error_response(
        _request(),
        status_code=422,
        code="VALIDATION_ERROR",
        message="Request validation failed.",
        details=[{"field": "body.items.0", "rejected_value": sentinel, "reason": "Invalid value"}],
    )

    assert saw_sentinel == [False]
    text = api_error_logs.text
    assert "redaction-failed" not in text
    assert "'rejected_value': '[redacted]'" in text
    assert sentinel not in text


# --------------------------------------------------------------------------- #
# The request id is not client-forgeable (#1704)
#
# `request_id=` is rendered bare into a space-separated line, so an inbound
# header carrying ` code=... path=...` used to write a second, attacker-chosen
# set of fields into the very line an operator greps. The header is now echoed
# only when it matches [A-Za-z0-9._-]{1,64}.
# --------------------------------------------------------------------------- #
_FORGED_REQUEST_ID = "7f3a code=OK status=200 path=/healthz"


def _assert_minted_uuid(value: str) -> None:
    assert UUID(value).version == 4


def test_a_forged_request_id_header_cannot_inject_line_fields(
    api_error_logs: pytest.LogCaptureFixture,
) -> None:
    client = TestClient(_probe_app(), raise_server_exceptions=False)

    response = client.get("/api/v1/probe-api-error", headers={"X-Request-ID": _FORGED_REQUEST_ID})

    assert response.status_code == 500
    _assert_minted_uuid(response.headers["X-Request-ID"])
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
    message = api_error_logs.records[0].getMessage()
    assert message.count("code=") == 1
    assert "path=/healthz" not in message
    assert "status=200" not in message
    assert f"request_id={response.headers['X-Request-ID']}" in message


def test_a_conforming_request_id_header_is_echoed_unchanged(
    api_error_logs: pytest.LogCaptureFixture,
) -> None:
    client = TestClient(_probe_app(), raise_server_exceptions=False)

    response = client.get("/api/v1/probe-api-error", headers={"X-Request-ID": REQUEST_ID})

    assert response.headers["X-Request-ID"] == REQUEST_ID
    assert response.json()["request_id"] == REQUEST_ID
    assert f"request_id={REQUEST_ID}" in api_error_logs.records[0].getMessage()


@pytest.mark.parametrize(
    ("header", "echoed"),
    [
        ("req-1704-abc", True),
        ("a" * 64, True),
        ("a" * 65, False),
        ("", False),
        ("req/1704", False),
        ("req 1704", False),
        ("req\n1704", False),
    ],
    ids=["conforming", "at-the-bound", "over-the-bound", "empty", "slash", "space", "newline"],
)
def test_request_id_acceptance_rule(header: str, echoed: bool) -> None:
    resolved = errors.sanitize_request_id(header)

    if echoed:
        assert resolved == header
    else:
        assert resolved != header
        _assert_minted_uuid(resolved)


def test_a_missing_request_id_header_is_minted() -> None:
    _assert_minted_uuid(errors.sanitize_request_id(None))


def test_the_pre_body_denial_path_also_refuses_a_forged_request_id(
    api_error_logs: pytest.LogCaptureFixture,
) -> None:
    """`main._ensure_request_id` runs BEFORE `add_request_id` on this path.

    The unauthenticated POST is denied by `_PRE_BODY_PROTECTED_MUTATIONS` in
    apps/api/main.py, which mints the id itself; without the shared helper the
    forged header would reach the response header, the audit record and the log
    line from a second code path.
    """
    client = TestClient(main.app, raise_server_exceptions=False)

    response = client.post(
        "/api/v1/basins",
        headers={"X-Request-ID": _FORGED_REQUEST_ID},
        json={"model_id": "large_model"},
    )

    assert response.status_code == 401
    request_id = response.headers["X-Request-ID"]
    _assert_minted_uuid(request_id)
    assert response.json()["request_id"] == request_id
    body_audit = response.json()["error"]["details"]["audit_record"]
    assert body_audit["request_id"] == request_id
    message = api_error_logs.records[0].getMessage()
    assert f"request_id={request_id}" in message
    assert message.count("code=") == 1
    assert "path=/healthz" not in message


# --------------------------------------------------------------------------- #
# The path segment is not client-forgeable either (#1704)
#
# `path=` is rendered bare from `request.url.path`, which is the DECODED URL:
# on any matched path-parameter route the client owns that segment, so
# `/api/v1/met/stations/STA%20code=OK%20.../series` used to write a second
# `code=`/`status=`/`request_id=` triple into the very line the runbook tells
# the operator to `grep -F request_id=<id>`. Control bytes came through the
# same way -- a `%00` makes `grep -F` answer "Binary file matches" for the whole
# unrotated log, and `%1B[2K` erases the operator's terminal line. The path is
# now percent-encoded with `safe="/"`, which leaves a clean path unchanged.
# --------------------------------------------------------------------------- #
_FORGED_PATH_SEGMENT = "STA%20code=OK%20status=200%20request_id=deadbeef%20"
# Expected value derived from the URL grammar, not from the implementation:
# the decoded segment is `STA code=OK status=200 request_id=deadbeef `, and
# RFC 3986 percent-encoding of SP is `%20` and of `=` is `%3D`.
_FORGED_PATH_ENCODED = "/api/v1/met/stations/STA%20code%3DOK%20status%3D200%20request_id%3Ddeadbeef%20/series"


def _logged_path(message: str) -> str:
    """The `path=` field as one whitespace-free token, or fail loudly."""
    match = re.search(r" path=(\S+) details=", message)
    assert match, f"the line has no single-token path= field: {message!r}"
    return match.group(1)


def _assert_no_control_byte_anywhere(message: str) -> None:
    """The strict rule applied to the WHOLE record, details segment included.

    Only sound where `details` cannot carry a raw control byte -- `None`, or a
    mapping, whose values render through `repr`. The path-forgery scenario
    states this about the record, so it is asserted at those call sites instead
    of being folded into `_assert_one_clean_physical_line`, which must stay
    true for the bare-string `details` arm as well.
    """
    for char in message:
        assert not (ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F), f"control char {char!r} in {message!r}"
        assert char not in {"\u2028", "\u2029"}, f"unicode line break {char!r} in {message!r}"


def _assert_one_of_each_field(message: str) -> None:
    """Exactly one `request_id=`, `code=`, `status=` and `path=` in the FIELD region.

    Counted before ` details=`, not over the whole line: the `details=` segment
    echoes client input verbatim by design (the recorded redaction residual),
    so a look-alike `code=OK` there is expected and is exactly why the runbook
    tells the operator to parse by position. A whole-line count would either
    fail on that or, worse, quietly pin the absence of an echo that a future
    route is free to add.
    """
    fields, separator, _rendered = message.partition(" details=")
    assert separator, f"the line has no details= field: {message!r}"
    for field in ("request_id=", "code=", "status=", "path="):
        assert fields.count(field) == 1, f"{field} is not unique in {fields!r}"


# The seven characters `str.splitlines()` splits on, written out here rather
# than imported from `errors`: this is the oracle, not a mirror of the mapping
# under test.
_LINE_BREAKING_CHARS = frozenset({"\n", "\r", "\x0b", "\x0c", "\x85", "\u2028", "\u2029"})


def _assert_one_clean_physical_line(message: str) -> None:
    """One grep-able record, held to two different rules by region.

    The FIELD region -- everything before ` details=`, i.e. what the runbook
    tells the operator to parse positionally -- carries no C0/C1 control byte,
    no DEL and no Unicode line break: `request_id=` is shape-checked and
    `path=` is percent-encoded, so nothing there is client-controlled.

    The DETAILS region is held only to "does not break the line", because that
    is all `_render_details` promises: it escapes the seven line-breaking
    characters and nothing else, so a non-mapping `details` carrying ESC or TAB
    would reach the line verbatim. No arm produces one today (a mapping renders
    through `repr`, which escapes them), so the old whole-line rule passed only
    because nothing exercised it -- a helper that promises more than the code
    does is a false receipt, not a stricter test.
    """
    fields, separator, rendered = message.partition(" details=")
    assert separator, f"the line has no details= field: {message!r}"
    for char in fields:
        assert not (ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F), f"control char {char!r} in {fields!r}"
        assert char not in {"\u2028", "\u2029"}, f"unicode line break {char!r} in {fields!r}"
    for char in rendered:
        assert char not in _LINE_BREAKING_CHARS, f"line break {char!r} in details={rendered!r}"


def test_a_forged_path_segment_cannot_inject_line_fields(
    api_error_logs: pytest.LogCaptureFixture,
) -> None:
    client = TestClient(_probe_app(), raise_server_exceptions=False)

    response = client.get(f"/api/v1/met/stations/{_FORGED_PATH_SEGMENT}/series")

    assert response.status_code == 404
    message = api_error_logs.records[0].getMessage()
    assert _logged_path(message) == _FORGED_PATH_ENCODED
    # The two bytes that make a forged field parse as a field are gone.
    assert "%20" in _logged_path(message)
    assert "%3D" in _logged_path(message)
    # The field region still carries exactly one of each field an operator greps on.
    _assert_one_of_each_field(message)
    assert f"request_id={response.headers['X-Request-ID']}" in message
    assert "status=404" in message
    _assert_one_clean_physical_line(message)
    # `details=None` here, so the whole record is held to the strict rule.
    _assert_no_control_byte_anywhere(message)


def test_a_clean_path_segment_is_rendered_byte_identically(
    api_error_logs: pytest.LogCaptureFixture,
) -> None:
    """The encoding must be invisible on an identifier-shaped path, or the runbook shape moves.

    The segment carries every unreserved character class RFC 3986 names --
    ALPHA, DIGIT and all four of `-`, `.`, `_`, `~` -- because those are
    precisely the bytes `quote(safe="/")` leaves alone, and "a clean path is
    byte-identical" is a claim about that set, not about the one identifier
    shape this app happens to use most.
    """
    client = TestClient(_probe_app(), raise_server_exceptions=False)

    client.get("/api/v1/met/stations/STA-0001.v2_beta~rc/series")

    message = api_error_logs.records[0].getMessage()
    assert _logged_path(message) == "/api/v1/met/stations/STA-0001.v2_beta~rc/series"
    assert "%" not in _logged_path(message)


@pytest.mark.parametrize(
    ("segment", "expected_path"),
    [
        ("x%00y", "/api/v1/met/stations/x%00y/series"),
        ("x%1B%5B2Ky", "/api/v1/met/stations/x%1B%5B2Ky/series"),
        ("x%7Fy", "/api/v1/met/stations/x%7Fy/series"),
        ("x%C2%85y", "/api/v1/met/stations/x%C2%85y/series"),
        ("x%E2%80%A8y", "/api/v1/met/stations/x%E2%80%A8y/series"),
        ("x%E2%80%A9y", "/api/v1/met/stations/x%E2%80%A9y/series"),
    ],
    ids=["nul", "ansi-erase-line", "del", "nel-u0085", "line-separator-u2028", "paragraph-separator-u2029"],
)
def test_control_bytes_in_the_path_are_percent_encoded(
    api_error_logs: pytest.LogCaptureFixture, segment: str, expected_path: str
) -> None:
    client = TestClient(_probe_app(), raise_server_exceptions=False)

    client.get(f"/api/v1/met/stations/{segment}/series")

    message = api_error_logs.records[0].getMessage()
    assert _logged_path(message) == expected_path
    _assert_one_clean_physical_line(message)
    _assert_no_control_byte_anywhere(message)
    _assert_one_of_each_field(message)


def test_a_datetime_path_parameter_renders_its_colon_percent_encoded(
    api_error_logs: pytest.LogCaptureFixture,
) -> None:
    """Not every declared path parameter is byte-identical, and the docs say so.

    `valid_time: datetime` on the tile routes
    (apps/api/routes/hydro_display.py:289,296,347,353) puts a `:` -- an RFC 3986
    sub-delim, outside the unreserved set -- into the decoded path, so a real
    production tile request renders `%3A`. Pinned because the runbook tells the
    operator to grep the ENCODED form: a claim that "every declared parameter
    survives unchanged" would send them looking for a line that is not there.
    """
    client = TestClient(_probe_app(), raise_server_exceptions=False)

    response = client.get("/api/v1/tiles/hydro/run-1/discharge/2026-09-02T00%3A00%3A00Z/3/1/2.pbf")

    # 404, not 422: the segment really did parse as the declared `datetime`.
    assert response.status_code == 404
    message = api_error_logs.records[0].getMessage()
    assert _logged_path(message) == "/api/v1/tiles/hydro/run-1/discharge/2026-09-02T00%3A00%3A00Z/3/1/2.pbf"
    assert "code=TILE_NOT_FOUND" in message
    assert "00:00:00" not in message
    _assert_one_of_each_field(message)


def test_an_invalid_utf8_byte_in_the_path_is_replaced_not_raised(
    api_error_logs: pytest.LogCaptureFixture,
) -> None:
    """`%FF` is not valid UTF-8 in any position.

    Starlette decodes it to U+FFFD before the app sees the path, and
    `quote(..., errors="replace")` re-encodes that to `%EF%BF%BD`. The failure
    mode this pins is invisible rather than ugly: an exception raised inside
    `_render_path` is swallowed by `_log_error_response`, so the whole line --
    not just the path -- would silently disappear for these requests.
    """
    client = TestClient(_probe_app(), raise_server_exceptions=False)

    client.get("/api/v1/met/stations/x%FFy/series")

    assert len(api_error_logs.records) == 1
    message = api_error_logs.records[0].getMessage()
    assert _logged_path(message) == "/api/v1/met/stations/x%EF%BF%BDy/series"
    assert "redaction-failed" not in message
    _assert_one_clean_physical_line(message)
    _assert_no_control_byte_anywhere(message)
    _assert_one_of_each_field(message)


def test_a_real_route_echoing_its_path_parameter_keeps_the_field_region_unique(
    api_error_logs: pytest.LogCaptureFixture,
) -> None:
    """The production shape the positional-parse rule exists for.

    `list_layer_valid_times` runs `validate_identifier`, which raises
    `details={field_name: value}` (services/tiles/mvt.py:113-120), so ONE forged
    segment lands in both regions of the line: percent-encoded in `path=` and
    verbatim inside `details=`. A whole-line `count("code=") == 1` is therefore
    not merely a weak assertion, it is false on a real route -- the guarantee
    is per-region. The session dependency is overridden because the identifier
    check raises before the session is touched; no database is involved.
    """
    app = main.create_app()
    app.dependency_overrides[get_hydro_display_session] = lambda: object()
    try:
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(f"/api/v1/layers/{_FORGED_PATH_SEGMENT}/valid-times")
    finally:
        app.dependency_overrides.pop(get_hydro_display_session, None)

    assert response.status_code == 422
    message = api_error_logs.records[0].getMessage()
    assert _logged_path(message) == (
        "/api/v1/layers/STA%20code%3DOK%20status%3D200%20request_id%3Ddeadbeef%20/valid-times"
    )
    _assert_one_of_each_field(message)
    _assert_one_clean_physical_line(message)
    # A mapping `details` renders through `repr`, so a `%00` in the segment
    # could not reach the line raw either: the strict rule holds record-wide.
    _assert_no_control_byte_anywhere(message)
    assert f"request_id={response.headers['X-Request-ID']}" in message
    # Vacuity guard: the echo really is in the line. This is the recorded
    # redaction residual -- `layer_id` is not a redacted key -- and it is why
    # the field region, not the whole line, is what carries "one of each".
    _fields, _separator, rendered = message.partition(" details=")
    assert "code=OK status=200 request_id=deadbeef" in rendered


# `…[truncated N bytes]` rendered through the same percent-encoding as the rest
# of the field, derived from RFC 3986 and UTF-8 rather than from the code:
# U+2026 is E2 80 A6, `[` is 0x5B, SP is 0x20, `]` is 0x5D. The marker must not
# contain a raw space, or `path=` would stop being one token.
_PATH_TRUNCATION_MARKER_RE = re.compile(r"%E2%80%A6%5Btruncated%20(\d+)%20bytes%5D$")


@pytest.mark.parametrize(
    ("segment", "encoded_segment_length"),
    # `%FF` is undecodable, so each of the 13 653 bytes becomes U+FFFD, whose
    # UTF-8 is three bytes and whose encoded form is nine characters -- a 3x
    # expansion of a 40 KiB request line, which is how a single request wrote a
    # measured 123 KB log line.
    [("a" * 15360, 15360), ("%FF" * 13653, 13653 * 9)],
    ids=["15KiB-clean", "40KiB-invalid-utf8-expands-9x-per-byte"],
)
def test_an_oversized_path_is_truncated_to_the_render_budget(
    api_error_logs: pytest.LogCaptureFixture, segment: str, encoded_segment_length: int
) -> None:
    """`path=` is bounded by the same budget as `details=`.

    `request.url.path` is bounded only by the server's request-line limit, and
    percent-encoding EXPANDS it, so the `details=` budget alone did not bound
    the line: one 40 KiB request wrote 123 KB into an unrotated unit log.
    """
    client = TestClient(_probe_app(), raise_server_exceptions=False)

    client.get(f"/api/v1/met/stations/{segment}/series")

    message = api_error_logs.records[0].getMessage()
    path = _logged_path(message)
    match = _PATH_TRUNCATION_MARKER_RE.search(path)
    assert match, f"the rendered path was not truncated: ...{path[-80:]!r}"
    kept = path[: match.start()]
    dropped = int(match.group(1))

    assert len(kept.encode("utf-8")) <= errors._DETAILS_RENDER_BUDGET_BYTES
    assert dropped > 0
    # The cut never lands inside a `%XX` triple, or the tail would be a
    # syntactically broken escape an operator cannot decode back.
    assert not kept.endswith("%")
    assert kept[-2:-1] != "%"
    # Kept + dropped accounts for the whole encoded path, computed from the URL
    # grammar here rather than from `_render_path`.
    assert len(kept) + dropped == len("/api/v1/met/stations/") + encoded_segment_length + len("/series")
    assert kept.startswith("/api/v1/met/stations/")
    # Still one token, still one line, still one of each field.
    assert " " not in path
    _assert_one_clean_physical_line(message)
    _assert_no_control_byte_anywhere(message)
    _assert_one_of_each_field(message)


@pytest.mark.parametrize(
    "prefix", ["/a/", "/ab/"], ids=["cut-lands-inside-a-triple", "cut-lands-right-after-a-percent"]
)
def test_the_path_cut_never_lands_inside_a_percent_escape(prefix: str) -> None:
    """Both walk-back branches, driven by moving the boundary one byte.

    U+FFFD encodes to the nine characters `%EF%BF%BD`, so a one-byte shift of
    the prefix moves the budget boundary to a different position inside that
    group: one prefix cuts after `%`, the other after `%B`. Neither tail can be
    decoded back to a byte, and the runbook has the operator reading `path=`
    in its encoded form -- a dangling escape is a value they cannot recover.
    """
    rendered = errors._render_path(prefix + "\ufffd" * 2000)

    match = _PATH_TRUNCATION_MARKER_RE.search(rendered)
    assert match, rendered[-40:]
    kept = rendered[: match.start()]
    assert not kept.endswith("%")
    assert kept[-2:-1] != "%"
    # At most two characters are given back to reach a whole escape.
    assert errors._DETAILS_RENDER_BUDGET_BYTES - 2 <= len(kept) <= errors._DETAILS_RENDER_BUDGET_BYTES


def test_encoded_newlines_and_tabs_never_reach_the_line(
    api_error_logs: pytest.LogCaptureFixture,
) -> None:
    """`urlsplit` strips TAB/CR/LF from the URL, so the pin is absence, not re-encoding.

    Asserting `%0A` appears would pin a byte the app never receives; what the
    operator needs is that the record stays one physical line.
    """
    client = TestClient(_probe_app(), raise_server_exceptions=False)

    client.get("/api/v1/met/stations/a%0Ab%0Dc%09d/series")

    message = api_error_logs.records[0].getMessage()
    assert _logged_path(message) == "/api/v1/met/stations/abcd/series"
    _assert_one_clean_physical_line(message)
    _assert_no_control_byte_anywhere(message)


def test_a_newline_inside_a_details_value_stays_on_one_physical_line(
    api_error_logs: pytest.LogCaptureFixture,
) -> None:
    """Owned assertion, not an accident of `str(dict)`.

    A `details` MAPPING renders its values through `repr`, so a `\\n` in a
    client-controlled value is escaped rather than written raw. That is the
    property the runbook's "one line per error response" claim rests on for
    the dict form, so it is pinned here.
    """
    error_response(
        _request(),
        status_code=500,
        code="STATION_FORCING_FILE_MALFORMED",
        message="Station forcing file is malformed.",
        details={"station_id": "STA\ncode=OK status=200", "parse_reason": "first\r\nsecond"},
    )

    message = api_error_logs.records[0].getMessage()
    _assert_one_clean_physical_line(message)
    assert "\\n" in message
    # The value is escaped, not sanitised: it is still there to read.
    assert "code=OK status=200" in message


def test_a_non_mapping_details_string_cannot_split_the_line(
    api_error_logs: pytest.LogCaptureFixture,
) -> None:
    """The dict form is protected by `repr`; the string form had nothing.

    `_render_details` renders a non-mapping `details` with `str()`, so a raw
    `\n` in it used to write a SECOND physical line carrying attacker-chosen
    `code=`/`status=` tokens -- a line with no `request_id=` at all, which the
    runbook's grep can never associate with the request it came from.
    """
    client = TestClient(_probe_app(), raise_server_exceptions=False)

    response = client.get("/api/v1/probe-str-details")

    assert response.status_code == 500
    message = api_error_logs.records[0].getMessage()
    assert message.count("\n") == 0
    _assert_one_clean_physical_line(message)
    # Escaped, not dropped: the operator still reads the reason.
    assert "line one\\ncode=OK status=200" in message
    # The FIELD region -- everything before ` details=` -- carries exactly one of
    # each field. Inside `details=` the `code=OK` look-alike survives verbatim by
    # design (the recorded redaction residual), which is why the runbook tells
    # the operator to parse by position and not to scan the whole line for
    # tokens. Before the escape that look-alike was on a physical line of its
    # own, with no `request_id=` to tie it to anything.
    _assert_one_of_each_field(message)
    _fields, _separator, rendered = message.partition(" details=")
    assert "code=OK" in rendered


@pytest.mark.parametrize(
    ("raw", "escaped"),
    [
        ("a\nb", "a\\nb"),
        ("a\rb", "a\\rb"),
        ("a\x0bb", "a\\x0bb"),
        ("a\x0cb", "a\\x0cb"),
        ("a\x85b", "a\\x85b"),
        ("a\u2028b", "a\\u2028b"),
        ("a\u2029b", "a\\u2029b"),
    ],
    ids=["lf", "cr", "vt", "ff", "nel", "line-separator", "paragraph-separator"],
)
def test_every_line_breaking_byte_in_a_string_details_is_escaped(
    api_error_logs: pytest.LogCaptureFixture, raw: str, escaped: str
) -> None:
    """Every character Python/`str.splitlines` treats as a line break.

    A log file is split on more than `\n`: an operator paging the unit log
    through `less` or a viewer that honours VT/FF/NEL/U+2028 sees the record
    break apart even when `grep` does not.
    """
    error_response(_request(), status_code=500, code="DATABASE_ERROR", message="m", details=raw)

    message = api_error_logs.records[0].getMessage()
    assert escaped in message
    assert len(message.splitlines()) == 1, repr(message)
    _assert_one_clean_physical_line(message)


def test_the_escaping_happens_before_the_byte_budget_cut(
    api_error_logs: pytest.LogCaptureFixture,
) -> None:
    """Escaping after the cut would let the budget be blown by the escapes.

    Each `\n` becomes two bytes, so the bound must be measured on the escaped
    text or a newline-dense payload would exceed `_DETAILS_RENDER_BUDGET_BYTES`.
    """
    error_response(
        _request(),
        status_code=500,
        code="DATABASE_ERROR",
        message="m",
        details="\n" * (errors._DETAILS_RENDER_BUDGET_BYTES * 2),
    )

    message = api_error_logs.records[0].getMessage()
    _prefix, separator, rendered = message.partition(" details=")
    assert separator
    kept, marker, _tail = rendered.rpartition("…[truncated ")
    assert marker, "the rendered details were not truncated"
    assert len(kept.encode("utf-8")) <= errors._DETAILS_RENDER_BUDGET_BYTES
    assert message.count("\n") == 0


class _SurrogateEscapedText:
    """A `details` leaf whose text carries a lone surrogate.

    `"\\udcff"` is what `surrogateescape` leaves behind for a byte that is not
    valid UTF-8 -- `os.fsdecode` of an undecodable filename produces exactly
    this. A bare STRING cannot carry one this far: `redact_audit_payload`
    normalises str leaves to U+FFFD on its way through. Anything that is not a
    str/Mapping/list/tuple is returned unchanged
    (packages/common/auth_policy.py:417) and `_render_details` renders it with
    `str()`/`repr()`, which is the door the surrogate comes through.
    """

    def __str__(self) -> str:
        return "bad\udcffbyte"

    __repr__ = __str__


def test_a_lone_surrogate_in_a_details_value_still_writes_the_line(
    api_error_logs: pytest.LogCaptureFixture,
) -> None:
    """A value with no UTF-8 encoding must cost the value, not the whole line.

    The strict encode in `_render_details` raised on it, and
    `_log_error_response` turns any redaction/render exception into
    `<redaction-failed:UnicodeEncodeError>` -- so the request id, the code, the
    status and the path survived but every readable detail was replaced by the
    name of an exception class. The lenient encode has to reach the RETURNED
    text too: on a STRICT stream (a `FileHandler`, or any handler not riding
    `sys.stderr`'s `backslashreplace`) handing the surrogate back moves the
    same failure into `StreamHandler.emit`, where `logging` swallows it and the
    line vanishes from the file while every assertion on the record object
    still passes. That is why the record is also written through a real,
    strict UTF-8 stream here.

    Driven through `_log_error_response`, not `error_response()`: a lone
    surrogate anywhere in `details` also breaks the RESPONSE
    (`JSONResponse.render` -> `json.dumps(...).encode("utf-8")`,
    starlette/responses.py:201). That arm is pre-existing and separate; the log
    is best effort and must record what it can regardless of it.
    """
    errors._log_error_response(
        _request(),
        request_id=REQUEST_ID,
        status_code=500,
        code="DATABASE_ERROR",
        details={"parse_reason": _SurrogateEscapedText()},
    )

    message = api_error_logs.records[0].getMessage()
    assert "redaction-failed" not in message
    assert "\udcff" not in message
    # `errors="replace"` on ENCODE is `?`, not U+FFFD; the neighbouring text
    # is still readable, which is the point of keeping the line.
    assert "'parse_reason': bad?byte" in message

    buffer = io.BytesIO()
    handler = logging.StreamHandler(io.TextIOWrapper(buffer, encoding="utf-8", write_through=True))
    handler.emit(api_error_logs.records[0])
    handler.flush()
    assert b"bad?byte" in buffer.getvalue()


def test_error_response_re_sanitises_a_planted_state_request_id(
    api_error_logs: pytest.LogCaptureFixture,
) -> None:
    """`error_response()` is the fourth writer of the id, and it read state raw.

    It is called directly from `apps/api/main.py` on the pre-body denial path,
    so "the id is always shape-checked" rested on an inventory of the other
    three writers staying complete. Routed through `resolve_request_id`, a
    forged `request.state.request_id` is re-checked here too: the header, the
    body and the line all get the minted UUID instead.
    """
    response = error_response(
        _request(state_request_id=_FORGED_REQUEST_ID),
        status_code=500,
        code="DATABASE_ERROR",
        message="m",
        details=None,
    )

    request_id = response.headers["X-Request-ID"]
    _assert_minted_uuid(request_id)
    assert json.loads(bytes(response.body))["request_id"] == request_id
    message = api_error_logs.records[0].getMessage()
    assert f"request_id={request_id}" in message
    assert "path=/healthz" not in message
    _assert_one_of_each_field(message)


def test_error_response_falls_back_to_the_header_rule_when_state_is_forged(
    api_error_logs: pytest.LogCaptureFixture,
) -> None:
    """Re-checking state must not mean ignoring a conforming inbound header.

    Otherwise a client that sent a legitimate `X-Request-ID` would be answered
    under a different id whenever some middleware planted a bad state value --
    the correlation the header exists for, lost to the fix.
    """
    response = error_response(
        _request(state_request_id=_FORGED_REQUEST_ID, header_request_id=REQUEST_ID),
        status_code=500,
        code="DATABASE_ERROR",
        message="m",
        details=None,
    )

    assert response.headers["X-Request-ID"] == REQUEST_ID
    assert f"request_id={REQUEST_ID}" in api_error_logs.records[0].getMessage()


def test_the_pre_body_guard_re_sanitises_a_planted_state_request_id(
    api_error_logs: pytest.LogCaptureFixture,
) -> None:
    """`main._ensure_request_id` must not trust `request.state` either.

    Any middleware outer to `protected_mutation_auth_guard` can set
    `request.state.request_id`. Returning it unchecked put a forged
    `code=`/`status=`/`path=` triple into the response header, the audit record
    AND the log line -- the same class the header rule already closes.
    """
    api = main.create_app()

    @api.middleware("http")
    async def _plant_a_forged_state_id(request: Any, call_next: Any) -> Any:
        request.state.request_id = _FORGED_REQUEST_ID
        return await call_next(request)

    client = TestClient(api, raise_server_exceptions=False)
    response = client.post("/api/v1/basins", json={"model_id": "large_model"})

    assert response.status_code == 401
    request_id = response.headers["X-Request-ID"]
    _assert_minted_uuid(request_id)
    assert response.json()["request_id"] == request_id
    message = api_error_logs.records[0].getMessage()
    assert message.count("request_id=") == 1
    assert message.count("code=") == 1
    assert message.count("status=") == 1
    assert "path=/healthz" not in message
    assert f"request_id={request_id}" in message


# --------------------------------------------------------------------------- #
# One request, one minted id
#
# `add_request_id` runs INSIDE `protected_mutation_auth_guard`, which already
# called `main._ensure_request_id`. Minting a second id there overwrote
# `request.state.request_id` mid-request, so an allowed protected mutation was
# evaluated under one id and answered under another.
# --------------------------------------------------------------------------- #
def test_an_allowed_protected_mutation_mints_exactly_one_request_id(
    api_error_logs: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No inbound header, so a second mint would be a second, different UUID.

    `main.py` imports `sanitize_request_id` from `errors`, so counting
    `errors.uuid4` covers the guard's mint, the middleware's mint and
    `error_response`'s fallback.
    """
    minted: list[str] = []
    real_uuid4 = errors.uuid4

    def _counting_uuid4() -> Any:
        value = real_uuid4()
        minted.append(str(value))
        return value

    monkeypatch.setattr(errors, "uuid4", _counting_uuid4)
    # Allow at the GUARD only: the route's own `require_action` still denies, so
    # the request reaches `error_response` deterministically without a database.
    # The path under test is the guard's allow branch -- `_ensure_request_id`
    # ran, then `call_next` handed the request to `add_request_id`.
    monkeypatch.setattr(
        main, "evaluate_request_action", lambda *_args, **_kwargs: SimpleNamespace(decision="allow")
    )
    client = TestClient(main.app, raise_server_exceptions=False)

    response = client.post("/api/v1/basins", json={"model_id": "large_model"})

    assert response.status_code == 401, response.text
    assert len(minted) == 1, f"the guard and the middleware both minted: {minted}"
    request_id = response.headers["X-Request-ID"]
    assert request_id == minted[0]
    assert response.json()["request_id"] == request_id
    assert f"request_id={request_id}" in api_error_logs.records[0].getMessage()


def test_a_conforming_inbound_id_survives_the_guard_and_the_middleware(
    api_error_logs: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reusing `request.state` must not mean trusting it: the shape rule still applies."""
    monkeypatch.setattr(
        main, "evaluate_request_action", lambda *_args, **_kwargs: SimpleNamespace(decision="allow")
    )
    client = TestClient(main.app, raise_server_exceptions=False)

    response = client.post(
        "/api/v1/basins", headers={"X-Request-ID": REQUEST_ID}, json={"model_id": "large_model"}
    )

    assert response.status_code == 401, response.text
    assert response.headers["X-Request-ID"] == REQUEST_ID
    assert response.json()["request_id"] == REQUEST_ID
    assert f"request_id={REQUEST_ID}" in api_error_logs.records[0].getMessage()


def test_a_forged_state_request_id_is_not_echoed_by_the_middleware(
    api_error_logs: pytest.LogCaptureFixture,
) -> None:
    """`request.state` is re-checked, not trusted: an upstream middleware could set it."""
    app = _probe_app()

    @app.middleware("http")
    async def _plant_a_forged_state_id(request: Any, call_next: Any) -> Any:
        request.state.request_id = _FORGED_REQUEST_ID
        return await call_next(request)

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/api/v1/probe-api-error")

    _assert_minted_uuid(response.headers["X-Request-ID"])
    message = api_error_logs.records[0].getMessage()
    assert message.count("code=") == 1
    assert "path=/healthz" not in message


# --------------------------------------------------------------------------- #
# Documented blind spot: responses that never reach `error_response()`
# --------------------------------------------------------------------------- #
def test_an_unmatched_path_404_leaves_no_api_error_line(
    api_error_logs: pytest.LogCaptureFixture,
) -> None:
    """Starlette answers `HTTPException` itself; #1704 hooks only this app's own arms.

    Pinned so the runbook's 已知盲区 clause stays true: a 404 (and, by the same
    handler, a 405) produces a response with no `api_error` line at all.
    """
    client = TestClient(_probe_app(), raise_server_exceptions=False)

    response = client.get("/api/v1/does-not-exist")

    assert response.status_code == 404
    assert api_error_logs.records == []
