import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
# #1370: archive-lane schemas retired with the lane itself. The retention receipt schema survives.
SCHEMA_BASES = ("timeseries_retention_receipt",)


def _validator() -> str:
    validator = shutil.which("check-jsonschema")
    if validator is not None:
        return validator
    # #2615: the venv interpreter run by path (node-27 worktree lane) does not put
    # its own bin on PATH. No `.resolve()`: that would follow the venv's python
    # symlink into the uv-managed base interpreter's bin.
    beside_interpreter = Path(sys.executable).parent / "check-jsonschema"
    if beside_interpreter.is_file() and os.access(beside_interpreter, os.X_OK):
        return str(beside_interpreter)
    raise RuntimeError(
        f"check-jsonschema is required; looked on PATH and at {beside_interpreter}; run `uv sync --all-extras --dev`"
    )


# #2615: `_validator()` itself. The node-27 worktree lane runs the venv's python
# by path, so its `bin` may be missing from PATH; these pin the fallback.
def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _off_path(monkeypatch: pytest.MonkeyPatch, interpreter: Path) -> None:
    monkeypatch.setattr(shutil, "which", lambda *args, **kwargs: None)
    monkeypatch.setattr(sys, "executable", str(interpreter))


def test_validator_prefers_path_hit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    on_path = _executable(tmp_path / "path-bin" / "check-jsonschema")
    _executable(tmp_path / "bin" / "check-jsonschema")
    monkeypatch.setattr(
        shutil, "which", lambda name, *args, **kwargs: str(on_path) if name == "check-jsonschema" else None
    )
    monkeypatch.setattr(sys, "executable", str(_executable(tmp_path / "bin" / "python")))

    assert _validator() == str(on_path)


def test_validator_falls_back_beside_interpreter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    beside = _executable(tmp_path / "bin" / "check-jsonschema")
    _off_path(monkeypatch, _executable(tmp_path / "bin" / "python"))

    assert _validator() == str(beside)


def test_validator_fallback_does_not_follow_the_interpreter_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A uv venv's python is a symlink into the managed base interpreter, whose
    # bin has no check-jsonschema; the fallback must stay in the venv's bin.
    base_python = _executable(tmp_path / "base" / "bin" / "python")
    venv_python = tmp_path / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(base_python)
    beside = _executable(tmp_path / "venv" / "bin" / "check-jsonschema")
    _off_path(monkeypatch, venv_python)

    assert _validator() == str(beside)


@pytest.mark.parametrize("state", ["missing", "not_executable"])
def test_validator_not_found_names_both_locations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: str
) -> None:
    beside = tmp_path / "bin" / "check-jsonschema"
    _off_path(monkeypatch, _executable(tmp_path / "bin" / "python"))
    if state == "not_executable":
        beside.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        beside.chmod(0o644)

    with pytest.raises(RuntimeError) as excinfo:
        _validator()

    message = str(excinfo.value)
    assert "PATH" in message
    assert str(beside) in message


