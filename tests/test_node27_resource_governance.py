from __future__ import annotations

import argparse
import json
import re
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jsonschema
import pytest

from packages.common import node27_cold_governance_collection as collection
from scripts import node27_resource_governance as governance


def _base_receipt() -> dict:
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


def _policy_rows(*identities: tuple[str, str]) -> dict:
    """A postgres block whose only content is bare `hypertables` rows."""
    return {
        "status": "ok",
        "hypertables": [
            {
                "hypertable_schema": schema,
                "hypertable_name": name,
                "num_chunks": 3,
                "compression_enabled": False,
                "retention_job_id": None,
                "compression_job_id": None,
            }
            for schema, name in identities
        ],
    }


@pytest.mark.parametrize(
    "identity",
    [
        ("hydro", "river_timeseries"),
        ("hydro", "river_timeseries_legacy"),
        ("met", "forcing_station_timeseries"),
        ("met", "forcing_station_timeseries_legacy"),
    ],
)
def test_every_candidate_hypertable_raises_the_policy_missing_pair(
    identity: tuple[str, str],
) -> None:
    """#1985: the audit's policy checks are keyed on the lifecycle CANDIDATE
    set, all four identities.

    A transitional `_legacy` sibling is governed by the same retention and
    compression policies as the table it was renamed from, so it must raise the
    same two warnings — and it must raise them under its OWN name, or an
    operator reading the receipt cannot tell which table lost its policy.
    Parametrised over all four so a narrowing back to the canonical pair (a
    `CANDIDATE_HYPERTABLES[:2]` slice, the shape the bug took) fails here.
    """
    receipt = {"filesystem": {"filesystems": {}}, "postgres": _policy_rows(identity)}
    qualified = f"{identity[0]}.{identity[1]}"
    evidence = {
        item["code"]: item["evidence"]
        for item in governance._recommendations(receipt, governance.AuditThresholds())
    }
    assert evidence["TIMESCALE_RETENTION_POLICY_MISSING"]["hypertable"] == qualified
    assert evidence["TIMESCALE_COMPRESSION_POLICY_MISSING"]["hypertable"] == qualified


@pytest.mark.parametrize(
    "identity",
    [
        # Right table name, wrong schema: a `public` copy must not borrow the
        # lifecycle lane's policy checks. This is what matching on (schema,
        # name) instead of the bare name buys.
        ("public", "river_timeseries"),
        ("hydro", "river_timeseries_old"),
        ("ops", "run_display_coverage"),
    ],
)
def test_a_hypertable_outside_the_candidate_set_raises_neither_policy_code(
    identity: tuple[str, str],
) -> None:
    receipt = {"filesystem": {"filesystems": {}}, "postgres": _policy_rows(identity)}
    codes = _codes(receipt)
    assert "TIMESCALE_RETENTION_POLICY_MISSING" not in codes
    assert "TIMESCALE_COMPRESSION_POLICY_MISSING" not in codes


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


def test_disk_usage_reports_reserved_bytes_and_identity_arithmetic(monkeypatch: pytest.MonkeyPatch) -> None:
    from packages.common import node27_cold_governance_collection as collection

    class Usage:
        f_blocks = 1000
        f_bfree = 400
        f_bavail = 300
        f_frsize = 4096
        f_fsid = 11

    monkeypatch.setattr(collection.os, "statvfs", lambda _path: Usage())
    monkeypatch.setattr(collection.os, "major", lambda _dev: 8)
    monkeypatch.setattr(collection.os, "minor", lambda _dev: 11)
    monkeypatch.setattr(collection.Path, "stat", lambda self: type("S", (), {"st_dev": 0x811})())
    observed = collection.disk_usage(Path("/home"))
    assert observed["status"] == "ok"
    assert observed["total_bytes"] == 1000 * 4096
    assert observed["free_bytes"] == 300 * 4096
    assert observed["used_bytes"] == 600 * 4096
    assert observed["reserved_bytes"] == 100 * 4096
    assert observed["total_bytes"] == observed["used_bytes"] + observed["free_bytes"] + observed["reserved_bytes"]


def test_cold_governance_sample_refuses_unavailable_disk_or_du_without_fabricating_zero() -> None:
    filesystem = {
        "filesystems": {
            "home": {"path": "/home", "status": "unavailable"},
            "cold": {
                "path": "/data/GHDC",
                "status": "ok",
                "total_bytes": 1000,
                "free_bytes": 200,
                "used_bytes": 700,
                "reserved_bytes": 100,
                "device_identity": "8:12",
            },
        },
        "path_sizes": {
            "pgdata_root": {"status": "missing"},
            "object_store_root": {"path": "/home/ghdc/nwm", "status": "unavailable"},
        },
    }
    home = governance._cold_governance_sample(filesystem, {}, path="/home", observed_at="2026-08-31T12:00:00Z")
    assert home.get("status") == "unavailable"
    assert home.get("blockers")
    assert home.get("total_bytes") is None
    assert home.get("used_bytes") is None
    assert home.get("free_bytes") is None
    assert home.get("reserved_bytes") is None
    named = " ".join(str(item) for item in home.get("blockers", [])).lower()
    assert "home" in named


def _ok_cold_filesystem() -> dict:
    return {
        "filesystems": {
            "cold": {
                "path": "/data/GHDC",
                "status": "ok",
                "total_bytes": 1000,
                "free_bytes": 200,
                "used_bytes": 700,
                "reserved_bytes": 100,
                "device_identity": "8:12",
            }
        },
        "path_sizes": {},
    }


@pytest.mark.parametrize(
    "postgres",
    (
        {},
        {"status": None, "cold_relation_by_tablespace": []},
        {"status": "skipped", "cold_relation_by_tablespace": []},
        {"status": "blocked", "cold_relation_by_tablespace": []},
        {"status": "ok"},
        {"status": "ok", "cold_relation_by_tablespace": "not-a-list"},
    ),
    ids=("missing-status", "none-status", "skipped", "blocked", "missing-field", "malformed-field"),
)
def test_cold_governance_sample_refuses_unobserved_postgres_inventory(postgres: dict) -> None:
    from packages.common.node27_cold_governance_collection import cold_governance_sample

    sample = cold_governance_sample(
        _ok_cold_filesystem(),
        postgres,
        path="/data/GHDC",
        observed_at="2026-08-31T12:00:00Z",
    )
    assert sample["status"] == "unavailable"
    assert sample["nhms_cold_relation_bytes"] is None
    assert sample["total_bytes"] is None
    assert sample["used_bytes"] is None
    assert sample.get("residual_bytes") is None
    assert any("cold relation inventory" in str(item) for item in sample["blockers"])


@pytest.mark.parametrize(
    "rows",
    (
        ["not-a-mapping"],
        [{}],
        [{"bytes": None}],
        [{"bytes": "400"}],
        [{"bytes": -1}],
        [{"bytes": True}],
    ),
    ids=("non-mapping", "missing-bytes", "none-bytes", "string-bytes", "negative-bytes", "bool-bytes"),
)
def test_cold_governance_sample_refuses_malformed_relation_rows(rows: list[object]) -> None:
    from packages.common.node27_cold_governance_collection import cold_governance_sample

    sample = cold_governance_sample(
        _ok_cold_filesystem(),
        {"status": "ok", "cold_relation_by_tablespace": rows},
        path="/data/GHDC",
        observed_at="2026-08-31T12:00:00Z",
    )
    assert sample["status"] == "unavailable"
    assert sample["nhms_cold_relation_bytes"] is None
    assert sample["total_bytes"] is None
    assert sample["used_bytes"] is None
    assert sample.get("residual_bytes") is None
    assert any("cold relation inventory" in str(item) for item in sample["blockers"])


@pytest.mark.parametrize(
    ("rows", "expected"),
    (
        ([], 0),
        ([{"bytes": 150}, {"bytes": 250}], 400),
    ),
    ids=("empty-list", "positive-list"),
)
def test_cold_governance_sample_accounts_observed_ok_relation_inventory(rows: list[object], expected: int) -> None:
    from packages.common.node27_cold_governance_collection import cold_governance_sample

    sample = cold_governance_sample(
        _ok_cold_filesystem(),
        {"status": "ok", "cold_relation_by_tablespace": rows},
        path="/data/GHDC",
        observed_at="2026-08-31T12:00:00Z",
    )
    assert sample["status"] == "ok"
    assert sample["nhms_cold_relation_bytes"] == expected
    assert sample["blockers"] == []


def test_unavailable_home_sample_publishes_null_bytes_not_fabricated_zero(tmp_path: Path) -> None:
    from packages.common.node27_cold_governance import GovernanceConfig, build_cold_governance_receipt

    home = {
        "path": "/home",
        "observed_at": "2026-08-31T12:00:01Z",
        "identity": None,
        "status": "unavailable",
        "blockers": ["home disk observation is unavailable"],
        "total_bytes": None,
        "free_bytes": None,
        "used_bytes": None,
        "reserved_bytes": None,
        "pgdata_bytes": None,
        "nhms_cold_relation_bytes": None,
        "object_store_bytes": None,
        "residual_bytes": None,
    }
    cold = {
        "path": "/data/GHDC",
        "observed_at": "2026-08-31T12:00:02Z",
        "identity": "8:12",
        "status": "ok",
        "total_bytes": 1000,
        "free_bytes": 200,
        "used_bytes": 700,
        "reserved_bytes": 100,
        "pgdata_bytes": 0,
        "nhms_cold_relation_bytes": 400,
        "object_store_bytes": 0,
        "residual_bytes": 300,
    }
    receipt, schema = build_cold_governance_receipt(
        config=GovernanceConfig(receipt_path=tmp_path / "g.json", head_sha="f" * 40),
        started_at="2026-08-31T12:00:00Z",
        finished_at="2026-08-31T12:00:05Z",
        home=home,
        cold=cold,
        evidence={
            "health": {"healthy": True, "raid": {"file_identity": {"sha256": "a" * 64}}, "smart": [
                {"device": "/dev/sdb1", "status": "PASS", "file_identity": {"sha256": "c" * 64}},
                {"device": "/dev/sdc1", "status": "PASS", "file_identity": {"sha256": "d" * 64}},
            ]},
            "backup": {"complete": True, "file_identity": {"sha256": "b" * 64}, "missing_targets": []},
            "mount_inventory": {"current": [], "stopped": []},
            "catalog": {
                "tablespace": "nhms_cold",
                "location": "/home/postgres/pgdata/tablespaces/nhms_cold",
                "relations": [],
            },
        },
    )
    assert receipt["outcome"] != "healthy"
    assert receipt["filesystems"]["home"]["status"] == "unavailable"
    assert receipt["filesystems"]["home"]["total_bytes"] is None
    assert receipt["filesystems"]["home"]["residual_bytes"] is None
    jsonschema.validate(receipt, schema)


