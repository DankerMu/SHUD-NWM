"""Unit tests for the direct-grid variant registration surface.

Covers Epic #961 / tasks.md §1.1 + §1.2 evidence for the two-leg registration
surface (`core.model_instance` insert + `met.met_station` mirror upsert):

* §1.1 evidence
  `-k "inactive_row or grain or shared_key or separate_variants or fix_forward_new_row"`
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

* §1.1 evidence `-k "key_from_snapshot"` proves the persisted
  ``resource_profile.canonical_grid_key`` equals the registered snapshot's
  ``canonical_grid_key`` byte-for-byte (verbatim copy, no re-derivation).

* §1.2 evidence
  `-k "mirror_inactive or station_id_form or grid_snapshot_binding or mvt_single_track or producer_shape"`
  proves every mirror row lands ``active_flag=false`` explicitly, with the
  ``<mapping_asset_identity>::cell:<grid_cell_id>`` station_id form, with
  ``grid_snapshot_id`` bound to the registered snapshot (while a legacy
  station keeps ``grid_snapshot_id IS NULL``), that the MVT-style
  ``basin_version_id=… AND active_flag=true`` query returns none of them,
  and that each row carries ``station_role='direct_grid_cache'`` plus the
  full derived-cache identity `properties_json` accepted by the producer's
  conditional-upsert predicate.

* §1.2 evidence `-k "mirror_collision_fails_closed"` proves a mirror write
  hitting an existing ``station_id`` with a different bound identity raises
  ``DIRECT_GRID_VARIANT_MIRROR_COLLISION`` and mutates no row (fail-closed,
  affected-row-count check; the foreign row's contents are byte-identical
  afterwards).

* §1.2 evidence `-k "no_forbidden_runtime_rows"` proves registration writes
  no `met.interp_weight`, `met.forcing_version`, cycle-dated `.tsd.forc`,
  or station weather CSV — only the `core.model_instance` variant row and
  the `met.met_station` mirror.

* Parser round-trip: the emitted ``resource_profile.direct_grid_forcing``
  block round-trips through
  ``workers.forcing_producer.direct_grid_contract.load_forcing_mapping_contract_from_manifest``
  without error.

The tests use an in-memory fake cursor that models the three tables the
registration surface touches — `met.canonical_grid_snapshot` (read),
`core.model_instance` (read + write), and `met.met_station` (write + read
for post-conditions) — by recognizing each SQL statement by fragment. Any
statement that would touch `met.interp_weight` or `met.forcing_version` is
rejected as forbidden (§1.2 non-goal). This locks the actual SQL shape while
remaining fast and transaction-free (real-DB coverage is a separate §5
evidence line).
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
    """Backing store for `_FakeCursor`, modeling the tables under test.

    Three tables:

    * ``snapshots``       - read source for ``met.canonical_grid_snapshot``
    * ``model_instances`` - read + write for ``core.model_instance``
    * ``met_stations``    - write + read for ``met.met_station`` mirror rows
                            (dict keyed on ``station_id`` so post-conditions
                            can read the mirror shape back)
    """

    def __init__(self) -> None:
        self.snapshots: list[dict[str, Any]] = []
        self.model_instances: list[dict[str, Any]] = []
        self.met_stations: dict[str, dict[str, Any]] = {}

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

    def add_met_station(
        self,
        *,
        station_id: str,
        basin_version_id: str,
        station_role: str = "forcing_proxy",
        active_flag: bool = True,
        properties_json: dict[str, Any] | None = None,
        grid_snapshot_id: str | None = None,
        longitude: float = 0.0,
        latitude: float = 0.0,
        elevation_m: float = 0.0,
        station_name: str | None = None,
    ) -> None:
        """Seed a pre-existing ``met.met_station`` row (foreign-row fixture).

        Used by ``mirror_collision_fails_closed`` to place a row whose bound
        identity differs from the incoming variant, and by
        ``grid_snapshot_binding`` to place a legacy IDW station.
        """

        self.met_stations[station_id] = {
            "station_id": station_id,
            "basin_version_id": basin_version_id,
            "station_name": station_name,
            "longitude": longitude,
            "latitude": latitude,
            "elevation_m": elevation_m,
            "station_role": station_role,
            "active_flag": active_flag,
            "properties_json": dict(properties_json or {}),
            "grid_snapshot_id": grid_snapshot_id,
        }


#: Table names whose appearance in ANY statement is a §1.2 non-goal — writing
#: them from registration would break the "only core.model_instance + met.met_station"
#: guarantee (spec Scenario: Registration writes no runtime-producer rows).
_FORBIDDEN_TABLE_FRAGMENTS: tuple[str, ...] = (
    "met.interp_weight",
    "met.forcing_version",
    ".tsd.forc",
    "station_inventory",
)


class _FakeCursor:
    """SQL-fragment-recognizing fake cursor for the direct-grid registration.

    Handles five statement kinds:

    1. ``SELECT ... FROM met.canonical_grid_snapshot WHERE grid_snapshot_id = %s``
    2. ``SELECT ... FROM met.canonical_grid_snapshot WHERE grid_id = %s AND grid_signature = %s``
    3. ``SELECT model_id FROM core.model_instance WHERE ... (JSONB path lookup)``
    4. ``INSERT INTO core.model_instance ...``
    5. ``INSERT INTO met.met_station ... ON CONFLICT (station_id) DO UPDATE ...``
       (per-row emit; the fake evaluates the DO UPDATE ``WHERE`` predicate
       against the in-memory row and reports ``rowcount==0`` on mismatch to
       trigger the fail-closed collision path).

    Any statement touching :data:`_FORBIDDEN_TABLE_FRAGMENTS` (interp_weight,
    forcing_version, .tsd.forc, station_inventory) fails loudly to catch a
    §1.2 non-goal regression. Any unrecognized statement also fails loudly.

    ``rowcount`` mirrors psycopg2's semantics for the two writer paths — 1
    for a happy `core.model_instance` INSERT, 1 for a happy mirror
    INSERT-or-matching-DO-UPDATE, 0 for a mirror WHERE-predicate rejection.
    """

    def __init__(self, db: _InMemoryDb) -> None:
        self.db = db
        self._pending: dict[str, Any] | None = None
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.rowcount: int = 0

    def execute(self, statement: str, parameters: tuple[Any, ...] = ()) -> None:
        self.statements.append((statement, tuple(parameters)))
        normalized = " ".join(statement.split()).lower()
        for forbidden in _FORBIDDEN_TABLE_FRAGMENTS:
            if forbidden in normalized:
                raise AssertionError(
                    f"Registration must not touch {forbidden!r} (§1.2 non-goal): {statement!r}"
                )
        if "from met.canonical_grid_snapshot" in normalized:
            self._pending = self._handle_snapshot_select(normalized, parameters)
            # SELECT paths conventionally report rowcount=1 on hit / 0 on miss.
            self.rowcount = 0 if self._pending is None else 1
            return
        if "from core.model_instance" in normalized and "resource_profile" in normalized:
            self._pending = self._handle_model_instance_select(parameters)
            self.rowcount = 0 if self._pending is None else 1
            return
        if "insert into core.model_instance" in normalized:
            self._pending = None
            self._handle_model_instance_insert(statement, parameters)
            self.rowcount = 1
            return
        if "insert into met.met_station" in normalized:
            self._pending = None
            self.rowcount = self._handle_met_station_upsert(statement, parameters)
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

    def _handle_met_station_upsert(
        self, statement: str, parameters: tuple[Any, ...]
    ) -> int:
        """Model the per-row mirror INSERT ... ON CONFLICT DO UPDATE.

        Returns the affected-row count psycopg2 would report:

        * ``1`` when the row is inserted (no conflict) OR updated (WHERE
          predicate matched)
        * ``0`` when the WHERE predicate rejects the existing row
          (station_id collision on a different bound identity)

        The fake also locks the load-bearing SQL invariants that make the
        upsert fail-closed:

        * ``active_flag=false`` is a LITERAL (never bound), matching the
          §D2 flag ownership boundary.
        * ``active_flag`` MUST NOT appear in the ``DO UPDATE SET`` list —
          it stays owned by registration until Change 8's cutover flip.
        * ``grid_snapshot_id`` MUST appear in both the SET list and the
          WHERE predicate (SUB-2 discriminator FK).
        """

        (
            station_id,
            basin_version_id,
            station_name,
            longitude,
            latitude,
            elevation_m,
            properties_wrapped,
            grid_snapshot_id,
        ) = parameters

        normalized = " ".join(statement.split()).lower()
        # active_flag=false MUST be a literal on the INSERT list.
        assert " false," in normalized or "false," in normalized, (
            "mirror INSERT must carry active_flag=false as a literal"
        )
        # station_role='direct_grid_cache' MUST be a literal — not the
        # 'forcing_proxy' column default (db/migrations/000005_met.sql:53).
        assert "'direct_grid_cache'" in normalized, (
            "mirror INSERT must carry station_role='direct_grid_cache' as a literal"
        )
        # The DO UPDATE SET list MUST NOT include `active_flag = ...` —
        # otherwise the runtime producer's flag flip could regress here.
        set_clause = normalized.split(" do update set ", 1)[1].split(" where ", 1)[0]
        assert "active_flag" not in set_clause, (
            "mirror DO UPDATE SET must not touch active_flag (§D2 flag ownership)"
        )
        # `grid_snapshot_id` MUST appear in both SET and WHERE (SUB-2).
        where_clause = normalized.split(" where ", 1)[1]
        assert "grid_snapshot_id" in set_clause, (
            "mirror DO UPDATE SET must refresh grid_snapshot_id"
        )
        assert "grid_snapshot_id" in where_clause, (
            "mirror DO UPDATE WHERE must gate on grid_snapshot_id equality"
        )

        properties = getattr(properties_wrapped, "adapted", properties_wrapped)
        if isinstance(properties, str):
            properties = json.loads(properties)
        assert isinstance(properties, dict)

        incoming = {
            "station_id": station_id,
            "basin_version_id": basin_version_id,
            "station_name": station_name,
            "longitude": longitude,
            "latitude": latitude,
            "elevation_m": elevation_m,
            "station_role": "direct_grid_cache",
            "active_flag": False,
            "properties_json": properties,
            "grid_snapshot_id": grid_snapshot_id,
        }

        existing = self.db.met_stations.get(station_id)
        if existing is None:
            self.db.met_stations[station_id] = incoming
            return 1

        # Evaluate the DO UPDATE WHERE predicate against the existing row.
        # A predicate mismatch means station_id collision on a different
        # bound identity → the row is NOT updated → rowcount == 0.
        existing_props = existing.get("properties_json") or {}
        predicate_ok = (
            existing.get("basin_version_id") == basin_version_id
            and existing.get("station_role") == "direct_grid_cache"
            and existing_props.get("derived_cache") is True
            and existing_props.get("forcing_mapping_mode") == "direct_grid"
            and existing_props.get("binding_checksum")
            == properties.get("binding_checksum")
            and existing_props.get("model_input_package_id")
            == properties.get("model_input_package_id")
            and existing_props.get("grid_signature")
            == properties.get("grid_signature")
            and existing_props.get("contract_grid_id")
            == properties.get("contract_grid_id")
            and existing_props.get("grid_id") == properties.get("grid_id")
            and existing.get("grid_snapshot_id") == grid_snapshot_id
        )
        if not predicate_ok:
            # DO NOT mutate the row. Fail-closed collision.
            return 0

        # Reconcile the row (preserving `active_flag`).
        preserved_flag = existing["active_flag"]
        self.db.met_stations[station_id] = {**incoming, "active_flag": preserved_flag}
        return 1


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


def _rebind_station_ids(payload: dict[str, Any], identity: str) -> None:
    """Rewrite each station's ``station_id`` to embed ``identity``.

    Mirrors what
    ``workers/mapping_builder/binding.py:assign_station_id_from_mapping_asset_identity``
    stamps upstream, so tests that register two different variants get
    disjoint mirror station_ids by construction (the real fix-forward /
    separate-variant paths never share ``mapping_asset_identity`` and so
    never share station_ids).
    """

    payload["station_bindings"] = [
        {**station, "station_id": f"{identity}::cell:{station['grid_cell_id']}"}
        for station in payload["station_bindings"]
    ]


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
    _rebind_station_ids(payload_a, "mai_variant_a")
    payload_b = _direct_grid_payload(
        grid_id=other_grid_id,
        grid_signature=other_grid_signature,
    )
    # Vary a station grid_id to match the payload_b grid_id (parser cross-check).
    payload_b["station_bindings"] = [
        {**station, "grid_id": other_grid_id} for station in payload_b["station_bindings"]
    ]
    _rebind_station_ids(payload_b, "mai_variant_b")

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
    A fix-forward rebuild produces a NEW ``mapping_asset_identity`` (new
    builder run), so the two variants have disjoint station_ids and the
    mirror emission for M1' does not collide on M1's mirror rows — mirroring
    the real upstream contract.
    """

    payload_m1 = _direct_grid_payload(
        model_input_package_id="model-input-a-v1",
        binding_checksum="sha256:binding-a-v1",
    )
    _rebind_station_ids(payload_m1, "mai_m1")
    payload_m1_prime = _direct_grid_payload(
        model_input_package_id="model-input-a-v2",
        binding_checksum="sha256:binding-a-v2",
    )
    _rebind_station_ids(payload_m1_prime, "mai_m1_prime")
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


