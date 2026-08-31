"""Durable bounded history and arithmetic trend decisions for cold governance."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.safe_fs import SafeFilesystemError, read_bytes_durable_no_follow, stat_no_follow

MAX_PRIOR_RECEIPT_BYTES = 512 * 1024


class GovernanceHistoryError(RuntimeError):
    """A prior public governance receipt cannot safely supply a trend baseline."""


@dataclass(frozen=True)
class TrendDecision:
    status: str
    prior: dict[str, Any] | None
    deltas: dict[str, int] | None
    blockers: tuple[str, ...]


def _parse(value: object) -> datetime:
    if not isinstance(value, str):
        raise GovernanceHistoryError("prior receipt timestamp is missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GovernanceHistoryError("prior receipt timestamp is not timezone-aware")
    return parsed.astimezone(UTC)


def _identity(path: Path, raw: bytes, info: Any) -> dict[str, Any]:
    return {
        "path": str(path),
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def read_prior_receipt(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read a mode-0600 regular receipt through a durable no-follow descriptor."""

    try:
        info = stat_no_follow(path)
        if info.st_mode & 0o777 != 0o600:
            raise GovernanceHistoryError("prior governance receipt mode is not 0600")
        raw = read_bytes_durable_no_follow(path, max_bytes=MAX_PRIOR_RECEIPT_BYTES)
        value = json.loads(raw.decode("utf-8"))
    except GovernanceHistoryError:
        raise
    except (SafeFilesystemError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GovernanceHistoryError("prior governance receipt is unavailable or malformed") from error
    if not isinstance(value, Mapping):
        raise GovernanceHistoryError("prior governance receipt is not an object")
    document = dict(value)
    if document.get("schema_version") != "1.0":
        raise GovernanceHistoryError("prior governance receipt schema differs")
    filesystems = document.get("filesystems")
    if not isinstance(filesystems, Mapping):
        raise GovernanceHistoryError("prior governance receipt filesystem observations are missing")
    for label, expected_path in (("home", "/home"), ("cold", "/data/GHDC")):
        sample = filesystems.get(label)
        if not isinstance(sample, Mapping) or sample.get("path") != expected_path:
            raise GovernanceHistoryError("prior governance receipt filesystem identity differs")
        if not isinstance(sample.get("residual_bytes"), int):
            raise GovernanceHistoryError("prior governance receipt residual is missing")
    _parse(document.get("finished_at"))
    return document, _identity(path, raw, info)


def build_trend(
    *,
    current_filesystems: Mapping[str, Mapping[str, Any]],
    current_started_at: str,
    prior_path: Path | None,
    max_age_seconds: int | None,
) -> TrendDecision:
    """Return a baseline, bounded delta, or refusal without ever scanning storage."""

    if prior_path is None:
        return TrendDecision(status="baseline", prior=None, deltas=None, blockers=())
    if max_age_seconds is None or max_age_seconds <= 0:
        return TrendDecision(
            status="invalid",
            prior=None,
            deltas=None,
            blockers=("prior receipt maximum age is unavailable",),
        )
    try:
        prior, identity = read_prior_receipt(prior_path)
        prior_finished = _parse(prior["finished_at"])
        current_started = _parse(current_started_at)
        age = (current_started - prior_finished).total_seconds()
        if age < 0:
            raise GovernanceHistoryError("prior governance receipt lies after this audit interval")
        if age > max_age_seconds:
            raise GovernanceHistoryError("prior governance receipt is stale")
        prior_filesystems = prior["filesystems"]
        deltas: dict[str, int] = {}
        for label in ("home", "cold"):
            current = current_filesystems.get(label)
            previous = prior_filesystems.get(label)
            if not isinstance(current, Mapping) or not isinstance(previous, Mapping):
                raise GovernanceHistoryError("trend filesystem observation is missing")
            if current.get("identity") != previous.get("identity"):
                raise GovernanceHistoryError("trend filesystem identity drifted")
            current_residual = current.get("residual_bytes")
            previous_residual = previous.get("residual_bytes")
            if not isinstance(current_residual, int) or not isinstance(previous_residual, int):
                raise GovernanceHistoryError("trend residual observation is invalid")
            deltas[f"{label}_residual_bytes"] = current_residual - previous_residual
        return TrendDecision(status="trend", prior=identity, deltas=deltas, blockers=())
    except GovernanceHistoryError as error:
        return TrendDecision(status="invalid", prior=None, deltas=None, blockers=(str(error),))


def trend_payload(decision: TrendDecision) -> dict[str, Any]:
    return {
        "status": decision.status,
        "prior": decision.prior,
        "deltas": decision.deltas,
        "blockers": list(decision.blockers),
    }
