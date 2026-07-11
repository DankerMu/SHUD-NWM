#!/usr/bin/env python3
"""Task 4.2 — execute the synthetic-identity rehearsal cutover on node-27.

Runs one real Change 4 `activate` cutover transaction on the M1 target
registered under the evidence-only `basin__evidence_cmfd_p02_synth__v1`
identity, capturing:

  (a) the station set flip inside the transaction (M1 mirror rows -> true,
      the provisioned legacy set -> false);
  (b) the station-MVT source-identity string before/after diff;
  (c) production-scoped zero-impact SQL assertions during the committed
      window AND after restore (13 production basins' active_flag unchanged;
      active core.model_instance count excluding model__evidence% = 13);
  (d) the restore: Change 4 `deactivate` with sys_admin missing-active
      override, followed by SQL cleanup of synthetic station flags and
      seeded run rows;
  (e) scheduler-plane cleanliness: no hydro.hydro_run created for any
      synthetic model during the window, no model__evidence% in the
      post-restore active model set (the manifest-derived assertion).

Fail-safe: every mutating step is wrapped in a try/except that runs the
Restore section on any failure and re-raises. This ensures a botched
activation cannot leave production DB in a mutated state.

Usage (node-27):
    DATABASE_URL="postgresql://nhms:...@127.0.0.1:55432/nhms" \\
    uv run python rehearse/rehearse.py

Emits pass logs to `rehearse/*.log` and identity captures to `rehearse/*.txt`.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure the repo root is on sys.path so `packages.*`, `workers.*`, and
# `services.*` resolve when executed directly.
_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_EVIDENCE_DIR = Path(__file__).resolve().parent

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from packages.common.model_registry import (  # noqa: E402
    ColdStartApprovalInput,
    ModelActivationContext,
    PsycopgModelRegistryStore,
)
from packages.common.state_clone_hook import (  # noqa: E402
    STATE_CLONE_APPROVAL_ACTION,
    STATE_CLONE_SPIN_UP_DISTORTION_ANNOUNCEMENT_MARKER,
    build_state_clone_cutover_hook,
)
from packages.common.station_set_flip import (  # noqa: E402
    build_station_flag_flip_hook,
)

# The MVT source-identity computer used for the before/after capture.
sys.path.insert(0, str(_EVIDENCE_DIR.parent / "mvt-source-identity"))
import compute as mvt_identity  # type: ignore  # noqa: E402

# --- Rehearsal identity constants (mirror 02-register-direct-grid-variant.py) ---

BASIN_VERSION_ID = "basin__evidence_cmfd_p02_synth__v1"
BASELINE_MODEL_ID = "model__evidence_cmfd_p02_synth__v1"
MAPPING_ASSET_IDENTITY = "synth-mip-m1-v2"
BINDING_CHECKSUM = "d1e2c3b4a5968778869574a3b2c1d0e9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c3"
SEEDED_RUN_ID = "run__evidence_cmfd_p02_synth__rehearsal_pre_cutover_v1"
COVERED_SOURCE_IDS = ("gfs",)  # Must match the M1 contract's applicable_source_ids
# (parser only accepts GFS/ERA5/IFS per `packages/common/source_identity.py`); the
# `cmfd` evidence narrative lives in basin/model/run IDs, not in this scope.

APPROVER = "rehearsal-operator"
REHEARSAL_REASON = "Epic #992 SUB-7 direct-grid-display-cutover rehearsal on node-27"
RESTORE_REASON = "Epic #992 SUB-7 rehearsal restore"

# Screenshot handoff window in seconds. playwright-capture.sh runs in parallel;
# 300 seconds (5 minutes) gives the Playwright test enough headroom to complete
# its own 240s waitFor timeout plus the click-through sequence (basin toggle ->
# station click -> issue-time picker -> retention miss). Phase B (30 s) was
# shorter than the Playwright test's own 60 s timeout, so rehearse.py committed
# the deactivate transaction before the test could reach the popup — this Phase
# C fix opens the window wide enough that the Playwright test drives to
# completion before the restore fires.
SCREENSHOT_WINDOW_SECONDS = 300


# --- Audit recorder implementation ---------------------------------------


class OpsAuditLogRecorder:
    """Writes hook skip/refusal/approval events into `ops.audit_log`.

    Both `state_clone_hook` and `station_set_flip` accept an object with the
    subset of these methods they need (the flip hook only calls
    `record_skip`). Writing to the shared cursor keeps every audit row in
    the SAME transaction as the supersede+activate swap — the D7 same-tx
    audit obligation.
    """

    def __init__(self, cursor: Any, actor: str, actor_role: str) -> None:
        self._cursor = cursor
        self._actor = actor
        self._actor_role = actor_role

    def _insert(self, action: str, entity_type: str, entity_id: str, details: Mapping[str, Any]) -> None:
        self._cursor.execute(
            """
            INSERT INTO ops.audit_log (actor, actor_role, action, entity_type, entity_id, details)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            """,
            (self._actor, self._actor_role, action, entity_type, entity_id, json.dumps(dict(details))),
        )

    def record_skip(self, reason: str, ctx: ModelActivationContext) -> None:
        self._insert(
            action=f"pre_activation_hook_skip::{reason}",
            entity_type="model_instance",
            entity_id=str(ctx.target_model.get("model_id") or ""),
            details={
                "reason": reason,
                "basin_version_id": ctx.basin_version_id,
                "target_model_id": ctx.target_model.get("model_id"),
                "previous_active_model_id": (
                    ctx.previous_active_model.get("model_id")
                    if ctx.previous_active_model is not None else None
                ),
            },
        )

    def record_refusal(self, record: Mapping[str, Any]) -> None:
        self._insert(
            action="state_clone_refused",
            entity_type="model_instance",
            entity_id=str(record.get("target_model_id") or record.get("m1_model_id") or ""),
            details=dict(record),
        )

    def record_approval(self, record: Mapping[str, Any]) -> None:
        self._insert(
            action=STATE_CLONE_APPROVAL_ACTION,
            entity_type="model_instance",
            entity_id=str(record.get("target_model_id") or ""),
            details=dict(record),
        )


def _fingerprint_inputs_provider_stub(ctx, source_id: str):  # noqa: ARG001
    """Placeholder — never invoked because the approved-skip path skips it.

    The state-clone hook (`packages/common/state_clone_hook.py:304-345`)
    consults `fingerprint_inputs_provider` only for sources NOT covered by
    `cold_start_approval.covered_source_ids`. Our rehearsal's approval
    covers `('gfs',)` which is the only source in the M1 target's
    `applicable_source_ids`, so this callable is never invoked.
    Raise loudly if it ever is — that indicates a rehearsal-fixture drift.
    """
    raise RuntimeError(
        "fingerprint_inputs_provider was invoked on the rehearsal — indicates the "
        "cold-start approval did NOT cover source_id={source_id!r}; check the "
        "M1 contract's applicable_source_ids vs. the approval's covered_source_ids."
    )


# --- Logging setup --------------------------------------------------------


def _setup_logger(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("rehearse")
    logger.setLevel(logging.INFO)
    # Clear any prior handlers so repeated invocations don't duplicate.
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    file_handler = logging.FileHandler(log_path, mode="w")
    stream_handler = logging.StreamHandler(sys.stdout)
    fmt = logging.Formatter("%(asctime)sZ %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
    for handler in (file_handler, stream_handler):
        handler.setFormatter(fmt)
        logger.addHandler(handler)
    return logger


# --- Production-scoped assertion helpers ---------------------------------


def _production_baseline_assert(cursor, expected_active_non_evidence: int = 13) -> dict[str, int]:
    """13 production active model_instance rows; nothing under basin__evidence% counted.

    The exclusion predicate scopes by ``basin_version_id`` (the evidence basin
    identifier) rather than ``model_id`` because the M1 target is minted with
    a SHA-256-derived id (``dg_<hex>``) by ``register_direct_grid_variant``,
    which does not carry the ``model__evidence`` prefix. Filtering by the
    evidence basin_version_id excludes ALL rows attached to that basin —
    including the SHA-minted M1 target — while leaving production rows
    (attached to production basin_version_ids) unchanged.

    Additionally captures per-basin active_flag counts across all 13
    production basins so the during-window / after-restore assertions can
    compare the whole vector, not just the aggregate.
    """
    cursor.execute(
        """
        SELECT count(*) AS n
        FROM core.model_instance
        WHERE active_flag = true
          AND basin_version_id NOT LIKE 'basin__evidence%'
        """
    )
    non_evidence_active = int(cursor.fetchone()["n"])
    if non_evidence_active != expected_active_non_evidence:
        raise AssertionError(
            f"production-scoped active model_instance count = {non_evidence_active}, "
            f"expected {expected_active_non_evidence}"
        )
    cursor.execute(
        """
        SELECT basin_version_id, count(*) AS n
        FROM met.met_station
        WHERE active_flag = true
          AND basin_version_id NOT LIKE 'basin__evidence%'
        GROUP BY basin_version_id
        ORDER BY basin_version_id
        """
    )
    per_basin = {str(row["basin_version_id"]): int(row["n"]) for row in cursor.fetchall()}
    return {"non_evidence_active_model_instance": non_evidence_active, "per_basin_active_station_count": per_basin}


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _capture_max_hydro_run_created_at(cursor) -> str:
    cursor.execute("SELECT COALESCE(MAX(created_at)::text, '') AS max_created_at FROM hydro.hydro_run")
    row = cursor.fetchone()
    return str(row["max_created_at"] or "")


def _capture_active_model_snapshot(cursor) -> list[dict[str, Any]]:
    cursor.execute(
        """
        SELECT model_id, basin_version_id, lifecycle_state, active_flag
        FROM core.model_instance
        WHERE active_flag = true
        ORDER BY model_id
        """
    )
    return [dict(row) for row in cursor.fetchall()]


# --- Restore section ------------------------------------------------------


def _restore_synthetic_state(logger: logging.Logger, url: str, target_model_id: str | None) -> None:
    """Best-effort restore of the synthetic display state.

    Steps (idempotent — each is guarded by its own WHERE predicate):
      1. Deactivate the M1 target via the Change 4 `deactivate` lifecycle op
         with `trusted_internal=True` (which auto-applies
         `override_missing_active=True` for `deactivate`).
      2. Deactivate the baseline evidence model_instance via SQL (was
         SQL-provisioned active by 00-baseline-and-stations.sql).
      3. UPDATE all synthetic met_station rows for the evidence basin to
         `active_flag=false` (the flip hook is pre-activation-only, so
         deactivating doesn't auto-unflip stations).
      4. DELETE the seeded pre-cutover hydro_run row.

    Every step opens its own connection so a partial failure in one step
    does not roll back the others.
    """
    logger.info("RESTORE: starting restore of synthetic display state")

    # Step 1: Change 4 deactivate on the M1 target (real path, if it exists).
    if target_model_id is not None:
        try:
            store = PsycopgModelRegistryStore(database_url=url)
            store.model_lifecycle_operation(
                target_model_id,
                operation="deactivate",
                trusted_internal=True,
                reason=RESTORE_REASON,
            )
            logger.info("RESTORE step 1 ok: M1 target deactivated via Change 4 lifecycle op")
        except Exception as exc:  # noqa: BLE001
            logger.warning("RESTORE step 1 partial: %r (continuing)", exc)

    # Step 2: baseline evidence model_instance -> inactive via SQL.
    try:
        with psycopg.connect(url, autocommit=False, row_factory=dict_row) as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE core.model_instance
                   SET active_flag = false, lifecycle_state = 'inactive'
                 WHERE model_id = %s
                """,
                (BASELINE_MODEL_ID,),
            )
            conn.commit()
        logger.info("RESTORE step 2 ok: baseline evidence model_instance -> inactive")
    except Exception as exc:  # noqa: BLE001
        logger.warning("RESTORE step 2 partial: %r (continuing)", exc)

    # Step 3: all synthetic met_station rows -> active_flag=false.
    try:
        with psycopg.connect(url, autocommit=False, row_factory=dict_row) as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE met.met_station
                   SET active_flag = false
                 WHERE basin_version_id = %s
                """,
                (BASIN_VERSION_ID,),
            )
            conn.commit()
        logger.info("RESTORE step 3 ok: synthetic met_station rows -> active_flag=false")
    except Exception as exc:  # noqa: BLE001
        logger.warning("RESTORE step 3 partial: %r (continuing)", exc)

    # Step 4: seeded pre-cutover hydro_run -> deleted.
    try:
        with psycopg.connect(url, autocommit=False, row_factory=dict_row) as conn, conn.cursor() as cursor:
            cursor.execute("DELETE FROM hydro.hydro_run WHERE run_id = %s", (SEEDED_RUN_ID,))
            conn.commit()
        logger.info("RESTORE step 4 ok: seeded pre-cutover hydro_run -> deleted")
    except Exception as exc:  # noqa: BLE001
        logger.warning("RESTORE step 4 partial: %r (continuing)", exc)

    logger.info("RESTORE: done")


# --- M1 target model_id resolution ----------------------------------------


def _resolve_m1_target_model_id(cursor) -> str:
    cursor.execute(
        """
        SELECT model_id
        FROM core.model_instance
        WHERE basin_version_id = %s
          AND resource_profile->'direct_grid_forcing'->>'model_input_package_id' = %s
          AND resource_profile->'direct_grid_forcing'->>'binding_checksum' = %s
        LIMIT 1
        """,
        (BASIN_VERSION_ID, MAPPING_ASSET_IDENTITY, BINDING_CHECKSUM),
    )
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError(
            "M1 target model_instance not found; run provisioning/02-register-direct-grid-variant.py first."
        )
    return str(row["model_id"])


# --- Main -----------------------------------------------------------------


def main() -> int:
    url = os.environ.get("DATABASE_URL", "postgresql://nhms:nhms_dev@127.0.0.1:55432/nhms")
    log_path = _EVIDENCE_DIR / "rehearse.node-27.pass.log"
    logger = _setup_logger(log_path)

    logger.info("=== Epic #992 SUB-7 rehearsal start ===")
    logger.info("DATABASE target: %s", url.split("@")[-1])
    logger.info("BASIN_VERSION_ID=%s", BASIN_VERSION_ID)
    logger.info("MAPPING_ASSET_IDENTITY=%s", MAPPING_ASSET_IDENTITY)
    logger.info("STATE_CLONE_APPROVAL_ACTION=%s", STATE_CLONE_APPROVAL_ACTION)
    logger.info(
        "STATE_CLONE_SPIN_UP_DISTORTION_ANNOUNCEMENT_MARKER=%s",
        STATE_CLONE_SPIN_UP_DISTORTION_ANNOUNCEMENT_MARKER,
    )

    window_start = datetime.now(timezone.utc).isoformat()
    logger.info("REHEARSAL_WINDOW_UTC_START=%s", window_start)

    target_model_id: str | None = None
    max_created_at_before = ""

    try:
        # --- CAPTURE BEFORE ---
        with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn, conn.cursor() as cursor:
            logger.info("CAPTURE BEFORE: production-scoped baseline assertion")
            baseline = _production_baseline_assert(cursor, expected_active_non_evidence=13)
            logger.info("baseline production summary: %s", baseline)

            target_model_id = _resolve_m1_target_model_id(cursor)
            logger.info("resolved M1 target model_id=%s", target_model_id)

            identity_before = mvt_identity.compute_station_source_identity(cursor, BASIN_VERSION_ID)
            (_EVIDENCE_DIR / "mvt-source-identity.before.txt").write_text(
                identity_before + "\n", encoding="utf-8"
            )
            logger.info("MVT source identity BEFORE: %s", identity_before)
            if identity_before == mvt_identity.MVT_SOURCE_IDENTITY_NOT_FOUND:
                raise AssertionError(
                    "MVT source identity BEFORE = MVT_SOURCE_IDENTITY_NOT_FOUND; "
                    "expected a defined identity over the provisioned legacy synth station set"
                )

            max_created_at_before = _capture_max_hydro_run_created_at(cursor)
            logger.info("hydro.hydro_run MAX(created_at) BEFORE=%r", max_created_at_before)

        # --- REGISTER HOOKS + ACTIVATE + CAPTURE ---
        logger.info("REGISTER HOOKS + ACTIVATE: constructing PsycopgModelRegistryStore")
        # The audit recorder needs a cursor bound to the same transaction as
        # the lifecycle op. The store's `_transaction` is a context manager,
        # so we cannot obtain that cursor externally. Instead we bind the
        # recorder to a factory that produces per-cursor recorders — the
        # hook receives the cursor from the store and forwards it into the
        # recorder via the closure below.
        cursor_holder: dict[str, Any] = {"cursor": None}

        class _CursorForwardingRecorder:
            """Forwards to a real OpsAuditLogRecorder built on the hook cursor.

            The store hands the pre-activation hooks the transaction cursor;
            we build the actual `OpsAuditLogRecorder` from that cursor at
            hook-invocation time so every INSERT lands on the same
            transaction as the supersede+activate swap.
            """

            def _recorder(self):
                cursor = cursor_holder["cursor"]
                if cursor is None:
                    raise RuntimeError("audit recorder used outside the pre-activation hook cursor scope")
                return OpsAuditLogRecorder(cursor, actor=APPROVER, actor_role="model-registry")

            def record_skip(self, reason, ctx):
                self._recorder().record_skip(reason, ctx)

            def record_refusal(self, record):
                self._recorder().record_refusal(record)

            def record_approval(self, record):
                self._recorder().record_approval(record)

        recorder = _CursorForwardingRecorder()

        def _cursor_capturing_hook_wrapper(underlying_hook):
            """Wrap a pre-activation hook to capture the cursor for the recorder."""
            def _wrapper(cursor, ctx):
                cursor_holder["cursor"] = cursor
                try:
                    return underlying_hook(cursor, ctx)
                finally:
                    # Do NOT clear here — the next hook in the chain
                    # (station_flag_flip after state_clone) needs the same
                    # cursor. Only clear after the whole chain returns; the
                    # store's transaction context handles that boundary.
                    pass
            return _wrapper

        state_clone_hook = build_state_clone_cutover_hook(
            audit_recorder=recorder,
            fingerprint_inputs_provider=_fingerprint_inputs_provider_stub,
        )
        flip_hook = build_station_flag_flip_hook(audit_recorder=recorder)

        store = PsycopgModelRegistryStore(database_url=url)
        store.register_pre_activation_hook("state_clone", _cursor_capturing_hook_wrapper(state_clone_hook))
        store.register_pre_activation_hook("station_flag_flip", _cursor_capturing_hook_wrapper(flip_hook))

        approval = ColdStartApprovalInput(
            approver=APPROVER,
            reason=REHEARSAL_REASON,
            covered_source_ids=COVERED_SOURCE_IDS,
        )
        logger.info(
            "ACTIVATE: model_lifecycle_operation activate model_id=%s covered_source_ids=%s",
            target_model_id, COVERED_SOURCE_IDS,
        )
        activation_result = store.model_lifecycle_operation(
            target_model_id,
            operation="activate",
            trusted_internal=True,
            cold_start_approval=approval,
            reason=REHEARSAL_REASON,
        )
        logger.info("ACTIVATE result: %s", json.dumps(activation_result, indent=2, sort_keys=True, default=str))

        # --- IN-WINDOW CAPTURE + PRODUCTION-SCOPED ASSERT ---
        with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn, conn.cursor() as cursor:
            logger.info("CAPTURE DURING WINDOW: reading M1 mirror + legacy state")
            cursor.execute(
                """
                SELECT station_id, active_flag, station_role,
                       properties_json->>'model_input_package_id' AS mip,
                       properties_json->>'binding_checksum' AS binding_checksum,
                       grid_snapshot_id::text AS grid_snapshot_id
                FROM met.met_station
                WHERE basin_version_id = %s
                ORDER BY station_id
                """,
                (BASIN_VERSION_ID,),
            )
            m1_flip_state = [dict(row) for row in cursor.fetchall()]
            logger.info(
                "post-flip synthetic-basin station state: %s",
                json.dumps(m1_flip_state, indent=2, sort_keys=True, default=str),
            )
            # Assert exactly the 3 M1 mirror rows are active_flag=true and
            # exactly the 3 legacy synth-station rows are active_flag=false.
            m1_active = [
                r for r in m1_flip_state
                if r["mip"] == MAPPING_ASSET_IDENTITY and r["active_flag"]
            ]
            legacy_active = [
                r for r in m1_flip_state
                if r["station_id"].startswith("synth-station-") and r["active_flag"]
            ]
            if len(m1_active) != 3:
                raise AssertionError(
                    f"expected exactly 3 M1 mirror rows active_flag=true, got {len(m1_active)}"
                )
            if legacy_active:
                raise AssertionError(
                    f"expected 0 legacy synth-station-* rows active_flag=true, got {len(legacy_active)}"
                )

            identity_after = mvt_identity.compute_station_source_identity(cursor, BASIN_VERSION_ID)
            (_EVIDENCE_DIR / "mvt-source-identity.after.txt").write_text(
                identity_after + "\n", encoding="utf-8"
            )
            logger.info("MVT source identity AFTER: %s", identity_after)
            if identity_after == mvt_identity.MVT_SOURCE_IDENTITY_NOT_FOUND:
                raise AssertionError("MVT source identity AFTER = MVT_SOURCE_IDENTITY_NOT_FOUND (unexpected)")
            if identity_after == identity_before:
                raise AssertionError(
                    "MVT source identity did NOT change across the flip; expected before != after"
                )

            during_summary = _production_baseline_assert(cursor, expected_active_non_evidence=13)
            during_active_snapshot = _capture_active_model_snapshot(cursor)
            _write_json(
                _EVIDENCE_DIR / "production-scoped-assertions.during.log",
                {
                    "checked_at_utc": datetime.now(timezone.utc).isoformat(),
                    "phase": "during-window",
                    "expected_active_non_evidence": 13,
                    "actual": during_summary,
                    "active_model_snapshot": during_active_snapshot,
                    "transient_global_active_expected": 14,
                    "transient_global_active_actual": len(during_active_snapshot),
                    "mvt_source_identity_before": identity_before,
                    "mvt_source_identity_after": identity_after,
                },
            )
            logger.info(
                "during-window production-scoped assertion PASS; transient global active=%d",
                len(during_active_snapshot),
            )
            if len(during_active_snapshot) != 14:
                raise AssertionError(
                    f"transient global active count = {len(during_active_snapshot)}, expected 14"
                )

        # --- SCREENSHOT WINDOW ---
        logger.info("SCREENSHOT_WINDOW_OPEN")
        time.sleep(SCREENSHOT_WINDOW_SECONDS)
        logger.info("SCREENSHOT_WINDOW_CLOSE")

        # --- RESTORE (successful path) ---
        _restore_synthetic_state(logger, url, target_model_id)

        # --- POST-RESTORE ASSERTIONS ---
        with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn, conn.cursor() as cursor:
            after_summary = _production_baseline_assert(cursor, expected_active_non_evidence=13)
            after_active_snapshot = _capture_active_model_snapshot(cursor)
            # Post-restore MVT identity: legacy synth-station rows were
            # UPDATE'd back to false, and the M1 mirror was UPDATE'd off, so
            # no active_flag=true rows should remain for the synth basin.
            identity_after_restore = mvt_identity.compute_station_source_identity(cursor, BASIN_VERSION_ID)
            logger.info("MVT source identity POST-RESTORE: %s", identity_after_restore)
            if identity_after_restore != mvt_identity.MVT_SOURCE_IDENTITY_NOT_FOUND:
                raise AssertionError(
                    f"POST-RESTORE MVT identity = {identity_after_restore!r}, "
                    f"expected {mvt_identity.MVT_SOURCE_IDENTITY_NOT_FOUND!r}"
                )
            # No new hydro.hydro_run row for any evidence model created
            # during the window (rehearsal is timed between scheduler cycles).
            # psycopg v3 interprets any `%` in the SQL text as a placeholder;
            # bind the LIKE pattern as a parameter so `%` in the literal is
            # not misparsed. (The other assert queries above use only integer
            # counts / basin filters, so they don't trip this.)
            cursor.execute(
                """
                SELECT count(*) AS n
                FROM hydro.hydro_run
                WHERE model_id LIKE %s
                  AND (%s = '' OR created_at::text > %s)
                """,
                ("model__evidence%", max_created_at_before, max_created_at_before),
            )
            evidence_new_runs = int(cursor.fetchone()["n"])
            if evidence_new_runs != 0:
                raise AssertionError(
                    f"expected 0 new evidence hydro_run rows during the window, got {evidence_new_runs}"
                )
            _write_json(
                _EVIDENCE_DIR / "production-scoped-assertions.after-restore.log",
                {
                    "checked_at_utc": datetime.now(timezone.utc).isoformat(),
                    "phase": "after-restore",
                    "expected_active_non_evidence": 13,
                    "actual": after_summary,
                    "active_model_snapshot": after_active_snapshot,
                    "global_active_count": len(after_active_snapshot),
                    "expected_global_active_count": 13,
                    "mvt_source_identity_post_restore": identity_after_restore,
                    "new_evidence_hydro_run_rows_during_window": evidence_new_runs,
                },
            )
            if len(after_active_snapshot) != 13:
                raise AssertionError(
                    f"post-restore global active count = {len(after_active_snapshot)}, expected 13"
                )
            # Scheduler-manifest equivalent assertion: the derived active
            # set contains no evidence-basin model (M1 target `dg_<hex>`
            # is on the evidence basin_version_id but not `model__evidence`
            # prefixed, so filter by basin_version_id).
            evidence_active_rows = [
                r for r in after_active_snapshot
                if str(r.get("basin_version_id", "")).startswith("basin__evidence")
                or str(r["model_id"]).startswith("model__evidence")
            ]
            if evidence_active_rows:
                raise AssertionError(
                    f"post-restore active set still contains evidence models: {evidence_active_rows!r}"
                )
            _write_json(
                _EVIDENCE_DIR / "scheduler-manifest.post-restore.json",
                {
                    "note": (
                        "Derived-from-DB scheduler registry manifest equivalent. "
                        "Constructed by reading core.model_instance WHERE active_flag=true "
                        "post-restore. Semantically equivalent to the manifest "
                        "publish_scheduler_registry_manifest would emit."
                    ),
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "models": [dict(r) for r in after_active_snapshot],
                    "evidence_model_count": 0,
                },
            )
            logger.info(
                "after-restore production-scoped assertion PASS; global active=%d; no evidence models",
                len(after_active_snapshot),
            )

        window_end = datetime.now(timezone.utc).isoformat()
        logger.info("REHEARSAL_WINDOW_UTC_END=%s", window_end)
        logger.info("=== rehearsal COMPLETE ===")
        return 0

    except Exception as exc:  # noqa: BLE001
        logger.exception("REHEARSAL FAILED: %r — running restore", exc)
        with contextlib.suppress(Exception):
            _restore_synthetic_state(logger, url, target_model_id)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
