"""C1-C4, publication, filesystem, and G8 identity/selection discriminators."""

from __future__ import annotations

from pathlib import Path

import pytest

from packages.common.node27_issue1895_c14 import (
    C1_BINDER,
    C1_OWNER,
    C1_SCHEMA,
    C2_ACCEPT_BINDER,
    C2_ACCEPT_OWNER,
    C2_SCHEMA,
    C3_CURRENT_BINDER,
    C3_CURRENT_OWNER,
    C3_CURRENT_SCHEMA,
    C4_DISPLAY_BINDER_TOKENS,
    C4_DISPLAY_SCHEMA,
    C4_LIVE_DISPLAY_COMMAND,
    C4_RIVER_CLICK_COMMAND,
    FORBIDDEN_G7_EMPTY_AGGREGATOR_TOKENS,
    fence_forbids_placeholders,
)
from packages.common.node27_issue1895_dsn import (
    bind_rc_dsn_file,
    extract_display_database_url,
    resolve_readonly_dsn,
)
from packages.common.node27_issue1895_fs import reconcile_moved_group_filesystem
from packages.common.node27_issue1895_publication import prove_gfs_ifs_products, registry_expected_identities
from packages.common.node27_issue1895_timer import (
    assert_exact_cold_groups,
    assert_natural_receipt_identity,
    assert_natural_tick_selection,
    persist_baseline_groups,
)
from packages.common.node27_issue1895_types import Issue1895ReadinessError
from tests.test_issue1895_runbook_contract import _gate, _gate_lines

REPO_ROOT = Path(__file__).resolve().parents[1]
DISPLAY_EXAMPLE = REPO_ROOT / "infra" / "env" / "display.example"


def _group(key: str, oid: int, residency: str = "already_target") -> dict:
    return {
        "key": key,
        "residency": residency,
        "durable": {
            "hypertable_schema": "hydro",
            "hypertable_name": "river_timeseries",
            "origin_schema": "_timescaledb_internal",
            "origin_name": f"origin_{oid}",
            "origin_oid": oid,
            "range_start": "2026-01-01T00:00:00Z",
            "range_end": "2026-01-08T00:00:00Z",
        },
        "compressed": {"oid": oid + 1000, "schema": "_timescaledb_internal", "name": f"comp_{oid}"},
        "members": [
            {
                "oid": oid,
                "relkind": "r",
                "bytes": 100,
                "kind": "origin",
                "schema": "s",
                "name": "n",
                "tablespace": "nhms_cold",
                "heap_oid": None,
                "toast_oid": None,
            },
            {
                "oid": oid + 1,
                "relkind": "i",
                "bytes": 10,
                "kind": "index",
                "schema": "s",
                "name": "i",
                "tablespace": "nhms_cold",
                "heap_oid": oid,
                "toast_oid": None,
            },
        ],
    }


def test_g7_c1_c4_are_executable_fences_without_placeholders() -> None:
    g7 = _gate("G7")
    fence_forbids_placeholders(g7)
    from packages.common.node27_issue1895_c14 import fence_forbids_empty_aggregator

    fence_forbids_empty_aggregator(g7)
    for token in (
        C1_OWNER,
        C1_BINDER,
        C1_SCHEMA,
        C2_ACCEPT_OWNER,
        C2_ACCEPT_BINDER,
        C2_SCHEMA,
        C3_CURRENT_OWNER,
        C3_CURRENT_BINDER,
        C3_CURRENT_SCHEMA,
    ):
        assert token in g7, token
    for token in FORBIDDEN_G7_EMPTY_AGGREGATOR_TOKENS:
        assert token not in g7, token
    assert C4_RIVER_CLICK_COMMAND in g7
    assert "river-click-receipt-binder.mjs" in g7
    assert C4_LIVE_DISPLAY_COMMAND in g7
    for token in C4_DISPLAY_BINDER_TOKENS:
        assert token in g7, token
    assert C4_DISPLAY_SCHEMA in g7
    assert "test:e2e:live-display" not in g7
    assert "<root>" not in g7 and "<id>" not in g7 and "<live>" not in g7
    assert "mode-0700" in g7 or "chmod 0700" in g7 or "chmod 700" in g7
    assert "chmod 600" in g7 or "0o600" in g7 or "mode-0600" in g7


