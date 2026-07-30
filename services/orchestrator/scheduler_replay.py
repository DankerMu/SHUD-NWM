"""Env-gated replay admission for the six-basin production replay (#1164, change 2).

The replay surface is a closed-set, window-bounded, explicitly-enabled operator
mode with exactly two effects on the scheduler:

1. a candidate-level terminal-success override (``scheduler_candidates``), and
2. a discovery admission branch for pinned cycles whose NFS raw manifest is
   gone but whose direct-grid forcing packages are still on disk
   (``scheduler_core``).

Everything here is inert unless all three environment variables are present and
well formed:

``NHMS_SCHEDULER_REPLAY_MODE``
    Boolean master switch; absent/false means no replay admission at all.
``NHMS_SCHEDULER_REPLAY_MODEL_IDS``
    Comma-separated closed set of model ids the override may touch.
``NHMS_SCHEDULER_REPLAY_WINDOW``
    ``<start10>..<end10>`` inclusive cycle range in ``%Y%m%d%H`` tokens.

Fail-closed, never silently downgraded: ``MODE`` truthy with a missing or
malformed companion raises :class:`SchedulerReplayConfigError` at configuration
time, and a companion set without ``MODE`` raises too (the same direction the
``repair_missing_forcing`` triple already takes in ``scheduler_config``).  With
none of the three set, :func:`parse_replay_admission` returns ``None`` and every
caller keeps its pre-change behaviour byte-identically.
"""

from __future__ import annotations

import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.safe_fs import (
    SafeFilesystemError,
    list_directory_no_follow_limited,
    stat_no_follow,
)
from packages.common.source_identity import normalize_source_id

REPLAY_MODE_ENV = "NHMS_SCHEDULER_REPLAY_MODE"
REPLAY_MODEL_IDS_ENV = "NHMS_SCHEDULER_REPLAY_MODEL_IDS"
REPLAY_WINDOW_ENV = "NHMS_SCHEDULER_REPLAY_WINDOW"

#: Evidence ``decision`` token of an overridden replay candidate.  It is also
#: the token appended to ``chain_forecast_orchestrator_cycle``'s
#: ``_FORCE_TERMINAL_RESUBMIT_DECISIONS`` whitelist, which matches on the
#: evidence ``decision`` string literally.
REPLAY_RESUBMIT_DECISION = "replay_resubmit"
#: Typed ``CandidateStateDecision.reason`` of an overridden replay candidate.
REPLAY_TERMINAL_OVERRIDE_REASON = "replay_terminal_override"
#: Typed discovery rejection reasons for the raw-manifest-less replay branch.
REPLAY_FORCING_EVIDENCE_MISSING_REASON = "replay_forcing_evidence_missing"
REPLAY_FORCING_EVIDENCE_UNDETERMINABLE_REASON = "replay_forcing_evidence_undeterminable"
#: ``CycleDiscovery.classifier`` of a cycle admitted on forcing evidence alone.
REPLAY_FORCING_READY_SOURCE = "replay_forcing_evidence"

WINDOW_SEPARATOR = ".."
_CYCLE_TOKEN_RE = re.compile(r"^[0-9]{10}$")
_CYCLE_TOKEN_FORMAT = "%Y%m%d%H"

#: Bound on the replay model set.  Production replays twelve dg scopes at most
#: (six basins x two sources); the bound only exists so a pasted registry dump
#: cannot turn the closed set into an open one.
MAX_REPLAY_MODEL_IDS = 64
#: Bound on the per-cycle forcing directory fan-out inspected by the readiness
#: probe.  Production carries one directory per basin version.
MAX_FORCING_PACKAGE_DIRECTORY_ENTRIES = 4096

#: Three-way probe verdicts.  ``undeterminable`` must never be folded into
#: ``missing``: an unreadable directory is not proof of absence (#1190).
PROBE_PRESENT = "present"
PROBE_MISSING = "missing"
PROBE_UNDETERMINABLE = "undeterminable"


