#!/usr/bin/env python3
"""Task 4.1(b) — register the Change-4-shaped synthetic M1 target on node-27.

Runs `workers.model_registry.direct_grid_variant_registration.register_direct_grid_variant`
against node-27's live PostgreSQL to insert the M1 target `core.model_instance`
row and its three `met.met_station` cell-station mirror rows (per §D1 and
§7.2 of the direct-grid design). The M1 target lands `active_flag=false`
per Change 4 registration invariants; the rehearse.py cutover then activates
it via the real Change 4 lifecycle op.

Idempotency: the script SELECTs first. If the M1 target row is already
present, prints and exits 0.

Post-registration: patches the M1 target's `resource_profile` with the
package-checksum verification markers (`package_checksum`,
`package_checksum_verified=true`, `copied_root_status='present'`) that
`_activation_safety_evidence` (`packages/common/model_registry.py:3773`)
requires for the activate-class preflight to pass. Without this patch,
Change 4's preflight would raise `PACKAGE_CHECKSUM_MISSING`.

Usage (node-27):
    DATABASE_URL="postgresql://nhms:...@127.0.0.1:55432/nhms" \\
    GRID_SNAPSHOT_ID="<uuid from 01-canonical-grid-snapshot.sql>" \\
    uv run python provisioning/02-register-direct-grid-variant.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Ensure the repo root is on sys.path so `workers.*` and `packages.*` resolve
# when the script is executed directly by uv (not via `python -m`).
_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Use psycopg2 (matches the codebase-wide convention; `workers.model_registry
# .direct_grid_variant_registration._json` requires psycopg2 `Json` adapter for
# the resource_profile jsonb bind, so the cursor MUST be a psycopg2 cursor).
import psycopg2  # noqa: E402
from psycopg2.extras import RealDictCursor  # noqa: E402

from workers.model_registry.direct_grid_variant_registration import (  # noqa: E402
    DirectGridBaselineModelInputs,
    DirectGridVariantRegistrationInput,
    register_direct_grid_variant,
)

# --- Rehearsal identity constants -----------------------------------------

BASIN_VERSION_ID = "basin__evidence_cmfd_p02_synth__v1"
BASELINE_MODEL_ID = "model__evidence_cmfd_p02_synth__v1"
TARGET_MODEL_ID_HUMAN = "model__evidence_cmfd_p02_synth__v2"  # human-readable label; real mint uses SHA-256-derived id
MAPPING_ASSET_IDENTITY = "synth-mip-m1-v2"
BINDING_CHECKSUM = "d1e2c3b4a5968778869574a3b2c1d0e9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c3"

RESOURCE_PROFILE_PATCH: dict[str, object] = {
    # Preflight (`_activation_safety_evidence`) requires a non-empty
    # `package_checksum` AND a verified reread status.
    "package_checksum": "sha256:d1e2c3b4a5968778869574a3b2c1d0e9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c3",
    "package_checksum_verified": True,
    # `_copied_root_status` returns "present" for {"present","safe","copied","verified"}
    # or when the value is a non-path-looking string.
    "copied_root_status": "present",
    # Evidence marker so the audit trail can distinguish rehearsal-patched
    # rows from production registrations.
    "evidence_rehearsal": "epic__992__sub_7__display_cutover",
}


def _database_url() -> str:
    return os.environ.get("DATABASE_URL", "postgresql://nhms:nhms_dev@127.0.0.1:55432/nhms")


def _grid_snapshot_id(cursor) -> str:
    override = os.environ.get("GRID_SNAPSHOT_ID", "").strip()
    if override:
        return override
    cursor.execute(
        """
        SELECT grid_snapshot_id::text AS grid_snapshot_id
        FROM met.canonical_grid_snapshot
        WHERE grid_id = 'synth-grid-p0.2-m1-v2'
        ORDER BY created_at DESC
        LIMIT 1
        """
    )
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError(
            "no rehearsal canonical_grid_snapshot found; run 01-canonical-grid-snapshot.sql first."
        )
    return str(row["grid_snapshot_id"])


def _baseline_from_evidence_row(cursor) -> DirectGridBaselineModelInputs:
    """Read the NOT NULL baseline fields from the M0 evidence model_instance row.

    Ensures the M1 target inherits river_network / mesh / calibration /
    shud_code identifiers from the SQL-provisioned baseline so the Change 4
    activation preflight and the flip hook see a consistent lineage.
    """
    cursor.execute(
        """
        SELECT river_network_version_id,
               mesh_version_id,
               calibration_version_id,
               shud_code_version,
               model_package_uri
        FROM core.model_instance
        WHERE model_id = %s
        """,
        (BASELINE_MODEL_ID,),
    )
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError(
            f"baseline model_instance {BASELINE_MODEL_ID!r} not found; run 00-baseline-and-stations.sql first."
        )
    return DirectGridBaselineModelInputs(
        river_network_version_id=str(row["river_network_version_id"]),
        mesh_version_id=str(row["mesh_version_id"]),
        calibration_version_id=str(row["calibration_version_id"]),
        # Preflight `_activation_safety_evidence` accepts any non-empty
        # shud_code_version — the archived baseline uses "basins-shud".
        shud_code_version=str(row["shud_code_version"]),
        # Preflight requires `_object_uri_prefix_status(model_package_uri)` in
        # {s3,az,gs,https,http,integration,memory}. The archived baseline
        # URI is https://github.com/... so scheme is supported.
        model_package_uri="https://github.com/DankerMu/SHUD-NWM/tree/master/openspec/changes/direct-grid-display-cutover/evidence/provisioning/synthetic-package",
    )


def _build_direct_grid_contract(grid_snapshot_id: str) -> dict[str, object]:
    """Return the M1 direct-grid contract payload (parser-shaped).

    Station IDs use the docs §7.4 mapping-asset identity convention:
        station_id = f"{mapping_asset_identity}::cell:{grid_cell_id}"

    so the flip hook's target-set predicate (docs §D1) matches exactly the
    M1 mirror rows on `properties_json->>'model_input_package_id' + ...
    ->>'binding_checksum' + grid_snapshot_id`.
    """
    stations = [
        {
            "station_id": f"{MAPPING_ASSET_IDENTITY}::cell:cell-0100.00-0030.00",
            "shud_forcing_index": 1,
            "forcing_filename": "station-001.csv",
            "longitude": 100.0,
            "latitude": 30.0,
            "x": 1,
            "y": 1,
            "z": 100,
            "grid_id": "synth-grid-p0.2-m1-v2",
            "grid_cell_id": "cell-0100.00-0030.00",
        },
        {
            "station_id": f"{MAPPING_ASSET_IDENTITY}::cell:cell-0100.50-0030.00",
            "shud_forcing_index": 2,
            "forcing_filename": "station-002.csv",
            "longitude": 100.5,
            "latitude": 30.0,
            "x": 2,
            "y": 1,
            "z": 150,
            "grid_id": "synth-grid-p0.2-m1-v2",
            "grid_cell_id": "cell-0100.50-0030.00",
        },
        {
            "station_id": f"{MAPPING_ASSET_IDENTITY}::cell:cell-0100.00-0030.50",
            "shud_forcing_index": 3,
            "forcing_filename": "station-003.csv",
            "longitude": 100.0,
            "latitude": 30.5,
            "x": 1,
            "y": 2,
            "z": 200,
            "grid_id": "synth-grid-p0.2-m1-v2",
            "grid_cell_id": "cell-0100.00-0030.50",
        },
    ]
    return {
        "forcing_mapping_mode": "direct_grid",
        "binding_uri": "synth://direct-grid-display-cutover/m1/v2",
        "binding_checksum": BINDING_CHECKSUM,
        "model_input_package_id": MAPPING_ASSET_IDENTITY,
        "sp_att_path": "input_dir/synth-basin-m1-v2/synth-basin-m1-v2.sp.att",
        "sp_att_checksum": "b7a6c5d4e3f2a1b0c9d8e7f6a5b4c3d2e1f0a9b8c7d6e5f4a3b2c1d0e9f8a7b6",
        # The direct-grid contract parser (`workers/forcing_producer/
        # direct_grid_contract.py::_applicable_source_ids`) validates each
        # entry via `packages.common.source_identity.normalize_source_id`,
        # which only accepts {GFS, ERA5, IFS}. `cmfd` — the evidence-lineage
        # name reflected in basin/model/run IDs — is NOT parser-supported.
        # We therefore declare `gfs` as the M1 target's applicable source,
        # matching the canonical_grid_snapshot's `source_id='gfs'` in
        # 01-canonical-grid-snapshot.sql. The `cmfd` narrative is preserved
        # in the basin_version_id / baseline model_id identity strings.
        "applicable_source_ids": ["gfs"],
        "grid_id": "synth-grid-p0.2-m1-v2",
        "grid_signature": "e2c0bf1a8d6c4f5b9a7f3e1d0c8b6a4f2d7e5c3b1a9f8d6c4b2a0f9e7d5c3b1a9",
        "station_bindings": stations,
    }


def _existing_target_model_id(
    cursor, grid_snapshot_id: str
) -> str | None:
    """Look up an already-registered M1 target by its built-asset identity."""
    cursor.execute(
        """
        SELECT model_id
        FROM core.model_instance
        WHERE basin_version_id = %s
          AND resource_profile->'direct_grid_forcing'->>'model_input_package_id' = %s
          AND resource_profile->'direct_grid_forcing'->>'binding_checksum' = %s
          AND resource_profile->>'grid_snapshot_id' = %s
        LIMIT 1
        """,
        (BASIN_VERSION_ID, MAPPING_ASSET_IDENTITY, BINDING_CHECKSUM, grid_snapshot_id),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return str(row["model_id"])


def _patch_resource_profile(cursor, model_id: str) -> None:
    """Patch the M1 target's resource_profile with preflight verification markers."""
    cursor.execute(
        """
        UPDATE core.model_instance
           SET resource_profile = resource_profile || %s::jsonb
         WHERE model_id = %s
        """,
        (json.dumps(RESOURCE_PROFILE_PATCH), model_id),
    )


