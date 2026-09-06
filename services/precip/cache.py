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
import threading
import uuid
from pathlib import Path

from services.precip.constants import FILE_CACHE_DIR_ENV

# The 8-byte PNG signature and the shortest byte count a complete PNG can have:
# signature (8) + IHDR chunk (4 length + 4 tag + 13 data + 4 CRC = 25) + IEND
# chunk (4 + 4 + 0 + 4 = 12). Anything shorter cannot be a whole image, so it is
# a half-written or truncated file rather than a cache hit.
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MINIMUM_PNG_BYTES = 45


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
    """Per-WRITER tmp name discriminator: pid, thread ident and a fresh uuid4.

    The pid alone (what the MVT cache uses) is not enough here: FastAPI runs
    these `def` handlers in the anyio worker threadpool, so two concurrent cold
    renders inside ONE uvicorn worker share a pid and would therefore share a tmp
    path — the second `write_bytes` truncates the file the first is about to
    `os.replace` into place, publishing a half-written PNG under the correct
    identity ETag. A uuid4 per call makes every writer's tmp file its own.
    """
    return f"{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}"


def read_cached_png(path: Path) -> bytes | None:
    """The cached bytes, or None when there is nothing trustworthy to serve.

    A file that does not begin with the PNG signature, or that is shorter than
    the minimal complete PNG, is treated as a MISS rather than as a hit: the
    ETag is derived from the identity tuple, not from the bytes, so serving a
    truncated body once would let the client's next `If-None-Match` confirm it
    with a 304 forever. A miss re-renders and overwrites the file (tmp+rename).
    """
    try:
        if not path.is_file():
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) < MINIMUM_PNG_BYTES or not data.startswith(PNG_SIGNATURE):
        return None
    return data


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