def test_optional_cold_governance_receipt_is_refusal_until_descriptor_evidence_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "cold-governance.json"
    args = governance.build_parser().parse_args(
        ["--cold-governance-receipt-path", str(output), "--cold-governance-head-sha", "a" * 40]
    )
    config = governance.config_from_args(args)
    monkeypatch.setattr(
        governance,
        "collect_filesystem",
        lambda _config: {
            "filesystems": {
                "home": {"path": "/home", "total_bytes": 100, "free_bytes": 20, "used_bytes": 80},
                "object_store_fs": {"path": "/data/GHDC", "total_bytes": 100, "free_bytes": 20, "used_bytes": 80},
            },
            "path_sizes": {},
        },
    )
    monkeypatch.setattr(governance, "collect_postgres", lambda _url, **_kwargs: {"status": "skipped"})
    monkeypatch.setattr(governance, "collect_systemd", lambda _services: {"units": []})

    governance.build_receipt(config)

    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["outcome"] == "refusal"
    assert output.stat().st_mode & 0o777 == 0o600


def test_governance_cli_accepts_descriptor_evidence_and_prior_trend_configuration(tmp_path: Path) -> None:
    args = governance.build_parser().parse_args(
        [
            "--cold-governance-receipt-path", str(tmp_path / "receipt.json"),
            "--cold-governance-evidence-hostname", "node27-test",
            "--cold-governance-evidence-max-age-seconds", "300",
            "--cold-governance-evidence-approved-mode", "0600",
            "--cold-governance-mdadm-evidence-path", str(tmp_path / "mdadm.json"),
            "--cold-governance-smart-evidence", f"/dev/sdb1={tmp_path / 'sdb.json'}",
            "--cold-governance-smart-evidence", f"/dev/sdc1={tmp_path / 'sdc.json'}",
            "--cold-governance-backup-evidence-path", str(tmp_path / "backup.json"),
            "--cold-governance-prior-receipt-path", str(tmp_path / "prior.json"),
            "--cold-governance-prior-receipt-max-age-seconds", "600",
        ]
    )

    config = governance.config_from_args(args)

    assert config.cold_governance_evidence_hostname == "node27-test"
    assert config.cold_governance_evidence_approved_modes == (0o600,)
    assert dict(config.cold_governance_smart_evidence_paths)["/dev/sdb1"] == tmp_path / "sdb.json"
    assert config.cold_governance_prior_receipt_max_age_seconds == 600


def test_quiet_flag_is_available_for_systemd_wrapper() -> None:
    args = governance.build_parser().parse_args(["--quiet"])

    assert args.quiet is True


def test_default_services_carry_no_retired_archive_units() -> None:
    """#1370: the archive lane is retired, so the four units #849 registered
    for governance visibility are gone. Keeping a permanently-refusing unit
    in the audit set is exactly the health-reading distortion this change
    removes.
    """
    retired = {
        "nhms-node27-product-archive.service",
        "nhms-node27-product-archive.timer",
        "nhms-node27-storage-inventory-audit.service",
        "nhms-node27-storage-inventory-audit.timer",
    }
    assert retired.isdisjoint(set(governance.DEFAULT_SERVICES))
    assert not [unit for unit in governance.DEFAULT_SERVICES if "archive" in unit]


def test_governance_config_and_receipt_carry_no_archive_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1370: `collect_archive_root` and the `archive_root` receipt block are
    gone; a live receipt must not claim to observe a volume that no lane uses.

    #1382: the attribute-level assertions below only pin the deletion of the
    retired collector surfaces — a renamed collector or a generic collector
    loop could reintroduce the top-level key with them still green. So this
    test also builds the receipt artifact itself (collectors stubbed; no DB,
    systemd, or filesystem probing) and pins the key's absence on the product.
    """
    assert not hasattr(governance, "collect_archive_root")
    assert not hasattr(governance.AuditThresholds(), "archive_free_warn_bytes")
    assert not hasattr(governance.AuditThresholds(), "archive_free_refuse_bytes")
    config = governance.config_from_args(governance.build_parser().parse_args([]))
    assert not hasattr(config, "archive_root")

    monkeypatch.setattr(governance, "collect_filesystem", lambda _config: {"filesystems": {}})
    monkeypatch.setattr(governance, "collect_postgres", lambda _url, **_kwargs: {"status": "skipped"})
    monkeypatch.setattr(governance, "collect_systemd", lambda _services: {"units": []})
    receipt = governance.build_receipt(config)
    assert "archive_root" not in receipt


def test_default_services_includes_timeseries_compression_units() -> None:
    # #853 registers the compression service + timer so the governance
    # audit receipt reflects their systemd state alongside the other
    # node-27 storage-tier units.
    expected = {
        "nhms-node27-timeseries-compression.service",
        "nhms-node27-timeseries-compression.timer",
    }
    assert expected.issubset(set(governance.DEFAULT_SERVICES))


def test_default_services_includes_timeseries_retention_units() -> None:
    # #855 registers the retention service + timer so the governance
    # audit receipt reflects their systemd state alongside the compression
    # sibling. Position is alphabetic — retention follows compression in
    # DEFAULT_SERVICES (see H11 fixture pin).
    expected = {
        "nhms-node27-timeseries-retention.service",
        "nhms-node27-timeseries-retention.timer",
    }
    assert expected.issubset(set(governance.DEFAULT_SERVICES))


def test_default_services_includes_frontier_alert_units() -> None:
    # #1368 registers the frontier stall alert service + timer so the
    # governance audit receipt reflects their systemd state. The alerting lane
    # is the thing that notices production stopped producing — a silently
    # disabled timer must be visible in the governance oracle, not only in the
    # alerter's own (equally silent) absence of mail.
    expected = {
        "nhms-node27-frontier-alert.service",
        "nhms-node27-frontier-alert.timer",
    }
    assert expected.issubset(set(governance.DEFAULT_SERVICES))


def test_collect_systemd_receipt_includes_frontier_alert_units(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1368 registration proven end-to-end through the collector (mocked
    systemctl), not only via the DEFAULT_SERVICES tuple."""

    def _fake_run_command(args, *, timeout: int = 20) -> dict:
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
    for unit in (
        "nhms-node27-frontier-alert.service",
        "nhms-node27-frontier-alert.timer",
    ):
        assert unit in services
        assert services[unit]["command"]["status"] == "ok"
        assert services[unit]["properties"].get("Id") == "stub"


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