# --- §1.2 / SUB-2: met.met_station cell-station mirror write --------------
#
# The §1.2 tests use station bindings whose ``station_id`` follows the
# ``<mapping_asset_identity>::cell:<grid_cell_id>`` form that
# ``workers/mapping_builder/binding.py:assign_station_id_from_mapping_asset_identity``
# produces upstream. The parser accepts arbitrary station_id strings, so the
# form is a caller-side contract (the mapping builder stamps it during binding
# assembly) that the mirror write MUST preserve verbatim — hence the shape
# lock in ``test_station_id_form_matches_mapping_asset_identity``.


MAPPING_ASSET_IDENTITY = "mai_direct_grid_v1_abc123"
CELL_ID_1 = "cell-001"
CELL_ID_2 = "cell-002"
STATION_ID_1 = f"{MAPPING_ASSET_IDENTITY}::cell:{CELL_ID_1}"
STATION_ID_2 = f"{MAPPING_ASSET_IDENTITY}::cell:{CELL_ID_2}"


def _mirror_ready_payload(
    *,
    binding_checksum: str = "sha256:mirror-binding-a",
    model_input_package_id: str = "mirror-model-input-a-v1",
) -> dict[str, Any]:
    """A parser-valid payload whose station_ids follow the SUB-2 form."""

    payload = _direct_grid_payload(
        binding_checksum=binding_checksum,
        model_input_package_id=model_input_package_id,
    )
    payload["station_bindings"] = [
        {
            "station_id": STATION_ID_1,
            "shud_forcing_index": 1,
            "forcing_filename": "X100.95Y36.25.csv",
            "longitude": 100.95,
            "latitude": 36.25,
            "x": 1,
            "y": 2,
            "z": 3657,
            "grid_id": GRID_ID,
            "grid_cell_id": CELL_ID_1,
        },
        {
            "station_id": STATION_ID_2,
            "shud_forcing_index": 2,
            "forcing_filename": "X101.05Y36.25.csv",
            "longitude": 101.05,
            "latitude": 36.25,
            "x": 2,
            "y": 3,
            "z": 3600,
            "grid_id": GRID_ID,
            "grid_cell_id": CELL_ID_2,
        },
    ]
    return payload


