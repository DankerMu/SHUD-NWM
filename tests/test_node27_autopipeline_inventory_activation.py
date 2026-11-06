"""Inventory-only seeds never take the basin's active flag (#3057).

Operator decision (2026-11-05): production only serves direct-grid variants, so
a basin's single active ``core.model_instance`` row (one per basin version,
``model_instance_active_basin_version_uidx``) must be the variant, never its
baseline. ``_activate_model`` only activates when no sibling holds the flag, so
a baseline activated by an inventory-only seed (empty database, no run yet)
silently blocked every later direct-grid run's activation (0 rows, reported as
``already_seeded``).

The registry below stands in for ``core.model_instance`` at the database
boundary only; the real ``_seed_basin`` and ``_ensure_seeded_basin_display_ready``
run, so the ``activate`` decision the tick makes is what reaches the registry.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import node27_autopipeline as autopipe
from tests.test_node27_autopipeline_handoff import (
    DIRECT_GRID_RUN,
    NODE27_DATABASE_URL,
    _command_kinds,
    _prepare_autopipe,
    _replace_run_model,
    _run_main,
    _seed_inventory,
    _write_run,
)

BASELINE = "basins_qhh_shud"
VARIANT = "dg_0123456789abcdef"
BASIN_ID = "basins_qhh"
BASIN_VERSION = "basins_qhh_v2026_06"
RIVER_NETWORK_VERSION = "rnv-qhh"
# Captured at import, before any test's monkeypatch replaces it.
_REAL_DISPLAY_READY = autopipe._ensure_seeded_basin_display_ready


class _Registry:
    """``core.basin`` + ``core.model_instance`` with the one-active-per-version rule."""

    def __init__(self) -> None:
        self.basins: set[str] = set()
        self.models: dict[str, dict[str, str | bool]] = {}

    def register(self, model_id: str, *, active: bool = False) -> None:
        self.models[model_id] = {"basin_version_id": BASIN_VERSION, "active": active}

    def active(self) -> dict[str, bool]:
        return {model_id: bool(row["active"]) for model_id, row in self.models.items()}

    def basin_seeded(self, _database_url: str, basin_id: str) -> bool:
        return basin_id in self.basins

    def river_network_version_id(self, _database_url: str, model_id: str) -> str | None:
        return RIVER_NETWORK_VERSION if model_id in self.models else None

    def _active_sibling(self, model_id: str) -> str | None:
        version = self.models[model_id]["basin_version_id"]
        holders = sorted(m for m, row in self.models.items() if row["basin_version_id"] == version and row["active"])
        return holders[0] if holders else None

    def activate_model(self, _database_url: str, model_id: str) -> int:
        # Mirrors `_activate_model`'s WHERE: already active, or no active sibling.
        if model_id not in self.models:
            return 0
        holder = self._active_sibling(model_id)
        if holder not in (None, model_id):
            return 0
        self.models[model_id]["active"] = True
        return 1

    def active_sibling_model_id(self, _database_url: str, model_id: str) -> str | None:
        return self._active_sibling(model_id) if model_id in self.models else None

    def commands(self, argv: list[str], _env: dict[str, str]) -> tuple[int, str, str]:
        command = " ".join(argv)
        if "discover-basins" in argv or "publish-basins" in argv:
            return 0, "{}\n", ""
        if "import-basins-registry" in argv:
            # The generic import registers the baseline WITHOUT an active flag.
            self.basins.add(BASIN_ID)
            self.register(BASELINE)
            return 0, json.dumps({"status": "ok", "river_network_version_id": RIVER_NETWORK_VERSION}) + "\n", ""
        if "node27_ingest_run.py" in command:
            return 0, json.dumps({"status": "registered"}) + "\n", ""
        if "workers.output_parser.cli" in command:
            return 0, json.dumps({"status": "parsed", "rows_written": 1}) + "\n", ""
        if "node27_refresh_coverage.py" in command:
            return 0, json.dumps({"refreshed": True}) + "\n", ""
        raise AssertionError(f"unexpected command: {argv}")


def _no_live_database(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("a database helper outside the fake registry opened a connection")


def _install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, list[list[str]], _Registry]:
    registry = _Registry()
    root, calls, _published = _prepare_autopipe(
        monkeypatch, tmp_path, runs={}, command_handler=registry.commands,
    )
    (root.parent / "Basins" / "qhh").mkdir()
    monkeypatch.setattr(
        autopipe, "discover_basins_inventory", lambda _root: {"models": [_seed_inventory("qhh", BASELINE)]},
    )
    # Undo `_prepare_autopipe`'s display-ready double: the real one is under test.
    monkeypatch.setattr(autopipe, "_ensure_seeded_basin_display_ready", _REAL_DISPLAY_READY)
    monkeypatch.setattr(autopipe, "_connect", _no_live_database)
    monkeypatch.setattr(autopipe, "_basin_seeded", registry.basin_seeded)
    monkeypatch.setattr(autopipe, "_model_river_network_version_id", registry.river_network_version_id)
    monkeypatch.setattr(autopipe, "_backfill_output_geometry", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(autopipe, "_activate_model", registry.activate_model)
    return root, calls, registry


def _add_direct_grid_run(root: Path) -> None:
    _write_run(root, DIRECT_GRID_RUN)
    _replace_run_model(root, DIRECT_GRID_RUN, VARIANT, baseline=BASELINE)


def test_inventory_only_seed_on_empty_database_leaves_the_flag_for_the_first_direct_grid_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    root, calls, registry = _install(monkeypatch, tmp_path)

    # Tick 1: empty database, BASINS_ROOT offers the basin, no run exists yet.
    rc, summary = _run_main(capsys, root)
    assert rc == 0
    assert summary["seed"]["seeded"] == ["qhh"]
    assert summary["seed"]["details"][0]["model_activated_rows"] == 0
    assert registry.active() == {BASELINE: False}

    # Tick 2: still no run; the already-seeded pass must not activate it either.
    rc, summary = _run_main(capsys, root)
    assert rc == 0
    assert summary["seed"]["already_seeded"] == ["qhh"]
    assert summary["seed"]["details"][0]["model_activated_rows"] == 0
    assert registry.active() == {BASELINE: False}

    # node-22 provisions the variant (registered inactive), then its first run lands.
    registry.register(VARIANT)
    _add_direct_grid_run(root)
    rc, summary = _run_main(capsys, root)

    assert rc == 0
    detail = summary["seed"]["details"][0]
    assert (detail["outcome"], detail["model_id"], detail["model_activated_rows"]) == ("already_seeded", VARIANT, 1)
    assert "activation_skipped_other_active" not in detail
    assert summary["seed"]["activation_skipped_other_active"] == []
    assert registry.active() == {BASELINE: False, VARIANT: True}
    assert summary["runs"]["ingested"] == 1
    assert _command_kinds(calls) == ["register", "parse", "coverage"]


def test_baseline_already_holding_the_flag_is_reported_not_flipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """A database seeded before #3057 keeps its baseline active; say so per basin."""
    root, calls, registry = _install(monkeypatch, tmp_path)
    monkeypatch.setattr(autopipe, "_active_sibling_model_id", registry.active_sibling_model_id)
    registry.basins.add(BASIN_ID)
    registry.register(BASELINE, active=True)
    registry.register(VARIANT)
    _add_direct_grid_run(root)

    rc, summary = _run_main(capsys, root)

    assert rc == 0
    detail = summary["seed"]["details"][0]
    assert (detail["outcome"], detail["model_id"], detail["model_activated_rows"]) == ("already_seeded", VARIANT, 0)
    assert detail["activation_skipped_other_active"] == BASELINE
    assert summary["seed"]["activation_skipped_other_active"] == [
        {"basin": "qhh", "model_id": VARIANT, "active_model_id": BASELINE},
    ]
    assert summary["seed"]["already_seeded"] == ["qhh"]
    # Visibility only: no automatic baseline -> variant flip, and the run still ingests.
    assert registry.active() == {BASELINE: True, VARIANT: False}
    assert summary["runs"]["ingested"] == 1
    assert _command_kinds(calls) == ["register", "parse", "coverage"]


def test_active_sibling_lookup_reads_the_flag_holder_on_the_same_basin_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executed: list[tuple[str, tuple[str, ...]]] = []
    closed: list[bool] = []

    class Cursor:
        def __enter__(self) -> Cursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, sql: str, parameters: tuple[str, ...]) -> None:
            executed.append((sql, parameters))

        def fetchone(self) -> tuple[str] | None:
            return (BASELINE,)

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(autopipe.psycopg2, "connect", lambda _url, **_kwargs: Connection())

    assert autopipe._active_sibling_model_id(NODE27_DATABASE_URL, VARIANT) == BASELINE
    assert closed == [True]
    sql, parameters = executed[0]
    assert "active_sibling.basin_version_id = model.basin_version_id" in sql
    assert "active_sibling.active_flag = true" in sql
    assert parameters == (VARIANT,)
