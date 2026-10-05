"""Receipt helpers shared by the steps of a model succession.

Every step of a succession (provision on node-27, publish on node-22) leaves
its receipts under ``<receipt-root>/<succession-id>/``.  The helpers here are
the part that does not depend on the step: the succession id, the receipt
directory, exclusive-create writes and the path / commit records.  What a step
records, and what its apply compares with its dry-run, lives with the step.

Paths are recorded as seen from the host that ran the step; ``object_store_key``
is what the other node resolves against its own mount of the same store.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from packages.common.object_store import normalize_object_key

DEFAULT_RECEIPT_ROOT_KEY = "scheduler/succession"
SUCCESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,80}")


class SuccessionReceiptError(RuntimeError):
    pass


def validate_succession_id(value: str) -> str:
    # "." and ".." match the character class but name the receipt root and its parent.
    if not SUCCESSION_ID_PATTERN.fullmatch(value) or value in {".", ".."}:
        raise SuccessionReceiptError(
            f"Invalid --succession-id {value!r}; expected {SUCCESSION_ID_PATTERN.pattern} and not '.' or '..'."
        )
    return value


def default_receipt_root(object_store_root: str | Path) -> Path:
    return Path(object_store_root) / DEFAULT_RECEIPT_ROOT_KEY


def file_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def object_store_key(path: str | Path, object_store_root: str | Path, object_store_prefix: str = "") -> str | None:
    """Return ``path`` relative to the object-store root, or None when outside it."""

    text = str(path)
    if "://" in text:
        try:
            return normalize_object_key(text, object_store_prefix)
        except ValueError:
            return None
    try:
        relative = Path(text).expanduser().resolve().relative_to(Path(object_store_root).resolve())
    except ValueError:
        return None
    return relative.as_posix()


def path_record(
    path: str | Path,
    *,
    object_store_root: str | Path,
    object_store_prefix: str = "",
    sha256: str | None,
) -> dict[str, Any]:
    return {
        "path": str(path),
        "object_store_key": object_store_key(path, object_store_root, object_store_prefix),
        "sha256": sha256,
    }


def git_commit() -> str | None:
    """Return the commit of the checkout this tool runs from, or None when unavailable."""

    try:
        completed = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = completed.stdout.strip()
    return commit if completed.returncode == 0 and commit else None


def prepare_receipt_target(path: Path, *, receipt_root: str | Path) -> None:
    """Refuse an existing receipt or a directory that cannot hold one.

    Called before a step writes anything else.  The directory is created here
    so that "can be created" is proven on the real filesystem (an NFS export
    answers ``os.access`` for the client, not for the server).  A succession
    directory this run creates is made group-writable and readable by other
    whatever the umask, because the next step of the succession writes its own
    receipt there from the other node as a different user of the same group; the
    tool never changes permissions on the receipt root or on a directory that
    was already there.
    """

    if os.path.lexists(path):
        raise SuccessionReceiptError(
            f"Receipt {path} already exists and is never overwritten; use a new --succession-id."
        )
    directory = path.parent
    try:
        try:
            directory.mkdir(parents=True)
        except FileExistsError:
            pass
        else:
            # Only adds bits: a setgid bit inherited from the receipt root stays.
            os.chmod(directory, stat.S_IMODE(directory.stat().st_mode) | 0o075)
        writable = os.access(directory, os.W_OK | os.X_OK)
        reason = "permission denied"
    except OSError as error:
        writable = False
        reason = str(error)
    if not writable:
        raise SuccessionReceiptError(
            f"Receipt directory {directory} cannot be created or written ({reason}). One-time setup of the "
            f"receipt root, as frd_muziyao on node-22 (on its mount of the same path): mkdir -p {receipt_root} "
            f"&& chgrp nwmuser {receipt_root} && chmod 2775 {receipt_root}. This tool never changes "
            "permissions on the receipt root."
        )


def write_receipt(path: Path, receipt: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    with os.fdopen(os.open(path, flags, 0o644), "w", encoding="utf-8") as handle:
        # 0644 whatever the umask: the other node reads the receipt as a different user.
        os.fchmod(handle.fileno(), 0o644)
        json.dump(receipt, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