def test_collect_systemd_receipt_includes_retention_units(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H11 test row: mocked systemctl → governance receipt must carry entries
    for BOTH new retention units so #855 registration is proven end-to-end
    through the collector rather than only via the tuple set."""

    def _fake_run_command(args, *, timeout: int = 20) -> dict:
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
    assert "nhms-node27-timeseries-retention.service" in services
    assert "nhms-node27-timeseries-retention.timer" in services
    for unit in (
        "nhms-node27-timeseries-retention.service",
        "nhms-node27-timeseries-retention.timer",
    ):
        assert services[unit]["command"]["status"] == "ok"
        assert services[unit]["properties"].get("Id") == "stub"


# ---------------------------------------------------------------------------
# #1765 — a critical finding must be audible
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parents[1]
_GOVERNANCE_UNIT_PATH = _ROOT / "infra/systemd/nhms-node27-resource-governance.service"
_GOVERNANCE_WRAPPER_PATH = _ROOT / "scripts/node27_resource_governance_once.sh"

# The stderr anchor, spelled out by hand: this literal is what the journal
# carries and what the `OnFailure=` mail body quotes, so it is a contract with
# an operator, not an implementation detail.
_CRITICAL_ANCHOR = "RESOURCE_GOVERNANCE_CRITICAL:"


def _receipt_with(*recommendations: dict) -> dict:
    """A completed receipt carrying exactly the given recommendations."""
    return {
        "schema_version": governance.SCHEMA_VERSION,
        "status": "completed",
        "execution_mode": "read_only_audit",
        "recommendations": list(recommendations),
    }


def _recommendation(severity: str, code: str) -> dict:
    return {"severity": severity, "area": "filesystem", "code": code, "evidence": {}, "action": "x"}


def _run_main(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    receipt: dict,
    summary_path: Path,
) -> tuple[int, str, str]:
    monkeypatch.setattr(governance, "build_receipt", lambda _config: receipt)
    # `--quiet` is what the systemd wrapper actually passes, so it is the only
    # configuration in which this signal has to work.
    rc = governance.main(["--summary-path", str(summary_path), "--quiet"])
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def test_critical_recommendation_exits_non_zero_after_writing_the_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Scenario "Root volume below the critical threshold".

    The receipt is evidence and must survive the failure, so it is written
    first and its `status` stays `completed`: the audit DID complete — it is
    the finding that is critical, and the schema does not move.
    """
    summary_path = tmp_path / "resource-governance.json"
    receipt = _receipt_with(
        _recommendation("critical", "ROOT_FREE_BELOW_CRITICAL"),
        _recommendation("warning", "HOME_FREE_BELOW_WARNING"),
    )

    rc, out, err = _run_main(monkeypatch, capsys, receipt, summary_path)

    assert rc == 1
    written = json.loads(summary_path.read_text(encoding="utf-8"))
    assert written["status"] == "completed"
    assert written == receipt
    assert err.splitlines() == [f"{_CRITICAL_ANCHOR}ROOT_FREE_BELOW_CRITICAL"]
    assert out == ""


def test_every_critical_recommendation_gets_its_own_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One line per finding: a mail body naming only the first critical code
    would send an operator to clean `/` while the database volume is the one
    that is actually out of room.
    """
    summary_path = tmp_path / "resource-governance.json"
    receipt = _receipt_with(
        _recommendation("critical", "ROOT_FREE_BELOW_CRITICAL"),
        _recommendation("warning", "TEMP_BYTES_ABOVE_WARNING"),
        _recommendation("critical", "DATABASE_SIZE_ABOVE_CRITICAL"),
    )

    rc, _out, err = _run_main(monkeypatch, capsys, receipt, summary_path)

    assert rc == 1
    assert err.splitlines() == [
        f"{_CRITICAL_ANCHOR}ROOT_FREE_BELOW_CRITICAL",
        f"{_CRITICAL_ANCHOR}DATABASE_SIZE_ABOVE_CRITICAL",
    ]


def test_no_critical_recommendation_exits_zero_and_prints_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Scenario "No critical recommendation" — warnings stay non-events.

    The audit runs daily; a warning that mailed an operator would be noise the
    lane learns to ignore, which is how the critical one gets missed.
    """
    summary_path = tmp_path / "resource-governance.json"
    receipt = _receipt_with(
        _recommendation("warning", "ROOT_FREE_BELOW_WARNING"),
        _recommendation("warning", "TEMP_BYTES_ABOVE_WARNING"),
    )

    rc, out, err = _run_main(monkeypatch, capsys, receipt, summary_path)

    assert rc == 0
    assert err == ""
    assert out == ""
    assert json.loads(summary_path.read_text(encoding="utf-8"))["status"] == "completed"


def test_the_real_recommendation_builder_produces_the_critical_severity() -> None:
    """The exit-code rule above is only reachable if the audit really labels a
    root-volume shortfall `critical` — this is the join between the two.
    """
    codes = [
        item["code"]
        for item in governance._recommendations(_base_receipt(), governance.AuditThresholds())
        if item["severity"] == "critical"
    ]

    assert "ROOT_FREE_BELOW_CRITICAL" in codes
    assert governance._critical_codes({"recommendations": []}) == []


def test_governance_unit_routes_stderr_to_the_journal_and_alerts_on_failure() -> None:
    """The exit code is only useful if something acts on it. `%n` expands to
    the FULL unit name (mirror of the retention unit).
    """
    text = _GOVERNANCE_UNIT_PATH.read_text(encoding="utf-8")

    assert "OnFailure=nhms-node27-unit-failure-alert@%n.service" in text
    assert "StandardError=journal" in text
    assert "StandardError=append:" not in text
    # stdout keeps its file. Not because the bracket lines live there — the
    # wrapper appends its own `start` / `done rc=` lines straight to
    # resource-governance.log and writes nothing to stdout — but because
    # `StandardOutput=` is the catch-all for anything the lane might print in
    # future, and retargeting it would be an unrelated change.
    assert (
        "StandardOutput=append:/home/nwm/node27-resource-governance-logs/systemd.log" in text
    )


def test_governance_wrapper_tees_and_keeps_the_audits_exit_code() -> None:
    """`tee` is the only way the transcript and the journal can both be
    complete; `PIPESTATUS[0]` is the only way `tee`'s own status cannot
    masquerade as the audit's.
    """
    text = _GOVERNANCE_WRAPPER_PATH.read_text(encoding="utf-8")

    assert '--summary-path "$SUMMARY_PATH" --quiet 2>&1 | tee -a "$LOG_FILE" >&2' in text
    assert "RC=${PIPESTATUS[0]}" in text
    # The pre-#1765 form must be gone, not merely shadowed: `$?` after a
    # pipeline is `tee`'s status.
    assert "RC=$?" not in text


def test_governance_lock_does_not_live_on_the_root_volume() -> None:
    """The audit's job is to notice `/` filling up. A lock file it cannot
    create because `/` is full would take the audit down with the condition it
    exists to report.
    """
    text = _GOVERNANCE_WRAPPER_PATH.read_text(encoding="utf-8")

    assert (
        'LOCK_PATH="${NODE27_RESOURCE_GOVERNANCE_LOCK_PATH:-$LOG_ROOT/node27-resource-governance.lock}"'
        in text
    )
    assert "/tmp/node27-resource-governance.lock" not in text


# ---------------------------------------------------------------------------
# I6 (#1985) — uncompressed working set and the next-compression peak
# ---------------------------------------------------------------------------

_GIB = 1024**3
_COMPRESSION_ENV_EXAMPLE = _ROOT / "infra/env/node27-timeseries-compression.example"
_GOVERNANCE_ENV_EXAMPLE = _ROOT / "infra/env/node27-resource-governance.example"
_WATERMARK = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)


class _FakeCursor:
    """Dispatches on SQL text, like every other catalog seam in this lane."""

    def __init__(self, *, hypertables: list[dict], chunks: list[dict]) -> None:
        self.hypertables = hypertables
        self.chunks = chunks
        self.executed: list[str] = []
        self._rows: list[dict] = []

    def execute(self, sql: str, params: object = None) -> None:
        self.executed.append(sql)
        self._rows = self.hypertables if "timescaledb_information.hypertables" in sql else self.chunks

    def fetchall(self) -> list[dict]:
        return list(self._rows)


def _hypertable_row(schema: str, name: str, *, num_chunks: int = 1) -> dict:
    return {
        "hypertable_schema": schema,
        "hypertable_name": name,
        "num_chunks": num_chunks,
        "compression_enabled": True,
    }


def _chunk_row(schema: str, name: str, *, day: float, total_bytes: int) -> dict:
    start = _WATERMARK - timedelta(days=day)
    return {
        "hypertable_schema": schema,
        "hypertable_name": name,
        "chunk_schema": "_timescaledb_internal",
        "chunk_name": f"_hyper_{name}_{day}_chunk",
        "range_start": start,
        "range_end": start + timedelta(days=1),
        "total_bytes": total_bytes,
    }


def _working_set(
    *,
    uncompressed_bytes: int,
    daily_ingest_bytes: int | None,
    next_compressible_at: str | None,
    home_free_bytes: int | None,
    projected_peak_bytes: int,
    projection_status: str = "ok",
) -> dict:
    return {
        "uncompressed_bytes": uncompressed_bytes,
        "daily_ingest_bytes": daily_ingest_bytes,
        "next_compressible_at": next_compressible_at,
        "home_free_bytes": home_free_bytes,
        "projected_peak_bytes": projected_peak_bytes,
        "projection_status": projection_status,
        "compression_lag_seconds": collection.DEFAULT_COMPRESSION_LAG_SECONDS,
        "watermark": "2026-09-01T00:00:00Z",
    }


def _codes(receipt: dict) -> dict[str, str]:
    return {
        item["code"]: item["severity"]
        for item in governance._recommendations(receipt, governance.AuditThresholds())
    }


# --- collection -------------------------------------------------------------


def test_working_set_collection_is_catalog_only() -> None:
    """Fixture decision 5 (new seam). The projection may never read a fact
    table: a 600 GB row scan on the volume the audit exists to protect is the
    incident, not the measurement. ``fetch_display_watermark``'s read of the
    metadata table ``hydro.hydro_run`` is a different query and out of scope
    here — this guard covers the working-set SQL this issue adds."""
    for sql in (collection.WORKING_SET_CHUNKS_SQL, collection.WORKING_SET_DISCOVERY_SQL):
        stripped = re.sub(r"'[^']*'", "''", sql)
        assert "hydro." not in stripped, sql
        assert "met." not in stripped, sql
        for relation in re.findall(r"(?is)\bFROM\s+([a-z_][a-z_0-9.]*)", stripped):
            assert relation.startswith(("timescaledb_information.", "pg_")), relation


def test_working_set_sums_uncompressed_chunk_bytes_over_the_discovery_set() -> None:
    cursor = _FakeCursor(
        hypertables=[
            _hypertable_row("hydro", "river_timeseries"),
            _hypertable_row("hydro", "river_timeseries_legacy"),
            _hypertable_row("met", "forcing_station_timeseries"),
        ],
        chunks=[
            _chunk_row("hydro", "river_timeseries", day=1, total_bytes=100),
            _chunk_row("hydro", "river_timeseries_legacy", day=9, total_bytes=400),
            _chunk_row("met", "forcing_station_timeseries", day=2, total_bytes=25),
        ],
    )
    sample = collection.collect_working_set(cursor, watermark=_WATERMARK, lag_seconds=172_800)
    assert sample["uncompressed_bytes"] == 525
    assert sample["hypertables"] == [
        "hydro.river_timeseries",
        "hydro.river_timeseries_legacy",
        "met.forcing_station_timeseries",
    ]
    assert sample["projection_status"] == "ok"
    # Oldest uncompressed chunk's range_end plus the lag.
    oldest_end = _WATERMARK - timedelta(days=9) + timedelta(days=1)
    assert sample["next_compressible_at"] == (oldest_end + timedelta(seconds=172_800)).isoformat().replace(
        "+00:00", "Z"
    )
    assert sample["compression_lag_seconds"] == 172_800


