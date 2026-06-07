from __future__ import annotations

from typing import Any

import pytest

from packages.common.forecast_store import QHH_LATEST_READY_RUN_STATUSES
from packages.common.model_registry import PsycopgModelRegistryStore

# Full registry: one basin with a ready (published) run, one with only a
# non-ready (downloading) run, one with no runs at all.
_ALL_BASINS = [
    {
        "basin_id": "basins_qhh",
        "basin_name": "QHH",
        "basin_group": "demo",
        "description": None,
        "created_at": "2026-05-14T00:00:00Z",
    },
    {
        "basin_id": "basins_downloading",
        "basin_name": "Downloading",
        "basin_group": "demo",
        "description": None,
        "created_at": "2026-05-14T00:00:00Z",
    },
    {
        "basin_id": "basins_empty",
        "basin_name": "Empty",
        "basin_group": "demo",
        "description": None,
        "created_at": "2026-05-14T00:00:00Z",
    },
]

# Basins that have at least one run in a display-ready status.
_READY_BASINS = [_ALL_BASINS[0]]


class _FakeCursor:
    """Mimics the registry DB cursor for both filtered and unfiltered queries."""

    def __init__(self, captured: list[dict[str, Any]]) -> None:
        self._result: list[dict[str, Any]] = []
        self._captured = captured

    def execute(self, statement: str, parameters: tuple[Any, ...]) -> None:
        self._captured.append({"sql": statement, "params": parameters})
        if "EXISTS" in statement and "hydro.hydro_run" in statement:
            self._result = list(_READY_BASINS)
        else:
            self._result = list(_ALL_BASINS)

    def fetchall(self) -> list[dict[str, Any]]:
        return self._result


def _install_fake_store(monkeypatch: pytest.MonkeyPatch) -> tuple[PsycopgModelRegistryStore, list[dict[str, Any]]]:
    captured: list[dict[str, Any]] = []

    class _FakeTransaction:
        def __enter__(self) -> _FakeCursor:
            return _FakeCursor(captured)

        def __exit__(self, *_args: object) -> bool:
            return False

    monkeypatch.setattr(PsycopgModelRegistryStore, "_transaction", lambda _self: _FakeTransaction())
    return PsycopgModelRegistryStore("postgresql://example"), captured


def test_has_display_product_true_returns_only_basins_with_ready_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, captured = _install_fake_store(monkeypatch)

    basins = store.list_basins(limit=200, offset=0, has_display_product=True)

    ids = {b["basin_id"] for b in basins}
    assert ids == {"basins_qhh"}
    assert "basins_empty" not in ids
    # ready filter must be parameterised with the shared status set.
    stmt = captured[0]
    assert "EXISTS" in stmt["sql"]
    assert list(QHH_LATEST_READY_RUN_STATUSES) in stmt["params"]


def test_default_returns_all_basins_backward_compatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, captured = _install_fake_store(monkeypatch)

    basins = store.list_basins(limit=200, offset=0)

    ids = {b["basin_id"] for b in basins}
    assert ids == {"basins_qhh", "basins_downloading", "basins_empty"}
    # default path issues no ready filter and passes only (limit, offset).
    stmt = captured[0]
    assert "EXISTS" not in stmt["sql"]
    assert stmt["params"] == (200, 0)


def test_non_ready_only_basin_excluded_consistent_with_latest_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _captured = _install_fake_store(monkeypatch)

    basins = store.list_basins(limit=200, offset=0, has_display_product=True)

    ids = {b["basin_id"] for b in basins}
    # a basin whose only run is `downloading` (not in ready set) is absent.
    assert "basins_downloading" not in ids
    assert "downloading" not in QHH_LATEST_READY_RUN_STATUSES
