"""Predecessor-select candidate emission for §8.6 (Issue #1081).

When ``evaluate_transition_decision`` blocks a successor cycle T with
``block_predecessor_pending`` (typed reason
``state_snapshot_index_prior_checkpoint_missing_after_history``), §8.6
requires the scheduler to emit a NEW candidate for the predecessor cycle
BEFORE retrying T — the predecessor cycles come first in the returned
candidate list and T stays deferred (not submitted, not permanently failed)
until the predecessor lands.

This module implements that emission as a post-processing step invoked from
``services.orchestrator.scheduler_candidates.build_candidates``.  It runs
after the main construction loop so the deep candidate-factory / decision
plumbing stays untouched; it only synthesizes a CycleDiscovery for the
predecessor time and re-runs strict-warm-start gating on the synthesized
candidate so §8 semantics apply uniformly.

Constraints
-----------
- We emit predecessor candidates ONLY when the successor block carries a
  ``registry_cutover_transition`` state_evidence field pointing to
  ``block_predecessor_pending`` with a well-formed ``selected_predecessor``.
  §8.6 spec Scenario "Backfill respects generation identity" refuses
  cross-generation predecessors; that guard is enforced downstream by the
  §8 gate applied to the emitted candidate itself.
- We skip emission when a candidate for the predecessor cycle is already
  present (avoid duplicates) or when the synthesized predecessor fails its
  own §8 gate; failing predecessors emit a bounded evidence entry so the
  operator can trace WHY §8.6 could not close the gap.
- Emission is bounded — no more than ``MAX_PREDECESSOR_EMISSIONS`` per
  scheduler pass so a malformed evidence stream cannot cause runaway
  candidate construction.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from workers.data_adapters.base import CycleDiscovery

# Bounded per-pass so a malformed evidence stream cannot drive unbounded
# construction; the scheduler pass max candidate cap (10000) still applies.
MAX_PREDECESSOR_EMISSIONS = 256


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _predecessor_key(source_id: str, cycle_time: datetime, model_id: str) -> tuple[str, str, str]:
    return (str(source_id), cycle_time.astimezone(UTC).isoformat(), str(model_id))


def _extract_pending_predecessors(
    blocked: Sequence[Any],
) -> list[dict[str, Any]]:
    """Return one dict per §8.6 predecessor-pending block."""
    pending: list[dict[str, Any]] = []
    for entry in blocked:
        evidence = getattr(entry, "state_evidence", None)
        if not isinstance(evidence, Mapping):
            continue
        transition = evidence.get("registry_cutover_transition")
        if not isinstance(transition, Mapping):
            continue
        if transition.get("decision") != "block_predecessor_pending":
            continue
        selected = transition.get("selected_predecessor") or {}
        if not isinstance(selected, Mapping):
            continue
        cycle_time = _parse_iso(selected.get("valid_time"))
        source_id = str(selected.get("source_id") or "")
        model_id = str(getattr(entry, "model_id", "") or "")
        if cycle_time is None or not source_id or not model_id:
            continue
        try:
            lead_hours = int(selected.get("lead_hours") or 0)
        except (TypeError, ValueError):
            continue
        if lead_hours <= 0:
            continue
        pending.append(
            {
                "source_id": source_id,
                "cycle_time": cycle_time,
                "lead_hours": lead_hours,
                "generation": str(selected.get("generation") or ""),
                "model_id": model_id,
                "successor_candidate_id": getattr(entry, "candidate_id", ""),
            }
        )
    return pending


def _predecessor_cycle_id(source_id: str, cycle_time: datetime) -> str:
    stamp = cycle_time.astimezone(UTC).strftime("%Y%m%d%H")
    return f"{source_id}_{stamp}"


def _predecessor_raw_manifest_ready(source_id: str, cycle_time: datetime) -> bool:
    """Return True when the raw manifest for the predecessor cycle is ready.

    §8.6 spec Scenario "Predecessor selected before successor" gates emission
    on the raw manifest for the predecessor existing.  We probe via the
    ``nfs_raw_manifest_readiness_from_env`` helper — same code path the
    scheduler discovery layer uses — so a NFS-raw-manifest that has not yet
    landed on disk cannot drive predecessor emission.  Import is local to
    avoid a module-load-time cycle with ``services.orchestrator.scheduler``.
    """
    try:
        from services.orchestrator import source_cycle_raw_manifest
    except ImportError:  # pragma: no cover - defensive
        return False
    readiness = source_cycle_raw_manifest.nfs_raw_manifest_readiness_from_env(
        source_id, cycle_time
    )
    if not isinstance(readiness, Mapping):
        return False
    return str(readiness.get("status") or "") == "ready"


def emit_predecessor_candidates(
    *,
    models: Sequence[Any],
    cycles: Sequence[Any],
    candidates: list[Any],
    blocked: list[Any],
    candidate_factory: Any,
    strict_warm_start_for_candidate: Any,
    blocked_candidate_factory: Any,
) -> list[dict[str, Any]]:
    """Emit predecessor candidates for §8.6 predecessor-pending blocks.

    Returns a list of emission-evidence dicts (one per emission attempt) for
    the scheduler pass evidence.  ``candidates`` and ``blocked`` are mutated
    in place — admittable predecessor candidates are inserted BEFORE current
    candidates (§8.6 "predecessor first" ordering); predecessor candidates
    whose own §8 gate blocks are appended to ``blocked`` so the operator can
    trace WHY the gap could not close.
    """
    pending = _extract_pending_predecessors(blocked)
    if not pending:
        return []

    existing_candidate_keys: set[tuple[str, str, str]] = set()
    for existing in candidates:
        cycle_time_utc = getattr(existing, "cycle_time_utc", None)
        if not isinstance(cycle_time_utc, datetime):
            continue
        existing_candidate_keys.add(
            _predecessor_key(
                getattr(existing, "source_id", ""),
                cycle_time_utc,
                getattr(existing, "model_id", ""),
            )
        )
    existing_blocked_keys: set[tuple[str, str, str]] = set()
    for entry in blocked:
        cycle_time_utc = getattr(entry, "cycle_time_utc", None)
        if not isinstance(cycle_time_utc, datetime):
            continue
        existing_blocked_keys.add(
            _predecessor_key(
                getattr(entry, "source_id", ""),
                cycle_time_utc,
                getattr(entry, "model_id", ""),
            )
        )
    cycles_by_source_time: dict[tuple[str, str], Any] = {}
    for cycle in cycles:
        discovery = getattr(cycle, "discovery", None)
        if discovery is None:
            continue
        cycle_time_iso = (
            discovery.cycle_time.astimezone(UTC).isoformat()
            if isinstance(discovery.cycle_time, datetime)
            else ""
        )
        cycles_by_source_time[(str(discovery.source_id), cycle_time_iso)] = cycle

    models_by_id: dict[str, Any] = {
        str(getattr(model, "model_id", "") or ""): model for model in models
    }

    admitted: list[Any] = []
    emission_evidence: list[dict[str, Any]] = []
    total_attempted = 0
    truncated = False
    for record in pending:
        if total_attempted >= MAX_PREDECESSOR_EMISSIONS:
            truncated = True
            break
        total_attempted += 1
        key = _predecessor_key(
            record["source_id"], record["cycle_time"], record["model_id"]
        )
        if key in existing_candidate_keys or key in existing_blocked_keys:
            emission_evidence.append(
                {
                    "status": "skipped",
                    "reason": "predecessor_already_present",
                    "successor_candidate_id": record["successor_candidate_id"],
                    "predecessor_source_id": record["source_id"],
                    "predecessor_cycle_time": record["cycle_time"].isoformat(),
                    "predecessor_model_id": record["model_id"],
                }
            )
            continue
        model = models_by_id.get(record["model_id"])
        if model is None:
            emission_evidence.append(
                {
                    "status": "skipped",
                    "reason": "predecessor_model_not_available",
                    "successor_candidate_id": record["successor_candidate_id"],
                    "predecessor_model_id": record["model_id"],
                }
            )
            continue
        # Reuse an existing cycle discovery when the predecessor happens to
        # sit at a source cycle already discovered this pass; otherwise
        # synthesize a minimal ready discovery — BUT only after the raw
        # manifest readiness probe reports the predecessor is available
        # on-disk.  §8.6 spec Scenario "Predecessor selected before
        # successor" gates emission on the predecessor manifest existing.
        cycle_time_iso = record["cycle_time"].astimezone(UTC).isoformat()
        source_key = (record["source_id"], cycle_time_iso)
        existing_cycle = cycles_by_source_time.get(source_key)
        if existing_cycle is not None:
            predecessor_cycle = existing_cycle
            predecessor_discovery = existing_cycle.discovery
        else:
            if not _predecessor_raw_manifest_ready(
                record["source_id"], record["cycle_time"]
            ):
                emission_evidence.append(
                    {
                        "status": "skipped",
                        "reason": "predecessor_raw_manifest_not_ready",
                        "successor_candidate_id": record["successor_candidate_id"],
                        "predecessor_source_id": record["source_id"],
                        "predecessor_cycle_time": cycle_time_iso,
                        "predecessor_model_id": record["model_id"],
                    }
                )
                continue
            predecessor_discovery = CycleDiscovery(
                cycle_id=_predecessor_cycle_id(record["source_id"], record["cycle_time"]),
                source_id=record["source_id"],
                cycle_time=record["cycle_time"],
                cycle_hour=int(record["cycle_time"].astimezone(UTC).hour),
                available=True,
                status="predecessor_backfill_synth",
                reason=None,
                classifier="registry_cutover_predecessor_backfill",
                retryable=False,
                probe_uri=None,
                evidence={
                    "predecessor_backfill": True,
                    "successor_candidate_id": record["successor_candidate_id"],
                    "generation": record["generation"],
                    "lead_hours": record["lead_hours"],
                },
            )
            predecessor_cycle = None
        horizon = predecessor_cycle.horizon if predecessor_cycle is not None else {}
        try:
            predecessor_candidate = candidate_factory(
                discovery=predecessor_discovery,
                model=model,
                horizon=horizon,
            )
        except Exception as error:  # noqa: BLE001 — bounded evidence on failure
            emission_evidence.append(
                {
                    "status": "skipped",
                    "reason": "predecessor_candidate_construction_failed",
                    "successor_candidate_id": record["successor_candidate_id"],
                    "error": type(error).__name__,
                }
            )
            continue

        # §8 gate the emitted predecessor: routes it to either the admit
        # list (ready) or the blocked list (its own predecessor is missing
        # / declaration-less cutover / wrong-generation etc.).  §8.6 spec
        # Scenario "Backfill respects generation identity" — a predecessor
        # from a different generation is refused via this gate.
        synthetic_cycle = predecessor_cycle
        if synthetic_cycle is None:
            # A lightweight cycle shim exposing ``discovery`` + ``horizon``
            # so ``strict_warm_start_for_candidate`` receives the same
            # protocol it does for real cycles.  We do NOT reuse the
            # SchedulerSourceCycle dataclass here to avoid importing
            # scheduler at module load time (circular).
            class _SynthCycle:
                discovery = predecessor_discovery
                horizon: dict[str, Any] = {}

            synthetic_cycle = _SynthCycle()
        try:
            gate = strict_warm_start_for_candidate(predecessor_candidate, synthetic_cycle)
        except Exception as error:  # noqa: BLE001 — bounded evidence on failure
            emission_evidence.append(
                {
                    "status": "skipped",
                    "reason": "predecessor_gate_failed",
                    "successor_candidate_id": record["successor_candidate_id"],
                    "error": type(error).__name__,
                }
            )
            continue
        if gate is not None and not bool(gate.get("ready")):
            blocked_predecessor = blocked_candidate_factory(
                predecessor_candidate,
                str(gate.get("reason") or "predecessor_backfill_blocked"),
                state_evidence={
                    **dict(gate),
                    "predecessor_backfill_marker": {
                        "predecessor_backfill": True,
                        "successor_candidate_id": record["successor_candidate_id"],
                        "generation": record["generation"],
                        "lead_hours": record["lead_hours"],
                    },
                },
            )
            blocked.append(blocked_predecessor)
            existing_blocked_keys.add(key)
            emission_evidence.append(
                {
                    "status": "blocked",
                    "reason": str(gate.get("reason") or ""),
                    "successor_candidate_id": record["successor_candidate_id"],
                    "predecessor_candidate_id": getattr(
                        predecessor_candidate, "candidate_id", ""
                    ),
                    "predecessor_source_id": record["source_id"],
                    "predecessor_cycle_time": record["cycle_time"].isoformat(),
                    "predecessor_model_id": record["model_id"],
                    "generation": record["generation"],
                    "lead_hours": record["lead_hours"],
                }
            )
            continue

        # Attach a predecessor-backfill marker so downstream evidence
        # readers can trace this candidate back to the §8.6 emission.
        marker_evidence = {
            "predecessor_backfill": True,
            "successor_candidate_id": record["successor_candidate_id"],
            "generation": record["generation"],
            "lead_hours": record["lead_hours"],
        }
        existing_state = dict(getattr(predecessor_candidate, "state_evidence", {}) or {})
        existing_state["predecessor_backfill_marker"] = marker_evidence
        # Add the §8 gate evidence under a stable key so audits can trace
        # exactly which admit decision the predecessor got.
        if isinstance(gate, Mapping):
            existing_state.setdefault("predecessor_backfill_gate", dict(gate))
        try:
            from dataclasses import replace as _dataclass_replace

            predecessor_candidate = _dataclass_replace(
                predecessor_candidate, state_evidence=existing_state
            )
        except (TypeError, ValueError):
            try:
                predecessor_candidate.state_evidence = existing_state  # type: ignore[misc]
            except Exception:  # pragma: no cover - opportunistic
                pass
        admitted.append(predecessor_candidate)
        existing_candidate_keys.add(key)
        emission_evidence.append(
            {
                "status": "emitted",
                "successor_candidate_id": record["successor_candidate_id"],
                "predecessor_candidate_id": getattr(
                    predecessor_candidate, "candidate_id", ""
                ),
                "predecessor_source_id": record["source_id"],
                "predecessor_cycle_time": record["cycle_time"].isoformat(),
                "predecessor_model_id": record["model_id"],
                "generation": record["generation"],
                "lead_hours": record["lead_hours"],
            }
        )
    # Prepend so predecessor candidates come first per §8.6 ordering.
    if admitted:
        candidates[:0] = admitted
    if truncated:
        emission_evidence.append(
            {
                "status": "truncated",
                "reason": "predecessor_emission_cap_reached",
                "total_attempted": total_attempted,
                "cap": MAX_PREDECESSOR_EMISSIONS,
            }
        )
    return emission_evidence


__all__ = (
    "MAX_PREDECESSOR_EMISSIONS",
    "emit_predecessor_candidates",
)