def test_daily_ingest_divides_by_the_days_the_window_actually_covers() -> None:
    """Chunks are attributed to a day by ``range_start``; the divisor is the
    span that table's own in-window chunks COVER (``watermark - that table's
    earliest in-window range_start``, floored at one HOUR), not a fixed seven.
    The seven-day cap is implied by the window bound, so there is no separate
    cap; the floor is sub-day (round-2 review, decision 11).

    Round-1 review: the fixed seven under-reported the rate systematically —
    the first node-27 receipt showed ``daily_ingest_bytes == uncompressed_bytes
    // 7`` byte for byte — and a capacity guard must err high, not low.

    Round-2 review: the window is two-sided (``floor <= range_start <=
    watermark``) and the divisor is floored at an hour, not a day.
    """
    cursor = _FakeCursor(
        hypertables=[
            _hypertable_row("hydro", "river_timeseries"),
            _hypertable_row("met", "forcing_station_timeseries"),
        ],
        chunks=[
            _chunk_row("hydro", "river_timeseries", day=1, total_bytes=700),
            _chunk_row("hydro", "river_timeseries", day=6, total_bytes=700),
            # Older than the trailing seven days: excluded from the rate.
            _chunk_row("hydro", "river_timeseries", day=30, total_bytes=7000),
        ],
    )
    sample = collection.collect_working_set(cursor, watermark=_WATERMARK, lag_seconds=172_800)
    assert sample["uncompressed_bytes"] == 8400
    # In-window chunks start 6 and 1 days before the watermark -> 6 covered days.
    assert sample["daily_ingest_bytes"] == 1400 // 6


def test_daily_ingest_in_steady_state_divides_by_the_uncompressed_span() -> None:
    """Steady state under a 2-day lag: everything older is already compressed,
    so only the last few one-day chunks are uncompressed. Compressed chunks
    inside the seven-day window are not returned by the catalog query at all,
    and must not dilute the rate."""
    cursor = _FakeCursor(
        hypertables=[
            _hypertable_row("hydro", "river_timeseries"),
            _hypertable_row("met", "forcing_station_timeseries"),
        ],
        # 7-day window, 4 older chunks already compressed (absent from the
        # is_compressed=false query), 3 uncompressed one-day chunks left.
        chunks=[
            _chunk_row("hydro", "river_timeseries", day=day, total_bytes=100)
            for day in (1, 2, 3)
        ],
    )
    sample = collection.collect_working_set(cursor, watermark=_WATERMARK, lag_seconds=172_800)
    assert sample["uncompressed_chunks"] == 3
    assert sample["daily_ingest_bytes"] == 300 // 3


def test_the_rate_is_computed_per_table_so_a_wide_chunk_cannot_dilute_it() -> None:
    """Round-3 review, spec D8: ``daily_ingest_bytes`` is computed PER governed
    hypertable and summed.

    A pooled divisor takes ``min(range_start)`` across the WHOLE governed set,
    so one long-span, byte-light table drags every other table's rate down with
    it. Here river writes 60 GiB/day into one-day chunks while forcing holds a
    single six-day-old 7-day chunk of 42 GiB (7 GiB/day). The truth is 67
    GiB/day; pooled it would be ``222 GiB / 6 == 37 GiB/day``, a 45% under-report
    on the guard whose whole job is to see the volume filling.
    """
    cursor = _FakeCursor(
        hypertables=[
            _hypertable_row("hydro", "river_timeseries"),
            _hypertable_row("met", "forcing_station_timeseries"),
        ],
        chunks=[
            _chunk_row("hydro", "river_timeseries", day=3, total_bytes=60 * _GIB),
            _chunk_row("hydro", "river_timeseries", day=2, total_bytes=60 * _GIB),
            _chunk_row("hydro", "river_timeseries", day=1, total_bytes=60 * _GIB),
            _chunk_row("met", "forcing_station_timeseries", day=6, total_bytes=42 * _GIB),
        ],
    )
    sample = collection.collect_working_set(cursor, watermark=_WATERMARK, lag_seconds=172_800)
    assert sample["uncompressed_bytes"] == 222 * _GIB
    # river 180 GiB / 3 d + forcing 42 GiB / 6 d.
    assert sample["daily_ingest_bytes"] == 67 * _GIB
    # The pooled divisor this replaces.
    assert sample["daily_ingest_bytes"] != 222 * _GIB // 6


def test_a_write_frozen_legacy_sibling_does_not_dilute_the_canonical_rate() -> None:
    """The transitional shape this rule exists for (D7 + round-3 review).

    From rename onward the ``_legacy`` sibling is write-frozen: it holds one
    stale 7-day chunk and takes on no new bytes. Pooled, its six-day-old
    ``range_start`` becomes the divisor for the canonical table too, halving the
    reported river rate for the entire expand window — exactly while the volume
    is under the most pressure. Per table, the frozen sibling contributes its own
    (near-zero) rate and nothing else.
    """
    cursor = _FakeCursor(
        hypertables=[
            _hypertable_row("hydro", "river_timeseries"),
            _hypertable_row("hydro", "river_timeseries_legacy"),
            _hypertable_row("met", "forcing_station_timeseries"),
        ],
        chunks=[
            _chunk_row("hydro", "river_timeseries", day=3, total_bytes=60 * _GIB),
            _chunk_row("hydro", "river_timeseries", day=2, total_bytes=60 * _GIB),
            _chunk_row("hydro", "river_timeseries", day=1, total_bytes=60 * _GIB),
            _chunk_row("hydro", "river_timeseries_legacy", day=6, total_bytes=_GIB),
        ],
    )
    sample = collection.collect_working_set(cursor, watermark=_WATERMARK, lag_seconds=172_800)
    assert sample["daily_ingest_bytes"] == 60 * _GIB + int(_GIB / 6)
    # Pooled, the frozen sibling would have cut the canonical rate in half.
    assert sample["daily_ingest_bytes"] > (181 * _GIB) // 6


def test_a_single_fresh_chunk_never_divides_by_a_fraction() -> None:
    """Round-2 review: the floor is ONE HOUR, not one day.

    In the drained steady state the only in-window chunk is the open one, so a
    whole-day floor divides a six-hour-old chunk's bytes by 1.0 instead of 0.25
    and reports a quarter of the real rate — a green flip on exactly the state
    this guard watches. Ten GiB in six hours is 40 GiB/day, and over-reporting
    early in a chunk's life is the fail-safe direction for a capacity guard.
    """
    cursor = _FakeCursor(
        hypertables=[
            _hypertable_row("hydro", "river_timeseries"),
            _hypertable_row("met", "forcing_station_timeseries"),
        ],
        chunks=[_chunk_row("hydro", "river_timeseries", day=0.25, total_bytes=10 * _GIB)],
    )
    sample = collection.collect_working_set(cursor, watermark=_WATERMARK, lag_seconds=172_800)
    assert sample["daily_ingest_bytes"] == 40 * _GIB


def test_the_divisor_floor_is_an_hour_not_a_day() -> None:
    """The floor still exists — a chunk seconds old must not divide by ~0 and
    manufacture an astronomical rate. One hour is the smallest denominator."""
    cursor = _FakeCursor(
        hypertables=[
            _hypertable_row("hydro", "river_timeseries"),
            _hypertable_row("met", "forcing_station_timeseries"),
        ],
        chunks=[_chunk_row("hydro", "river_timeseries", day=0, total_bytes=100)],
    )
    sample = collection.collect_working_set(cursor, watermark=_WATERMARK, lag_seconds=172_800)
    assert sample["daily_ingest_bytes"] == 100 * 24


def test_a_future_chunk_counts_as_stock_but_not_as_ingest() -> None:
    """Round-2 review, decision 18: the rate window is two-sided.

    A chunk whose ``range_start`` is in the FUTURE is a forecast-horizon chunk:
    real bytes on the volume, so it belongs in ``uncompressed_bytes``, but it
    carries no ingest that has happened yet. Counting it in the numerator while
    the divisor spans only observed time inflates the rate — post-expand, with
    one-day chunks and a multi-day forecast horizon, by roughly 4x — and an
    inflated rate is a projection nobody can act on.
    """
    cursor = _FakeCursor(
        hypertables=[
            _hypertable_row("hydro", "river_timeseries"),
            _hypertable_row("met", "forcing_station_timeseries"),
        ],
        chunks=[
            _chunk_row("hydro", "river_timeseries", day=2, total_bytes=200),
            # range_start two days AFTER the watermark.
            _chunk_row("met", "forcing_station_timeseries", day=-2, total_bytes=9000),
        ],
    )
    sample = collection.collect_working_set(cursor, watermark=_WATERMARK, lag_seconds=172_800)
    # Stock: both chunks.
    assert sample["uncompressed_bytes"] == 9200
    assert sample["uncompressed_chunks"] == 2
    # Rate: only the observed one, over the two days it covers.
    assert sample["daily_ingest_bytes"] == 100


def test_an_all_future_working_set_reports_a_zero_rate_not_a_crash() -> None:
    """Degenerate edge of the two-sided window: nothing observed yet. The stock
    is real and the status stays ``ok``; the rate is honestly zero rather than
    a division by an empty span."""
    cursor = _FakeCursor(
        hypertables=[
            _hypertable_row("hydro", "river_timeseries"),
            _hypertable_row("met", "forcing_station_timeseries"),
        ],
        chunks=[_chunk_row("hydro", "river_timeseries", day=-1, total_bytes=500)],
    )
    sample = collection.collect_working_set(cursor, watermark=_WATERMARK, lag_seconds=172_800)
    assert sample["projection_status"] == "ok"
    assert sample["uncompressed_bytes"] == 500
    assert sample["daily_ingest_bytes"] == 0