_REQUIRED_MIRROR_PROPERTIES: tuple[str, ...] = (
    "derived_cache",
    "forcing_mapping_mode",
    "direct_grid",
    "manifest_authority",
    "binding_checksum",
    "binding_uri",
    "model_input_package_id",
    "sp_att_path",
    "sp_att_checksum",
    "grid_id",
    "contract_grid_id",
    "grid_cell_id",
    "grid_signature",
    "shud_forcing_index",
    "forcing_filename",
    "x",
    "y",
    "z",
    "mirror_identity",
)


def test_mirror_inactive_rows_written_with_explicit_false_flag(
    db: _InMemoryDb, cursor: _FakeCursor
) -> None:
    """Every mirror row lands with ``active_flag=False`` set explicitly.

    Locks the §D2 flag ownership boundary: registration owns the ``False``
    landing; Change 8's cutover owns the flip to ``True``. If a future edit
    ever binds ``active_flag`` from user input or defaults to the column's
    ``true``, this test fires.
    """

    payload = _mirror_ready_payload()
    result = register_direct_grid_variant(
        cursor,
        _make_input(payload=payload, grid_snapshot_id=GRID_SNAPSHOT_ID),
    )

    assert result.inserted is True
    assert result.mirror_stations_written == 2
    assert len(db.met_stations) == 2
    for station_id in (STATION_ID_1, STATION_ID_2):
        row = db.met_stations[station_id]
        assert row["active_flag"] is False, (
            f"mirror row {station_id!r} must land active_flag=False (§D2)"
        )


