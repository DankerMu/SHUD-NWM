"""Bounded uncredentialed loopback GET for PGDATA forecast-series samples."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from packages.common.evidence_io import BoundedEvidenceError, validate_json_complexity
from packages.common.node27_pgdata_workload_types import PgdataWorkloadError, refuse

JSON_MAX_DEPTH = 16
JSON_MAX_NODES = 4096
JSON_MAX_ARRAY_ITEMS = 512
JSON_MAX_OBJECT_WIDTH = 32
ACCEPT_ENCODING = "identity"
HTTP_TIMEOUT_SECONDS = 5
HTTP_BODY_LIMIT_BYTES = 65536


class NoRedirect(HTTPRedirectHandler):
    """Refuse every HTTP redirect instead of following Location."""

    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        del req, fp, msg, headers, newurl
        refuse(f"API response redirected ({code})", code="API_REDIRECT", stage="performance")


def default_opener() -> Any:
    return build_opener(ProxyHandler({}), NoRedirect())


def bound_response_json(payload: Any, *, code: str, stage: str) -> Any:
    try:
        validate_json_complexity(
            payload,
            label="API JSON",
            max_depth=JSON_MAX_DEPTH,
            max_nodes=JSON_MAX_NODES,
            max_array_items=JSON_MAX_ARRAY_ITEMS,
            max_object_items=JSON_MAX_OBJECT_WIDTH,
        )
    except BoundedEvidenceError:
        refuse("API JSON exceeds complexity bounds", code=code, stage=stage)
    return payload


def _header(response: Any, name: str) -> str:
    headers = getattr(response, "headers", None)
    if headers is None:
        getter = getattr(response, "getheader", None)
        if callable(getter):
            return str(getter(name) or "").strip()
        return ""
    if isinstance(headers, Mapping):
        return str(headers.get(name) or headers.get(name.lower()) or "").strip()
    getter = getattr(headers, "get", None)
    if callable(getter):
        return str(getter(name) or getter(name.lower()) or "").strip()
    return ""


def _refuse_credentials(request: Request) -> None:
    for header in ("Authorization", "Cookie", "Proxy-Authorization"):
        if request.has_header(header):
            refuse("API request carries credentials", code="API_CREDENTIALS", stage="performance")


def build_get_request(url: str) -> Request:
    request = Request(url, method="GET")
    request.add_header("Accept-Encoding", ACCEPT_ENCODING)
    _refuse_credentials(request)
    return request


def assert_uncredentialed_get(request: Request) -> None:
    if request.get_method() != "GET":
        refuse("API request is not GET", code="API_METHOD_INVALID", stage="performance")
    encoding = request.get_header("Accept-encoding") or request.get_header("Accept-Encoding")
    if str(encoding or "").strip().lower() != ACCEPT_ENCODING:
        refuse("API request Accept-Encoding is not identity", code="API_ACCEPT_ENCODING", stage="performance")
    _refuse_credentials(request)


def _status(response: Any) -> int:
    raw = getattr(response, "status", None)
    if raw is None:
        raw = getattr(response, "code", 0)
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def _content_encoding(response: Any) -> str:
    return _header(response, "Content-Encoding").lower()


def read_bounded_json_body(
    response: Any,
    *,
    body_limit: int,
    stage: str,
) -> tuple[int, bytes, Any]:
    status = _status(response)
    if status < 200 or status > 299:
        refuse("API response is not 2xx", code="API_STATUS_INVALID", stage=stage)
    encoding = _content_encoding(response)
    if encoding and encoding != ACCEPT_ENCODING:
        refuse("API Content-Encoding is not identity", code="API_ENCODING_INVALID", stage=stage)
    try:
        body = response.read(body_limit + 1)
    except Exception:
        refuse("API body could not be read to completion", code="API_BODY_READ_FAILED", stage=stage)
    if len(body) > body_limit:
        refuse("API sample body exceeds the bounded limit", code="API_BODY_LIMIT", stage=stage)
    try:
        extra = response.read(1)
    except Exception:
        refuse("API body did not reach EOF", code="API_BODY_INCOMPLETE", stage=stage)
    if extra:
        refuse("API sample body exceeds the bounded limit", code="API_BODY_LIMIT", stage=stage)
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        refuse("API body is not JSON", code="API_BODY_INVALID", stage=stage)
    bound_response_json(payload, code="API_JSON_TOO_COMPLEX", stage=stage)
    return status, body, payload


def open_local_get(
    *,
    url: str,
    opener: Any | None,
    timeout_seconds: int,
    stage: str,
) -> tuple[Request, Any]:
    request = build_get_request(url)
    assert_uncredentialed_get(request)
    handle = opener if opener is not None else default_opener()
    try:
        response = handle.open(request, timeout=timeout_seconds)
    except PgdataWorkloadError:
        raise
    except HTTPError as error:
        code = int(getattr(error, "code", 0) or 0)
        if 300 <= code < 400:
            refuse("API response redirected", code="API_REDIRECT", stage=stage)
        refuse("API response is not 2xx", code="API_STATUS_INVALID", stage=stage)
    except URLError:
        refuse("API request failed", code="API_REQUEST_FAILED", stage=stage)
    except Exception as error:
        if isinstance(error, PgdataWorkloadError):
            raise
        refuse("API request failed", code="API_REQUEST_FAILED", stage=stage)
    return request, response


def close_response(response: Any) -> None:
    closer = getattr(response, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:
            pass