def test_empty_state_reports_no_uncompressed_chunk_and_projects_nothing() -> None:
    cursor = _FakeCursor(
        hypertables=[
            _hypertable_row("hydro", "river_timeseries"),
            _hypertable_row("met", "forcing_station_timeseries"),
        ],
        chunks=[],
    )
    sample = collection.collect_working_set(cursor, watermark=_WATERMARK, lag_seconds=172_800)
    final = collection.finalize_working_set(sample, home_free_bytes=10 * _GIB)
    assert final["next_compressible_at"] is None
    assert final["projection_status"] == "no_uncompressed_chunk"
    assert final["projected_peak_bytes"] == final["uncompressed_bytes"] == 0


def test_watermark_unavailable_is_a_lane_fault_not_a_zero() -> None:
    cursor = _FakeCursor(
        hypertables=[
            _hypertable_row("hydro", "river_timeseries"),
            _hypertable_row("met", "forcing_station_timeseries"),
        ],
        chunks=[_chunk_row("hydro", "river_timeseries", day=3, total_bytes=64)],
    )
    sample = collection.collect_working_set(cursor, watermark=None, lag_seconds=172_800)
    final = collection.finalize_working_set(sample, home_free_bytes=10 * _GIB)
    assert final["projection_status"] == "watermark_unavailable"
    assert final["watermark"] is None
    assert final["daily_ingest_bytes"] is None
    assert final["projected_peak_bytes"] == final["uncompressed_bytes"] == 64


def test_projection_multiplies_the_rate_by_fractional_days() -> None:
    sample = {
        "uncompressed_bytes": 600 * _GIB,
        "daily_ingest_bytes": 100 * _GIB,
        "next_compressible_at": (_WATERMARK + timedelta(hours=36)).isoformat().replace("+00:00", "Z"),
        "watermark": _WATERMARK.isoformat().replace("+00:00", "Z"),
        "projection_status": "ok",
        "compression_lag_seconds": 172_800,
        "hypertables": [],
        "uncompressed_chunks": 1,
    }
    final = collection.finalize_working_set(sample, home_free_bytes=900 * _GIB)
    assert final["projected_peak_bytes"] == 600 * _GIB + int(1.5 * 100 * _GIB)


def test_projection_never_goes_backwards_for_an_overdue_chunk() -> None:
    """``max(0, ...)``: a chunk already past its compressible time projects the
    working set as it stands, never a negative subtraction."""
    sample = {
        "uncompressed_bytes": 600 * _GIB,
        "daily_ingest_bytes": 100 * _GIB,
        "next_compressible_at": (_WATERMARK - timedelta(days=4)).isoformat().replace("+00:00", "Z"),
        "watermark": _WATERMARK.isoformat().replace("+00:00", "Z"),
        "projection_status": "ok",
        "compression_lag_seconds": 172_800,
        "hypertables": [],
        "uncompressed_chunks": 1,
    }
    final = collection.finalize_working_set(sample, home_free_bytes=900 * _GIB)
    assert final["projected_peak_bytes"] == 600 * _GIB


# --- recommendations: the five scenarios ------------------------------------


def test_scenario_peak_fits() -> None:
    """Spec: 600 GiB working set, 75 GiB/day, two days, 900 GiB free → 750 GiB
    peak, no critical, exit 0 even though database size exceeds 500 GiB."""
    receipt = _base_receipt()
    receipt["postgres"]["database_sizes"] = [{"datname": "nhms", "bytes": 600 * _GIB}]
    receipt["working_set"] = _working_set(
        uncompressed_bytes=600 * _GIB,
        daily_ingest_bytes=75 * _GIB,
        next_compressible_at="2026-09-03T00:00:00Z",
        home_free_bytes=900 * _GIB,
        projected_peak_bytes=750 * _GIB,
    )
    codes = _codes(receipt)
    assert "PROJECTED_PEAK_EXCEEDS_HOME_FREE" not in codes
    assert codes["WORKING_SET_ABOVE_WARNING"] == "warning"
    assert codes["DATABASE_SIZE_ABOVE_CRITICAL"] == "info"
    working_set_codes = {
        "PROJECTED_PEAK_EXCEEDS_HOME_FREE",
        "WORKING_SET_ABOVE_WARNING",
        "WATERMARK_UNAVAILABLE",
        # #1985 round-1/round-2/round-3: the new criticals must stay silent on
        # a healthy measured sample — no phantom code on the fits path.
        "WORKING_SET_UNAVAILABLE",
        "HOME_FREE_UNAVAILABLE",
        "POSTGRES_UNAVAILABLE",
    }
    assert "critical" not in {
        item["severity"]
        for item in governance._recommendations(receipt, governance.AuditThresholds())
        if item["code"] in working_set_codes
    }


def test_scenario_peak_does_not_fit() -> None:
    receipt = _base_receipt()
    receipt["working_set"] = _working_set(
        uncompressed_bytes=600 * _GIB,
        daily_ingest_bytes=75 * _GIB,
        next_compressible_at="2026-09-03T00:00:00Z",
        home_free_bytes=800 * _GIB,
        projected_peak_bytes=750 * _GIB,
    )
    codes = _codes(receipt)
    assert codes["PROJECTED_PEAK_EXCEEDS_HOME_FREE"] == "critical"


def test_scenario_no_uncompressed_chunk_emits_no_critical() -> None:
    receipt = _base_receipt()
    receipt["working_set"] = _working_set(
        uncompressed_bytes=0,
        daily_ingest_bytes=0,
        next_compressible_at=None,
        home_free_bytes=1 * _GIB,
        projected_peak_bytes=0,
        projection_status="no_uncompressed_chunk",
    )
    codes = _codes(receipt)
    assert "PROJECTED_PEAK_EXCEEDS_HOME_FREE" not in codes
    assert "WATERMARK_UNAVAILABLE" not in codes
    assert "WORKING_SET_ABOVE_WARNING" not in codes


def test_an_empty_working_set_with_a_measured_home_still_makes_no_peak_claim() -> None:
    """The trap in widening HOME_FREE_UNAVAILABLE to `no_uncompressed_chunk`.

    Here `/home` IS measured — 1 GiB free, well under the 100 GiB safety
    margin — and `projected_peak_bytes` is 0 because there is nothing left to
    compress. Widening the whole branch instead of just the null-home check
    makes `0 > 1 GiB - 100 GiB` true and fires
    PROJECTED_PEAK_EXCEEDS_HOME_FREE about a compression that will not happen.
    The filesystem block already owns low free space.
    """
    receipt = _base_receipt()
    receipt["working_set"] = _working_set(
        uncompressed_bytes=0,
        daily_ingest_bytes=0,
        next_compressible_at=None,
        home_free_bytes=1 * _GIB,
        projected_peak_bytes=0,
        projection_status="no_uncompressed_chunk",
    )
    codes = _codes(receipt)
    assert "PROJECTED_PEAK_EXCEEDS_HOME_FREE" not in codes
    assert "HOME_FREE_UNAVAILABLE" not in codes


def test_an_unobservable_home_pages_under_the_empty_state_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Round-3 review, decision 19: BOTH measured statuses, not just `ok`.

    `no_uncompressed_chunk` is the state right after a compression tick drains
    the backlog — the catalog answered, the working set is measured, and the
    lane is about to start accumulating again. If `/home` is unobservable then,
    the guard is just as blind as it is under `ok`, and staying silent means
    the volume can fill through the entire next accumulation cycle with a green
    daily audit.
    """
    monkeypatch.setattr(
        governance,
        "collect_filesystem",
        lambda _config: {
            "filesystems": {"home": {"path": "/home", "status": "unavailable"}},
            "path_sizes": {},
        },
    )
    monkeypatch.setattr(governance, "collect_systemd", lambda _services: {"units": []})
    monkeypatch.setattr(
        governance,
        "collect_postgres",
        lambda *_args, **_kwargs: {
            "status": "ok",
            "working_set": {
                "hypertables": ["hydro.river_timeseries", "met.forcing_station_timeseries"],
                "uncompressed_bytes": 0,
                "uncompressed_chunks": 0,
                "daily_ingest_bytes": 0,
                "next_compressible_at": None,
                "compression_lag_seconds": collection.DEFAULT_COMPRESSION_LAG_SECONDS,
                "watermark": "2026-09-01T00:00:00Z",
                "projection_status": "no_uncompressed_chunk",
            },
        },
    )
    summary_path = tmp_path / "resource-governance.json"
    rc = governance.main(["--summary-path", str(summary_path), "--quiet"])
    err = capsys.readouterr().err
    assert rc == 1
    assert f"{_CRITICAL_ANCHOR}HOME_FREE_UNAVAILABLE" in err.splitlines()
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    # The catalog was fine, so the status is NOT downgraded to unavailable.
    assert summary["working_set"]["projection_status"] == "no_uncompressed_chunk"
    assert summary["working_set"]["home_free_bytes"] is None
    # Round-4: the guard's own comment said the status "stays `ok`", which is
    # the single-status claim this test exists to refute — a maintainer reading
    # it would narrow the branch back to `ok` and restore the fail-open.
    source = (Path(__file__).resolve().parents[1] / "scripts" / "node27_resource_governance.py").read_text(
        encoding="utf-8"
    )
    assert "`projection_status` stays `ok`" not in source
    assert "(`ok` or `no_uncompressed_chunk`)" in source


def test_scenario_watermark_unavailable_is_critical() -> None:
    receipt = _base_receipt()
    receipt["working_set"] = _working_set(
        uncompressed_bytes=10,
        daily_ingest_bytes=None,
        next_compressible_at=None,
        home_free_bytes=900 * _GIB,
        projected_peak_bytes=10,
        projection_status="watermark_unavailable",
    )
    codes = _codes(receipt)
    assert codes["WATERMARK_UNAVAILABLE"] == "critical"
    assert "PROJECTED_PEAK_EXCEEDS_HOME_FREE" not in codes


def test_scenario_info_only_database_size_never_exits_non_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`DATABASE_SIZE_ABOVE_CRITICAL` is demoted to info: the crossing it names
    stopped meaning "out of room" the day compression landed, and a daily false
    critical is how the real one gets ignored."""
    receipt = _base_receipt()
    receipt["postgres"]["database_sizes"] = [{"datname": "nhms", "bytes": 900 * _GIB}]
    receipt["filesystem"]["filesystems"]["root"] = {"free_bytes": 500 * _GIB}
    # Drop the unrelated index-ratio hotspot: this row exists to prove that the
    # DATABASE_SIZE_* demotion alone decides the exit code.
    receipt["postgres"]["hypertable_size_breakdown"] = []
    receipt["working_set"] = _working_set(
        uncompressed_bytes=1,
        daily_ingest_bytes=0,
        next_compressible_at="2026-09-03T00:00:00Z",
        home_free_bytes=900 * _GIB,
        projected_peak_bytes=1,
    )
    recommendations = governance._recommendations(receipt, governance.AuditThresholds())
    assert {item["code"]: item["severity"] for item in recommendations}[
        "DATABASE_SIZE_ABOVE_CRITICAL"
    ] == "info"
    full = {
        "schema_version": governance.SCHEMA_VERSION,
        "status": "completed",
        "execution_mode": "read_only_audit",
        "working_set": receipt["working_set"],
        "recommendations": recommendations,
    }
    summary_path = tmp_path / "resource-governance.json"
    rc, out, err = _run_main(monkeypatch, capsys, full, summary_path)
    assert rc == 0
    assert err == ""
    assert out == ""


