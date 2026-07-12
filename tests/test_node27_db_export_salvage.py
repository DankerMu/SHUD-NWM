"""Unit tests for the node-27 DB-export salvage runner (issue #850)."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from scripts import node27_db_export_salvage as salvage

_ROOT = Path(__file__).resolve().parents[1]
_MANIFEST_SCHEMA_PATH = _ROOT / "schemas/salvage_manifest.schema.json"
_MANIFEST_EXAMPLE_PATH = _ROOT / "schemas/examples/salvage_manifest.example.json"
_RECEIPT_INPUT_SCHEMA_PATH = _ROOT / "schemas/archive_completeness_receipt.schema.json"
_RECEIPT_INPUT_EXAMPLE_PATH = _ROOT / "schemas/examples/archive_completeness_receipt.example.json"
_RUNNER_SOURCE_PATH = _ROOT / "scripts/node27_db_export_salvage.py"
_WRAPPER_PATH = _ROOT / "scripts/node27_db_export_salvage_once.sh"
_MIGRATION_MET_PATH = _ROOT / "db/migrations/000005_met.sql"
_MIGRATION_HYDRO_PATH = _ROOT / "db/migrations/000006_hydro.sql"

_NOW = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)
_DSN = "postgresql://user:secretpw@127.0.0.1:55432/nhms"


# === Fixture / stub helpers ===


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_receipt_input(path: Path, salvage_selectors: list[dict[str, Any]] | None = None) -> None:
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "generated_at": "2026-07-11T12:05:00Z",
        "coverage_bounds": {"start": "2026-05-28T00:00:00Z", "end": "2026-06-16T00:00:00Z"},
        "windows": [
            {
                "lane": "forcing",
                "subject": {"forcing_version_id": "forc-a"},
                "window": {"start": "2026-05-28T00:00:00Z", "end": "2026-06-16T00:00:00Z"},
                "coverage": "none",
                "verdict": "gap",
                "evidence": ["db rows present; no archive"],
            }
        ],
        "salvage_selectors": salvage_selectors
        if salvage_selectors is not None
        else [
            {
                "table": "met.forcing_station_timeseries",
                "identity": {"forcing_version_id": "forc-a"},
                "window": {"start": "2026-05-28T00:00:00Z", "end": "2026-06-16T00:00:00Z"},
            }
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _base_env(tmp_path: Path, *, override: dict[str, str | None] | None = None) -> dict[str, str]:
    receipt_input = tmp_path / "completeness-receipt.json"
    _write_receipt_input(receipt_input)
    env: dict[str, str] = {
        "DATABASE_URL": _DSN,
        "NHMS_ARCHIVE_ROOT": str(tmp_path / "archive"),
        "NHMS_ARCHIVE_COMPLETENESS_RECEIPT_PATH": str(receipt_input),
        "NODE27_DB_EXPORT_SALVAGE_RECEIPT_PATH": str(tmp_path / "salvage-receipt.json"),
        "NODE27_DB_EXPORT_SALVAGE_LOCK_PATH": str(tmp_path / "salvage.lock"),
        "NODE27_DB_EXPORT_SALVAGE_PER_TICK_BOUND": "8",
        "NODE27_DB_EXPORT_SALVAGE_ZSTD_LEVEL": "3",
        "NODE27_DB_EXPORT_SALVAGE_STATEMENT_TIMEOUT_MS": "300000",
        "NODE27_DB_EXPORT_SALVAGE_SOURCE_INSTANCE_ID": "node27-primary-pg15",
        "NODE27_DB_EXPORT_SALVAGE_MODE": "dry-run",
    }
    (tmp_path / "archive").mkdir(exist_ok=True)
    if override:
        for k, v in override.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
    return env


def _args(**overrides: object) -> argparse.Namespace:
    defaults = {"selectors": None}
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _stub_check_read_only(_dsn: str) -> str | None:
    return None


def _stub_check_writable(_dsn: str) -> str | None:
    return "role can INSERT into met.forcing_station_timeseries"


def _make_stub_copy_and_count(
    csv_by_key: dict[tuple[str, str], bytes] | None = None,
    row_count_by_key: dict[tuple[str, str], int] | None = None,
    fail_copy_for: set[tuple[str, str]] | None = None,
):
    csv_by_key = csv_by_key or {}
    row_count_by_key = row_count_by_key or {}
    fail_copy_for = fail_copy_for or set()

    def _key(table: str, selector: dict) -> tuple[str, str]:
        identity_col = "forcing_version_id" if table.startswith("met.") else "run_id"
        return table, str(selector["identity"][identity_col])

    def fake_row_count(dsn: str, table: str, selector: dict, timeout_ms: int) -> int:
        return row_count_by_key.get(_key(table, selector), 3)

    def fake_copy(dsn: str, table: str, columns, selector: dict, timeout_ms: int) -> bytes:
        key = _key(table, selector)
        if key in fail_copy_for:
            raise RuntimeError("simulated statement_timeout on COPY")
        return csv_by_key.get(key, b"forcing_version_id,station_id,variable,valid_time,value\n")

    def fake_compress(data: bytes, level: int) -> bytes:
        # Identity "compression" so tests can assert on stable byte content.
        return b"ZSTD:" + data

    return fake_row_count, fake_copy, fake_compress


# === Config parse fail-closed ===


@pytest.mark.parametrize(
    ("override", "match"),
    [
        ({"DATABASE_URL": None}, "DATABASE_URL"),
        ({"DATABASE_URL": ""}, "DATABASE_URL"),
        ({"NHMS_ARCHIVE_ROOT": None}, "NHMS_ARCHIVE_ROOT"),
        ({"NHMS_ARCHIVE_ROOT": ""}, "NHMS_ARCHIVE_ROOT"),
        ({"NHMS_ARCHIVE_COMPLETENESS_RECEIPT_PATH": None}, "COMPLETENESS_RECEIPT_PATH"),
        ({"NODE27_DB_EXPORT_SALVAGE_RECEIPT_PATH": None}, "RECEIPT_PATH"),
        ({"NODE27_DB_EXPORT_SALVAGE_LOCK_PATH": None}, "LOCK_PATH"),
        ({"NODE27_DB_EXPORT_SALVAGE_PER_TICK_BOUND": "0"}, "PER_TICK_BOUND"),
        ({"NODE27_DB_EXPORT_SALVAGE_PER_TICK_BOUND": ""}, "PER_TICK_BOUND"),
        ({"NODE27_DB_EXPORT_SALVAGE_PER_TICK_BOUND": "-1"}, "PER_TICK_BOUND"),
        ({"NODE27_DB_EXPORT_SALVAGE_PER_TICK_BOUND": "1.5"}, "PER_TICK_BOUND"),
        ({"NODE27_DB_EXPORT_SALVAGE_PER_TICK_BOUND": "abc"}, "PER_TICK_BOUND"),
        ({"NODE27_DB_EXPORT_SALVAGE_PER_TICK_BOUND": None}, "PER_TICK_BOUND"),
        ({"NODE27_DB_EXPORT_SALVAGE_ZSTD_LEVEL": "0"}, "ZSTD_LEVEL"),
        ({"NODE27_DB_EXPORT_SALVAGE_ZSTD_LEVEL": "23"}, "ZSTD_LEVEL"),
        ({"NODE27_DB_EXPORT_SALVAGE_ZSTD_LEVEL": "abc"}, "ZSTD_LEVEL"),
        ({"NODE27_DB_EXPORT_SALVAGE_STATEMENT_TIMEOUT_MS": "0"}, "STATEMENT_TIMEOUT_MS"),
        ({"NODE27_DB_EXPORT_SALVAGE_STATEMENT_TIMEOUT_MS": "999"}, "STATEMENT_TIMEOUT_MS"),
        ({"NODE27_DB_EXPORT_SALVAGE_STATEMENT_TIMEOUT_MS": "abc"}, "STATEMENT_TIMEOUT_MS"),
        ({"NODE27_DB_EXPORT_SALVAGE_SOURCE_INSTANCE_ID": None}, "SOURCE_INSTANCE_ID"),
        ({"NODE27_DB_EXPORT_SALVAGE_SOURCE_INSTANCE_ID": ""}, "SOURCE_INSTANCE_ID"),
        ({"NODE27_DB_EXPORT_SALVAGE_SOURCE_INSTANCE_ID": "  padded"}, "SOURCE_INSTANCE_ID"),
        ({"NODE27_DB_EXPORT_SALVAGE_MODE": "compress"}, "MODE"),
        ({"NHMS_ARCHIVE_ROOT": "relative/archive"}, "absolute"),
        ({"NODE27_DB_EXPORT_SALVAGE_RECEIPT_PATH": "relative.json"}, "absolute"),
        ({"NODE27_DB_EXPORT_SALVAGE_LOCK_PATH": "relative.lock"}, "absolute"),
        ({"NHMS_ARCHIVE_COMPLETENESS_RECEIPT_PATH": "relative.json"}, "absolute"),
    ],
)
def test_config_parse_fails_closed(
    tmp_path: Path, override: dict[str, str | None], match: str
) -> None:
    env = _base_env(tmp_path, override=override)
    with pytest.raises(salvage.SalvageConfigError, match=match):
        salvage.config_from_args(_args(), env)


def test_config_parse_happy_path(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    config = salvage.config_from_args(_args(), env)
    assert config.per_tick_bound == 8
    assert config.zstd_level == 3
    assert config.statement_timeout_ms == 300000
    assert config.mode == "dry-run"
    assert config.enforce is False


def test_config_defaults_for_optional_ints(tmp_path: Path) -> None:
    env = _base_env(
        tmp_path,
        override={
            "NODE27_DB_EXPORT_SALVAGE_ZSTD_LEVEL": None,
            "NODE27_DB_EXPORT_SALVAGE_STATEMENT_TIMEOUT_MS": None,
        },
    )
    config = salvage.config_from_args(_args(), env)
    assert config.zstd_level == 3
    assert config.statement_timeout_ms == 300_000


# === --selectors flag refusal ===


def test_refuse_hardcoded_selector_flag_config_level(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    with pytest.raises(salvage.SalvageConfigError, match="hardcoded --selectors"):
        salvage.config_from_args(_args(selectors="forc-a,run-b"), env)


def test_refuse_hardcoded_selector_flag_main_exits_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env = _base_env(tmp_path)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    receipt_out = Path(env["NODE27_DB_EXPORT_SALVAGE_RECEIPT_PATH"])
    existed = receipt_out.exists()
    code = salvage.main(argv=["--selectors", "forc-a"], now_utc=_NOW)
    assert code != 0
    diagnostic = json.loads(capsys.readouterr().err.strip())
    assert diagnostic["outcome"] == "refused_config"
    assert receipt_out.exists() == existed


# === Input receipt schema validation ===


@pytest.mark.parametrize(
    "mutator",
    [
        lambda bad: bad.unlink(),
        lambda bad: bad.write_text(
            json.dumps({k: v for k, v in json.loads(bad.read_text()).items() if k != "salvage_selectors"})
        ),
        lambda bad: bad.write_text(json.dumps({**json.loads(bad.read_text()), "surprise_key": "x"})),
        lambda bad: bad.write_text("not-a-json{{"),
    ],
    ids=["missing-file", "missing-salvage-selectors", "unknown-top-level-key", "invalid-json"],
)
def test_receipt_schema_validation_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mutator,
) -> None:
    env = _base_env(tmp_path)
    mutator(Path(env["NHMS_ARCHIVE_COMPLETENESS_RECEIPT_PATH"]))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    code = salvage.main(argv=[], now_utc=_NOW)
    assert code != 0
    diag = json.loads(capsys.readouterr().err.strip())
    assert diag["outcome"] == "refused_config"


def test_valid_receipt_loads_and_returns_dict(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    data = salvage._load_input_receipt(Path(env["NHMS_ARCHIVE_COMPLETENESS_RECEIPT_PATH"]))
    assert isinstance(data.get("salvage_selectors"), list)


# === Role write-privilege preflight ===


def test_role_write_privilege_refused_when_has_table_privilege_true(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env = _base_env(tmp_path, override={"NODE27_DB_EXPORT_SALVAGE_MODE": "enforce"})
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    receipt_out = Path(env["NODE27_DB_EXPORT_SALVAGE_RECEIPT_PATH"])

    code = salvage.main(
        argv=[],
        now_utc=_NOW,
        check_write_privileges=_stub_check_writable,
        fetch_row_count=lambda *a, **k: 1,
        perform_copy_export=lambda *a, **k: b"",
        compress_bytes=lambda data, level: data,
    )
    assert code != 0
    diag = json.loads(capsys.readouterr().err.strip())
    assert diag["outcome"] == "refused_role"
    # No receipt output on refuse
    assert not receipt_out.exists()


def test_role_write_privilege_refused_when_sentinel_insert_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env = _base_env(tmp_path, override={"NODE27_DB_EXPORT_SALVAGE_MODE": "enforce"})
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    def sentinel_leg(_dsn: str) -> str | None:
        return "role can INSERT into met.forcing_station_timeseries (sentinel INSERT succeeded)"

    code = salvage.main(
        argv=[],
        now_utc=_NOW,
        check_write_privileges=sentinel_leg,
        fetch_row_count=lambda *a, **k: 1,
        perform_copy_export=lambda *a, **k: b"",
        compress_bytes=lambda data, level: data,
    )
    assert code != 0
    diag = json.loads(capsys.readouterr().err.strip())
    assert diag["outcome"] == "refused_role"
    assert "secretpw" not in json.dumps(diag)


# === Dry-run + enforce receipts ===


def _run_build_receipt(
    tmp_path: Path,
    *,
    env_override: dict[str, str | None] | None = None,
    selectors: list[dict[str, Any]] | None = None,
    check_write_privileges=_stub_check_read_only,
    fetch_row_count=None,
    perform_copy_export=None,
    compress_bytes=None,
    input_receipt_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    env = _base_env(tmp_path, override=env_override)
    if selectors is not None:
        _write_receipt_input(
            Path(env["NHMS_ARCHIVE_COMPLETENESS_RECEIPT_PATH"]), salvage_selectors=selectors
        )
    config = salvage.config_from_args(_args(), env)
    input_receipt = input_receipt_override or salvage._load_input_receipt(
        Path(env["NHMS_ARCHIVE_COMPLETENESS_RECEIPT_PATH"])
    )
    if fetch_row_count is None or perform_copy_export is None or compress_bytes is None:
        default_row, default_copy, default_compress = _make_stub_copy_and_count()
        if fetch_row_count is None:
            fetch_row_count = default_row
        if perform_copy_export is None:
            perform_copy_export = default_copy
        if compress_bytes is None:
            compress_bytes = default_compress
    return salvage.build_receipt(
        config,
        now_utc=_NOW,
        input_receipt=input_receipt,
        check_write_privileges=check_write_privileges,
        fetch_row_count=fetch_row_count,
        perform_copy_export=perform_copy_export,
        compress_bytes=compress_bytes,
    )


def test_dry_run_writes_no_object(tmp_path: Path) -> None:
    receipt = _run_build_receipt(tmp_path)
    assert receipt["mode"] == "dry-run"
    assert receipt["outcome"] == "clean"
    (only,) = receipt["selected"]
    assert only["state"] == "skipped_dry_run"
    assert only["exported_row_count"] is None
    assert receipt["per_selector_totals"]["skipped_dry_run"] == 1
    # No object file on disk
    archive_root = tmp_path / "archive"
    csv_path = archive_root / "db-export/forcing/forc-a/data.csv.zst"
    manifest_path = archive_root / "db-export/forcing/forc-a/manifest.json"
    assert not csv_path.exists()
    assert not manifest_path.exists()


def test_dry_run_publishes_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _base_env(tmp_path)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    default_row, default_copy, default_compress = _make_stub_copy_and_count()
    code = salvage.main(
        argv=[],
        now_utc=_NOW,
        check_write_privileges=_stub_check_read_only,
        fetch_row_count=default_row,
        perform_copy_export=default_copy,
        compress_bytes=default_compress,
    )
    assert code == 0
    receipt_out = Path(env["NODE27_DB_EXPORT_SALVAGE_RECEIPT_PATH"])
    assert receipt_out.exists()
    payload = json.loads(receipt_out.read_text())
    assert payload["mode"] == "dry-run"
    assert payload["outcome"] == "clean"


def test_enforce_produces_object_and_manifest_and_row_count_matches_db(
    tmp_path: Path,
) -> None:
    csv_body = b"forcing_version_id,station_id,variable,valid_time,value\nforc-a,st1,q,2026-05-28T00:00:00Z,42.0\n"
    row_count = 1
    default_row, _c, default_compress = _make_stub_copy_and_count(
        row_count_by_key={("met.forcing_station_timeseries", "forc-a"): row_count},
    )

    def stub_copy(dsn, table, columns, selector, timeout_ms):
        return csv_body

    receipt = _run_build_receipt(
        tmp_path,
        env_override={"NODE27_DB_EXPORT_SALVAGE_MODE": "enforce"},
        fetch_row_count=default_row,
        perform_copy_export=stub_copy,
        compress_bytes=default_compress,
    )
    assert receipt["mode"] == "enforce"
    assert receipt["outcome"] == "clean"
    (only,) = receipt["selected"]
    assert only["state"] == "exported"
    assert only["exported_row_count"] == row_count

    archive_root = tmp_path / "archive"
    csv_path = archive_root / "db-export/forcing/forc-a/data.csv.zst"
    manifest_path = archive_root / "db-export/forcing/forc-a/manifest.json"
    assert csv_path.exists()
    assert manifest_path.exists()

    manifest = _load_json(manifest_path)
    schema = _load_json(_MANIFEST_SCHEMA_PATH)
    jsonschema.validate(manifest, schema)
    assert manifest["provenance"] == "db-export"
    export = manifest["exports"][0]
    assert export["exported_row_count"] == row_count
    assert export["columns"] == list(salvage._COLUMNS_FORCING)
    assert export["object"]["sha256"] == only["object"]["sha256"]
    # Sha256 of the compressed bytes matches on disk.
    disk_sha = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    assert disk_sha == export["object"]["sha256"]
    assert manifest["source_database"]["instance_id"] == "node27-primary-pg15"


# === Idempotency ===


def test_idempotent_skip_on_verified_existing_object(tmp_path: Path) -> None:
    """One selector already exported and verified; second selector is missing.

    Only the missing selector is exported. The existing pair is untouched.
    """
    selectors = [
        {
            "table": "met.forcing_station_timeseries",
            "identity": {"forcing_version_id": "forc-existing"},
            "window": {"start": "2026-05-28T00:00:00Z", "end": "2026-06-16T00:00:00Z"},
        },
        {
            "table": "met.forcing_station_timeseries",
            "identity": {"forcing_version_id": "forc-missing"},
            "window": {"start": "2026-05-28T00:00:00Z", "end": "2026-06-16T00:00:00Z"},
        },
    ]
    # Pre-populate the "existing" selector's object + manifest with matching
    # sha256 and DB row count.
    archive_root = tmp_path / "archive"
    existing_dir = archive_root / "db-export/forcing/forc-existing"
    existing_dir.mkdir(parents=True)
    existing_object_bytes = b"ZSTD:pre-existing-csv-bytes"
    existing_object_path = existing_dir / "data.csv.zst"
    existing_object_path.write_bytes(existing_object_bytes)
    existing_sha = hashlib.sha256(existing_object_bytes).hexdigest()
    existing_manifest = {
        "schema_version": "1.0",
        "provenance": "db-export",
        "generated_at": "2026-07-01T00:00:00Z",
        "source_database": {"database": "nhms", "instance_id": "node27-primary-pg15"},
        "exports": [
            {
                "selector": selectors[0],
                "exported_row_count": 5,
                "columns": list(salvage._COLUMNS_FORCING),
                "object": {
                    "path": "db-export/forcing/forc-existing/data.csv.zst",
                    "sha256": existing_sha,
                    "size_bytes": len(existing_object_bytes),
                },
            }
        ],
    }
    (existing_dir / "manifest.json").write_text(json.dumps(existing_manifest))
    # The pre-existing object was 100 mtime; record it so we can prove it
    # was not touched.
    pre_mtime = existing_object_path.stat().st_mtime_ns

    def fake_row_count(dsn, table, selector, timeout_ms):
        if str(selector["identity"].get("forcing_version_id")) == "forc-existing":
            return 5  # matches manifest
        return 2

    def fake_copy(dsn, table, columns, selector, timeout_ms):
        return (
            b"forcing_version_id,station_id,variable,valid_time,value\n"
            b"forc-missing,s1,q,2026-05-28T00:00:00Z,1.0\n"
            b"forc-missing,s2,q,2026-05-29T00:00:00Z,2.0\n"
        )

    def fake_compress(data: bytes, level: int) -> bytes:
        return b"ZSTD:" + data

    receipt = _run_build_receipt(
        tmp_path,
        env_override={"NODE27_DB_EXPORT_SALVAGE_MODE": "enforce"},
        selectors=selectors,
        fetch_row_count=fake_row_count,
        perform_copy_export=fake_copy,
        compress_bytes=fake_compress,
    )
    assert receipt["outcome"] == "clean"
    states = [d["state"] for d in receipt["selected"]]
    assert states == ["skipped_verified", "exported"]
    assert receipt["per_selector_totals"]["skipped_verified"] == 1
    assert receipt["per_selector_totals"]["exported"] == 1

    # Existing pair untouched.
    assert existing_object_path.stat().st_mtime_ns == pre_mtime
    # Missing pair produced.
    new_object = archive_root / "db-export/forcing/forc-missing/data.csv.zst"
    new_manifest = archive_root / "db-export/forcing/forc-missing/manifest.json"
    assert new_object.exists()
    assert new_manifest.exists()


# === Per-selector failure isolation ===


def test_per_selector_failure_isolated(tmp_path: Path) -> None:
    selectors = [
        {
            "table": "met.forcing_station_timeseries",
            "identity": {"forcing_version_id": "forc-ok"},
            "window": {"start": "2026-05-28T00:00:00Z", "end": "2026-06-16T00:00:00Z"},
        },
        {
            "table": "met.forcing_station_timeseries",
            "identity": {"forcing_version_id": "forc-fail"},
            "window": {"start": "2026-05-28T00:00:00Z", "end": "2026-06-16T00:00:00Z"},
        },
    ]

    def fake_row_count(dsn, table, selector, timeout_ms):
        return 4

    def fake_copy(dsn, table, columns, selector, timeout_ms):
        if str(selector["identity"]["forcing_version_id"]) == "forc-fail":
            raise RuntimeError("simulated statement_timeout on COPY")
        return b"forcing_version_id,station_id,variable,valid_time,value\n"

    def fake_compress(data, level):
        return b"ZSTD:" + data

    receipt = _run_build_receipt(
        tmp_path,
        env_override={"NODE27_DB_EXPORT_SALVAGE_MODE": "enforce"},
        selectors=selectors,
        fetch_row_count=fake_row_count,
        perform_copy_export=fake_copy,
        compress_bytes=fake_compress,
    )
    assert receipt["outcome"] == "partial"
    by_identity = {
        d["selector"]["identity"]["forcing_version_id"]: d for d in receipt["selected"]
    }
    assert by_identity["forc-ok"]["state"] == "exported"
    assert by_identity["forc-fail"]["state"] == "error"
    assert "simulated statement_timeout" in by_identity["forc-fail"]["error"]
    # per_selector_totals reflects only the successful set + error count
    totals = receipt["per_selector_totals"]
    assert totals["exported"] == 1
    assert totals["error"] == 1
    assert totals["row_count"] == 4  # only the successful one contributes


# === Safe relative path enforcement ===


@pytest.mark.parametrize(
    "malicious_identity",
    [
        "../evil",
        "..",
        ".",
        "a/b",
        "a\\b",
        "a\x00b",
        "",
        "/etc/passwd",
    ],
)
def test_safe_relative_path_refused_at_runtime(
    tmp_path: Path, malicious_identity: str
) -> None:
    selectors = [
        {
            "table": "met.forcing_station_timeseries",
            "identity": {"forcing_version_id": malicious_identity},
            "window": {"start": "2026-05-28T00:00:00Z", "end": "2026-06-16T00:00:00Z"},
        }
    ]
    # Skip the ones the JSON schema would already reject at load-time
    # (empty string). Load-time refusal is separately tested below.
    if malicious_identity == "":
        with pytest.raises(salvage.SalvageConfigError):
            _run_build_receipt(
                tmp_path,
                env_override={"NODE27_DB_EXPORT_SALVAGE_MODE": "enforce"},
                selectors=selectors,
            )
        return
    receipt = _run_build_receipt(
        tmp_path,
        env_override={"NODE27_DB_EXPORT_SALVAGE_MODE": "enforce"},
        selectors=selectors,
    )
    (only,) = receipt["selected"]
    assert only["state"] == "error"
    assert only["error"]  # non-empty refusal reason


def test_safe_relative_path_refused_at_manifest_schema_level() -> None:
    """A malicious path that could bypass runtime checks still fails at schema."""
    schema = _load_json(_MANIFEST_SCHEMA_PATH)
    example = _load_json(_MANIFEST_EXAMPLE_PATH)
    example["exports"][0]["object"]["path"] = "db-export/../evil/data.csv.zst"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(example, schema)


# === DSN masking ===


def test_mask_dsn_strips_credentials() -> None:
    masked = salvage._mask_dsn(_DSN)
    assert "secretpw" not in masked
    assert "user" not in masked
    assert "127.0.0.1" in masked


def test_dsn_never_leaks_via_exception_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env = _base_env(tmp_path, override={"NODE27_DB_EXPORT_SALVAGE_MODE": "enforce"})
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    def leaky_copy(dsn, table, columns, selector, timeout_ms):
        raise RuntimeError(f"error contacting {dsn}")

    default_row, _c, default_compress = _make_stub_copy_and_count()
    code = salvage.main(
        argv=[],
        now_utc=_NOW,
        check_write_privileges=_stub_check_read_only,
        fetch_row_count=default_row,
        perform_copy_export=leaky_copy,
        compress_bytes=default_compress,
    )
    # Per-selector error masked in the receipt (not stderr — receipt gets published)
    receipt_out = Path(env["NODE27_DB_EXPORT_SALVAGE_RECEIPT_PATH"])
    assert receipt_out.exists()
    receipt = json.loads(receipt_out.read_text())
    assert receipt["outcome"] == "partial"
    error_msg = receipt["selected"][0]["error"]
    assert "secretpw" not in error_msg
    # partial → non-zero exit
    assert code != 0
    err = capsys.readouterr().err
    assert "secretpw" not in err


# === Lock contention ===


def test_lock_contention_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env = _base_env(tmp_path)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    lock_path = Path(env["NODE27_DB_EXPORT_SALVAGE_LOCK_PATH"])
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    receipt_out = Path(env["NODE27_DB_EXPORT_SALVAGE_RECEIPT_PATH"])
    existed = receipt_out.exists()
    try:
        code = salvage.main(argv=[], now_utc=_NOW)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert code != 0
    diag = json.loads(capsys.readouterr().err.strip())
    assert diag["outcome"] == "refused_lock"
    # Receipt not touched
    assert receipt_out.exists() == existed


# === Migration invariants + column constants ===


def _extract_ddl_columns(text: str, table_name: str) -> tuple[str, ...]:
    # Isolate the CREATE TABLE ... table_name ( ... ); block and grab first
    # token per column-definition line (skip PRIMARY KEY / FOREIGN KEY and
    # any multi-line constraint continuations such as "REFERENCES ...").
    match = re.search(
        rf"CREATE TABLE IF NOT EXISTS {re.escape(table_name)}\s*\(([^;]+)\);",
        text,
        flags=re.DOTALL,
    )
    assert match, f"could not find CREATE TABLE {table_name} in migration"
    body = match.group(1)
    columns: list[str] = []
    skip_continuation = False
    for raw_line in body.splitlines():
        line = raw_line.strip().rstrip(",")
        if not line:
            continue
        upper = line.upper()
        if upper.startswith("PRIMARY KEY") or upper.startswith("FOREIGN KEY") or upper.startswith("UNIQUE"):
            # A FOREIGN KEY constraint may wrap onto continuation lines
            # ("    REFERENCES ..."); swallow them by tracking whether the
            # constraint line closed with its own ")".
            skip_continuation = ")" not in line
            continue
        if skip_continuation:
            if ")" in line:
                skip_continuation = False
            continue
        first = line.split()[0]
        # DDL column identifiers are lowercase snake_case; reject anything
        # that looks like a keyword continuation.
        if not re.match(r"^[a-z][a-z0-9_]*$", first):
            continue
        columns.append(first)
    return tuple(columns)


def test_column_constants_match_migration_ddl() -> None:
    forcing = _extract_ddl_columns(
        _MIGRATION_MET_PATH.read_text(encoding="utf-8"), "met.forcing_station_timeseries"
    )
    river = _extract_ddl_columns(
        _MIGRATION_HYDRO_PATH.read_text(encoding="utf-8"), "hydro.river_timeseries"
    )
    assert forcing == salvage._COLUMNS_FORCING
    assert river == salvage._COLUMNS_RIVER
    # Non-empty tuples
    assert len(salvage._COLUMNS_FORCING) >= 1
    assert len(salvage._COLUMNS_RIVER) >= 1


def test_migration_has_no_ddl_in_runner() -> None:
    """The runner runs zero DDL / transactional statements outside sentinel-INSERT preflight."""
    source = _RUNNER_SOURCE_PATH.read_text(encoding="utf-8")
    # Filter out sentinel-INSERT preflight block: it legally uses ROLLBACK to
    # discard a probe INSERT. Strip _sentinel_insert_check function.
    filtered_lines = []
    inside_sentinel = False
    for line in source.splitlines():
        if line.startswith("def _sentinel_insert_check"):
            inside_sentinel = True
            continue
        if inside_sentinel:
            if line.startswith("def ") and not line.startswith("def _sentinel_insert_check"):
                inside_sentinel = False
            else:
                continue
        filtered_lines.append(line)
    filtered = "\n".join(filtered_lines)
    forbidden = re.search(
        r"\bALTER\s+TABLE\b|\bCREATE\s+TABLE\b|\bDROP\s+TABLE\b|\bTRUNCATE\b|\bBEGIN\b|\bCOMMIT\b|\bROLLBACK\b|\bSAVEPOINT\b",
        filtered,
        flags=re.IGNORECASE,
    )
    assert forbidden is None, (
        f"runner source outside sentinel probe block contains forbidden DDL/TXN token: "
        f"{forbidden.group(0) if forbidden else ''!r}"
    )


# === Negative jsonschema — manifest ===


def _example_manifest() -> dict[str, Any]:
    return _load_json(_MANIFEST_EXAMPLE_PATH)


def test_example_salvage_manifest_still_validates() -> None:
    jsonschema.validate(_example_manifest(), _load_json(_MANIFEST_SCHEMA_PATH))


def test_manifest_schema_rejects_missing_provenance() -> None:
    manifest = _example_manifest()
    del manifest["provenance"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(manifest, _load_json(_MANIFEST_SCHEMA_PATH))


def test_manifest_schema_rejects_wrong_provenance() -> None:
    manifest = _example_manifest()
    manifest["provenance"] = "product-archive"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(manifest, _load_json(_MANIFEST_SCHEMA_PATH))


def test_manifest_schema_rejects_missing_sha256() -> None:
    manifest = _example_manifest()
    del manifest["exports"][0]["object"]["sha256"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(manifest, _load_json(_MANIFEST_SCHEMA_PATH))


def test_manifest_schema_rejects_unknown_top_level_key() -> None:
    manifest = _example_manifest()
    manifest["surprise"] = "x"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(manifest, _load_json(_MANIFEST_SCHEMA_PATH))


def test_manifest_schema_rejects_bad_path_shape() -> None:
    manifest = _example_manifest()
    manifest["exports"][0]["object"]["path"] = "runs/forc-a/data.csv.zst"  # wrong lane prefix
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(manifest, _load_json(_MANIFEST_SCHEMA_PATH))


# === Negative jsonschema — receipt input ===


def test_receipt_input_schema_rejects_missing_salvage_selectors() -> None:
    receipt = _load_json(_RECEIPT_INPUT_EXAMPLE_PATH)
    del receipt["salvage_selectors"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(receipt, _load_json(_RECEIPT_INPUT_SCHEMA_PATH))


def test_receipt_input_schema_rejects_unknown_top_level_key() -> None:
    receipt = _load_json(_RECEIPT_INPUT_EXAMPLE_PATH)
    receipt["surprise"] = "x"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(receipt, _load_json(_RECEIPT_INPUT_SCHEMA_PATH))


# === ADR 0001 display carve-out ===


def test_display_carve_out() -> None:
    # Grep confirmation that neither apps/api nor apps/frontend references the
    # salvage runner or its env vars.
    result = subprocess.run(
        [
            "grep",
            "-rn",
            "-E",
            r"db_export_salvage|NODE27_DB_EXPORT_SALVAGE|db-export/",
            str(_ROOT / "apps/api"),
            str(_ROOT / "apps/frontend"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0 or result.stdout == "", (
        f"display carve-out violated. grep output:\n{result.stdout}"
    )


# === Wrapper shell contract (6-case parametrized) ===


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    [
        ("relative-wrapper-path", "wrapper paths must be absolute"),
        ("env-mode", "env file must have mode 0600"),
        ("env-symlink", "env file must be a regular non-symlink file"),
        ("missing-python", "python executable is unavailable"),
        ("missing-script", "salvage entrypoint is unavailable or a symlink"),
        ("symlink-script", "salvage entrypoint is unavailable or a symlink"),
    ],
)
def test_wrapper_shell_contract(
    tmp_path: Path, case: str, expected_reason: str
) -> None:
    wrapper = _WRAPPER_PATH
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stat_shim = bin_dir / "stat"
    stat_shim.write_text(
        "#!/bin/sh\n"
        "for last do :; done\n"
        "case \"$last\" in\n"
        "  *bad-mode.env) printf '644\\n' ;;\n"
        "  *) printf '600\\n' ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    stat_shim.chmod(0o700)

    python_bin = tmp_path / "python"
    python_bin.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    python_bin.chmod(0o700)
    entrypoint = tmp_path / "salvage.py"
    entrypoint.write_text("raise SystemExit(99)\n", encoding="utf-8")

    env_file = tmp_path / ("bad-mode.env" if case == "env-mode" else "runner.env")
    env_file.write_text("", encoding="utf-8")
    env_file.chmod(0o600)
    if case == "env-symlink":
        target = tmp_path / "real.env"
        env_file.rename(target)
        env_file.symlink_to(target)

    configured_python = str(python_bin)
    if case == "missing-python":
        configured_python = str(tmp_path / "missing-python")

    configured_script = str(entrypoint)
    if case == "missing-script":
        configured_script = str(tmp_path / "missing-script.py")
    elif case == "symlink-script":
        script_link = tmp_path / "salvage-link.py"
        script_link.symlink_to(entrypoint)
        configured_script = str(script_link)

    process_env = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "NODE27_DB_EXPORT_SALVAGE_ENV_FILE": (
            "relative.env" if case == "relative-wrapper-path" else str(env_file)
        ),
        "NODE27_DB_EXPORT_SALVAGE_PYTHON": configured_python,
        "NODE27_DB_EXPORT_SALVAGE_SCRIPT": configured_script,
    }
    result = subprocess.run(
        ["/bin/sh", str(wrapper)],
        env=process_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    failure = json.loads(result.stderr.strip())
    assert failure == {"status": "failed", "reason": expected_reason}


# === Per-tick bound ===


def test_per_tick_bound_slices_selectors(tmp_path: Path) -> None:
    selectors = [
        {
            "table": "met.forcing_station_timeseries",
            "identity": {"forcing_version_id": f"forc-{i:03d}"},
            "window": {"start": "2026-05-28T00:00:00Z", "end": "2026-06-16T00:00:00Z"},
        }
        for i in range(5)
    ]
    receipt = _run_build_receipt(
        tmp_path,
        env_override={"NODE27_DB_EXPORT_SALVAGE_PER_TICK_BOUND": "2"},
        selectors=selectors,
    )
    # Only 2 selectors processed
    assert len(receipt["selected"]) == 2
    assert all(d["state"] == "skipped_dry_run" for d in receipt["selected"])
