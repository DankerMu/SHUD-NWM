from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

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


def test_summary_sink_redacts_path_device_and_error_evidence(tmp_path: Path) -> None:
    output = tmp_path / "receipt.json"
    governance._write_summary(
        output,
        {
            "working_set": {
                "working_set_free_bytes": None,
                "working_set_filesystem": {
                    "path": "/srv/password=path-secret",
                    "device_identity": "password=device-secret",
                    "status": "unavailable",
                    "blockers": ["PGDATA_FILESYSTEM_UNAVAILABLE"],
                },
            },
            "filesystem": {"error": "postgresql://user:driver-secret@example/db?password=query-secret"},
        },
    )
    text = output.read_text()
    for secret in ("path-secret", "device-secret", "driver-secret", "query-secret"):
        assert secret not in text
    receipt = json.loads(text)
    assert receipt["working_set"]["working_set_filesystem"]["status"] == "unavailable"
    assert receipt["working_set"]["working_set_free_bytes"] is None


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
    from packages.common import node27_resource_governance_collection as collection

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


@pytest.mark.parametrize("fallback", (False, True), ids=("gnu-du", "portable-du"))
def test_du_observation_resolves_symlink_and_identifies_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fallback: bool
) -> None:
    import os
    import subprocess

    from packages.common.node27_resource_governance_collection import disk_usage, du_bytes

    target = tmp_path / "actual-pgdata"
    target.mkdir()
    alias = tmp_path / "pgdata"
    alias.symlink_to(target, target_is_directory=True)

    def run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        if fallback and "-B1" in args:
            return subprocess.CompletedProcess(args, 1, "", "unsupported option")
        return subprocess.CompletedProcess(args, 0, f"{4 if fallback else 4096}\t{target}", "")

    monkeypatch.setattr(subprocess, "run", run)
    observed = du_bytes(alias)
    stat = target.stat()
    usage = os.statvfs(target)
    assert observed["path"] == str(target)
    assert observed["bytes"] == 4096
    assert observed["device_identity"] == f"{os.major(stat.st_dev)}:{os.minor(stat.st_dev)}:{usage.f_fsid}"
    assert observed["device_identity"] == disk_usage(target)["device_identity"]


def test_removed_cold_receipt_argument_refuses_before_audit(tmp_path: Path) -> None:
    output = tmp_path / "cold-governance.json"
    with pytest.raises(SystemExit) as error:
        governance.main(["--cold-governance-receipt-path", str(output)])
    assert error.value.code == 2
    assert not output.exists()


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


def test_default_services_includes_coverage_freshness_alert_units() -> None:
    # #2466 registers the coverage freshness alert service + timer, the sibling
    # of the #1368 frontier pair above. The same argument carries: this lane is
    # the only observer of a coverage stall, and a disabled timer is silent in
    # exactly the way the lane exists to remove — so its liveness has to be
    # visible in the governance oracle, not only in the absence of mail.
    expected = {
        "nhms-node27-coverage-freshness-alert.service",
        "nhms-node27-coverage-freshness-alert.timer",
    }
    assert expected.issubset(set(governance.DEFAULT_SERVICES))


