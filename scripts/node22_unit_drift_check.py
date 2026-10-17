#!/usr/bin/env python3
"""Read-only drift probe for the node-22 user-scope systemd units (#2911).

``infra/systemd/node22/MANIFEST.json`` lists every unit file deployed under
node-22's ``~/.config/systemd/user/`` as ``{deployed_relative_path,
tracked_path, sha256}``.  This probe compares each deployed file with its
tracked copy byte for byte, classifies it ``same`` / ``differs`` / ``missing``
(``differs`` prints a unified diff to stdout), then lists every deployed
``nhms-*.service`` / ``nhms-*.timer`` at the top level and every
``nhms-*.service.d/*.conf`` one level down that the manifest does not name as
``untracked``.  Backup directories, ``*.service.pre-*`` / ``*.conf.<date>``
leftovers, ``*.wants/`` and every symlink found by the scan are ignored by
construction.

Read-only guarantees, load-bearing for running it on the live host:

* It never writes under the systemd directory.  The only file it writes is
  the ``--receipt`` (temp file in the receipt's own directory, then
  ``os.replace``, mode 0600).  Before anything is read or written, the
  receipt is refused when it already exists as anything but a regular file
  (a symlink, directory, FIFO ...), when the directory it sits in is the
  systemd directory or below it, or when its fully resolved path is.
* It never calls ``systemctl`` or any other program: standard library only,
  no ``subprocess``, no import of ``nhms`` or any repo package, so it runs
  under node-22's 3.12.7 ``.venv`` and the repo's 3.11 pin alike.
* The receipt never carries file content -- only paths, statuses and the
  manifest's sha256 -- so a secret pasted into a drop-in reaches stdout (the
  diff) but never the receipt.

Exit status is the contract: 0 when every manifest entry is ``same`` and
nothing is untracked; 1 when any entry is ``differs`` or ``missing`` or
anything is untracked; 2 when the probe cannot be trusted (manifest missing,
empty, invalid or malformed, a tracked file that is unreadable or disagrees
with its sha256, the systemd directory missing, a deployed file that exists
but cannot be read as a regular file, or the receipt refused or unwritable).
2 outranks 1.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import stat
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

PROGRAM = "node22_unit_drift_check.py"
RECEIPT_SCHEMA_VERSION = "nhms.node22.unit_drift_check.v1"
MANIFEST_SCHEMA_VERSION = "nhms.node22.unit_manifest.v1"
MANIFEST_RELATIVE_PATH = "infra/systemd/node22/MANIFEST.json"
ENTRY_KEYS = frozenset({"deployed_relative_path", "tracked_path", "sha256"})
TRACKED_PREFIX = "infra/systemd/"

STATUS_SAME = "same"
STATUS_DIFFERS = "differs"
STATUS_MISSING = "missing"

RESULT_OK = "ok"
RESULT_DRIFT = "drift"
RESULT_PROBE_FAILED = "probe_failed"


class ProbeError(Exception):
    """A condition that makes the probe's answer untrustworthy (exit 2)."""


@dataclass(frozen=True)
class ManifestEntry:
    deployed_relative_path: str
    tracked_path: str
    tracked_bytes: bytes


