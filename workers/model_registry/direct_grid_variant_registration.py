"""Direct-grid variant registration surface (SUB-1 / issue #962).

Implements the `core.model_instance` insert leg of the source-specific
model-variant routing change (Epic #961, tasks.md §1.1). Registers a built
direct-grid variant as a NEW `core.model_instance` row at the
`(basin_version_id, canonical_grid_key)` grain, keyed for cross-source dedup
and idempotency on the built mapping-asset identity
(`model_input_package_id`, `binding_checksum`) from the §7.2 manifest.

Boundaries (§1.1 non-goals — owned by sibling sub-issues):

* `met.met_station` mirror write → SUB-2 (#963).
* Legacy-row retention regression + no-duplicate-mirror idempotency → SUB-3 (#964).
* Producer `active_flag` ownership → SUB-4 (#965).
* Lifecycle activation, scheduler manifest re-publish → §2 group.
* Guard, dispatch enforcement → §3–§4 groups.

Contract:

* The registration input carries the direct-grid contract payload
  (parser-shaped, matching `workers/forcing_producer/direct_grid_contract.py`),
  the baseline `core.model_instance` NOT NULL fields, and either an explicit
  `grid_snapshot_id` or the built manifest's `grid_signature`/`grid_id` for
  resolution against `met.canonical_grid_snapshot`.
* The surface copies the snapshot row's `canonical_grid_key` verbatim (never
  recomputing it) and persists it at `resource_profile.canonical_grid_key`
  (top-level, alongside — not inside — the parser-validated
  `direct_grid_forcing` block) so the grain/idempotency lookup runs over
  existing columns with no new migration (design.md §D8).
* A registered row lands `active_flag=false` (literal in the INSERT list) and
  `lifecycle_state` is omitted from the INSERT list so the `'inactive'`
  column default applies (matches `basins_registry_import._ensure_model_instance`
  precedent).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from workers.forcing_producer.direct_grid_contract import (
    DirectGridContractError,
    load_forcing_mapping_contract_from_manifest,
)

DIRECT_GRID_VARIANT_LINEAGE = "direct_grid_variant_registration"
DIRECT_GRID_VARIANT_MODEL_ID_PREFIX = "dg"


class DirectGridVariantRegistrationError(RuntimeError):
    """Raised when a direct-grid variant cannot be registered.

    Mirrors the shape of `BasinsRegistryImportError` so callers can uniformly
    surface error codes and structured details.
    """

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        model_id: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.model_id = model_id
        self.details = dict(details or {})

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error_code": self.error_code, "message": str(self)}
        if self.model_id is not None:
            payload["model_id"] = self.model_id
        payload.update(self.details)
        return payload


@dataclass(frozen=True)
class DirectGridBaselineModelInputs:
    """Baseline `core.model_instance` NOT NULL fields inherited from the basin.

    These are the columns that cannot be defaulted at INSERT time (per
    `db/migrations/000004_core.sql:71-85`). The caller derives them from the
    baseline model row (typically the currently active legacy IDW row on the
    same `basin_version_id`).
    """

    river_network_version_id: str
    mesh_version_id: str
    calibration_version_id: str
    shud_code_version: str
    model_package_uri: str


@dataclass(frozen=True)
class DirectGridVariantRegistrationInput:
    """Input to :func:`register_direct_grid_variant`.

    Exactly one of ``grid_snapshot_id`` OR (``grid_signature`` + ``grid_id``)
    MUST be supplied to resolve the registered `met.canonical_grid_snapshot`
    row. Providing both is accepted; the explicit `grid_snapshot_id` wins.
    """

    basin_version_id: str
    direct_grid_forcing: Mapping[str, Any]
    baseline: DirectGridBaselineModelInputs
    grid_snapshot_id: str | None = None
    grid_signature: str | None = None
    grid_id: str | None = None


@dataclass(frozen=True)
class DirectGridVariantRegistrationResult:
    """Outcome of a single `register_direct_grid_variant` call."""

    model_id: str
    inserted: bool
    canonical_grid_key: str
    grid_snapshot_id: str


def register_direct_grid_variant(
    cursor: Any,
    registration_input: DirectGridVariantRegistrationInput,
) -> DirectGridVariantRegistrationResult:
    """Register a built direct-grid variant as a `core.model_instance` row.

    The caller opens the DB transaction — this function only issues statements
    on the supplied cursor, mirroring the
    `basins_registry_import.import_basin_into_registry_core` cursor-borrowing
    convention.

    Returns
    -------
    DirectGridVariantRegistrationResult
        Whether a new row was inserted (``inserted=True``) or an existing row
        with the same built-asset identity was returned (``inserted=False``),
        plus the resolved snapshot identity for downstream mirror writes.

    Raises
    ------
    DirectGridVariantRegistrationError
        Invalid baseline inputs, invalid direct-grid contract payload, or
        snapshot resolution failure. All raise-cases leave the transaction
        state unchanged (no partial insert).
    """

    _validate_baseline(registration_input.baseline)
    contract_payload = _validate_contract_payload(registration_input.direct_grid_forcing)

    canonical_grid_key, grid_snapshot_id = _resolve_snapshot(cursor, registration_input)

    existing_model_id = _lookup_existing_variant(
        cursor,
        basin_version_id=registration_input.basin_version_id,
        canonical_grid_key=canonical_grid_key,
        model_input_package_id=contract_payload["model_input_package_id"],
        binding_checksum=contract_payload["binding_checksum"],
    )
    if existing_model_id is not None:
        return DirectGridVariantRegistrationResult(
            model_id=existing_model_id,
            inserted=False,
            canonical_grid_key=canonical_grid_key,
            grid_snapshot_id=grid_snapshot_id,
        )

    model_id = _mint_model_id(
        basin_version_id=registration_input.basin_version_id,
        canonical_grid_key=canonical_grid_key,
        model_input_package_id=contract_payload["model_input_package_id"],
        binding_checksum=contract_payload["binding_checksum"],
    )
    resource_profile = _build_resource_profile(
        canonical_grid_key=canonical_grid_key,
        grid_snapshot_id=grid_snapshot_id,
        contract_payload=contract_payload,
    )
    _insert_variant_row(
        cursor,
        model_id=model_id,
        registration_input=registration_input,
        resource_profile=resource_profile,
    )
    return DirectGridVariantRegistrationResult(
        model_id=model_id,
        inserted=True,
        canonical_grid_key=canonical_grid_key,
        grid_snapshot_id=grid_snapshot_id,
    )


# --- validation ------------------------------------------------------------


def _validate_baseline(baseline: DirectGridBaselineModelInputs) -> None:
    """Ensure every `core.model_instance` NOT NULL column has a value.

    We fail early with a domain error rather than letting the INSERT hit a
    `NOT NULL` violation and roll back the whole caller transaction.
    """

    missing: list[str] = []
    for field_name in (
        "river_network_version_id",
        "mesh_version_id",
        "calibration_version_id",
        "shud_code_version",
        "model_package_uri",
    ):
        value = getattr(baseline, field_name)
        if not isinstance(value, str) or not value.strip():
            missing.append(field_name)
    if missing:
        raise DirectGridVariantRegistrationError(
            "DIRECT_GRID_VARIANT_BASELINE_MISSING",
            "Direct-grid variant registration is missing required baseline model_instance fields.",
            details={"missing_fields": missing},
        )


def _validate_contract_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Parse the direct-grid payload through the runtime producer's parser.

    The parser is the single authority for the contract shape (design.md D6).
    We pass the payload in its "flat" form — the same shape the runtime
    producer's tests build via `_direct_grid_manifest()` — so that the
    persisted `resource_profile.direct_grid_forcing` block round-trips through
    `load_forcing_mapping_contract_from_manifest` directly, without a caller
    having to re-wrap it.

    Returns
    -------
    dict[str, Any]
        A shallow copy of the input payload with
        ``forcing_mapping_mode='direct_grid'`` set (defense-in-depth) and
        ``applicable_source_ids`` replaced by the parser's normalized tuple
        form (`gfs`/`IFS`), ready for persistence at
        ``resource_profile.direct_grid_forcing``.
    """

    if not isinstance(payload, Mapping):
        raise DirectGridVariantRegistrationError(
            "DIRECT_GRID_VARIANT_CONTRACT_INVALID",
            "Direct-grid contract payload must be a JSON object.",
        )
    prepared: dict[str, Any] = dict(payload)
    # The parser requires forcing_mapping_mode inside the contract payload.
    # Registration only accepts direct-grid variants, so we assert the mode
    # and reject a mismatched pre-supplied value.
    mode = prepared.get("forcing_mapping_mode")
    if mode is not None and mode != "direct_grid":
        raise DirectGridVariantRegistrationError(
            "DIRECT_GRID_VARIANT_CONTRACT_INVALID",
            "Direct-grid contract payload carries a non-direct forcing_mapping_mode.",
            details={"forcing_mapping_mode": mode},
        )
    prepared["forcing_mapping_mode"] = "direct_grid"
    try:
        contract = load_forcing_mapping_contract_from_manifest(prepared)
    except DirectGridContractError as error:
        raise DirectGridVariantRegistrationError(
            "DIRECT_GRID_VARIANT_CONTRACT_INVALID",
            f"Direct-grid contract payload failed parser validation: {error}",
            details={"parser_error": error.to_dict()},
        ) from error
    if contract is None:
        raise DirectGridVariantRegistrationError(
            "DIRECT_GRID_VARIANT_CONTRACT_INVALID",
            "Direct-grid contract payload did not resolve to a direct-grid contract.",
        )
    prepared["applicable_source_ids"] = list(contract.applicable_source_ids)
    return prepared