def test_station_id_form_matches_mapping_asset_identity(
    db: _InMemoryDb, cursor: _FakeCursor
) -> None:
    """Each mirror row's station_id follows ``<identity>::cell:<grid_cell_id>``.

    The mapping builder stamps this form during binding assembly
    (``assign_station_id_from_mapping_asset_identity``); the mirror write
    MUST preserve it verbatim so the DB mirror can detect version-reuse
    (docs §7.4).
    """

    payload = _mirror_ready_payload()
    register_direct_grid_variant(
        cursor,
        _make_input(payload=payload, grid_snapshot_id=GRID_SNAPSHOT_ID),
    )

    for station_id, expected_cell_id in (
        (STATION_ID_1, CELL_ID_1),
        (STATION_ID_2, CELL_ID_2),
    ):
        assert station_id in db.met_stations
        # Parse the form back to prove the separator is intact.
        identity_part, _, cell_part = station_id.partition("::cell:")
        assert identity_part == MAPPING_ASSET_IDENTITY
        assert cell_part == expected_cell_id
        # And the row's properties_json carries the matching grid_cell_id.
        assert (
            db.met_stations[station_id]["properties_json"]["grid_cell_id"]
            == expected_cell_id
        )


def test_grid_snapshot_binding_populated_on_mirror_but_null_on_legacy(
    db: _InMemoryDb, cursor: _FakeCursor
) -> None:
    """Mirror rows carry ``grid_snapshot_id``; a legacy IDW row stays NULL.

    Locks the SUB-2 discriminator FK: after registration, the mirror rows
    reference the variant's registered snapshot, while a pre-existing
    legacy IDW station in the same basin retains ``grid_snapshot_id IS
    NULL`` — so the two station sets are distinguishable without a
    ``model_id`` column (spec Scenario "Mirror rows are bound to the
    canonical grid snapshot").
    """

    # Seed a legacy IDW station: same basin, NULL grid_snapshot_id.
    db.add_met_station(
        station_id="legacy_forcing_proxy_001",
        basin_version_id=BASIN_VERSION_ID,
        station_role="forcing_proxy",
        active_flag=True,
        properties_json={},
        grid_snapshot_id=None,
    )

    payload = _mirror_ready_payload()
    register_direct_grid_variant(
        cursor,
        _make_input(payload=payload, grid_snapshot_id=GRID_SNAPSHOT_ID),
    )

    for station_id in (STATION_ID_1, STATION_ID_2):
        assert db.met_stations[station_id]["grid_snapshot_id"] == GRID_SNAPSHOT_ID
    legacy = db.met_stations["legacy_forcing_proxy_001"]
    assert legacy["grid_snapshot_id"] is None
    assert legacy["station_role"] == "forcing_proxy"


