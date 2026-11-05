"""Fresh-database seed identity for node-27 autopipe (direct-grid + basin dir).

Two failures seen on the 2026-11-05 node-27 rebuild (empty database, 66 basins):

* A direct-grid run manifest names a variant (``dg_*`` under
  ``models/direct_grid_variants/<baseline>/dg-<src>-<id>/package``). The tick used
  that variant as the SEED identity, so ``publish-basins --model-id dg_*`` failed
  ``BASINS_MODEL_NOT_FOUND`` for every unseeded basin (39/39). Only the baseline
  is seedable from BASINS_ROOT; the variant is registered by
  ``scripts/provision_direct_grid_scheduler_registry.py`` and stays the display /
  activation target. The baseline is seeded INACTIVE so only the variant ever
  carries the basin's active flag.
* A run-only basin's manifest slug (``bj_ers``) is not its directory name
  (``BJ-ERS``); ``shutil.copytree`` raised out of ``main`` and aborted the tick.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts import node27_autopipeline as autopipe
from tests.test_node27_autopipeline_handoff import (
    DIRECT_GRID_RUN,
    RUN_HEIHE,
    _command_kinds,
    _prepare_autopipe,
    _run_main,
    _seed_inventory,
)

VARIANT = "dg_0123456789abcdef"
BASELINE = "basins_qhh_shud"


def _make_direct_grid_variant(root: Path, run_id: str, *, baseline: str = BASELINE) -> None:
    path = root / "runs" / run_id / "input" / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["identity"]["model_id"] = VARIANT
    manifest["identity"]["model_package_uri"] = (
        f"s3://nhms/models/direct_grid_variants/{baseline}/dg-gfs-f7751638e3b5ef6811b6/package/"
    )
    path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")


def _unseeded_with_capture(
    monkeypatch: pytest.MonkeyPatch,
    *,
    display_ready_error: Exception | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    seeded: list[dict[str, Any]] = []
    display_ready: list[str] = []
    monkeypatch.setattr(autopipe, "_basin_seeded", lambda *_args, **_kwargs: False)

    def seed(**kwargs: Any) -> dict[str, Any]:
        seeded.append({"identity": kwargs["identity"], "activate": kwargs["activate"], "basin": kwargs["basin"]})
        return {"basin": kwargs["basin"], "outcome": "seeded", "model_id": kwargs["identity"]["model_id"]}

    def ready(_database_url: str, model_id: str) -> dict[str, Any]:
        display_ready.append(model_id)
        if display_ready_error is not None:
            raise display_ready_error
        return {"model_id": model_id, "river_network_version_id": "rnv", "model_activated_rows": 1}

    monkeypatch.setattr(autopipe, "_seed_basin", seed)
    monkeypatch.setattr(autopipe, "_ensure_seeded_basin_display_ready", ready)
    return seeded, display_ready


@pytest.mark.parametrize(
    ("segments", "expected"),
    [
        (["s3:", "nhms", "models", "direct_grid_variants", BASELINE, "dg-gfs-abc", "package"], BASELINE),
        (["s3:", "nhms", "models", "direct_grid_variants", BASELINE, "dg-ifs-abc", "package"], BASELINE),
        (["s3:", "nhms", "models", BASELINE, "v2026_06", "package"], ""),
        (["s3:", "nhms", "models", "dg_abc", "vnew", "package"], ""),
        (["s3:", "nhms", "models", "direct_grid_variants", BASELINE, "v1", "package"], ""),
        (["s3:", "nhms", "models", "direct_grid_variants", BASELINE, "dg-gfs-abc"], ""),
        (["s3:", "nhms", "runs", "x"], ""),
    ],
)
def test_direct_grid_baseline_is_read_only_from_the_variant_package_layout(
    segments: list[str], expected: str,
) -> None:
    assert autopipe._direct_grid_baseline_model_id(segments) == expected


def test_unseeded_direct_grid_basin_seeds_its_baseline_inactive_then_readies_the_variant(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    root, calls, _published = _prepare_autopipe(monkeypatch, tmp_path, runs={DIRECT_GRID_RUN: True})
    _make_direct_grid_variant(root, DIRECT_GRID_RUN)
    monkeypatch.setattr(
        autopipe, "discover_basins_inventory", lambda _root: {"models": [_seed_inventory("qhh", BASELINE)]},
    )
    seeded, display_ready = _unseeded_with_capture(monkeypatch)

    rc, summary = _run_main(capsys, root)

    assert rc == 0
    assert [(s["identity"]["model_id"], s["activate"], s["basin"]) for s in seeded] == [(BASELINE, False, "qhh")]
    assert seeded[0]["identity"]["identity_source"] == "basins_inventory"
    assert display_ready == [VARIANT]
    detail = summary["seed"]["details"][0]
    assert (detail["outcome"], detail["model_id"], detail["seeded_model_id"]) == ("seeded", VARIANT, BASELINE)
    assert summary["runs"]["ingested"] == 1
    assert _command_kinds(calls) == ["register", "parse", "coverage"]


def test_unprovisioned_variant_fails_that_basin_closed_and_the_tick_still_reports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    root, calls, _published = _prepare_autopipe(monkeypatch, tmp_path, runs={DIRECT_GRID_RUN: True, RUN_HEIHE: True})
    _make_direct_grid_variant(root, DIRECT_GRID_RUN)
    monkeypatch.setattr(
        autopipe, "discover_basins_inventory",
        lambda _root: {"models": [_seed_inventory("qhh", BASELINE), _seed_inventory("heihe", "basins_heihe_shud")]},
    )
    seeded, display_ready = _unseeded_with_capture(
        monkeypatch, display_ready_error=ValueError(f"model_instance missing or incomplete for {VARIANT}"),
    )

    rc, summary = _run_main(capsys, root)

    assert rc == 1
    assert [(s["identity"]["model_id"], s["activate"]) for s in seeded] == [
        ("basins_heihe_shud", True),
        (BASELINE, False),
    ]
    assert display_ready == [VARIANT]
    failed = {item["basin"]: item for item in summary["seed"]["failed"]}
    assert set(failed) == {"qhh"} and failed["qhh"]["stage"] == "display_ready"
    assert VARIANT in failed["qhh"]["error"]
    assert [detail["run_id"] for detail in summary["runs"]["details"]] == [RUN_HEIHE]
    assert _command_kinds(calls) == ["register", "parse", "coverage"]


@pytest.mark.parametrize(
    "inventory",
    [[], [_seed_inventory("qhh", "basins_qhh_shud_vgrid_5539e2bd0b14")]],
    ids=["no-inventory-model", "inventory-is-not-the-baseline"],
)
def test_direct_grid_basin_without_its_baseline_in_inventory_is_never_seeded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    inventory: list[dict[str, Any]],
) -> None:
    root, calls, _published = _prepare_autopipe(monkeypatch, tmp_path, runs={DIRECT_GRID_RUN: True, RUN_HEIHE: True})
    _make_direct_grid_variant(root, DIRECT_GRID_RUN)
    models = [*inventory, _seed_inventory("heihe", "basins_heihe_shud")]
    monkeypatch.setattr(autopipe, "discover_basins_inventory", lambda _root: {"models": models})
    seeded, display_ready = _unseeded_with_capture(monkeypatch)

    rc, summary = _run_main(capsys, root)

    assert rc == 1
    assert [s["identity"]["model_id"] for s in seeded] == ["basins_heihe_shud"]
    assert display_ready == []
    failed = {item["basin"]: item for item in summary["seed"]["failed"]}
    assert set(failed) == {"qhh"} and failed["qhh"]["stage"] == "identity"
    assert BASELINE in failed["qhh"]["error"] and "provision_direct_grid_scheduler_registry" in failed["qhh"]["error"]
    assert [detail["run_id"] for detail in summary["runs"]["details"]] == [RUN_HEIHE]
    assert _command_kinds(calls) == ["register", "parse", "coverage"]


def test_seeded_direct_grid_basin_still_readies_the_variant_not_the_baseline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    root, _calls, _published = _prepare_autopipe(monkeypatch, tmp_path, runs={DIRECT_GRID_RUN: True})
    _make_direct_grid_variant(root, DIRECT_GRID_RUN)
    monkeypatch.setattr(
        autopipe, "discover_basins_inventory", lambda _root: {"models": [_seed_inventory("qhh", BASELINE)]},
    )
    seeded, display_ready = _unseeded_with_capture(monkeypatch)
    monkeypatch.setattr(autopipe, "_basin_seeded", lambda *_args, **_kwargs: True)

    rc, summary = _run_main(capsys, root)

    assert rc == 0
    assert seeded == []
    assert display_ready == [VARIANT]
    assert summary["seed"]["details"][0]["outcome"] == "already_seeded"
    assert summary["runs"]["ingested"] == 1


# --------------------------------------------------------------------------- #
# basin directory resolution (manifest slug != directory name)
# --------------------------------------------------------------------------- #
def test_resolve_basin_dir_matches_the_unique_normalised_directory(tmp_path: Path) -> None:
    (tmp_path / "BJ-ERS").mkdir()
    (tmp_path / "Haihe-ziyahe").mkdir()
    (tmp_path / "hn_file").write_text("not a dir", encoding="utf-8")

    assert autopipe._resolve_basin_dir(tmp_path, "BJ-ERS") == "BJ-ERS"
    assert autopipe._resolve_basin_dir(tmp_path, "bj_ers") == "BJ-ERS"
    assert autopipe._resolve_basin_dir(tmp_path, "haihe_ziyahe") == "Haihe-ziyahe"
    assert autopipe._resolve_basin_dir(tmp_path, "hn_file") is None
    assert autopipe._resolve_basin_dir(tmp_path, "missing") is None
    assert autopipe._resolve_basin_dir(tmp_path / "absent-root", "bj_ers") is None


def test_resolve_basin_dir_refuses_an_ambiguous_normalised_name(tmp_path: Path) -> None:
    (tmp_path / "BJ-ERS").mkdir()
    (tmp_path / "BJ_ERS").mkdir()

    # `bj.ers` is no directory's literal name on any filesystem (macOS is
    # case-insensitive) but normalises to both.
    assert autopipe._resolve_basin_dir(tmp_path, "bj.ers") is None


def test_seed_basin_copies_the_resolved_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    basins_root = tmp_path / "Basins"
    (basins_root / "BJ-ERS").mkdir(parents=True)
    monkeypatch.setattr(autopipe, "WORK_ROOT", str(tmp_path / "work"))
    isolated: list[str] = []

    def isolate(_root: Path, basin: str) -> Path:
        isolated.append(basin)
        only_root = tmp_path / "only-root"
        only_root.mkdir(exist_ok=True)
        return only_root

    monkeypatch.setattr(autopipe, "_isolate_basin_root", isolate)
    monkeypatch.setattr(autopipe, "_run", lambda _argv, _env: (1, "", "discover stopped here"))

    result = autopipe._seed_basin(
        basin="bj_ers",
        identity={"model_id": "basins_bj_ers_shud", "package_version": "v1"},
        database_url="postgresql://unused",
        basins_root=basins_root,
        env={},
    )

    assert isolated == ["BJ-ERS"]
    assert (result["basin"], result["outcome"], result["stage"]) == ("BJ-ERS", "seed_failed", "discover")


def test_run_only_basin_without_a_directory_fails_that_basin_not_the_tick(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    root, calls, _published = _prepare_autopipe(monkeypatch, tmp_path, runs={DIRECT_GRID_RUN: True, RUN_HEIHE: True})
    (root.parent / "Basins").mkdir(exist_ok=True)
    monkeypatch.setattr(
        autopipe, "discover_basins_inventory",
        lambda _root: {"models": [_seed_inventory("heihe", "basins_heihe_shud")]},
    )
    monkeypatch.setattr(autopipe, "_basin_seeded", lambda _database_url, basin_id: basin_id == "basins_heihe")

    rc, summary = _run_main(capsys, root)

    assert rc == 1
    failed = {item["basin"]: item for item in summary["seed"]["failed"]}
    assert set(failed) == {"qhh"} and failed["qhh"]["stage"] == "discover"
    assert [detail["run_id"] for detail in summary["runs"]["details"]] == [RUN_HEIHE]
    assert _command_kinds(calls) == ["register", "parse", "coverage"]