def test_scenario_working_set_unavailable_is_critical() -> None:
    """Same class as WATERMARK_UNAVAILABLE (decision 12): the projection could
    not be made, which is a lane fault and not a quiet zero."""
    receipt = _base_receipt()
    receipt["working_set"] = {
        "hypertables": [],
        "uncompressed_bytes": None,
        "uncompressed_chunks": None,
        "daily_ingest_bytes": None,
        "next_compressible_at": None,
        "home_free_bytes": 900 * _GIB,
        "projected_peak_bytes": None,
        "projection_status": "working_set_unavailable",
        "compression_lag_seconds": 172_800,
        "watermark": None,
    }
    codes = _codes(receipt)
    assert codes["WORKING_SET_UNAVAILABLE"] == "critical"
    assert "PROJECTED_PEAK_EXCEEDS_HOME_FREE" not in codes
    assert "WATERMARK_UNAVAILABLE" not in codes


def test_a_missing_working_set_block_on_a_healthy_postgres_is_critical() -> None:
    """Round-1 review reproduced the hole: dropping the block entirely (the
    timescale queries raising) exited 0 at HEAD where the same failure exited 1
    at base. With a healthy postgres sample the block MUST be there, so its
    absence is the unavailable state."""
    receipt = _base_receipt()
    receipt.pop("working_set", None)
    assert receipt["postgres"]["status"] == "ok"
    codes = _codes(receipt)
    assert codes["WORKING_SET_UNAVAILABLE"] == "critical"


def test_a_missing_working_set_block_is_silent_when_postgres_was_never_sampled() -> None:
    """Round-3 review: the silent set is exactly ONE reason.

    ``database_url_missing`` is the configured-not-to-look skip — the operator
    told the audit there is no database, so silence is the honest answer and
    exit 0 is correct. Every OTHER "no database" reason is an outage, not a
    skip, and is covered by the parametrised test below.
    """
    receipt = _base_receipt()
    receipt.pop("working_set", None)
    receipt["postgres"] = {"status": "skipped", "reason": "database_url_missing"}
    codes = _codes(receipt)
    assert "WORKING_SET_UNAVAILABLE" not in codes
    assert "POSTGRES_UNAVAILABLE" not in codes


@pytest.mark.parametrize("reason", ["connection_failed", "psycopg2_unavailable"])
def test_an_unreachable_database_is_a_critical_not_a_skip(reason: str) -> None:
    """Round-3 review (spec D8): a database the audit CANNOT reach is not the
    same as one it was told to ignore.

    Before this, both shapes returned no recommendations: the daily tick exited
    0 and mailed nothing while the capacity guard was completely off. A driver
    that failed to import or a refused connection can persist for weeks that
    way — precisely the window in which `/home` fills.
    """
    receipt = _base_receipt()
    receipt.pop("working_set", None)
    receipt["postgres"] = {"status": "blocked", "reason": reason, "error": "boom"}
    codes = _codes(receipt)
    assert codes["POSTGRES_UNAVAILABLE"] == "critical"
    # One finding, not two: there is no working set to also call unavailable.
    assert "WORKING_SET_UNAVAILABLE" not in codes
    assert reason in governance.POSTGRES_UNREACHABLE_REASONS


@pytest.mark.parametrize("reason", ["connection_failed", "psycopg2_unavailable"])
def test_an_unreachable_database_exits_non_zero_with_its_own_stderr_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], reason: str
) -> None:
    """The wire contract, not just the recommendation list: rc 1 plus the
    anchored stderr line the systemd `OnFailure=` handler mails."""
    monkeypatch.setattr(
        governance,
        "collect_filesystem",
        lambda _config: {
            "filesystems": {"home": {"path": "/home", "free_bytes": 10 * _GIB}},
            "path_sizes": {},
        },
    )
    monkeypatch.setattr(
        governance,
        "collect_postgres",
        lambda *_args, **_kwargs: {"status": "blocked", "reason": reason, "error": "boom"},
    )
    monkeypatch.setattr(governance, "collect_systemd", lambda _services: {"units": []})
    summary_path = tmp_path / "resource-governance.json"
    rc = governance.main(["--summary-path", str(summary_path), "--quiet"])
    err = capsys.readouterr().err
    assert rc == 1
    assert f"{_CRITICAL_ANCHOR}POSTGRES_UNAVAILABLE" in err.splitlines()


def test_query_failed_yields_exactly_one_critical() -> None:
    """`query_failed` is `status == "blocked"` too, so keying the new critical
    on the status alone would emit POSTGRES_UNAVAILABLE *and*
    WORKING_SET_UNAVAILABLE for a single fault. The discriminator is the
    reason: the connection succeeded, so the working-set block is present and
    speaks for itself (decision 20)."""
    receipt = _base_receipt()
    receipt["postgres"] = {"status": "blocked", "reason": "query_failed", "error": "boom"}
    receipt["working_set"] = _working_set(
        uncompressed_bytes=0,
        daily_ingest_bytes=None,
        next_compressible_at=None,
        home_free_bytes=None,
        projected_peak_bytes=0,
        projection_status="working_set_unavailable",
    )
    codes = _codes(receipt)
    assert codes["WORKING_SET_UNAVAILABLE"] == "critical"
    assert "POSTGRES_UNAVAILABLE" not in codes
    assert "query_failed" not in governance.POSTGRES_UNREACHABLE_REASONS


def test_query_failed_is_not_in_the_silent_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Round-2 review, decision 20: `collect_postgres`'s OUTER handler keeps the
    working set.

    A `pg_settings` read that raises has nothing to do with the hypertable
    catalog, but the outer `except` used to replace the whole result dict and
    take the projection with it — leaving a receipt that exits 0 while the lane
    has stopped watching /home entirely.
    """

    class _SettingsRaisingCursor(_FakeCursor):
        def execute(self, sql: str, params: object = None) -> None:
            if "pg_settings" in sql:
                raise RuntimeError("permission denied for relation pg_settings")
            super().execute(sql, params)

        def __enter__(self) -> "_SettingsRaisingCursor":
            return self

        def __exit__(self, *args: object) -> bool:
            return False

    cursor = _SettingsRaisingCursor(hypertables=[], chunks=[])

    class _Connection:
        autocommit = False

        def cursor(self) -> _SettingsRaisingCursor:
            return cursor

        def close(self) -> None:
            return None

    fake_psycopg2 = types.SimpleNamespace(
        connect=lambda *_a, **_k: _Connection(),
        extras=types.SimpleNamespace(RealDictCursor=object),
    )
    monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)
    monkeypatch.setitem(sys.modules, "psycopg2.extras", fake_psycopg2.extras)
    monkeypatch.setattr(
        collection, "fetch_display_watermark", lambda _url: datetime(2026, 9, 1, tzinfo=UTC)
    )
    postgres = collection.collect_postgres("postgresql://ro@127.0.0.1/nhms")
    assert postgres["status"] == "blocked"
    assert postgres["reason"] == "query_failed"
    assert postgres["working_set"]["projection_status"] == "working_set_unavailable"

    # ... and the whole audit pages on it.
    summary_path = tmp_path / "resource-governance.json"
    monkeypatch.setattr(
        governance,
        "collect_filesystem",
        lambda _config: {"filesystems": {"home": {"path": "/home", "free_bytes": 10 * _GIB}}, "path_sizes": {}},
    )
    monkeypatch.setattr(governance, "collect_postgres", lambda _url, **_kwargs: postgres)
    monkeypatch.setattr(governance, "collect_systemd", lambda _services: {"units": []})
    rc = governance.main(["--summary-path", str(summary_path), "--quiet"])
    captured = capsys.readouterr()
    assert rc == 1
    assert f"{_CRITICAL_ANCHOR}WORKING_SET_UNAVAILABLE" in captured.err.splitlines()


def test_home_unavailable_with_a_measured_working_set_is_critical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Round-2 review, decision 19: an unobservable `/home` must page.

    The catalog side is fine — `projection_status` stays `ok` — but statvfs on
    `/home` failed, so `home_free_bytes` is null and the
    `projected_peak_bytes > home_free - margin` comparison cannot be made. The
    filesystem block cannot help: `HOME_FREE_BELOW_WARNING` needs a number to
    compare, so without this code the audit says nothing at all about the one
    volume it exists to watch.
    """
    summary_path = tmp_path / "resource-governance.json"
    monkeypatch.setattr(
        governance,
        "collect_filesystem",
        lambda _config: {
            "filesystems": {
                "root": {"path": "/", "free_bytes": 500 * _GIB},
                # Exactly what `disk_usage` returns when statvfs fails.
                "home": {"path": "/home", "status": "unavailable", "error": "[Errno 5] I/O error"},
            },
            "path_sizes": {},
        },
    )
    monkeypatch.setattr(
        governance,
        "collect_postgres",
        lambda _url, **_kwargs: {
            "status": "ok",
            "working_set": {
                "hypertables": ["hydro.river_timeseries", "met.forcing_station_timeseries"],
                "uncompressed_bytes": 10 * _GIB,
                "uncompressed_chunks": 2,
                "daily_ingest_bytes": _GIB,
                "next_compressible_at": "2026-09-03T00:00:00Z",
                "compression_lag_seconds": 172_800,
                "watermark": "2026-09-01T00:00:00Z",
                "projection_status": "ok",
            },
        },
    )
    monkeypatch.setattr(governance, "collect_systemd", lambda _services: {"units": []})

    rc = governance.main(["--summary-path", str(summary_path), "--quiet"])
    captured = capsys.readouterr()

    assert rc == 1
    lines = captured.err.splitlines()
    assert f"{_CRITICAL_ANCHOR}HOME_FREE_UNAVAILABLE" in lines
    # The alert body still carries the numbers the operator needs.
    assert any(line.startswith(governance.WORKING_SET_DIAGNOSTIC_PREFIX) for line in lines)
    written = json.loads(summary_path.read_text(encoding="utf-8"))
    # The catalog was fine; only the volume observation was not.
    assert written["working_set"]["projection_status"] == "ok"
    assert written["working_set"]["home_free_bytes"] is None
    # And the peak comparison is NOT silently reported as fitting.
    assert "PROJECTED_PEAK_EXCEEDS_HOME_FREE" not in _codes(written)


