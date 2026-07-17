"""Generation-aware cutover consumer for the DB-free scheduler (Issue #1081).

This module implements the OpenSpec §8 delta of
``node22-db-free-scheduler-state``: the scheduler-side consumer of the
``nhms.scheduler.registry_package_cutover.v1`` declaration channel emitted by
the registry publisher (schema landed by #1080), plus the deterministic
``generation`` token that threads through candidate construction, state-index
lookup, backfill selection, and evidence.

Decision surface
----------------
``evaluate_transition_decision`` returns a ``TransitionEvaluation`` whose
``decision`` is one of the eight closed enum values pinned by D8.8:

Admit:
  - ``warm_continue`` — same-generation exact predecessor exists.
  - ``cold_new_model`` — no prior state history for this ``model_id`` in ANY
    generation.
  - ``cold_declared_cutover`` — a valid declaration admits a cold start at
    exactly ``effective_cycle_utc``.

Block (each maps 1:1 to a typed reason surfaced in candidate evidence):
  - ``block_predecessor_pending`` →
    ``state_snapshot_index_prior_checkpoint_missing_after_history``.
  - ``block_declaration_missing`` →
    ``registry_cutover_declaration_missing``.
  - ``block_declaration_stale`` →
    ``registry_cutover_declaration_stale``.
  - ``block_cold_start_out_of_window`` →
    ``registry_cutover_cold_start_out_of_window``.
  - ``block_wrong_generation`` →
    ``state_snapshot_index_generation_mismatch``.

Design constraints
------------------
- Declaration loading happens at scheduler-planning time (D8.1) so a mid-plan
  declaration change cannot corrupt an in-flight candidate.
- The generation token is derived deterministically from the
  ``package_checksum`` following the ``manifest-<12hex>`` convention mirrored
  from ``scripts/scheduler_file_provider_refresh._prospective_registry_generation``
  (D8.2); scheduler evidence records the full checksum plus the short form.
- ``NHMS_REQUIRE_FORECAST_WARM_START=false`` continues to affect only optional
  warm-start hints (D-must-preserve): this module never admits a
  declaration-less cutover, a missing predecessor, or a wrong-generation
  checkpoint on the basis of that flag.
- Old-generation state entries remain audit-readable but are quarantined from
  current-generation warm-start / readiness scoring (D8.3, D8.7).
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from packages.common.safe_fs import SafeFilesystemError, read_bytes_limited_no_follow

__all__ = (
    "CUTOVER_DECLARATION_ENV",
    "CUTOVER_DECLARATION_SCHEMA_VERSION",
    "CUTOVER_TRANSITION_MODES",
    "MAX_CUTOVER_DECLARATION_BYTES",
    "TransitionDecision",
    "TRANSITION_DECISION_REASONS",
    "TransitionEvaluation",
    "derive_generation",
    "evaluate_transition_decision",
    "generation_evidence",
    "load_cutover_declaration",
    "match_declaration_entry",
)


# ---------------------------------------------------------------------------
# Constants (kept in-sync with the #1080 publisher side)
# ---------------------------------------------------------------------------


#: Env var that points at the ``nhms.scheduler.registry_package_cutover.v1``
#: declaration file consumed by both publisher (refresh gate) and scheduler
#: (this module).  Introduced by #1080 — this PR MUST NOT add new env vars.
CUTOVER_DECLARATION_ENV = "NHMS_REGISTRY_CUTOVER_DECLARATION_PATH"

CUTOVER_DECLARATION_SCHEMA_VERSION = "nhms.scheduler.registry_package_cutover.v1"

#: Bounded reader guard: mirrors the publisher constant so a corrupt / huge
#: declaration file cannot exhaust scheduler memory before validation.
MAX_CUTOVER_DECLARATION_BYTES = 256 * 1024

CUTOVER_TRANSITION_MODES = frozenset({"replace"})

#: Allowed cycle hours for a declared cutover ``effective_cycle_utc``.  D8.5
#: pins 00/12 for the current GFS/IFS cadence; the same rule extrapolates to a
#: 6h cadence source when one is introduced.  We accept 00 / 06 / 12 / 18 here
#: so a future 6h source does not require a spec update in this module.
_ALLOWED_EFFECTIVE_CYCLE_HOURS = frozenset({0, 6, 12, 18})

_PACKAGE_CHECKSUM_HEX_RE = None  # accept any string; the derivation is total


# ---------------------------------------------------------------------------
# Transition-decision enum + 1:1 typed reason mapping (D8.8)
# ---------------------------------------------------------------------------


class TransitionDecision:
    """Closed set of ``transition_decision`` enum values (D8.8).

    Represented as string constants (not ``enum.Enum``) so evidence
    serializes as JSON strings without special handling and so operators can
    compare against the value literals shown in runbooks.
    """

    WARM_CONTINUE = "warm_continue"
    COLD_NEW_MODEL = "cold_new_model"
    COLD_DECLARED_CUTOVER = "cold_declared_cutover"
    BLOCK_PREDECESSOR_PENDING = "block_predecessor_pending"
    BLOCK_DECLARATION_MISSING = "block_declaration_missing"
    BLOCK_DECLARATION_STALE = "block_declaration_stale"
    BLOCK_COLD_START_OUT_OF_WINDOW = "block_cold_start_out_of_window"
    BLOCK_WRONG_GENERATION = "block_wrong_generation"

    ADMIT = frozenset({WARM_CONTINUE, COLD_NEW_MODEL, COLD_DECLARED_CUTOVER})
    BLOCK = frozenset(
        {
            BLOCK_PREDECESSOR_PENDING,
            BLOCK_DECLARATION_MISSING,
            BLOCK_DECLARATION_STALE,
            BLOCK_COLD_START_OUT_OF_WINDOW,
            BLOCK_WRONG_GENERATION,
        }
    )
    ALL = ADMIT | BLOCK


#: Fixed 1:1 mapping — extending this dict is an OpenSpec change (D8.8).
TRANSITION_DECISION_REASONS: Mapping[str, str] = {
    TransitionDecision.BLOCK_PREDECESSOR_PENDING: (
        "state_snapshot_index_prior_checkpoint_missing_after_history"
    ),
    TransitionDecision.BLOCK_DECLARATION_MISSING: (
        "registry_cutover_declaration_missing"
    ),
    TransitionDecision.BLOCK_DECLARATION_STALE: (
        "registry_cutover_declaration_stale"
    ),
    TransitionDecision.BLOCK_COLD_START_OUT_OF_WINDOW: (
        "registry_cutover_cold_start_out_of_window"
    ),
    TransitionDecision.BLOCK_WRONG_GENERATION: (
        "state_snapshot_index_generation_mismatch"
    ),
}


@dataclass(frozen=True)
class TransitionEvaluation:
    """Result of the generation-aware transition decision.

    Every field lands in candidate evidence via ``generation_evidence``.

    Attributes
    ----------
    decision:
        One of :class:`TransitionDecision` string constants.
    generation:
        Short form (``manifest-<12hex>``) derived from
        ``current_package_checksum``.
    package_checksum:
        Full ``package_checksum`` for the current candidate; kept alongside
        the short form so audits can rebuild the derivation.
    typed_reason:
        The single typed-reason string mapped from ``decision`` (``None`` when
        ``decision`` is an admit).
    selected_predecessor:
        Identity of the predecessor cycle the decision refers to (or ``None``
        when N/A — e.g. ``cold_new_model``, ``cold_declared_cutover``).
    cold_start_reason:
        Short reason string used only for the admit-side cold decisions
        (``no_prior_history`` for ``cold_new_model`` /
        ``declared_cutover_at_effective_cycle`` for
        ``cold_declared_cutover``); ``None`` on all other decisions.
    declaration_evidence:
        Bounded slice of the bound declaration entry (or the loader error) —
        never inlined raw file contents.
    """

    decision: str
    generation: str
    package_checksum: str
    typed_reason: str | None = None
    selected_predecessor: dict[str, Any] | None = None
    cold_start_reason: str | None = None
    declaration_evidence: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Generation-token derivation (D8.2)
# ---------------------------------------------------------------------------


def derive_generation(package_checksum: str | None) -> str:
    """Derive the short-form ``manifest-<12hex>`` generation token.

    D8.2 mandates a deterministic function of ``package_checksum``.  We hash
    the checksum bytes with SHA-256 and take the first 12 hex characters —
    keeping the ``manifest-<12hex>`` shape from #1080's
    ``_prospective_registry_generation`` while remaining well-defined for a
    single ``package_checksum`` input (the publisher input is a set of models;
    this input is one model's canonical checksum).

    An empty / ``None`` input yields ``manifest-empty`` so the caller can
    surface a stable evidence value rather than an implicit error — the
    downstream binding step catches the missing-checksum case with a typed
    block reason before submission.
    """
    value = str(package_checksum or "").strip()
    if not value:
        return "manifest-empty"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"manifest-{digest[:12]}"


# ---------------------------------------------------------------------------
# Declaration file loader
# ---------------------------------------------------------------------------


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_effective_cycle(raw: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    parsed = parsed.astimezone(UTC)
    if parsed.minute or parsed.second or parsed.microsecond:
        return None
    if parsed.hour not in _ALLOWED_EFFECTIVE_CYCLE_HOURS:
        return None
    return parsed


def _valid_hex64(value: Any) -> bool:
    text = str(value or "")
    if len(text) != 64:
        return False
    try:
        int(text, 16)
    except ValueError:
        return False
    return True


def load_cutover_declaration(
    env_path: str | None,
    *,
    now: datetime | None = None,
    max_bytes: int = MAX_CUTOVER_DECLARATION_BYTES,
) -> dict[str, Any] | None:
    """Load and structurally validate the declaration file.

    Returns:
        ``None`` when the env var is empty (no declaration configured — the
        scheduler treats every candidate as ``no_declaration`` for binding
        purposes; declared-cutover candidates then block with
        ``block_declaration_missing``).

        A dict containing at least ``generation`` (str) and ``entries`` (list
        of entries with ``model_id``, ``old_checksum``, ``new_checksum``,
        ``effective_cycle_utc`` (parsed datetime), ``transition_mode``) when
        the file is present, readable, and passes the structural checks
        mirrored from the publisher-side ``_load_cutover_declaration``.

    Raises:
        This function NEVER raises; a malformed / stale declaration returns
        ``None`` with the failure recorded in the returned envelope's
        ``_load_error`` field so the scheduler can emit a
        ``block_declaration_stale`` on candidates that need it.
    """
    if not env_path:
        return None
    reference_now = (now or datetime.now(UTC)).astimezone(UTC)
    path = Path(env_path).expanduser()
    if not path.is_absolute():
        return {"_load_error": "declaration_path_not_absolute"}
    try:
        stat_result = path.lstat()
    except OSError:
        return {"_load_error": "declaration_file_missing"}
    if not stat.S_ISREG(stat_result.st_mode):
        return {"_load_error": "declaration_not_regular_file"}
    if not os.access(str(path), os.R_OK):
        return {"_load_error": "declaration_not_readable"}
    try:
        content = read_bytes_limited_no_follow(
            path,
            max_bytes=max_bytes,
            containment_root=path.parent,
        )
    except (OSError, SafeFilesystemError):
        return {"_load_error": "declaration_read_failed"}
    if len(content) > max_bytes:
        return {"_load_error": "declaration_oversize"}
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"_load_error": "declaration_malformed_json"}
    if not isinstance(payload, Mapping):
        return {"_load_error": "declaration_not_object"}
    if payload.get("schema_version") != CUTOVER_DECLARATION_SCHEMA_VERSION:
        return {"_load_error": "declaration_wrong_schema"}
    if not isinstance(payload.get("generation"), str) or not payload["generation"].strip():
        return {"_load_error": "declaration_generation_missing"}
    entries_raw = payload.get("entries")
    if not isinstance(entries_raw, Sequence) or isinstance(entries_raw, str | bytes | bytearray):
        return {"_load_error": "declaration_entries_invalid"}
    if not entries_raw:
        return {"_load_error": "declaration_entries_empty"}
    normalized_entries: list[dict[str, Any]] = []
    seen_model_ids: set[str] = set()
    for index, entry in enumerate(entries_raw):
        if not isinstance(entry, Mapping):
            return {"_load_error": "declaration_entry_not_object", "_load_error_index": index}
        model_id = str(entry.get("model_id") or "").strip()
        if not model_id or model_id in seen_model_ids:
            return {"_load_error": "declaration_entry_model_id_invalid"}
        seen_model_ids.add(model_id)
        old_checksum = str(entry.get("old_checksum") or "").strip()
        new_checksum = str(entry.get("new_checksum") or "").strip()
        if not _valid_hex64(old_checksum) or not _valid_hex64(new_checksum):
            return {"_load_error": "declaration_entry_checksum_invalid"}
        effective_cycle = _parse_effective_cycle(entry.get("effective_cycle_utc"))
        if effective_cycle is None:
            return {"_load_error": "declaration_entry_effective_cycle_invalid"}
        transition_mode = str(entry.get("transition_mode") or "").strip()
        if transition_mode not in CUTOVER_TRANSITION_MODES:
            return {"_load_error": "declaration_entry_transition_mode_invalid"}
        normalized_entries.append(
            {
                "model_id": model_id,
                "old_checksum": old_checksum,
                "new_checksum": new_checksum,
                "effective_cycle_utc": effective_cycle,
                "transition_mode": transition_mode,
            }
        )
    return {
        "schema_version": CUTOVER_DECLARATION_SCHEMA_VERSION,
        "generation": str(payload["generation"]).strip(),
        "generated_at": str(payload.get("generated_at") or ""),
        "entries": normalized_entries,
        "_reference_now": _iso_utc(reference_now),
    }


def match_declaration_entry(
    declaration: Mapping[str, Any] | None,
    *,
    model_id: str,
) -> dict[str, Any] | None:
    """Return the declaration entry for ``model_id`` (or ``None``).

    Never raises.  A declaration with ``_load_error`` is treated as
    entry-absent — the caller decides between ``block_declaration_missing``
    (no declaration) and ``block_declaration_stale`` (present but invalid).
    """
    if not declaration or declaration.get("_load_error"):
        return None
    for entry in declaration.get("entries") or []:
        if str(entry.get("model_id") or "") == str(model_id):
            return dict(entry)
    return None


# ---------------------------------------------------------------------------
# Transition-decision evaluation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _HistorySignal:
    """Bounded state-index history summary consumed by the decision matrix."""

    exists_current_generation: bool
    exists_any_generation: bool
    latest_current_generation_checkpoint: dict[str, Any] | None = None
    latest_any_generation_checkpoint: dict[str, Any] | None = None


def _predecessor_identity(
    *, source_id: str, valid_time: datetime, lead_hours: int, generation: str
) -> dict[str, Any]:
    predecessor_time = valid_time.astimezone(UTC) - timedelta(hours=int(lead_hours))
    return {
        "source_id": source_id,
        "valid_time": _iso_utc(predecessor_time),
        "lead_hours": int(lead_hours),
        "generation": generation,
    }


def _declaration_load_evidence(declaration: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a bounded, redacted summary suitable for candidate evidence."""
    if declaration is None:
        return {"present": False}
    if declaration.get("_load_error"):
        return {
            "present": True,
            "status": "invalid",
            "load_error": str(declaration.get("_load_error")),
        }
    entries = declaration.get("entries") or []
    return {
        "present": True,
        "status": "loaded",
        "schema_version": declaration.get("schema_version"),
        "generation": declaration.get("generation"),
        "entry_count": len(entries),
        "entry_model_ids": [str(entry.get("model_id") or "") for entry in entries[:64]],
    }


def _bound_entry_evidence(entry: Mapping[str, Any] | None) -> dict[str, Any]:
    """Redacted bound-entry evidence: full checksums are audit-only elsewhere."""
    if entry is None:
        return {}
    effective = entry.get("effective_cycle_utc")
    if isinstance(effective, datetime):
        effective_repr = _iso_utc(effective)
    else:
        effective_repr = str(effective or "")
    return {
        "model_id": str(entry.get("model_id") or ""),
        "effective_cycle_utc": effective_repr,
        "transition_mode": str(entry.get("transition_mode") or ""),
        "old_checksum_prefix": str(entry.get("old_checksum") or "")[:12],
        "new_checksum_prefix": str(entry.get("new_checksum") or "")[:12],
    }


def evaluate_transition_decision(
    *,
    model_id: str,
    package_checksum: str | None,
    source_id: str,
    candidate_cycle_time_utc: datetime,
    required_lead_hours: int,
    history: _HistorySignal,
    declaration: Mapping[str, Any] | None,
) -> TransitionEvaluation:
    """Return the ``TransitionEvaluation`` for one candidate.

    The decision follows D8.1–D8.8 in this order:

    1. If the candidate's package_checksum is missing → block_declaration_stale
       (the registry state cannot be trusted).
    2. Look for a declaration entry for this ``model_id``.
    3. Compute the generation token from ``package_checksum``.
    4. Emit the decision along the matrix documented in :class:`TransitionEvaluation`.
    """
    candidate_generation = derive_generation(package_checksum)
    checksum_text = str(package_checksum or "").strip()
    declaration_evidence = _declaration_load_evidence(declaration)

    # (a) Missing / invalid current registry checksum WITH a declaration —
    # we cannot verify the declaration binds to this candidate → stale.
    # Missing checksum WITHOUT a declaration means we operate on a legacy
    # model row that predates registry checksums; defer to the caller so
    # the pre-§8 warm-start path handles it without regression.
    if not checksum_text:
        if declaration is not None:
            return TransitionEvaluation(
                decision=TransitionDecision.BLOCK_DECLARATION_STALE,
                generation=candidate_generation,
                package_checksum="",
                typed_reason=TRANSITION_DECISION_REASONS[
                    TransitionDecision.BLOCK_DECLARATION_STALE
                ],
                selected_predecessor=None,
                cold_start_reason=None,
                declaration_evidence={
                    **declaration_evidence,
                    "block_hint": "candidate_package_checksum_missing",
                },
            )
        # No checksum + no declaration: cold_new_model when no history in
        # any generation, warm_continue if same-generation history exists,
        # block_predecessor_pending otherwise.  With no checksum we cannot
        # actually match a generation, so history.exists_current_generation
        # would be False by construction — this hands off to the (d)/(e)
        # branches below with the "current" branch effectively unreachable
        # until an operator supplies a package_checksum in the registry.

    entry = match_declaration_entry(declaration, model_id=model_id)
    entry_evidence = _bound_entry_evidence(entry)

    # (b) Declaration file present but its file-level load failed — every
    # candidate blocks with ``block_declaration_stale`` until the operator
    # replaces the file.  This mirrors the fail-closed semantics from #1080
    # publisher-side and prevents an unusable declaration file from silently
    # gating in the wrong direction.
    if declaration is not None and declaration.get("_load_error"):
        return TransitionEvaluation(
            decision=TransitionDecision.BLOCK_DECLARATION_STALE,
            generation=candidate_generation,
            package_checksum=checksum_text,
            typed_reason=TRANSITION_DECISION_REASONS[
                TransitionDecision.BLOCK_DECLARATION_STALE
            ],
            selected_predecessor=None,
            cold_start_reason=None,
            declaration_evidence=declaration_evidence,
        )

    # (c) No prior history in ANY generation → cold_new_model.
    if not history.exists_any_generation:
        return TransitionEvaluation(
            decision=TransitionDecision.COLD_NEW_MODEL,
            generation=candidate_generation,
            package_checksum=checksum_text,
            typed_reason=None,
            selected_predecessor=None,
            cold_start_reason="no_prior_history",
            declaration_evidence={
                **declaration_evidence,
                "bound_entry": entry_evidence,
            },
        )

    # (d) Old-generation history exists but current-generation history does
    # not — a package cutover boundary.  Require an explicit declaration.
    if not history.exists_current_generation:
        if entry is None:
            return TransitionEvaluation(
                decision=TransitionDecision.BLOCK_DECLARATION_MISSING,
                generation=candidate_generation,
                package_checksum=checksum_text,
                typed_reason=TRANSITION_DECISION_REASONS[
                    TransitionDecision.BLOCK_DECLARATION_MISSING
                ],
                selected_predecessor=None,
                cold_start_reason=None,
                declaration_evidence=declaration_evidence,
            )
        # Declaration must bind identity (D8.2): new_checksum matches current
        # package_checksum AND declaration.generation equals the derivation of
        # entry.new_checksum.
        if str(entry["new_checksum"]) != checksum_text:
            return TransitionEvaluation(
                decision=TransitionDecision.BLOCK_DECLARATION_STALE,
                generation=candidate_generation,
                package_checksum=checksum_text,
                typed_reason=TRANSITION_DECISION_REASONS[
                    TransitionDecision.BLOCK_DECLARATION_STALE
                ],
                selected_predecessor=None,
                cold_start_reason=None,
                declaration_evidence={
                    **declaration_evidence,
                    "bound_entry": entry_evidence,
                    "stale_reason": "new_checksum_mismatch",
                },
            )
        expected_declaration_generation = derive_generation(entry["new_checksum"])
        declaration_generation = str((declaration or {}).get("generation") or "")
        if declaration_generation != expected_declaration_generation:
            return TransitionEvaluation(
                decision=TransitionDecision.BLOCK_DECLARATION_STALE,
                generation=candidate_generation,
                package_checksum=checksum_text,
                typed_reason=TRANSITION_DECISION_REASONS[
                    TransitionDecision.BLOCK_DECLARATION_STALE
                ],
                selected_predecessor=None,
                cold_start_reason=None,
                declaration_evidence={
                    **declaration_evidence,
                    "bound_entry": entry_evidence,
                    "stale_reason": "generation_field_mismatch",
                    "expected_generation": expected_declaration_generation,
                },
            )
        # Old-checksum must match the latest old-generation checkpoint we saw
        # (if we tracked it in history_signal).  A None old-gen sample means
        # we cannot verify; we accept and rely on new_checksum + generation.
        latest_old = history.latest_any_generation_checkpoint or {}
        old_gen_checksum = str(latest_old.get("model_package_checksum") or "")
        if old_gen_checksum and old_gen_checksum != str(entry["old_checksum"]):
            return TransitionEvaluation(
                decision=TransitionDecision.BLOCK_DECLARATION_STALE,
                generation=candidate_generation,
                package_checksum=checksum_text,
                typed_reason=TRANSITION_DECISION_REASONS[
                    TransitionDecision.BLOCK_DECLARATION_STALE
                ],
                selected_predecessor=None,
                cold_start_reason=None,
                declaration_evidence={
                    **declaration_evidence,
                    "bound_entry": entry_evidence,
                    "stale_reason": "old_checksum_mismatch",
                },
            )

        # Declaration binds.  Window logic:
        effective = entry["effective_cycle_utc"]
        assert isinstance(effective, datetime)
        candidate_time = candidate_cycle_time_utc.astimezone(UTC)
        if candidate_time < effective:
            # D8.4: earlier cycles remain OLD-generation warm-start.  But we
            # already know current-gen history does not exist — so an earlier
            # cycle here can neither warm-start (old gen) nor cold-start
            # (no declaration coverage at earlier cycle) → block.
            return TransitionEvaluation(
                decision=TransitionDecision.BLOCK_COLD_START_OUT_OF_WINDOW,
                generation=candidate_generation,
                package_checksum=checksum_text,
                typed_reason=TRANSITION_DECISION_REASONS[
                    TransitionDecision.BLOCK_COLD_START_OUT_OF_WINDOW
                ],
                selected_predecessor=None,
                cold_start_reason=None,
                declaration_evidence={
                    **declaration_evidence,
                    "bound_entry": entry_evidence,
                    "window_direction": "before_effective_cycle",
                },
            )
        if candidate_time == effective:
            return TransitionEvaluation(
                decision=TransitionDecision.COLD_DECLARED_CUTOVER,
                generation=candidate_generation,
                package_checksum=checksum_text,
                typed_reason=None,
                selected_predecessor=None,
                cold_start_reason="declared_cutover_at_effective_cycle",
                declaration_evidence={
                    **declaration_evidence,
                    "bound_entry": entry_evidence,
                },
            )
        # candidate_time > effective: require exact NEW-generation predecessor.
        selected_predecessor = _predecessor_identity(
            source_id=source_id,
            valid_time=candidate_time,
            lead_hours=required_lead_hours,
            generation=candidate_generation,
        )
        return TransitionEvaluation(
            decision=TransitionDecision.BLOCK_PREDECESSOR_PENDING,
            generation=candidate_generation,
            package_checksum=checksum_text,
            typed_reason=TRANSITION_DECISION_REASONS[
                TransitionDecision.BLOCK_PREDECESSOR_PENDING
            ],
            selected_predecessor=selected_predecessor,
            cold_start_reason=None,
            declaration_evidence={
                **declaration_evidence,
                "bound_entry": entry_evidence,
                "window_direction": "after_effective_cycle",
            },
        )

    # (e) Current-generation history exists.  Warm-continuation case:
    latest_current = history.latest_current_generation_checkpoint or {}
    if latest_current.get("has_exact_predecessor"):
        return TransitionEvaluation(
            decision=TransitionDecision.WARM_CONTINUE,
            generation=candidate_generation,
            package_checksum=checksum_text,
            typed_reason=None,
            selected_predecessor={
                "source_id": source_id,
                "valid_time": str(latest_current.get("predecessor_valid_time") or ""),
                "cycle_id": str(latest_current.get("predecessor_cycle_id") or ""),
                "lead_hours": int(latest_current.get("predecessor_lead_hours") or required_lead_hours),
                "generation": candidate_generation,
            },
            cold_start_reason=None,
            declaration_evidence={
                **declaration_evidence,
                "bound_entry": entry_evidence,
            },
        )
    # Current-generation history exists but exact predecessor missing.
    selected_predecessor = _predecessor_identity(
        source_id=source_id,
        valid_time=candidate_cycle_time_utc,
        lead_hours=required_lead_hours,
        generation=candidate_generation,
    )
    return TransitionEvaluation(
        decision=TransitionDecision.BLOCK_PREDECESSOR_PENDING,
        generation=candidate_generation,
        package_checksum=checksum_text,
        typed_reason=TRANSITION_DECISION_REASONS[
            TransitionDecision.BLOCK_PREDECESSOR_PENDING
        ],
        selected_predecessor=selected_predecessor,
        cold_start_reason=None,
        declaration_evidence={
            **declaration_evidence,
            "bound_entry": entry_evidence,
            "window_direction": "current_generation_history",
        },
    )


def generation_evidence(evaluation: TransitionEvaluation) -> dict[str, Any]:
    """Serialize a ``TransitionEvaluation`` for candidate evidence.

    Fields land under ``state_evidence.registry_cutover_transition`` on
    scheduler candidates so downstream evidence readers can decide the
    outcome without re-parsing the declaration file.
    """
    return {
        "decision": evaluation.decision,
        "generation": evaluation.generation,
        "package_checksum_prefix": evaluation.package_checksum[:12],
        "typed_reason": evaluation.typed_reason,
        "selected_predecessor": evaluation.selected_predecessor,
        "cold_start_reason": evaluation.cold_start_reason,
        "declaration": evaluation.declaration_evidence,
    }
