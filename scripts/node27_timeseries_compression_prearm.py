"""Committed pre-arm reset for the node-27 controlled compression replay (#1088).

The replay supervisor refuses to overwrite any pre-existing supervisor-owned
artifact (``run_child``: "{kind} output {label} exists before spawn";
``_artifact_ref``: "checkpoint artifact path already exists"), which is the
correct anti-evidence-shadow trust boundary.  What was missing is the committed
counterpart: a rerun-safe reset that relocates the PREVIOUS arm's residue so the
next arm can spawn at all.

This tool never discards evidence: every eviction is a move-only relocation
(``shutil.move``) into a timestamped ``prearm-archive/<UTC>/`` directory with a
manifest, and this module calls no deletion API directly.  A same-device move is
a rename; a cross-device move (the schema dump may live on another mount) copies
the bytes into the archive first and only then lets shutil's own fallback drop
the source, so the content exists at the destination before the source goes
away.  It fails closed BEFORE the first move when the replay unit is not
``inactive``/``failed``, when the pinned expected-stale digest is missing or
malformed, when an existing terminal receipt does not match that digest, when
the failure-intent family is unresolved, when the run plan is present but
unparsable, or when a plan-authored label would name a destination outside the
archive.  A move that fails mid-sweep writes a PARTIAL manifest (recording what
already moved and what failed) and then refuses.  Non-move filesystem failures
AFTER the first move (associations-directory creation, the association re-probe,
the terminal manifest write) take the same shape: they attempt the same PARTIAL
manifest and carry the completed-move record inside the refusal message.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from packages.common import compression_terminal_state as terminal_state
from packages.common.safe_fs import SafeFilesystemError, atomic_write_bytes_no_follow

REPO_ROOT = Path(__file__).resolve().parents[1]
UNIT_NAME = "nhms-node27-timeseries-compression-replay.service"
DEFAULT_WORKDIR = "/home/nwm/node27-timeseries-compression-replay"
DEFAULT_ENV_PATH = REPO_ROOT / "infra" / "env" / "node27-timeseries-compression-replay.env"
DEFAULT_SYSTEMCTL = "/usr/bin/systemctl"

RUN_PLAN_NAME = "run-plan.json"
RECEIPT_NAME = "terminal-evidence.json"
ARCHIVE_DIR_NAME = "prearm-archive"
ASSOCIATIONS_DIR_NAME = "associations"
MANIFEST_NAME = "prearm-manifest.json"

# Deliberately minimal: the run plan feeds the unit's ConditionPathExists and the
# terminal receipt is the supervisor's expected-stale CAS anchor.  Everything
# else in the working directory is residue of the previous arm.
WHITELIST = frozenset({RUN_PLAN_NAME, RECEIPT_NAME})

# A running ``Type=oneshot`` unit reports ``activating`` with a NONZERO return
# code, so the gate compares the state TEXT and never the return code alone.
SAFE_UNIT_STATES = frozenset({"inactive", "failed"})

EXPECTED_STALE_ENV_KEY = "NODE27_COMPRESSION_EXPECTED_STALE_SHA256"
NEXT_STEP_COMMAND = f"systemctl --user start {UNIT_NAME}"

SYSTEMCTL_TIMEOUT_SECONDS = 30.0
MAX_ENV_BYTES = 64 * 1024
MAX_GATE_STATE_BYTES = 64 * 1024
MAX_RUN_PLAN_BYTES = 4 * 1024 * 1024
MANIFEST_SCHEMA_VERSION = 1

_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class PrearmError(RuntimeError):
    """One fail-closed refusal.

    The working directory is left byte-identical, except post-move refusals,
    which report what already moved.
    """


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _absolute(value: str | os.PathLike[str]) -> Path:
    """Normalize textually, never through symlinks.

    Plan-authored association paths are literals; resolving them would make an
    operator working under a symlinked prefix (``/var`` -> ``/private/var``)
    silently fall out of the working-directory containment checks below.
    """

    return Path(os.path.normpath(os.path.abspath(os.fspath(value))))


def _read_bytes_no_follow(path: Path, *, limit: int, label: str) -> tuple[bytes, os.stat_result]:
    """Read a small regular file without following a symlinked final component."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise PrearmError(f"{label} is unreadable ({error.strerror}): {path}") from error
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise PrearmError(f"{label} is not a regular file: {path}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise PrearmError(f"{label} exceeds {limit} bytes: {path}")
            chunks.append(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks), info


def parse_env_file(path: Path) -> dict[str, str]:
    """Strict ``KEY=VALUE`` parser for the replay env file.

    Mirrors ``scripts/validate_two_node_docker_runtime.py`` and additionally
    refuses a symlinked file, a mode other than ``0600`` and duplicate keys: the
    file carries the DB password and the pinned expected-stale digest.
    """

    try:
        raw, info = _read_bytes_no_follow(path, limit=MAX_ENV_BYTES, label="replay env file")
    except FileNotFoundError as error:
        raise PrearmError(f"replay env file is absent: {path}") from error
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise PrearmError(f"replay env file mode must be 0600, found {stat.S_IMODE(info.st_mode):04o}: {path}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PrearmError(f"replay env file is not UTF-8: {path}") from error

    env: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            raise PrearmError(f"{path}:{line_number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise PrearmError(f"{path}:{line_number}: empty environment key")
        if key in env:
            raise PrearmError(f"{path}:{line_number}: duplicate environment key {key}")
        if len(value) >= 2 and (
            (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'"))
        ):
            value = value[1:-1]
        env[key] = value
    return env


def probe_unit_state(systemctl: str) -> str:
    """Gate (a): the replay unit must be ``inactive`` or ``failed``.

    ``systemctl is-active`` exits nonzero for every non-active state, so the
    return code cannot distinguish a safe ``inactive`` from a RUNNING oneshot's
    ``activating``.  Only the printed state text is trusted.
    """

    argv = [systemctl, "--user", "is-active", UNIT_NAME]
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=SYSTEMCTL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise PrearmError(f"replay unit state is unavailable via {systemctl}: {error}") from error
    state = completed.stdout.strip()
    if state not in SAFE_UNIT_STATES:
        reported = state or "<no output>"
        raise PrearmError(
            f"replay unit {UNIT_NAME} reports state {reported!r}; "
            "the pre-arm reset only runs when the unit is 'inactive' or 'failed'"
        )
    return state


def assert_expected_stale_digest(env: Mapping[str, str]) -> str:
    """Gate (b1): the pinned expected-stale digest must be a 64-hex sha256.

    Validated UNCONDITIONALLY, including when the receipt is absent: the
    supervisor requires ``re.fullmatch(r"[0-9a-f]{64}", expected_stale_sha256)``
    AND an existing receipt on every arm (no first-arm branch exists), so a
    placeholder digest is a pre-arm defect no matter what is on disk.
    """

    value = env.get(EXPECTED_STALE_ENV_KEY, "").strip()
    if _SHA256_RE.fullmatch(value) is None:
        reported = value or "<unset>"
        raise PrearmError(
            f"{EXPECTED_STALE_ENV_KEY} must be a 64-hex sha256, found {reported!r}; "
            "the supervisor's expected-stale gate requires it on every arm"
        )
    return value


def assert_receipt_matches_expected_stale(receipt_path: Path, expected_sha256: str) -> str | None:
    """Gate (b2): an existing receipt must be the pinned expected-stale one.

    ``None`` means the receipt is ABSENT.  That is not a "first arm" green
    light — the supervisor refuses without it — but the sweep itself is still
    valid, so the caller proceeds and warns.
    """

    try:
        identity = terminal_state.terminal_identity(receipt_path)
    except terminal_state.TerminalStateError as error:
        raise PrearmError(f"terminal receipt identity is unusable: {error}") from error
    if identity is None:
        return None
    if identity.sha256 != expected_sha256:
        raise PrearmError(
            f"{receipt_path} sha256 {identity.sha256} differs from the pinned "
            f"{EXPECTED_STALE_ENV_KEY} {expected_sha256}"
        )
    return identity.sha256


def assert_intent_family_resolved(receipt_path: Path) -> None:
    """Gate (c): the failure-intent state machine must be resolved.

    ``packages/common/compression_terminal_state.py`` keys the whole intent
    family off the RECEIPT name.  A pending ``.failure-intent/`` directory, a
    ``.failure-intent.consumed-<hex>`` sibling or a gate state other than
    ``idle`` means a decided-but-unpublished failure is in flight; relocating it
    would itself be evidence-shadowing, and a PARTIAL sweep of the family
    manufactures the unrecoverable "ambiguous durable location" state.
    """

    parent = receipt_path.parent
    root_name = f".{receipt_path.name}.failure-intent"

    try:
        os.lstat(parent / root_name)
    except FileNotFoundError:
        pass
    except OSError as error:
        raise PrearmError(f"failure-intent directory is unreadable ({error.strerror}): {parent / root_name}") from error
    else:
        raise PrearmError(
            f"unresolved failure intent at {parent / root_name}; resolve it first "
            "(e.g. node27_timeseries_compression_supervisor.py --finalize-only) and re-run the pre-arm reset"
        )

    try:
        names = sorted(os.listdir(parent))
    except OSError as error:
        raise PrearmError(f"working directory is unreadable ({error.strerror}): {parent}") from error
    consumed = [name for name in names if name.startswith(f"{root_name}.consumed-")]
    if consumed:
        raise PrearmError(
            f"unresolved consumed failure intent at {parent / consumed[0]}; resolve it first "
            "(e.g. node27_timeseries_compression_supervisor.py --finalize-only) and re-run the pre-arm reset"
        )

    state_path = parent / f".{receipt_path.name}.intent-gate.json"
    try:
        raw, _ = _read_bytes_no_follow(state_path, limit=MAX_GATE_STATE_BYTES, label="terminal intent gate state")
    except FileNotFoundError:
        # A gate that never transitioned has no state document at all: idle.
        return
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PrearmError(f"terminal intent gate state is not readable JSON: {state_path}") from error
    if not isinstance(parsed, Mapping):
        raise PrearmError(f"terminal intent gate state is not a JSON object: {state_path}")
    state = parsed.get("state")
    if state != "idle":
        raise PrearmError(
            f"terminal intent gate at {state_path} reports state {state!r} (expected 'idle'); resolve the intent "
            "first (e.g. node27_timeseries_compression_supervisor.py --finalize-only) and re-run the pre-arm reset"
        )


def load_run_plan(run_plan_path: Path) -> dict[str, Any] | None:
    """Gate (d): present-but-unparsable refuses; absent means sweep-only mode."""

    try:
        raw, _ = _read_bytes_no_follow(run_plan_path, limit=MAX_RUN_PLAN_BYTES, label="run plan")
    except FileNotFoundError:
        return None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PrearmError(f"run plan is not valid JSON: {run_plan_path}") from error
    if not isinstance(parsed, Mapping):
        raise PrearmError(f"run plan is not a JSON object: {run_plan_path}")
    return dict(parsed)


def _assert_safe_label(label: str, *, kind: str, run_plan_path: Path) -> str:
    """Refuse a plan-authored name that is not ONE safe path component.

    Archive destinations are named from these labels
    (``_collision_safe_destination``), and ``Path("dir") / "/abs"`` is ``/abs``:
    an absolute label lands the archived evidence OUTSIDE the archive entirely,
    and a traversing one ("../../..") lands it back in the replay working
    directory as the next arm's "output exists before spawn" mine.  Checked
    inside gate (d), i.e. BEFORE the first move.
    """

    separators = {"/", os.sep} | ({os.altsep} if os.altsep else set())
    if (
        not label
        or "\x00" in label
        or any(separator in label for separator in separators)
        or label in {".", ".."}
        or os.path.isabs(label)
    ):
        raise PrearmError(
            f"run plan {kind} {label!r} must be a single safe path component "
            f"(non-empty, no path separator, not '.'/'..', not absolute): {run_plan_path}"
        )
    return label


def plan_owned_paths(plan: Mapping[str, Any], *, run_plan_path: Path) -> list[tuple[str, Path]]:
    """Every path the supervisor refuses to overwrite, as (label, path) pairs.

    Commands carry ``artifact_associations``; captures carry exactly
    ``{capture_id, kind, argv, output_path}`` (no associations).  Both sides are
    trust boundaries, so both sides are collected.
    """

    entries: list[tuple[str, Path]] = []

    commands = plan.get("commands")
    if not isinstance(commands, list):
        raise PrearmError(f"run plan commands must be an array: {run_plan_path}")
    for command in commands:
        if not isinstance(command, Mapping):
            raise PrearmError(f"run plan command must be an object: {run_plan_path}")
        associations = command.get("artifact_associations")
        if not isinstance(associations, Mapping):
            raise PrearmError(f"run plan artifact_associations must be an object: {run_plan_path}")
        for label, value in associations.items():
            if not isinstance(label, str) or not label or not isinstance(value, str) or not Path(value).is_absolute():
                raise PrearmError(f"run plan artifact associations must be named absolute paths: {run_plan_path}")
            _assert_safe_label(label, kind="artifact association label", run_plan_path=run_plan_path)
            entries.append((label, _absolute(value)))

    captures = plan.get("captures")
    if not isinstance(captures, list):
        raise PrearmError(f"run plan captures must be an array: {run_plan_path}")
    for capture in captures:
        if not isinstance(capture, Mapping):
            raise PrearmError(f"run plan capture must be an object: {run_plan_path}")
        output_path = capture.get("output_path")
        if not isinstance(output_path, str) or not Path(output_path).is_absolute():
            raise PrearmError(f"run plan capture output_path must be an absolute path: {run_plan_path}")
        raw_label = capture.get("capture_id") or capture.get("kind") or "capture"
        if not isinstance(raw_label, str):
            raise PrearmError(f"run plan capture label must be a string: {run_plan_path}")
        label = _assert_safe_label(raw_label, kind="capture label", run_plan_path=run_plan_path)
        entries.append((label, _absolute(output_path)))

    return entries


def _workdir_owner(workdir: Path, target: Path) -> str | None:
    """The workdir direct-child component that owns ``target``, else ``None``."""

    try:
        relative = target.relative_to(workdir)
    except ValueError:
        return None
    parts = relative.parts
    if not parts:
        return ""
    return parts[0]


def select_association_residue(
    entries: Sequence[tuple[str, Path]], *, workdir: Path
) -> list[tuple[str, Path]]:
    """Plan-owned paths that the working-directory sweep does not already cover."""

    selected: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for label, target in entries:
        owner = _workdir_owner(workdir, target)
        if owner is not None:
            # Whitelisted members stay in place on purpose; anything else under
            # the working directory is relocated by the sweep itself.
            continue
        if target in seen:
            continue
        try:
            os.lstat(target)
        except FileNotFoundError:
            continue
        except OSError as error:
            raise PrearmError(f"plan-owned path is unreadable ({error.strerror}): {target}") from error
        seen.add(target)
        selected.append((label, target))
    return selected


def select_workdir_residue(workdir: Path) -> list[str]:
    """Direct children of the working directory that must be relocated."""

    try:
        names = sorted(os.listdir(workdir))
    except OSError as error:
        raise PrearmError(f"working directory is unreadable ({error.strerror}): {workdir}") from error
    return [name for name in names if name not in WHITELIST and name != ARCHIVE_DIR_NAME]


def _underlying_error_text(error: BaseException) -> str:
    """Render a wrapped filesystem failure the way the existing PrearmError sites do.

    ``SafeFilesystemError`` is a ``RuntimeError`` subclass, NOT an ``OSError``
    (``packages/common/safe_fs.py:10``), so it carries no ``strerror`` and is
    rendered by type name instead.
    """

    if isinstance(error, OSError) and error.strerror:
        return error.strerror
    return f"{type(error).__name__}: {error}"


def _create_archive_dir(archive_root: Path, *, now: datetime) -> Path:
    base = now.strftime("%Y%m%dT%H%M%SZ")
    try:
        os.makedirs(archive_root, exist_ok=True)
    except (OSError, SafeFilesystemError) as error:
        raise PrearmError(
            f"archive root creation failed ({_underlying_error_text(error)}): {archive_root}"
        ) from error
    for attempt in range(1000):
        candidate = archive_root / (base if attempt == 0 else f"{base}-{attempt}")
        try:
            os.mkdir(candidate, 0o700)
        except FileExistsError:
            continue
        except (OSError, SafeFilesystemError) as error:
            raise PrearmError(
                f"archive directory creation failed ({_underlying_error_text(error)}): {candidate}"
            ) from error
        return candidate
    raise PrearmError(f"could not allocate a fresh archive directory under {archive_root}")


def _collision_safe_destination(directory: Path, name: str) -> Path:
    stem = name or "artifact"
    for attempt in range(1000):
        candidate = directory / (stem if attempt == 0 else f"{stem}.{attempt}")
        if not os.path.lexists(candidate):
            return candidate
    raise PrearmError(f"could not allocate a collision-free destination for {name} under {directory}")


def _manifest_document(
    *,
    now: datetime,
    unit_state: str,
    workdir: Path,
    archive_dir: Path,
    run_plan_path: Path | None,
    receipt_sha256: str | None,
    retained: Sequence[str],
    moves: Sequence[Mapping[str, str]],
    failure: Mapping[str, str] | None,
) -> dict[str, Any]:
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "unit": UNIT_NAME,
        "unit_state": unit_state,
        "workdir": os.fspath(workdir),
        "archive_dir": os.fspath(archive_dir),
        "run_plan_path": os.fspath(run_plan_path) if run_plan_path is not None else None,
        "receipt_sha256": receipt_sha256,
        "retained": list(retained),
        "status": "partial" if failure is not None else "complete",
        "failure": dict(failure) if failure is not None else None,
        "moves": [dict(move) for move in moves],
    }


def _write_next_step(stdout: TextIO, *, receipt_present: bool) -> None:
    """The next-step line, qualified when the arm cannot actually start.

    The supervisor requires an existing receipt matching the pin on EVERY arm,
    so a bare green light without one would send the operator into a guaranteed
    refusal.
    """

    if receipt_present:
        stdout.write(f"next: {NEXT_STEP_COMMAND}\n")
        return
    stdout.write(f"next (blocked until {RECEIPT_NAME} is seeded and pinned): {NEXT_STEP_COMMAND}\n")


def _write_manifest(archive_dir: Path, document: Mapping[str, Any]) -> Path:
    manifest_path = archive_dir / MANIFEST_NAME
    atomic_write_bytes_no_follow(
        manifest_path,
        json.dumps(document, indent=2, sort_keys=True).encode("utf-8") + b"\n",
        mode=0o600,
    )
    return manifest_path


def run_prearm(
    *,
    workdir: Path,
    env_path: Path,
    systemctl: str,
    stdout: TextIO,
) -> dict[str, Any]:
    """Run every refusal gate, then relocate residue.  Never destroys anything."""

    workdir = _absolute(workdir)
    env_path = _absolute(env_path)

    # (a) unit state — before anything else touches the working directory.
    unit_state = probe_unit_state(systemctl)

    env = parse_env_file(env_path)

    try:
        info = os.lstat(workdir)
    except FileNotFoundError as error:
        raise PrearmError(f"working directory is absent: {workdir}") from error
    except OSError as error:
        raise PrearmError(f"working directory is unavailable ({error.strerror}): {workdir}") from error
    if not stat.S_ISDIR(info.st_mode):
        raise PrearmError(f"working directory is not a directory: {workdir}")

    receipt_path = workdir / RECEIPT_NAME
    run_plan_path = workdir / RUN_PLAN_NAME

    # (b) expected-stale receipt digest: the pin is validated unconditionally,
    # the comparison only when a receipt exists.
    expected_stale = assert_expected_stale_digest(env)
    receipt_sha256 = assert_receipt_matches_expected_stale(receipt_path, expected_stale)
    if receipt_sha256 is None:
        stdout.write(
            f"warning: {RECEIPT_NAME} is absent; the arm will refuse at the supervisor "
            "expected-stale gate until the stale receipt is seeded\n"
        )

    # (c) failure-intent family.
    assert_intent_family_resolved(receipt_path)

    # (d) run plan.
    plan = load_run_plan(run_plan_path)
    if plan is None:
        stdout.write(
            f"notice: {run_plan_path} is absent; sweep-only mode "
            "(plan-owned residue outside the working directory is not inspected)\n"
        )
        association_entries: list[tuple[str, Path]] = []
    else:
        association_entries = select_association_residue(
            plan_owned_paths(plan, run_plan_path=run_plan_path), workdir=workdir
        )

    workdir_residue = select_workdir_residue(workdir)

    # Whitelisted members are never moved, so this is stable across the sweep;
    # only the ones that actually exist may be reported as retained.
    retained = sorted(name for name in WHITELIST if os.path.lexists(workdir / name))

    # Every gate has passed; only now may anything move.
    if not workdir_residue and not association_entries:
        stdout.write(f"pre-arm reset: nothing to archive; {workdir} is already clean\n")
        _write_next_step(stdout, receipt_present=receipt_sha256 is not None)
        return {
            "archive_dir": None,
            "unit_state": unit_state,
            "receipt_sha256": receipt_sha256,
            "moves": [],
        }

    now = _utc_now()
    archive_dir = _create_archive_dir(workdir / ARCHIVE_DIR_NAME, now=now)
    moves: list[dict[str, str]] = []

    def _archive_move(*, kind: str, label: str, source: Path, destination: Path) -> None:
        """Move one path, or write a PARTIAL manifest and refuse.

        Without this the first ENOSPC mid-sweep escapes as a raw traceback with
        the working directory half swept and NO manifest at all (the manifest is
        written last), which is exactly the state an operator cannot reconstruct.
        """

        record = {
            "kind": kind,
            "label": label,
            "from": os.fspath(source),
            "to": os.fspath(destination),
        }
        try:
            shutil.move(os.fspath(source), os.fspath(destination))
        except (OSError, shutil.Error) as error:
            failure = {**record, "error": f"{type(error).__name__}: {error}"}
            try:
                _write_manifest(
                    archive_dir,
                    _manifest_document(
                        now=now,
                        unit_state=unit_state,
                        workdir=workdir,
                        archive_dir=archive_dir,
                        run_plan_path=run_plan_path if plan is not None else None,
                        receipt_sha256=receipt_sha256,
                        retained=retained,
                        moves=moves,
                        failure=failure,
                    ),
                )
            except (OSError, SafeFilesystemError):
                # A manifest failure must never mask the move failure below.
                pass
            raise PrearmError(
                f"archive move failed: {source} -> {destination} ({error}); "
                f"the partial sweep and its manifest are under {archive_dir}"
            ) from error
        moves.append(record)

    def _document(failure: Mapping[str, str] | None) -> dict[str, Any]:
        return _manifest_document(
            now=now,
            unit_state=unit_state,
            workdir=workdir,
            archive_dir=archive_dir,
            run_plan_path=run_plan_path if plan is not None else None,
            receipt_sha256=receipt_sha256,
            retained=retained,
            moves=moves,
            failure=failure,
        )

    def _best_effort_partial_manifest(failure: Mapping[str, str]) -> str:
        """Write the same PARTIAL manifest the move path writes, best effort.

        Only for post-move refusals that are NOT the manifest write itself; the
        terminal write never retries, because it IS the manifest write.
        """

        try:
            manifest_path = _write_manifest(archive_dir, _document(failure))
        except (OSError, SafeFilesystemError):
            # A manifest failure must never mask the refusal it is recording.
            return f"partial {MANIFEST_NAME} manifest was NOT written"
        return f"partial manifest: {manifest_path}"

    def _post_move_refusal(summary: str, *, manifest_status: str) -> PrearmError:
        """Carry the sweep forensics INSIDE the refusal message.

        ``main()`` prints the refusal with the ``pre-arm reset refused: ``
        prefix in ONE call, so the forensics must ride in the message rather
        than being printed first: otherwise a post-move failure loses the
        out-of-workdir association originals, which exist nowhere else once the
        manifest is gone.
        """

        lines = [summary, f"archive directory: {archive_dir}", manifest_status]
        if moves:
            lines.append(f"completed moves ({len(moves)}), restore manually if needed:")
            lines.extend(f"  {move['from']} -> {move['to']}" for move in moves)
        else:
            lines.append("completed moves: <none>")
        return PrearmError("\n".join(lines))

    for name in workdir_residue:
        _archive_move(
            kind="workdir_member",
            label=name,
            source=workdir / name,
            destination=archive_dir / name,
        )

    if association_entries:
        associations_dir = archive_dir / ASSOCIATIONS_DIR_NAME
        try:
            os.makedirs(associations_dir, mode=0o700, exist_ok=True)
        except (OSError, SafeFilesystemError) as error:
            text = _underlying_error_text(error)
            raise _post_move_refusal(
                f"associations directory creation failed ({text}): {associations_dir}",
                manifest_status=_best_effort_partial_manifest(
                    {
                        "kind": "associations_dir",
                        "label": ASSOCIATIONS_DIR_NAME,
                        "path": os.fspath(associations_dir),
                        "error": f"{type(error).__name__}: {error}",
                    }
                ),
            ) from error
        for label, target in association_entries:
            try:
                os.lstat(target)
            except FileNotFoundError:
                continue
            except OSError as error:
                # POST-move: an unreadable re-probe would otherwise escape as a
                # bare traceback with the workdir residue already relocated.
                # ``os.lstat`` cannot raise ``SafeFilesystemError``, so plain
                # ``OSError`` is the whole surface here.
                raise _post_move_refusal(
                    f"association re-probe failed ({_underlying_error_text(error)}): {target}",
                    manifest_status=_best_effort_partial_manifest(
                        {
                            "kind": "plan_association",
                            "label": label,
                            "path": os.fspath(target),
                            "error": f"{type(error).__name__}: {error}",
                        }
                    ),
                ) from error
            # Narrow on purpose: only the destination probe.  A broad
            # ``except PrearmError`` around the loop would re-wrap the move's
            # own refusal and overwrite its partial manifest.
            try:
                destination = _collision_safe_destination(associations_dir, f"{label}-{target.name}")
            except PrearmError as error:
                raise _post_move_refusal(
                    str(error),
                    manifest_status=_best_effort_partial_manifest(
                        {
                            "kind": "plan_association",
                            "label": label,
                            "path": os.fspath(target),
                            "error": f"{type(error).__name__}: {error}",
                        }
                    ),
                ) from error
            _archive_move(
                kind="plan_association",
                label=label,
                source=target,
                destination=destination,
            )

    try:
        manifest_path = _write_manifest(archive_dir, _document(None))
    except (OSError, SafeFilesystemError) as error:
        text = _underlying_error_text(error)
        raise _post_move_refusal(
            f"manifest write failed ({text}): {archive_dir / MANIFEST_NAME}",
            # The terminal write IS the manifest write: it does not retry.
            manifest_status=f"{MANIFEST_NAME} manifest was NOT written",
        ) from error

    stdout.write(f"pre-arm archive: {archive_dir}\n")
    stdout.write(f"archived {len(moves)} path(s); manifest: {manifest_path}\n")
    for move in moves:
        stdout.write(f"  {move['from']} -> {move['to']}\n")
    stdout.write(f"retained in place: {', '.join(retained) if retained else '<none>'}\n")
    _write_next_step(stdout, receipt_present=receipt_sha256 is not None)

    return {
        "archive_dir": archive_dir,
        "unit_state": unit_state,
        "receipt_sha256": receipt_sha256,
        "moves": moves,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="node27_timeseries_compression_prearm",
        description=(
            "Archive the previous compression-replay arm's residue (move only, never destroy) so the "
            "supervisor's refuse-to-overwrite trust boundary does not burn the next one-shot replay window."
        ),
    )
    parser.add_argument(
        "--workdir",
        default=DEFAULT_WORKDIR,
        help="replay working directory (default: %(default)s)",
    )
    parser.add_argument(
        "--replay-env-path",
        default=os.fspath(DEFAULT_ENV_PATH),
        help="replay env file carrying the pinned expected-stale digest (default: %(default)s)",
    )
    parser.add_argument(
        "--systemctl",
        default=DEFAULT_SYSTEMCTL,
        help="systemctl binary used for the unit-state gate (default: %(default)s)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run_prearm(
            workdir=Path(args.workdir),
            env_path=Path(args.replay_env_path),
            systemctl=args.systemctl,
            stdout=sys.stdout,
        )
    except PrearmError as error:
        print(f"pre-arm reset refused: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
