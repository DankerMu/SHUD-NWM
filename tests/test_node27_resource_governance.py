from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import node27_resource_governance as governance


def _base_receipt(*, archive: dict | None = None) -> dict:
    thresholds = governance.AuditThresholds()
    receipt: dict = {
        "filesystem": {
            "filesystems": {
                "root": {"free_bytes": thresholds.root_free_critical_bytes - 1},
                "home": {"free_bytes": thresholds.home_free_warn_bytes - 1},
            }
        },
        "postgres": {
            "status": "ok",
            "database_sizes": [{"datname": "nhms", "bytes": thresholds.database_warn_bytes + 1}],
            "settings": [{"name": "log_temp_files", "setting": "-1", "unit": "kB"}],
            "stat_database": [{"datname": "nhms", "temp_bytes": thresholds.temp_bytes_warn + 1}],
            "hypertables": [
                {
                    "hypertable_schema": "hydro",
                    "hypertable_name": "river_timeseries",
                    "num_chunks": 6,
                    "compression_enabled": False,
                    "retention_job_id": None,
                    "compression_job_id": None,
                }
            ],
            "hypertable_size_breakdown": [
                {
                    "hypertable_schema": "hydro",
                    "hypertable_name": "river_timeseries",
                    "table_bytes": 10,
                    "indexes_bytes": 50,
                }
            ],
            "dead_tuple_hotspots": [
                {
                    "schemaname": "_timescaledb_internal",
                    "relname": "_hyper_3_9_chunk",
                    "dead_pct": thresholds.dead_tuple_warn_pct,
                    "n_dead_tup": 100001,
                    "total_pretty": "47 GB",
                }
            ],
        },
    }
    if archive is not None:
        receipt["archive_root"] = archive
    return receipt


def test_recommendations_capture_node27_resource_risks() -> None:
    recommendations = governance._recommendations(_base_receipt(), governance.AuditThresholds())
    codes = {item["code"] for item in recommendations}

    assert "ROOT_FREE_BELOW_CRITICAL" in codes
    assert "HOME_FREE_BELOW_WARNING" in codes
    assert "DATABASE_SIZE_ABOVE_WARNING" in codes
    assert "TEMP_SPILL_LOGGING_DISABLED" in codes
    assert "TIMESCALE_RETENTION_POLICY_MISSING" in codes
    assert "TIMESCALE_COMPRESSION_POLICY_MISSING" in codes
    assert "HYPERTABLE_INDEX_RATIO_HIGH" in codes
    assert "DEAD_TUPLE_HOTSPOT" in codes


def test_write_summary_rejects_relative_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="summary path must be absolute"):
        governance._write_summary(Path("relative.json"), {"status": "completed"})

    output = tmp_path / "receipt.json"
    governance._write_summary(output, {"status": "completed"})
    assert json.loads(output.read_text(encoding="utf-8")) == {"status": "completed"}


def test_config_does_not_emit_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret-user:secret-pass@localhost:55432/nhms")
    args = governance.build_parser().parse_args(["--repo-root", "/tmp/repo", "--object-store-root", "/tmp/os"])

    config = governance.config_from_args(args)
    receipt = {
        "filesystem": {"filesystems": {}},
        "postgres": {"status": "skipped"},
        "safety": {"database_url_redacted": bool(config.database_url)},
    }
    rendered = json.dumps(receipt)

    assert config.database_url == "postgresql://secret-user:secret-pass@localhost:55432/nhms"
    assert "secret-pass" not in rendered
    assert receipt["safety"]["database_url_redacted"] is True


def test_quiet_flag_is_available_for_systemd_wrapper() -> None:
    args = governance.build_parser().parse_args(["--quiet"])

    assert args.quiet is True


def test_default_services_include_new_archive_and_audit_units() -> None:
    # #849 registers four new units for governance oracle visibility.
    expected = {
        "nhms-node27-product-archive.service",
        "nhms-node27-product-archive.timer",
        "nhms-node27-storage-inventory-audit.service",
        "nhms-node27-storage-inventory-audit.timer",
    }
    assert expected.issubset(set(governance.DEFAULT_SERVICES))


