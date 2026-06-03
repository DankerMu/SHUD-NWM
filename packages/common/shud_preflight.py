"""Shared SHUD executable production preflight.

This module is the single source of truth for rejecting stub SHUD executables
and validating that a configured SHUD binary is runnable before any Slurm
submission or runtime invocation happens. It is reused by:

- ``workers.shud_runtime.runtime`` (runtime-level guard before ``subprocess``),
- ``services.production_closure.slurm_validation`` (offline preflight evidence),
- ``services.orchestrator.scheduler`` (pass-level pre-submit gate).

Every external probe (``--version``/``--help``, ``ldd``) is bounded by a timeout
and fail-safe: a probe that errors or times out never raises and never produces
a false PASS. All blocker messages and library/path fields are routed through
``packages.common.redaction`` so secrets are never leaked into evidence.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from packages.common.redaction import redact_text

# Basenames / realpath leaves that are never a real SHUD solver. ``true``/``false``
# are the canonical stubs called out by the spec; the rest are common no-op shims.
STUB_EXECUTABLE_BASENAMES = frozenset({"true", "false", ":", "echo", "cat", "noop", "stub"})

# Tokens that a genuine SHUD ``--version``/``--help`` banner is expected to emit.
# A stub like ``/bin/true`` produces no output and therefore fails this signal.
SHUD_IDENTITY_TOKENS = ("shud", "simulator of hydrologic")

# Bounded external-probe timeout (seconds). Kept small so a hung binary cannot
# stall the scheduler pass.
VERSION_PROBE_TIMEOUT_SECONDS = 10.0
LDD_PROBE_TIMEOUT_SECONDS = 10.0

# Bounded captured-output length for redaction/inspection.
MAX_PROBE_OUTPUT_BYTES = 64 * 1024

_LDD_NOT_FOUND_RE = re.compile(r"^\s*(?P<lib>\S+)\s*=>\s*not found", re.MULTILINE)


@dataclass(frozen=True)
class ShudPreflightResult:
    """Outcome of a SHUD executable preflight.

    ``ok`` is ``True`` only when no blockers were produced. ``blockers`` is a list
    of typed, redacted dicts shaped like the rest of the production-closure /
    scheduler blocker payloads (``error_code`` + safe fields).
    """

    ok: bool
    blockers: list[dict[str, str]] = field(default_factory=list)
    checks: dict[str, object] = field(default_factory=dict)


def _redact(value: str) -> str:
    return redact_text(str(value))


def _resolve_executable_path(executable: str) -> str | None:
    """Return an absolute path for ``executable`` or ``None`` if not resolvable.

    Mirrors ``subprocess`` lookup: an executable containing a path separator is
    used as-is, otherwise it is resolved against ``PATH`` via ``shutil.which``.
    """

    candidate = executable.strip()
    if not candidate:
        return None
    if os.sep in candidate or (os.altsep and os.altsep in candidate):
        return str(Path(candidate).expanduser())
    return shutil.which(candidate)


def _is_stub_basename(path: str) -> bool:
    name = Path(path).name.lower()
    return name in STUB_EXECUTABLE_BASENAMES


def check_shud_executable(
    executable: str | None,
    *,
    probe_version: bool = True,
    probe_libraries: bool = True,
) -> ShudPreflightResult:
    """Validate a configured SHUD executable, fail-safe and bounded.

    Order of checks (cheapest / most decisive first):
    1. empty / unset configuration,
    2. stub basename (``true``/``false``/…) before/after resolution,
    3. resolvable on PATH / exists on disk,
    4. is a regular, executable file (visible from this execution context),
    5. shared libraries resolve (``ldd``; missing libs -> blocker),
    6. emits a bounded SHUD ``--version``/``--help`` identity signal.

    ``probe_version`` / ``probe_libraries`` allow callers to skip the external
    probes (e.g. unit tests, or platforms without ``ldd``) while keeping the
    static checks.
    """

    blockers: list[dict[str, str]] = []
    checks: dict[str, object] = {}

    configured = "" if executable is None else str(executable).strip()
    checks["configured"] = bool(configured)
    if not configured:
        blockers.append(
            {
                "error_code": "SHUD_EXECUTABLE_NOT_CONFIGURED",
                "field": "SHUD_EXECUTABLE",
                "message": "SHUD_EXECUTABLE is empty or unset; refusing to submit a production hydro run.",
            }
        )
        return ShudPreflightResult(ok=False, blockers=blockers, checks=checks)

    # Stub basename rejection on the configured value (catches ``/bin/true`` even
    # if the file does not exist on this box).
    if _is_stub_basename(configured):
        blockers.append(
            {
                "error_code": "SHUD_EXECUTABLE_STUB_REJECTED",
                "field": "SHUD_EXECUTABLE",
                "executable": _redact(Path(configured).name),
                "message": "SHUD_EXECUTABLE points at a known stub/no-op binary; refusing to submit.",
            }
        )
        return ShudPreflightResult(ok=False, blockers=blockers, checks=checks)

    resolved = _resolve_executable_path(configured)
    checks["resolved"] = bool(resolved)
    if resolved is None:
        blockers.append(
            {
                "error_code": "SHUD_EXECUTABLE_MISSING",
                "field": "SHUD_EXECUTABLE",
                "executable": _redact(Path(configured).name),
                "message": "SHUD_EXECUTABLE is not found on PATH or on disk in this execution context.",
            }
        )
        return ShudPreflightResult(ok=False, blockers=blockers, checks=checks)

    # Re-check stub after realpath resolution (e.g. a symlink named ``shud`` that
    # points at ``/bin/true``).
    try:
        real = os.path.realpath(resolved)
    except OSError:
        real = resolved
    if _is_stub_basename(real):
        blockers.append(
            {
                "error_code": "SHUD_EXECUTABLE_STUB_REJECTED",
                "field": "SHUD_EXECUTABLE",
                "executable": _redact(Path(real).name),
                "message": "SHUD_EXECUTABLE resolves to a known stub/no-op binary; refusing to submit.",
            }
        )
        return ShudPreflightResult(ok=False, blockers=blockers, checks=checks)

    try:
        file_stat = os.stat(resolved)
    except OSError:
        blockers.append(
            {
                "error_code": "SHUD_EXECUTABLE_MISSING",
                "field": "SHUD_EXECUTABLE",
                "executable": _redact(Path(resolved).name),
                "message": "SHUD_EXECUTABLE is not accessible in this execution context.",
            }
        )
        return ShudPreflightResult(ok=False, blockers=blockers, checks=checks)

    is_regular = stat.S_ISREG(file_stat.st_mode)
    is_executable = bool(file_stat.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
    checks["regular_file"] = is_regular
    checks["executable_bit"] = is_executable
    if not is_regular or not is_executable:
        blockers.append(
            {
                "error_code": "SHUD_EXECUTABLE_NOT_EXECUTABLE",
                "field": "SHUD_EXECUTABLE",
                "executable": _redact(Path(resolved).name),
                "message": "SHUD_EXECUTABLE is not a regular executable file.",
            }
        )
        return ShudPreflightResult(ok=False, blockers=blockers, checks=checks)

    if probe_libraries:
        missing_libs = _missing_shared_libraries(resolved)
        if missing_libs is not None:
            checks["shared_libraries_checked"] = True
            if missing_libs:
                checks["missing_shared_libraries"] = [_redact(lib) for lib in missing_libs]
                for lib in missing_libs:
                    blockers.append(
                        {
                            "error_code": "SHUD_EXECUTABLE_LIBRARY_MISSING",
                            "field": "SHUD_EXECUTABLE",
                            "library": _redact(lib),
                            "message": "SHUD_EXECUTABLE is missing a required shared library.",
                        }
                    )
        else:
            checks["shared_libraries_checked"] = False

    if probe_version:
        signal = _version_identity_signal(resolved)
        checks["version_signal"] = signal
        if signal == "absent":
            blockers.append(
                {
                    "error_code": "SHUD_EXECUTABLE_VERSION_SIGNAL_MISSING",
                    "field": "SHUD_EXECUTABLE",
                    "executable": _redact(Path(resolved).name),
                    "message": (
                        "SHUD_EXECUTABLE did not emit a recognizable SHUD version/help banner; "
                        "it may be a non-SHUD stub."
                    ),
                }
            )

    return ShudPreflightResult(ok=not blockers, blockers=blockers, checks=checks)


def _missing_shared_libraries(resolved: str) -> list[str] | None:
    """Return missing shared libraries via ``ldd``, fail-safe.

    Returns ``None`` when the check could not be performed (no ``ldd``, timeout,
    error) so the caller records "not checked" rather than a false blocker.
    Returns ``[]`` when all libraries resolve.
    """

    ldd = shutil.which("ldd")
    if ldd is None:
        return None
    try:
        completed = subprocess.run(
            [ldd, resolved],
            capture_output=True,
            text=True,
            timeout=LDD_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    combined = ((completed.stdout or "") + "\n" + (completed.stderr or ""))[:MAX_PROBE_OUTPUT_BYTES]
    if "not a dynamic executable" in combined.lower():
        return []
    return sorted({match.group("lib") for match in _LDD_NOT_FOUND_RE.finditer(combined)})


def _version_identity_signal(resolved: str) -> str:
    """Probe ``--version``/``-v``/``--help`` for a SHUD identity token, fail-safe.

    Returns ``"present"`` when a SHUD banner is seen, ``"absent"`` when probes ran
    but emitted no SHUD identity, and ``"unknown"`` when no probe could run
    (all errored/timed out) so the caller does not fabricate a failure.
    """

    ran_any = False
    for flag in ("--version", "-v", "--help", "-h"):
        try:
            completed = subprocess.run(
                [resolved, flag],
                capture_output=True,
                text=True,
                timeout=VERSION_PROBE_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        ran_any = True
        combined = ((completed.stdout or "") + " " + (completed.stderr or ""))[:MAX_PROBE_OUTPUT_BYTES].lower()
        if any(token in combined for token in SHUD_IDENTITY_TOKENS):
            return "present"
    return "absent" if ran_any else "unknown"
