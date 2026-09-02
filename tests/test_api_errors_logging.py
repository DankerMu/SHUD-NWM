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
from collections.abc import Mapping
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