def test_default_services_includes_timeseries_compression_units() -> None:
    # #853 registers the compression service + timer so the governance
    # audit receipt reflects their systemd state alongside the other
    # node-27 storage-tier units.
    expected = {
        "nhms-node27-timeseries-compression.service",
        "nhms-node27-timeseries-compression.timer",
    }
    assert expected.issubset(set(governance.DEFAULT_SERVICES))


def test_collect_systemd_receipt_includes_compression_units(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When systemctl is mocked, the audit receipt must carry entries for
    both new compression units so #853 governance registration is proven
    end-to-end through the collector rather than only via the tuple set."""

    def _fake_run_command(args, *, timeout: int = 20) -> dict:
        # Simulate a healthy systemctl show/list-timers response.
        return {
            "status": "ok",
            "return_code": 0,
            "stdout": "Id=stub\nActiveState=active\nSubState=running\nResult=success\n",
            "stderr": "",
            "args": list(args),
        }

    monkeypatch.setattr(governance, "_run_command", _fake_run_command)
    payload = governance.collect_systemd(governance.DEFAULT_SERVICES)
    services = payload["services"]
    assert "nhms-node27-timeseries-compression.service" in services
    assert "nhms-node27-timeseries-compression.timer" in services
    for unit in (
        "nhms-node27-timeseries-compression.service",
        "nhms-node27-timeseries-compression.timer",
    ):
        assert services[unit]["command"]["status"] == "ok"
        assert services[unit]["properties"].get("Id") == "stub"


def test_config_absent_archive_env_yields_none_archive_root(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "NODE27_GOVERNANCE_ARCHIVE_ROOT",
        "NHMS_ARCHIVE_ROOT",
        "NHMS_ARCHIVE_FREE_SPACE_WARN_BYTES",
        "NHMS_ARCHIVE_FREE_SPACE_REFUSE_BYTES",
    ):
        monkeypatch.delenv(key, raising=False)
    args = governance.build_parser().parse_args([])
    config = governance.config_from_args(args)
    assert config.archive_root is None
    assert config.thresholds.archive_free_warn_bytes is None
    assert config.thresholds.archive_free_refuse_bytes is None


def test_config_reads_archive_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NHMS_ARCHIVE_ROOT", str(tmp_path))
    monkeypatch.setenv("NHMS_ARCHIVE_FREE_SPACE_WARN_BYTES", "2000")
    monkeypatch.setenv("NHMS_ARCHIVE_FREE_SPACE_REFUSE_BYTES", "1000")
    args = governance.build_parser().parse_args([])
    config = governance.config_from_args(args)
    assert config.archive_root == tmp_path
    assert config.thresholds.archive_free_warn_bytes == 2000
    assert config.thresholds.archive_free_refuse_bytes == 1000


def test_governance_archive_root_env_precedence_over_shared_alias(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    per_script = tmp_path / "per-script"
    per_script.mkdir()
    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setenv("NODE27_GOVERNANCE_ARCHIVE_ROOT", str(per_script))
    monkeypatch.setenv("NHMS_ARCHIVE_ROOT", str(shared))
    monkeypatch.delenv("NHMS_ARCHIVE_FREE_SPACE_WARN_BYTES", raising=False)
    monkeypatch.delenv("NHMS_ARCHIVE_FREE_SPACE_REFUSE_BYTES", raising=False)
    args = governance.build_parser().parse_args([])
    config = governance.config_from_args(args)
    assert config.archive_root == per_script


@pytest.mark.parametrize(
    ("warn", "refuse"),
    [
        (None, "100"),
        ("200", None),
        ("", "100"),
        ("200", ""),
        ("not-an-int", "100"),
        ("200", "not-an-int"),
        ("0", "0"),
        ("-1", "-2"),
        ("100", "200"),  # refuse >= warn
        ("100", "100"),  # refuse == warn
    ],
)
def test_invalid_archive_watermark_env_fails_closed(
    monkeypatch: pytest.MonkeyPatch, warn: str | None, refuse: str | None
) -> None:
    monkeypatch.delenv("NHMS_ARCHIVE_ROOT", raising=False)
    monkeypatch.delenv("NODE27_GOVERNANCE_ARCHIVE_ROOT", raising=False)
    if warn is None:
        monkeypatch.delenv("NHMS_ARCHIVE_FREE_SPACE_WARN_BYTES", raising=False)
    else:
        monkeypatch.setenv("NHMS_ARCHIVE_FREE_SPACE_WARN_BYTES", warn)
    if refuse is None:
        monkeypatch.delenv("NHMS_ARCHIVE_FREE_SPACE_REFUSE_BYTES", raising=False)
    else:
        monkeypatch.setenv("NHMS_ARCHIVE_FREE_SPACE_REFUSE_BYTES", refuse)
    args = governance.build_parser().parse_args([])
    with pytest.raises(ValueError):
        governance.config_from_args(args)


def test_invalid_archive_watermark_env_makes_main_exit_nonzero_without_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("NHMS_ARCHIVE_FREE_SPACE_WARN_BYTES", "100")
    monkeypatch.delenv("NHMS_ARCHIVE_FREE_SPACE_REFUSE_BYTES", raising=False)
    monkeypatch.delenv("NHMS_ARCHIVE_ROOT", raising=False)
    receipt = tmp_path / "receipt.json"
    code = governance.main([
        "--repo-root",
        str(tmp_path),
        "--object-store-root",
        str(tmp_path),
        "--summary-path",
        str(receipt),
        "--quiet",
    ])
    assert code != 0
    assert not receipt.exists()
    diagnostic = json.loads(capsys.readouterr().err)
    assert diagnostic["status"] == "failed"


def test_collect_archive_root_reports_free_and_used(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        governance.shutil,
        "disk_usage",
        lambda path: type("U", (), {"total": 1_000, "used": 400, "free": 600})(),
    )
    monkeypatch.setattr(
        governance,
        "_du_bytes",
        lambda path: {"path": str(path), "status": "ok", "bytes": 42, "pretty": "42 B"},
    )
    config = governance.AuditConfig(
        repo_root=tmp_path,
        object_store_root=tmp_path,
        pgdata_root=None,
        database_url=None,
        summary_path=None,
        services=(),
        thresholds=governance.AuditThresholds(
            archive_free_warn_bytes=800, archive_free_refuse_bytes=400
        ),
        archive_root=tmp_path,
    )
    payload = governance.collect_archive_root(config)
    assert payload["status"] == "ok"
    assert payload["total_bytes"] == 1_000
    assert payload["free_bytes"] == 600
    assert payload["used_bytes"] == 42
    assert payload["warn_free_bytes"] == 800
    assert payload["refuse_free_bytes"] == 400
    # 600 >= 400 (refuse) and < 800 (warn) → band=warn
    assert payload["band"] == "warn"


def test_collect_archive_root_skips_when_unset(tmp_path: Path) -> None:
    config = governance.AuditConfig(
        repo_root=tmp_path,
        object_store_root=tmp_path,
        pgdata_root=None,
        database_url=None,
        summary_path=None,
        services=(),
        thresholds=governance.AuditThresholds(),
        archive_root=None,
    )
    payload = governance.collect_archive_root(config)
    assert payload == {"status": "skipped", "reason": "archive_root_unset"}


def test_collect_archive_root_band_unconfigured_when_no_watermarks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        governance.shutil,
        "disk_usage",
        lambda path: type("U", (), {"total": 1_000, "used": 400, "free": 600})(),
    )
    monkeypatch.setattr(
        governance,
        "_du_bytes",
        lambda path: {"path": str(path), "status": "ok", "bytes": 1, "pretty": "1 B"},
    )
    config = governance.AuditConfig(
        repo_root=tmp_path,
        object_store_root=tmp_path,
        pgdata_root=None,
        database_url=None,
        summary_path=None,
        services=(),
        thresholds=governance.AuditThresholds(),
        archive_root=tmp_path,
    )
    payload = governance.collect_archive_root(config)
    assert payload["band"] == "unconfigured"


@pytest.mark.parametrize(
    ("free", "expected_band", "expected_code"),
    [
        (100, "refuse", "ARCHIVE_FREE_BELOW_REFUSE"),
        (500, "warn", "ARCHIVE_FREE_BELOW_WARN"),
        (900, "clean", None),
    ],
)
def test_recommendations_flag_archive_free_space_band(
    free: int, expected_band: str, expected_code: str | None
) -> None:
    archive_payload = {
        "status": "ok",
        "path": "/home/ghdc/nwm/archive",
        "free_bytes": free,
        "warn_free_bytes": 800,
        "refuse_free_bytes": 200,
        "band": expected_band,
    }
    receipt = _base_receipt(archive=archive_payload)
    thresholds = governance.AuditThresholds(
        archive_free_warn_bytes=800, archive_free_refuse_bytes=200
    )
    codes = {item["code"] for item in governance._recommendations(receipt, thresholds)}
    if expected_code is None:
        assert "ARCHIVE_FREE_BELOW_REFUSE" not in codes
        assert "ARCHIVE_FREE_BELOW_WARN" not in codes
    else:
        assert expected_code in codes


def test_recommendations_skip_archive_when_status_not_ok() -> None:
    archive_payload = {"status": "skipped", "reason": "archive_root_unset"}
    receipt = _base_receipt(archive=archive_payload)
    codes = {item["code"] for item in governance._recommendations(receipt, governance.AuditThresholds())}
    assert not any(code.startswith("ARCHIVE_") for code in codes)


def _parse_env_value(text: str, key: str) -> str:
    """Extract the value declared for ``key`` in a bash-style env file.

    Approximates ``bash source`` for simple ``KEY=VALUE`` lines: strips the
    inline ``# comment`` suffix (bash treats ``#`` preceded by whitespace as
    a comment separator) and trailing whitespace. Returns ``""`` if the key
    is not declared, which lets callers distinguish "missing" from "empty
    value" via the presence assertion above.
    """
    prefix = f"\n{key}="
    marker = "\n" + text
    idx = marker.find(prefix)
    if idx < 0:
        return ""
    line_end = marker.find("\n", idx + 1)
    raw = marker[idx + len(prefix) : (line_end if line_end != -1 else None)]
    for sep in (" #", "\t#"):
        pos = raw.find(sep)
        if pos != -1:
            raw = raw[:pos]
    return raw.strip()


def test_env_examples_declare_shared_archive_watermarks() -> None:
    """All three env examples must declare NHMS_ARCHIVE_ROOT and the
    free-space watermark pair so governance/mover/audit observe the same
    values. The wrappers source only their own env file, so drift across
    these three files is a governance blind-spot bug (governance may report
    a different band than the mover actually enforces). See #849."""
    required_keys = (
        "NHMS_ARCHIVE_ROOT",
        "NHMS_ARCHIVE_FREE_SPACE_WARN_BYTES",
        "NHMS_ARCHIVE_FREE_SPACE_REFUSE_BYTES",
    )
    envs = (
        "infra/env/node27-product-archive.example",
        "infra/env/node27-storage-inventory-audit.example",
        "infra/env/node27-resource-governance.example",
    )
    root = Path(__file__).resolve().parents[1]
    env_texts = {env_path: (root / env_path).read_text(encoding="utf-8") for env_path in envs}
    for env_path, text in env_texts.items():
        for key in required_keys:
            assert f"\n{key}=" in "\n" + text, (
                f"{env_path} is missing shared archive env {key}; "
                "the three env files must stay synchronized so "
                "governance/mover/audit observe the same watermarks."
            )
    # Beyond mere presence, the three files must agree on the VALUES for
    # every shared key. Each wrapper sources only its own env file; a value
    # mismatch here means governance may report a different band than the
    # mover actually enforces (the exact drift #849 closes).
    for key in required_keys:
        values = {env_path: _parse_env_value(text, key) for env_path, text in env_texts.items()}
        unique = set(values.values())
        assert len(unique) == 1, (
            f"Shared archive env {key} drifted across env files: {values}. "
            "The mover, audit, and governance wrappers each source only their "
            "own env file, so a value mismatch here means governance may "
            "report a different band than the mover actually enforces. Keep "
            "the three files byte-identical for these shared keys."
        )