def test_a_measured_home_still_reaches_the_peak_comparison(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard on the guard: `HOME_FREE_UNAVAILABLE` must not shadow the code it
    was inserted in front of."""
    receipt = _base_receipt()
    receipt["working_set"] = _working_set(
        uncompressed_bytes=600 * _GIB,
        daily_ingest_bytes=75 * _GIB,
        next_compressible_at="2026-09-03T00:00:00Z",
        home_free_bytes=400 * _GIB,
        projected_peak_bytes=750 * _GIB,
    )
    codes = _codes(receipt)
    assert codes["PROJECTED_PEAK_EXCEEDS_HOME_FREE"] == "critical"
    assert "HOME_FREE_UNAVAILABLE" not in codes


def test_empty_catalog_is_unavailable_not_no_uncompressed_chunk() -> None:
    """A read-only role that lost `timescaledb_information.*` visibility sees
    zero rows. Reading that as "everything is compressed" is exactly how the
    capacity alarm switches itself off."""
    cursor = _FakeCursor(hypertables=[], chunks=[])
    sample = collection.collect_working_set(cursor, watermark=_WATERMARK, lag_seconds=172_800)
    final = collection.finalize_working_set(sample, home_free_bytes=10 * _GIB)
    assert final["projection_status"] == "working_set_unavailable"
    assert final["uncompressed_bytes"] is None
    assert final["projected_peak_bytes"] is None
    assert _codes({"postgres": {"status": "ok"}, "working_set": final})[
        "WORKING_SET_UNAVAILABLE"
    ] == "critical"


def test_one_missing_canonical_table_is_already_unavailable() -> None:
    """Both canonical tables or nothing: a catalog that reports only river has
    stopped answering for forcing, and half a working set is not a projection."""
    cursor = _FakeCursor(
        hypertables=[_hypertable_row("hydro", "river_timeseries")],
        chunks=[_chunk_row("hydro", "river_timeseries", day=1, total_bytes=64)],
    )
    sample = collection.collect_working_set(cursor, watermark=_WATERMARK, lag_seconds=172_800)
    assert sample["projection_status"] == "working_set_unavailable"
    assert sample["hypertables"] == ["hydro.river_timeseries"]


def test_a_blocked_timescale_block_still_leaves_an_unavailable_working_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`collect_postgres` gives the working set its own try: an inventory query
    that raises must not delete the projection from the receipt."""

    class _RaisingCursor(_FakeCursor):
        def execute(self, sql: str, params: object = None) -> None:
            if "timescaledb_information" in sql:
                raise RuntimeError("permission denied for schema timescaledb_information")
            super().execute(sql, params)

        def __enter__(self) -> "_RaisingCursor":
            return self

        def __exit__(self, *args: object) -> bool:
            return False

    cursor = _RaisingCursor(hypertables=[], chunks=[])

    class _Connection:
        autocommit = False

        def cursor(self) -> _RaisingCursor:
            return cursor

        def close(self) -> None:
            return None

    fake_psycopg2 = types.SimpleNamespace(
        connect=lambda *_a, **_k: _Connection(),
        extras=types.SimpleNamespace(RealDictCursor=object),
    )
    monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)
    monkeypatch.setitem(sys.modules, "psycopg2.extras", fake_psycopg2.extras)
    # `collect_postgres` reaches the watermark through `display_watermark`,
    # which holds its OWN psycopg2 reference: without this stub the test would
    # depend on a real connection to 127.0.0.1 failing, and this suite must
    # never touch a database.
    monkeypatch.setattr(
        collection, "fetch_display_watermark", lambda _url: datetime(2026, 9, 1, tzinfo=UTC)
    )
    result = collection.collect_postgres("postgresql://ro@127.0.0.1/nhms")
    assert result["timescale_status"]["status"] == "blocked"
    assert result["working_set"]["projection_status"] == "working_set_unavailable"
    # The CAUSE rides in the receipt, not only in the status: an operator
    # reading the archived summary must be able to tell a permissions loss from
    # a dropped catalog without re-running the audit. Same precedent as
    # `timescale_status.error`, and deliberately unredacted — the summary is
    # written by `_write_summary` without `redact_payload`, and only catalog
    # `cursor.execute` errors can reach this field (the watermark fetcher
    # collapses every failure to its type name), so it cannot carry a DSN.
    assert result["working_set_error"] == (
        "permission denied for schema timescaledb_information"
    )
    assert _codes({"postgres": {"status": "ok"}, "working_set": result["working_set"]})[
        "WORKING_SET_UNAVAILABLE"
    ] == "critical"


def test_the_runbook_home_free_line_pins_point_at_the_real_code() -> None:
    """Round-4: three of these numbers were one line stale.

    §8's "`/home` has no critical tier" block cites exact line numbers so an
    operator can check the claim without trusting the prose. A line pin that
    drifts is worse than no pin, and it drifts on any insert above it — so the
    numbers are read OUT of the runbook and checked against the file here,
    which is the only way this stays true without anybody remembering to look.
    """
    root = Path(__file__).resolve().parents[1]
    runbook = (root / "docs/runbooks/tier-node27-timeseries-storage.md").read_text(encoding="utf-8")
    flat = " ".join(runbook.split())
    match = re.search(
        r"`HOME_FREE_BELOW_WARNING`, `scripts/node27_resource_governance\.py:(\d+)` "
        r"\(threshold\), `:(\d+)` \(comparison\), `:(\d+)` \(the code literal\)",
        flat,
    )
    assert match is not None, "the §8 /home line-pin sentence changed shape"
    threshold_line, comparison_line, literal_line = (int(group) for group in match.groups())
    lines = (root / "scripts" / "node27_resource_governance.py").read_text(encoding="utf-8").splitlines()
    assert "home_free_warn_bytes: int" in lines[threshold_line - 1], lines[threshold_line - 1]
    assert "thresholds.home_free_warn_bytes" in lines[comparison_line - 1], lines[comparison_line - 1]
    assert '"code": "HOME_FREE_BELOW_WARNING"' in lines[literal_line - 1], lines[literal_line - 1]


def test_the_collection_connect_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """#1985 round-4 (decision 21's rationale, decision 23's lane).

    `nhms-node27-resource-governance.service` is a oneshot with
    `TimeoutStartSec=0`, so nothing outside this call bounds it: an unbounded
    `psycopg2.connect` against a wedged host would hold the unit in
    `activating` and every later timer tick would be skipped, leaving the
    capacity guard off without a single failure to show for it. The kwarg is
    asserted by VALUE against the module constant so a stray `connect_timeout`
    of, say, 300 cannot pass this test.
    """
    recorded: dict[str, object] = {}

    class _Connection:
        autocommit = False

        def cursor(self) -> _FakeCursor:
            raise AssertionError("this test stops at the connect")

        def close(self) -> None:
            return None

    def _connect(*args: object, **kwargs: object) -> _Connection:
        recorded.update(kwargs)
        recorded["args"] = args
        return _Connection()

    fake_psycopg2 = types.SimpleNamespace(
        connect=_connect,
        extras=types.SimpleNamespace(RealDictCursor=object),
    )
    monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)
    monkeypatch.setitem(sys.modules, "psycopg2.extras", fake_psycopg2.extras)
    monkeypatch.setattr(
        collection, "fetch_display_watermark", lambda _url: datetime(2026, 9, 1, tzinfo=UTC)
    )

    result = collection.collect_postgres("postgresql://ro@127.0.0.1/nhms")

    # The cursor stub refuses to work, so the tick reports itself blocked --
    # what matters here is the kwargs the connect was given.
    assert result["status"] == "blocked"
    assert recorded["connect_timeout"] == collection._CONNECT_TIMEOUT_SECONDS == 10
    # The DSN still travels positionally; this pass did not rewrite the call.
    assert recorded["args"] == ("postgresql://ro@127.0.0.1/nhms",)