def test_collect_systemd_receipt_includes_coverage_freshness_alert_units(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2466 registration proven end-to-end through the collector (mocked
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
        "nhms-node27-coverage-freshness-alert.service",
        "nhms-node27-coverage-freshness-alert.timer",
    ):
        assert unit in services
        assert services[unit]["command"]["status"] == "ok"
        assert services[unit]["properties"].get("Id") == "stub"


_COVERAGE_TIMER = "nhms-node27-coverage-freshness-alert.timer"

#: `systemctl --user show` replies measured on node-27 (2026-09-18) with a
#: throwaway unit: a timer disabled with `disable --now` and one that was never
#: installed read the SAME `ActiveState=inactive` / `SubState=dead`; only
#: `LoadState` and `UnitFileState` tell them apart. `list-timers --all` shows
#: neither of them.
_ENABLED_TIMER_REPLY = "Id=stub\nLoadState=loaded\nActiveState=active\nSubState=waiting\nUnitFileState=enabled\n"
_DISABLED_TIMER_REPLY = "Id=stub\nLoadState=loaded\nActiveState=inactive\nSubState=dead\nUnitFileState=disabled\n"
_NEVER_INSTALLED_REPLY = "Id=stub\nLoadState=not-found\nActiveState=inactive\nSubState=dead\nUnitFileState=\n"


def _fake_systemctl(show_replies: dict[str, str], calls: list[list[str]]):
    """`_run_command` stand-in: `show <unit>` answers from `show_replies`, every
    other call (the `list-timers` table) answers empty. Records every argv."""

    def _fake_run_command(args, *, timeout: int = 20) -> dict:
        argv = list(args)
        calls.append(argv)
        stdout = ""
        if "show" in argv:
            stdout = show_replies.get(argv[argv.index("show") + 1], _ENABLED_TIMER_REPLY)
        return {"status": "ok", "return_code": 0, "stdout": stdout, "stderr": "", "args": argv}

    return _fake_run_command


def test_collect_systemd_probes_load_and_unit_file_state_for_every_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2473: "disabled" and "never installed" are distinguishable in the
    receipt only through `LoadState` + `UnitFileState` (measured, see the
    replies above), so the collector must ask systemctl for both, for EVERY
    inventoried unit, and the answers must land in `properties`."""

    calls: list[list[str]] = []
    replies = {
        _COVERAGE_TIMER: _DISABLED_TIMER_REPLY,
        "nhms-node27-frontier-alert.timer": _NEVER_INSTALLED_REPLY,
    }
    monkeypatch.setattr(governance, "_run_command", _fake_systemctl(replies, calls))

    payload = governance.collect_systemd(governance.DEFAULT_SERVICES)

    show_calls = {argv[argv.index("show") + 1]: argv for argv in calls if "show" in argv}
    assert set(show_calls) == set(governance.DEFAULT_SERVICES)
    for unit, argv in show_calls.items():
        requested = {argv[index + 1] for index, token in enumerate(argv) if token == "-p"}
        assert {"LoadState", "UnitFileState"} <= requested, unit

    services = payload["services"]
    disabled = services[_COVERAGE_TIMER]["properties"]
    assert (disabled["LoadState"], disabled["UnitFileState"]) == ("loaded", "disabled")
    absent = services["nhms-node27-frontier-alert.timer"]["properties"]
    assert (absent["LoadState"], absent["UnitFileState"]) == ("not-found", "")
    # The pair that could NOT tell them apart stays identical — this is why the
    # two new properties are needed at all.
    assert (disabled["ActiveState"], disabled["SubState"]) == (absent["ActiveState"], absent["SubState"])


def _audit_with_coverage_timer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    timer_reply: str,
    label: str,
) -> tuple[int, dict]:
    """One full audit through `main` → the real `build_receipt` → the real
    `collect_systemd` and `_recommendations`. Only the host probes are stubbed:
    filesystem, postgres and working set are fixed (the `_base_receipt` risks,
    so recommendations and a critical exit are non-trivially present), and
    systemctl answers from `_fake_systemctl` with the coverage timer set to
    `timer_reply`."""

    base = _base_receipt()
    working_set = {
        "projection_status": "no_uncompressed_chunk",
        "working_set_filesystem": {"status": "ok", "path": "/data/GHDC", "device_identity": "8:12", "blockers": []},
        "working_set_free_bytes": 900 * governance.GIB,
        "uncompressed_bytes": 1,
    }
    monkeypatch.setattr(governance, "collect_filesystem", lambda _config: json.loads(json.dumps(base["filesystem"])))
    monkeypatch.setattr(governance, "collect_postgres", lambda _url: json.loads(json.dumps(base["postgres"])))
    monkeypatch.setattr(governance, "collect_working_set", lambda *_a, **_k: dict(working_set))
    monkeypatch.setattr(governance, "_run_command", _fake_systemctl({_COVERAGE_TIMER: timer_reply}, []))

    summary_path = tmp_path / f"governance-{label}.json"
    rc = governance.main(["--summary-path", str(summary_path), "--quiet"])
    capsys.readouterr()
    return rc, json.loads(summary_path.read_text(encoding="utf-8"))


def test_disabling_a_registered_timer_is_visibility_not_an_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Spec scenario "Registration is visibility, not an alert" (#2473).

    Characterization pin: `_recommendations` never reads the `systemd` section,
    so a disabled coverage timer changes the receipt but neither the
    recommendations nor the audit's exit code. The runbook (§10.8, §11.5) says
    exactly this; if the audit ever starts alerting on it, the docs must move
    with it and this test is where that shows up.
    """

    enabled_rc, enabled = _audit_with_coverage_timer(monkeypatch, tmp_path, capsys, _ENABLED_TIMER_REPLY, "enabled")
    disabled_rc, disabled = _audit_with_coverage_timer(monkeypatch, tmp_path, capsys, _DISABLED_TIMER_REPLY, "disabled")

    # The input really differed, and it reached the receipt.
    enabled_timer = enabled["systemd"]["services"][_COVERAGE_TIMER]["properties"]
    disabled_timer = disabled["systemd"]["services"][_COVERAGE_TIMER]["properties"]
    assert (enabled_timer["UnitFileState"], enabled_timer["ActiveState"]) == ("enabled", "active")
    assert (disabled_timer["UnitFileState"], disabled_timer["ActiveState"]) == ("disabled", "inactive")

    assert enabled["recommendations"], "fixture must produce recommendations, or the comparison is vacuous"
    assert disabled["recommendations"] == enabled["recommendations"]
    assert disabled_rc == enabled_rc
    assert governance._critical_codes(disabled) == governance._critical_codes(enabled)


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
        _recommendation("critical", "PROJECTED_PEAK_EXCEEDS_HOME_FREE"),
    )

    rc, _out, err = _run_main(monkeypatch, capsys, receipt, summary_path)

    assert rc == 1
    assert [line for line in err.splitlines() if line.startswith(_CRITICAL_ANCHOR)] == [
        f"{_CRITICAL_ANCHOR}ROOT_FREE_BELOW_CRITICAL",
        f"{_CRITICAL_ANCHOR}PROJECTED_PEAK_EXCEEDS_HOME_FREE",
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
    assert "StandardOutput=append:/home/nwm/node27-resource-governance-logs/systemd.log" in text


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

    assert 'LOCK_PATH="${NODE27_RESOURCE_GOVERNANCE_LOCK_PATH:-$LOG_ROOT/node27-resource-governance.lock}"' in text
    assert "/tmp/node27-resource-governance.lock" not in text


# ---------------------------------------------------------------------------
# #1769 / #1770 — autovacuum and autoanalyze judged by output, not configuration
# ---------------------------------------------------------------------------

_HOUR = 3600.0
_DAY = 86400.0
_MAINTENANCE_CODES = {
    "TABLE_STATISTICS_STALE",
    "TABLE_VACUUM_DEBT_STALE",
    "AUTOVACUUM_OUTPUT_STALLED",
    "MAINTENANCE_OUTPUT_UNAVAILABLE",
    "TABLE_ZERO_STATISTICS",
}
# The pre-change recommendation sequence of `_base_receipt()`, read off the
# existing builder; the maintenance check must append, never reorder or drop.
_BASE_RECEIPT_EXISTING_CODES = [
    "WORKING_SET_FILESYSTEM_UNAVAILABLE",
    "PGDATA_USAGE_UNAVAILABLE",
    "ROOT_FREE_BELOW_CRITICAL",
    "HOME_FREE_BELOW_WARNING",
    "DATABASE_SIZE_ABOVE_WARNING",
    "TEMP_SPILL_LOGGING_DISABLED",
    "TIMESCALE_RETENTION_POLICY_MISSING",
    "TIMESCALE_COMPRESSION_POLICY_MISSING",
    "HYPERTABLE_INDEX_RATIO_HIGH",
    "DEAD_TUPLE_HOTSPOT",
]


def _maintenance_row(schema: str = "core", relation: str = "river_segment", **fields: object) -> dict:
    row: dict = {
        "schema": schema,
        "relation": relation,
        "relpages": 100,
        "reltuples": 1000.0,
        "n_live_tup": 1000,
        "n_dead_tup": 0,
        "n_mod_since_analyze": 0,
        "vacuum_threshold": 250.0,
        "analyze_threshold": 150.0,
        "last_autovacuum_age_seconds": 600.0,
        "last_vacuum_age_seconds": None,
        "last_autoanalyze_age_seconds": 600.0,
        "last_analyze_age_seconds": None,
    }
    row.update(fields)
    return row


def _river_segment_1769(**fields: object) -> dict:
    # reloptions threshold 500 / scale 0.01 over reltuples 209126 -> 2591.26.
    historical = {
        "relpages": 127782,
        "reltuples": 209126.0,
        "n_live_tup": 209126,
        "n_mod_since_analyze": 94380,
        "analyze_threshold": 500 + 0.01 * 209126,
        "vacuum_threshold": 50 + 0.2 * 209126,
        "last_autoanalyze_age_seconds": None,
        "last_analyze_age_seconds": 3.5 * _DAY,
        "last_autovacuum_age_seconds": 10 * 60.0,
    }
    return _maintenance_row(**{**historical, **fields})


def _maintenance_section(rows: list, *, max_autoanalyze: object = 600.0, max_autovacuum: object = 600.0) -> dict:
    return {
        "status": "ok",
        "summary": {
            "max_last_autoanalyze_age_seconds": max_autoanalyze,
            "max_last_autovacuum_age_seconds": max_autovacuum,
            "over_vacuum_threshold_count": 0,
            "over_analyze_threshold_count": 0,
            "relation_count": 80,
        },
        "rows": rows,
    }


def _healthy_receipt(maintenance_output: object = None, *, postgres_status: str = "ok") -> dict:
    """A receipt with no pre-existing finding, so maintenance findings stand alone."""
    postgres: dict = {"status": postgres_status}
    if maintenance_output is not None:
        postgres["maintenance_output"] = maintenance_output
    return {
        "schema_version": governance.SCHEMA_VERSION,
        "status": "completed",
        "execution_mode": "read_only_audit",
        "filesystem": {
            "filesystems": {"root": {"free_bytes": 900 * governance.GIB}, "home": {"free_bytes": 900 * governance.GIB}},
            "path_sizes": {"pgdata_root": {"status": "ok", "bytes": 10}},
        },
        "working_set": {
            "projection_status": "ok",
            "working_set_filesystem": {"status": "ok", "path": "/data/GHDC", "device_identity": "8:12", "blockers": []},
            "working_set_free_bytes": 900 * governance.GIB,
            "uncompressed_bytes": 1,
            "projected_peak_bytes": 1,
        },
        "postgres": postgres,
    }


def _maintenance_findings(receipt: dict) -> list[dict]:
    return [
        item
        for item in governance._recommendations(receipt, governance.AuditThresholds())
        if item["code"] in _MAINTENANCE_CODES
    ]


def test_maintenance_thresholds_default_to_the_1769_criterion() -> None:
    thresholds = governance.AuditThresholds()
    assert thresholds.maintenance_stale_multiplier == 10
    assert thresholds.maintenance_stale_age_seconds == 86400


def test_healthy_receipt_has_no_recommendation_at_all() -> None:
    receipt = _healthy_receipt(_maintenance_section([]))
    assert governance._recommendations(receipt, governance.AuditThresholds()) == []


def test_1769_river_segment_row_is_statistics_stale() -> None:
    findings = _maintenance_findings(_healthy_receipt(_maintenance_section([_river_segment_1769()])))

    assert [(item["severity"], item["code"]) for item in findings] == [("warning", "TABLE_STATISTICS_STALE")]
    evidence = findings[0]["evidence"]
    assert evidence["relation"] == "core.river_segment"
    assert evidence["ratio"] == 36.4
    assert evidence["n_mod_since_analyze"] == 94380
    assert evidence["last_autoanalyze_age_seconds"] is None
    assert evidence["last_analyze_age_seconds"] == 3.5 * _DAY


def test_decimal_database_values_are_judged_like_floats() -> None:
    row = _river_segment_1769(
        reltuples=Decimal("209126"),
        n_mod_since_analyze=Decimal("94380"),
        analyze_threshold=Decimal("2591.26"),
        last_analyze_age_seconds=Decimal("302400.5"),
    )
    findings = _maintenance_findings(_healthy_receipt(_maintenance_section([row])))

    assert [item["code"] for item in findings] == ["TABLE_STATISTICS_STALE"]
    assert findings[0]["evidence"]["ratio"] == 36.4


def test_freshly_autoanalyzed_row_has_no_finding() -> None:
    row = _river_segment_1769(n_mod_since_analyze=0, last_autoanalyze_age_seconds=10 * 60.0)
    assert _maintenance_findings(_healthy_receipt(_maintenance_section([row]))) == []


def test_recent_output_suppresses_the_warning_even_when_counts_are_high() -> None:
    row = _river_segment_1769(last_autoanalyze_age_seconds=2 * _HOUR)
    assert _maintenance_findings(_healthy_receipt(_maintenance_section([row]))) == []


def test_manually_analyzed_table_that_is_stale_again_still_warns() -> None:
    row = _maintenance_row(
        relation="model_instance",
        analyze_threshold=100.0,
        n_mod_since_analyze=2000,
        last_autoanalyze_age_seconds=None,
        last_analyze_age_seconds=5 * _DAY,
    )
    findings = _maintenance_findings(_healthy_receipt(_maintenance_section([row])))

    assert [(item["severity"], item["code"]) for item in findings] == [("warning", "TABLE_STATISTICS_STALE")]
    assert findings[0]["evidence"]["ratio"] == 20.0


def _met_station_0820() -> dict:
    vacuum_threshold = 50 + 0.2 * 42029
    return _maintenance_row(
        schema="met",
        relation="met_station",
        reltuples=42029.0,
        n_live_tup=42029,
        n_dead_tup=round(65.9 * vacuum_threshold),
        vacuum_threshold=vacuum_threshold,
        last_autovacuum_age_seconds=67 * _HOUR,
        last_autoanalyze_age_seconds=61 * _HOUR,
    )


def test_0820_database_wide_silence_is_critical_and_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    receipt = _healthy_receipt(
        _maintenance_section([_met_station_0820()], max_autoanalyze=61 * _HOUR, max_autovacuum=67 * _HOUR)
    )
    receipt["recommendations"] = governance._recommendations(receipt, governance.AuditThresholds())
    findings = [item for item in receipt["recommendations"] if item["code"] in _MAINTENANCE_CODES]

    assert [(item["severity"], item["code"]) for item in findings] == [
        ("warning", "TABLE_VACUUM_DEBT_STALE"),
        ("critical", "AUTOVACUUM_OUTPUT_STALLED"),
    ]
    assert findings[0]["evidence"]["relation"] == "met.met_station"
    assert findings[0]["evidence"]["ratio"] == 65.9
    assert findings[1]["evidence"]["stalled_outputs"] == ["autovacuum"]
    assert findings[1]["evidence"]["max_last_autovacuum_age_seconds"] == 67 * _HOUR

    summary_path = tmp_path / "resource-governance.json"
    rc, out, err = _run_main(monkeypatch, capsys, receipt, summary_path)
    assert rc == 1
    assert err.splitlines() == [f"{_CRITICAL_ANCHOR}AUTOVACUUM_OUTPUT_STALLED"]
    assert out == ""
    assert json.loads(summary_path.read_text(encoding="utf-8"))["status"] == "completed"


def test_stale_warning_with_fresh_output_maxima_is_warning_only_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    receipt = _healthy_receipt(
        _maintenance_section([_met_station_0820()], max_autoanalyze=10 * 60.0, max_autovacuum=10 * 60.0)
    )
    receipt["recommendations"] = governance._recommendations(receipt, governance.AuditThresholds())

    assert [(item["severity"], item["code"]) for item in receipt["recommendations"]] == [
        ("warning", "TABLE_VACUUM_DEBT_STALE")
    ]
    rc, _out, err = _run_main(monkeypatch, capsys, receipt, tmp_path / "resource-governance.json")
    assert rc == 0
    assert err == ""


def test_autoanalyze_only_silence_is_critical_while_autovacuum_runs() -> None:
    receipt = _healthy_receipt(
        _maintenance_section([_river_segment_1769()], max_autoanalyze=61 * _HOUR, max_autovacuum=10 * 60.0)
    )
    findings = _maintenance_findings(receipt)

    assert [(item["severity"], item["code"]) for item in findings] == [
        ("warning", "TABLE_STATISTICS_STALE"),
        ("critical", "AUTOVACUUM_OUTPUT_STALLED"),
    ]
    assert findings[1]["evidence"]["stalled_outputs"] == ["autoanalyze"]


def test_vacuum_silence_alone_does_not_escalate_analyze_stale_rows() -> None:
    receipt = _healthy_receipt(
        _maintenance_section([_river_segment_1769()], max_autoanalyze=10 * 60.0, max_autovacuum=61 * _HOUR)
    )
    assert [item["code"] for item in _maintenance_findings(receipt)] == ["TABLE_STATISTICS_STALE"]


def test_never_produced_output_maxima_count_as_silent() -> None:
    receipt = _healthy_receipt(_maintenance_section([_met_station_0820()], max_autoanalyze=None, max_autovacuum=None))
    assert [item["severity"] for item in _maintenance_findings(receipt)] == ["warning", "critical"]


def test_core_basin_zero_statistics_is_info_only() -> None:
    row = _maintenance_row(
        relation="basin",
        relpages=0,
        reltuples=-1.0,
        n_live_tup=18,
        n_dead_tup=0,
        n_mod_since_analyze=19,
        vacuum_threshold=50.0,
        analyze_threshold=50.0,
        last_autovacuum_age_seconds=None,
        last_autoanalyze_age_seconds=None,
        last_analyze_age_seconds=None,
    )
    findings = _maintenance_findings(
        _healthy_receipt(_maintenance_section([row], max_autoanalyze=61 * _HOUR, max_autovacuum=67 * _HOUR))
    )

    assert [(item["severity"], item["code"]) for item in findings] == [("info", "TABLE_ZERO_STATISTICS")]
    assert findings[0]["evidence"]["relation"] == "core.basin"


def test_probe_error_is_exactly_one_unavailable_warning_and_keeps_existing_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    error_section = {"status": "error", "error": "QueryCanceled"}
    base = _base_receipt()
    base["postgres"]["maintenance_output"] = error_section
    codes = [item["code"] for item in governance._recommendations(base, governance.AuditThresholds())]

    assert codes == [*_BASE_RECEIPT_EXISTING_CODES, "MAINTENANCE_OUTPUT_UNAVAILABLE"]

    receipt = _healthy_receipt(error_section)
    receipt["recommendations"] = governance._recommendations(receipt, governance.AuditThresholds())
    assert receipt["recommendations"] == [
        {
            "severity": "warning",
            "area": "postgres",
            "code": "MAINTENANCE_OUTPUT_UNAVAILABLE",
            "evidence": {"status": "error", "error": "QueryCanceled"},
            "action": receipt["recommendations"][0]["action"],
        }
    ]
    rc, _out, err = _run_main(monkeypatch, capsys, receipt, tmp_path / "resource-governance.json")
    assert rc == 0
    assert err == ""


def test_healthy_probe_leaves_existing_code_sequence_unchanged() -> None:
    base = _base_receipt()
    base["postgres"]["maintenance_output"] = _maintenance_section([])
    codes = [item["code"] for item in governance._recommendations(base, governance.AuditThresholds())]
    assert codes == _BASE_RECEIPT_EXISTING_CODES


@pytest.mark.parametrize(
    "section",
    (
        None,
        "not-a-mapping",
        {"status": "ok", "rows": []},
        {"status": "ok", "summary": {}, "rows": []},
        {
            "status": "ok",
            "summary": {"max_last_autoanalyze_age_seconds": "1h", "max_last_autovacuum_age_seconds": 1.0},
            "rows": [],
        },
        {
            "status": "ok",
            "summary": {"max_last_autoanalyze_age_seconds": 1.0, "max_last_autovacuum_age_seconds": 1.0},
            "rows": "not-a-list",
        },
        {"status": "skipped"},
    ),
    ids=("missing", "non-mapping", "no-summary", "empty-summary", "string-age", "rows-not-list", "status-not-ok"),
)
def test_malformed_or_missing_probe_is_unavailable_not_green(section: object) -> None:
    findings = _maintenance_findings(_healthy_receipt(section))
    assert [(item["severity"], item["code"]) for item in findings] == [("warning", "MAINTENANCE_OUTPUT_UNAVAILABLE")]


def test_postgres_not_ok_emits_no_maintenance_finding() -> None:
    receipt = _healthy_receipt({"status": "error", "error": "QueryCanceled"}, postgres_status="blocked")
    assert _maintenance_findings(receipt) == []


def test_malformed_rows_are_skipped_without_raising() -> None:
    rows = [
        "not-a-mapping",
        _maintenance_row(schema=None),
        _river_segment_1769(n_mod_since_analyze="94380"),
        _river_segment_1769(analyze_threshold=None),
        _river_segment_1769(last_analyze_age_seconds="3.5 days"),
        _river_segment_1769(n_mod_since_analyze=True),
        _river_segment_1769(analyze_threshold=float("nan")),
        _river_segment_1769(relation="valid"),
    ]
    findings = _maintenance_findings(_healthy_receipt(_maintenance_section(rows)))

    assert [(item["code"], item["evidence"]["relation"]) for item in findings] == [
        ("TABLE_STATISTICS_STALE", "core.valid")
    ]


class _MaintenanceFakeCursor:
    def __init__(self, fail_maintenance: bool) -> None:
        self.fail_maintenance = fail_maintenance
        self._rows: list = []

    def __enter__(self) -> _MaintenanceFakeCursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str, params: object = None) -> None:
        import psycopg2.errors

        self._rows = []
        if "WITH rel AS" in sql:
            if self.fail_maintenance:
                raise psycopg2.errors.QueryCanceled("canceling statement due to statement timeout")
            if "max_last_autovacuum_age_seconds" in sql:
                self._rows = [{"max_last_autovacuum_age_seconds": 1.0, "max_last_autoanalyze_age_seconds": 2.0}]
            else:
                self._rows = [{"schema": "core", "relation": "basin"}]

    def fetchall(self) -> list:
        return self._rows


