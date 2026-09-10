"""Owned isolated-oracle probe name/root and current-run mtime contract."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from pathlib import Path

from packages.common.compressed_chunk_cold_probe.types import OWNED_NAME_RE, PROBE_NAME_PREFIX
from packages.common.node27_issue1895_types import Issue1895ReadinessError

_TOKEN_HEX_BYTES = 8


def owned_probe_token() -> str:
    """Return a hex token that makes ``owned_probe_name`` satisfy ``OWNED_NAME_RE``."""

    return secrets.token_hex(_TOKEN_HEX_BYTES)


def owned_probe_name(token: str) -> str:
    """Build a container name whose *basename* matches the shipping owned-name regex."""

    if not isinstance(token, str) or not token or any(ch not in "0123456789abcdef" for ch in token):
        raise Issue1895ReadinessError(
            "probe token must be lowercase hex",
            code="PROBE_NAME_INVALID",
            stage="probe",
        )
    name = f"{PROBE_NAME_PREFIX}{token}"
    if not OWNED_NAME_RE.fullmatch(name):
        raise Issue1895ReadinessError(
            "probe name does not match the shipping owned-name regex",
            code="PROBE_NAME_INVALID",
            stage="probe",
        )
    if not name.startswith(PROBE_NAME_PREFIX):
        raise Issue1895ReadinessError(
            "probe name does not start with the shipping prefix",
            code="PROBE_NAME_INVALID",
            stage="probe",
        )
    return name


def owned_probe_root(parent: str | Path, name: str) -> Path:
    """Require the work-root *basename* (not a parent) to carry ``PROBE_NAME_PREFIX``."""

    root = Path(parent) / name
    if Path(name).name != name or name in {".", ".."} or "/" in name or "\\" in name:
        raise Issue1895ReadinessError(
            "probe root basename must be a single owned name",
            code="PROBE_ROOT_INVALID",
            stage="probe",
        )
    if not OWNED_NAME_RE.fullmatch(name):
        raise Issue1895ReadinessError(
            "probe root basename does not match the shipping owned-name regex",
            code="PROBE_ROOT_INVALID",
            stage="probe",
        )
    if PROBE_NAME_PREFIX not in root.name:
        raise Issue1895ReadinessError(
            "probe work-root basename must contain the shipping probe prefix",
            code="PROBE_ROOT_INVALID",
            stage="probe",
        )
    return root


def parse_bracket_instant(value: str) -> datetime:
    """Parse one timezone-aware bracket instant without leaking its input."""

    try:
        text = str(value).strip()
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone is required")
        return parsed.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        raise Issue1895ReadinessError(
            "bracket instant is not a valid timezone-aware timestamp",
            code="BRACKET_INVALID",
            stage="probe",
        ) from None


def assert_report_within_command_bracket(
    *,
    report_mtime: float,
    start: datetime,
    end: datetime,
) -> None:
    """Require report mtime after the start marker and at or before bracket close.

    Comparing report mtime to the *bracket file* mtime is impossible: the bracket
    is written after the report.  The command start/end instants are the bound.
    """

    if start.tzinfo is None or end.tzinfo is None:
        raise Issue1895ReadinessError(
            "bracket instants must be timezone-aware",
            code="BRACKET_INVALID",
            stage="probe",
        )
    if end < start:
        raise Issue1895ReadinessError(
            "command bracket is reversed",
            code="BRACKET_INVALID",
            stage="probe",
        )
    if not isinstance(report_mtime, (int, float)) or isinstance(report_mtime, bool):
        raise Issue1895ReadinessError(
            "report mtime is not a real timestamp",
            code="REPORT_MTIME_INVALID",
            stage="probe",
        )
    start_ts = start.timestamp()
    end_ts = end.timestamp()
    if report_mtime < start_ts:
        raise Issue1895ReadinessError(
            "oracle report mtime precedes the command start marker",
            code="REPORT_OUT_OF_BRACKET",
            stage="probe",
        )
    if report_mtime > end_ts:
        raise Issue1895ReadinessError(
            "oracle report mtime is after the command bracket close",
            code="REPORT_OUT_OF_BRACKET",
            stage="probe",
        )
