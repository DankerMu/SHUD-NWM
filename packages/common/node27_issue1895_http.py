"""Bounded uncredentialed loopback GET for identity-only and forecast-series."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from packages.common.evidence_io import BoundedEvidenceError, validate_json_complexity
from packages.common.node27_issue1895_types import Issue1895ReadinessError

JSON_MAX_DEPTH = 16
JSON_MAX_NODES = 4096
JSON_MAX_ARRAY_ITEMS = 512
JSON_MAX_OBJECT_WIDTH = 32
ACCEPT_ENCODING = "identity"


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
        raise Issue1895ReadinessError(
            f"API response redirected ({code})",
            code="API_REDIRECT",
            stage="performance",
        )


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
        )
    except BoundedEvidenceError:
        raise Issue1895ReadinessError(
            "API JSON exceeds complexity bounds",
            code=code,
            stage=stage,
        ) from None
    stack: list[Any] = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            if len(current) > JSON_MAX_OBJECT_WIDTH:
                raise Issue1895ReadinessError(
                    "API JSON exceeds object width bounds",
                    code=code,
                    stage=stage,
                )
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
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
            raise Issue1895ReadinessError(
                "API request carries credentials",
                code="API_CREDENTIALS",
                stage="performance",
            )


def build_get_request(url: str) -> Request:
    request = Request(url, method="GET")
    request.add_header("Accept-Encoding", ACCEPT_ENCODING)
    _refuse_credentials(request)
    return request


def assert_uncredentialed_get(request: Request) -> None:
    if request.get_method() != "GET":
        raise Issue1895ReadinessError(
            "API request is not GET",
            code="API_METHOD_INVALID",
            stage="performance",
        )
    encoding = request.get_header("Accept-encoding") or request.get_header("Accept-Encoding")
    if str(encoding or "").strip().lower() != ACCEPT_ENCODING:
        raise Issue1895ReadinessError(
            "API request Accept-Encoding is not identity",
            code="API_ACCEPT_ENCODING",
            stage="performance",
        )
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
        raise Issue1895ReadinessError(
            "API response is not 2xx",
            code="API_STATUS_INVALID",
            stage=stage,
        )
    encoding = _content_encoding(response)
    if encoding and encoding != ACCEPT_ENCODING:
        raise Issue1895ReadinessError(
            "API Content-Encoding is not identity",
            code="API_ENCODING_INVALID",
            stage=stage,
        )
    try:
        body = response.read(body_limit + 1)
    except Exception:
        raise Issue1895ReadinessError(
            "API body could not be read to completion",
            code="API_BODY_READ_FAILED",
            stage=stage,
        ) from None
    if len(body) > body_limit:
        raise Issue1895ReadinessError(
            "API sample body exceeds the bounded limit",
            code="API_BODY_LIMIT",
            stage=stage,
        )
    try:
        extra = response.read(1)
    except Exception:
        raise Issue1895ReadinessError(
            "API body did not reach EOF",
            code="API_BODY_INCOMPLETE",
            stage=stage,
        ) from None
    if extra:
        raise Issue1895ReadinessError(
            "API sample body exceeds the bounded limit",
            code="API_BODY_LIMIT",
            stage=stage,
        )
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise Issue1895ReadinessError(
            "API body is not JSON",
            code="API_BODY_INVALID",
            stage=stage,
        ) from None
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
    except Issue1895ReadinessError:
        raise
    except HTTPError as error:
        code = int(getattr(error, "code", 0) or 0)
        if 300 <= code < 400:
            raise Issue1895ReadinessError(
                "API response redirected",
                code="API_REDIRECT",
                stage=stage,
            ) from None
        raise Issue1895ReadinessError(
            "API response is not 2xx",
            code="API_STATUS_INVALID",
            stage=stage,
        ) from None
    except URLError:
        raise Issue1895ReadinessError(
            "API request failed",
            code="API_REQUEST_FAILED",
            stage=stage,
        ) from None
    except Exception as error:
        if isinstance(error, Issue1895ReadinessError):
            raise
        raise Issue1895ReadinessError(
            "API request failed",
            code="API_REQUEST_FAILED",
            stage=stage,
        ) from None
    return request, response


def read_bounded_expected_status_body(
    response: Any,
    *,
    expected_status: int,
    body_limit: int,
    stage: str,
) -> tuple[int, bytes]:
    """Read an expected non-2xx response without weakening the 2xx JSON reader.

    Denial routes such as display's Slurm health endpoint must prove their exact
    status with the same no-proxy/no-redirect/identity-encoding and bounded-body
    rules as ordinary API evidence.  Callers that require JSON 2xx remain on
    ``read_bounded_json_body``.
    """

    status = _status(response)
    if status != expected_status:
        raise Issue1895ReadinessError(
            "API response status differs from the required denial status",
            code="API_EXPECTED_STATUS_INVALID",
            stage=stage,
        )
    encoding = _content_encoding(response)
    if encoding and encoding != ACCEPT_ENCODING:
        raise Issue1895ReadinessError(
            "API Content-Encoding is not identity",
            code="API_ENCODING_INVALID",
            stage=stage,
        )
    try:
        body = response.read(body_limit + 1)
    except Exception:
        raise Issue1895ReadinessError(
            "API body could not be read to completion",
            code="API_BODY_READ_FAILED",
            stage=stage,
        ) from None
    if len(body) > body_limit:
        raise Issue1895ReadinessError(
            "API sample body exceeds the bounded limit",
            code="API_BODY_LIMIT",
            stage=stage,
        )
    try:
        extra = response.read(1)
    except Exception:
        raise Issue1895ReadinessError(
            "API body did not reach EOF",
            code="API_BODY_INCOMPLETE",
            stage=stage,
        ) from None
    if extra:
        raise Issue1895ReadinessError(
            "API sample body exceeds the bounded limit",
            code="API_BODY_LIMIT",
            stage=stage,
        )
    return status, body


def fetch_local_expected_status(
    *,
    url: str,
    expected_status: int,
    opener: Any | None,
    timeout_seconds: int,
    body_limit: int,
    stage: str,
) -> tuple[int, bytes]:
    """Issue one bounded uncredentialed GET and require one exact status.

    ``urllib`` raises ``HTTPError`` for a 404; the exception itself is the
    response object and is deliberately retained only long enough to prove its
    bounded identity-encoded body before it is closed.
    """

    request = build_get_request(url)
    assert_uncredentialed_get(request)
    handle = opener if opener is not None else default_opener()
    response: Any | None = None
    try:
        try:
            response = handle.open(request, timeout=timeout_seconds)
        except HTTPError as error:
            response = error
        except URLError:
            raise Issue1895ReadinessError(
                "API request failed",
                code="API_REQUEST_FAILED",
                stage=stage,
            ) from None
        except Exception as error:
            if isinstance(error, Issue1895ReadinessError):
                raise
            raise Issue1895ReadinessError(
                "API request failed",
                code="API_REQUEST_FAILED",
                stage=stage,
            ) from None
        status = _status(response)
        if 300 <= status < 400:
            raise Issue1895ReadinessError(
                "API response redirected",
                code="API_REDIRECT",
                stage=stage,
            )
        return read_bounded_expected_status_body(
            response,
            expected_status=expected_status,
            body_limit=body_limit,
            stage=stage,
        )
    finally:
        if response is not None:
            close_response(response)


def close_response(response: Any) -> None:
    closer = getattr(response, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:
            pass


__all__ = (
    "ACCEPT_ENCODING",
    "JSON_MAX_ARRAY_ITEMS",
    "JSON_MAX_DEPTH",
    "JSON_MAX_NODES",
    "JSON_MAX_OBJECT_WIDTH",
    "NoRedirect",
    "assert_uncredentialed_get",
    "bound_response_json",
    "build_get_request",
    "close_response",
    "default_opener",
    "fetch_local_expected_status",
    "open_local_get",
    "read_bounded_expected_status_body",
    "read_bounded_json_body",
)
