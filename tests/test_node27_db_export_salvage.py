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
        "schema_version": "1.1",
        "generated_at": "2026-07-11T12:05:00Z",
        "outcome": "incomplete",
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


def _make_fake_zstd_binary(tmp_path: Path) -> Path:
    """Create an executable regular non-symlink stub that passes `_validate_zstd_path`."""
    zstd_bin = tmp_path / "fake-zstd"
    zstd_bin.write_text("#!/bin/sh\ncat\n", encoding="utf-8")
    zstd_bin.chmod(0o700)
    return zstd_bin


def _base_env(tmp_path: Path, *, override: dict[str, str | None] | None = None) -> dict[str, str]:
    receipt_input = tmp_path / "completeness-receipt.json"
    _write_receipt_input(receipt_input)
    zstd_bin = _make_fake_zstd_binary(tmp_path)
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
        "NODE27_DB_EXPORT_SALVAGE_ZSTD": str(zstd_bin),
        "NODE27_DB_EXPORT_SALVAGE_MAX_SELECTOR_BYTES": str(2 * 1024 * 1024 * 1024),
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

    def fake_compress(data: bytes, level: int, zstd_path: Path) -> bytes:
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
        # Upper ceiling on MAX_SELECTOR_BYTES: an operator typo (extra trailing
        # zero) must not effectively disable the cap. 999999999999999 bytes
        # (~909 TiB) is well above the 16 GiB ceiling.
        (
            {"NODE27_DB_EXPORT_SALVAGE_MAX_SELECTOR_BYTES": "999999999999999"},
            "MAX_SELECTOR_BYTES",
        ),
        # Off-by-one at the 16 GiB ceiling: exactly one byte over must be
        # refused. Pairs with test_max_selector_bytes_boundary_accepts_ceiling
        # below, which proves the inclusive-ceiling value is accepted.
        (
            {"NODE27_DB_EXPORT_SALVAGE_MAX_SELECTOR_BYTES": "17179869185"},
            "MAX_SELECTOR_BYTES",
        ),
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
        compress_bytes=lambda data, level, zstd_path: data,
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
        compress_bytes=lambda data, level, zstd_path: data,
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

    def fake_compress(data: bytes, level: int, zstd_path: Path) -> bytes:
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

    ok_csv = (
        b"forcing_version_id,station_id,variable,valid_time,value\n"
        b"forc-ok,s1,q,2026-05-28T00:00:00Z,1.0\n"
        b"forc-ok,s1,q,2026-05-29T00:00:00Z,2.0\n"
        b"forc-ok,s1,q,2026-05-30T00:00:00Z,3.0\n"
        b"forc-ok,s1,q,2026-05-31T00:00:00Z,4.0\n"
    )

    def fake_copy(dsn, table, columns, selector, timeout_ms):
        if str(selector["identity"]["forcing_version_id"]) == "forc-fail":
            raise RuntimeError("simulated statement_timeout on COPY")
        return ok_csv

    def fake_compress(data, level, zstd_path):
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
    # per_selector_totals reflects only the successful set + error count.
    # row_count is derived from the CSV bytes (newlines minus header), so
    # the ok_csv above with four data lines contributes exactly 4.
    totals = receipt["per_selector_totals"]
    assert totals["exported"] == 1
    assert totals["error"] == 1
    assert totals["row_count"] == 4


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


def test_keyword_dsn_mask_and_source_database_are_allowlist_rebuilt() -> None:
    dsn = "host=db.example.test port=55432 user=reader password='keyword secret' dbname=nhms"
    masked = salvage._mask_dsn(dsn)
    assert masked == "postgresql://***@db.example.test:55432/nhms"
    assert salvage._source_database_from_dsn(dsn) == "nhms"
    assert dsn not in masked and "keyword secret" not in masked


@pytest.mark.parametrize(
    "dsn",
    [
        "host=db user=reader password=secret dbname='unsafe/path'",
        "host='/tmp/postgres socket' user=reader password=secret dbname=nhms",
        "opaque-dsn-secret",
    ],
)
def test_dsn_mask_never_reemits_unvalidated_database_or_host(dsn: str) -> None:
    assert salvage._mask_dsn(dsn) == "postgresql://***@***/***"
    assert salvage._source_database_from_dsn(dsn) == "unknown"


def test_keyword_dsn_success_receipt_contains_only_safe_database_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dsn = "password='keyword secret' dbname=nhms host=db.example.test user=reader port=55432"
    env = _base_env(tmp_path, override={"DATABASE_URL": dsn})
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    row_count, copy_export, compress = _make_stub_copy_and_count()
    assert (
        salvage.main(
            argv=[],
            now_utc=_NOW,
            check_write_privileges=_stub_check_read_only,
            fetch_row_count=row_count,
            perform_copy_export=copy_export,
            compress_bytes=compress,
        )
        == 0
    )
    receipt = Path(env["NODE27_DB_EXPORT_SALVAGE_RECEIPT_PATH"]).read_text(encoding="utf-8")
    payload = json.loads(receipt)
    assert payload["source_database"]["database"] == "nhms"
    assert dsn not in receipt and "keyword secret" not in receipt


def test_opaque_dsn_success_receipt_uses_safe_database_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dsn = "opaque-dsn-secret"
    env = _base_env(tmp_path, override={"DATABASE_URL": dsn})
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    row_count, copy_export, compress = _make_stub_copy_and_count()
    assert (
        salvage.main(
            argv=[],
            now_utc=_NOW,
            check_write_privileges=_stub_check_read_only,
            fetch_row_count=row_count,
            perform_copy_export=copy_export,
            compress_bytes=compress,
        )
        == 0
    )
    receipt = Path(env["NODE27_DB_EXPORT_SALVAGE_RECEIPT_PATH"]).read_text(encoding="utf-8")
    assert json.loads(receipt)["source_database"]["database"] == "unknown"
    assert dsn not in receipt


