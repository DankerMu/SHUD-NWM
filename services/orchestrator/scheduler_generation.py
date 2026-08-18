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
    generation AND no packaged-IC qualification signal was supplied (the
    legacy carve-out; see ``packaged_initial_condition`` below).
  - ``cold_declared_cutover`` — a valid declaration admits a cold start at
    exactly ``effective_cycle_utc``.
  - ``packaged_ic_bootstrap`` — first cycle (no history in ANY generation) and
    the model package ships a qualified calibrated ``*.cfg.ic`` (#1164).

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
  - ``block_first_cycle_initial_state_undecided`` →
    ``first_cycle_initial_state_undecided`` (#1164: the model package's
    calibrated IC is unqualified or its manifest is unreadable — the first
    cycle must never silently cold-start).

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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jsonschema

from packages.common.safe_fs import SafeFilesystemError, read_bytes_limited_no_follow

# ``PACKAGED_IC_QUALITY`` lives in the shared leaf so the decision layer, the
# manifest assembler, and the SHUD runtime cannot drift apart on the token; it is
# re-exported here (see ``__all__``) because orchestrator callers reach for it
# alongside the transition-decision surface.
from packages.common.state_lineage import PACKAGED_IC_QUALITY

# ``cycle_id_for`` is deliberately taken from ``state_manager``'s module
# surface (it re-exports the adapter helper) rather than from
# ``workers.data_adapters.base``: this module must stay importable from the
# scheduler without pulling the adapter package in directly, and the expected
# token has to be composed by exactly the same functions the write side uses
# (``packages.common.state_cli`` → ``state_manager.save_state_snapshot``).
from packages.common.state_manager import cycle_id_for, state_snapshot_id

#: Sentinel that separates "declaration not yet loaded" from "loaded and
#: returned ``None``" (env unset — no declaration configured).  Lives on this
#: leaf module (no orchestrator imports) so that ``scheduler_generation_gate``
#: and ``scheduler_core`` can share one identity without a circular-import
#: window: the gate imports ``scheduler`` before its own constants are bound,
#: so any constant the cycle reads back must be defined below the cycle.
CUTOVER_DECLARATION_UNLOADED: object = object()

__all__ = (
    "CUTOVER_DECLARATION_ENV",
    "CUTOVER_DECLARATION_SCHEMA_VERSION",
    "CUTOVER_TRANSITION_MODES",
    "EMPTY_FILE_SHA256",
    "MAX_CUTOVER_DECLARATION_BYTES",
    "MAX_PACKAGED_IC_PROBE_BYTES",
    "PACKAGED_IC_BOOTSTRAP_MODE",
    "PACKAGED_IC_HEADER_SHAPE_INVALID_DETAIL",
    "PACKAGED_IC_QUALIFIED",
    "PACKAGED_IC_SOURCE_INVENTORY",
    "PACKAGED_IC_SOURCE_OBJECT_PROBE",
    "PACKAGED_IC_UNQUALIFIED",
    "PACKAGED_IC_UNREADABLE",
    "PACKAGED_IC_QUALITY",
    "PackagedIcObjectProbe",
    "PackagedIcSignal",
    "TransitionDecision",
    "TRANSITION_DECISION_REASONS",
    "TransitionEvaluation",
    "canonical_packaged_ic_object_uri",
    "classify_packaged_initial_condition",
    "derive_generation",
    "evaluate_transition_decision",
    "expected_journal_init_state_tokens",
    "generation_evidence",
    "journal_identity_quarantine_breaker_engaged",
    "journal_identity_quarantine_occurrence_count",
    "journal_init_state_lineage_matches_expected",
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

#: Mirrors the publisher constant and the schema enum.  ``"retire"`` (#1433) is
#: accepted so a retirement declaration does not invalidate the whole shared
#: file for this consumer; retirement entries are skipped during normalization
#: below because they derive no generation.
CUTOVER_TRANSITION_MODES = frozenset({"replace", "retire"})

#: Allowed cycle hours for a declared cutover ``effective_cycle_utc``.  This
#: MUST mirror the publisher constant (``CUTOVER_CYCLE_HOURS`` in
#: ``scripts/scheduler_file_provider_refresh.py``).  A future 6h source
#: requires publisher + schema + consumer + spec revision together — do NOT
#: relax unilaterally here.
_ALLOWED_EFFECTIVE_CYCLE_HOURS = frozenset({0, 12})

#: Past / future tolerance windows for ``effective_cycle_utc`` — mirrors
#: publisher ``CUTOVER_PAST_TOLERANCE`` / ``CUTOVER_FUTURE_TOLERANCE`` so a
#: declaration that survived the publisher gate cannot be re-injected outside
#: the same 24h-past / 168h-future envelope on the consumer side.
_CUTOVER_PAST_TOLERANCE = timedelta(hours=24)
_CUTOVER_FUTURE_TOLERANCE = timedelta(hours=168)

_PACKAGE_CHECKSUM_HEX_RE = None  # accept any string; the derivation is total

#: JSON Schema for the declaration payload, shared with the publisher.  We
#: build a ``Draft202012Validator`` at import time so per-request loads do not
#: pay the metaschema resolution cost that ``jsonschema.validate`` does.
_CUTOVER_DECLARATION_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "schemas"
    / "scheduler_registry_package_cutover.schema.json"
)
try:
    _CUTOVER_DECLARATION_SCHEMA = json.loads(
        _CUTOVER_DECLARATION_SCHEMA_PATH.read_text(encoding="utf-8")
    )
except (OSError, json.JSONDecodeError) as _cutover_schema_load_error:  # pragma: no cover
    raise RuntimeError(
        f"cutover declaration schema unavailable: {_cutover_schema_load_error}"
    ) from _cutover_schema_load_error
# R2-B6 (round-2 review): attach the Draft-2020-12 FormatChecker so
# ``format`` keywords are enforced at validator time rather than being
# silently symbolic.  ``date-time`` is not part of the default
# jsonschema built-ins without the ``rfc3339-validator`` extra, so we
# register a custom check via ``FormatChecker.checks`` that mirrors what
# ``_parse_effective_cycle`` accepts.  The publisher-side validator at
# ``scripts/scheduler_file_provider_refresh.py`` mirrors this instantiation.
_CUTOVER_FORMAT_CHECKER = jsonschema.FormatChecker()


@_CUTOVER_FORMAT_CHECKER.checks("date-time", raises=(TypeError, ValueError))
def _check_declaration_datetime_format(value: Any) -> bool:  # pragma: no cover - trivial
    """Return True when ``value`` parses as an aware RFC 3339 date-time."""
    if not isinstance(value, str):
        return False
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("naive datetime not permitted")
    return True


_CUTOVER_DECLARATION_VALIDATOR = jsonschema.Draft202012Validator(
    _CUTOVER_DECLARATION_SCHEMA,
    format_checker=_CUTOVER_FORMAT_CHECKER,
)


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
    #: #1164: first cycle consumes the calibrated IC shipped in the package.
    PACKAGED_IC_BOOTSTRAP = "packaged_ic_bootstrap"
    BLOCK_PREDECESSOR_PENDING = "block_predecessor_pending"
    BLOCK_DECLARATION_MISSING = "block_declaration_missing"
    BLOCK_DECLARATION_STALE = "block_declaration_stale"
    BLOCK_COLD_START_OUT_OF_WINDOW = "block_cold_start_out_of_window"
    BLOCK_WRONG_GENERATION = "block_wrong_generation"
    #: #1164: first cycle whose packaged IC is unqualified / unreadable.
    BLOCK_FIRST_CYCLE_INITIAL_STATE_UNDECIDED = "block_first_cycle_initial_state_undecided"

    ADMIT = frozenset(
        {WARM_CONTINUE, COLD_NEW_MODEL, COLD_DECLARED_CUTOVER, PACKAGED_IC_BOOTSTRAP}
    )
    BLOCK = frozenset(
        {
            BLOCK_PREDECESSOR_PENDING,
            BLOCK_DECLARATION_MISSING,
            BLOCK_DECLARATION_STALE,
            BLOCK_COLD_START_OUT_OF_WINDOW,
            BLOCK_WRONG_GENERATION,
            BLOCK_FIRST_CYCLE_INITIAL_STATE_UNDECIDED,
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
    TransitionDecision.BLOCK_FIRST_CYCLE_INITIAL_STATE_UNDECIDED: (
        "first_cycle_initial_state_undecided"
    ),
}


# ---------------------------------------------------------------------------
# #1164: packaged calibrated-IC qualification signal
# ---------------------------------------------------------------------------


#: SHA-256 of a zero-byte file.  A package ``included_files`` entry carrying
#: this digest is a placeholder, never a calibrated initial condition.
EMPTY_FILE_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

#: Candidate ``state_evidence.mode`` emitted by the gate for an admitted
#: packaged-IC bootstrap.  Declared on this leaf module (rather than on the
#: gate) so the cohort carrier in ``chain_forecast_cycle`` can share the single
#: source of truth without importing the scheduler package.
PACKAGED_IC_BOOTSTRAP_MODE = "db_free_packaged_ic_bootstrap"

PACKAGED_IC_QUALIFIED = "qualified"
PACKAGED_IC_UNQUALIFIED = "unqualified"
PACKAGED_IC_UNREADABLE = "unreadable"

#: Qualification sources (D1 two-tier).  ``inventory`` means the verdict came
#: from the package manifest's ``included_files`` entry (basins package shape,
#: zero object IO); ``object_probe`` means the manifest carried no inventory
#: (direct-grid variant shape, which publishes only ``direct_grid_forcing``) and
#: the verdict came from a bounded no-follow stat+digest read of the single
#: canonical IC object derived from the registry row.
PACKAGED_IC_SOURCE_INVENTORY = "inventory"
PACKAGED_IC_SOURCE_OBJECT_PROBE = "object_probe"

#: Content verdict for a packaged IC whose header line was READ and found not to
#: carry the native 3-token / compatibility 4-token layout (issue #1197's
#: ``23106\t6``).  Deliberately in the UNQUALIFIED domain, never UNREADABLE: the
#: object was readable, the package is simply not usable.
PACKAGED_IC_HEADER_SHAPE_INVALID_DETAIL = "packaged_initial_condition_header_shape_invalid"

#: Hard cap for the tier-(b) canonical-IC object probe.  The probe hashes the
#: object, so the cap bounds how much a corrupt / oversized package object can
#: pull into scheduler memory during planning.  Production ICs run 128 KiB –
#: ~4.4 MB (largest observed 2026-07), so 16 MiB leaves ~3.6x headroom while
#: staying at the same order as ``MAX_MODEL_PACKAGE_MANIFEST_BYTES``.  An object
#: larger than this reads as UNREADABLE (fail closed), never as "no IC".
MAX_PACKAGED_IC_PROBE_BYTES = 16 * 1024 * 1024

#: Suffix of the SHUD initial-condition file inside a Basins model package.
_PACKAGED_IC_SUFFIX = ".cfg.ic"


@dataclass(frozen=True)
class PackagedIcSignal:
    """Machine-decidable verdict on a model package's calibrated IC (D1).

    The signal is computed by the gate (all IO lives there) and injected into
    the otherwise-pure :func:`evaluate_transition_decision`.  ``None`` — as
    opposed to any instance of this class — means "no published package
    manifest reference", which is the legacy carve-out: the decision then stays
    byte-identical to the pre-#1164 ``cold_new_model`` behavior.

    Attributes
    ----------
    status:
        One of :data:`PACKAGED_IC_QUALIFIED` / :data:`PACKAGED_IC_UNQUALIFIED`
        / :data:`PACKAGED_IC_UNREADABLE`.  Anything that is not exactly
        ``qualified`` fails closed at the decision layer.
    ic_sha256:
        The digest of the packaged ``*.cfg.ic`` (only set when ``status`` is
        qualified) — manifest-recorded on the inventory tier, freshly probed on
        the object-probe tier.  This is the value that threads through candidate
        evidence → basin marker → run manifest → runtime verification.
    qualification_source:
        :data:`PACKAGED_IC_SOURCE_INVENTORY` or
        :data:`PACKAGED_IC_SOURCE_OBJECT_PROBE` — which of the two D1 tiers
        produced this verdict.  Empty when no tier ran (e.g. the manifest itself
        was unreadable), which is why it is recorded in evidence: an auditor must
        be able to tell a manifest-recorded digest from a probed one.
    """

    status: str
    ic_sha256: str = ""
    ic_relative_path: str = ""
    ic_size_bytes: int = 0
    detail: str = ""
    qualification_source: str = ""

    @property
    def qualified(self) -> bool:
        return self.status == PACKAGED_IC_QUALIFIED

    def evidence(self) -> dict[str, Any]:
        """Return a bounded evidence view (no manifest contents are inlined)."""
        payload: dict[str, Any] = {"status": self.status}
        if self.ic_relative_path:
            payload["ic_relative_path"] = self.ic_relative_path
        if self.ic_sha256:
            payload["ic_sha256"] = self.ic_sha256
        if self.ic_size_bytes:
            payload["ic_size_bytes"] = self.ic_size_bytes
        if self.detail:
            payload["detail"] = self.detail
        if self.qualification_source:
            payload["qualification_source"] = self.qualification_source
        return payload


@dataclass(frozen=True)
class PackagedIcObjectProbe:
    """Outcome of the bounded tier-(b) canonical-IC object probe (D1).

    The probe itself is IO and therefore lives in the caller (the gate, or the
    audit tool); this dataclass is the pure boundary between the two.  A probe
    that could not complete sets ``unreadable_detail`` — a probe that completed
    and found nothing sets ``exists=False`` with an empty ``unreadable_detail``.
    Conflating the two is exactly the fail-open mistake #1164 exists to prevent.
    """

    exists: bool
    size_bytes: int = 0
    sha256: str = ""
    unreadable_detail: str = ""
    header_shape_invalid_reason: str = ""
    """Set when the probe READ the object and its header line is malformed.

    Filled by each probe implementation with
    :func:`packages.common.state_qc.cfg_ic_header_shape` -- one rule, two probes
    -- and consumed by :func:`_classify_packaged_ic_by_object_probe`, which turns
    it into the ``packaged_initial_condition_header_shape_invalid`` content
    verdict.  It is emphatically NOT ``unreadable_detail``: a header the probe
    could read and found malformed is a disqualified package (issue #1197's
    ``23106\\t6`` delivery), while an object the probe could not read stays
    undetermined.  Conflating the two would lose exactly the distinction #1164
    and this gate both exist to keep.
    """


def classify_packaged_initial_condition(
    package_manifest: Any,
    *,
    resource_profile: Mapping[str, Any] | None = None,
    canonical_object_probe: Callable[[str], PackagedIcObjectProbe] | None = None,
) -> PackagedIcSignal:
    """Classify a model package's calibrated-IC qualification (D1, two-tier).

    TOTAL and side-effect free apart from ``canonical_object_probe``: the caller
    has already performed (or injected) the IO, so every malformed shape maps to
    a signal rather than an exception.

    Tier (a) — inventory.  When the manifest carries an ``included_files``
    inventory (the Basins package shape written by
    ``workers/model_registry/basins_package.py``) the verdict reads ONLY fields
    the publisher already writes: an entry whose ``relative_path`` ends with
    ``.cfg.ic``, whose ``sha256`` differs from :data:`EMPTY_FILE_SHA256`, and
    whose ``size_bytes`` is positive.  Zero object IO.

    Tier (b) — canonical object probe.  Production registry rows currently point
    at direct-grid VARIANT manifests whose only top-level key is
    ``direct_grid_forcing`` — readable, but with no inventory to consult.  Such a
    manifest is not "a package without an IC": the registry row's
    ``shud_input_name`` + ``model_package_uri`` locate exactly one canonical IC
    object, and ``canonical_object_probe`` (supplied by the gate / audit tool)
    decides on a bounded no-follow stat+digest read of that single object.
    Without a probe (or without the registry fields it needs) the tier cannot
    run and the package is UNQUALIFIED with a distinct reason — never silently
    qualified.

    A payload that is not a manifest object at all is
    :data:`PACKAGED_IC_UNREADABLE` — never "no IC" — so an unreadable manifest
    can never be mistaken for a package that ships no calibrated state.

    Qualification is decided on the CANONICAL entry only, symmetrically with the
    runtime's exactly-one check: the canonical entry is
    ``<shud_input_name>.cfg.ic`` when the manifest names the SHUD input directory
    (``basins_package.py`` publishes ``shud_input_name`` next to
    ``included_files``), otherwise any top-level ``*.cfg.ic``.  Entries under a
    subdirectory (``CALIB/…``, which sorts BEFORE the canonical entry) are never
    the qualification subject: a stray calibration IC must not lend its digest to
    the run manifest.  When the inventory lists more than one ``*.cfg.ic``
    anywhere the package is AMBIGUOUS and is blocked here — deliberately
    fail-closed, even though the runtime's non-empty filter would skip a 0-byte
    ``CALIB`` placeholder: the gate cannot tell a placeholder from a second real
    IC without opening objects, and blocking a package the runtime's recursive
    exactly-one search would also refuse keeps the two layers symmetric.  Tier
    (b) has no inventory to enumerate, so its ambiguity backstop is that same
    runtime exactly-one check (recorded as a limit in the design).
    """
    if not isinstance(package_manifest, Mapping):
        return PackagedIcSignal(
            status=PACKAGED_IC_UNREADABLE,
            detail="package_manifest_not_object",
        )
    included_files = package_manifest.get("included_files")
    if not isinstance(included_files, Sequence) or isinstance(included_files, str | bytes):
        # Inventory-less (direct-grid variant) shape → tier (b).
        return _classify_packaged_ic_by_object_probe(
            resource_profile=resource_profile,
            canonical_object_probe=canonical_object_probe,
        )
    ic_entries = [
        entry
        for entry in included_files
        if isinstance(entry, Mapping) and str(entry.get("relative_path") or "").endswith(_PACKAGED_IC_SUFFIX)
    ]
    if not ic_entries:
        return PackagedIcSignal(
            status=PACKAGED_IC_UNQUALIFIED,
            detail="packaged_initial_condition_entry_absent",
            qualification_source=PACKAGED_IC_SOURCE_INVENTORY,
        )
    if len(ic_entries) > 1:
        return PackagedIcSignal(
            status=PACKAGED_IC_UNQUALIFIED,
            detail="packaged_initial_condition_ambiguous",
            qualification_source=PACKAGED_IC_SOURCE_INVENTORY,
        )
    entry = ic_entries[0]
    relative_path = str(entry.get("relative_path") or "")
    if not _is_canonical_packaged_ic_path(relative_path, package_manifest.get("shud_input_name")):
        return PackagedIcSignal(
            status=PACKAGED_IC_UNQUALIFIED,
            ic_relative_path=relative_path,
            detail="packaged_initial_condition_not_canonical",
            qualification_source=PACKAGED_IC_SOURCE_INVENTORY,
        )
    sha256 = str(entry.get("sha256") or "").strip().lower()
    try:
        size_bytes = int(entry.get("size_bytes") or 0)
    except (TypeError, ValueError):
        size_bytes = 0
    if not sha256 or sha256 == EMPTY_FILE_SHA256 or size_bytes <= 0:
        return PackagedIcSignal(
            status=PACKAGED_IC_UNQUALIFIED,
            ic_relative_path=relative_path,
            ic_size_bytes=max(size_bytes, 0),
            detail="packaged_initial_condition_empty",
            qualification_source=PACKAGED_IC_SOURCE_INVENTORY,
        )
    return PackagedIcSignal(
        status=PACKAGED_IC_QUALIFIED,
        ic_sha256=sha256,
        ic_relative_path=relative_path,
        ic_size_bytes=size_bytes,
        qualification_source=PACKAGED_IC_SOURCE_INVENTORY,
    )


def canonical_packaged_ic_object_uri(
    *,
    model_package_uri: Any,
    shud_input_name: Any,
) -> str | None:
    """Return the canonical packaged-IC object uri for a registry row, or ``None``.

    ``model_package_uri`` is a DIRECTORY reference: both
    ``basins_package._directory_uri`` and
    ``scripts/provision_direct_grid_scheduler_registry`` publish it with a
    trailing ``/``.  The trailing separator is normalized here rather than
    trusted, so a row that lost it still resolves to the same object instead of
    to a sibling key.  ``None`` means the row cannot locate an IC at all (a
    missing / blank field, or a ``shud_input_name`` that is not a single safe
    path segment).
    """
    package_uri = str(model_package_uri or "").strip()
    input_name = str(shud_input_name or "").strip()
    if not package_uri or not input_name:
        return None
    if "/" in input_name or input_name in (".", ".."):
        return None
    return f"{package_uri.rstrip('/')}/{input_name}{_PACKAGED_IC_SUFFIX}"


def _classify_packaged_ic_by_object_probe(
    *,
    resource_profile: Mapping[str, Any] | None,
    canonical_object_probe: Callable[[str], PackagedIcObjectProbe] | None,
) -> PackagedIcSignal:
    """Tier (b): decide an inventory-less manifest on the canonical IC object.

    Every outcome is explicit.  "The caller supplied no probe" and "the registry
    row names no SHUD input directory" are UNQUALIFIED with their own reasons
    (the package cannot be shown to ship a usable IC); only a probe that FAILED
    is UNREADABLE.
    """
    if canonical_object_probe is None:
        return PackagedIcSignal(
            status=PACKAGED_IC_UNQUALIFIED,
            detail="package_manifest_included_files_absent",
        )
    profile = resource_profile if isinstance(resource_profile, Mapping) else {}
    object_uri = canonical_packaged_ic_object_uri(
        model_package_uri=profile.get("model_package_uri"),
        shud_input_name=profile.get("shud_input_name"),
    )
    if object_uri is None:
        return PackagedIcSignal(
            status=PACKAGED_IC_UNQUALIFIED,
            detail="packaged_initial_condition_registry_fields_absent",
            qualification_source=PACKAGED_IC_SOURCE_OBJECT_PROBE,
        )
    relative_path = object_uri.rsplit("/", 1)[-1]
    probe = canonical_object_probe(object_uri)
    if probe.unreadable_detail:
        return PackagedIcSignal(
            status=PACKAGED_IC_UNREADABLE,
            ic_relative_path=relative_path,
            detail=probe.unreadable_detail,
            qualification_source=PACKAGED_IC_SOURCE_OBJECT_PROBE,
        )
    if not probe.exists:
        return PackagedIcSignal(
            status=PACKAGED_IC_UNQUALIFIED,
            ic_relative_path=relative_path,
            detail="packaged_initial_condition_object_missing",
            qualification_source=PACKAGED_IC_SOURCE_OBJECT_PROBE,
        )
    sha256 = str(probe.sha256 or "").strip().lower()
    size_bytes = max(int(probe.size_bytes or 0), 0)
    if size_bytes <= 0 or not sha256 or sha256 == EMPTY_FILE_SHA256:
        return PackagedIcSignal(
            status=PACKAGED_IC_UNQUALIFIED,
            ic_relative_path=relative_path,
            ic_size_bytes=size_bytes,
            detail="packaged_initial_condition_object_empty",
            qualification_source=PACKAGED_IC_SOURCE_OBJECT_PROBE,
        )
    if probe.header_shape_invalid_reason:
        # Content verdict, NOT a probe failure: the object was read and its header
        # line does not carry a usable minute-time slot.  Disqualifying here is what
        # moves issue #1197's malformed delivery from "detonates in the first real
        # SHUD run" to "never qualifies".
        return PackagedIcSignal(
            status=PACKAGED_IC_UNQUALIFIED,
            ic_sha256=sha256,
            ic_relative_path=relative_path,
            ic_size_bytes=size_bytes,
            detail=PACKAGED_IC_HEADER_SHAPE_INVALID_DETAIL,
            qualification_source=PACKAGED_IC_SOURCE_OBJECT_PROBE,
        )
    return PackagedIcSignal(
        status=PACKAGED_IC_QUALIFIED,
        ic_sha256=sha256,
        ic_relative_path=relative_path,
        ic_size_bytes=size_bytes,
        qualification_source=PACKAGED_IC_SOURCE_OBJECT_PROBE,
    )


def _is_canonical_packaged_ic_path(relative_path: str, shud_input_name: Any) -> bool:
    """Return whether ``relative_path`` is the package's canonical IC entry.

    ``role`` is deliberately NOT part of the rule: the planned and written
    manifests label the same file differently (``shud_input`` vs
    ``runtime_input``), so only the path is a stable identity.
    """
    if isinstance(shud_input_name, str) and shud_input_name.strip():
        return relative_path == f"{shud_input_name.strip()}{_PACKAGED_IC_SUFFIX}"
    return "/" not in relative_path


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
    packaged_ic_checksum:
        #1164: the manifest-recorded SHA-256 of the packaged calibrated
        ``*.cfg.ic`` when ``decision`` is ``packaged_ic_bootstrap``; ``None``
        on every other decision.  Carried end-to-end so the runtime can verify
        the staged file it is about to consume.
    """

    decision: str
    generation: str
    package_checksum: str
    typed_reason: str | None = None
    selected_predecessor: dict[str, Any] | None = None
    cold_start_reason: str | None = None
    declaration_evidence: dict[str, Any] = field(default_factory=dict)
    packaged_ic_checksum: str | None = None


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
    """Parse ``effective_cycle_utc`` into an aware UTC ``datetime``.

    Structural validation (pattern / maxLength / date-time format) is enforced
    by the module-level ``_CUTOVER_DECLARATION_VALIDATOR``; this helper is a
    permissive parser that only rejects timezone-naive values, off-minute
    boundaries, and hours outside ``_ALLOWED_EFFECTIVE_CYCLE_HOURS`` (D8.5).
    """
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

    Structural validation is delegated to the shared
    ``schemas/scheduler_registry_package_cutover.schema.json`` via
    ``jsonschema.Draft202012Validator`` so consumer and publisher cannot
    silently diverge on generation pattern, model_id pattern, checksum case,
    ``entries`` bounds, or ``effective_cycle_utc`` maxLength.  The
    consumer additionally re-enforces the publisher's cycle-hour set and
    past/future tolerance window so a declaration that skipped the publisher
    gate (e.g. hand-edited on disk) still fails closed here.

    Raises:
        This function NEVER raises; a malformed / stale declaration returns
        an envelope with a populated ``_load_error`` field so the scheduler
        can emit ``block_declaration_missing`` (file absent while configured)
        or ``block_declaration_stale`` (present but invalid) on candidates
        that need it.
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
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        # RecursionError arises on deeply-nested JSON — mirror the peer at
        # ``packages/common/state_manager.py:1720`` so this loader honors the
        # documented "NEVER raises" contract on operator-controlled files.
        return {"_load_error": "declaration_malformed_json"}
    if not isinstance(payload, Mapping):
        return {"_load_error": "declaration_not_object"}
    try:
        _CUTOVER_DECLARATION_VALIDATOR.validate(payload)
    except jsonschema.ValidationError:
        return {"_load_error": "declaration_wrong_schema"}
    # jsonschema enforces every structural bound; the semantic loops below only
    # apply publisher-side rules that are not schema-encoded: cycle-hour set,
    # past/future tolerance, and normalization to typed values.
    normalized_entries: list[dict[str, Any]] = []
    seen_model_ids: set[str] = set()
    for index, entry in enumerate(payload["entries"]):
        model_id = str(entry["model_id"]).strip()
        if model_id in seen_model_ids:
            return {
                "_load_error": "declaration_entry_model_id_invalid",
                "_load_error_index": index,
            }
        seen_model_ids.add(model_id)
        if str(entry["transition_mode"]).strip() == "retire":
            # #1433: a retirement declares that a model LEAVES the canonical
            # registry; it carries no new package, so it can never bind a
            # candidate here.  Skip it before the checksum normalization below
            # turns its ``null`` ``new_checksum`` into the string ``"None"``,
            # and after the duplicate-id check above so a duplicated id is
            # still caught.  The entry is not added to ``normalized_entries``,
            # so ``match_declaration_entry`` never returns it and the
            # ``_declaration_load_evidence`` entry count reflects the entries
            # this consumer acts on, not the file's line count.
            continue
        old_checksum = str(entry["old_checksum"]).strip()
        new_checksum = str(entry["new_checksum"]).strip()
        effective_cycle = _parse_effective_cycle(entry["effective_cycle_utc"])
        if effective_cycle is None:
            return {
                "_load_error": "declaration_entry_effective_cycle_invalid",
                "_load_error_index": index,
            }
        if (
            effective_cycle < reference_now - _CUTOVER_PAST_TOLERANCE
            or effective_cycle > reference_now + _CUTOVER_FUTURE_TOLERANCE
        ):
            return {
                "_load_error": "declaration_entry_effective_cycle_out_of_window",
                "_load_error_index": index,
            }
        transition_mode = str(entry["transition_mode"]).strip()
        if transition_mode not in CUTOVER_TRANSITION_MODES:
            return {
                "_load_error": "declaration_entry_transition_mode_invalid",
                "_load_error_index": index,
            }
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
    """Bounded state-index history summary consumed by the decision matrix.

    ``wrong_generation_predecessor_present`` is True when a state-index entry
    sits at the expected predecessor key (same model / source / valid_time /
    cycle_id / lead_hours) but its ``model_package_checksum`` differs from the
    candidate's ``package_checksum`` — the exact case §8.3 spec Scenario
    "Wrong-generation checkpoint never satisfies strict warm-start" targets.
    ``wrong_generation_predecessor_checksum`` carries the mismatching
    checksum (audit only) so evidence can surface the generation that was
    seen; the field is empty when no such entry exists.
    """

    exists_current_generation: bool
    exists_any_generation: bool
    latest_current_generation_checkpoint: dict[str, Any] | None = None
    latest_any_generation_checkpoint: dict[str, Any] | None = None
    wrong_generation_predecessor_present: bool = False
    wrong_generation_predecessor_checksum: str = ""


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
    packaged_initial_condition: PackagedIcSignal | None = None,
) -> TransitionEvaluation:
    """Return the ``TransitionEvaluation`` for one candidate.

    The decision follows D8.1–D8.8 in this order:

    1. If the candidate's package_checksum is missing → block_declaration_stale
       (the registry state cannot be trusted).
    2. Look for a declaration entry for this ``model_id``.
    3. Compute the generation token from ``package_checksum``.
    4. Emit the decision along the matrix documented in :class:`TransitionEvaluation`.

    ``packaged_initial_condition`` (#1164) is OPTIONAL and only consulted on the
    first-cycle branch (no history in ANY generation).  Its default of ``None``
    is load-bearing: every caller that cannot produce a qualification signal —
    legacy models without a published package-manifest reference, and the two
    named gate bypasses — keeps the pre-#1164 ``cold_new_model`` decision with
    zero rebaselining.  The function stays pure: all IO happens in the gate.
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

    # (b) Declaration file present but its file-level load failed — split
    # ``declaration_file_missing`` (env configured + file absent, D8.8
    # ``registry_cutover_declaration_missing``) from content-mismatch errors
    # (present + invalid, ``registry_cutover_declaration_stale``) so the
    # operator remediation surface is unambiguous.  Every other load-error
    # token keeps the STALE mapping.
    if declaration is not None and declaration.get("_load_error"):
        load_error = str(declaration.get("_load_error") or "")
        decision = (
            TransitionDecision.BLOCK_DECLARATION_MISSING
            if load_error == "declaration_file_missing"
            else TransitionDecision.BLOCK_DECLARATION_STALE
        )
        return TransitionEvaluation(
            decision=decision,
            generation=candidate_generation,
            package_checksum=checksum_text,
            typed_reason=TRANSITION_DECISION_REASONS[decision],
            selected_predecessor=None,
            cold_start_reason=None,
            declaration_evidence=declaration_evidence,
        )

    # (c) No prior history in ANY generation — the FIRST CYCLE branch.
    if not history.exists_any_generation:
        # (c1) #1164: a qualification signal was produced for this candidate,
        # i.e. the registry publishes a package-manifest reference we could
        # read.  The packaged calibrated IC then decides the first cycle:
        # qualified → consume it; anything else → fail closed.  No silent
        # unlabeled cold start is reachable from here.
        if packaged_initial_condition is not None:
            if packaged_initial_condition.qualified:
                return TransitionEvaluation(
                    decision=TransitionDecision.PACKAGED_IC_BOOTSTRAP,
                    generation=candidate_generation,
                    package_checksum=checksum_text,
                    typed_reason=None,
                    selected_predecessor=None,
                    cold_start_reason=None,
                    declaration_evidence={
                        **declaration_evidence,
                        "bound_entry": entry_evidence,
                        "packaged_initial_condition": packaged_initial_condition.evidence(),
                    },
                    packaged_ic_checksum=packaged_initial_condition.ic_sha256,
                )
            return TransitionEvaluation(
                decision=TransitionDecision.BLOCK_FIRST_CYCLE_INITIAL_STATE_UNDECIDED,
                generation=candidate_generation,
                package_checksum=checksum_text,
                typed_reason=TRANSITION_DECISION_REASONS[
                    TransitionDecision.BLOCK_FIRST_CYCLE_INITIAL_STATE_UNDECIDED
                ],
                selected_predecessor=None,
                cold_start_reason=None,
                declaration_evidence={
                    **declaration_evidence,
                    "bound_entry": entry_evidence,
                    "packaged_initial_condition": packaged_initial_condition.evidence(),
                },
            )
        # (c2) No signal at all → legacy labeled cold start (carve-out).
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
        # A wrong-generation entry sitting at the exact predecessor key must
        # NOT be admitted — emit ``block_wrong_generation`` per §8.3 spec
        # Scenario "Wrong-generation checkpoint never satisfies strict
        # warm-start".  Otherwise fall through to the pending block.
        selected_predecessor = _predecessor_identity(
            source_id=source_id,
            valid_time=candidate_time,
            lead_hours=required_lead_hours,
            generation=candidate_generation,
        )
        if history.wrong_generation_predecessor_present:
            return TransitionEvaluation(
                decision=TransitionDecision.BLOCK_WRONG_GENERATION,
                generation=candidate_generation,
                package_checksum=checksum_text,
                typed_reason=TRANSITION_DECISION_REASONS[
                    TransitionDecision.BLOCK_WRONG_GENERATION
                ],
                selected_predecessor=selected_predecessor,
                cold_start_reason=None,
                declaration_evidence={
                    **declaration_evidence,
                    "bound_entry": entry_evidence,
                    "window_direction": "after_effective_cycle",
                    "wrong_generation_predecessor_checksum_prefix": (
                        history.wrong_generation_predecessor_checksum[:12]
                    ),
                },
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
    # Same wrong-generation guard as branch (d): a state-index entry at the
    # exact predecessor key that carries the OLD checksum blocks with
    # ``block_wrong_generation`` rather than pending, so operators know the
    # index is not merely missing an entry but holding a stale-lineage one.
    selected_predecessor = _predecessor_identity(
        source_id=source_id,
        valid_time=candidate_cycle_time_utc,
        lead_hours=required_lead_hours,
        generation=candidate_generation,
    )
    if history.wrong_generation_predecessor_present:
        return TransitionEvaluation(
            decision=TransitionDecision.BLOCK_WRONG_GENERATION,
            generation=candidate_generation,
            package_checksum=checksum_text,
            typed_reason=TRANSITION_DECISION_REASONS[
                TransitionDecision.BLOCK_WRONG_GENERATION
            ],
            selected_predecessor=selected_predecessor,
            cold_start_reason=None,
            declaration_evidence={
                **declaration_evidence,
                "bound_entry": entry_evidence,
                "window_direction": "current_generation_history",
                "wrong_generation_predecessor_checksum_prefix": (
                    history.wrong_generation_predecessor_checksum[:12]
                ),
            },
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
    evidence: dict[str, Any] = {
        "decision": evaluation.decision,
        "generation": evaluation.generation,
        "package_checksum_prefix": evaluation.package_checksum[:12],
        "typed_reason": evaluation.typed_reason,
        "selected_predecessor": evaluation.selected_predecessor,
        "cold_start_reason": evaluation.cold_start_reason,
        "declaration": evaluation.declaration_evidence,
    }
    # #1164: the first-cycle verdict is hoisted out of ``declaration`` so
    # operators do not have to know it was computed alongside the cutover
    # declaration to find it.
    packaged = evaluation.declaration_evidence.get("packaged_initial_condition")
    if isinstance(packaged, Mapping):
        evidence["packaged_initial_condition"] = dict(packaged)
    return evidence


# ---------------------------------------------------------------------------
# §8.7 journal-recorded predecessor identity (Issue #1107)
# ---------------------------------------------------------------------------


#: Error classes treated as "no judgement" by the §8.7 identity surface.
#: ``ValueError``/``TypeError`` are the contracted token-construction errors;
#: ``AttributeError`` is added so a caller passing a non-datetime
#: ``candidate_valid_time`` still yields no judgement instead of breaking the
#: TOTAL guarantee inside the scheduler's candidate loop.
JOURNAL_IDENTITY_INPUT_ERRORS = (AttributeError, TypeError, ValueError)


def expected_journal_init_state_tokens(
    *,
    source_id: str,
    model_id: str,
    candidate_valid_time: datetime,
    required_lead_hours: int,
) -> tuple[str, str]:
    """Return ``(expected_base_prefix, expected_token)`` for cycle ``T``.

    Both values are composed by ``state_manager.state_snapshot_id`` so the
    base prefix can never drift from how the write side builds the full
    token: the prefix is literally the same call with no lineage inputs.
    The expected predecessor sits ``required_lead_hours`` before ``T`` while
    the state's ``valid_time`` is ``T`` itself (a checkpoint is named by the
    cycle it initialises, mirroring
    ``state_manager._expected_state_index_cycle_id`` /
    ``scheduler_generation_gate.evaluate_transition_decision``).

    Raises for unusable inputs (see ``JOURNAL_IDENTITY_INPUT_ERRORS``) —
    callers on the judgement path go through
    :func:`journal_init_state_lineage_matches_expected`, which is total.
    """
    expected_base = state_snapshot_id(model_id, candidate_valid_time, source_id=source_id)
    expected_cycle_id = cycle_id_for(
        source_id,
        candidate_valid_time - timedelta(hours=int(required_lead_hours)),
    )
    expected_token = state_snapshot_id(
        model_id,
        candidate_valid_time,
        source_id=source_id,
        cycle_id=expected_cycle_id,
        lead_hours=int(required_lead_hours),
    )
    return expected_base, expected_token


def journal_init_state_lineage_matches_expected(
    recorded_init_state_id: str | None,
    *,
    source_id: str,
    model_id: str,
    candidate_valid_time: datetime,
    required_lead_hours: int,
) -> bool | None:
    """Judge a journal-recorded ``init_state_id`` against cycle ``T``'s expected one.

    Three-valued and TOTAL (never raises).  The name states the ``True``
    polarity on purpose, so ``if not helper(...)`` cannot be written by
    accident:

    - ``True``  — the recorded id equals the expected predecessor token.
    - ``False`` — POSITIVE MISMATCH: the recorded id shares the expected
      BASE key (same source / model / ``valid_time`` = ``T``) but carries a
      DIFFERENT non-empty lineage suffix, i.e. the §8.7 "right state slot,
      wrong predecessor cycle or lead" class.  This is the only quarantine
      trigger.
    - ``None``  — NO JUDGEMENT: missing/empty id, a suffix-less legacy id
      equal to the base prefix, a different base key (notably the legal
      earlier-``valid_time`` fallback warm start selected under
      ``NHMS_REQUIRE_FORECAST_WARM_START=false``), a malformed string, or
      any token-construction error (``JOURNAL_IDENTITY_INPUT_ERRORS`` are
      caught: ``ValueError``/``TypeError`` per the §8.7 contract, plus
      ``AttributeError`` so a non-datetime argument cannot break totality).

    The narrow ``False`` criterion is deliberate: full-token inequality
    would quarantine well-formed fallback warm starts en masse.
    """
    if recorded_init_state_id is None:
        return None
    try:
        recorded = str(recorded_init_state_id).strip()
    except JOURNAL_IDENTITY_INPUT_ERRORS:
        return None
    if not recorded:
        return None
    try:
        expected_base, expected_token = expected_journal_init_state_tokens(
            source_id=source_id,
            model_id=model_id,
            candidate_valid_time=candidate_valid_time,
            required_lead_hours=required_lead_hours,
        )
    except JOURNAL_IDENTITY_INPUT_ERRORS:
        return None
    if recorded == expected_token:
        return True
    # Same base key + a non-empty, different lineage suffix.  A recorded id
    # equal to the base prefix (legacy, suffix-less) does not start with
    # ``base + "_"`` and therefore stays a no-judgement shape.
    if recorded.startswith(f"{expected_base}_"):
        return False
    return None


#: How many DISTINCT completed forecast submissions must have recorded the SAME
#: stale token before the §8.7 quarantine stops retrying it (#1157).  Two is the
#: smallest count that proves the rerun re-selected the same wrong lineage: the
#: first recording is the original defect, the second is the failed convergence.
#: Deliberately a constant, not a configuration knob (YAGNI).
_JOURNAL_IDENTITY_QUARANTINE_BREAKER_THRESHOLD = 2


def journal_identity_quarantine_occurrence_count(
    repository: Any,
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str,
    recorded_init_state_id: str,
) -> int:
    """Read how many completed submissions already recorded this stale token.

    Accessor injection follows the repository ``getattr`` convention (cf.
    ``scheduler_discovery._journal_predecessor_identity_is_stale``), so a
    repository without ``completed_pipeline_init_state_id_occurrences`` — a DB
    repository, or any test double — simply yields ``0``.

    TOTAL: every unavailable shape (no repository, no accessor, an accessor
    that raises, a non-integer answer) returns ``0``, which leaves the breaker
    disengaged and the quarantine retry in force.  That direction is the safe
    one: an undercount costs one more rerun, an overcount would fail-stop a
    cycle that was still converging.
    """
    accessor = (
        getattr(repository, "completed_pipeline_init_state_id_occurrences", None)
        if repository is not None
        else None
    )
    if not callable(accessor):
        return 0
    try:
        count = int(
            accessor(
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=model_id,
                init_state_id=recorded_init_state_id,
            )
        )
    except Exception:  # noqa: BLE001 - a foreign accessor must not break the pass
        return 0
    return count if count > 0 else 0


def journal_identity_quarantine_breaker_engaged(occurrences: Any) -> bool:
    """Whether ``occurrences`` reaches the §8.7 quarantine breaker threshold."""

    try:
        return int(occurrences) >= _JOURNAL_IDENTITY_QUARANTINE_BREAKER_THRESHOLD
    except (TypeError, ValueError):
        return False