def test_the_collection_connect_timeout_mirrors_both_lifecycle_runners() -> None:
    """One number, three lanes: a divergence here is a silent policy fork.

    The retention suite owns the runner-to-runner half of this mirror; this
    assertion is the collection module's own end of it, so the constant cannot
    be re-tuned in one lane alone.
    """
    root = Path(__file__).resolve().parents[1]
    for relative in (
        "scripts/node27_timeseries_compression.py",
        "scripts/node27_timeseries_retention.py",
    ):
        source = (root / relative).read_text(encoding="utf-8")
        assert "_CONNECT_TIMEOUT_SECONDS = 10" in source, relative
    assert collection._CONNECT_TIMEOUT_SECONDS == 10


def test_an_unavailable_working_set_pages_through_main_with_a_non_zero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The all-`None` sample has to survive the WHOLE pipeline.

    The other unavailable tests stop at `_codes`; this one drives the sample
    through `_working_set_block` -> `finalize_working_set` -> `main`, because
    an arithmetic slip on a `None` byte count anywhere in that chain would turn
    the page into a crash (or, worse, into silence).
    """
    summary_path = tmp_path / "resource-governance.json"
    thresholds = governance.AuditThresholds()
    monkeypatch.setattr(
        governance,
        "collect_filesystem",
        lambda _config: {
            "filesystems": {
                "root": {"path": "/", "free_bytes": thresholds.root_free_warn_bytes + 1},
                "home": {"path": "/home", "free_bytes": thresholds.home_free_warn_bytes + 1},
            },
            "path_sizes": {},
        },
    )
    monkeypatch.setattr(
        governance,
        "collect_postgres",
        lambda _url, **_kwargs: {
            "status": "ok",
            "working_set": collection.unavailable_working_set(),
        },
    )
    monkeypatch.setattr(governance, "collect_systemd", lambda _services: {"units": []})

    rc = governance.main(["--summary-path", str(summary_path), "--quiet"])
    captured = capsys.readouterr()

    assert rc == 1
    lines = captured.err.splitlines()
    # Exactly the anchor and the payload: one critical, one diagnostic. A third
    # line would mean this state also tripped something else.
    assert lines[0] == f"{_CRITICAL_ANCHOR}WORKING_SET_UNAVAILABLE"
    assert len(lines) == 2
    assert lines[1].startswith(governance.WORKING_SET_DIAGNOSTIC_PREFIX)
    payload = json.loads(lines[1][len(governance.WORKING_SET_DIAGNOSTIC_PREFIX) :])
    # Every number the mail body would quote is null, not zero: the alert says
    # "unknown", never "it fits".
    for field in (
        "uncompressed_bytes",
        "daily_ingest_bytes",
        "next_compressible_at",
        "projected_peak_bytes",
    ):
        assert payload[field] is None, field
    assert payload["projection_status"] == "working_set_unavailable"
    assert payload["compression_lag_seconds"] == governance.DEFAULT_COMPRESSION_LAG_SECONDS
    written = json.loads(summary_path.read_text(encoding="utf-8"))
    assert written["working_set"]["projection_status"] == "working_set_unavailable"
    # Not 0: an unobserved working set must never read as "it fits".
    assert written["working_set"]["projected_peak_bytes"] is None


# --- stderr line ------------------------------------------------------------


def test_critical_run_prints_the_working_set_numbers_on_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The shared unit-failure alert mails the journal tail, so the numbers an
    operator needs must be ON stderr. The per-code anchor line keeps its exact
    byte shape (other tooling greps it); the numbers ride on their own line."""
    summary_path = tmp_path / "resource-governance.json"
    receipt = _receipt_with(_recommendation("critical", "PROJECTED_PEAK_EXCEEDS_HOME_FREE"))
    receipt["working_set"] = _working_set(
        uncompressed_bytes=600 * _GIB,
        daily_ingest_bytes=75 * _GIB,
        next_compressible_at="2026-09-03T00:00:00Z",
        home_free_bytes=800 * _GIB,
        projected_peak_bytes=750 * _GIB,
    )
    rc, _out, err = _run_main(monkeypatch, capsys, receipt, summary_path)
    assert rc == 1
    lines = err.splitlines()
    assert lines[0] == f"{_CRITICAL_ANCHOR}PROJECTED_PEAK_EXCEEDS_HOME_FREE"
    assert len(lines) == 2
    assert lines[1].startswith(governance.WORKING_SET_DIAGNOSTIC_PREFIX)
    payload = json.loads(lines[1][len(governance.WORKING_SET_DIAGNOSTIC_PREFIX) :])
    assert payload["projected_peak_bytes"] == 750 * _GIB
    assert payload["home_free_bytes"] == 800 * _GIB
    assert payload["next_compressible_at"] == "2026-09-03T00:00:00Z"
    assert payload["uncompressed_bytes"] == 600 * _GIB
    assert payload["compression_lag_seconds"] == 172_800


def test_a_run_without_criticals_stays_silent_even_with_a_working_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    summary_path = tmp_path / "resource-governance.json"
    receipt = _receipt_with(_recommendation("warning", "WORKING_SET_ABOVE_WARNING"))
    receipt["working_set"] = _working_set(
        uncompressed_bytes=600 * _GIB,
        daily_ingest_bytes=75 * _GIB,
        next_compressible_at="2026-09-03T00:00:00Z",
        home_free_bytes=900 * _GIB,
        projected_peak_bytes=750 * _GIB,
    )
    rc, out, err = _run_main(monkeypatch, capsys, receipt, summary_path)
    assert rc == 0
    assert err == ""
    assert out == ""


# --- lag configuration and the template cross-pin ---------------------------


def test_governance_lag_default_equals_the_compression_template_lag() -> None:
    """Two templates, one number. Nothing in a unit test can see the DEPLOYED
    node-27 values, so this pin covers template↔template only; the deployed
    drift is caught by the rollout receipt comparing the echoed
    ``compression_lag_seconds`` with the live compression env."""
    text = _COMPRESSION_ENV_EXAMPLE.read_text(encoding="utf-8")
    template = re.findall(r"(?m)^NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS=(\d+)$", text)
    assert template == ["172800"]
    assert collection.DEFAULT_COMPRESSION_LAG_SECONDS == int(template[0])


def test_governance_env_example_carries_the_lag_and_threshold_variables() -> None:
    text = _GOVERNANCE_ENV_EXAMPLE.read_text(encoding="utf-8")
    assert re.search(r"(?m)^NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS=172800$", text)
    assert re.search(r"(?m)^NODE27_GOVERNANCE_SAFETY_MARGIN_BYTES=\d+$", text)
    assert re.search(r"(?m)^NODE27_GOVERNANCE_WORKING_SET_WARN_BYTES=\d+$", text)
    # Round-3 review: the template used to say "A missing or unparseable value
    # is a configuration error", which is false for the missing half — an
    # absent variable resolves to the code default (pinned by
    # `test_absent_lag_env_falls_back_to_the_pinned_default`). Only an
    # EMPTY or unparseable value refuses. An operator who trusted the old
    # sentence would have added the line to silence a refusal that never comes.
    assert "A missing or unparseable value" not in text
    lag_comment = text.split("NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS=")[0]
    assert "EMPTY or unparseable" in lag_comment
    assert "absent" in lag_comment.lower()
    assert "172800" in lag_comment


def test_default_thresholds_are_the_decided_values() -> None:
    thresholds = governance.AuditThresholds()
    assert thresholds.safety_margin_bytes == 100 * _GIB
    assert thresholds.working_set_warn_bytes == 400 * _GIB


def test_operator_can_override_margin_warning_and_lag() -> None:
    args = governance.build_parser().parse_args(
        [
            "--safety-margin-bytes",
            str(50 * _GIB),
            "--working-set-warn-bytes",
            str(200 * _GIB),
            "--compression-lag-seconds",
            "604800",
        ]
    )
    config = governance.config_from_args(args)
    assert config.thresholds.safety_margin_bytes == 50 * _GIB
    assert config.thresholds.working_set_warn_bytes == 200 * _GIB
    assert config.compression_lag_seconds == 604_800


@pytest.mark.parametrize("value", ["", "0", "-1", "not-a-number"])
def test_an_unparseable_lag_is_a_config_error_with_a_non_zero_exit(
    value: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS", value)
    rc = governance.main(["--quiet"])
    captured = capsys.readouterr()
    assert rc != 0
    assert json.loads(captured.err.splitlines()[0])["status"] == "failed"


def test_absent_lag_env_falls_back_to_the_pinned_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UNSET is the normal state on a box whose env file predates #1985, and it
    must resolve to 172800 — the same number the compression template ships."""
    monkeypatch.delenv("NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS", raising=False)
    config = governance.config_from_args(governance.build_parser().parse_args([]))
    assert config.compression_lag_seconds == 172_800
    assert config.compression_lag_seconds == collection.DEFAULT_COMPRESSION_LAG_SECONDS


def test_an_invalid_lag_is_refused_in_seconds_not_bytes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The lag is a duration; telling an operator it "must be an integer byte
    count" points at the wrong unit. Both entry points are checked: the flag
    (argparse turns the type error into a usage message) and the env default
    (evaluated while the parser is built)."""
    monkeypatch.delenv("NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS", raising=False)
    with pytest.raises(SystemExit):
        governance.build_parser().parse_args(["--compression-lag-seconds", "2d"])
    message = capsys.readouterr().err
    assert "must be an integer number of seconds" in message
    assert "byte count" not in message

    monkeypatch.setenv("NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS", "2d")
    with pytest.raises(argparse.ArgumentTypeError) as excinfo:
        governance.build_parser()
    assert "must be an integer number of seconds" in str(excinfo.value)
    assert "byte count" not in str(excinfo.value)


def test_receipt_echoes_the_configured_lag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS", "259200")
    args = governance.build_parser().parse_args([])
    assert governance.config_from_args(args).compression_lag_seconds == 259_200
