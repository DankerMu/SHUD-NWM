"""G4 private cold-env rewrite: preserve unrelated text, replace governed keys only."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from packages.common.compressed_chunk_cold_target import (
    CONTAINER_EXEC_ID_MAX,
    CONTAINER_EXEC_ID_MIN,
    container_exec_id_from_decimal,
)
from packages.common.node27_issue1895_types import Issue1895ReadinessError
from packages.common.safe_fs import atomic_write_bytes_no_follow

UID_KEYS = {
    "NODE27_COLD_RESIDENCY_CONTAINER_EXEC_UID",
    "NODE27_COLD_RESIDENCY_CONTAINER_EXEC_GID",
}

ASSIGNMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
DECIMAL_RE = re.compile(r"^(?:0|[1-9][0-9]*)$")
POSITIVE_DECIMAL_RE = re.compile(r"^[1-9][0-9]*$")
DEVICE_IDENTITY_RE = re.compile(r"^[0-9]+:[0-9]+$")

G4_GOVERNED_KEYS: tuple[str, ...] = (
    "NODE27_COLD_RESIDENCY_COLD_RESERVE_BYTES",
    "NODE27_COLD_RESIDENCY_WAL_RESERVE_BYTES",
    "NODE27_COLD_RESIDENCY_PER_TICK_BOUND",
    "NODE27_COLD_RESIDENCY_CONTAINER_EXEC_UID",
    "NODE27_COLD_RESIDENCY_CONTAINER_EXEC_GID",
)
DEVICE_IDENTITY_KEY = "NODE27_COLD_RESIDENCY_DEVICE_IDENTITY"
LAG_SECONDS_KEY = "NODE27_COLD_RESIDENCY_LAG_SECONDS"
G5_GOVERNED_KEYS: tuple[str, ...] = (
    DEVICE_IDENTITY_KEY,
    LAG_SECONDS_KEY,
)
DATABASE_URL_KEY = "DATABASE_URL"
PRESERVE_BYTE_FOR_BYTE = frozenset({DATABASE_URL_KEY})


def validate_canonical_positive_decimal(value: object, *, label: str) -> str:
    """Accept only a canonical positive decimal: no sign, whitespace, or leading zeros."""

    if not isinstance(value, str) or POSITIVE_DECIMAL_RE.fullmatch(value) is None:
        raise Issue1895ReadinessError(
            f"{label} is not a canonical positive decimal",
            code="ENV_VALUE_INVALID",
            stage="env",
        )
    return value


def parse_assignment_line(line: str) -> tuple[str, str] | None:
    """Return (key, value) for a real assignment; comments and blanks are None."""

    text = line.rstrip("\n\r")
    match = ASSIGNMENT_RE.fullmatch(text)
    if match is None:
        return None
    return match.group(1), match.group(2)


def _assignment_keys(text: str) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines(keepends=True):
        parsed = parse_assignment_line(line)
        if parsed is None:
            continue
        key, _value = parsed
        if key in seen:
            raise Issue1895ReadinessError(
                f"duplicate assignment for {key}",
                code="ENV_DUPLICATE_KEY",
                stage="env",
            )
        seen.add(key)
        keys.append(key)
    return keys


def assert_unambiguous_assignments(text: str) -> None:
    """Refuse duplicate keys. Comments are not assignments and may name the same key."""

    _assignment_keys(text)


def rewrite_cold_env_text(
    original: str,
    *,
    updates: Mapping[str, str],
    governed_keys: Sequence[str] = G4_GOVERNED_KEYS,
    require_device_identity_unassigned: bool = True,
) -> str:
    """Rewrite only governed assignments; preserve comments, blanks, and unrelated keys.

    ``DATABASE_URL`` is copied byte-for-byte from the original assignment line.
    Duplicate or ambiguous assignments refuse.  A nonempty device-identity
    assignment is refused when ``require_device_identity_unassigned`` is true.
    """

    if not isinstance(original, str):
        raise Issue1895ReadinessError("cold env text must be a string", code="ENV_INVALID", stage="env")
    assert_unambiguous_assignments(original)
    governed = tuple(governed_keys)
    missing_updates = [key for key in governed if key not in updates]
    if missing_updates:
        raise Issue1895ReadinessError(
            "governed key update is missing",
            code="ENV_MISSING_UPDATE",
            stage="env",
        )
    extra_updates = [key for key in updates if key not in governed]
    if extra_updates:
        raise Issue1895ReadinessError(
            "update set includes a key this rewrite does not govern",
            code="ENV_UNGOVERNED_UPDATE",
            stage="env",
        )
    for key, value in updates.items():
        if not isinstance(value, str):
            raise Issue1895ReadinessError(
                f"{key} is not a canonical value",
                code="ENV_VALUE_INVALID",
                stage="env",
            )
        if key == DEVICE_IDENTITY_KEY:
            if DEVICE_IDENTITY_RE.fullmatch(value) is None:
                raise Issue1895ReadinessError(
                    "device identity is not st_dev:st_ino",
                    code="ENV_DEVICE_IDENTITY_INVALID",
                    stage="env",
                )
            continue
        if key == LAG_SECONDS_KEY:
            validate_canonical_positive_decimal(value, label=key)
            continue
        if key in UID_KEYS:
            if DECIMAL_RE.fullmatch(value) is None:
                raise Issue1895ReadinessError(
                    f"{key} is not a canonical decimal",
                    code="ENV_VALUE_INVALID",
                    stage="env",
                )
            parsed = container_exec_id_from_decimal(value)
            if parsed is None or parsed < CONTAINER_EXEC_ID_MIN or parsed > CONTAINER_EXEC_ID_MAX:
                raise Issue1895ReadinessError(
                    f"{key} is not a canonical decimal",
                    code="ENV_VALUE_INVALID",
                    stage="env",
                )
            continue
        if POSITIVE_DECIMAL_RE.fullmatch(value) is None:
            raise Issue1895ReadinessError(
                f"{key} is not a canonical decimal",
                code="ENV_VALUE_INVALID",
                stage="env",
            )

    lines_out: list[str] = []
    seen_governed: set[str] = set()
    database_url_line: str | None = None
    for line in original.splitlines(keepends=True):
        newline = ""
        body = line
        if line.endswith("\r\n"):
            newline = "\r\n"
            body = line[:-2]
        elif line.endswith("\n"):
            newline = "\n"
            body = line[:-1]
        parsed = parse_assignment_line(body + "\n")
        if parsed is None:
            stripped = body.lstrip()
            if stripped.startswith("#"):
                commented = parse_assignment_line(stripped[1:] + "\n")
                if commented is not None and commented[0] in governed:
                    key = commented[0]
                    seen_governed.add(key)
                    lines_out.append(f"{key}={updates[key]}{newline or chr(10)}")
                    continue
            lines_out.append(line if line.endswith(("\n", "\r\n")) else line + (newline or "\n"))
            continue
        key, value = parsed
        if key == DEVICE_IDENTITY_KEY and value and require_device_identity_unassigned:
            raise Issue1895ReadinessError(
                "device identity already assigned before install",
                code="ENV_DEVICE_IDENTITY_ASSIGNED",
                stage="env",
            )
        if key == DATABASE_URL_KEY:
            if database_url_line is not None:
                raise Issue1895ReadinessError(
                    "duplicate assignment for DATABASE_URL",
                    code="ENV_DUPLICATE_KEY",
                    stage="env",
                )
            database_url_line = line if line.endswith(("\n", "\r\n")) else line + (newline or "\n")
            if not value.startswith("postgresql://"):
                raise Issue1895ReadinessError(
                    "DATABASE_URL missing",
                    code="ENV_DATABASE_URL_INVALID",
                    stage="env",
                )
            lines_out.append(database_url_line)
            continue
        if key in governed:
            seen_governed.add(key)
            lines_out.append(f"{key}={updates[key]}{newline or chr(10)}")
            continue
        lines_out.append(line if line.endswith(("\n", "\r\n")) else line + (newline or "\n"))

    if database_url_line is None:
        raise Issue1895ReadinessError(
            "DATABASE_URL missing",
            code="ENV_DATABASE_URL_INVALID",
            stage="env",
        )
    for key in governed:
        if key not in seen_governed:
            lines_out.append(f"{key}={updates[key]}\n")
    rewritten = "".join(lines_out)
    assert_unambiguous_assignments(rewritten)
    original_url = parse_assignment_line(database_url_line)
    rewritten_url = None
    for line in rewritten.splitlines(keepends=True):
        parsed = parse_assignment_line(line)
        if parsed is not None and parsed[0] == DATABASE_URL_KEY:
            rewritten_url = line
            break
    if original_url is None or rewritten_url is None:
        raise Issue1895ReadinessError(
            "DATABASE_URL missing after rewrite",
            code="ENV_DATABASE_URL_INVALID",
            stage="env",
        )
    if rewritten_url.encode("utf-8") != (
        database_url_line if database_url_line.endswith(("\n", "\r\n")) else database_url_line + "\n"
    ).encode("utf-8"):
        raise Issue1895ReadinessError(
            "DATABASE_URL bytes changed",
            code="ENV_DATABASE_URL_MUTATED",
            stage="env",
        )
    return rewritten


def rewrite_cold_env_file(
    path: str | Path,
    *,
    updates: Mapping[str, str],
    governed_keys: Sequence[str] = G4_GOVERNED_KEYS,
    require_device_identity_unassigned: bool = True,
    mode: int = 0o600,
) -> bytes:
    """Atomically rewrite ``path`` privately (mode 0600 temp sibling + replace)."""

    target = Path(path)
    original = target.read_bytes()
    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError:
        raise Issue1895ReadinessError(
            "cold env is not UTF-8",
            code="ENV_INVALID",
            stage="env",
        ) from None
    rewritten = rewrite_cold_env_text(
        text,
        updates=updates,
        governed_keys=governed_keys,
        require_device_identity_unassigned=require_device_identity_unassigned,
    )
    payload = rewritten.encode("utf-8")
    atomic_write_bytes_no_follow(
        target,
        payload,
        containment_root=target.parent,
        mode=mode,
        require_durable_replace=True,
    )
    os.chmod(target, mode)
    return payload