def test_c4_display_and_river_click_stay_separate_from_legacy_monitoring() -> None:
    g7 = _gate("G7")
    assert C4_LIVE_DISPLAY_COMMAND in g7
    assert C4_RIVER_CLICK_COMMAND in g7
    assert g7.index(C4_LIVE_DISPLAY_COMMAND) != g7.index(C4_RIVER_CLICK_COMMAND)
    assert "test:e2e:live-display" not in g7
    assert "mocked-regression" not in " ".join(_gate_lines("G7"))


def test_g7_binds_readonly_dsn_from_display_env_before_c2(tmp_path: Path) -> None:
    text = DISPLAY_EXAMPLE.read_text(encoding="utf-8")
    dsn = extract_display_database_url(text)
    assert "nhms_display_ro" in dsn
    target = tmp_path / "readonly-dsn.env"
    bind_rc_dsn_file(target, dsn=dsn)
    assert oct(target.stat().st_mode & 0o777) == "0o600"
    assert target.read_text(encoding="utf-8").startswith("NHMS_DISPLAY_READONLY_DATABASE_URL=")
    with pytest.raises(Issue1895ReadinessError):
        bind_rc_dsn_file(target, dsn=dsn)
    g7 = _gate("G7")
    assert "scripts/node27_issue1895_bind_readonly_dsn.py" in g7
    assert "infra/env/display.env" in g7
    assert "unset NHMS_DISPLAY_READONLY_DATABASE_URL NHMS_READONLY_DB_VALIDATION_DATABASE_URL" in g7
    assert 'mktemp -d "$REPO_ROOT/artifacts/.nhms-issue1895-readonly-XXXXXX"' in g7
    assert '--evidence-root "$RUN_ROOT/receipts/readonly-boundary"' not in g7
    assert "--database-url" not in g7
    assert "--force" not in g7
    assert '--merge-source-dir "$RC_EVIDENCE_ROOT/$RC_GFS_RUN_ID/db/readonly-db-boundary"' in g7
    assert '--merge-source-dir "$RC_EVIDENCE_ROOT/$RC_IFS_RUN_ID/db/readonly-db-boundary"' in g7
    assert "--merge-declared-source GFS --merge-declared-source IFS" in g7
    assert 'test -s "$RC_DSN_FILE"' not in g7 or "bind_readonly_dsn" in g7