def _document(base: str) -> dict[str, Any]:
    path = ROOT / "schemas" / "examples" / f"{base}.example.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_document(tmp_path: Path, base: str, document: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    candidate = tmp_path / f"{base}.json"
    candidate.write_text(json.dumps(document), encoding="utf-8")
    return subprocess.run(
        [
            _validator(),
            "--schemafile",
            str(ROOT / "schemas" / f"{base}.schema.json"),
            str(candidate),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("base", SCHEMA_BASES)
def test_timeseries_storage_schema_and_example_are_valid(tmp_path: Path, base: str) -> None:
    schema = ROOT / "schemas" / f"{base}.schema.json"
    metaschema = subprocess.run(
        [_validator(), "--check-metaschema", str(schema)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert metaschema.returncode == 0, metaschema.stdout + metaschema.stderr

    result = _validate_document(tmp_path, base, _document(base))
    assert result.returncode == 0, result.stdout + result.stderr


# #1369: schema 1.1 made ``archive_gate`` a required top-level object. These
# hand-written documents carry it (mode ``enabled`` = the fail-closed default,
# which must NOT cite the ADR) so each row still fails/passes for the reason it
# was written to test, not because it is stuck on the pre-bump shape.
_RETENTION_GATE_ENABLED = {"mode": "enabled"}


def test_retention_refusal_requires_reason(tmp_path: Path) -> None:
    document = {
        "schema_version": "1.1",
        "generated_at": "2026-07-11T12:30:00Z",
        "mode": "enforce",
        "outcome": "refused",
        "archive_gate": _RETENTION_GATE_ENABLED,
    }

    result = _validate_document(tmp_path, "timeseries_retention_receipt", document)
    assert result.returncode != 0


def test_retention_refusal_with_reason_is_valid(tmp_path: Path) -> None:
    document = {
        "schema_version": "1.1",
        "generated_at": "2026-07-11T12:30:00Z",
        "mode": "enforce",
        "outcome": "refused",
        "archive_gate": _RETENTION_GATE_ENABLED,
        "refusal_reason": "archive completeness receipt is stale",
    }

    result = _validate_document(tmp_path, "timeseries_retention_receipt", document)
    assert result.returncode == 0, result.stdout + result.stderr


def test_retention_dry_run_with_candidates_is_valid(tmp_path: Path) -> None:
    document = {
        "schema_version": "1.1",
        "generated_at": "2026-07-11T12:30:00Z",
        "mode": "dry-run",
        "outcome": "dry-run",
        "archive_gate": _RETENTION_GATE_ENABLED,
        "candidate_chunks": ["_hyper_1_42_chunk"],
        "deferred_remainder": ["_hyper_1_43_chunk"],
    }

    result = _validate_document(tmp_path, "timeseries_retention_receipt", document)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("invalid_variant", ["missing-candidates", "carries-dropped-chunks"])
def test_retention_dry_run_rejects_invalid_outcome_details(tmp_path: Path, invalid_variant: str) -> None:
    document = {
        "schema_version": "1.1",
        "generated_at": "2026-07-11T12:30:00Z",
        "mode": "dry-run",
        "outcome": "dry-run",
        "archive_gate": _RETENTION_GATE_ENABLED,
        "candidate_chunks": ["_hyper_1_42_chunk"],
        "deferred_remainder": [],
    }
    if invalid_variant == "missing-candidates":
        del document["candidate_chunks"]
    else:
        document["dropped_chunks"] = [{"name": "_hyper_1_42_chunk", "freed_bytes": 0}]

    result = _validate_document(tmp_path, "timeseries_retention_receipt", document)
    assert result.returncode != 0


@pytest.mark.parametrize(
    "missing",
    [
        "dropped_chunks",
        "dropped_chunks.0.name",
        "dropped_chunks.0.freed_bytes",
        "deferred_remainder",
        "salvage_backed_windows",
    ],
)
def test_retention_enforce_requires_outcome_details(tmp_path: Path, missing: str) -> None:
    document = copy.deepcopy(_document("timeseries_retention_receipt"))
    if missing.startswith("dropped_chunks.0."):
        del document["dropped_chunks"][0][missing.rsplit(".", maxsplit=1)[1]]
    else:
        del document[missing]

    result = _validate_document(tmp_path, "timeseries_retention_receipt", document)
    assert result.returncode != 0


def test_display_api_has_no_archive_resolver_dependency() -> None:
    """ADR 0001 display carve-out. #1370 deleted these four resolvers from
    `packages/common/storage.py` along with the archive lane, so this now also
    pins that they are never reintroduced on the display side.
    """
    display_sources = (ROOT / "apps" / "api").rglob("*.py")
    forbidden = (
        "resolve_archive_root",
        "resolve_archive_storage_config",
        "archive_provenance_paths",
        "archive_identity_for_state_reference",
    )

    for source in display_sources:
        content = source.read_text(encoding="utf-8")
        assert all(symbol not in content for symbol in forbidden), source