def test_mvt_single_track_query_excludes_new_mirror_rows(
    db: _InMemoryDb, cursor: _FakeCursor
) -> None:
    """The MVT-style ``basin_version_id=… AND active_flag=true`` returns none.

    Simulates the station-MVT query (Scenario "The mirror never lands
    active at registration"). None of the newly registered mirror rows
    appear because they are ``active_flag=false`` (§D2). This guards the
    shadow-window display single-track invariant.
    """

    # Seed an ACTIVE legacy station so the MVT query has something to return.
    db.add_met_station(
        station_id="legacy_active_001",
        basin_version_id=BASIN_VERSION_ID,
        station_role="forcing_proxy",
        active_flag=True,
        properties_json={},
        grid_snapshot_id=None,
    )
    payload = _mirror_ready_payload()
    register_direct_grid_variant(
        cursor,
        _make_input(payload=payload, grid_snapshot_id=GRID_SNAPSHOT_ID),
    )

    # Simulate: SELECT ... WHERE basin_version_id=%s AND active_flag=true
    visible_to_mvt = [
        row
        for row in db.met_stations.values()
        if row["basin_version_id"] == BASIN_VERSION_ID and row["active_flag"] is True
    ]
    visible_station_ids = {row["station_id"] for row in visible_to_mvt}
    # Neither mirror row is visible to the MVT query.
    assert STATION_ID_1 not in visible_station_ids
    assert STATION_ID_2 not in visible_station_ids
    # The legacy active station IS visible.
    assert "legacy_active_001" in visible_station_ids