def test_keyword_dsn_refused_stderr_masks_reordered_echo_and_bare_password(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dsn = "host=db user=reader password='keyword secret' dbname=nhms"
    reordered = "dbname=nhms password='keyword secret' host=db user=reader"
    env = _base_env(tmp_path, override={"DATABASE_URL": dsn})
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert (
        salvage.main(
            argv=[],
            now_utc=_NOW,
            check_write_privileges=lambda _dsn: (
                f"role probe echoed {reordered}; bare=keyword secret"
            ),
            fetch_row_count=lambda *_args, **_kwargs: 0,
            perform_copy_export=lambda *_args, **_kwargs: b"",
            compress_bytes=lambda data, _level, _zstd_path: data,
        )
        == 1
    )
    stderr = capsys.readouterr().err
    assert json.loads(stderr)["outcome"] == "refused_role"
    assert dsn not in stderr and reordered not in stderr and "keyword secret" not in stderr


def test_quoted_credential_keys_are_masked_in_helper_and_refused_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    diagnostic = (
        '{"auth_\\u0068eader": "Bearer helper-secret", "safe": "visible"} '
        'payload="password=helper-fragment-secret"'
    )
    masked = salvage._mask_dsn_in_message(diagnostic, _DSN)
    assert "helper-secret" not in masked and "helper-fragment-secret" not in masked
    assert "visible" in masked

    env = _base_env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert (
        salvage.main(
            argv=[],
            now_utc=_NOW,
            check_write_privileges=lambda _dsn: (
                "role {'\\u0061uth': 'Basic refused-secret', 'safe': 'visible'} "
                "source='token=refused-fragment-secret'"
            ),
            fetch_row_count=lambda *_args, **_kwargs: 0,
            perform_copy_export=lambda *_args, **_kwargs: b"",
            compress_bytes=lambda data, _level, _zstd_path: data,
        )
        == 1
    )
    stderr = capsys.readouterr().err
    assert json.loads(stderr)["outcome"] == "refused_role"
    assert "refused-secret" not in stderr and "refused-fragment-secret" not in stderr
    assert "visible" in stderr


def test_quoted_credential_keys_are_masked_in_selector_receipt_and_runner_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env = _base_env(
        tmp_path,
        override={"NODE27_DB_EXPORT_SALVAGE_MODE": "enforce"},
    )
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    def fail_copy(*_args: object, **_kwargs: object) -> bytes:
        raise RuntimeError(
            'selector {"前缀authorization": "Bearer selector-secret", "safe": "visible"} '
            'payload="api_key=selector-fragment-secret"'
        )

    assert (
        salvage.main(
            argv=[],
            now_utc=_NOW,
            check_write_privileges=_stub_check_read_only,
            fetch_row_count=lambda *_args, **_kwargs: 0,
            perform_copy_export=fail_copy,
            compress_bytes=lambda data, _level, _zstd_path: data,
        )
        == 1
    )
    receipt = Path(env["NODE27_DB_EXPORT_SALVAGE_RECEIPT_PATH"]).read_text(encoding="utf-8")
    assert json.loads(receipt)["selected"][0]["state"] == "error"
    assert "selector-secret" not in receipt and "selector-fragment-secret" not in receipt
    assert "visible" in receipt

    monkeypatch.setattr(
        salvage,
        "build_receipt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError(
                "runner {'proxy_\\u0061uthorization': 'Bearer runner-secret', 'safe': 'visible'} "
                'source="password=runner-fragment-secret"'
            )
        ),
    )
    assert salvage.main(argv=[], now_utc=_NOW) == 1
    stderr = capsys.readouterr().err
    assert json.loads(stderr)["outcome"] == "partial"
    assert "runner-secret" not in stderr and "runner-fragment-secret" not in stderr
    assert "visible" in stderr


def test_unicode_escaped_credential_key_is_masked_from_salvage_publication_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env = _base_env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    row_count, copy_export, compress = _make_stub_copy_and_count()
    monkeypatch.setattr(
        salvage,
        "publish_receipt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            salvage.SafeFilesystemError(
                'publisher {"auth_\\u0068eader": "Basic publication-secret", '
                '"safe": "visible"} payload="token=publication-fragment-secret"'
            )
        ),
    )
    assert (
        salvage.main(
            argv=[],
            now_utc=_NOW,
            check_write_privileges=_stub_check_read_only,
            fetch_row_count=row_count,
            perform_copy_export=copy_export,
            compress_bytes=compress,
        )
        == 1
    )
    stderr = capsys.readouterr().err
    assert json.loads(stderr)["outcome"] == "partial"
    assert "publication-secret" not in stderr and "publication-fragment-secret" not in stderr
    assert "visible" in stderr


@pytest.mark.parametrize("quoted", [False, True])
@pytest.mark.parametrize("backslash_count", [1, 2, 3])
def test_mask_dsn_message_redacts_raw_escaped_password_body(
    quoted: bool, backslash_count: int
) -> None:
    raw_password = "raw-secret" + "\\" * backslash_count + "tail"
    lexical = f"'{raw_password}'" if quoted else raw_password
    dsn = f"host=db user=reader password={lexical} dbname=nhms"
    masked = salvage._mask_dsn_in_message(f"driver raw={raw_password}", dsn)
    assert raw_password not in masked


def test_raw_escaped_password_is_masked_in_refused_stderr_and_selector_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw_password = "raw-secret" + "\\" * 3 + "tail"
    dsn = f"host=db user=reader password='{raw_password}' dbname=nhms"
    env = _base_env(tmp_path, override={"DATABASE_URL": dsn})
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert (
        salvage.main(
            argv=[],
            now_utc=_NOW,
            check_write_privileges=lambda _dsn: f"role probe raw={raw_password}",
            fetch_row_count=lambda *_args, **_kwargs: 0,
            perform_copy_export=lambda *_args, **_kwargs: b"",
            compress_bytes=lambda data, _level, _zstd_path: data,
        )
        == 1
    )
    stderr = capsys.readouterr().err
    assert raw_password not in stderr and dsn not in stderr

    for key, value in _base_env(
        tmp_path,
        override={"DATABASE_URL": dsn, "NODE27_DB_EXPORT_SALVAGE_MODE": "enforce"},
    ).items():
        monkeypatch.setenv(key, value)

    def fail_copy(*_args: object, **_kwargs: object) -> bytes:
        raise RuntimeError(f"selector runner raw={raw_password}")

    assert (
        salvage.main(
            argv=[],
            now_utc=_NOW,
            check_write_privileges=_stub_check_read_only,
            fetch_row_count=lambda *_args, **_kwargs: 0,
            perform_copy_export=fail_copy,
            compress_bytes=lambda data, _level, _zstd_path: data,
        )
        == 1
    )
    receipt_path = Path(os.environ["NODE27_DB_EXPORT_SALVAGE_RECEIPT_PATH"])
    receipt = receipt_path.read_text(encoding="utf-8")
    assert raw_password not in receipt and dsn not in receipt
    assert json.loads(receipt)["selected"][0]["state"] == "error"


