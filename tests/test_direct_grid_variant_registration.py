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

* §1.3 evidence
  `-k "legacy_retained or legacy_stays_active or idempotent"`
  proves legacy-row retention (INV-1: the legacy IDW `core.model_instance`
  row is byte-identical before/after registration) and that the currently
  active legacy model stays active across variant registration, and that
  re-registration idempotency keys on the `(model_input_package_id,
  binding_checksum)` built-asset identity: same asset returns the existing
  `model_id` with no duplicate row and no duplicate mirror (emit-always
  self-heal reports `mirror_stations_written == 2` with byte-identical
  mirror rows post-reconciliation), while a different
  `model_input_package_id` OR a different `binding_checksum` alone mints a
  NEW row with the prior generation's row + mirror rows byte-identical
  (fix-forward not swallowed by idempotency).

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
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from workers.forcing_producer.direct_grid_contract import (
    load_forcing_mapping_contract_from_manifest,
)
from workers.forcing_producer.direct_grid_contract import (
    parse_direct_grid_forcing_contract as _parse_direct_grid_contract,
)
from workers.forcing_producer.store import (
    DIRECT_GRID_CACHE_STATION_ROLE,
    MetStoreError,
    PsycopgForcingRepository,
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


# --- §1.3 / SUB-3: legacy retention + idempotency LOCKS -------------------
#
# These tests LOCK three §1.3 evidence claims from tasks.md:
#
# * legacy_retained  — the legacy IDW `core.model_instance` row is byte-identical
#   before and after registration (INV-1: legacy row is retained forever, never
#   mutated). Enforcement mechanism: `_lookup_existing_variant` filters on
#   `resource_profile->>'canonical_grid_key'` and
#   `resource_profile->'direct_grid_forcing'->>'model_input_package_id'`/
#   `binding_checksum`, all of which are NULL on a legacy row (whose
#   `resource_profile` is the `basins_registry_import._resource_profile`
#   shape). NULL = <string> is NULL (not TRUE) → legacy rows fall out of the
#   grain query without a per-row exclusion clause. The INSERT/mirror legs
#   only mutate their own new rows, so INV-1 holds by construction.
#
# * legacy_stays_active — a legacy row with `active_flag=true` /
#   `lifecycle_state='active'` remains so after variant registration, and
#   the newly-registered variant lands inactive (§D2 flag ownership plus
#   the "Registration does not deactivate the currently active legacy
#   model" scenario).
#
# * idempotent — three sub-cases:
#   - Same-asset re-registration returns the existing `model_id`, appends
#     no duplicate `core.model_instance` row, appends no duplicate
#     `met.met_station` row, and (this is the SUB-3-scoped mirror
#     retention lock that Phase 4.5 REFUTED from PR #1004) reports
#     `mirror_stations_written == 2` on the emit-always self-heal path
#     while every mirror row remains byte-identical after the DO UPDATE
#     reconciliation.
#   - A different `model_input_package_id` for the SAME grain registers as
#     a NEW row (fix-forward not swallowed by idempotency); the prior
#     generation's variant row AND its mirror rows are byte-identical
#     afterwards, and the successor's mirror rows land alongside on
#     disjoint station_ids.
#   - A different `binding_checksum` alone also mints a new row, proving
#     the identity is `(model_input_package_id, binding_checksum)` — not
#     `model_input_package_id` alone.


def _seed_legacy_model_instance(
    db: _InMemoryDb,
    *,
    model_id: str = "legacy_idw_m0",
    basin_version_id: str = BASIN_VERSION_ID,
    active_flag: bool = False,
    lifecycle_state: str = "inactive",
    model_package_uri: str = "s3://nhms/models/legacy/idw/",
) -> None:
    """Seed a pre-existing legacy IDW ``core.model_instance`` row.

    The ``resource_profile`` shape mirrors
    ``workers/model_registry/basins_registry_import.py:_resource_profile``:
    it has NO ``canonical_grid_key`` at the top level and NO
    ``direct_grid_forcing`` block, so the direct-grid registration's JSONB
    grain query (``resource_profile->>'canonical_grid_key' = %s AND
    resource_profile->'direct_grid_forcing'->>'model_input_package_id' = %s
    AND ...``) yields NULL on every path and skips the legacy row without a
    per-row exclusion clause.
    """

    db.model_instances.append(
        {
            "model_id": model_id,
            "basin_version_id": basin_version_id,
            "river_network_version_id": "rnv_v01",
            "mesh_version_id": "mesh_v01",
            "calibration_version_id": "cal_v01",
            "shud_code_version": "basins-shud",
            "model_package_uri": model_package_uri,
            "active_flag": active_flag,
            "lifecycle_state": lifecycle_state,
            "resource_profile": {
                "scheduler": "slurm",
                "partition": "standard",
                "nodes": 1,
                "ntasks": 1,
                "cpus_per_task": 4,
                "memory_mb": 8192,
                "walltime_minutes": 720,
                "lineage": "basins_registry_import",
                "basin_slug": "demo-legacy",
                "shud_input_name": "demo_legacy_input",
            },
        }
    )


def test_legacy_retained_row_bytes_identical_after_register(
    db: _InMemoryDb, cursor: _FakeCursor
) -> None:
    """INV-1 lock: the legacy IDW row is byte-identical after registration.

    Seeds an inactive legacy row (matching the `basins_registry_import`
    shape) alongside the snapshot fixture, snapshots the row's fields via
    `json.dumps`/`json.loads` deep-copy, registers a direct-grid variant,
    and asserts the legacy row's `model_id`, `active_flag`,
    `lifecycle_state`, `model_package_uri`, and `resource_profile` are
    byte-identical after registration — the spec scenario "Legacy row is
    untouched by variant registration".
    """

    _seed_legacy_model_instance(
        db,
        model_id="legacy_idw_m0",
        active_flag=False,
        lifecycle_state="inactive",
        model_package_uri="s3://nhms/models/legacy/idw-v0/",
    )
    legacy_before = json.loads(
        json.dumps(next(row for row in db.model_instances if row["model_id"] == "legacy_idw_m0"))
    )

    payload = _mirror_ready_payload()
    result = register_direct_grid_variant(
        cursor,
        _make_input(payload=payload, grid_snapshot_id=GRID_SNAPSHOT_ID),
    )

    assert result.inserted is True
    # Legacy row still exists and is byte-identical field-by-field.
    legacy_after_row = next(
        row for row in db.model_instances if row["model_id"] == "legacy_idw_m0"
    )
    legacy_after = json.loads(json.dumps(legacy_after_row))
    assert legacy_after == legacy_before
    # Per-field explicit assertions per §1.3 evidence line 14.
    assert legacy_after["model_id"] == legacy_before["model_id"]
    assert legacy_after["active_flag"] == legacy_before["active_flag"]
    assert legacy_after["lifecycle_state"] == legacy_before["lifecycle_state"]
    assert legacy_after["model_package_uri"] == legacy_before["model_package_uri"]
    assert legacy_after["resource_profile"] == legacy_before["resource_profile"]
    # The newly-registered variant is a DIFFERENT row.
    assert result.model_id != "legacy_idw_m0"
    assert len(db.model_instances) == 2


def test_legacy_stays_active_across_variant_register(
    db: _InMemoryDb, cursor: _FakeCursor
) -> None:
    """An `active` legacy model stays active after variant registration.

    Locks the "Registration does not deactivate the currently active
    legacy model" scenario: seeding an active legacy row and registering a
    direct-grid variant leaves the legacy row `active_flag=True`,
    `lifecycle_state='active'`, and the new variant lands
    `active_flag=False` / `lifecycle_state='inactive'` (no accidental
    activation, no accidental supersede).
    """

    _seed_legacy_model_instance(
        db,
        model_id="legacy_idw_active",
        active_flag=True,
        lifecycle_state="active",
    )

    payload = _mirror_ready_payload()
    result = register_direct_grid_variant(
        cursor,
        _make_input(payload=payload, grid_snapshot_id=GRID_SNAPSHOT_ID),
    )

    assert result.inserted is True
    legacy_after = next(
        row for row in db.model_instances if row["model_id"] == "legacy_idw_active"
    )
    assert legacy_after["active_flag"] is True
    assert legacy_after["lifecycle_state"] == "active"
    # The newly-registered variant is inactive — no accidental activation.
    variant_row = next(row for row in db.model_instances if row["model_id"] == result.model_id)
    assert variant_row["active_flag"] is False
    assert variant_row["lifecycle_state"] == "inactive"
    # Exactly two rows: legacy + new variant, no duplicate legacy row minted.
    assert len(db.model_instances) == 2


def test_idempotent_same_asset_returns_existing_identity_no_duplicate_mirror(
    db: _InMemoryDb, cursor: _FakeCursor
) -> None:
    """Second registration of the SAME asset is idempotent end-to-end.

    Locks the SUB-3 emit-always self-heal contract:

    * The second call returns the SAME `model_id` the first call minted
      (identity is stable across re-registration).
    * No duplicate `core.model_instance` row lands (grain-level dedup).
    * No duplicate `met.met_station` row lands (mirror-level dedup — same
      station_id reconciled via ``ON CONFLICT (station_id) DO UPDATE``).
    * The second `RegistrationResult.mirror_stations_written` is 2, NOT 0
      — the SUB-3-scoped mirror-emit-always self-heal fix that Phase 4.5
      REFUTED from PR #1004 (`inserted=False` runs the mirror leg because
      a prior partial run's mirror row could still be missing).
    * Every `met.met_station` row is byte-identical before-vs-after the
      second call: DO UPDATE reconciliation touched only same-value
      identity fields, and `active_flag` is preserved (not in the SET
      list), so the row image doesn't drift.
    """

    _seed_legacy_model_instance(db, model_id="legacy_idw_m0")
    legacy_instance_count = len(db.model_instances)

    payload = _mirror_ready_payload()
    first = register_direct_grid_variant(
        cursor,
        _make_input(payload=payload, grid_snapshot_id=GRID_SNAPSHOT_ID),
    )
    # After first call: legacy + 1 variant = 2 model_instance rows.
    assert first.inserted is True
    assert first.mirror_stations_written == 2
    assert len(db.model_instances) == legacy_instance_count + 1
    assert len(db.met_stations) == 2
    met_stations_before = json.loads(json.dumps(db.met_stations))
    instances_before = json.loads(json.dumps(db.model_instances))

    # Byte-identical input on the second call.
    second = register_direct_grid_variant(
        cursor,
        _make_input(payload=_mirror_ready_payload(), grid_snapshot_id=GRID_SNAPSHOT_ID),
    )

    # Same identity returned — not a re-mint.
    assert second.model_id == first.model_id
    assert second.inserted is False
    # Emit-always self-heal: mirror leg re-runs and reports 2 rows written
    # (matching-identity DO UPDATE reconciliation, per §D2 self-heal).
    assert second.mirror_stations_written == 2
    # No duplicate rows appended anywhere.
    assert len(db.model_instances) == legacy_instance_count + 1
    assert len(db.met_stations) == 2
    # Byte-identical row images post-reconciliation.
    assert json.loads(json.dumps(db.met_stations)) == met_stations_before
    assert json.loads(json.dumps(db.model_instances)) == instances_before


def test_idempotent_different_model_input_package_new_row_prior_mirror_retained(
    db: _InMemoryDb, cursor: _FakeCursor
) -> None:
    """Different `model_input_package_id` → new row; M1's row + mirror byte-identical.

    Locks the "fix-forward not swallowed by idempotency" contract at the
    §1.3-scoped mirror-retention granularity (the assertion Phase 4.5
    REFUTED from PR #1004): registering M1' with a DIFFERENT
    `model_input_package_id` at the same
    `(basin_version_id, canonical_grid_key)` grain mints a NEW `model_id`,
    while M1's `core.model_instance` row AND all of M1's mirror rows are
    byte-identical afterwards, and M1''s mirror rows land alongside on
    disjoint station_ids (upstream `mapping_asset_identity` differs per
    §11.2 fix-forward).
    """

    payload_m1 = _direct_grid_payload(
        model_input_package_id="mai-pkg-m1-v1",
        binding_checksum="sha256:m1-binding",
    )
    _rebind_station_ids(payload_m1, "mai_m1")
    payload_m1_prime = _direct_grid_payload(
        model_input_package_id="mai-pkg-m1-v2",  # DIFFERENT package id
        binding_checksum="sha256:m1-binding",     # SAME binding checksum
    )
    _rebind_station_ids(payload_m1_prime, "mai_m1_prime")

    result_m1 = register_direct_grid_variant(
        cursor, _make_input(payload=payload_m1, grid_snapshot_id=GRID_SNAPSHOT_ID)
    )
    # Snapshot M1's persisted model_instance row + all M1 mirror rows.
    m1_instance_before = json.loads(
        json.dumps(next(row for row in db.model_instances if row["model_id"] == result_m1.model_id))
    )
    m1_station_ids_before = {
        station_id for station_id in db.met_stations if station_id.startswith("mai_m1::cell:")
    }
    assert m1_station_ids_before, "M1 mirror rows must be seeded before fix-forward"
    m1_stations_before = json.loads(
        json.dumps({sid: db.met_stations[sid] for sid in m1_station_ids_before})
    )

    result_prime = register_direct_grid_variant(
        cursor, _make_input(payload=payload_m1_prime, grid_snapshot_id=GRID_SNAPSHOT_ID)
    )

    # Fix-forward: new identity, not idempotent short-circuit.
    assert result_prime.inserted is True
    assert result_prime.model_id != result_m1.model_id
    assert len(db.model_instances) == 2

    # M1's model_instance row is byte-identical after M1''s registration.
    m1_instance_after = next(
        row for row in db.model_instances if row["model_id"] == result_m1.model_id
    )
    assert json.loads(json.dumps(m1_instance_after)) == m1_instance_before

    # M1's mirror rows are still present + byte-identical.
    m1_stations_after = {
        sid: db.met_stations[sid] for sid in m1_station_ids_before if sid in db.met_stations
    }
    assert set(m1_stations_after.keys()) == m1_station_ids_before, (
        "M1 mirror rows must not be deleted by M1' registration"
    )
    assert json.loads(json.dumps(m1_stations_after)) == m1_stations_before

    # M1' mirror rows exist alongside on DISJOINT station_ids.
    prime_station_ids = {
        station_id for station_id in db.met_stations if station_id.startswith("mai_m1_prime::cell:")
    }
    assert prime_station_ids, "M1' mirror rows must be written"
    assert prime_station_ids.isdisjoint(m1_station_ids_before)
    assert result_prime.mirror_stations_written == len(prime_station_ids)


def test_idempotent_different_binding_checksum_new_row_prior_mirror_retained(
    db: _InMemoryDb, cursor: _FakeCursor
) -> None:
    """Different `binding_checksum` alone also mints a new row.

    The identity is `(model_input_package_id, binding_checksum)` — NOT
    `model_input_package_id` alone. Same package with a different binding
    checksum (e.g. a rebuilt binding for the same asset id) registers as
    a new row with the prior row + prior mirror rows byte-identical.
    Symmetric proof to the `model_input_package_id`-varies case.
    """

    payload_m1 = _direct_grid_payload(
        model_input_package_id="mai-pkg-shared",
        binding_checksum="sha256:binding-v1",
    )
    _rebind_station_ids(payload_m1, "mai_bc_v1")
    payload_m1_prime = _direct_grid_payload(
        model_input_package_id="mai-pkg-shared",         # SAME package id
        binding_checksum="sha256:binding-v2-fixforward",  # DIFFERENT checksum
    )
    _rebind_station_ids(payload_m1_prime, "mai_bc_v2")

    result_m1 = register_direct_grid_variant(
        cursor, _make_input(payload=payload_m1, grid_snapshot_id=GRID_SNAPSHOT_ID)
    )
    m1_instance_before = json.loads(
        json.dumps(next(row for row in db.model_instances if row["model_id"] == result_m1.model_id))
    )
    m1_station_ids_before = {
        station_id for station_id in db.met_stations if station_id.startswith("mai_bc_v1::cell:")
    }
    assert m1_station_ids_before
    m1_stations_before = json.loads(
        json.dumps({sid: db.met_stations[sid] for sid in m1_station_ids_before})
    )

    result_prime = register_direct_grid_variant(
        cursor, _make_input(payload=payload_m1_prime, grid_snapshot_id=GRID_SNAPSHOT_ID)
    )

    assert result_prime.inserted is True
    assert result_prime.model_id != result_m1.model_id
    assert len(db.model_instances) == 2

    m1_instance_after = next(
        row for row in db.model_instances if row["model_id"] == result_m1.model_id
    )
    assert json.loads(json.dumps(m1_instance_after)) == m1_instance_before

    m1_stations_after = {
        sid: db.met_stations[sid] for sid in m1_station_ids_before if sid in db.met_stations
    }
    assert set(m1_stations_after.keys()) == m1_station_ids_before
    assert json.loads(json.dumps(m1_stations_after)) == m1_stations_before

    prime_station_ids = {
        station_id for station_id in db.met_stations if station_id.startswith("mai_bc_v2::cell:")
    }
    assert prime_station_ids
    assert prime_station_ids.isdisjoint(m1_station_ids_before)
    assert result_prime.mirror_stations_written == len(prime_station_ids)


# --- §1.4 / SUB-4: producer mirror `active_flag` ownership LOCKS -----------
#
# These tests LOCK the §D2 flag-ownership boundary at the runtime producer
# plane (`workers/forcing_producer/store.py:ensure_direct_grid_met_stations`,
# `workers/forcing_producer/file_store.py:_handoff_station_rows`, and the
# ingest path in `packages/common/forcing_domain_handoff_apply.py:_upsert_met_stations`).
#
# After #965, none of these paths may:
#   * insert `active_flag=true` on a fresh mirror row, or
#   * escalate an existing row's `active_flag` from `false` to `true`.
# Mirror activation belongs exclusively to Change 8's cutover flip. The
# fail-closed derived-cache collision predicate is retained unchanged.
#
# Test infrastructure:
#   * `_ProducerFakeStore` — minimal `PsycopgForcingRepository` subclass that
#     overrides `_replace_values` to accumulate `execute_values` calls and
#     simulate the DO UPDATE against an in-memory station table with the
#     producer's exact WHERE predicate (fail-closed on identity drift, flag
#     preservation on match). Kept scoped to §1.4 because the producer path
#     uses `execute_values` (multi-row batch), while the registration surface
#     uses per-row `cursor.execute` — the two SQL shapes diverge enough that
#     extending `_FakeCursor` to host both would obscure both.
#   * `_producer_contract_from_payload` — parses a §1.2/SUB-2 payload into
#     the runtime `DirectGridForcingContract` the producer expects.

class _ProducerFakeStore(PsycopgForcingRepository):
    """Minimal producer-side fake for `ensure_direct_grid_met_stations`.

    The producer plane calls `_replace_values(..., execute_values-style)`,
    not per-row `cursor.execute`. This fake intercepts that call, captures
    the raw SQL for shape assertions, and simulates the mirror upsert
    against an in-memory `station_id -> row` dict:

    * Fresh station_id -> INSERT lands with `active_flag=False` (matching
      the production INSERT template's literal `false`).
    * Existing station_id with matching derived-cache identity -> DO UPDATE
      that PRESERVES the existing `active_flag` (matching the SET clause
      that no longer touches `active_flag`).
    * Existing station_id with non-matching identity -> the affected-row
      count would be 0; the fake raises `MetStoreError` with the producer's
      conflict message, matching `_replace_values`'s `expected_insert_count`
      check.
    """

    def __init__(self, station_rows: list[dict[str, Any]] | None = None) -> None:
        super().__init__(database_url="memory://producer-fake")
        # `frozen=True` on the base dataclass prevents normal assignment.
        object.__setattr__(self, "station_rows", list(station_rows or []))
        object.__setattr__(self, "sql_calls", [])
        object.__setattr__(self, "insert_statements", [])

    def _replace_values(
        self,
        pre_delete_statement: str | None,
        pre_delete_parameters: tuple[Any, ...],
        delete_statement: str | None,
        delete_parameters: tuple[Any, ...],
        insert_statement: str,
        rows: Any,
        *,
        template: str | None = None,
        expected_insert_count: int | None = None,
        conflict_error: str | None = None,
    ) -> None:
        assert pre_delete_statement is None
        assert delete_statement is None
        row_list = [tuple(row) for row in rows]
        self.sql_calls.append(
            {"insert_statement": insert_statement, "template": template, "rows": row_list}
        )
        self.insert_statements.append(insert_statement)

        matched = 0
        pending = [dict(row) for row in self.station_rows]
        for row in row_list:
            (
                station_id,
                basin_version_id,
                station_name,
                longitude,
                latitude,
                elevation_m,
                station_role,
                properties_wrapped,
            ) = row
            properties = _adapt(properties_wrapped)
            new_record = {
                "station_id": station_id,
                "basin_version_id": basin_version_id,
                "station_name": station_name,
                "longitude": longitude,
                "latitude": latitude,
                "elevation_m": elevation_m,
                "station_role": station_role,
                "properties_json": dict(properties),
            }
            existing = next(
                (item for item in pending if item["station_id"] == station_id), None
            )
            if existing is None:
                # Fresh INSERT lands with active_flag=False literal.
                pending.append({**new_record, "active_flag": False})
                matched += 1
                continue
            if not _producer_predicate_matches(existing, new_record):
                # WHERE predicate rejects this row -> rowcount=0 for it,
                # which the caller catches as MetStoreError.
                continue
            # DO UPDATE reconciles same-value fields; active_flag is PRESERVED
            # (registration/cutover ownership, §D2).
            preserved_flag = existing["active_flag"]
            existing.update(new_record)
            existing["active_flag"] = preserved_flag
            matched += 1

        if expected_insert_count is not None and matched != expected_insert_count:
            raise MetStoreError(
                conflict_error or "Forcing database write affected an unexpected row count."
            )
        object.__setattr__(self, "station_rows", pending)


def _adapt(value: Any) -> Any:
    return getattr(value, "adapted", value)


def _producer_predicate_matches(
    existing: Mapping[str, Any], incoming: Mapping[str, Any]
) -> bool:
    """Model the store.py DO UPDATE WHERE predicate for the fake."""

    existing_props = existing.get("properties_json") or {}
    incoming_props = incoming.get("properties_json") or {}
    return (
        existing.get("basin_version_id") == incoming.get("basin_version_id")
        and existing.get("station_role") == DIRECT_GRID_CACHE_STATION_ROLE
        and existing_props.get("derived_cache") is True
        and existing_props.get("forcing_mapping_mode") == "direct_grid"
        and existing_props.get("binding_checksum") == incoming_props.get("binding_checksum")
        and existing_props.get("model_input_package_id")
        == incoming_props.get("model_input_package_id")
        and existing_props.get("grid_signature") == incoming_props.get("grid_signature")
        and existing_props.get("contract_grid_id") == incoming_props.get("contract_grid_id")
        and existing_props.get("grid_id") == incoming_props.get("grid_id")
    )


def _producer_contract(payload: dict[str, Any]):
    """Parse a §1.2/SUB-2-shaped payload into the runtime producer contract."""

    return _parse_direct_grid_contract(payload, source_id="GFS")


def _seed_registered_mirror(
    store: _ProducerFakeStore,
    *,
    payload: dict[str, Any],
    basin_version_id: str = BASIN_VERSION_ID,
    active_flag: bool = False,
) -> list[dict[str, Any]]:
    """Seed the fake store's in-memory table with post-registration mirror rows.

    Mirrors the shape the §1.2 registration surface persists (`derived_cache:true`,
    `forcing_mapping_mode:'direct_grid'`, etc.) so a subsequent producer upsert
    hits the DO UPDATE path (identity matches, WHERE predicate returns TRUE).
    ``active_flag`` defaults to ``False`` (fresh registration) but callers may
    seed ``True`` to simulate the post-cutover state (Change 8 flip).
    """

    contract = _producer_contract(payload)
    rows: list[dict[str, Any]] = []
    for station in contract.stations:
        properties = {
            **dict(station.properties),
            "derived_cache": True,
            "forcing_mapping_mode": "direct_grid",
            "direct_grid": True,
            "manifest_authority": True,
            "binding_checksum": contract.binding_checksum,
            "binding_uri": contract.binding_uri,
            "model_input_package_id": contract.model_input_package_id,
            "sp_att_path": contract.sp_att_path,
            "sp_att_checksum": contract.sp_att_checksum,
            "grid_id": station.grid_id,
            "contract_grid_id": contract.grid_id,
            "grid_cell_id": station.grid_cell_id,
            "grid_signature": contract.grid_signature,
            "shud_forcing_index": station.shud_forcing_index,
            "forcing_filename": station.forcing_filename,
            "x": station.x,
            "y": station.y,
            "z": station.z,
        }
        row = {
            "station_id": station.station_id,
            "basin_version_id": basin_version_id,
            "station_name": f"Direct-grid station {station.shud_forcing_index}",
            "longitude": station.longitude,
            "latitude": station.latitude,
            "elevation_m": station.z,
            "station_role": DIRECT_GRID_CACHE_STATION_ROLE,
            "active_flag": active_flag,
            "properties_json": properties,
        }
        rows.append(row)
    object.__setattr__(store, "station_rows", store.station_rows + rows)
    return rows


# --- §1.4 Group A: producer preserves the registration-owned active_flag --


def test_producer_preserves_registration_flag_false_on_registered_variant_pre_cutover() -> None:
    """Producer upsert against a registered mirror never escalates `false` -> `true`.

    Seeds two mirror rows the registration surface wrote (`active_flag=false`,
    §1.2). Invokes the runtime producer's `ensure_direct_grid_met_stations`
    with the same asset identity so the DO UPDATE path fires. Locks two
    invariants:

    1. Every mirror row still has ``active_flag=False`` afterwards
       (registration-owned inactivity survives a pre-cutover shadow run).
    2. The producer's INSERT template carries `active_flag` as literal
       ``false`` and the DO UPDATE SET clause omits `active_flag` entirely
       (SQL-shape lock so a future edit that adds `active_flag = true`
       back into SET trips this test).
    """

    payload = _mirror_ready_payload()
    store = _ProducerFakeStore()
    seeded = _seed_registered_mirror(store, payload=payload, active_flag=False)
    assert all(row["active_flag"] is False for row in seeded)

    store.ensure_direct_grid_met_stations(
        basin_version_id=BASIN_VERSION_ID, contract=_producer_contract(payload)
    )

    # Row-level invariant: every mirror row stayed inactive.
    for row in store.station_rows:
        assert row["active_flag"] is False, (
            f"producer must preserve registration-owned inactivity for {row['station_id']!r}"
        )

    # SQL-shape invariant: the producer INSERT template lands `false` literally
    # and the DO UPDATE SET clause never touches active_flag.
    insert_sql = store.insert_statements[-1]
    template = store.sql_calls[-1]["template"] or ""
    assert " false, " in template, (
        "producer INSERT template must carry active_flag=false as a literal (§D2)"
    )
    set_clause = insert_sql.split("DO UPDATE SET", 1)[1].split("WHERE", 1)[0]
    assert "active_flag" not in set_clause, (
        "producer DO UPDATE SET must not touch active_flag (§D2 flag ownership)"
    )


def test_producer_preserves_registration_flag_true_stays_true() -> None:
    """A row already flipped `true` by Change 8's cutover stays `true`.

    Locks the "never de-escalates" half of §D2 flag preservation: after the
    cutover flip, subsequent producer runs must not silently reset a live
    mirror row back to inactive. Seeds mirror rows with ``active_flag=True``,
    runs the producer's DO UPDATE, and asserts every row is still ``True``.
    """

    payload = _mirror_ready_payload()
    store = _ProducerFakeStore()
    _seed_registered_mirror(store, payload=payload, active_flag=True)

    store.ensure_direct_grid_met_stations(
        basin_version_id=BASIN_VERSION_ID, contract=_producer_contract(payload)
    )

    for row in store.station_rows:
        assert row["active_flag"] is True, (
            f"producer must never de-escalate active_flag; {row['station_id']!r} regressed"
        )


def test_producer_preserves_registration_flag_file_plane_handoff_carries_no_forced_true() -> None:
    """The DB-free file plane emits mirror rows without `active_flag=true`.

    Locks the §D2 boundary at the file plane
    (`workers/forcing_producer/file_store.py:_handoff_station_rows`): the
    emitted station-inventory row dict either omits `active_flag` entirely
    OR carries `False`. It MUST NOT carry `True`. Direct method call
    against a bare `FileForcingRepository` shell (the method touches only
    `self._stations_by_basin_version`, which stays empty for this test).
    """

    from workers.forcing_producer.file_store import FileForcingRepository
    from workers.forcing_producer.producer import ForcingTimeseriesRow

    # Bypass the dataclass __init__ (which requires an object_store) — the
    # method under test doesn't touch object_store. Set the caches to empty.
    repo = object.__new__(FileForcingRepository)
    object.__setattr__(repo, "object_store", None)
    object.__setattr__(repo, "registry_manifest", None)
    object.__setattr__(repo, "_registry_cache", None)
    object.__setattr__(repo, "_model_manifest_cache", {})
    object.__setattr__(repo, "_stations_by_basin_version", {})
    object.__setattr__(repo, "_weights_by_scope", {})
    object.__setattr__(repo, "_forcing_versions", {})
    object.__setattr__(repo, "_forcing_components", {})
    object.__setattr__(repo, "_forcing_timeseries_summary", {})
    object.__setattr__(repo, "_forcing_timeseries_rows", {})

    package_manifest = {
        "station_order": [
            {
                "station_id": "qhh_forc_001",
                "shud_forcing_index": 1,
                "forcing_filename": "X100.125Y38.25.csv",
                "longitude": 100.125,
                "latitude": 38.25,
                "elevation_m": 3280.0,
            }
        ]
    }
    row = ForcingTimeseriesRow(
        forcing_version_id="fv-1",
        basin_version_id="basins_qhh_v2026_06",
        station_id="qhh_forc_001",
        valid_time=datetime(2026, 6, 20, 12, tzinfo=UTC),
        source_id="gfs",
        variable="PRCP",
        value=0.0,
        unit="kg m-2 s-1",
        native_resolution="1h",
    )
    record = {
        "forcing_version_id": "fv-1",
        "source_id": "gfs",
        "cycle_time": row.valid_time,
        "model_id": "basins_qhh_shud",
    }

    emitted = repo._handoff_station_rows(
        record=record,
        package_manifest=package_manifest,
        rows=[row],
        basin_id="basins_qhh",
        basin_version_id="basins_qhh_v2026_06",
        model_id="basins_qhh_shud",
    )

    assert len(emitted) == 1
    emitted_row = emitted[0]
    # The row MUST NOT carry `active_flag=True`. It may be missing OR False.
    assert emitted_row.get("active_flag") is not True, (
        "file-plane handoff must not force active_flag=true (§D2)"
    )
    # Prefer explicit False (the ingest apply requires a bool, so absence
    # would fail parse-time validation upstream). Lock the explicit-False
    # form so a future edit that switches to "drop the key" fails loudly.
    assert emitted_row["active_flag"] is False, (
        "file-plane handoff must emit active_flag=False (registration-owned)"
    )


# --- §1.4 Group B: end-to-end production run mirror stays inactive --------


def test_production_run_mirror_stays_inactive_end_to_end(
    db: _InMemoryDb, cursor: _FakeCursor
) -> None:
    """A full registration -> producer upsert -> ingest apply run leaves mirror inactive.

    Integrated end-to-end proof of §D2 across the three planes touched by
    #965:

    1. Registration writes mirror rows with `active_flag=false` (§1.2).
    2. The runtime producer's DO UPDATE runs against the same identity
       (§1.4 flip: no `active_flag=true` in SET; INSERT lands `false`).
    3. The ingest apply's `_upsert_met_stations` template lands the literal
       `false` and drops `active_flag` from the identity predicate.

    Locks the shadow-window display invariant: pre-cutover production
    cannot create a mixed display because every mirror row ends
    `active_flag=false`, so the MVT single-track query still returns only
    the legacy track.
    """

    # --- Plane 1: registration writes the mirror rows inactive ---
    payload = _mirror_ready_payload()
    result = register_direct_grid_variant(
        cursor, _make_input(payload=payload, grid_snapshot_id=GRID_SNAPSHOT_ID)
    )
    assert result.inserted is True
    assert result.mirror_stations_written == 2
    for row in db.met_stations.values():
        assert row["active_flag"] is False

    # --- Plane 2: runtime producer's mirror upsert against the same identity ---
    producer_store = _ProducerFakeStore(
        station_rows=[
            {**dict(row), "properties_json": dict(row["properties_json"])}
            for row in db.met_stations.values()
        ]
    )
    producer_store.ensure_direct_grid_met_stations(
        basin_version_id=BASIN_VERSION_ID, contract=_producer_contract(payload)
    )
    for row in producer_store.station_rows:
        assert row["active_flag"] is False

    # --- Plane 3: ingest apply's _upsert_met_stations SQL-shape check ---
    # Simulate the ingest apply's `execute_values` call by inspecting the
    # module's SQL. The template must land a literal `false` and the ON
    # CONFLICT identity predicate must NOT include `active_flag`.
    import inspect as _inspect

    from packages.common import forcing_domain_handoff_apply as _apply_module

    apply_source = _inspect.getsource(_apply_module._upsert_met_stations)
    assert "false, %s)" in apply_source, (
        "ingest apply INSERT template must land active_flag=false literal (§D2)"
    )
    conflict_predicate = apply_source.split("ON CONFLICT", 1)[1]
    assert "active_flag = EXCLUDED.active_flag" not in conflict_predicate, (
        "ingest apply ON CONFLICT predicate must NOT gate on active_flag (§D2)"
    )

    # MVT single-track invariant: no mirror row is active anywhere.
    mirror_active_rows = [
        row for row in db.met_stations.values() if row["active_flag"] is True
    ]
    assert mirror_active_rows == [], (
        "pre-cutover production must not create any active mirror row (single-track invariant)"
    )


# --- §1.4 Group C: producer collision still fails closed ------------------


def test_producer_collision_still_fails_closed_on_non_matching_station_id() -> None:
    """The producer's derived-cache collision predicate is untouched by §1.4.

    Regression lock: the flag ownership change relaxes NOTHING about the
    fail-closed identity predicate. Seeds a foreign row on the same
    station_id with a DIFFERENT `binding_checksum` (identity mismatch);
    runs the producer's `ensure_direct_grid_met_stations`; asserts it
    raises `MetStoreError` with the producer's mirror-conflict message.

    This is the §1.2 collision policy retained verbatim; §1.4 does not
    change the WHERE predicate on the DO UPDATE — only removes the
    `active_flag = true` SET term.
    """

    payload = _mirror_ready_payload(
        binding_checksum="sha256:mirror-binding-INCOMING",
    )
    contract = _producer_contract(payload)
    foreign_station_id = contract.stations[0].station_id

    store = _ProducerFakeStore(
        station_rows=[
            {
                "station_id": foreign_station_id,
                "basin_version_id": BASIN_VERSION_ID,
                "station_name": "Foreign station",
                "longitude": 99.99,
                "latitude": 35.55,
                "elevation_m": 1234.5,
                "station_role": DIRECT_GRID_CACHE_STATION_ROLE,
                # Even active_flag=false — the collision is on IDENTITY, not
                # on the flag. §1.4 removes active_flag from the SET clause;
                # it does NOT relax the identity WHERE predicate.
                "active_flag": False,
                "properties_json": {
                    "derived_cache": True,
                    "forcing_mapping_mode": "direct_grid",
                    # A DIFFERENT binding_checksum than the incoming producer contract.
                    "binding_checksum": "sha256:FOREIGN-binding-not-INCOMING",
                    "model_input_package_id": contract.model_input_package_id,
                    "grid_signature": contract.grid_signature,
                    "contract_grid_id": contract.grid_id,
                    "grid_id": contract.stations[0].grid_id,
                },
            }
        ]
    )
    foreign_before = json.loads(json.dumps(store.station_rows))

    with pytest.raises(MetStoreError, match="mirror conflicts"):
        store.ensure_direct_grid_met_stations(
            basin_version_id=BASIN_VERSION_ID, contract=contract
        )

    # Fail-closed: the foreign row is byte-identical afterwards.
    assert json.loads(json.dumps(store.station_rows)) == foreign_before


# --- §5.1 mechanism-only invariant proof (real DB) ------------------------
#
# Epic #961 SUB-11 (#972): registering direct-grid variants for SYNTHETIC
# basins leaves the 13 live basins' active models and station sets byte-for-
# byte unchanged, and every registered synthetic variant lands
# `lifecycle_state='inactive'` / `active_flag=false`. This proof runs on
# node-27's real Postgres (the data oracle per project convention); the
# session-scoped `integration_database_url` fixture provisions a scratch DB
# and `apply_migrations_from_zero` brings it up from zero — deliberately
# NOT reusing `sub5_migrated_database` (module-scoped, coupled to grid
# snapshot tests) so this test owns its full DB lifecycle.
#
# Receipt: on pass, the test writes
# `artifacts/mechanism-only-receipts/receipt-<UTC-timestamp>.json` with a
# per-basin before-hash / after-hash record so the orchestrator can compare
# receipts across runs and archive them as PR evidence.


_MECHANISM_ONLY_LIVE_BASIN_COUNT = 13
_MECHANISM_ONLY_SYNTHETIC_VARIANT_COUNT = 3
_MECHANISM_ONLY_STATIONS_PER_BASIN = 4
_MECHANISM_ONLY_PREFIX = "mo972"
_MECHANISM_ONLY_SOURCE_ID = "gfs"


def _mo_row_hash(row: Mapping[str, Any]) -> str:
    """Byte-stable sha256 over a row dict (sorted keys, default str coercion).

    Uses ``sort_keys=True`` so key ordering can never influence the hash, and
    ``default=str`` so JSON-unserializable types (``uuid.UUID``, ``datetime``,
    ``Decimal``, ``memoryview`` for PostGIS geom, etc.) fall through to their
    ``str()`` form deterministically. The hash is the byte-identity oracle
    for before/after snapshots of the 13 live-analog basins.
    """

    import hashlib

    payload = json.dumps(dict(row), sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _mo_snapshot_live_basin(
    connection: Any, basin_version_id: str
) -> dict[str, Any]:
    """Snapshot ONE live-analog basin's active model + active station set.

    Returns a dict with the active model row hash, the active-station-set
    row hashes (sorted for order-stability), and the identifying fields the
    receipt needs (``active_model_id``, ``active_station_count``). Uses the
    ``geom::text`` WKB projection so the hash captures the PostGIS point
    identity byte-for-byte across the before/after boundary.
    """

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT model_id, basin_version_id, river_network_version_id,
                   mesh_version_id, calibration_version_id, shud_code_version,
                   model_package_uri, active_flag, lifecycle_state, resource_profile
            FROM core.model_instance
            WHERE basin_version_id = %s
              AND active_flag = true
              AND lifecycle_state = 'active'
            """,
            (basin_version_id,),
        )
        active_model = cursor.fetchone()
        assert active_model is not None, (
            f"live-analog basin {basin_version_id!r} has no active model"
        )
        active_model_dict = dict(active_model)

        cursor.execute(
            """
            SELECT station_id, basin_version_id, station_name,
                   ST_AsText(geom) AS geom_wkt, elevation_m, station_role,
                   active_flag, properties_json, grid_snapshot_id
            FROM met.met_station
            WHERE basin_version_id = %s
              AND active_flag = true
            ORDER BY station_id
            """,
            (basin_version_id,),
        )
        active_stations = [dict(row) for row in cursor.fetchall()]

    station_hashes = sorted(_mo_row_hash(row) for row in active_stations)
    stations_agg_hash = _mo_row_hash({"station_hashes": station_hashes})
    return {
        "basin_version_id": basin_version_id,
        "active_model_id": str(active_model_dict["model_id"]),
        "active_model_hash": _mo_row_hash(active_model_dict),
        "active_station_count": len(active_stations),
        "active_stations_hash": stations_agg_hash,
    }


def _mo_seed_live_basin(cursor: Any, index: int) -> str:
    """Seed one live-analog basin: basin/version/network/mesh + active model + stations.

    Returns the ``basin_version_id``. Each basin gets a unique ``basin_id``
    keyed on ``index`` so 13 rows can be inserted without collision, an
    ``active_flag=true``/``lifecycle_state='active'`` model row (proxying the
    legacy IDW model), and N=``_MECHANISM_ONLY_STATIONS_PER_BASIN`` mirror
    stations with ``active_flag=true`` so the assertion has enough surface
    to catch even a single-row drift.
    """

    from psycopg2.extras import Json

    basin_id = f"{_MECHANISM_ONLY_PREFIX}_basin_{index:02d}"
    basin_version_id = f"{_MECHANISM_ONLY_PREFIX}_bv_{index:02d}"
    rnv_id = f"{_MECHANISM_ONLY_PREFIX}_rnv_{index:02d}"
    mesh_id = f"{_MECHANISM_ONLY_PREFIX}_mesh_{index:02d}"
    model_id = f"{_MECHANISM_ONLY_PREFIX}_active_model_{index:02d}"

    # Offset the basin envelope by index so each basin has a distinct bbox —
    # PostGIS constraints don't require it, but keeping bboxes disjoint makes
    # the fixture read as "13 different basins" if a maintainer inspects the
    # scratch DB during a debug session.
    lon_offset = 100.0 + index * 0.5
    lat_offset = 30.0 + index * 0.2

    cursor.execute(
        """
        INSERT INTO core.basin (basin_id, basin_name, basin_group, description)
        VALUES (%s, %s, 'mechanism-only-live-analog', %s)
        """,
        (basin_id, f"Live-analog basin {index:02d}", f"Proxy for live basin #{index:02d}"),
    )
    cursor.execute(
        """
        INSERT INTO core.basin_version (
            basin_version_id, basin_id, version_label, geom, active_flag,
            source_uri, checksum
        )
        VALUES (
            %s, %s, 'v1',
            ST_Multi(ST_MakeEnvelope(%s, %s, %s, %s, 4490)),
            true, 'mechanism-only://live-analog', %s
        )
        """,
        (
            basin_version_id,
            basin_id,
            lon_offset,
            lat_offset,
            lon_offset + 0.4,
            lat_offset + 0.4,
            f"live-basin-{index:02d}-sha",
        ),
    )
    cursor.execute(
        """
        INSERT INTO core.river_network_version (
            river_network_version_id, basin_version_id, version_label,
            segment_count, source_uri, checksum
        )
        VALUES (%s, %s, 'v1', 1, 'mechanism-only://rnv', 'live-rnv-sha')
        """,
        (rnv_id, basin_version_id),
    )
    cursor.execute(
        """
        INSERT INTO core.mesh_version (
            mesh_version_id, basin_version_id, version_label, mesh_uri,
            checksum, properties_json
        )
        VALUES (%s, %s, 'v1', 'mechanism-only://mesh', 'live-mesh-sha', %s)
        """,
        (mesh_id, basin_version_id, Json({"cell_count": 1})),
    )
    # Active legacy model row: `active_flag=true` + `lifecycle_state='active'`
    # so the SUB-9 partial unique index (`model_instance_active_basin_version_uidx`)
    # and the consistency CHECK (`model_instance_active_lifecycle_consistency_chk`)
    # from migration 000022 are both satisfied on insert.
    cursor.execute(
        """
        INSERT INTO core.model_instance (
            model_id, basin_version_id, river_network_version_id,
            mesh_version_id, calibration_version_id, shud_code_version,
            model_package_uri, active_flag, lifecycle_state, resource_profile
        )
        VALUES (%s, %s, %s, %s, 'calib-v1', 'shud-v1',
                'mechanism-only://live-active-package/', true, 'active', %s)
        """,
        (
            model_id,
            basin_version_id,
            rnv_id,
            mesh_id,
            Json({"lineage": "mechanism_only_live_analog"}),
        ),
    )
    # N legacy-shape (``station_role='forcing_proxy'``) active mirror stations
    # per basin — every one MUST land `active_flag=true` so the MVT-style
    # `basin_version_id=… AND active_flag=true` query returns them and so the
    # before/after byte-identity assertion has real substance.
    for station_index in range(_MECHANISM_ONLY_STATIONS_PER_BASIN):
        station_id = f"{_MECHANISM_ONLY_PREFIX}_live_{index:02d}_st_{station_index:02d}"
        # Deterministic per-station coordinate offset inside the basin envelope.
        st_lon = lon_offset + 0.05 + station_index * 0.05
        st_lat = lat_offset + 0.05 + station_index * 0.05
        cursor.execute(
            """
            INSERT INTO met.met_station (
                station_id, basin_version_id, station_name, geom,
                elevation_m, station_role, active_flag, properties_json
            )
            VALUES (
                %s, %s, %s,
                ST_SetSRID(ST_MakePoint(%s, %s), 4490),
                %s, 'forcing_proxy', true, %s
            )
            """,
            (
                station_id,
                basin_version_id,
                f"Live station {station_index:02d} of basin {index:02d}",
                st_lon,
                st_lat,
                1000.0 + station_index,
                Json({"lineage": "mechanism_only_live_analog", "index": station_index}),
            ),
        )
    return basin_version_id


def _mo_seed_synthetic_basin(cursor: Any, index: int) -> tuple[str, str, str, str]:
    """Seed a SYNTHETIC basin for a direct-grid variant registration.

    Returns ``(basin_version_id, river_network_version_id, mesh_version_id,
    model_package_uri)`` so the caller can hand them to
    ``DirectGridBaselineModelInputs``. Deliberately DISJOINT from the live-
    analog basin id space (different prefix suffix) so a bug that lets the
    registration surface mutate the wrong basin would blow the byte-identity
    assertion loudly instead of silently overlapping.
    """

    from psycopg2.extras import Json

    basin_id = f"{_MECHANISM_ONLY_PREFIX}_synbasin_{index:02d}"
    basin_version_id = f"{_MECHANISM_ONLY_PREFIX}_synbv_{index:02d}"
    rnv_id = f"{_MECHANISM_ONLY_PREFIX}_synrnv_{index:02d}"
    mesh_id = f"{_MECHANISM_ONLY_PREFIX}_synmesh_{index:02d}"
    model_package_uri = f"mechanism-only://synthetic-{index:02d}/package/"

    lon_offset = 120.0 + index * 0.5
    lat_offset = 40.0 + index * 0.2

    cursor.execute(
        """
        INSERT INTO core.basin (basin_id, basin_name, basin_group, description)
        VALUES (%s, %s, 'mechanism-only-synthetic', 'Synthetic basin for direct-grid variant registration')
        """,
        (basin_id, f"Synthetic basin {index:02d}"),
    )
    cursor.execute(
        """
        INSERT INTO core.basin_version (
            basin_version_id, basin_id, version_label, geom, active_flag,
            source_uri, checksum
        )
        VALUES (
            %s, %s, 'v1',
            ST_Multi(ST_MakeEnvelope(%s, %s, %s, %s, 4490)),
            false, 'mechanism-only://synthetic', %s
        )
        """,
        (
            basin_version_id,
            basin_id,
            lon_offset,
            lat_offset,
            lon_offset + 0.4,
            lat_offset + 0.4,
            f"syn-basin-{index:02d}-sha",
        ),
    )
    cursor.execute(
        """
        INSERT INTO core.river_network_version (
            river_network_version_id, basin_version_id, version_label,
            segment_count, source_uri, checksum
        )
        VALUES (%s, %s, 'v1', 1, 'mechanism-only://synrnv', 'syn-rnv-sha')
        """,
        (rnv_id, basin_version_id),
    )
    cursor.execute(
        """
        INSERT INTO core.mesh_version (
            mesh_version_id, basin_version_id, version_label, mesh_uri,
            checksum, properties_json
        )
        VALUES (%s, %s, 'v1', 'mechanism-only://synmesh', 'syn-mesh-sha', %s)
        """,
        (mesh_id, basin_version_id, Json({"cell_count": 2})),
    )
    return basin_version_id, rnv_id, mesh_id, model_package_uri


def _mo_seed_data_source(cursor: Any) -> None:
    """Seed the ``gfs`` data source so the canonical_grid_snapshot FK holds."""

    from psycopg2.extras import Json

    cursor.execute(
        """
        INSERT INTO met.data_source (
            source_id, source_name, source_type, status, native_format,
            adapter_name, config_json
        )
        VALUES (%s, 'GFS mechanism-only source', 'forecast', 'mock',
                'netcdf', 'gfs', %s)
        ON CONFLICT (source_id) DO NOTHING
        """,
        (_MECHANISM_ONLY_SOURCE_ID, Json({"mechanism_only": True})),
    )


def _mo_seed_snapshot(cursor: Any, index: int) -> tuple[str, str]:
    """Seed a canonical_grid_snapshot row; return ``(grid_snapshot_id, canonical_grid_key)``."""

    import uuid as _uuid

    grid_snapshot_id = str(_uuid.uuid4())
    canonical_grid_key = f"mo972_canonical_key_{index:02d}"
    grid_id = f"mo972_grid_{index:02d}"
    grid_signature = f"sha256:mo972-sig-{index:02d}"
    grid_definition_uri = f"mechanism-only://grid/{index:02d}/grid.json"
    grid_definition_checksum = f"{index:064x}"

    cursor.execute(
        """
        INSERT INTO met.canonical_grid_snapshot (
            grid_snapshot_id, canonical_grid_key, source_id, grid_id,
            grid_signature, grid_definition_uri, grid_definition_checksum,
            longitude_convention, latitude_order, flatten_order,
            native_resolution, bbox_south, bbox_north, bbox_west, bbox_east,
            converter_version, valid_from, valid_to, applicable_source_ids
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s,
            '[-180,180)', 'descending', 'y_major_lat_then_lon',
            0.25, 8.0, 64.0, 63.0, 145.0,
            'converter-v1', %s, NULL, %s
        )
        """,
        (
            grid_snapshot_id,
            canonical_grid_key,
            _MECHANISM_ONLY_SOURCE_ID,
            grid_id,
            grid_signature,
            grid_definition_uri,
            grid_definition_checksum,
            datetime(2026, 1, 1, tzinfo=UTC),
            [_MECHANISM_ONLY_SOURCE_ID],
        ),
    )
    # Canonical grid cells: the direct-grid parser doesn't consult them for
    # the registration path, but keeping ordinals present makes the fixture
    # closer to the production shape.
    cursor.execute(
        """
        INSERT INTO met.canonical_grid_cell (
            grid_snapshot_id, grid_cell_id, longitude, latitude,
            canonical_ordinal
        ) VALUES (%s, %s, %s, %s, %s), (%s, %s, %s, %s, %s)
        """,
        (
            grid_snapshot_id, f"cell-{index:02d}-001", 100.95, 36.25, 1,
            grid_snapshot_id, f"cell-{index:02d}-002", 101.05, 36.25, 2,
        ),
    )
    return grid_snapshot_id, canonical_grid_key


def _mo_synthetic_payload(index: int, grid_id: str, grid_signature: str) -> dict[str, Any]:
    """Build a parser-valid direct-grid payload disjoint from live-basin ids."""

    mai = f"mo972_mai_{index:02d}"
    return {
        "forcing_mapping_mode": "direct_grid",
        "binding_uri": f"mechanism-only://synthetic/{index:02d}/binding.json",
        "binding_checksum": f"sha256:mo972-binding-{index:02d}",
        "model_input_package_id": f"mo972_mip_{index:02d}",
        "sp_att_path": f"input/mo972-{index:02d}.sp.att",
        "sp_att_checksum": f"sha256:mo972-spatt-{index:02d}",
        "applicable_source_ids": [_MECHANISM_ONLY_SOURCE_ID],
        "grid_id": grid_id,
        "grid_signature": grid_signature,
        "station_bindings": [
            {
                "station_id": f"{mai}::cell:cell-{index:02d}-001",
                "shud_forcing_index": 1,
                "forcing_filename": "X100.95Y36.25.csv",
                "longitude": 100.95,
                "latitude": 36.25,
                "x": 1,
                "y": 2,
                "z": 3657,
                "grid_id": grid_id,
                "grid_cell_id": f"cell-{index:02d}-001",
            },
            {
                "station_id": f"{mai}::cell:cell-{index:02d}-002",
                "shud_forcing_index": 2,
                "forcing_filename": "X101.05Y36.25.csv",
                "longitude": 101.05,
                "latitude": 36.25,
                "x": 2,
                "y": 3,
                "z": 3600,
                "grid_id": grid_id,
                "grid_cell_id": f"cell-{index:02d}-002",
            },
        ],
    }


@pytest.mark.integration
def test_mechanism_only_live_basins_undisturbed(
    integration_database_url: str,
    tmp_path: Any,
) -> None:
    """§5.1 mechanism-only invariant: variant registration is INERT for live basins.

    Applies the migrations from zero (session-scoped fixture provisions a
    fresh scratch DB per node-27 project convention), seeds
    ``_MECHANISM_ONLY_LIVE_BASIN_COUNT`` (=13) live-analog basins each with
    an ``active_flag=true`` legacy model + N ``active_flag=true`` mirror
    stations, snapshots each basin's active-model row and active-station
    row-set as sha256 byte-hashes, then registers
    ``_MECHANISM_ONLY_SYNTHETIC_VARIANT_COUNT`` (=3) direct-grid variants on
    SYNTHETIC other ``basin_version_id`` scopes, and re-snapshots the 13
    live basins. Asserts:

    1. Every live basin's active-model row hash is byte-identical before
       and after — no writes on any live scope.
    2. Every live basin's active-station-set hash is byte-identical before
       and after — no writes on any live scope's ``met.met_station`` rows.
    3. Every registered synthetic variant lands
       ``lifecycle_state='inactive'`` and ``active_flag=false`` (mechanism
       is inactive by construction).

    Emits an ``artifacts/mechanism-only-receipts/receipt-<UTC>.json``
    receipt with the per-basin before/after hashes and the synthetic-variant
    identities so the orchestrator can archive it as PR evidence.
    """

    import pathlib
    import uuid as _uuid

    import psycopg2
    from psycopg2.extras import RealDictCursor

    from tests.integration_helpers import apply_migrations_from_zero

    apply_migrations_from_zero(integration_database_url)

    # Register psycopg2's UUID adapter so grid_snapshot_id UUID columns return
    # as `uuid.UUID` and the byte-hash routines see a stable canonical form.
    import psycopg2.extras as _pg_extras

    _pg_extras.register_uuid()

    live_basin_version_ids: list[str] = []
    synthetic_registrations: list[dict[str, Any]] = []

    # --- Seed phase (single tx, autocommit=False, one commit at end) --------
    connection = psycopg2.connect(
        integration_database_url, cursor_factory=RealDictCursor
    )
    connection.autocommit = False
    try:
        with connection.cursor() as seed_cursor:
            _mo_seed_data_source(seed_cursor)
            for basin_index in range(1, _MECHANISM_ONLY_LIVE_BASIN_COUNT + 1):
                live_basin_version_ids.append(
                    _mo_seed_live_basin(seed_cursor, basin_index)
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    # --- Before snapshot ----------------------------------------------------
    connection = psycopg2.connect(
        integration_database_url, cursor_factory=RealDictCursor
    )
    connection.autocommit = True
    try:
        before_snapshots = [
            _mo_snapshot_live_basin(connection, basin_version_id)
            for basin_version_id in live_basin_version_ids
        ]
    finally:
        connection.close()

    # --- Register synthetic variants ---------------------------------------
    #
    # Each registration commits inside its own transaction so a mid-loop
    # failure surfaces at the offending index. This mirrors the caller
    # contract (register_direct_grid_variant borrows the caller's cursor;
    # here the caller is the test).
    for variant_index in range(1, _MECHANISM_ONLY_SYNTHETIC_VARIANT_COUNT + 1):
        connection = psycopg2.connect(
            integration_database_url, cursor_factory=RealDictCursor
        )
        connection.autocommit = False
        try:
            with connection.cursor() as reg_cursor:
                syn_bv, syn_rnv, syn_mesh, syn_pkg_uri = _mo_seed_synthetic_basin(
                    reg_cursor, variant_index
                )
                grid_snapshot_id, canonical_grid_key = _mo_seed_snapshot(
                    reg_cursor, variant_index
                )
                grid_id = f"mo972_grid_{variant_index:02d}"
                grid_signature = f"sha256:mo972-sig-{variant_index:02d}"
                payload = _mo_synthetic_payload(
                    variant_index, grid_id=grid_id, grid_signature=grid_signature
                )
                registration_input = DirectGridVariantRegistrationInput(
                    basin_version_id=syn_bv,
                    direct_grid_forcing=payload,
                    baseline=DirectGridBaselineModelInputs(
                        river_network_version_id=syn_rnv,
                        mesh_version_id=syn_mesh,
                        calibration_version_id=f"mo972-calib-{variant_index:02d}",
                        shud_code_version="mo972-shud-v1",
                        model_package_uri=syn_pkg_uri,
                    ),
                    grid_snapshot_id=grid_snapshot_id,
                )
                result = register_direct_grid_variant(reg_cursor, registration_input)
                assert result.inserted is True
                assert result.canonical_grid_key == canonical_grid_key
                # UUID adapter → `grid_snapshot_id` may return as `uuid.UUID`;
                # normalize to str for the string equality check.
                assert str(result.grid_snapshot_id) == str(grid_snapshot_id)
                synthetic_registrations.append(
                    {
                        "basin_version_id": syn_bv,
                        "model_id": str(result.model_id),
                        "canonical_grid_key": canonical_grid_key,
                        "grid_snapshot_id": str(grid_snapshot_id),
                    }
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    # --- After snapshot ----------------------------------------------------
    connection = psycopg2.connect(
        integration_database_url, cursor_factory=RealDictCursor
    )
    connection.autocommit = True
    try:
        after_snapshots = [
            _mo_snapshot_live_basin(connection, basin_version_id)
            for basin_version_id in live_basin_version_ids
        ]

        # --- Assert 1 + 2: byte-identical before/after per live basin -----
        assert len(before_snapshots) == _MECHANISM_ONLY_LIVE_BASIN_COUNT
        assert len(after_snapshots) == _MECHANISM_ONLY_LIVE_BASIN_COUNT
        for before, after in zip(before_snapshots, after_snapshots, strict=True):
            assert before["basin_version_id"] == after["basin_version_id"]
            assert before["active_model_id"] == after["active_model_id"], (
                f"live basin {before['basin_version_id']!r} lost its active model"
            )
            assert before["active_model_hash"] == after["active_model_hash"], (
                f"live basin {before['basin_version_id']!r} active model row drifted "
                f"(before={before['active_model_hash']}, after={after['active_model_hash']})"
            )
            assert before["active_station_count"] == after["active_station_count"], (
                f"live basin {before['basin_version_id']!r} active station count changed "
                f"(before={before['active_station_count']}, after={after['active_station_count']})"
            )
            assert before["active_stations_hash"] == after["active_stations_hash"], (
                f"live basin {before['basin_version_id']!r} active station set drifted "
                f"(before={before['active_stations_hash']}, after={after['active_stations_hash']})"
            )

        # --- Assert 3: every synthetic variant is inactive ----------------
        with connection.cursor() as verify_cursor:
            for reg in synthetic_registrations:
                verify_cursor.execute(
                    """
                    SELECT model_id, active_flag, lifecycle_state
                    FROM core.model_instance
                    WHERE model_id = %s
                    """,
                    (reg["model_id"],),
                )
                row = verify_cursor.fetchone()
                assert row is not None, (
                    f"registered variant {reg['model_id']!r} is missing from core.model_instance"
                )
                row_dict = dict(row)
                assert row_dict["active_flag"] is False, (
                    f"registered variant {reg['model_id']!r} has active_flag=true "
                    "(mechanism must land inactive by construction)"
                )
                assert row_dict["lifecycle_state"] == "inactive", (
                    f"registered variant {reg['model_id']!r} has "
                    f"lifecycle_state={row_dict['lifecycle_state']!r} (expected 'inactive')"
                )
                reg["lifecycle_state"] = row_dict["lifecycle_state"]
                reg["active_flag"] = row_dict["active_flag"]
    finally:
        connection.close()

    # --- Emit receipt to `artifacts/mechanism-only-receipts/` --------------
    #
    # Path is `artifacts/<subdir>/receipt-<UTC>.json` under the REPO ROOT so
    # the orchestrator can archive the file without extracting from the
    # pytest scratch dir. `artifacts/` is already gitignored (top-level).
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    receipt_dir = repo_root / "artifacts" / "mechanism-only-receipts"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    timestamp_utc = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    receipt_path = (
        receipt_dir / f"receipt-{timestamp_utc}-{_uuid.uuid4().hex[:8]}.json"
    )
    receipt = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "test_id": "test_mechanism_only_live_basins_undisturbed",
        "live_basin_count": _MECHANISM_ONLY_LIVE_BASIN_COUNT,
        "synthetic_variant_count": _MECHANISM_ONLY_SYNTHETIC_VARIANT_COUNT,
        "live_basins": [
            {
                "basin_version_id": snap["basin_version_id"],
                "active_model_id": snap["active_model_id"],
                "active_model_hash": snap["active_model_hash"],
                "active_station_count": snap["active_station_count"],
                "active_stations_hash": snap["active_stations_hash"],
            }
            for snap in after_snapshots
        ],
        "registered_synthetic_variants": [
            {
                "basin_version_id": reg["basin_version_id"],
                "model_id": reg["model_id"],
                "lifecycle_state": reg["lifecycle_state"],
                "active_flag": reg["active_flag"],
                "canonical_grid_key": reg["canonical_grid_key"],
                "grid_snapshot_id": reg["grid_snapshot_id"],
            }
            for reg in synthetic_registrations
        ],
        "verdict": (
            f"{_MECHANISM_ONLY_LIVE_BASIN_COUNT} live-analog basins byte-identical "
            f"before/after; all {_MECHANISM_ONLY_SYNTHETIC_VARIANT_COUNT} synthetic "
            "variants lifecycle_state=inactive."
        ),
    }
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8")
