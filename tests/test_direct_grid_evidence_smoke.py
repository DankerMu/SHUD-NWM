"""Skeleton smoke carrier for CMFD P0.2 §2.4 (real-object-store + real-DB direct-grid smoke).

Task 2.3 (this PR) commits this carrier as a prerequisite; §2.4 will extend it
to exercise the real object store and the real DB. Current tests are STRUCTURAL
only:
  * files present on disk
  * binding-manifest.json parses and has §7.2 required fields
  * .sp.att FORC values ⊆ .tsd.forc ID set (fixture pre-flight; real runtime
    oracle is workers/shud_runtime/runtime._read_sp_att_forcing_ids exercised
    in §2.4/§2.5)
  * SHA-256 sidecars match parent-file bytes and the aggregate manifest matches

Gating (all four required, else all tests skip):
  * NHMS_RUN_E2E=1 — bypasses tests/conftest.py:22-36 e2e-marker auto-skip
    (fires at collection time BEFORE _gate_or_skip)
  * NHMS_RUN_CMFD_P02_SMOKE=1
  * DATABASE_URL (inherited from infra/env/node27-ingest.env on node-27)
  * NHMS_CMFD_P02_SYNTHETIC_PACKAGE_ROOT=<absolute path to the package/ tree>

CI behavior: the [e2e, real_disk] marker set is auto-skipped by
tests/conftest.py:22-36 (marker-based CI defense-in-depth), and _gate_or_skip()
at test-body time is a second layer. Do NOT hoist _gate_or_skip() to module
scope or a session fixture without also verifying that conftest's marker
auto-skip still runs first.

CI scope note: this file lives under tests/**, which triggers the CI backend
paths-filter. The targeted-selection fallback in scripts/select_ci_tests.py
runs `pytest -q` against this file, which collect + immediate-skip via the
marker auto-skip — expected and cheap.

See openspec/changes/cmfd-direct-grid-platform-readiness/evidence/synthetic-package/README.md
for the package this smoke consumes.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

# NOTE: [e2e, real_disk] together provide marker-based CI defense-in-depth
# (auto-skipped by tests/conftest.py:22-36) alongside the runtime env gate.
pytestmark = [pytest.mark.e2e, pytest.mark.real_disk]

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
        "input_dir/synth-basin/synth-basin.sp.att",
        "forcing/qhh.tsd.forc",
        "forcing/station-001.csv",
        "forcing/station-002.csv",
        "forcing/station-003.csv",
        "binding-manifest.json",
    ]
    missing = [p for p in expected if not (root / p).is_file()]
    assert not missing, f"Missing synthetic package files: {missing}"


def test_binding_manifest_parses_and_has_required_fields() -> None:
    """Structural check for the §7.2 top-level binding manifest."""
    root = _gate_or_skip()
    manifest_path = root / "binding-manifest.json"
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
    # Non-goal for §2.3: this test hand-parses .sp.att / .tsd.forc rather than calling
    # workers/shud_runtime/runtime._read_sp_att_forcing_ids. The real runtime oracle for
    # the FORC⊆tsd invariant is exercised in §2.4 (node-27 real-DB smoke) and §2.5
    # (node-22 shud_omp staging); this smoke's FORC check is a fixture pre-flight only.
    root = _gate_or_skip()
    sp_att = (root / "input_dir/synth-basin/synth-basin.sp.att").read_text(
        encoding="utf-8"
    ).splitlines()
    # line 0 = element count; line 1 = header (ID\tA\tB\tC\tFORC); lines 2..N element rows.
    forc_ids: set[int] = set()
    for row in sp_att[2:]:
        parts = row.split("\t")
        if len(parts) < 5:
            continue
        forc_ids.add(int(parts[4]))

    tsd = (root / "forcing/qhh.tsd.forc").read_text(encoding="utf-8").splitlines()
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


def test_sha256_sidecars_match_parent_bytes() -> None:
    """Recompute sha256 of each parent file and assert equality to sidecar contents.

    Also recompute the aggregate ``package.manifest.sha256`` per README §5 rule
    and assert equality to the recorded value. Catches silent drift from editor
    reformatting or line-ending normalization on the synthetic package.

    Trade-off: this test is gated by the module's [e2e, real_disk] marker set
    and by _gate_or_skip(), so default CI does NOT catch drift — operators must
    run this manually (see synthetic-package/README.md §7) or via §2.4 smoke.
    Making it ungated would require moving it to a separate test module without
    the marker set.
    """
    root = _gate_or_skip()
    sidecars = [
        p for p in root.rglob("*.sha256")
        if p.name != "package.manifest.sha256"
    ]
    assert sidecars, f"no per-file .sha256 sidecars under {root}"
    for sidecar in sidecars:
        parent = sidecar.with_suffix("")  # strip ".sha256"
        assert parent.exists(), f"sidecar {sidecar} has no parent {parent}"
        expected = sidecar.read_text().strip().split()[0]
        actual = hashlib.sha256(parent.read_bytes()).hexdigest()
        assert actual == expected, (
            f"sidecar drift: {parent.relative_to(root)} has sha256 {actual} "
            f"but sidecar records {expected}"
        )

    aggregate_sidecar = root / "package.manifest.sha256"
    assert aggregate_sidecar.exists(), f"missing aggregate {aggregate_sidecar}"
    expected_aggregate = aggregate_sidecar.read_text().strip().split()[0]
    sorted_sidecar_paths = sorted(sidecars)
    concat = b"".join(p.read_bytes() for p in sorted_sidecar_paths)
    actual_aggregate = hashlib.sha256(concat).hexdigest()
    assert actual_aggregate == expected_aggregate, (
        f"aggregate drift: recomputed {actual_aggregate}, recorded {expected_aggregate}"
    )

    # Completeness inversion: every non-sidecar regular file under root MUST have a companion .sha256 sidecar.
    # This catches silent additions to the fixture package (stray .DS_Store, editor swap files, a new
    # station-N.csv added without its sidecar) that the sidecar-set-only walks above cannot see.
    non_sidecar_files = [
        p for p in root.rglob("*")
        if p.is_file() and not p.name.endswith(".sha256")
    ]
    sidecar_parents = {sidecar.with_suffix("") for sidecar in sidecars}
    # aggregate has no parent file; add sentinel so it is excluded defensively
    sidecar_parents.add(aggregate_sidecar.with_suffix(""))
    orphans = [
        p.relative_to(root)
        for p in non_sidecar_files
        if p not in sidecar_parents
    ]
    assert not orphans, (
        f"orphan files without .sha256 sidecar under {root}: {orphans}. "
        "Add a sidecar (sha256sum <file> > <file>.sha256), regenerate the aggregate "
        "(README §5 recipe), or remove the file to preserve byte-stability."
    )