def test_overlapping_password_candidates_are_masked_in_helper_stderr_and_selector_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dsn = "host=db password=s3c password=s3cLONG dbname=nhms"
    assert "LONG" not in salvage._mask_dsn_in_message("driver raw=s3cLONG", dsn)
    env = _base_env(tmp_path, override={"DATABASE_URL": dsn})
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert (
        salvage.main(
            argv=[],
            now_utc=_NOW,
            check_write_privileges=lambda _dsn: "role raw=s3cLONG",
            fetch_row_count=lambda *_args, **_kwargs: 0,
            perform_copy_export=lambda *_args, **_kwargs: b"",
            compress_bytes=lambda data, _level, _zstd_path: data,
        )
        == 1
    )
    stderr = capsys.readouterr().err
    assert "s3c" not in stderr and "LONG" not in stderr

    enforce_env = _base_env(
        tmp_path,
        override={"DATABASE_URL": dsn, "NODE27_DB_EXPORT_SALVAGE_MODE": "enforce"},
    )
    for key, value in enforce_env.items():
        monkeypatch.setenv(key, value)

    def fail_copy(*_args: object, **_kwargs: object) -> bytes:
        raise RuntimeError("selector raw=s3cLONG")

    assert (
        salvage.main(
            argv=[],
            now_utc=_NOW,
            check_write_privileges=_stub_check_read_only,
            fetch_row_count=lambda *_args, **_kwargs: 0,
            perform_copy_export=fail_copy,
            compress_bytes=lambda data, _level, _zstd_path: data,
        )
        == 1
    )
    receipt = Path(enforce_env["NODE27_DB_EXPORT_SALVAGE_RECEIPT_PATH"]).read_text(
        encoding="utf-8"
    )
    assert "s3c" not in receipt and "LONG" not in receipt


@pytest.mark.parametrize(
    "malformed_dsn",
    [
        "postgresql://user:secret@db.example.test:not-a-port/nhms",
        "postgresql://user:secret@db.example.test:99999/nhms",
        "postgresql://user:secret@[::1/nhms",
        "postgresql://user:secret@[not-ipv6]/nhms",
    ],
)
def test_mask_dsn_is_total_and_fail_closed_for_malformed_urls(malformed_dsn: str) -> None:
    masked = salvage._mask_dsn(malformed_dsn)
    assert masked == "postgresql://***@***/***"
    assert "secret" not in masked


def test_mask_dsn_in_message_is_total_for_malformed_remote_url() -> None:
    malformed = "https://user:url-secret@example.test:not-a-port/path?token=query"
    masked = salvage._mask_dsn_in_message(f"remote failed: {malformed}", _DSN)
    assert "url-secret" not in masked
    assert "token=query" not in masked


def test_main_diagnostic_masks_malformed_config_dsn_and_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    malformed_dsn = "postgresql://user:dsn-secret@db.example.test:not-a-port/nhms"
    malformed_remote = "https://user:url-secret@example.test:99999/path?token=query"
    env = _base_env(tmp_path, override={"DATABASE_URL": malformed_dsn})
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    code = salvage.main(
        argv=[],
        now_utc=_NOW,
        check_write_privileges=lambda _dsn: (
            f"role probe failed at {malformed_remote}; password=probe-secret"
        ),
        fetch_row_count=lambda *_args, **_kwargs: 0,
        perform_copy_export=lambda *_args, **_kwargs: b"",
        compress_bytes=lambda data, _level, _zstd_path: data,
    )
    assert code == 1
    diagnostic = capsys.readouterr().err
    payload = json.loads(diagnostic)
    assert payload["outcome"] == "refused_role"
    assert payload["dsn"] == "postgresql://***@***/***"
    for secret in ("dsn-secret", "url-secret", "token=query", "probe-secret"):
        assert secret not in diagnostic


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
    # Per-selector error masked in the receipt (not stderr — receipt gets published).
    # With a single selector failing the outcome is now ``all_failed`` (cand-I)
    # rather than ``partial``; both are non-clean and produce a non-zero exit.
    receipt_out = Path(env["NODE27_DB_EXPORT_SALVAGE_RECEIPT_PATH"])
    assert receipt_out.exists()
    receipt = json.loads(receipt_out.read_text())
    assert receipt["outcome"] == "all_failed"
    error_msg = receipt["selected"][0]["error"]
    assert "secretpw" not in error_msg
    # non-clean → non-zero exit
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
    """Balance-parse the CREATE TABLE ... table_name ( ... ) body.

    The old `[^;]+` regex broke on any semicolon-containing comment inside
    the DDL. We now find the opening ``(`` of the table body and walk the
    stream with a depth counter until we hit the matching ``)`` — this is
    exact rather than heuristic (cand-N).
    """
    head_match = re.search(
        rf"CREATE TABLE IF NOT EXISTS {re.escape(table_name)}\s*\(",
        text,
    )
    assert head_match, f"could not find CREATE TABLE {table_name} in migration"
    start = head_match.end()  # points immediately AFTER the opening '('
    depth = 1
    i = start
    while i < len(text):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                break
        i += 1
    assert depth == 0, f"unbalanced parentheses in CREATE TABLE {table_name}"
    body = text[start:i]
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
    """The runner runs zero DDL / transactional statements outside sentinel-INSERT preflight.

    Uses ``ast.parse`` to locate ``_sentinel_insert_check`` by line-span so
    the sentinel block is excluded exactly (source-slicing on
    ``line.startswith("def ")`` would break if a nested/decorated def
    appeared, or if the function was moved). This is more robust than the
    previous grep (cand-K).
    """
    import ast

    source = _RUNNER_SOURCE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    exclude_ranges: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_sentinel_insert_check":
            start = node.lineno  # 1-based, inclusive
            end = node.end_lineno or node.lineno
            exclude_ranges.append((start, end))
    assert exclude_ranges, "expected to find _sentinel_insert_check in runner source"
    lines = source.splitlines()
    kept: list[str] = []
    for idx, line in enumerate(lines, start=1):
        if any(start <= idx <= end for (start, end) in exclude_ranges):
            continue
        kept.append(line)
    filtered = "\n".join(kept)
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


@pytest.mark.parametrize(
    "terminal",
    [
        {
            "schema_version": "1.1",
            "generated_at": "2026-07-11T12:05:00Z",
            "outcome": "blocked",
            "refusal_reason": "EVIDENCE_BLOCKED",
        },
        {
            "schema_version": "1.1",
            "generated_at": "2026-07-11T12:05:00Z",
            "outcome": "indeterminate",
            "error_reason": "UNEXPECTED_AUDIT_ERROR",
        },
    ],
)
def test_terminal_receipt_refuses_before_any_db_read_or_archive_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal: dict[str, Any],
) -> None:
    env = _base_env(tmp_path)
    input_path = Path(env["NHMS_ARCHIVE_COMPLETENESS_RECEIPT_PATH"])
    input_path.write_text(json.dumps(terminal), encoding="utf-8")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    calls: list[str] = []

    def called(*_args: object, **_kwargs: object) -> int:
        calls.append("db-or-write")
        return 0

    code = salvage.main(
        argv=[],
        now_utc=_NOW,
        check_write_privileges=called,
        fetch_row_count=called,
        perform_copy_export=called,
        compress_bytes=called,
    )
    assert code == 1
    assert calls == []
    assert not Path(env["NODE27_DB_EXPORT_SALVAGE_RECEIPT_PATH"]).exists()