# --- snapshot resolution ---------------------------------------------------


def _resolve_snapshot(
    cursor: Any,
    registration_input: DirectGridVariantRegistrationInput,
) -> tuple[str, str]:
    """Return ``(canonical_grid_key, grid_snapshot_id)`` for the registered snapshot.

    Resolution precedence: an explicit ``grid_snapshot_id`` on the input wins.
    Otherwise the built manifest's ``grid_signature`` + ``grid_id`` are used to
    look up the snapshot registered by the ``canonical-source-grid-registry``
    change.

    Raises
    ------
    DirectGridVariantRegistrationError
        Neither resolution path was satisfied by an existing snapshot row.
    """

    if registration_input.grid_snapshot_id:
        cursor.execute(
            """
            SELECT grid_snapshot_id, canonical_grid_key
            FROM met.canonical_grid_snapshot
            WHERE grid_snapshot_id = %s
            """,
            (registration_input.grid_snapshot_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise DirectGridVariantRegistrationError(
                "DIRECT_GRID_VARIANT_SNAPSHOT_NOT_FOUND",
                "Registered canonical grid snapshot not found by grid_snapshot_id.",
                details={"grid_snapshot_id": registration_input.grid_snapshot_id},
            )
        row_dict = dict(row)
        return str(row_dict["canonical_grid_key"]), str(row_dict["grid_snapshot_id"])

    if not registration_input.grid_id or not registration_input.grid_signature:
        raise DirectGridVariantRegistrationError(
            "DIRECT_GRID_VARIANT_SNAPSHOT_INPUT_MISSING",
            "Snapshot resolution requires either grid_snapshot_id or (grid_signature + grid_id).",
        )
    cursor.execute(
        """
        SELECT grid_snapshot_id, canonical_grid_key
        FROM met.canonical_grid_snapshot
        WHERE grid_id = %s AND grid_signature = %s
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (registration_input.grid_id, registration_input.grid_signature),
    )
    row = cursor.fetchone()
    if row is None:
        raise DirectGridVariantRegistrationError(
            "DIRECT_GRID_VARIANT_SNAPSHOT_NOT_FOUND",
            "Registered canonical grid snapshot not found by (grid_id, grid_signature).",
            details={
                "grid_id": registration_input.grid_id,
                "grid_signature": registration_input.grid_signature,
            },
        )
    row_dict = dict(row)
    return str(row_dict["canonical_grid_key"]), str(row_dict["grid_snapshot_id"])


# --- idempotency lookup + insert ------------------------------------------


def _lookup_existing_variant(
    cursor: Any,
    *,
    basin_version_id: str,
    canonical_grid_key: str,
    model_input_package_id: str,
    binding_checksum: str,
) -> str | None:
    """Return the existing variant's ``model_id`` if the grain is already registered.

    The lookup keys the built-asset identity on the SAME JSONB paths the INSERT
    populates: ``resource_profile.canonical_grid_key`` (top-level, per design
    D1) plus ``resource_profile.direct_grid_forcing.{model_input_package_id,
    binding_checksum}``. Keeping the paths symmetric between INSERT and lookup
    is what makes the grain a query over existing columns with no new
    migration (design D8).
    """

    cursor.execute(
        """
        SELECT model_id
        FROM core.model_instance
        WHERE basin_version_id = %s
          AND resource_profile->>'canonical_grid_key' = %s
          AND resource_profile->'direct_grid_forcing'->>'model_input_package_id' = %s
          AND resource_profile->'direct_grid_forcing'->>'binding_checksum' = %s
        LIMIT 1
        """,
        (basin_version_id, canonical_grid_key, model_input_package_id, binding_checksum),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return str(dict(row)["model_id"])


def _mint_model_id(
    *,
    basin_version_id: str,
    canonical_grid_key: str,
    model_input_package_id: str,
    binding_checksum: str,
) -> str:
    """Deterministic model_id derived from the grain + built-asset identity.

    The same asset re-registered under the same grain therefore mints the same
    id (the idempotency lookup already short-circuits before mint, but the
    determinism guards against unlikely INSERT retries producing a divergent
    id). A different asset (fix-forward M1→M1′) mints a different id, so both
    generations coexist as distinct rows per D1.
    """

    payload = "|".join(
        (basin_version_id, canonical_grid_key, model_input_package_id, binding_checksum)
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:32]
    return f"{DIRECT_GRID_VARIANT_MODEL_ID_PREFIX}_{digest}"


def _build_resource_profile(
    *,
    canonical_grid_key: str,
    grid_snapshot_id: str,
    contract_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Assemble the persisted `resource_profile` JSONB.

    Layout matches design D1: ``canonical_grid_key`` sits at the top level
    (grain/idempotency lookup target) alongside — NOT inside — the
    parser-validated ``direct_grid_forcing`` block, so the JSONB paths used
    by the INSERT and by the idempotency SELECT stay symmetric.
    """

    return {
        "lineage": DIRECT_GRID_VARIANT_LINEAGE,
        "canonical_grid_key": canonical_grid_key,
        "grid_snapshot_id": grid_snapshot_id,
        "direct_grid_forcing": dict(contract_payload),
    }


def _insert_variant_row(
    cursor: Any,
    *,
    model_id: str,
    registration_input: DirectGridVariantRegistrationInput,
    resource_profile: Mapping[str, Any],
) -> None:
    """Insert the `core.model_instance` row.

    Mirrors the `basins_registry_import._ensure_model_instance` INSERT
    template: ``active_flag=false`` is a literal (never bound from user
    input), and ``lifecycle_state`` is omitted from the column list so the
    ``'inactive'`` default from `db/migrations/000022_model_asset_lifecycle.sql`
    applies. Registration therefore never produces an active variant.
    """

    baseline = registration_input.baseline
    cursor.execute(
        """
        INSERT INTO core.model_instance (
            model_id, basin_version_id, river_network_version_id, mesh_version_id,
            calibration_version_id, shud_code_version, model_package_uri, active_flag, resource_profile
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, false, %s)
        """,
        (
            model_id,
            registration_input.basin_version_id,
            baseline.river_network_version_id,
            baseline.mesh_version_id,
            baseline.calibration_version_id,
            baseline.shud_code_version,
            baseline.model_package_uri,
            _json(dict(resource_profile)),
        ),
    )


# --- psycopg2 JSONB helper (parity with basins_registry_import._json) ------


def _json(value: dict[str, Any]) -> Any:
    """Wrap a dict for JSONB binding, matching `basins_registry_import._json`.

    Kept as a thin wrapper (rather than importing the sibling helper) so the
    two modules stay independently reviewable and this module has no import
    dependency on the Basins registry surface.
    """

    try:
        from psycopg2.extras import Json
    except ImportError as error:  # pragma: no cover - psycopg2 is a required dep
        raise DirectGridVariantRegistrationError(
            "DIRECT_GRID_VARIANT_PSYCOPG_MISSING",
            "psycopg2 is required for direct-grid variant registration.",
        ) from error
    return Json(value)


# --- convenience for test/read paths ---------------------------------------


def _json_dict(value: Any) -> dict[str, Any]:
    """Coerce a JSONB column value to a plain dict, defensively.

    Used only by tests and read paths that inspect a returned
    ``resource_profile``; the writer path exclusively takes dicts.
    """

    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}
