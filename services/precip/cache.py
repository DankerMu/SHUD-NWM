"""PNG file cache and ETag derivation.

The cache directory segment `<storage_source>/<cycle_token>` is byte-identical to
the mirror's `canonical/<S>/<K>` pair (`IFS/2026090212`), so #2011 retention can
prune both trees by the same name. Writes use the `.<name>.<suffix>.tmp` +
`os.replace` pattern of `services/tiles/mvt.py::_write_file_cache`.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from services.precip.constants import FILE_CACHE_DIR_ENV


def cache_root() -> Path | None:
    """The configured cache root, or None when caching is off."""
    root = os.getenv(FILE_CACHE_DIR_ENV, "").strip()
    if not root:
        return None
    return Path(root).expanduser()


def cache_file_path(
    root: Path | str,
    *,
    storage_source: str,
    cycle_token: str,
    valid_time: str,
    palette_version: str,
    digest: str,
) -> Path:
    return Path(root) / "precip" / storage_source / cycle_token / f"{valid_time}.{palette_version}.{digest}.png"


def tmp_suffix() -> str:
    """Per-writer tmp name discriminator (the pid, as the MVT cache uses)."""
    return str(os.getpid())


def read_cached_png(path: Path) -> bytes | None:
    try:
        if not path.is_file():
            return None
        return path.read_bytes()
    except OSError:
        return None


def write_cached_png(path: Path, data: bytes) -> bool:
    """Atomic tmp+rename write; any OSError degrades to "not cached", never to a 500."""
    tmp_path = path.with_name(f".{path.name}.{tmp_suffix()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_bytes(data)
        os.replace(tmp_path, path)
    except OSError:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        return False
    return True


def precip_etag(
    *,
    source: str,
    cycle: str,
    valid_time: str,
    palette_version: str,
    object_keys: list[str],
) -> str:
    """Weak ETag over the identity tuple, NOT over the rendered bytes.

    The slice key list is part of the identity, so a late-arriving intermediate
    cycle rotates the ETag together with the cache file name.
    """
    payload = json.dumps([source, cycle, valid_time, palette_version, object_keys], separators=(",", ":")).encode(
        "utf-8"
    )
    return f'W/"precip-{hashlib.sha256(payload).hexdigest()}"'
