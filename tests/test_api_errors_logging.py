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
redacted before it is written: `redact_audit_payload` for value-shaped secrets
plus a key-level pass over `rejected_value`, whose value is raw client input of
any shape.
"""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from apps.api import errors, main
from apps.api.errors import ApiError, error_response, register_error_handlers
from packages.common.object_store_forcing import _station_csv_failure_reason
from packages.common.safe_fs import SafeFilesystemError

REQUEST_ID = "req-1704-abc"
MALFORMED_DETAILS: dict[str, Any] = {
    "station_id": "STA-0001",
    "expected_path": "/ghdc/data/nwm/forcing/STA-0001.csv",
    "parse_reason": "concurrent-replace: row count shrank mid-read",
}


def _request(path: str = "/api/v1/met/stations/STA-0001/series") -> Any:
    """The minimal Request surface `error_response()` actually touches."""
    return SimpleNamespace(state=SimpleNamespace(request_id=REQUEST_ID), url=SimpleNamespace(path=path))


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
    assert reason.startswith("concurrent-replace: ")

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
    api_logger = logging.getLogger(main.API_LOGGER_NAME)
    before = list(api_logger.handlers)
    try:
        main._install_api_log_handler()
        main._install_api_log_handler()
        stream_handlers = [h for h in api_logger.handlers if isinstance(h, logging.StreamHandler)]
        assert len(stream_handlers) == 1
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
