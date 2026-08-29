"""Deployment-side auth guard evidence for the Slurm gateway.

Covers issue #1684's deployment half: the process-level loopback bind guard
and the scheduler preflight's fail-closed missing-token gate.

Helpers/constants are imported from ``tests/test_slurm_gateway_auth.py``.
"""

from __future__ import annotations

import pytest

from tests.test_slurm_gateway_auth import (
    SERVICE_TOKEN,
)

# ---------------------------------------------------------------------------
# F6 - deployable loopback bind guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "host", "port", "valid"),
    [
        ("http://127.0.0.1:8090", None, None, True),
        ("http://127.0.0.1:8090", "127.0.0.1", 8090, True),
        ("http://127.0.0.1:8090", "127.0.0.2", None, True),
        ("http://[::1]:8090", None, None, True),
        ("http://0.0.0.0:8090", None, None, False),
        ("http://[::]:8090", None, None, False),
        ("http://0.0.0.0:8090", "127.0.0.1", None, True),
        ("http://localhost:8090", None, None, False),
        ("http://192.168.1.5:8090", None, None, False),
    ],
)
def test_bind_guard_resolves_loopback_only(monkeypatch, url, host, port, valid) -> None:
    from services.slurm_gateway.__main__ import _resolve_host_port, _validate_bind_host

    resolved_host, _resolved_port = _resolve_host_port(url)
    if host:
        resolved_host = host
    if valid:
        assert _validate_bind_host(resolved_host) is None
    else:
        with pytest.raises(SystemExit):
            _validate_bind_host(resolved_host)


def _fake_uvicorn_module(monkeypatch, called: dict) -> None:
    import sys

    class FakeUvicorn:
        @staticmethod
        def run(app, **kwargs):
            called["uvicorn"] += 1
            called["app"] = app
            called["kwargs"] = kwargs
            return None

    monkeypatch.setitem(sys.modules, "uvicorn", FakeUvicorn)


def test_bind_guard_rejects_non_loopback_before_uvicorn(monkeypatch) -> None:
    import builtins

    import services.slurm_gateway.__main__ as cli_module

    called = {"uvicorn": 0}
    _fake_uvicorn_module(monkeypatch, called)
    original_import = builtins.__import__

    def _record_import(name, *args, **kwargs):
        imported_names.append(name)
        return original_import(name, *args, **kwargs)

    imported_names: list[str] = []
    monkeypatch.setattr(builtins, "__import__", _record_import)
    with pytest.raises(SystemExit) as exc_info:
        cli_module.main(["--url", "http://0.0.0.0:8090"])
    assert exc_info.value.code == 2
    assert called["uvicorn"] == 0
    assert "uvicorn" not in imported_names, (
        "bind guard must reject before uvicorn is imported/started"
    )


def test_bind_guard_allows_loopback_then_starts_uvicorn(monkeypatch) -> None:
    import services.slurm_gateway.__main__ as cli_module

    called = {"uvicorn": 0}
    _fake_uvicorn_module(monkeypatch, called)
    cli_module.main(["--url", "http://127.0.0.1:8090"])
    assert called["uvicorn"] == 1


def test_uvicorn_run_pins_http_h11_and_preserves_host_port(monkeypatch) -> None:
    """The module entrypoint must deterministically pin the pure-Python h11
    protocol implementation.

    node-22's maintained active Python 3.12.7 environment has a live-proven
    broken optional native httptools (``AttributeError: module 'httptools.parser'
    has no attribute '__all__'``), and uvicorn's ``http="auto"`` default selects
    it, so a ``uvicorn.run(..., http="h11")`` keyword is the only deterministic
    control (``UVICORN_HTTP`` does not affect programmatic ``uvicorn.run``).
    Host/port derived from the URL and any CLI override must be forwarded
    unchanged, and the app must still be the gateway app.
    """
    import services.slurm_gateway.__main__ as cli_module

    called = {"uvicorn": 0}
    _fake_uvicorn_module(monkeypatch, called)
    cli_module.main(["--url", "http://127.0.0.1:8090"])
    assert called["uvicorn"] == 1
    assert called["kwargs"]["http"] == "h11"
    assert called["kwargs"]["host"] == "127.0.0.1"
    assert called["kwargs"]["port"] == 8090
    assert called["app"] is not None