class _MaintenanceFakeConnection:
    def __init__(self, fail_maintenance: bool) -> None:
        self.fail_maintenance = fail_maintenance
        self.autocommit = False

    def cursor(self) -> _MaintenanceFakeCursor:
        return _MaintenanceFakeCursor(self.fail_maintenance)

    def close(self) -> None:
        return None


@pytest.mark.parametrize("fail_maintenance", (True, False), ids=("probe-error", "probe-ok"))
def test_collect_postgres_isolates_maintenance_output_failure(
    monkeypatch: pytest.MonkeyPatch, fail_maintenance: bool
) -> None:
    import psycopg2

    from packages.common.node27_resource_governance_collection import collect_postgres

    monkeypatch.setattr(psycopg2, "connect", lambda *_a, **_k: _MaintenanceFakeConnection(fail_maintenance))

    result = collect_postgres("postgresql://user:pw@localhost/nhms")

    assert result["status"] == "ok"
    if fail_maintenance:
        assert result["maintenance_output"] == {"status": "error", "error": "QueryCanceled"}
    else:
        assert result["maintenance_output"] == {
            "status": "ok",
            "summary": {"max_last_autovacuum_age_seconds": 1.0, "max_last_autoanalyze_age_seconds": 2.0},
            "rows": [{"schema": "core", "relation": "basin"}],
        }
    for section in ("dead_tuple_hotspots", "hypertables", "largest_chunks"):
        assert result[section] == []
    assert {"external_pg_tblspc_targets", "cold_tablespace", "cold_relation_by_tablespace"}.isdisjoint(result)