# === ADR 0001 display carve-out ===


def test_display_carve_out() -> None:
    """Neither apps/api nor apps/frontend references the salvage runner or its env vars.

    Guards against silent-pass on renamed / missing target dirs: assert they
    exist first, and constrain grep return code to ``{0, 1}`` (2 = grep
    itself failed and should be surfaced as a test failure, cand-L).
    """
    api_dir = _ROOT / "apps/api"
    frontend_dir = _ROOT / "apps/frontend"
    assert api_dir.is_dir(), f"apps/api target missing: {api_dir}"
    assert frontend_dir.is_dir(), f"apps/frontend target missing: {frontend_dir}"
    result = subprocess.run(
        [
            "grep",
            "-rn",
            "-E",
            r"db_export_salvage|NODE27_DB_EXPORT_SALVAGE|db-export/",
            str(api_dir),
            str(frontend_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode in {0, 1}, (
        f"grep failed unexpectedly (rc={result.returncode}): stderr={result.stderr!r}"
    )
    assert result.returncode == 1 and result.stdout == "", (
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


# === cand-A: exported_row_count is byte-derived from the CSV, not a
# separate DB round-trip whose MVCC snapshot can drift under concurrent
# writes.


def test_manifest_exported_row_count_matches_csv_bytes(tmp_path: Path) -> None:
    """Reg regression: manifest.exports[0].exported_row_count must equal
    ``csv_bytes.count(b"\\n") - 1``, so a stale ``SELECT COUNT(*)`` on a
    second connection can never disagree with the shipped object.
    """
    csv_body = (
        b"forcing_version_id,basin_version_id,station_id,valid_time,source_id,"
        b"variable,value,unit,native_resolution,quality_flag\n"
        b"forc-a,bv1,s1,2026-05-28T00:00:00Z,src,q,1.0,mm,PT1H,0\n"
        b"forc-a,bv1,s1,2026-05-29T00:00:00Z,src,q,2.0,mm,PT1H,0\n"
        b"forc-a,bv1,s1,2026-05-30T00:00:00Z,src,q,3.0,mm,PT1H,0\n"
    )
    expected_row_count = csv_body.count(b"\n") - 1

    def stub_copy(dsn, table, columns, selector, timeout_ms):
        return csv_body

    # Intentionally have fetch_row_count LIE about the DB count — cand-A's
    # bug shape was "second connection disagrees with the shipped bytes".
    # The manifest must ignore this drift and report the byte-derived count.
    lying_row_count_calls = {"n": 0}

    def lying_row_count(dsn, table, selector, timeout_ms):
        lying_row_count_calls["n"] += 1
        return 999999

    receipt = _run_build_receipt(
        tmp_path,
        env_override={"NODE27_DB_EXPORT_SALVAGE_MODE": "enforce"},
        fetch_row_count=lying_row_count,
        perform_copy_export=stub_copy,
    )
    (descriptor,) = receipt["selected"]
    assert descriptor["state"] == "exported"
    assert descriptor["exported_row_count"] == expected_row_count

    manifest_path = (
        tmp_path / "archive" / "db-export/forcing/forc-a/manifest.json"
    )
    manifest = _load_json(manifest_path)
    assert manifest["exports"][0]["exported_row_count"] == expected_row_count
    # And explicitly not the lying DB count.
    assert manifest["exports"][0]["exported_row_count"] != 999999


def test_count_csv_rows_edge_cases() -> None:
    """Byte-derivation helper handles empty / header-only / no-trailing-newline."""
    assert salvage._count_csv_rows(b"") == 0
    # Header only (one newline) -> 0 data rows
    assert salvage._count_csv_rows(b"a,b,c\n") == 0
    # Header + 1 data row
    assert salvage._count_csv_rows(b"a,b,c\n1,2,3\n") == 1
    # Header + 2 data rows
    assert salvage._count_csv_rows(b"a,b,c\n1,2,3\n4,5,6\n") == 2
    # Defensive: no trailing newline on the last row (COPY always emits
    # one, but the helper must not underflow).
    assert salvage._count_csv_rows(b"a,b,c") == 0


# === cand-B: DSN mask completeness ===


@pytest.mark.parametrize(
    ("dsn", "message_template", "must_not_contain", "sample_hostname"),
    [
        # URL-encoded password with %-encoding echoed
        (
            "postgresql://user:s%40cret%23pw@127.0.0.1:55432/nhms",
            "boom: {dsn} (network)",
            ["s%40cret%23pw"],
            "127.0.0.1",
        ),
        # URL-encoded password with the DECODED form echoed
        (
            "postgresql://user:s%40cret%23pw@127.0.0.1:55432/nhms",
            "psql: FATAL:  password authentication failed for user 'user' pw=s@cret#pw",
            ["s@cret#pw"],
            "user",  # username may still appear (design choice; not a secret)
        ),
        # libpq keyword form (with password= verbatim)
        (
            "postgresql://user:secretpw@127.0.0.1:55432/nhms",
            "connection refused: host=127.0.0.1 port=55432 user=user password=secretpw dbname=nhms",
            ["secretpw"],
            "127.0.0.1",
        ),
        # libpq quoted-value form: password embedded in single quotes,
        # value contains an embedded space (a valid libpq DSN shape a
        # driver may echo). The banned substrings are chosen to
        # DISCRIMINATE between the old ``\S+``-only regex (which stops at
        # whitespace, producing tail-leak ``... password=*** space pw'
        # dbname=nhms``) and the new ``('[^']*'|\S+)`` alternation (which
        # consumes the full quoted value, producing ``... password=***
        # dbname=nhms``). Asserting ``"has space pw" not in masked`` was a
        # fake oracle because the substring ``has space pw`` never appears
        # verbatim in either branch's output; the fragments ``space pw``
        # and ``pw'`` DO appear in the old-regex output and are proof of a
        # tail leak.
        (
            "postgresql://user:secretpw@127.0.0.1:55432/nhms",
            "connection refused: host=127.0.0.1 port=55432 user=user password='has space pw' dbname=nhms",
            ["space pw", "pw'"],
            "127.0.0.1",
        ),
    ],
    ids=[
        "url-encoded-echoed",
        "url-decoded-echoed",
        "libpq-keyword-form",
        "libpq-quoted-value-form",
    ],
)
def test_mask_dsn_in_message_scrubs_all_password_shapes(
    dsn: str,
    message_template: str,
    must_not_contain: list[str],
    sample_hostname: str,
) -> None:
    message = message_template.format(dsn=dsn)
    masked = salvage._mask_dsn_in_message(message, dsn)
    for banned in must_not_contain:
        assert banned not in masked, (
            f"password shape {banned!r} leaked in {masked!r}"
        )
    # Hostname or username is NOT considered secret and stays visible for
    # diagnostics. The test proves the design intent explicitly.
    assert sample_hostname in masked or "***" in masked


def test_mask_dsn_in_message_leaves_hostname_and_username() -> None:
    """Design contract: hostname + username are diagnostic, not secret."""
    dsn = "postgresql://alice:s%40cret@db.example.internal:55432/nhms"
    message = "psql: could not connect to db.example.internal as alice"
    masked = salvage._mask_dsn_in_message(message, dsn)
    assert "db.example.internal" in masked
    assert "alice" in masked


def test_mask_dsn_in_message_scrubs_libpq_password_even_without_matching_dsn() -> None:
    """If the message carries a keyword-form password= that came from
    elsewhere (e.g. a driver echoing a caller-provided libpq DSN), scrub
    it defensively regardless of whether the runner's DSN matches.
    """
    dsn = "postgresql://user:secretpw@127.0.0.1:55432/nhms"
    unrelated_message = "sub-driver reported: password=someOtherThing failed"
    masked = salvage._mask_dsn_in_message(unrelated_message, dsn)
    assert "someOtherThing" not in masked
    assert "password=***" in masked


# === cand-C: MemoryError must not be silently classified as per-selector
# error; the byte cap gate refuses selectors whose CSV exceeds the bound.


def test_memory_error_is_not_swallowed_by_broad_except(tmp_path: Path) -> None:
    """A MemoryError inside the COPY/compress path must produce a
    distinct diagnostic — never a generic 'partial' with masked message.
    """
    def stub_copy(dsn, table, columns, selector, timeout_ms):
        raise MemoryError("simulated OOM inside copy_expert")

    receipt = _run_build_receipt(
        tmp_path,
        env_override={"NODE27_DB_EXPORT_SALVAGE_MODE": "enforce"},
        perform_copy_export=stub_copy,
    )
    (descriptor,) = receipt["selected"]
    assert descriptor["state"] == "error"
    # Distinct label — not the generic exception str().
    assert "out-of-memory" in descriptor["error"]
    assert "MAX_SELECTOR_BYTES" in descriptor["error"]
    # Totals bookkeeping must remain locked.
    assert receipt["per_selector_totals"]["error"] == 1
    assert receipt["per_selector_totals"]["exported"] == 0


def test_per_selector_bytes_cap_refuses(tmp_path: Path) -> None:
    """When CSV size > cap, refuse the selector (never write the object)."""
    big_csv = b"a,b,c\n" + b"1,2,3\n" * 100

    def stub_copy(dsn, table, columns, selector, timeout_ms):
        return big_csv

    receipt = _run_build_receipt(
        tmp_path,
        env_override={
            "NODE27_DB_EXPORT_SALVAGE_MODE": "enforce",
            "NODE27_DB_EXPORT_SALVAGE_MAX_SELECTOR_BYTES": str(len(big_csv) - 1),
        },
        perform_copy_export=stub_copy,
    )
    (descriptor,) = receipt["selected"]
    assert descriptor["state"] == "error"
    assert "exceeds cap" in descriptor["error"]
    assert descriptor["object"] is None
    # No object hit disk.
    csv_path = tmp_path / "archive" / "db-export/forcing/forc-a/data.csv.zst"
    assert not csv_path.exists()


# === cand-D: full receipt shape pinned by a positive example ===


def test_receipt_positive_shape_pins_all_keys(tmp_path: Path) -> None:
    receipt = _run_build_receipt(tmp_path)
    expected_keys = {
        "schema_version",
        "tool_version",
        "generated_at",
        "mode",
        "outcome",
        "source_database",
        "receipt_input_path",
        "selected",
        "per_selector_totals",
    }
    assert set(receipt.keys()) == expected_keys
    assert receipt["schema_version"] == salvage.SCHEMA_VERSION == "1.0"
    assert isinstance(receipt["tool_version"], str) and receipt["tool_version"]
    assert receipt["tool_version"] == salvage.TOOL_VERSION
    assert isinstance(receipt["generated_at"], str) and receipt["generated_at"].endswith("Z")
    assert receipt["mode"] in {"dry-run", "enforce"}
    assert receipt["outcome"] in {"clean", "partial", "all_failed"}
    assert set(receipt["source_database"].keys()) == {"database", "instance_id"}
    assert receipt["source_database"]["database"] == "nhms"
    assert receipt["source_database"]["instance_id"] == "node27-primary-pg15"
    # receipt_input_path echoes the configured input path
    env = _base_env(tmp_path)
    assert receipt["receipt_input_path"] == env["NHMS_ARCHIVE_COMPLETENESS_RECEIPT_PATH"]


# === cand-E: universal per_selector_totals accounting invariant ===


def _reached_exported(descriptor: dict[str, Any]) -> bool:
    return descriptor.get("state") == "exported"


def _expected_totals_from_descriptors(descriptors: list[dict[str, Any]]) -> dict[str, int]:
    exported = sum(1 for d in descriptors if d["state"] == "exported")
    skipped_verified = sum(1 for d in descriptors if d["state"] == "skipped_verified")
    skipped_dry_run = sum(1 for d in descriptors if d["state"] == "skipped_dry_run")
    error = sum(1 for d in descriptors if d["state"] == "error")
    row_count = sum(
        int(d["exported_row_count"]) for d in descriptors if _reached_exported(d)
    )
    compressed_bytes = sum(
        int(d["object"]["size_bytes"]) for d in descriptors if _reached_exported(d)
    )
    return {
        "exported": exported,
        "skipped_verified": skipped_verified,
        "skipped_dry_run": skipped_dry_run,
        "error": error,
        "row_count": row_count,
        "compressed_bytes": compressed_bytes,
    }


@pytest.mark.parametrize(
    "scenario",
    [
        "dry_run_only",
        "per_selector_copy_fail",
        "path_derivation_error",
        "unknown_lane",
        "idempotency_skip",
        "valid_enforce",
        "mixed_all_states",
        "all_failed",
    ],
)
def test_partial_receipt_totals_stay_locked_across_all_selector_paths(
    tmp_path: Path, scenario: str
) -> None:
    """Universal invariant: ``exported + skipped_verified + skipped_dry_run + error
    == len(selectors_processed)`` AND per-key totals are the sum of the
    matching descriptor field, derived from the descriptor stream itself.
    """
    body_row = (
        b"forcing_version_id,basin_version_id,station_id,valid_time,source_id,"
        b"variable,value,unit,native_resolution,quality_flag\n"
        b"forc-a,bv,s,2026-05-28T00:00:00Z,src,q,1.0,mm,PT1H,0\n"
    )
    ok_selector = {
        "table": "met.forcing_station_timeseries",
        "identity": {"forcing_version_id": "forc-a"},
        "window": {"start": "2026-05-28T00:00:00Z", "end": "2026-06-16T00:00:00Z"},
    }
    env_override: dict[str, str | None] = {}
    selectors: list[dict[str, Any]] = []
    if scenario == "dry_run_only":
        selectors = [ok_selector]
    elif scenario == "per_selector_copy_fail":
        env_override["NODE27_DB_EXPORT_SALVAGE_MODE"] = "enforce"
        selectors = [ok_selector]
    elif scenario == "path_derivation_error":
        env_override["NODE27_DB_EXPORT_SALVAGE_MODE"] = "enforce"
        selectors = [
            {
                "table": "met.forcing_station_timeseries",
                "identity": {"forcing_version_id": "../evil"},
                "window": {"start": "2026-05-28T00:00:00Z", "end": "2026-06-16T00:00:00Z"},
            }
        ]
    elif scenario == "unknown_lane":
        # The completeness-receipt schema itself rejects unknown tables, so
        # unknown-lane can only be reached by an in-memory
        # ``input_receipt_override`` that bypasses schema validation. This
        # exercises the runner's internal ``table not in _TABLE_TO_LANE``
        # guard.
        env_override["NODE27_DB_EXPORT_SALVAGE_MODE"] = "enforce"
        selectors = []  # unused; override supplies selectors directly
    elif scenario == "idempotency_skip":
        env_override["NODE27_DB_EXPORT_SALVAGE_MODE"] = "enforce"
        selectors = [ok_selector]
        _prepare_verified_existing_pair(tmp_path, ok_selector, body_row)
    elif scenario == "valid_enforce":
        env_override["NODE27_DB_EXPORT_SALVAGE_MODE"] = "enforce"
        selectors = [ok_selector]
    elif scenario == "mixed_all_states":
        env_override["NODE27_DB_EXPORT_SALVAGE_MODE"] = "enforce"
        existing_selector = {
            "table": "met.forcing_station_timeseries",
            "identity": {"forcing_version_id": "forc-existing"},
            "window": {"start": "2026-05-28T00:00:00Z", "end": "2026-06-16T00:00:00Z"},
        }
        fail_selector = {
            "table": "met.forcing_station_timeseries",
            "identity": {"forcing_version_id": "forc-fail"},
            "window": {"start": "2026-05-28T00:00:00Z", "end": "2026-06-16T00:00:00Z"},
        }
        selectors = [ok_selector, existing_selector, fail_selector]
        _prepare_verified_existing_pair(tmp_path, existing_selector, body_row)
    elif scenario == "all_failed":
        env_override["NODE27_DB_EXPORT_SALVAGE_MODE"] = "enforce"
        selectors = [
            {
                "table": "met.forcing_station_timeseries",
                "identity": {"forcing_version_id": "forc-a"},
                "window": {"start": "2026-05-28T00:00:00Z", "end": "2026-06-16T00:00:00Z"},
            },
            {
                "table": "met.forcing_station_timeseries",
                "identity": {"forcing_version_id": "forc-b"},
                "window": {"start": "2026-05-28T00:00:00Z", "end": "2026-06-16T00:00:00Z"},
            },
        ]

    def stub_copy(dsn, table, columns, selector, timeout_ms):
        # In "per_selector_copy_fail" always fail; in "mixed_all_states"
        # fail only forc-fail; in "all_failed" always fail; else return body.
        if scenario == "per_selector_copy_fail":
            raise RuntimeError("simulated fault")
        if scenario == "all_failed":
            raise RuntimeError("all failed")
        if scenario == "mixed_all_states" and selector["identity"].get(
            "forcing_version_id"
        ) == "forc-fail":
            raise RuntimeError("simulated fault")
        return body_row

    def stub_row_count(dsn, table, selector, timeout_ms):
        # For idempotency probe: existing manifest has row_count = 1 (one data
        # row in body_row). Return 1 so verified path is taken.
        return 1

    input_receipt_override: dict[str, Any] | None = None
    if scenario == "unknown_lane":
        input_receipt_override = {
            "salvage_selectors": [
                {
                    "table": "met.unknown_table",
                    "identity": {"forcing_version_id": "forc-x"},
                    "window": {
                        "start": "2026-05-28T00:00:00Z",
                        "end": "2026-06-16T00:00:00Z",
                    },
                }
            ]
        }

    receipt = _run_build_receipt(
        tmp_path,
        env_override=env_override,
        selectors=selectors if selectors else None,
        fetch_row_count=stub_row_count,
        perform_copy_export=stub_copy,
        input_receipt_override=input_receipt_override,
    )

    descriptors = receipt["selected"]
    totals = receipt["per_selector_totals"]
    expected = _expected_totals_from_descriptors(descriptors)

    # Universal invariant #1: state counts sum to processed count.
    assert (
        totals["exported"]
        + totals["skipped_verified"]
        + totals["skipped_dry_run"]
        + totals["error"]
        == len(descriptors)
    ), f"state totals must sum to descriptor count in {scenario}"

    # Universal invariant #2: every totals key matches the descriptor-derived
    # expected value.
    for key, want in expected.items():
        assert totals[key] == want, (
            f"totals[{key!r}] mismatch in {scenario}: got {totals[key]}, want {want}"
        )

    # Universal invariant #3: outcome enum is derived from the totals
    # themselves (not the scenario name), so any future selector arm that
    # produces the same failure/success mix will pin the same outcome.
    #
    # NOTE: the ``MemoryError`` and ``SalvageOversizeError`` arms are
    # intentionally NOT in this parametrize list because they exercise the
    # ``perform_copy_export`` path and land inside a per-selector descriptor
    # (state="error") — same shape as ``per_selector_copy_fail`` and
    # ``all_failed`` here. Dedicated tests
    # ``test_memory_error_is_not_swallowed_by_broad_except`` and
    # ``test_per_selector_bytes_cap_refuses`` cover their distinct error
    # messages; the totals invariant they'd add here is already exercised
    # by the failure arms above.
    any_success = (
        totals["exported"] > 0
        or totals["skipped_verified"] > 0
        or totals["skipped_dry_run"] > 0
    )
    any_error = totals["error"] > 0
    if any_error and not any_success:
        assert receipt["outcome"] == "all_failed"
    elif any_error and any_success:
        assert receipt["outcome"] == "partial"
    else:
        assert receipt["outcome"] == "clean"


def _prepare_verified_existing_pair(
    tmp_path: Path, selector: dict[str, Any], body_row: bytes
) -> None:
    """Create an on-disk manifest + object that idempotency verifies."""
    identity = str(
        selector["identity"].get("forcing_version_id")
        or selector["identity"].get("run_id")
    )
    lane = "forcing" if selector["table"].startswith("met.") else "runs"
    existing_dir = tmp_path / "archive" / f"db-export/{lane}/{identity}"
    existing_dir.mkdir(parents=True, exist_ok=True)
    compressed_bytes = b"ZSTD:" + body_row  # matches test _make_stub_copy_and_count
    existing_object_path = existing_dir / "data.csv.zst"
    existing_object_path.write_bytes(compressed_bytes)
    existing_sha = hashlib.sha256(compressed_bytes).hexdigest()
    row_count = body_row.count(b"\n") - 1
    manifest = {
        "schema_version": "1.0",
        "provenance": "db-export",
        "generated_at": "2026-07-01T00:00:00Z",
        "source_database": {"database": "nhms", "instance_id": "node27-primary-pg15"},
        "exports": [
            {
                "selector": selector,
                "exported_row_count": row_count,
                "columns": list(
                    salvage._COLUMNS_FORCING
                    if selector["table"].startswith("met.")
                    else salvage._COLUMNS_RIVER
                ),
                "object": {
                    "path": f"db-export/{lane}/{identity}/data.csv.zst",
                    "sha256": existing_sha,
                    "size_bytes": len(compressed_bytes),
                },
            }
        ],
    }
    (existing_dir / "manifest.json").write_text(json.dumps(manifest))


# === cand-F: idempotency multi-gate coverage ===


@pytest.mark.parametrize(
    "case",
    [
        "sha256_mismatch",
        "size_bytes_mismatch",
        "row_count_mismatch",
        "schema_invalid_manifest",
        "exports_length_2",
        "selector_drift",
    ],
)
def test_idempotency_gate_negative_cases(tmp_path: Path, case: str) -> None:
    """Any single-property mismatch in the existing pair must cause the
    idempotency check to reject the verified skip. state must NOT be
    ``skipped_verified``.
    """
    selector = {
        "table": "met.forcing_station_timeseries",
        "identity": {"forcing_version_id": "forc-a"},
        "window": {"start": "2026-05-28T00:00:00Z", "end": "2026-06-16T00:00:00Z"},
    }
    body_row = (
        b"forcing_version_id,basin_version_id,station_id,valid_time,source_id,"
        b"variable,value,unit,native_resolution,quality_flag\n"
        b"forc-a,bv,s,2026-05-28T00:00:00Z,src,q,1.0,mm,PT1H,0\n"
    )
    identity = "forc-a"
    existing_dir = tmp_path / "archive" / f"db-export/forcing/{identity}"
    existing_dir.mkdir(parents=True)
    truthful_compressed = b"ZSTD:" + body_row
    existing_object_path = existing_dir / "data.csv.zst"
    truthful_sha = hashlib.sha256(truthful_compressed).hexdigest()
    truthful_size = len(truthful_compressed)
    truthful_row_count = body_row.count(b"\n") - 1

    # Baseline manifest — mutated per case.
    manifest: dict[str, Any] = {
        "schema_version": "1.0",
        "provenance": "db-export",
        "generated_at": "2026-07-01T00:00:00Z",
        "source_database": {"database": "nhms", "instance_id": "node27-primary-pg15"},
        "exports": [
            {
                "selector": selector,
                "exported_row_count": truthful_row_count,
                "columns": list(salvage._COLUMNS_FORCING),
                "object": {
                    "path": f"db-export/forcing/{identity}/data.csv.zst",
                    "sha256": truthful_sha,
                    "size_bytes": truthful_size,
                },
            }
        ],
    }
    existing_object_path.write_bytes(truthful_compressed)
    row_count_stub_returns = truthful_row_count

    if case == "sha256_mismatch":
        manifest["exports"][0]["object"]["sha256"] = "0" * 64
    elif case == "size_bytes_mismatch":
        manifest["exports"][0]["object"]["size_bytes"] = truthful_size + 1
    elif case == "row_count_mismatch":
        # Manifest claims one row; DB says a different number.
        row_count_stub_returns = truthful_row_count + 5
    elif case == "schema_invalid_manifest":
        # provenance must be "db-export" — replace with something invalid.
        manifest["provenance"] = "product-archive"
    elif case == "exports_length_2":
        # Duplicate the export entry so len != 1.
        manifest["exports"].append(dict(manifest["exports"][0]))
    elif case == "selector_drift":
        drifted = dict(selector)
        drifted["window"] = {
            "start": "2000-01-01T00:00:00Z",
            "end": "2000-01-02T00:00:00Z",
        }
        manifest["exports"][0]["selector"] = drifted

    (existing_dir / "manifest.json").write_text(json.dumps(manifest))

    def stub_row_count(dsn, table, selector, timeout_ms):
        return row_count_stub_returns

    receipt = _run_build_receipt(
        tmp_path,
        env_override={"NODE27_DB_EXPORT_SALVAGE_MODE": "enforce"},
        selectors=[selector],
        fetch_row_count=stub_row_count,
        perform_copy_export=lambda *a, **k: body_row,
    )
    (descriptor,) = receipt["selected"]
    assert descriptor["state"] != "skipped_verified", (
        f"idempotency gate must reject case {case!r} — got skipped_verified"
    )
    # And the selector must re-run (either exported or error), never
    # silently claimed clean.
    assert descriptor["state"] in {"exported", "error"}


# === cand-I: outcome enum extended with all_failed ===


def test_outcome_all_failed_when_no_success_and_any_error(tmp_path: Path) -> None:
    """`any_errors and not any_success` must yield ``outcome="all_failed"``,
    distinct from the mixed ``partial`` case.
    """
    selectors = [
        {
            "table": "met.forcing_station_timeseries",
            "identity": {"forcing_version_id": f"forc-{i}"},
            "window": {"start": "2026-05-28T00:00:00Z", "end": "2026-06-16T00:00:00Z"},
        }
        for i in range(2)
    ]

    def failing_copy(dsn, table, columns, selector, timeout_ms):
        raise RuntimeError("simulated fault")

    receipt = _run_build_receipt(
        tmp_path,
        env_override={"NODE27_DB_EXPORT_SALVAGE_MODE": "enforce"},
        selectors=selectors,
        perform_copy_export=failing_copy,
    )
    assert receipt["outcome"] == "all_failed"
    assert receipt["per_selector_totals"]["error"] == 2
    assert receipt["per_selector_totals"]["exported"] == 0


# === cand-J: default mode is dry-run when env var omitted ===


def test_config_defaults_mode_to_dry_run(tmp_path: Path) -> None:
    env = _base_env(
        tmp_path, override={"NODE27_DB_EXPORT_SALVAGE_MODE": None}
    )
    config = salvage.config_from_args(_args(), env)
    assert config.mode == "dry-run"
    assert config.enforce is False


# === cand-M: _sentinel_insert_check internals fail-closed classification ===


class _FakePsycopgErrors:
    class InsufficientPrivilege(Exception):
        pass

    class SyntaxError(Exception):  # noqa: N818 — mirror psycopg2's naming
        pass

    class NotNullViolation(Exception):
        pass

    class OperationalError(Exception):
        pass

    class QueryCanceled(Exception):
        pass


class _FakeCursor:
    def __init__(self, on_execute):
        self._on_execute = on_execute

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, *_args, **_kwargs):
        self._on_execute()


class _FakeConnection:
    def __init__(self, on_execute):
        self._on_execute = on_execute
        self.rolled_back = False

    def cursor(self):
        return _FakeCursor(self._on_execute)

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


@pytest.mark.parametrize(
    ("case", "expected_prefix"),
    [
        ("insufficient_privilege_returns_none", None),
        ("not_null_violation_refuses", "role can INSERT into"),
        ("syntax_error_refuses_as_unavailable", "role privilege probe unavailable"),
        ("operational_error_refuses_as_unavailable", "role privilege probe unavailable"),
        ("query_canceled_refuses_as_unavailable", "role privilege probe unavailable"),
        ("success_refuses", "role can INSERT into"),
    ],
)
def test_sentinel_insert_check_classification(
    monkeypatch: pytest.MonkeyPatch, case: str, expected_prefix: str | None
) -> None:
    fake_errors = _FakePsycopgErrors()

    # Patch the psycopg2 modules at import-time inside the runner.
    import types

    fake_psycopg2 = types.ModuleType("psycopg2")
    fake_errors_module = types.ModuleType("psycopg2.errors")
    fake_errors_module.InsufficientPrivilege = fake_errors.InsufficientPrivilege
    fake_errors_module.SyntaxError = fake_errors.SyntaxError
    fake_errors_module.NotNullViolation = fake_errors.NotNullViolation
    fake_errors_module.OperationalError = fake_errors.OperationalError
    fake_errors_module.QueryCanceled = fake_errors.QueryCanceled
    fake_psycopg2.errors = fake_errors_module

    def on_execute():
        if case == "insufficient_privilege_returns_none":
            raise fake_errors.InsufficientPrivilege("permission denied")
        if case == "not_null_violation_refuses":
            raise fake_errors.NotNullViolation("null in column")
        if case == "syntax_error_refuses_as_unavailable":
            raise fake_errors.SyntaxError("syntax error at or near")
        if case == "operational_error_refuses_as_unavailable":
            raise fake_errors.OperationalError("server closed the connection")
        if case == "query_canceled_refuses_as_unavailable":
            raise fake_errors.QueryCanceled("canceling statement due to statement timeout")
        # "success_refuses" — do not raise; INSERT "succeeded".

    connection = _FakeConnection(on_execute)
    fake_psycopg2.connect = lambda dsn: connection

    monkeypatch.setitem(__import__("sys").modules, "psycopg2", fake_psycopg2)
    monkeypatch.setitem(__import__("sys").modules, "psycopg2.errors", fake_errors_module)

    result = salvage._sentinel_insert_check(
        "postgresql://ignored", "met.forcing_station_timeseries", "forcing_version_id"
    )
    if expected_prefix is None:
        assert result is None
    else:
        assert result is not None and result.startswith(expected_prefix), (
            f"case {case!r} expected prefix {expected_prefix!r}, got {result!r}"
        )
    # Sentinel probe MUST rollback regardless of outcome.
    assert connection.rolled_back


# === cand-G / cand-H: river schema example still validates ===


def test_salvage_manifest_river_example_validates() -> None:
    river_example = _ROOT / "schemas/examples/salvage_manifest_river.example.json"
    assert river_example.exists(), "river example must ship"
    jsonschema.validate(_load_json(river_example), _load_json(_MANIFEST_SCHEMA_PATH))


def test_forcing_example_columns_match_full_ddl() -> None:
    """The manifest example's columns list must span the full DDL, not a subset."""
    example = _load_json(_MANIFEST_EXAMPLE_PATH)
    assert tuple(example["exports"][0]["columns"]) == salvage._COLUMNS_FORCING


def test_river_example_columns_match_full_ddl() -> None:
    river_example = _ROOT / "schemas/examples/salvage_manifest_river.example.json"
    example = _load_json(river_example)
    assert tuple(example["exports"][0]["columns"]) == salvage._COLUMNS_RIVER


# === Extra: zstd binary validation contract ===


def test_zstd_env_var_refused_when_relative(tmp_path: Path) -> None:
    env = _base_env(tmp_path, override={"NODE27_DB_EXPORT_SALVAGE_ZSTD": "relative-zstd"})
    with pytest.raises(salvage.SalvageConfigError, match="ZSTD"):
        salvage.config_from_args(_args(), env)


def test_zstd_env_var_refused_when_missing(tmp_path: Path) -> None:
    env = _base_env(
        tmp_path,
        override={"NODE27_DB_EXPORT_SALVAGE_ZSTD": str(tmp_path / "missing-zstd")},
    )
    with pytest.raises(salvage.SalvageConfigError, match="ZSTD"):
        salvage.config_from_args(_args(), env)


def test_zstd_env_var_refused_when_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real-zstd"
    real.write_text("#!/bin/sh\ncat\n", encoding="utf-8")
    real.chmod(0o700)
    link = tmp_path / "zstd-symlink"
    link.symlink_to(real)
    env = _base_env(tmp_path, override={"NODE27_DB_EXPORT_SALVAGE_ZSTD": str(link)})
    with pytest.raises(salvage.SalvageConfigError, match="ZSTD"):
        salvage.config_from_args(_args(), env)


def test_max_selector_bytes_default_is_2gib(tmp_path: Path) -> None:
    env = _base_env(
        tmp_path, override={"NODE27_DB_EXPORT_SALVAGE_MAX_SELECTOR_BYTES": None}
    )
    config = salvage.config_from_args(_args(), env)
    assert config.max_selector_bytes == 2 * 1024 * 1024 * 1024


def test_max_selector_bytes_refused_when_non_positive(tmp_path: Path) -> None:
    env = _base_env(
        tmp_path, override={"NODE27_DB_EXPORT_SALVAGE_MAX_SELECTOR_BYTES": "0"}
    )
    with pytest.raises(salvage.SalvageConfigError, match="MAX_SELECTOR_BYTES"):
        salvage.config_from_args(_args(), env)


def test_max_selector_bytes_boundary_accepts_ceiling(tmp_path: Path) -> None:
    """Inclusive 16 GiB ceiling: exact-ceiling value parses successfully.

    Pairs with the ``"17179869185"`` (ceiling+1) parametrize entry in
    ``test_config_parse_fails_closed``, which proves off-by-one refusal.
    """
    ceiling = 16 * 1024 * 1024 * 1024  # 17_179_869_184
    env = _base_env(
        tmp_path,
        override={"NODE27_DB_EXPORT_SALVAGE_MAX_SELECTOR_BYTES": str(ceiling)},
    )
    config = salvage.config_from_args(_args(), env)
    assert config.max_selector_bytes == ceiling
