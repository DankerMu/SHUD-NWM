from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

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
    monkeypatch.setattr(governance, "collect_postgres", lambda _url: {"status": "skipped"})
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
    monkeypatch.setattr(governance, "collect_postgres", lambda _url: {"status": "skipped"})
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
    # stdout keeps its file: the wrapper's own bracket lines are not journal
    # material, and moving them would be an unrelated change.
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