def test_uvicorn_run_pins_http_h11_with_cli_overrides(monkeypatch) -> None:
    """CLI ``--host``/``--port`` overrides must still reach uvicorn.run with the
    h11 pin in place (override semantics must not be phased out by the pin)."""
    import services.slurm_gateway.__main__ as cli_module

    called = {"uvicorn": 0}
    _fake_uvicorn_module(monkeypatch, called)
    cli_module.main(
        ["--url", "http://127.0.0.1:8090", "--host", "127.0.0.2", "--port", "9090"]
    )
    assert called["kwargs"]["http"] == "h11"
    assert called["kwargs"]["host"] == "127.0.0.2"
    assert called["kwargs"]["port"] == 9090


# ---------------------------------------------------------------------------
# F7 - scheduler preflight fail-closed missing-token gate
# ---------------------------------------------------------------------------


def test_preflight_missing_token_fails_submit_ready_clearly(monkeypatch) -> None:
    # A live/real Slurm deployment without a usable service token must not be
    # reported submit-ready from anonymous health alone: the preflight fails
    # closed with a secret-safe reason.
    from services.orchestrator.scheduler import _default_gateway_probe

    class _Config:
        slurm_gateway_url = "http://gw-node22.internal:8081"

    monkeypatch.setenv("SLURM_GATEWAY_BACKEND", "real")
    monkeypatch.delenv("SLURM_GATEWAY_SERVICE_TOKEN", raising=False)
    result = _default_gateway_probe(_Config())
    assert result["healthy"] is False
    assert result["submit_capable"] is False
    assert "SLURM_GATEWAY_SERVICE_TOKEN" in result["reason"]
    assert "not configured" in result["reason"]


def test_preflight_non_ascii_token_is_unusable_and_fails_closed(monkeypatch) -> None:
    # A non-ASCII configured token is unusable configuration (the ASCII opaque
    # bearer-token contract). The preflight must NOT report submit-ready from
    # anonymous health with such a value, and must not leak it in the reason.
    from services.orchestrator.scheduler import _default_gateway_probe

    class _Config:
        slurm_gateway_url = "http://127.0.0.1:8090"

    monkeypatch.setenv("SLURM_GATEWAY_BACKEND", "real")
    monkeypatch.setenv("SLURM_GATEWAY_SERVICE_TOKEN", "tókén-0123456789abcdef")
    result = _default_gateway_probe(_Config())
    assert result["healthy"] is False
    assert result["submit_capable"] is False
    assert "SLURM_GATEWAY_SERVICE_TOKEN" in result["reason"]
    assert "not configured" in result["reason"]
    assert "tókén" not in str(result)


def test_preflight_token_present_probes_health(monkeypatch) -> None:
    import httpx

    from services.orchestrator.scheduler import _default_gateway_probe

    class _FakeHttpResponse:
        status_code = 200

        def json(self) -> dict:
            return {
                "backend": "slurm",
                "version": "24.05",
                "status": "healthy",
                "healthy": True,
                "binaries": {
                    name: {"resolved": True, "executable": True}
                    for name in ("sbatch", "squeue", "sacct", "scancel")
                },
            }

    class _FakeHttpClient:
        def __init__(self, *args, **kwargs) -> None:  # noqa: D401
            del args, kwargs

        def __enter__(self) -> _FakeHttpClient:
            return self

        def __exit__(self, *args) -> None:
            del args

        def get(self, url: str) -> _FakeHttpResponse:
            return _FakeHttpResponse()

    class _Config:
        slurm_gateway_url = "http://gw-node22.internal:8081"

    monkeypatch.setenv("SLURM_GATEWAY_BACKEND", "real")
    monkeypatch.setenv("SLURM_GATEWAY_SERVICE_TOKEN", SERVICE_TOKEN)
    monkeypatch.setattr(httpx, "Client", _FakeHttpClient)
    result = _default_gateway_probe(_Config())
    assert result["healthy"] is True
    assert result["submit_capable"] is True
