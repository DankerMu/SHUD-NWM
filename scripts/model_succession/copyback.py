"""Copy the new package directories from the shared store to the compute store.

Part of ``scripts/node22_model_succession.py`` (the entry point).  The package
directory of a new row's ``model_package_uri`` is copied to the same key under
the compute store: into a sibling temporary directory first, compared with the
source, then renamed, so a destination is either absent or complete.  File
modes are not preserved (the compute store does not support it).  Nothing that
exists is overwritten.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from pathlib import Path
from typing import Any

from packages.common.object_store import normalize_object_key
from scripts.model_succession.model import Inputs, Settings, StepFailure

COPIED = "copied"
ALREADY_PRESENT = "already_present"
WOULD_COPY = "would_copy"
DIFFERS = "differs"
_CHUNK = 1024 * 1024


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _tree(root: Path, *, what: str) -> dict[str, tuple[int, str]]:
    """Every regular file under ``root`` as ``relative name -> (bytes, sha256)``; anything else is refused."""

    if root.is_symlink() or not root.is_dir():
        raise StepFailure(f"Refused: {what} {root} is not a directory (or is a symlink).")
    files: dict[str, tuple[int, str]] = {}
    for directory, names, filenames in os.walk(root, followlinks=False):
        for name in (*names, *filenames):
            path = Path(directory) / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise StepFailure(
                    f"Refused: {what} {root} holds a symlink or a non-regular file: {path}. Nothing was copied "
                    "for this package."
                )
            if stat.S_ISREG(mode):
                files[path.relative_to(root).as_posix()] = (path.stat().st_size, _file_sha256(path))
    return files


def _difference(source: dict[str, tuple[int, str]], destination: dict[str, tuple[int, str]]) -> list[str]:
    return sorted(name for name in source.keys() | destination.keys() if source.get(name) != destination.get(name))


def package_paths(settings: Settings, row: dict[str, Any]) -> tuple[Path, Path]:
    """``(source under the shared store, destination under the compute store)`` of a row's package directory."""

    uri = str(row.get("model_package_uri") or "")
    try:
        key = normalize_object_key(uri, settings.object_store_prefix).strip("/")
    except ValueError as error:
        raise StepFailure(f"Refused: model_package_uri {uri!r} of {row.get('model_id')}: {error}") from error
    if not key or ".." in Path(key).parts:
        raise StepFailure(f"Refused: model_package_uri {uri!r} of {row.get('model_id')} names no package directory.")
    return settings.provider_store_root / key, settings.object_store_root / key


def _temporary(destination: Path) -> Path:
    return destination.with_name(f".{destination.name}.model-succession-copy")


def classify(settings: Settings, row: dict[str, Any]) -> dict[str, Any]:
    """What the copy of one package would do, without writing."""

    source, destination = package_paths(settings, row)
    files = _tree(source, what="the package source")
    record: dict[str, Any] = {
        "model_id": str(row["model_id"]),
        "source": str(source),
        "destination": str(destination),
        "file_count": len(files),
        "bytes": sum(size for size, _sha256 in files.values()),
    }
    if not os.path.lexists(destination):
        return {**record, "outcome": WOULD_COPY}
    different = _difference(files, _tree(destination, what="the existing package destination"))
    if different:
        return {**record, "outcome": DIFFERS, "different_files": different[:20], "different_total": len(different)}
    return {**record, "outcome": ALREADY_PRESENT}


def _copy(source: Path, temporary: Path) -> None:
    temporary.mkdir()
    for directory, names, filenames in os.walk(source, followlinks=False):
        target = temporary / Path(directory).relative_to(source)
        for name in names:
            (target / name).mkdir()
        for name in filenames:
            # Content only: neither mode nor times are carried over.
            shutil.copyfile(Path(directory) / name, target / name, follow_symlinks=False)


def copy_package(settings: Settings, row: dict[str, Any]) -> dict[str, Any]:
    record = classify(settings, row)
    if record["outcome"] == ALREADY_PRESENT:
        return record
    destination = Path(record["destination"])
    if record["outcome"] == DIFFERS:
        raise StepFailure(
            f"Refused: the package destination {destination} exists and differs from its source "
            f"{record['source']} in {record['different_total']} file(s), e.g. {record['different_files']}. "
            "Nothing was overwritten; move the destination away after checking where it came from."
        )
    source, temporary = Path(record["source"]), _temporary(destination)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(temporary):
            # What a killed run left behind.
            shutil.rmtree(temporary)
        try:
            _copy(source, temporary)
            different = _difference(
                _tree(source, what="the package source"), _tree(temporary, what="the copied package")
            )
            if different:
                raise StepFailure(
                    f"The copy of {source} differs from its source in {len(different)} file(s), e.g. "
                    f"{different[:20]}; the destination {destination} was not created."
                )
            os.rename(temporary, destination)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
    except OSError as error:
        raise StepFailure(f"Copying {source} to {destination} failed: {error}") from error
    return {**record, "outcome": COPIED}


def run(settings: Settings, inputs: Inputs) -> dict[str, Any]:
    """Copy every new package; the facts of ``step-copyback.json``."""

    return {"packages": [copy_package(settings, row) for row in inputs.new_rows.values()]}


def report(settings: Settings, inputs: Inputs) -> tuple[list[dict[str, Any]], list[str]]:
    """``(per package what an apply would do, what it would refuse)``; writes nothing."""

    packages: list[dict[str, Any]] = []
    refusals: list[str] = []
    for row in inputs.new_rows.values():
        try:
            record = classify(settings, row)
        except StepFailure as error:
            record = {"model_id": str(row["model_id"]), "outcome": "refused", "reason": str(error)}
        if record["outcome"] == DIFFERS:
            refusals.append(f"package of {record['model_id']} differs at {record['destination']}")
        elif record["outcome"] == "refused":
            refusals.append(record["reason"])
        packages.append(record)
    return packages, refusals