def test_producer_shape_mirror_row_matches_conditional_upsert_predicate(
    db: _InMemoryDb, cursor: _FakeCursor
) -> None:
    """Each mirror row carries the producer's derived-cache identity contract.

    Locks parity with
    ``workers/forcing_producer/store.py:ensure_direct_grid_met_stations``
    (lines 347-379). Two independent shape checks:

    1. Every required properties_json key is present.
    2. Simulating the producer's conditional-upsert ``WHERE`` predicate
       against the persisted row + a fresh producer-shaped incoming row
       yields ``True`` (so a subsequent runtime producer run reconciles
       rather than fails closed with its collision error, per
       Scenario "Mirror rows carry the producer's derived-cache identity
       shape").
    """

    payload = _mirror_ready_payload()
    register_direct_grid_variant(
        cursor,
        _make_input(payload=payload, grid_snapshot_id=GRID_SNAPSHOT_ID),
    )

    for station_id in (STATION_ID_1, STATION_ID_2):
        row = db.met_stations[station_id]
        assert row["station_role"] == "direct_grid_cache"
        props = row["properties_json"]
        for key in _REQUIRED_MIRROR_PROPERTIES:
            assert key in props, (
                f"mirror row {station_id!r} missing required properties key {key!r}"
            )
        assert props["derived_cache"] is True
        assert props["forcing_mapping_mode"] == "direct_grid"
        assert props["direct_grid"] is True
        assert props["manifest_authority"] is True
        assert props["binding_checksum"] == "sha256:mirror-binding-a"
        assert props["model_input_package_id"] == "mirror-model-input-a-v1"

        # Simulate the producer's `_direct_grid_mirror_identity` shape and
        # the WHERE predicate reconciliation: an incoming row with the same
        # identity fields would match the persisted row.
        incoming_identity = {
            "binding_checksum": props["binding_checksum"],
            "model_input_package_id": props["model_input_package_id"],
            "grid_signature": props["grid_signature"],
            "contract_grid_id": props["contract_grid_id"],
            "grid_id": props["grid_id"],
        }
        stored_identity = props["mirror_identity"]
        assert stored_identity == incoming_identity


# --- §1.2 Group B: mirror_collision_fails_closed --------------------------


