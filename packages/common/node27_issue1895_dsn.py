"""G7 private nhms_display_ro DSN provenance and RC_DSN_FILE binding."""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from psycopg2.extensions import parse_dsn

from packages.common.node27_issue1895_types import Issue1895ReadinessError
from packages.common.safe_fs import SafeFilesystemError
from packages.common.safe_fs_publication import write_bytes_no_follow_exclusive

DISPLAY_ENV_PATH = Path("/home/nwm/NWM/infra/env/display.env")
DISPLAY_ENV_RELATIVE = Path("infra/env/display.env")
READONLY_ROLE = "nhms_display_ro"
DSN_ENV_KEYS: tuple[str, ...] = (
    "NHMS_DISPLAY_READONLY_DATABASE_URL",
    "NHMS_READONLY_DB_VALIDATION_DATABASE_URL",
)
RC_DSN_ASSIGNMENT = "NHMS_DISPLAY_READONLY_DATABASE_URL"


def _dsn_user(dsn: str) -> str:
    try:
        parsed = parse_dsn(dsn)
    except Exception:
        raise Issue1895ReadinessError(
            "readonly DSN is not parseable",
            code="DSN_INVALID",
            stage="dsn",
        ) from None
    user = str(parsed.get("user") or "")
    if not user:
        try:
            user = urlsplit(dsn).username or ""
        except Exception:
            user = ""
    if user != READONLY_ROLE:
        raise Issue1895ReadinessError(
            "readonly DSN is not the nhms_display_ro role",
            code="DSN_ROLE_INVALID",
            stage="dsn",
        )
    return user


def extract_display_database_url(text: str) -> str:
    """Read DATABASE_URL from display.env text without logging it."""

    found: str | None = None
    assignment = re.compile(r"^DATABASE_URL=(.*)$")
    for line in text.splitlines():
        match = assignment.fullmatch(line.rstrip("\r"))
        if match is None:
            continue
        if found is not None:
            raise Issue1895ReadinessError(
                "display.env has duplicate DATABASE_URL assignments",
                code="DSN_DUPLICATE",
                stage="dsn",
            )
        found = match.group(1)
    if not found or not found.startswith("postgresql://"):
        raise Issue1895ReadinessError(
            "display.env DATABASE_URL is missing",
            code="DSN_MISSING",
            stage="dsn",
        )
    _dsn_user(found)
    return found


def resolve_readonly_dsn(*, display_env_text: str | None = None, environ: dict[str, str] | None = None) -> str:
    """Prefer an already-bound private env, else the display.env DATABASE_URL."""

    env = os.environ if environ is None else environ
    present = [key for key in DSN_ENV_KEYS if str(env.get(key) or "").strip()]
    if len(present) > 1:
        values = {str(env[key]).strip() for key in present}
        if len(values) != 1:
            raise Issue1895ReadinessError(
                "readonly DSN env keys disagree",
                code="DSN_AMBIGUOUS",
                stage="dsn",
            )
    if present:
        dsn = str(env[present[0]]).strip()
        _dsn_user(dsn)
        return dsn
    if display_env_text is None:
        raise Issue1895ReadinessError(
            "readonly DSN provenance is missing",
            code="DSN_MISSING",
            stage="dsn",
        )
    return extract_display_database_url(display_env_text)


def read_display_env_text(path: str | Path) -> str:
    """Read display.env through the shared held no-follow private identity reader."""

    from packages.common.node27_issue1895_private_receipt import read_held_private_text

    return read_held_private_text(
        Path(path),
        label="display.env",
        stage="dsn",
        unreadable_code="DSN_UNREADABLE",
        identity_code="DSN_UNREADABLE",
        toctou_code="DSN_UNREADABLE",
        require_private_parent=False,
    )


def bind_rc_dsn_file(
    path: str | Path,
    *,
    dsn: str,
    mode: int = 0o600,
) -> None:
    """Write RC_DSN_FILE privately. Never log the DSN. Never replace an existing path."""

    if not isinstance(mode, int) or isinstance(mode, bool) or mode != 0o600:
        raise Issue1895ReadinessError(
            "RC_DSN_FILE mode must be exact private 0600 before publication",
            code="DSN_FILE_INVALID",
            stage="dsn",
        )
    _dsn_user(dsn)
    target = Path(path)
    try:
        parent = os.lstat(target.parent)
    except OSError:
        raise Issue1895ReadinessError(
            "RC_DSN_FILE parent is unavailable",
            code="DSN_FILE_PARENT_INVALID",
            stage="dsn",
        ) from None
    if (
        not target.is_absolute()
        or "\x00" in str(target)
        or target.is_symlink()
        or not target.parent.is_dir()
        or parent.st_uid != os.geteuid()
        or (parent.st_mode & 0o777) != 0o700
    ):
        raise Issue1895ReadinessError(
            "RC_DSN_FILE parent must be an euid-owned mode-0700 real directory",
            code="DSN_FILE_PARENT_INVALID",
            stage="dsn",
        )
    if os.path.lexists(target):
        raise Issue1895ReadinessError(
            "RC_DSN_FILE already exists",
            code="DSN_FILE_EXISTS",
            stage="dsn",
        )
    payload = f"{RC_DSN_ASSIGNMENT}={dsn}\n".encode("utf-8")
    from packages.common.node27_issue1895_private_receipt import read_held_private_bytes

    try:
        write_bytes_no_follow_exclusive(
            target,
            payload,
            containment_root=target.parent,
            require_durable_create=True,
            mode=mode,
        )
    except FileExistsError:
        raise Issue1895ReadinessError(
            "RC_DSN_FILE already exists",
            code="DSN_FILE_EXISTS",
            stage="dsn",
        ) from None
    except (OSError, SafeFilesystemError):
        raise Issue1895ReadinessError(
            "RC_DSN_FILE cannot be published exclusively",
            code="DSN_FILE_INVALID",
            stage="dsn",
        ) from None
    try:
        readback, info = read_held_private_bytes(
            target,
            label="RC_DSN_FILE",
            stage="dsn",
            unreadable_code="DSN_FILE_INVALID",
            identity_code="DSN_FILE_INVALID",
            toctou_code="DSN_FILE_INVALID",
        )
    except Issue1895ReadinessError:
        raise
    if readback != payload or (info.st_mode & 0o777) != mode or info.st_nlink != 1 or info.st_uid != os.geteuid():
        raise Issue1895ReadinessError(
            "RC_DSN_FILE post-publication identity is unsafe",
            code="DSN_FILE_INVALID",
            stage="dsn",
        )


def assert_readonly_identity(*, current_user: str, rolsuper: bool) -> None:
    if current_user != READONLY_ROLE:
        raise Issue1895ReadinessError(
            "session current_user is not nhms_display_ro",
            code="DSN_ROLE_INVALID",
            stage="dsn",
        )
    if rolsuper:
        raise Issue1895ReadinessError(
            "nhms_display_ro session is superuser",
            code="DSN_ROLE_NOT_READONLY",
            stage="dsn",
        )
