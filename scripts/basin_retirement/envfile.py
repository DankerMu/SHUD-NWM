"""The ingest env file of node-27 as the basin retirement tool reads and edits it.

Part of ``scripts/node27_retire_basin.py`` (the entry point).  The file is
sourced by bash (``scripts/node27_autopipe_cron.sh``), so every line this tool
relies on must be one bash reads the same way: a plain ``NAME=value`` with no
quotes, no ``export``, no ``+=`` and nothing after the value.  Anything else
is refused instead of guessed at.  The one write is the ``AUTOPIPE_EXCLUDE_BASINS``
line, after a backup, by atomic replacement; every other byte stays.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from scripts.basin_retirement.model import (
    DATABASE_URL_ENV,
    NOTHING_WRITTEN,
    RetirementRefusal,
    Settings,
    StepFailure,
    fsync_directory,
)

# Imported, not copied: the key this tool writes and compares must be the one the autopipeline derives.
from scripts.node27_autopipeline import _basin_key_set

EXCLUDE_NAME = "AUTOPIPE_EXCLUDE_BASINS"
LOCK_PATH_NAME = "AUTOPIPE_LOCK_PATH"
LOCK_PATH_PROCESS_ENV = "NODE27_AUTOPIPE_LOCK_PATH"
DEFAULT_AUTOPIPE_LOCK_PATH = "/tmp/autopipe.cron.lock"
REQUIRED_MODE = 0o600

_EXCLUDE_LINE = re.compile(rb"AUTOPIPE_EXCLUDE_BASINS=([A-Za-z0-9_,.-]*)")
_DATABASE_URL_LINE = re.compile(rb"DATABASE_URL=(.*)")
# A path as bash reads it literally: no quote, no blank, no expansion, no comment.
_LOCK_PATH_LINE = re.compile(rb"AUTOPIPE_LOCK_PATH=([A-Za-z0-9_./+:@%,=-]*)")
_KEY = re.compile(r"[A-Za-z0-9_.-]+")
_QUOTES = ("'", '"')


def _any_assignment(name: str) -> re.Pattern[bytes]:
    """Every form in which a line can assign ``name`` when bash sources the file."""

    return re.compile(rb"\s*(export\s+)?" + name.encode("ascii") + rb"\+?=")


def _plain_lines(content: bytes, name: str, plain: re.Pattern[bytes]) -> tuple[list[tuple[int, bytes]], int]:
    """``(index, value)`` of every plain assignment line of ``name``, and how many lines assign it in any form.

    The file is split on ``\\n`` alone, so a line with a carriage return is not a plain line.
    """

    anything = _any_assignment(name)
    lines = content.split(b"\n")
    matches = [(index, match.group(1)) for index, line in enumerate(lines) if (match := plain.fullmatch(line))]
    return matches, sum(1 for line in lines if anything.match(line))


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def basin_key(basin_id: str) -> str:
    """The basin as the exclusion list names it; ``ValueError`` when the id does not give exactly one key."""

    keys = _basin_key_set(basin_id)
    if len(keys) != 1:
        raise ValueError(f"basin_id {basin_id!r} gives {len(keys)} exclusion keys; expected exactly one")
    (key,) = keys
    if not _KEY.fullmatch(key):
        raise ValueError(f"basin_id {basin_id!r} gives the exclusion key {key!r}, which the list cannot hold")
    return key


def basin_keys(value: str) -> set[str]:
    """The keys a comma-separated list, or one basin id, stands for in the autopipeline."""

    return _basin_key_set(value)


def check_database_binding(settings: Settings) -> None:
    """Refuse unless the env file names, in one plain line, the database this process would write to.

    Neither value is ever put in a message.
    """

    try:
        content = settings.env_file.read_bytes()
    except OSError as error:
        raise RetirementRefusal(
            f"Refused: cannot read the env file {settings.env_file} ({error}). {NOTHING_WRITTEN}"
        ) from error
    plain, assignments = _plain_lines(content, DATABASE_URL_ENV, _DATABASE_URL_LINE)
    if len(plain) != 1 or assignments != 1:
        raise RetirementRefusal(
            f"Refused: the env file {settings.env_file} must hold exactly one line {DATABASE_URL_ENV}=<value> "
            f"(unquoted, the whole line); it has {len(plain)} such lines and {assignments} lines assigning "
            f"{DATABASE_URL_ENV} in any form. {NOTHING_WRITTEN}"
        )
    try:
        value = plain[0][1].decode("utf-8")
    except UnicodeDecodeError as error:
        raise RetirementRefusal(
            f"Refused: the {DATABASE_URL_ENV} line of {settings.env_file} is not UTF-8. {NOTHING_WRITTEN}"
        ) from error
    if not value or value.startswith(_QUOTES) or value.endswith(_QUOTES):
        raise RetirementRefusal(
            f"Refused: the {DATABASE_URL_ENV} line of {settings.env_file} must be {DATABASE_URL_ENV}=<value>, "
            f"non-empty and unquoted. {NOTHING_WRITTEN}"
        )
    if value != settings.database_url:
        raise RetirementRefusal(
            f"Refused: {DATABASE_URL_ENV} of this process is not the one in {settings.env_file}. The tool edits "
            "the exclusion list of that file and writes to the database of this process: they must be the same "
            f"deployment. Load {DATABASE_URL_ENV} from that file (source it, or cut its line). {NOTHING_WRITTEN}"
        )


def hold_instance_lock(settings: Settings) -> int:
    """Take the exclusive lock beside the env file for the whole run; the descriptor is held until exit."""

    path = settings.lock_file
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, REQUIRED_MODE)
    except OSError as error:
        raise RetirementRefusal(f"Refused: cannot open the lock file {path} ({error}). {NOTHING_WRITTEN}") from error
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        os.close(descriptor)
        raise RetirementRefusal(
            f"Refused: another basin retirement is running (it holds {path}): {error}. Two retirements editing "
            f"the same env file would lose one of the keys; run them one after the other. {NOTHING_WRITTEN}"
        ) from error
    return descriptor


@dataclass(frozen=True)
class EnvFile:
    """The env file as read once, after its checks."""

    path: Path
    content: bytes
    line_index: int  # of the one AUTOPIPE_EXCLUDE_BASINS line, in ``content.split(b"\n")``
    value: str  # what follows ``AUTOPIPE_EXCLUDE_BASINS=`` on that line

    @property
    def sha256(self) -> str:
        return sha256(self.content)

    @property
    def keys(self) -> set[str]:
        return basin_keys(self.value)


def _read_checked(path: Path) -> bytes:
    try:
        status = os.lstat(path)
    except OSError as error:
        raise StepFailure(f"The env file {path} cannot be read ({error}).") from error
    if stat.S_ISLNK(status.st_mode):
        raise StepFailure(f"The env file {path} is a symlink; the autopipe refuses it too.")
    if not stat.S_ISREG(status.st_mode):
        raise StepFailure(f"The env file {path} is not a regular file.")
    mode = stat.S_IMODE(status.st_mode)
    if mode != REQUIRED_MODE:
        raise StepFailure(f"The env file {path} has mode {mode:04o}; it must be exactly {REQUIRED_MODE:04o}.")
    if status.st_uid != os.geteuid():
        raise StepFailure(f"The env file {path} is owned by uid {status.st_uid}, not by this user ({os.geteuid()}).")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise StepFailure(f"The env file {path} cannot be opened ({error}).") from error
    with os.fdopen(descriptor, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if (opened.st_dev, opened.st_ino) != (status.st_dev, status.st_ino):
            raise StepFailure(f"The env file {path} was replaced while it was being checked.")
        return handle.read()


def load(path: Path) -> EnvFile:
    """Read the env file; a ``StepFailure`` when it is not a file this tool may rely on or edit."""

    content = _read_checked(path)
    plain, assignments = _plain_lines(content, EXCLUDE_NAME, _EXCLUDE_LINE)
    if len(plain) != 1 or assignments != 1:
        raise StepFailure(
            f"The env file {path} must hold exactly one line {EXCLUDE_NAME}=<list> with nothing but "
            f"[A-Za-z0-9_,.-] after the '=' (no quotes, no comment, no export, no +=) and no other assignment "
            f"of it; it has {len(plain)} such lines and {assignments} lines assigning {EXCLUDE_NAME} in any form. "
            "bash would read something other than what this tool writes. The file was not changed."
        )
    index, value = plain[0]
    return EnvFile(path=path, content=content, line_index=index, value=value.decode("ascii"))


def autopipe_lock_path(content: bytes, environ: Mapping[str, str]) -> str:
    """The lock file the autopipe cron script holds, resolved in the order the script resolves it."""

    plain, assignments = _plain_lines(content, LOCK_PATH_NAME, _LOCK_PATH_LINE)
    if len(plain) > 1 or assignments != len(plain):
        raise StepFailure(
            f"The env file must hold at most one plain line {LOCK_PATH_NAME}=<path> and no other assignment of it; "
            f"it has {len(plain)} such lines and {assignments} lines assigning {LOCK_PATH_NAME} in any form. "
            "Which lock file the autopipe holds cannot be told."
        )
    if plain and plain[0][1]:
        return plain[0][1].decode("ascii")
    return environ.get(LOCK_PATH_PROCESS_ENV) or DEFAULT_AUTOPIPE_LOCK_PATH


def _ensure_backup(backup: Path, content: bytes) -> str:
    """Write the backup, or keep one an earlier attempt left; ``written`` or ``kept``.  Never overwrites."""

    try:
        descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, REQUIRED_MODE)
    except FileExistsError:
        try:
            existing_descriptor = os.open(backup, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(existing_descriptor, "rb") as handle:
                existing = handle.read()
        except OSError as error:
            raise StepFailure(
                f"The env backup {backup} already exists and cannot be read ({error}); it is never overwritten. "
                "The env file was not changed."
            ) from error
        if existing != content:
            raise StepFailure(
                f"The env backup {backup} already exists and its bytes differ from the env file's; it is never "
                "overwritten. Compare the two, move the backup aside if the env file is the one to keep, and run "
                "the same command. The env file was not changed."
            )
        return "kept"
    except OSError as error:
        raise StepFailure(
            f"The env backup {backup} cannot be created ({error}). The env file was not changed."
        ) from error
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), REQUIRED_MODE)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        fsync_directory(backup.parent)
    except BaseException:
        # A cut-off backup of this run would block the rerun as "bytes differ".
        backup.unlink(missing_ok=True)
        raise
    return "written"


def add_key(settings: Settings, env: EnvFile, key: str) -> dict[str, str]:
    """Append ``key`` to the exclusion line: backup, temporary file, fsync, rename, directory fsync."""

    backup_outcome = _ensure_backup(settings.env_backup, env.content)
    lines = env.content.split(b"\n")
    lines[env.line_index] += (b"," if env.value else b"") + key.encode("ascii")
    content = b"\n".join(lines)
    directory = env.path.parent
    try:
        descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=f".{env.path.name}.retire-", suffix=".tmp")
    except OSError as error:
        raise StepFailure(
            f"Cannot create a temporary file in {directory} ({error}). The env file was not changed."
        ) from error
    renamed = False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), REQUIRED_MODE)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        # The instance lock keeps other retirements out, not an editor: what is replaced must be what was read.
        if _read_checked(env.path) != env.content:
            raise StepFailure(f"The env file {env.path} changed while it was being edited. It was not replaced.")
        os.replace(temporary, env.path)
        renamed = True
    except OSError as error:
        raise StepFailure(f"The env file {env.path} could not be replaced ({error}). It was not changed.") from error
    finally:
        if not renamed:
            Path(temporary).unlink(missing_ok=True)
    fsync_directory(directory)
    return {"backup_outcome": backup_outcome, "sha256_after": sha256(content)}
