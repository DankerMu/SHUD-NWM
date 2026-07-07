"""CMFD P0.2 §2.4 real-object-store + real-DB direct-grid smoke carrier.

Gated by ``NHMS_RUN_CMFD_P02_SMOKE=1`` + ``DATABASE_URL`` +
``NHMS_CMFD_P02_SYNTHETIC_PACKAGE_ROOT``; skipped in default CI. Runnable on
node-27 for §2.4 evidence. Carries the ``real_disk`` marker so a marker-based
CI filter still excludes it; the ``integration`` marker is intentionally NOT
attached because that marker is globally short-circuited by
``tests/conftest.py`` under ``NHMS_RUN_INTEGRATION`` and would preempt the
smoke's own env gate.

See openspec/changes/cmfd-direct-grid-platform-readiness/evidence/synthetic-package/README.md
for the package this smoke consumes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytestmark = [pytest.mark.real_disk]

_GATE_ENV = "NHMS_RUN_CMFD_P02_SMOKE"
_PACKAGE_ROOT_ENV = "NHMS_CMFD_P02_SYNTHETIC_PACKAGE_ROOT"
_TRUE_SET = {"1", "true", "yes", "on"}


def _gate_or_skip() -> Path:
    if os.environ.get(_GATE_ENV, "").strip().lower() not in _TRUE_SET:
        pytest.skip(
            f"{_GATE_ENV} not set; §2.4 real-backend smoke gated off. "
            "Set on node-27 to run against the synthetic evidence contract."
        )
    if not os.environ.get("DATABASE_URL", "").strip():
        pytest.skip("DATABASE_URL not set; smoke needs live node-27 primary PG.")
    root = os.environ.get(_PACKAGE_ROOT_ENV, "").strip()
    if not root:
        pytest.skip(
            f"{_PACKAGE_ROOT_ENV} not set; point to the synthetic package tree."
        )
    root_path = Path(root)
    if not root_path.is_dir():
        pytest.skip(f"{_PACKAGE_ROOT_ENV}={root} is not a directory.")
    return root_path


def test_synthetic_package_files_present() -> None:
    """Placeholder: confirms the fixture assets we expect §2.4 to consume are on disk."""
    root = _gate_or_skip()
    expected = [
        "package/input_dir/synth-basin/synth-basin.sp.att",
        "package/forcing/qhh.tsd.forc",
        "package/forcing/station-001.csv",
        "package/forcing/station-002.csv",
        "package/forcing/station-003.csv",
        "package/binding-manifest.json",
    ]
    missing = [p for p in expected if not (root / p).is_file()]
    assert not missing, f"Missing synthetic package files: {missing}"


def test_binding_manifest_parses_and_has_required_fields() -> None:
    """Structural check for the §7.2 top-level binding manifest."""
    root = _gate_or_skip()
    manifest_path = root / "package/binding-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    required = {
        "forcing_mapping_mode",
        "binding_uri",
        "binding_checksum",
        "model_input_package_id",
        "sp_att_path",
        "sp_att_checksum",
        "applicable_source_ids",
        "grid_id",
        "grid_signature",
        "station_bindings",
    }
    missing = required - manifest.keys()
    assert not missing, f"§7.2 fields missing from binding manifest: {missing}"
    assert manifest["forcing_mapping_mode"] == "direct_grid"
    assert isinstance(manifest["station_bindings"], list) and manifest["station_bindings"], (
        "station_bindings must be a non-empty list"
    )


def test_sp_att_forc_subset_of_tsd_forc_ids() -> None:
    """Runtime invariant .sp.att FORC ⊆ .tsd.forc IDs — the DIRECT_GRID_FORCING_OWNERSHIP_RANGE gate."""
    root = _gate_or_skip()
    sp_att = (root / "package/input_dir/synth-basin/synth-basin.sp.att").read_text(
        encoding="utf-8"
    ).splitlines()
    # line 0 = element count; line 1 = header (ID\tA\tB\tC\tFORC); lines 2..N element rows.
    forc_ids: set[int] = set()
    for row in sp_att[2:]:
        parts = row.split("\t")
        if len(parts) < 5:
            continue
        forc_ids.add(int(parts[4]))

    tsd = (root / "package/forcing/qhh.tsd.forc").read_text(encoding="utf-8").splitlines()
    # line 0 = "<count> <YYYYMMDD>"; line 1 = "shud"; line 2 = header; lines 3.. rows.
    tsd_ids: set[int] = set()
    for row in tsd[3:]:
        parts = row.split("\t")
        if not parts or not parts[0].strip():
            continue
        tsd_ids.add(int(parts[0]))

    assert forc_ids <= tsd_ids, (
        f"synthetic .sp.att FORC {sorted(forc_ids)} not ⊆ .tsd.forc IDs {sorted(tsd_ids)}"
    )