def test_mirror_collision_fails_closed_on_conflicting_identity(
    db: _InMemoryDb, cursor: _FakeCursor
) -> None:
    """A foreign row on the same station_id → raise; foreign row unchanged.

    Fail-closed collision policy (docs §7.4 + spec Scenario "A station_id
    collision with a different bound identity fails closed"). The foreign
    row's contents are byte-identical afterwards (deep dict compare) — no
    silent clobber.
    """

    foreign_props = {
        "derived_cache": True,
        "forcing_mapping_mode": "direct_grid",
        # A DIFFERENT binding_checksum than what the registration will emit.
        "binding_checksum": "sha256:FOREIGN-binding",
        "model_input_package_id": "mirror-model-input-a-v1",
        "grid_signature": GRID_SIGNATURE,
        "contract_grid_id": GRID_ID,
        "grid_id": GRID_ID,
    }
    db.add_met_station(
        station_id=STATION_ID_1,
        basin_version_id=BASIN_VERSION_ID,
        station_role="direct_grid_cache",
        active_flag=False,
        properties_json=foreign_props,
        grid_snapshot_id=GRID_SNAPSHOT_ID,
        longitude=99.99,
        latitude=35.55,
        elevation_m=1234.5,
        station_name="Foreign station",
    )
    foreign_snapshot_before = json.loads(json.dumps(db.met_stations[STATION_ID_1]))

    payload = _mirror_ready_payload(
        binding_checksum="sha256:mirror-binding-INCOMING",
    )

    with pytest.raises(DirectGridVariantRegistrationError) as excinfo:
        register_direct_grid_variant(
            cursor,
            _make_input(payload=payload, grid_snapshot_id=GRID_SNAPSHOT_ID),
        )

    assert excinfo.value.error_code == "DIRECT_GRID_VARIANT_MIRROR_COLLISION"
    assert excinfo.value.details.get("station_id") == STATION_ID_1

    # The foreign row is byte-identical after the failed registration.
    foreign_snapshot_after = json.loads(json.dumps(db.met_stations[STATION_ID_1]))
    assert foreign_snapshot_after == foreign_snapshot_before


def test_mirror_collision_fails_closed_leaves_second_row_unwritten(
    db: _InMemoryDb, cursor: _FakeCursor
) -> None:
    """A collision on row 1 aborts before row 2 is written.

    The per-row loop raises on the first mismatched row, so the second
    station's mirror row never lands. Downstream callers relying on a
    caller-managed transaction see no partial mirror emission.
    """

    db.add_met_station(
        station_id=STATION_ID_1,
        basin_version_id=BASIN_VERSION_ID,
        station_role="direct_grid_cache",
        active_flag=False,
        properties_json={
            "derived_cache": True,
            "forcing_mapping_mode": "direct_grid",
            "binding_checksum": "sha256:FOREIGN-binding",
            "model_input_package_id": "mirror-model-input-a-v1",
            "grid_signature": GRID_SIGNATURE,
            "contract_grid_id": GRID_ID,
            "grid_id": GRID_ID,
        },
        grid_snapshot_id=GRID_SNAPSHOT_ID,
    )

    payload = _mirror_ready_payload(
        binding_checksum="sha256:mirror-binding-INCOMING",
    )

    with pytest.raises(DirectGridVariantRegistrationError):
        register_direct_grid_variant(
            cursor,
            _make_input(payload=payload, grid_snapshot_id=GRID_SNAPSHOT_ID),
        )

    # Row 2 was never written.
    assert STATION_ID_2 not in db.met_stations


# --- §1.2 Group C: no_forbidden_runtime_rows ------------------------------


def test_no_forbidden_runtime_rows_written_during_registration(
    db: _InMemoryDb, cursor: _FakeCursor
) -> None:
    """Registration writes ZERO ``met.interp_weight`` / ``met.forcing_version`` rows.

    The fake cursor's ``_FORBIDDEN_TABLE_FRAGMENTS`` guard raises
    ``AssertionError`` on any statement touching those tables (or
    ``.tsd.forc`` / ``station_inventory``), so a passing registration
    proves no forbidden statement was issued (spec Scenario "Registration
    writes no runtime-producer rows"). Also asserts positively that only
    the two allowed tables were touched.
    """

    payload = _mirror_ready_payload()
    register_direct_grid_variant(
        cursor,
        _make_input(payload=payload, grid_snapshot_id=GRID_SNAPSHOT_ID),
    )

    # Positive assertion: every statement targets ONE of the allowed tables.
    allowed_targets = ("met.canonical_grid_snapshot", "core.model_instance", "met.met_station")
    for statement, _params in cursor.statements:
        normalized = " ".join(statement.split()).lower()
        assert any(target in normalized for target in allowed_targets), (
            f"registration issued statement against an unexpected target: {statement!r}"
        )
        # Belt-and-braces: none of the forbidden tables appear.
        for forbidden in _FORBIDDEN_TABLE_FRAGMENTS:
            assert forbidden not in normalized, (
                f"forbidden target {forbidden!r} appeared in statement: {statement!r}"
            )
