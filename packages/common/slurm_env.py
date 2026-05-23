from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

SENSITIVE_SLURM_ENV_KEY_RE = re.compile(
    r"(TOKEN|PASSWORD|PASSWD|PWD|SECRET|CREDENTIAL|API_?KEY|ACCESS_?KEY|SESSION_?KEY|SIGNATURE)",
    re.IGNORECASE,
)
DATABASE_DSN_ENV_KEY_RE = re.compile(
    r"(^DATABASE_URL$|DATABASE.*(?:DSN|URI|URL)|(?:^|_)DB_(?:DSN|URI|URL)$|"
    r"(?:^|_)(?:PG|POSTGRES|POSTGRESQL)_(?:DSN|URI|URL)$|SQLALCHEMY_DATABASE_URI)",
    re.IGNORECASE,
)
SECRET_URL_QUERY_KEY_RE = re.compile(
    r"(^|[-_])(token|password|passwd|pwd|secret|signature|credential|api[-_]?key|"
    r"access[-_]?key|session[-_]?key)$|^x-amz-signature$|^x-amz-credential$",
    re.IGNORECASE,
)


def is_sensitive_slurm_env_key(key: str) -> bool:
    """Return whether a Slurm env key is unsafe to export or record as evidence."""

    return bool(SENSITIVE_SLURM_ENV_KEY_RE.search(key) or DATABASE_DSN_ENV_KEY_RE.search(key))


def secret_bearing_url_reason(value: str) -> str | None:
    """Return why a URL-shaped env value carries a secret, or None when safe."""

    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return None
    if parsed.username is not None or parsed.password is not None:
        return "url_userinfo"
    for key, _value in parse_qsl(parsed.query, keep_blank_values=True):
        if SECRET_URL_QUERY_KEY_RE.search(key):
            return "url_secret_query_param"
    return None

