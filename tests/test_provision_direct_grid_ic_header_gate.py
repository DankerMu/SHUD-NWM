"""Direct-grid provision gate for malformed IC headers (#1197).

``scripts/provision_direct_grid_scheduler_registry.py:354`` is the ONLY point in
the direct-grid flow that reads baseline ``*.cfg.ic`` bytes into a published
package (``workers/model_registry/direct_grid_variant_registration.py`` is pure DB
row insertion and never touches IC bytes).  Whatever it hands to
``build_direct_grid_variant`` is copied verbatim into every provisioned variant, so
a malformed baseline header multiplies across sources and only detonates at first
runtime consumption.  These tests pin the fail-closed refusal at that read point.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.provision_direct_grid_scheduler_registry import (
    DirectGridProvisionError,
    _validated_state_schema_bytes,
)

# The exact lh_gl delivery: two numeric tokens, mesh count then column count, no
# minute-time.  Present, non-empty, checksum-clean -- and catastrophic.
INCIDENT_HEADER = "23106\t6"
NATIVE_HEADER = "23106\t6\t29714400.000000"
MESH_HEADER = "23106\t8"


def _baseline(tmp_path: Path, *, ic_header: str, mesh_header: str = MESH_HEADER) -> Path:
    root = tmp_path / "baseline"
    root.mkdir()
    (root / "LH-GL.cfg.ic").write_text(f"{ic_header}\n1\t0.1\t0.2\t0.3\t0.4\n", encoding="utf-8")
    (root / "LH-GL.sp.mesh").write_text(f"{mesh_header}\nID\tNode1\n", encoding="utf-8")
    return root


def test_well_formed_baseline_ic_is_returned_verbatim(tmp_path: Path) -> None:
    root = _baseline(tmp_path, ic_header=NATIVE_HEADER)

    payload = _validated_state_schema_bytes(root)

    assert payload == (root / "LH-GL.cfg.ic").read_bytes()


def test_compatibility_four_token_layout_is_accepted(tmp_path: Path) -> None:
    root = _baseline(tmp_path, ic_header="23106\t50\t3\t29714400.000000")

    assert _validated_state_schema_bytes(root)


def test_two_token_header_refuses_provisioning(tmp_path: Path) -> None:
    root = _baseline(tmp_path, ic_header=INCIDENT_HEADER)

    with pytest.raises(DirectGridProvisionError) as exc_info:
        _validated_state_schema_bytes(root)

    message = str(exc_info.value)
    # Locatable: the offending path and the actual token count.
    assert "LH-GL.cfg.ic" in message
    assert "2 numeric token(s)" in message


def test_mesh_count_mismatch_refuses_provisioning(tmp_path: Path) -> None:
    root = _baseline(tmp_path, ic_header=NATIVE_HEADER, mesh_header="484\t8")

    with pytest.raises(DirectGridProvisionError) as exc_info:
        _validated_state_schema_bytes(root)

    message = str(exc_info.value)
    assert "23106" in message and "484" in message


def test_unparseable_mesh_header_refuses_provisioning(tmp_path: Path) -> None:
    root = _baseline(tmp_path, ic_header=NATIVE_HEADER, mesh_header="ID\tNode1")

    with pytest.raises(DirectGridProvisionError) as exc_info:
        _validated_state_schema_bytes(root)

    assert "sp.mesh" in str(exc_info.value)


def test_binary_ic_refuses_provisioning(tmp_path: Path) -> None:
    root = _baseline(tmp_path, ic_header=NATIVE_HEADER)
    (root / "LH-GL.cfg.ic").write_bytes(b"\xff\xfe\x00binary\n")

    with pytest.raises(DirectGridProvisionError) as exc_info:
        _validated_state_schema_bytes(root)

    assert "UTF-8" in str(exc_info.value)


def test_missing_baseline_ic_still_refuses(tmp_path: Path) -> None:
    root = _baseline(tmp_path, ic_header=NATIVE_HEADER)
    (root / "LH-GL.cfg.ic").unlink()

    with pytest.raises(DirectGridProvisionError):
        _validated_state_schema_bytes(root)