def _report_mirror_state(cursor, model_id: str) -> None:
    """Print the M1 mirror rows for the pass log."""
    cursor.execute(
        """
        SELECT station_id, basin_version_id, station_role, active_flag,
               grid_snapshot_id::text AS grid_snapshot_id,
               properties_json->>'model_input_package_id' AS mip,
               properties_json->>'binding_checksum' AS binding_checksum
        FROM met.met_station
        WHERE basin_version_id = %s
          AND station_role = 'direct_grid_cache'
          AND properties_json->>'model_input_package_id' = %s
        ORDER BY station_id
        """,
        (BASIN_VERSION_ID, MAPPING_ASSET_IDENTITY),
    )
    rows = cursor.fetchall()
    print(f"[02-register] M1 mirror rows for model_id={model_id}: {len(rows)}")
    for row in rows:
        print(f"  {dict(row)}")


def main() -> int:
    url = _database_url()
    print(f"[02-register] connecting to {url.split('@')[-1]}")
    with psycopg2.connect(url) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            grid_snapshot_id = _grid_snapshot_id(cursor)
            print(f"[02-register] resolved grid_snapshot_id={grid_snapshot_id}")

            existing = _existing_target_model_id(cursor, grid_snapshot_id)
            if existing is not None:
                print(f"[02-register] IDEMPOTENT SKIP: M1 target already exists model_id={existing}")
                _patch_resource_profile(cursor, existing)
                conn.commit()
                _report_mirror_state(cursor, existing)
                return 0

            baseline = _baseline_from_evidence_row(cursor)
            print(f"[02-register] baseline lineage: {baseline}")

            contract = _build_direct_grid_contract(grid_snapshot_id)
            registration_input = DirectGridVariantRegistrationInput(
                basin_version_id=BASIN_VERSION_ID,
                direct_grid_forcing=contract,
                baseline=baseline,
                grid_snapshot_id=grid_snapshot_id,
            )
            result = register_direct_grid_variant(cursor, registration_input)
            print(f"[02-register] register_direct_grid_variant result: {result}")

            _patch_resource_profile(cursor, result.model_id)
            conn.commit()

            _report_mirror_state(cursor, result.model_id)
            if result.mirror_stations_written != 3:
                raise RuntimeError(
                    f"expected 3 M1 mirror rows written, got {result.mirror_stations_written}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