@dataclass
class Manifest:
    path: Path
    sha256: str | None = None
    entries: list[ManifestEntry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Manifest validation (fail-closed)
# ---------------------------------------------------------------------------


def _path_field_errors(label: str, value: str) -> list[str]:
    errors = []
    posix = PurePosixPath(value)
    if posix.is_absolute():
        errors.append(f"{label} is absolute: {value!r}")
    if ".." in posix.parts:
        errors.append(f"{label} contains a '..' segment: {value!r}")
    if posix.as_posix() != value:
        errors.append(f"{label} is not a normalized relative POSIX path: {value!r}")
    return errors


def load_manifest(manifest_path: Path) -> Manifest:
    """Read and validate the manifest; every problem lands in ``errors``.

    Any error means the manifest is untrusted and nothing is classified.  The
    tracked side is part of the manifest's trust: each ``tracked_path`` is
    resolved against the repository root (``parents[3]`` of the manifest) and
    must hash to its entry's ``sha256``.
    """

    manifest = Manifest(path=manifest_path)
    try:
        raw = manifest_path.read_bytes()
    except OSError as error:
        manifest.errors.append(f"manifest unreadable: {manifest_path}: {error.strerror or error}")
        return manifest
    manifest.sha256 = hashlib.sha256(raw).hexdigest()
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        manifest.errors.append(f"manifest is not valid JSON: {error}")
        return manifest
    if not isinstance(document, dict):
        manifest.errors.append("manifest top level is not an object")
        return manifest
    if document.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        manifest.errors.append(
            f"manifest schema_version is {document.get('schema_version')!r}, expected {MANIFEST_SCHEMA_VERSION!r}"
        )
    entries = document.get("entries")
    if not isinstance(entries, list) or not entries:
        manifest.errors.append("manifest entries is missing, not a list, or empty")
        return manifest
    try:
        repo_root = manifest_path.resolve().parents[3]
    except IndexError:
        manifest.errors.append(f"manifest path is too shallow to locate the repository root: {manifest_path}")
        return manifest

    seen: set[str] = set()
    validated: list[ManifestEntry] = []
    for index, entry in enumerate(entries):
        where = f"manifest entry #{index}"
        if not isinstance(entry, dict):
            manifest.errors.append(f"{where} is not an object")
            continue
        if set(entry) != ENTRY_KEYS:
            missing = sorted(ENTRY_KEYS - set(entry))
            extra = sorted(set(entry) - ENTRY_KEYS)
            manifest.errors.append(f"{where} keys differ from {sorted(ENTRY_KEYS)}: missing={missing} extra={extra}")
            continue
        if not all(isinstance(entry[key], str) and entry[key] for key in ENTRY_KEYS):
            manifest.errors.append(f"{where} has a non-string or empty field")
            continue
        deployed = entry["deployed_relative_path"]
        tracked = entry["tracked_path"]
        where = f"manifest entry {deployed!r}"
        entry_errors = _path_field_errors("deployed_relative_path", deployed)
        entry_errors += _path_field_errors("tracked_path", tracked)
        if not tracked.startswith(TRACKED_PREFIX):
            entry_errors.append(f"tracked_path is outside {TRACKED_PREFIX}: {tracked!r}")
        if len(PurePosixPath(deployed).parts) > 2:
            entry_errors.append(f"deployed_relative_path is more than one directory deep: {deployed!r}")
        if deployed in seen:
            entry_errors.append(f"duplicate deployed_relative_path: {deployed!r}")
        seen.add(deployed)
        if entry_errors:
            manifest.errors.extend(f"{where}: {message}" for message in entry_errors)
            continue
        try:
            tracked_bytes = (repo_root / tracked).read_bytes()
        except OSError as error:
            manifest.errors.append(f"{where}: tracked file unreadable: {tracked}: {error.strerror or error}")
            continue
        if hashlib.sha256(tracked_bytes).hexdigest() != entry["sha256"]:
            manifest.errors.append(f"{where}: tracked file {tracked} does not match the manifest sha256")
            continue
        validated.append(ManifestEntry(deployed, tracked, tracked_bytes))

    if not manifest.errors:
        manifest.entries = validated
    return manifest


# ---------------------------------------------------------------------------
# Classification and untracked scan (read-only)
# ---------------------------------------------------------------------------


def read_deployed(path: Path) -> bytes | None:
    """Deployed bytes, or None when the path does not exist (``missing``).

    ``os.stat`` follows a symlink, so a manifest entry deployed as a link is
    classified by its target's content and a dangling link is ``missing``.
    Anything other than a regular file is refused before it is opened (a FIFO
    would block ``open``).
    """

    try:
        status = os.stat(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ProbeError(f"deployed file unreadable: {path}: {error.strerror or error}") from error
    if not stat.S_ISREG(status.st_mode):
        raise ProbeError(f"deployed path is not a regular file: {path}")
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ProbeError(f"deployed file unreadable: {path}: {error.strerror or error}") from error


def unified_diff(entry: ManifestEntry, deployed: bytes) -> str:
    """Display-only diff; classification is by bytes, never by this text.

    Lines split on LF only.  A final line without LF is marked the way git
    does (``\\ No newline at end of file``), so a deployed copy that differs
    only in its trailing newline still prints a hunk.  If the bytes differ in
    a way the ``errors="replace"`` decode cannot show, a one-line notice is
    printed instead of an empty diff.
    """

    def lines(data: bytes) -> list[str]:
        parts = data.decode("utf-8", errors="replace").split("\n")
        tail = parts.pop()
        out = [part + "\n" for part in parts]
        if tail:
            # One element, so it never compares equal to the same text with LF.
            out.append(tail + "\n\\ No newline at end of file\n")
        return out

    fromfile = f"a/{entry.tracked_path}"
    tofile = f"b/{entry.deployed_relative_path}"
    text = "".join(difflib.unified_diff(lines(entry.tracked_bytes), lines(deployed), fromfile=fromfile, tofile=tofile))
    if not text:
        text = f"--- {fromfile}\n+++ {tofile}\n@@ bytes differ but are not representable as UTF-8 text @@\n"
    return text


def _is_unit_name(name: str) -> bool:
    return name.startswith("nhms-") and (name.endswith(".service") or name.endswith(".timer"))


def scan_deployed(systemd_dir: Path) -> list[str]:
    """Relative POSIX paths of the unit files and drop-ins the scan covers.

    Top level: ``nhms-*.service`` and ``nhms-*.timer`` regular files.  One
    level down: ``nhms-*.service.d/*.conf`` regular files.  Every symlink is
    skipped (a same-name top-level link, a linked directory, ``*.wants/``), as
    is every other directory and anything deeper.
    """

    found: list[str] = []
    try:
        with os.scandir(systemd_dir) as top:
            top_entries = sorted(top, key=lambda item: item.name)
        for item in top_entries:
            if item.is_symlink():
                continue
            if item.is_file(follow_symlinks=False) and _is_unit_name(item.name):
                found.append(item.name)
            elif (
                item.is_dir(follow_symlinks=False)
                and item.name.startswith("nhms-")
                and item.name.endswith(".service.d")
            ):
                with os.scandir(item.path) as dropins:
                    for dropin in dropins:
                        if dropin.is_symlink():
                            continue
                        if dropin.is_file(follow_symlinks=False) and dropin.name.endswith(".conf"):
                            found.append(f"{item.name}/{dropin.name}")
    except OSError as error:
        raise ProbeError(f"systemd directory scan failed: {error.filename}: {error.strerror or error}") from error
    return found


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def receipt_refusal(receipt: Path, systemd_dir: Path) -> str | None:
    """Why the receipt path is refused, or None.  Checked before any input is read.

    ``os.replace`` swaps the directory entry itself, so a receipt that is a
    symlink would replace the link, not its target: an existing receipt must
    be a regular file (``lstat``, never followed).  Containment is checked on
    where the entry sits (resolved parent plus the unresolved name) -- so a
    link inside the systemd directory pointing outside is refused -- and on
    the fully resolved path.
    """

    try:
        parent_is_dir = receipt.parent.is_dir()
    except OSError as error:
        return f"receipt parent directory cannot be inspected: {receipt.parent}: {error.strerror or error}"
    if not parent_is_dir:
        return f"receipt parent directory does not exist: {receipt.parent}"
    try:
        status = os.lstat(receipt)
    except FileNotFoundError:
        pass
    except OSError as error:
        return f"receipt path cannot be inspected: {receipt}: {error.strerror or error}"
    else:
        if not stat.S_ISREG(status.st_mode):
            return f"receipt path exists and is not a regular file: {receipt}"
    systemd_resolved = systemd_dir.resolve()
    for label, candidate in (
        ("sits", receipt.parent.resolve() / receipt.name),
        ("resolves", receipt.resolve()),
    ):
        if candidate == systemd_resolved or candidate.is_relative_to(systemd_resolved):
            return f"receipt path {label} under the systemd directory: {candidate}"
    return None


def write_receipt(receipt: Path, document: dict[str, Any]) -> None:
    """Temp file in the receipt's own directory, 0600, then ``os.replace``."""

    content = (json.dumps(document, sort_keys=True) + "\n").encode("utf-8")
    descriptor, staged = tempfile.mkstemp(dir=receipt.parent, prefix=f".{receipt.name}.", suffix=".partial")
    try:
        try:
            os.fchmod(descriptor, 0o600)
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(staged, receipt)
    except BaseException:
        try:
            os.unlink(staged)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def default_manifest() -> Path:
    return Path(__file__).resolve().parents[1] / MANIFEST_RELATIVE_PATH


def default_systemd_user_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    if xdg and os.path.isabs(xdg):
        return Path(xdg) / "systemd" / "user"
    return Path.home() / ".config" / "systemd" / "user"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description=(
            "Read-only drift probe for the node-22 user systemd units. It compares every "
            "MANIFEST.json entry's deployed file with its tracked copy and lists untracked "
            "nhms-* units and drop-ins; it never calls systemctl and never writes under the "
            "systemd directory."
        ),
    )
    parser.add_argument(
        "--manifest",
        default=None,
        help=f"manifest path (default: <repo>/{MANIFEST_RELATIVE_PATH} next to this script)",
    )
    parser.add_argument(
        "--systemd-user-dir",
        default=None,
        help=(
            "deployed unit directory (default: $XDG_CONFIG_HOME/systemd/user when "
            "absolute, else ~/.config/systemd/user)"
        ),
    )
    parser.add_argument(
        "--receipt",
        required=True,
        help=(
            "receipt JSON file path; must not sit or resolve under the systemd directory and, if it "
            "exists, must be a regular file (no environment default)"
        ),
    )
    return parser


def _fail_without_receipt(reason: str) -> int:
    print(f"{PROGRAM}: status: {RESULT_PROBE_FAILED} exit_code=2 receipt not written: {reason}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    receipt = Path(arguments.receipt)
    try:
        manifest_path = Path(arguments.manifest) if arguments.manifest is not None else default_manifest()
        systemd_dir = (
            Path(arguments.systemd_user_dir) if arguments.systemd_user_dir is not None else default_systemd_user_dir()
        )
    except RuntimeError as error:
        return _fail_without_receipt(f"cannot resolve a default path: {error}")

    refusal = receipt_refusal(receipt, systemd_dir)
    if refusal is not None:
        return _fail_without_receipt(refusal)

    errors: list[str] = []
    entries: list[dict[str, str]] = []
    untracked: list[str] = []

    manifest = load_manifest(manifest_path)
    errors.extend(manifest.errors)
    try:
        if not systemd_dir.is_dir():
            errors.append(f"systemd directory does not exist: {systemd_dir}")
    except OSError as error:
        errors.append(f"systemd directory cannot be inspected: {systemd_dir}: {error.strerror or error}")

    if not errors:
        for entry in manifest.entries:
            try:
                deployed = read_deployed(systemd_dir / entry.deployed_relative_path)
            except ProbeError as error:
                errors.append(f"manifest entry {entry.deployed_relative_path!r}: {error}")
                continue
            if deployed is None:
                status = STATUS_MISSING
            elif deployed == entry.tracked_bytes:
                status = STATUS_SAME
            else:
                status = STATUS_DIFFERS
                sys.stdout.write(unified_diff(entry, deployed))
            entries.append(
                {
                    "deployed_relative_path": entry.deployed_relative_path,
                    "tracked_path": entry.tracked_path,
                    "status": status,
                }
            )
        try:
            listed = {entry.deployed_relative_path for entry in manifest.entries}
            untracked = sorted(path for path in scan_deployed(systemd_dir) if path not in listed)
        except ProbeError as error:
            errors.append(str(error))

    drift = bool(untracked) or any(entry["status"] != STATUS_SAME for entry in entries)
    if errors:
        exit_code, result = 2, RESULT_PROBE_FAILED
    elif drift:
        exit_code, result = 1, RESULT_DRIFT
    else:
        exit_code, result = 0, RESULT_OK

    document = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "generated_at": utc_now_iso(),
        "status": result,
        "exit_code": exit_code,
        "manifest_path": str(manifest_path.resolve()),
        "manifest_sha256": manifest.sha256,
        "systemd_user_dir": str(systemd_dir.resolve()),
        "entries": entries,
        "untracked": untracked,
        "errors": errors,
    }

    for message in errors:
        print(f"{PROGRAM}: {message}", file=sys.stderr)
    for path in untracked:
        print(f"{PROGRAM}: untracked: {path}", file=sys.stderr)
    try:
        write_receipt(receipt, document)
    except OSError as error:
        return _fail_without_receipt(f"{receipt}: {error.strerror or error}")
    print(f"{PROGRAM}: status: {result} exit_code={exit_code} receipt={receipt}", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