def test_bind_readonly_dsn_cli_ignores_ambient_writer_and_stale_readonly_urls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import importlib.util

    script = REPO_ROOT / "scripts" / "node27_issue1895_bind_readonly_dsn.py"
    spec = importlib.util.spec_from_file_location("issue1895_bind_readonly_dsn", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    env = tmp_path / "display.env"
    expected = "postgresql://nhms_display_ro:display-secret@127.0.0.1/nhms"
    env.write_text(f"DATABASE_URL={expected}\n", encoding="utf-8")
    private = tmp_path / "private"
    private.mkdir()
    private.chmod(0o700)
    target = private / "readonly.env"
    monkeypatch.setenv("NHMS_DISPLAY_READONLY_DATABASE_URL", "postgresql://nhms_ingest_rw:writer-secret@127.0.0.1/nhms")
    monkeypatch.setenv(
        "NHMS_READONLY_DB_VALIDATION_DATABASE_URL", "postgresql://nhms_display_ro:old-secret@127.0.0.1/other"
    )
    assert module.main(["--display-env", str(env), "--rc-dsn-file", str(target)]) == 0
    captured = capsys.readouterr()
    assert "writer-secret" not in captured.out + captured.err
    assert "old-secret" not in captured.out + captured.err
    assert target.read_text(encoding="utf-8") == f"NHMS_DISPLAY_READONLY_DATABASE_URL={expected}\n"


def test_resolve_readonly_dsn_refuses_writer_role() -> None:
    with pytest.raises(Issue1895ReadinessError) as caught:
        resolve_readonly_dsn(display_env_text="DATABASE_URL=postgresql://nhms_ingest_rw:x@127.0.0.1/nhms\n")
    assert caught.value.code == "DSN_ROLE_INVALID"


def test_registry_expected_identities_uses_canonical_source_keys() -> None:
    registry = [{"model_id": "basins_qhh_shud", "basin_id": "basins_qhh"}]
    expected = registry_expected_identities(registry, sources=("GFS", "IFS"))
    assert expected == {
        "gfs": frozenset({("basins_qhh_shud", "basins_qhh")}),
        "IFS": frozenset({("basins_qhh_shud", "basins_qhh")}),
    }


def test_gfs_ifs_proof_binds_independent_identities_and_rejects_mixed_cycles() -> None:
    gfs = {
        "source_id": "gfs",
        "run_id": "run-gfs",
        "cycle_time": "2026-09-04T00:00:00Z",
        "model_id": "basins_qhh_shud",
        "basin_id": "basins_qhh",
        "status": "ready",
        "run_status": "published",
    }
    ifs = {
        "source_id": "IFS",
        "run_id": "run-ifs",
        "cycle_time": "2026-09-04T12:00:00Z",
        "model_id": "basins_qhh_shud",
        "basin_id": "basins_qhh",
        "status": "ready",
        "run_status": "published",
    }
    expected = {("basins_qhh_shud", "basins_qhh")}
    gfs_rows = [
        {
            "source_id": "gfs",
            "cycle_time": gfs["cycle_time"],
            "run_status": "published",
            "model_id": "basins_qhh_shud",
            "basin_id": "basins_qhh",
        }
    ]
    ifs_rows = [
        {
            "source_id": "IFS",
            "cycle_time": ifs["cycle_time"],
            "run_status": "published",
            "model_id": "basins_qhh_shud",
            "basin_id": "basins_qhh",
        }
    ]
    proved = prove_gfs_ifs_products(
        gfs=gfs,
        ifs=ifs,
        gfs_rows=gfs_rows,
        ifs_rows=ifs_rows,
        expected_gfs=expected,
        expected_ifs=expected,
        current_valid_times=["2026-09-04T18:00:00Z"],
        baseline_valid_times=["2026-09-04T12:00:00Z"],
    )
    assert proved["gfs"]["cycle_time"] != proved["ifs"]["cycle_time"]
    assert proved["gfs"]["status"] == "published"
    display_status_rows = [
        {
            "source_id": "gfs",
            "cycle_time": gfs["cycle_time"],
            "status": "ready",
            "count": 1,
        }
    ]
    with pytest.raises(Issue1895ReadinessError) as display_status:
        prove_gfs_ifs_products(
            gfs=gfs,
            ifs=ifs,
            gfs_rows=display_status_rows,
            ifs_rows=ifs_rows,
            expected_gfs=expected,
            expected_ifs=expected,
            current_valid_times=["2026-09-04T18:00:00Z"],
            baseline_valid_times=["2026-09-04T12:00:00Z"],
        )
    assert display_status.value.code == "PUBLICATION_CYCLE_INCOMPLETE"
    mixed = [
        {
            "source_id": "gfs",
            "cycle_time": "2026-09-03T00:00:00Z",
            "status": "published",
            "count": 1,
        }
    ]
    with pytest.raises(Issue1895ReadinessError) as caught:
        prove_gfs_ifs_products(
            gfs=gfs,
            ifs=ifs,
            gfs_rows=mixed,
            ifs_rows=ifs_rows,
            expected_gfs=expected,
            expected_ifs=expected,
            current_valid_times=["2026-09-04T18:00:00Z"],
            baseline_valid_times=["2026-09-04T12:00:00Z"],
        )
    assert caught.value.code == "PUBLICATION_CYCLE_MIXED"
    with pytest.raises(Issue1895ReadinessError):
        prove_gfs_ifs_products(
            gfs={**gfs, "series": [1, 2, 3]},
            ifs=ifs,
            gfs_rows=gfs_rows,
            ifs_rows=ifs_rows,
            expected_gfs=expected,
            expected_ifs=expected,
            current_valid_times=["2026-09-04T18:00:00Z"],
            baseline_valid_times=["2026-09-04T12:00:00Z"],
        )
    g7 = _gate("G7")
    assert "CURRENT_CYCLE" not in g7 or "gfs" in g7
    assert "scripts/node27_issue1895_publication_current.py" in g7
    assert '--c4-receipt "$C4_RECEIPT"' in g7
    assert '--baseline-valid-times "$RUN_ROOT/census/valid-times-baseline.json"' in g7
    assert "scripts/node27_issue1895_publication_prove.py" not in g7


def test_filesystem_reconciliation_distinguishes_plausible_from_reversed() -> None:
    members = [{"bytes": 1000}, {"bytes": 500}]
    ok = reconcile_moved_group_filesystem(
        members=members,
        hot_avail_before=10_000,
        hot_avail_after=11_500,
        cold_avail_before=20_000,
        cold_avail_after=18_500,
        allocation_granularity_bytes=1,
        concurrent_noise_bytes=0,
    )
    assert ok["approved"] is True
    with pytest.raises(Issue1895ReadinessError) as reversed_hot:
        reconcile_moved_group_filesystem(
            members=members,
            hot_avail_before=11_500,
            hot_avail_after=10_000,
            cold_avail_before=20_000,
            cold_avail_after=18_500,
            allocation_granularity_bytes=1,
            concurrent_noise_bytes=0,
        )
    assert reversed_hot.value.code == "FS_HOT_NOT_POSITIVE"
    with pytest.raises(Issue1895ReadinessError):
        reconcile_moved_group_filesystem(
            members=members,
            hot_avail_before=10_000,
            hot_avail_after=10_000,
            cold_avail_before=20_000,
            cold_avail_after=20_000,
            allocation_granularity_bytes=1,
            concurrent_noise_bytes=0,
        )
    g6 = _gate("G6")
    assert "scripts/node27_issue1895_fs_reconcile.py" in g6
    assert "operator-reviewed measured" not in g6 or "fs_reconcile" in g6


def test_g8_receipt_identity_and_exact_six_groups() -> None:
    groups = [_group(f"k{index}", index) for index in range(1, 7)]
    baseline = persist_baseline_groups(groups)
    assert len(baseline) == 6
    assert_exact_cold_groups(groups, baseline=groups)
    drifted = [_group(f"k{index}", index) for index in range(1, 6)] + [_group("extra", 99)]
    with pytest.raises(Issue1895ReadinessError):
        assert_exact_cold_groups(drifted, baseline=groups)
    mixed = [_group("k1", 1, residency="mixed")] + [_group(f"k{index}", index) for index in range(2, 7)]
    with pytest.raises(Issue1895ReadinessError) as residency:
        assert_exact_cold_groups(mixed, baseline=groups)
    assert residency.value.code == "COLD_RESIDENCY_MIXED"
    receipt = {
        "schema_version": "1.1",
        "head_sha": "a" * 40,
        "cutoff": "2026-08-01T00:00:00Z",
        "watermark": "2026-09-01T00:00:00Z",
        "per_tick_bound": 1,
        "outcome": "no_op",
        "selected": [],
        "deferred": [],
    }
    assert_natural_receipt_identity(
        receipt,
        reviewed_sha="a" * 40,
        expected_cutoff="2026-08-01T00:00:00Z",
        expected_watermark="2026-09-01T00:00:00Z",
        invoked_unit="nhms-node27-timeseries-compression.service",
    )
    from packages.common.node27_issue1895_receipt import durable_key as _durable_key

    baseline_keys = tuple(_durable_key(group["durable"]) for group in groups)
    k7 = _durable_key(_group("k7", 7)["durable"])
    k8 = _durable_key(_group("k8", 8)["durable"])
    assert_natural_tick_selection(
        receipt,
        remaining_complete_source_keys=(),
        newly_terminal_keys=(),
        baseline_keys=baseline_keys,
    )
    with pytest.raises(Issue1895ReadinessError) as leftover:
        assert_natural_tick_selection(
            receipt,
            remaining_complete_source_keys=(k7,),
            newly_terminal_keys=(),
        )
    assert leftover.value.code == "TICK_NOOP_NOT_EXHAUSTIVE"
    selected_receipt = {
        **receipt,
        "outcome": "clean",
        "selected": [
            {
                "outcome": "migrated",
                "durable": _group("k7", 7)["durable"],
            }
        ],
        "deferred": [],
    }
    assert_natural_tick_selection(
        selected_receipt,
        remaining_complete_source_keys=(),
        newly_terminal_keys=(k7,),
        baseline_keys=baseline_keys,
    )
    with pytest.raises(Issue1895ReadinessError):
        assert_natural_tick_selection(
            selected_receipt,
            remaining_complete_source_keys=(k8,),
            newly_terminal_keys=(k8,),
        )
    g8 = _gate("G8")
    assert "assert_natural_receipt_identity" in g8 or "head_sha" in g8
    assert 'test "$MOVED_GROUPS" -ge "$REQUIRE_COUNT"' not in g8
    assert "assert_exact_cold_groups" in g8 or "persist_baseline_groups" in g8