class SchedulerReplayConfigError(ValueError):
    """Typed replay configuration error; the pass must submit nothing."""

    def __init__(self, reason: str, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.field = field

    def to_dict(self) -> dict[str, Any]:
        return {"error": "replay_config_invalid", "reason": self.reason, "field": self.field}


@dataclass(frozen=True)
class ReplayAdmission:
    """Resolved replay admission scope: a closed model set and a cycle window."""

    model_ids: frozenset[str]
    window_start: datetime
    window_end: datetime

    def includes_model(self, model_id: str | None) -> bool:
        return str(model_id or "") in self.model_ids

    def includes_cycle(self, cycle_time: datetime | None) -> bool:
        if not isinstance(cycle_time, datetime):
            return False
        moment = _ensure_utc(cycle_time)
        return self.window_start <= moment <= self.window_end

    def covers(self, *, model_id: str | None, cycle_time: datetime | None) -> bool:
        return self.includes_model(model_id) and self.includes_cycle(cycle_time)

    def to_evidence(self) -> dict[str, Any]:
        return {
            "model_ids": sorted(self.model_ids),
            "window_start": _format_cycle_token(self.window_start),
            "window_end": _format_cycle_token(self.window_end),
        }


def parse_replay_admission(
    *,
    mode: bool | str | None,
    model_ids: str | Sequence[str] | None,
    window: str | None,
) -> ReplayAdmission | None:
    """Resolve the replay triple, or ``None`` when replay is entirely absent.

    Raises :class:`SchedulerReplayConfigError` for every partial configuration:
    a truthy mode with a missing/malformed companion, and a companion supplied
    without the mode.  Neither may degrade into an ordinary production pass.
    """

    enabled = _coerce_mode(mode)
    requested_model_ids = _parse_model_ids(model_ids)
    window_text = (window or "").strip() if isinstance(window, str) else ""
    if not enabled:
        if requested_model_ids or window_text:
            raise SchedulerReplayConfigError(
                "replay_companion_without_mode",
                "production scheduler replay model ids/window require "
                f"{REPLAY_MODE_ENV} to be enabled",
                field=REPLAY_MODE_ENV,
            )
        return None
    if not requested_model_ids:
        raise SchedulerReplayConfigError(
            "replay_model_ids_missing",
            f"production scheduler replay mode requires a non-empty {REPLAY_MODEL_IDS_ENV}",
            field=REPLAY_MODEL_IDS_ENV,
        )
    if len(requested_model_ids) > MAX_REPLAY_MODEL_IDS:
        raise SchedulerReplayConfigError(
            "replay_model_ids_limit_exceeded",
            f"production scheduler replay model set exceeds {MAX_REPLAY_MODEL_IDS} entries",
            field=REPLAY_MODEL_IDS_ENV,
        )
    if not window_text:
        raise SchedulerReplayConfigError(
            "replay_window_missing",
            f"production scheduler replay mode requires {REPLAY_WINDOW_ENV}",
            field=REPLAY_WINDOW_ENV,
        )
    start, end = _parse_window(window_text)
    return ReplayAdmission(model_ids=frozenset(requested_model_ids), window_start=start, window_end=end)


def forcing_cycle_root(object_store_root: str | Path, source_id: str, cycle_time: datetime) -> Path:
    """``<root>/forcing/<source>/<cycle>`` — the per-cycle forcing package parent."""

    return (
        Path(str(object_store_root))
        / "forcing"
        / normalize_source_id(source_id).lower()
        / _format_cycle_token(cycle_time)
    )


def find_forcing_package_dir(
    object_store_root: str | Path,
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str,
) -> Path | None:
    """Locate one model's direct-grid forcing package, or ``None`` if absent.

    Shares the layout knowledge with :func:`replay_forcing_readiness`
    (``<cycle>/<basin_version_id>/<model_id>/``) so the readiness gate and the
    replay driver's staging step can never disagree about where a package lives.
    """

    cycle_root = forcing_cycle_root(object_store_root, source_id, cycle_time)
    try:
        parents = list_directory_no_follow_limited(
            cycle_root,
            max_entries=MAX_FORCING_PACKAGE_DIRECTORY_ENTRIES,
        )
    except (FileNotFoundError, OSError, SafeFilesystemError):
        return None
    for parent in sorted(parents):
        candidate = cycle_root / parent / model_id
        try:
            metadata = stat_no_follow(candidate)
        except (FileNotFoundError, OSError, SafeFilesystemError):
            continue
        if _is_directory(metadata):
            return candidate
    return None


def replay_forcing_readiness(
    *,
    object_store_root: str | Path | None,
    source_id: str,
    cycle_time: datetime,
    model_ids: Sequence[str],
) -> dict[str, Any]:
    """Bounded no-follow readiness probe of every replay model's forcing package.

    Production writes direct-grid forcing to
    ``forcing/{source}/{cycle}/{basin_version_id}/{model_id}/`` (see
    ``chain_manifest_contracts._forcing_uri``), so the probe lists the cycle
    directory once and looks for a non-empty ``<basin_version>/<model_id>``
    directory per requested model.

    Every model lands on one of three verdicts and the cycle-level status is the
    join: ``ready`` only when every model is ``present``; ``missing`` when at
    least one model is provably absent or empty; ``undeterminable`` when a probe
    could not be completed and nothing was provably absent.  An uncompletable
    probe is never reported as a completed negative.
    """

    canonical_source = normalize_source_id(source_id)
    requested = [str(model_id) for model_id in model_ids if str(model_id or "")]
    evidence: dict[str, Any] = {
        "source": REPLAY_FORCING_READY_SOURCE,
        "source_id": canonical_source,
        "cycle_time": _format_utc(cycle_time),
        "cycle_token": _format_cycle_token(cycle_time),
        "model_ids": sorted(requested),
        "models": {},
    }
    if not requested:
        evidence["status"] = PROBE_MISSING
        evidence["reason"] = REPLAY_FORCING_EVIDENCE_MISSING_REASON
        evidence["detail"] = "replay model set is empty"
        return evidence
    if object_store_root in (None, ""):
        evidence["status"] = PROBE_UNDETERMINABLE
        evidence["reason"] = REPLAY_FORCING_EVIDENCE_UNDETERMINABLE_REASON
        evidence["detail"] = "object store root is not configured"
        return evidence

    cycle_root = (
        Path(str(object_store_root))
        / "forcing"
        / canonical_source.lower()
        / _format_cycle_token(cycle_time)
    )
    evidence["cycle_root"] = str(cycle_root)
    try:
        package_parents = list_directory_no_follow_limited(
            cycle_root,
            max_entries=MAX_FORCING_PACKAGE_DIRECTORY_ENTRIES,
        )
    except FileNotFoundError:
        for model_id in requested:
            evidence["models"][model_id] = {"status": PROBE_MISSING, "detail": "cycle directory absent"}
        evidence["status"] = PROBE_MISSING
        evidence["reason"] = REPLAY_FORCING_EVIDENCE_MISSING_REASON
        return evidence
    except (OSError, SafeFilesystemError) as error:
        for model_id in requested:
            evidence["models"][model_id] = {
                "status": PROBE_UNDETERMINABLE,
                "detail": f"cycle directory unreadable: {error}",
            }
        evidence["status"] = PROBE_UNDETERMINABLE
        evidence["reason"] = REPLAY_FORCING_EVIDENCE_UNDETERMINABLE_REASON
        return evidence
    if len(package_parents) > MAX_FORCING_PACKAGE_DIRECTORY_ENTRIES:
        evidence["status"] = PROBE_UNDETERMINABLE
        evidence["reason"] = REPLAY_FORCING_EVIDENCE_UNDETERMINABLE_REASON
        evidence["detail"] = "cycle directory fan-out exceeds the probe bound"
        for model_id in requested:
            evidence["models"][model_id] = {
                "status": PROBE_UNDETERMINABLE,
                "detail": "cycle directory fan-out exceeds the probe bound",
            }
        return evidence

    for model_id in requested:
        evidence["models"][model_id] = _forcing_package_probe(
            cycle_root,
            package_parents=sorted(package_parents),
            model_id=model_id,
        )
    statuses = {str(item.get("status")) for item in evidence["models"].values()}
    if statuses == {PROBE_PRESENT}:
        evidence["status"] = "ready"
        return evidence
    if PROBE_MISSING in statuses:
        evidence["status"] = PROBE_MISSING
        evidence["reason"] = REPLAY_FORCING_EVIDENCE_MISSING_REASON
        return evidence
    evidence["status"] = PROBE_UNDETERMINABLE
    evidence["reason"] = REPLAY_FORCING_EVIDENCE_UNDETERMINABLE_REASON
    return evidence


def _forcing_package_probe(
    cycle_root: Path,
    *,
    package_parents: Sequence[str],
    model_id: str,
) -> dict[str, Any]:
    undeterminable: str | None = None
    for parent in package_parents:
        package_dir = cycle_root / parent / model_id
        try:
            metadata = stat_no_follow(package_dir)
        except FileNotFoundError:
            continue
        except (OSError, SafeFilesystemError) as error:
            undeterminable = f"package stat failed: {error}"
            continue
        if not _is_directory(metadata):
            undeterminable = "package path is not a directory"
            continue
        try:
            entries = list_directory_no_follow_limited(
                package_dir,
                max_entries=MAX_FORCING_PACKAGE_DIRECTORY_ENTRIES,
            )
        except FileNotFoundError:
            continue
        except (OSError, SafeFilesystemError) as error:
            undeterminable = f"package unreadable: {error}"
            continue
        if entries:
            return {
                "status": PROBE_PRESENT,
                "package_dir": str(package_dir),
                "entry_count": len(entries),
            }
        undeterminable = None
        return {"status": PROBE_MISSING, "package_dir": str(package_dir), "detail": "package directory is empty"}
    if undeterminable is not None:
        return {"status": PROBE_UNDETERMINABLE, "detail": undeterminable}
    return {"status": PROBE_MISSING, "detail": "no forcing package directory for this model"}


def _is_directory(metadata: Any) -> bool:
    return bool(stat.S_ISDIR(metadata.st_mode))


def _coerce_mode(value: bool | str | None) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return False
    if text in {"1", "true", "t", "yes", "y", "on", "enabled"}:
        return True
    if text in {"0", "false", "f", "no", "n", "off", "disabled"}:
        return False
    raise SchedulerReplayConfigError(
        "replay_mode_invalid",
        f"production scheduler {REPLAY_MODE_ENV} must be a boolean value",
        field=REPLAY_MODE_ENV,
    )


def _parse_model_ids(value: str | Sequence[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        tokens = [item.strip() for item in value.split(",")]
    elif isinstance(value, Mapping):  # pragma: no cover - defensive
        raise SchedulerReplayConfigError(
            "replay_model_ids_invalid",
            f"production scheduler {REPLAY_MODEL_IDS_ENV} must be a comma-separated list",
            field=REPLAY_MODEL_IDS_ENV,
        )
    else:
        tokens = [str(item).strip() for item in value]
    resolved: list[str] = []
    for token in tokens:
        if not token:
            continue
        if token in resolved:
            continue
        resolved.append(token)
    return resolved


def _parse_window(value: str) -> tuple[datetime, datetime]:
    parts = value.split(WINDOW_SEPARATOR)
    if len(parts) != 2:
        raise SchedulerReplayConfigError(
            "replay_window_invalid",
            f"production scheduler {REPLAY_WINDOW_ENV} must be '<start10>..<end10>'",
            field=REPLAY_WINDOW_ENV,
        )
    start = _parse_cycle_token(parts[0].strip())
    end = _parse_cycle_token(parts[1].strip())
    if start > end:
        raise SchedulerReplayConfigError(
            "replay_window_inverted",
            f"production scheduler {REPLAY_WINDOW_ENV} start must not be after end",
            field=REPLAY_WINDOW_ENV,
        )
    return start, end


def _parse_cycle_token(token: str) -> datetime:
    if not _CYCLE_TOKEN_RE.match(token):
        raise SchedulerReplayConfigError(
            "replay_window_invalid",
            f"production scheduler {REPLAY_WINDOW_ENV} bounds must be %Y%m%d%H tokens",
            field=REPLAY_WINDOW_ENV,
        )
    try:
        return datetime.strptime(token, _CYCLE_TOKEN_FORMAT).replace(tzinfo=UTC)
    except ValueError as error:
        raise SchedulerReplayConfigError(
            "replay_window_invalid",
            f"production scheduler {REPLAY_WINDOW_ENV} bounds must be valid UTC cycles",
            field=REPLAY_WINDOW_ENV,
        ) from error


def _ensure_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _format_cycle_token(value: datetime) -> str:
    return _ensure_utc(value).strftime(_CYCLE_TOKEN_FORMAT)


def _format_utc(value: datetime) -> str:
    return _ensure_utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")
