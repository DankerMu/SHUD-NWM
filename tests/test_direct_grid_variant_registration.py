"""Unit tests for the direct-grid variant registration surface.

Covers Epic #961 / tasks.md §1.1 "Direct-Grid Variant Registration" evidence:

* `-k "inactive_row or grain or shared_key or separate_variants or fix_forward_new_row"`
  proves a registered variant is a new `core.model_instance` row with
  ``active_flag=false`` / ``lifecycle_state='inactive'`` / contract at
  ``resource_profile.direct_grid_forcing`` / ``canonical_grid_key`` top-level,
  that exactly one variant exists per
  ``(basin_version_id, canonical_grid_key, built-asset identity)``,
  that IFS+GFS sharing one key yields a single row whose
  ``applicable_source_ids`` contains both normalized ids,
  that non-shared sources register as separate variants, and that a
  fix-forward (new ``model_input_package_id``) inserts a NEW row while the
  prior generation is untouched.
* `-k "key_from_snapshot"` proves the persisted
  ``resource_profile.canonical_grid_key`` equals the registered snapshot's
  ``canonical_grid_key`` byte-for-byte (verbatim copy, no re-derivation).
* Parser round-trip: the emitted ``resource_profile.direct_grid_forcing``
  block round-trips through
  ``workers.forcing_producer.direct_grid_contract.load_forcing_mapping_contract_from_manifest``
  without error.

The tests use an in-memory fake cursor that models the two tables the
registration surface touches — `met.canonical_grid_snapshot` (read) and
`core.model_instance` (read + write) — by recognizing each SQL statement by
fragment. This locks the actual SQL shape while remaining fast and
transaction-free (real-DB coverage is a separate §5 evidence line).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from workers.forcing_producer.direct_grid_contract import (
    load_forcing_mapping_contract_from_manifest,
)
from workers.model_registry.direct_grid_variant_registration import (
    DirectGridBaselineModelInputs,
    DirectGridVariantRegistrationError,
    DirectGridVariantRegistrationInput,
    register_direct_grid_variant,
)

# --- fake cursor + backing store -------------------------------------------


class _InMemoryDb:
    """Backing store for `_FakeCursor`, modeling the two tables under test."""

    def __init__(self) -> None:
        self.snapshots: list[dict[str, Any]] = []
        self.model_instances: list[dict[str, Any]] = []

    def add_snapshot(
        self,
        *,
        grid_snapshot_id: str,
        canonical_grid_key: str,
        grid_id: str = "grid_a",
        grid_signature: str = "sha256:grid-signature-a",
    ) -> None:
        self.snapshots.append(
            {
                "grid_snapshot_id": grid_snapshot_id,
                "canonical_grid_key": canonical_grid_key,
                "grid_id": grid_id,
                "grid_signature": grid_signature,
            }
        )


class _FakeCursor:
    """SQL-fragment-recognizing fake cursor for the direct-grid registration.

    Handles three statement kinds:

    1. ``SELECT ... FROM met.canonical_grid_snapshot WHERE grid_snapshot_id = %s``
    2. ``SELECT ... FROM met.canonical_grid_snapshot WHERE grid_id = %s AND grid_signature = %s``
    3. ``SELECT model_id FROM core.model_instance WHERE ... (JSONB path lookup)``
    4. ``INSERT INTO core.model_instance ...``

    Any other statement is a signal that the registration surface is issuing
    SQL the tests do not expect; the fake fails loudly rather than silently
    swallowing it.
    """

    def __init__(self, db: _InMemoryDb) -> None:
        self.db = db
        self._pending: dict[str, Any] | None = None
        self.statements: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, statement: str, parameters: tuple[Any, ...] = ()) -> None:
        self.statements.append((statement, tuple(parameters)))
        normalized = " ".join(statement.split()).lower()
        if "from met.canonical_grid_snapshot" in normalized:
            self._pending = self._handle_snapshot_select(normalized, parameters)
            return
        if "from core.model_instance" in normalized and "resource_profile" in normalized:
            self._pending = self._handle_model_instance_select(parameters)
            return
        if "insert into core.model_instance" in normalized:
            self._pending = None
            self._handle_model_instance_insert(statement, parameters)
            return
        raise AssertionError(f"Unexpected SQL for fake cursor: {statement!r}")

    def fetchone(self) -> dict[str, Any] | None:
        result = self._pending
        self._pending = None
        return result

    def _handle_snapshot_select(
        self, normalized: str, parameters: tuple[Any, ...]
    ) -> dict[str, Any] | None:
        if "where grid_snapshot_id" in normalized:
            (grid_snapshot_id,) = parameters
            matches = [
                snapshot for snapshot in self.db.snapshots if snapshot["grid_snapshot_id"] == grid_snapshot_id
            ]
        else:
            grid_id, grid_signature = parameters
            matches = [
                snapshot
                for snapshot in self.db.snapshots
                if snapshot["grid_id"] == grid_id and snapshot["grid_signature"] == grid_signature
            ]
        if not matches:
            return None
        row = matches[0]
        return {
            "grid_snapshot_id": row["grid_snapshot_id"],
            "canonical_grid_key": row["canonical_grid_key"],
        }

    def _handle_model_instance_select(
        self, parameters: tuple[Any, ...]
    ) -> dict[str, Any] | None:
        basin_version_id, canonical_grid_key, model_input_package_id, binding_checksum = parameters
        for row in self.db.model_instances:
            profile = row.get("resource_profile", {})
            direct_grid_forcing = profile.get("direct_grid_forcing", {})
            if (
                row["basin_version_id"] == basin_version_id
                and profile.get("canonical_grid_key") == canonical_grid_key
                and direct_grid_forcing.get("model_input_package_id") == model_input_package_id
                and direct_grid_forcing.get("binding_checksum") == binding_checksum
            ):
                return {"model_id": row["model_id"]}
        return None

    def _handle_model_instance_insert(
        self, statement: str, parameters: tuple[Any, ...]
    ) -> None:
        (
            model_id,
            basin_version_id,
            river_network_version_id,
            mesh_version_id,
            calibration_version_id,
            shud_code_version,
            model_package_uri,
            resource_profile_wrapped,
        ) = parameters
        # `active_flag=false` MUST appear literally in the INSERT column list —
        # never as a bound parameter — to match the precedent set by
        # `basins_registry_import._ensure_model_instance` and to make the
        # lifecycle owner (Change 8 activation flip) the sole authority for
        # `true`. Assert here so a future edit that flips it to a parameter
        # trips this test rather than silently drifting.
        normalized_stmt = " ".join(statement.split()).lower()
        assert ", false," in normalized_stmt, (
            "INSERT must carry active_flag=false as a literal, not a bound parameter"
        )
        # `lifecycle_state` MUST be OMITTED from the INSERT column list so the
        # column default ('inactive' per db/migrations/000022) applies.
        assert "lifecycle_state" not in normalized_stmt, (
            "INSERT must not name lifecycle_state so the 'inactive' default applies"
        )
        # Extract the underlying dict from psycopg2's Json wrapper.
        resource_profile = getattr(resource_profile_wrapped, "adapted", resource_profile_wrapped)
        if isinstance(resource_profile, str):
            resource_profile = json.loads(resource_profile)
        self.db.model_instances.append(
            {
                "model_id": model_id,
                "basin_version_id": basin_version_id,
                "river_network_version_id": river_network_version_id,
                "mesh_version_id": mesh_version_id,
                "calibration_version_id": calibration_version_id,
                "shud_code_version": shud_code_version,
                "model_package_uri": model_package_uri,
                "active_flag": False,
                "lifecycle_state": "inactive",
                "resource_profile": resource_profile,
            }
        )


# --- fixtures --------------------------------------------------------------


BASIN_VERSION_ID = "basin_v01"
GRID_SNAPSHOT_ID = "snapshot-uuid-01"
CANONICAL_GRID_KEY = "canonical_key_grid_a_v1"
GRID_ID = "grid_a"
GRID_SIGNATURE = "sha256:grid-signature-a"


def _baseline() -> DirectGridBaselineModelInputs:
    return DirectGridBaselineModelInputs(
        river_network_version_id="rnv_v01",
        mesh_version_id="mesh_v01",
        calibration_version_id="cal_v01",
        shud_code_version="basins-shud",
        model_package_uri="s3://nhms/models/demo/direct-grid/",
    )


def _direct_grid_payload(
    *,
    binding_checksum: str = "sha256:binding-a",
    model_input_package_id: str = "model-input-a-v1",
    applicable_source_ids: list[str] | None = None,
    grid_id: str = GRID_ID,
    grid_signature: str = GRID_SIGNATURE,
) -> dict[str, Any]:
    """A minimal, parser-valid direct-grid forcing payload."""

    return {
        "forcing_mapping_mode": "direct_grid",
        "binding_uri": "s3://nhms/models/demo/direct-grid/binding.json",
        "binding_checksum": binding_checksum,
        "model_input_package_id": model_input_package_id,
        "sp_att_path": "input/demo.sp.att",
        "sp_att_checksum": "sha256:sp-att",
        "applicable_source_ids": list(applicable_source_ids or ["GFS"]),
        "grid_id": grid_id,
        "grid_signature": grid_signature,
        "station_bindings": [
            {
                "station_id": "demo_forc_001",
                "shud_forcing_index": 1,
                "forcing_filename": "X100.95Y36.25.csv",
                "longitude": 100.95,
                "latitude": 36.25,
                "x": 1,
                "y": 2,
                "z": 3657,
                "grid_id": grid_id,
                "grid_cell_id": "cell-001",
            },
            {
                "station_id": "demo_forc_002",
                "shud_forcing_index": 2,
                "forcing_filename": "X101.05Y36.25.csv",
                "longitude": 101.05,
                "latitude": 36.25,
                "x": 2,
                "y": 3,
                "z": 3600,
                "grid_id": grid_id,
                "grid_cell_id": "cell-002",
            },
        ],
    }


def _make_input(
    *,
    basin_version_id: str = BASIN_VERSION_ID,
    payload: dict[str, Any] | None = None,
    grid_snapshot_id: str | None = None,
    grid_signature: str | None = None,
    grid_id: str | None = None,
) -> DirectGridVariantRegistrationInput:
    resolved_payload = payload if payload is not None else _direct_grid_payload()
    return DirectGridVariantRegistrationInput(
        basin_version_id=basin_version_id,
        direct_grid_forcing=resolved_payload,
        baseline=_baseline(),
        grid_snapshot_id=grid_snapshot_id,
        grid_signature=grid_signature,
        grid_id=grid_id,
    )


@pytest.fixture
def db() -> _InMemoryDb:
    store = _InMemoryDb()
    store.add_snapshot(
        grid_snapshot_id=GRID_SNAPSHOT_ID,
        canonical_grid_key=CANONICAL_GRID_KEY,
        grid_id=GRID_ID,
        grid_signature=GRID_SIGNATURE,
    )
    return store


@pytest.fixture
def cursor(db: _InMemoryDb) -> _FakeCursor:
    return _FakeCursor(db)


# --- Group A: registration semantics --------------------------------------


def test_inactive_row_registers_new_variant_with_expected_shape(
    db: _InMemoryDb, cursor: _FakeCursor
) -> None:
    """A registered variant is inserted as a new inactive row.

    Locks §1.1 evidence: new `model_id`, `active_flag=False`,
    `lifecycle_state='inactive'`, contract at
    `resource_profile.direct_grid_forcing`, and `canonical_grid_key` at
    top-level `resource_profile.canonical_grid_key`.
    """

    result = register_direct_grid_variant(
        cursor,
        _make_input(grid_snapshot_id=GRID_SNAPSHOT_ID),
    )

    assert result.inserted is True
    assert result.canonical_grid_key == CANONICAL_GRID_KEY
    assert result.grid_snapshot_id == GRID_SNAPSHOT_ID
    assert len(db.model_instances) == 1
    row = db.model_instances[0]
    assert row["model_id"] == result.model_id
    assert row["basin_version_id"] == BASIN_VERSION_ID
    assert row["active_flag"] is False
    assert row["lifecycle_state"] == "inactive"
    profile = row["resource_profile"]
    # canonical_grid_key sits at the TOP LEVEL of resource_profile, per D1.
    assert profile["canonical_grid_key"] == CANONICAL_GRID_KEY
    # The contract lives under resource_profile.direct_grid_forcing.
    assert "direct_grid_forcing" in profile
    # And canonical_grid_key MUST NOT be nested inside direct_grid_forcing.
    assert "canonical_grid_key" not in profile["direct_grid_forcing"]


def test_grain_dedups_same_basin_and_asset_identity(
    db: _InMemoryDb, cursor: _FakeCursor
) -> None:
    """Same grain + same asset identity → returns existing row, no insert."""

    first = register_direct_grid_variant(
        cursor, _make_input(grid_snapshot_id=GRID_SNAPSHOT_ID)
    )
    second = register_direct_grid_variant(
        cursor, _make_input(grid_snapshot_id=GRID_SNAPSHOT_ID)
    )

    assert first.inserted is True
    assert second.inserted is False
    assert first.model_id == second.model_id
    assert len(db.model_instances) == 1


def test_shared_key_yields_single_row_with_both_normalized_source_ids(
    db: _InMemoryDb, cursor: _FakeCursor
) -> None:
    """IFS + GFS sharing one canonical_grid_key + one asset identity share one row.

    The persisted `applicable_source_ids` MUST list BOTH normalized ids
    (`gfs` and `IFS` per `packages.common.source_identity.normalize_source_id`).
    """

    payload = _direct_grid_payload(applicable_source_ids=["GFS", "IFS"])
    result = register_direct_grid_variant(
        cursor,
        _make_input(payload=payload, grid_snapshot_id=GRID_SNAPSHOT_ID),
    )

    assert result.inserted is True
    assert len(db.model_instances) == 1
    persisted_sources = db.model_instances[0]["resource_profile"]["direct_grid_forcing"][
        "applicable_source_ids"
    ]
    # Order-insensitive check against normalized ids.
    assert set(persisted_sources) == {"gfs", "IFS"}


def test_separate_variants_when_canonical_grid_keys_differ(
    db: _InMemoryDb, cursor: _FakeCursor
) -> None:
    """Two registrations with DIFFERENT canonical_grid_keys → two rows."""

    other_snapshot_id = "snapshot-uuid-02"
    other_canonical_key = "canonical_key_grid_b_v1"
    other_grid_id = "grid_b"
    other_grid_signature = "sha256:grid-signature-b"
    db.add_snapshot(
        grid_snapshot_id=other_snapshot_id,
        canonical_grid_key=other_canonical_key,
        grid_id=other_grid_id,
        grid_signature=other_grid_signature,
    )
    payload_a = _direct_grid_payload()  # grid_id=grid_a, sig=…-a
    payload_b = _direct_grid_payload(
        grid_id=other_grid_id,
        grid_signature=other_grid_signature,
    )
    # Vary a station grid_id to match the payload_b grid_id (parser cross-check).
    payload_b["station_bindings"] = [
        {**station, "grid_id": other_grid_id} for station in payload_b["station_bindings"]
    ]

    result_a = register_direct_grid_variant(
        cursor,
        _make_input(payload=payload_a, grid_snapshot_id=GRID_SNAPSHOT_ID),
    )
    result_b = register_direct_grid_variant(
        cursor,
        _make_input(payload=payload_b, grid_snapshot_id=other_snapshot_id),
    )

    assert result_a.inserted is True
    assert result_b.inserted is True
    assert result_a.model_id != result_b.model_id
    assert len(db.model_instances) == 2
    persisted_keys = sorted(
        row["resource_profile"]["canonical_grid_key"] for row in db.model_instances
    )
    assert persisted_keys == sorted([CANONICAL_GRID_KEY, other_canonical_key])


def test_fix_forward_new_row_preserves_prior_generation(
    db: _InMemoryDb, cursor: _FakeCursor
) -> None:
    """Same grain, DIFFERENT `model_input_package_id` → new row; prior untouched.

    Locks D1/D11.2 fix-forward: M1 remains byte-identical after M1' registers.
    """

    payload_m1 = _direct_grid_payload(
        model_input_package_id="model-input-a-v1",
        binding_checksum="sha256:binding-a-v1",
    )
    payload_m1_prime = _direct_grid_payload(
        model_input_package_id="model-input-a-v2",
        binding_checksum="sha256:binding-a-v2",
    )
    m1_result = register_direct_grid_variant(
        cursor,
        _make_input(payload=payload_m1, grid_snapshot_id=GRID_SNAPSHOT_ID),
    )
    # Snapshot M1's persisted row for a byte-identical post-condition check.
    m1_row_before = json.loads(
        json.dumps(next(row for row in db.model_instances if row["model_id"] == m1_result.model_id))
    )

    m1_prime_result = register_direct_grid_variant(
        cursor,
        _make_input(payload=payload_m1_prime, grid_snapshot_id=GRID_SNAPSHOT_ID),
    )

    assert m1_prime_result.inserted is True
    assert m1_prime_result.model_id != m1_result.model_id
    assert len(db.model_instances) == 2
    m1_row_after = next(
        row for row in db.model_instances if row["model_id"] == m1_result.model_id
    )
    # Prior generation's row is byte-identical after the fix-forward registration.
    assert json.loads(json.dumps(m1_row_after)) == m1_row_before


# --- Group B: canonical_grid_key comes verbatim from the snapshot ---------


def test_key_from_snapshot_is_copied_verbatim_from_registered_row(
    db: _InMemoryDb, cursor: _FakeCursor
) -> None:
    """The persisted canonical_grid_key equals the snapshot row's value byte-for-byte.

    Uses a sentinel key that cannot be produced by any hashing or derivation
    over the input payload, so a passing assertion proves the registration
    surface COPIED the snapshot's key rather than recomputing it (design D1).
    """

    sentinel_key = "SENTINEL_KEY_ABC123"
    sentinel_snapshot_id = "snapshot-uuid-sentinel"
    sentinel_grid_id = "grid_sentinel"
    sentinel_signature = "sha256:sentinel-signature"
    db.add_snapshot(
        grid_snapshot_id=sentinel_snapshot_id,
        canonical_grid_key=sentinel_key,
        grid_id=sentinel_grid_id,
        grid_signature=sentinel_signature,
    )
    payload = _direct_grid_payload(
        grid_id=sentinel_grid_id, grid_signature=sentinel_signature
    )
    payload["station_bindings"] = [
        {**station, "grid_id": sentinel_grid_id} for station in payload["station_bindings"]
    ]

    result = register_direct_grid_variant(
        cursor,
        _make_input(payload=payload, grid_snapshot_id=sentinel_snapshot_id),
    )

    assert result.canonical_grid_key == sentinel_key
    row = next(row for row in db.model_instances if row["model_id"] == result.model_id)
    assert row["resource_profile"]["canonical_grid_key"] == sentinel_key


def test_key_from_snapshot_resolves_via_grid_signature_and_grid_id(
    db: _InMemoryDb, cursor: _FakeCursor
) -> None:
    """The signature/grid_id path resolves the snapshot without an explicit id.

    Locks the D1 alternative resolution path where the input only carries the
    built manifest's ``grid_signature``/``grid_id`` (no explicit
    ``grid_snapshot_id``).
    """

    result = register_direct_grid_variant(
        cursor,
        _make_input(grid_signature=GRID_SIGNATURE, grid_id=GRID_ID),
    )

    assert result.canonical_grid_key == CANONICAL_GRID_KEY
    assert result.grid_snapshot_id == GRID_SNAPSHOT_ID


# --- Group C: parser round-trip -------------------------------------------


def test_persisted_contract_round_trips_through_producer_parser(
    db: _InMemoryDb, cursor: _FakeCursor
) -> None:
    """`resource_profile.direct_grid_forcing` parses back via the runtime parser.

    Locks §1.1 evidence line 3: the emitted payload reuses the existing parser
    contract rather than a new shape. Failure here means the registration
    surface has silently drifted from
    `workers.forcing_producer.direct_grid_contract`.
    """

    result = register_direct_grid_variant(
        cursor, _make_input(grid_snapshot_id=GRID_SNAPSHOT_ID)
    )
    persisted = db.model_instances[0]["resource_profile"]["direct_grid_forcing"]

    contract = load_forcing_mapping_contract_from_manifest(persisted)

    assert contract is not None
    assert contract.forcing_mapping_mode == "direct_grid"
    assert contract.binding_checksum == persisted["binding_checksum"]
    assert contract.model_input_package_id == persisted["model_input_package_id"]
    assert result.inserted is True


def test_persisted_contract_round_trips_for_shared_binding_variant(
    db: _InMemoryDb, cursor: _FakeCursor
) -> None:
    """Shared-binding (GFS+IFS) payload also round-trips via the parser.

    The parser normalizes ``applicable_source_ids`` to a tuple; the persisted
    list form must survive a second parse call.
    """

    payload = _direct_grid_payload(applicable_source_ids=["GFS", "IFS"])
    register_direct_grid_variant(
        cursor,
        _make_input(payload=payload, grid_snapshot_id=GRID_SNAPSHOT_ID),
    )
    persisted = db.model_instances[0]["resource_profile"]["direct_grid_forcing"]

    contract = load_forcing_mapping_contract_from_manifest(persisted, source_id="IFS")

    assert contract is not None
    assert set(contract.applicable_source_ids) == {"gfs", "IFS"}


# --- error / edge behavior ------------------------------------------------


def test_missing_baseline_field_raises_before_insert(
    db: _InMemoryDb, cursor: _FakeCursor
) -> None:
    """A blank baseline field fails before any INSERT touches the cursor."""

    bad_baseline = DirectGridBaselineModelInputs(
        river_network_version_id="",  # blank triggers the failure
        mesh_version_id="mesh_v01",
        calibration_version_id="cal_v01",
        shud_code_version="basins-shud",
        model_package_uri="s3://nhms/models/demo/direct-grid/",
    )
    registration_input = DirectGridVariantRegistrationInput(
        basin_version_id=BASIN_VERSION_ID,
        direct_grid_forcing=_direct_grid_payload(),
        baseline=bad_baseline,
        grid_snapshot_id=GRID_SNAPSHOT_ID,
    )

    with pytest.raises(DirectGridVariantRegistrationError) as excinfo:
        register_direct_grid_variant(cursor, registration_input)

    assert excinfo.value.error_code == "DIRECT_GRID_VARIANT_BASELINE_MISSING"
    assert "river_network_version_id" in excinfo.value.details["missing_fields"]
    assert db.model_instances == []


def test_unresolved_snapshot_raises_domain_error(
    db: _InMemoryDb, cursor: _FakeCursor
) -> None:
    """A grid_snapshot_id that doesn't exist raises, never falls back to a blind INSERT."""

    with pytest.raises(DirectGridVariantRegistrationError) as excinfo:
        register_direct_grid_variant(
            cursor,
            _make_input(grid_snapshot_id="snapshot-does-not-exist"),
        )

    assert excinfo.value.error_code == "DIRECT_GRID_VARIANT_SNAPSHOT_NOT_FOUND"
    assert db.model_instances == []


def test_missing_snapshot_resolution_inputs_raises(
    db: _InMemoryDb, cursor: _FakeCursor
) -> None:
    """Neither `grid_snapshot_id` nor `(grid_signature + grid_id)` provided → error."""

    with pytest.raises(DirectGridVariantRegistrationError) as excinfo:
        register_direct_grid_variant(cursor, _make_input())

    assert excinfo.value.error_code == "DIRECT_GRID_VARIANT_SNAPSHOT_INPUT_MISSING"
    assert db.model_instances == []
