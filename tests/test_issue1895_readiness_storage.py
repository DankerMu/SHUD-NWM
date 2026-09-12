"""G3/G5/G6/G8 storage-state-machine discriminators. No live DB/API/node."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from packages.common.compressed_chunk_cold_residency import CatalogChunk, ResidencyGroup, ResidencyMember
from packages.common.compressed_chunk_cold_runtime_catalog import BoundInventories, HypertableInventory, WindowParity
from packages.common.node27_issue1895_census_bind import bind_pre_movement_census
from packages.common.node27_issue1895_engine import assert_engine_sql_row
from packages.common.node27_issue1895_env import (
    G4_GOVERNED_KEYS,
    G5_GOVERNED_KEYS,
    rewrite_cold_env_text,
    validate_canonical_positive_decimal,
)
from packages.common.node27_issue1895_fs import reconcile_moved_group_filesystem
from packages.common.node27_issue1895_post_target import (
    newly_terminal_keys,
    observe_named_group,
    observe_post_target,
    run_post_target_observation,
)
from packages.common.node27_issue1895_receipt import (
    assert_sequential_tick_receipt,
    durable_key,
    unique_migrated_observation,
)
from packages.common.node27_issue1895_timer import assert_exact_cold_groups, assert_natural_tick_selection
from packages.common.node27_issue1895_types import Issue1895ReadinessError
from scripts import node27_issue1895_census_bind as census_bind_cli
from scripts import node27_issue1895_env_rewrite as env_cli
from scripts import node27_issue1895_fs_reconcile as fs_reconcile_cli
from scripts import node27_issue1895_post_target_observe as post_target_observe_cli
from scripts import node27_issue1895_sequential_receipt as sequential_receipt_cli
from scripts import node27_issue1895_watermark as watermark_cli
from tests.test_issue1895_readiness_c14 import _group
from tests.test_issue1895_runbook_contract import _gate_bash, _gate_lines

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "infra" / "env" / "node27-cold-residency.example"
SHA = "a" * 40


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _write_private(path: Path, text: str) -> Path:
    _private_dir(path.parent)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def _write_private_json(path: Path, document: dict) -> Path:
    return _write_private(path, json.dumps(document))


def _substitute_identity(path: Path, kind: str) -> None:
    if kind == "symlink":
        real = path.with_name(path.name + ".real")
        path.rename(real)
        path.symlink_to(real)
        return
    if kind == "mode":
        os.chmod(path, 0o644)
        return
    os.link(path, path.with_name(path.name + ".alink"))

def _durable(index: int) -> dict:
    return _group(f"k{index}", index)["durable"]

KEYS = tuple(durable_key(_durable(index)) for index in range(1, 7))
_MISSING = object()


G4_UPDATES = {
    "NODE27_COLD_RESIDENCY_COLD_RESERVE_BYTES": "4096",
    "NODE27_COLD_RESIDENCY_WAL_RESERVE_BYTES": "4096",
    "NODE27_COLD_RESIDENCY_PER_TICK_BOUND": "1",
    "NODE27_COLD_RESIDENCY_CONTAINER_EXEC_UID": "1005",
    "NODE27_COLD_RESIDENCY_CONTAINER_EXEC_GID": "1005",
}

def _shipping_receipt(*, call_index: int, outcome: str = "migrated") -> dict:
    selected = [
        {"outcome": "already_cold", "durable": _durable(index)}
        for index in range(1, call_index)
    ]
    selected.append({"outcome": outcome, "durable": _durable(call_index), "after": {"members": [{"bytes": 10}]}})
    deferred = [
        {"reason": "per_tick_bound", "durable": _durable(index)}
        for index in range(call_index + 1, 7)
    ]
    return {
        "schema_version": "1.1",
        "per_tick_bound": 1,
        "outcome": "clean",
        "selected": selected,
        "deferred": deferred,
    }


def _census_artifact(*, digest: str = "abc") -> dict:
    groups = [_group(key, index, residency="all_source") for index, key in enumerate(KEYS, start=1)]
    for group in groups:
        group["group_digest"] = f"g-{group['key']}"
        group["inventory_digest"] = "inv"
        group["parity"] = {"row_count": 1}
        group["before_compression_total_bytes"] = 100
        group["retained_source_bytes"] = 50
    return {
        "verdict": "GO",
        "head_sha": SHA,
        "generated_at": "2026-09-04T04:00:30Z",
        "census_digest": digest,
        "group_keys": list(KEYS),
        "residency_counts": {"all_source": 6},
        "groups": groups,
        "capacity_policy": {"S": 1, "E": 2},
    }


def test_g3_engine_gate_accepts_ubuntu_suffix_and_rejects_wrong_major() -> None:
    assert_engine_sql_row("15.2 (Ubuntu 15.2-1.pgdg22.04+1)|2.10.2")
    with pytest.raises(Issue1895ReadinessError) as major:
        assert_engine_sql_row("16.2 (Ubuntu 16.2-1)|2.10.2")
    assert major.value.code == "ENGINE_PG_MISMATCH"
    with pytest.raises(Issue1895ReadinessError) as ext:
        assert_engine_sql_row("15.2 (Ubuntu 15.2-1.pgdg22.04+1)|2.11.0")
    assert ext.value.code == "ENGINE_TIMESCALEDB_MISMATCH"
    g3 = " ".join(_gate_lines("G3"))
    assert "scripts/node27_issue1895_engine.py" in g3
    assert "grep -Eqx '15\\.2\\|2\\.10\\.2'" not in g3


def test_g5_census_binder_loads_both_json_paths(tmp_path: Path) -> None:
    current = _census_artifact()
    original = _census_artifact()
    private = _private_dir(tmp_path / "private")
    current_path = _write_private_json(private / "current.json", current)
    original_path = _write_private_json(private / "original.json", original)
    bracket = _write_private(private / "bracket", "2026-09-04T04:00:00+00:00\n2026-09-04T04:01:00+00:00\n0\n")
    bound = bind_pre_movement_census(
        current_path=current_path,
        original_path=original_path,
        expected_digest="abc",
        bracket_path=bracket,
        reviewed_sha=SHA,
    )
    assert bound["verdict"] == "GO"
    bracket.write_text("not-a-bracket\n2026-09-04T04:01:00+00:00\n0\n", encoding="utf-8")
    os.chmod(bracket, 0o600)
    assert census_bind_cli.main(
        [
            "--current", str(current_path), "--original", str(original_path), "--digest", "abc",
            "--bracket", str(bracket), "--reviewed-sha", SHA,
        ]
    ) == 1
    bracket.write_text("2026-09-04T04:00:00+00:00\n2026-09-04T04:01:00+00:00\n0\n", encoding="utf-8")
    os.chmod(bracket, 0o600)
    rc = census_bind_cli.main(
        [
            "--current",
            str(current_path),
            "--original",
            str(original_path),
            "--digest",
            "abc",
            "--bracket",
            str(bracket),
            "--reviewed-sha",
            SHA,
        ]
    )
    assert rc == 0
    drifted = dict(current)
    drifted["census_digest"] = "nope"
    current_path.write_text(json.dumps(drifted), encoding="utf-8")
    os.chmod(current_path, 0o600)
    with pytest.raises(Issue1895ReadinessError) as digest:
        bind_pre_movement_census(
            current_path=current_path,
            original_path=original_path,
            expected_digest="abc",
            bracket_path=bracket,
            reviewed_sha=SHA,
        )
    assert digest.value.code == "CENSUS_DIGEST_DRIFT"
    g5 = " ".join(_gate_lines("G5"))
    assert "scripts/node27_issue1895_census_bind.py" in g5
    assert "assert current[\"verdict\"]" not in g5


@pytest.mark.parametrize("kind", ("symlink", "mode", "hardlink"))
@pytest.mark.parametrize("which", ("current", "original", "bracket"))
def test_g5_census_binder_refuses_unsafe_current_original_or_bracket_identity(
    tmp_path: Path, kind: str, which: str
) -> None:
    private = _private_dir(tmp_path / "private")
    current_path = _write_private_json(private / "current.json", _census_artifact())
    original_path = _write_private_json(private / "original.json", _census_artifact())
    bracket = _write_private(private / "bracket", "2026-09-04T04:00:00+00:00\n2026-09-04T04:01:00+00:00\n0\n")
    target = {"current": current_path, "original": original_path, "bracket": bracket}[which]
    _substitute_identity(target, kind)
    with pytest.raises(Issue1895ReadinessError) as refused:
        bind_pre_movement_census(
            current_path=current_path,
            original_path=original_path,
            expected_digest="abc",
            bracket_path=bracket,
            reviewed_sha=SHA,
        )
    assert refused.value.code in {
        "CENSUS_JSON_INVALID",
        "CENSUS_BRACKET_INVALID",
        "READINESS_INPUT_IDENTITY",
        "READINESS_INPUT_INVALID",
        "CENSUS_IDENTITY",
        "CENSUS_NOT_REGULAR",
        "CENSUS_IDENTITY_DRIFT",
        "CENSUS_OPEN",
        "CENSUS_MISSING",
    }


def test_g4_then_g5_env_rewrite_passes_through_live_compression_lag(tmp_path: Path) -> None:
    original = EXAMPLE.read_text(encoding="utf-8")
    after_g4 = rewrite_cold_env_text(original, updates=G4_UPDATES, governed_keys=G4_GOVERNED_KEYS)
    assert any(line.startswith("#NODE27_COLD_RESIDENCY_DEVICE_IDENTITY") for line in after_g4.splitlines())
    assert any(line.startswith("#NODE27_COLD_RESIDENCY_LAG_SECONDS") for line in after_g4.splitlines())
    after_g5 = rewrite_cold_env_text(
        after_g4,
        updates={
            "NODE27_COLD_RESIDENCY_DEVICE_IDENTITY": "8:1",
            "NODE27_COLD_RESIDENCY_LAG_SECONDS": "172800",
        },
        governed_keys=G5_GOVERNED_KEYS,
        require_device_identity_unassigned=False,
    )
    assert after_g5.count("NODE27_COLD_RESIDENCY_DEVICE_IDENTITY=") == 1
    assert after_g5.count("NODE27_COLD_RESIDENCY_LAG_SECONDS=") == 1
    assert "NODE27_COLD_RESIDENCY_DEVICE_IDENTITY=8:1" in after_g5
    assert "NODE27_COLD_RESIDENCY_LAG_SECONDS=172800" in after_g5
    assert any(line.startswith("#NODE27_COLD_RESIDENCY_LAG_SECONDS") for line in after_g4.splitlines())
    assert any(line.startswith("#") for line in after_g5.splitlines())
    original_url = next(line for line in original.splitlines() if line.startswith("DATABASE_URL="))
    assert next(line for line in after_g5.splitlines() if line.startswith("DATABASE_URL=")) == original_url
    other = rewrite_cold_env_text(
        after_g4,
        updates={
            "NODE27_COLD_RESIDENCY_DEVICE_IDENTITY": "8:1",
            "NODE27_COLD_RESIDENCY_LAG_SECONDS": "86400",
        },
        governed_keys=G5_GOVERNED_KEYS,
        require_device_identity_unassigned=False,
    )
    assert "NODE27_COLD_RESIDENCY_LAG_SECONDS=86400" in other
    path = tmp_path / "node27-cold-residency.env"
    path.write_text(original, encoding="utf-8")
    assert env_cli.main(
        [
            "--path",
            str(path),
            "--cold-reserve-bytes",
            "4096",
            "--wal-reserve-bytes",
            "4096",
            "--per-tick-bound",
            "1",
            "--container-exec-uid",
            "1005",
            "--container-exec-gid",
            "1005",
        ]
    ) == 0
    assert env_cli.main(
        [
            "--path",
            str(path),
            "--stage",
            "g5",
            "--device-identity",
            "8:1",
            "--lag-seconds",
            "172800",
        ]
    ) == 0
    published = path.read_text(encoding="utf-8")
    assert "NODE27_COLD_RESIDENCY_LAG_SECONDS=172800" in published
    g5 = " ".join(_gate_lines("G5"))
    assert "--stage g5" in g5
    assert "--lag-seconds \"$NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS\"" in g5
    assert "test \"$NODE27_COLD_RESIDENCY_LAG_SECONDS\" = \"$NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS\"" in g5
    assert "test \"$NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS\" = \"604800\"" not in g5
    assert "values[key] = value" not in g5


def test_live_compression_lag_rejects_noncanonical_and_missing_values(tmp_path: Path) -> None:
    for invalid in ("0", "0172800", "-172800", "172800.0", "172800 ", "", "1e5"):
        with pytest.raises(Issue1895ReadinessError) as caught:
            validate_canonical_positive_decimal(invalid, label="lag_seconds")
        assert caught.value.code == "ENV_VALUE_INVALID"
    with pytest.raises(Issue1895ReadinessError):
        validate_canonical_positive_decimal(None, label="lag_seconds")
    original = EXAMPLE.read_text(encoding="utf-8")
    after_g4 = rewrite_cold_env_text(original, updates=G4_UPDATES, governed_keys=G4_GOVERNED_KEYS)
    with pytest.raises(Issue1895ReadinessError) as lag:
        rewrite_cold_env_text(
            after_g4,
            updates={
                "NODE27_COLD_RESIDENCY_DEVICE_IDENTITY": "8:1",
                "NODE27_COLD_RESIDENCY_LAG_SECONDS": "0172800",
            },
            governed_keys=G5_GOVERNED_KEYS,
            require_device_identity_unassigned=False,
        )
    assert lag.value.code == "ENV_VALUE_INVALID"
    missing_path = tmp_path / "missing-lag.env"
    missing_path.write_text(after_g4, encoding="utf-8")
    with pytest.raises(SystemExit):
        env_cli.main(
            [
                "--path",
                str(missing_path),
                "--stage",
                "g5",
                "--device-identity",
                "8:1",
            ]
        )


def test_g6_sequential_receipts_bind_one_migrated_key_and_suffix() -> None:
    for call in range(1, 7):
        receipt = _shipping_receipt(call_index=call)
        migrated = assert_sequential_tick_receipt(receipt, ordered_keys=KEYS, call_index=call)
        assert unique_migrated_observation(receipt) is migrated
        if call < 6:
            assert receipt["deferred"]
        else:
            assert receipt["deferred"] == []
    wrong = _shipping_receipt(call_index=2)
    wrong["selected"][1]["durable"] = _durable(3)
    with pytest.raises(Issue1895ReadinessError) as mismatch:
        assert_sequential_tick_receipt(wrong, ordered_keys=KEYS, call_index=2)
    assert mismatch.value.code == "RECEIPT_MIGRATED_KEY_MISMATCH"
    missing_prior = _shipping_receipt(call_index=3)
    missing_prior["selected"] = [item for item in missing_prior["selected"] if item.get("outcome") != "already_cold"]
    assert_sequential_tick_receipt(missing_prior, ordered_keys=KEYS, call_index=3)
    suffix = _shipping_receipt(call_index=2)
    suffix["deferred"][0]["reason"] = "other"
    with pytest.raises(Issue1895ReadinessError) as reason:
        assert_sequential_tick_receipt(suffix, ordered_keys=KEYS, call_index=2)
    assert reason.value.code == "RECEIPT_DEFERRED_REASON"
    extra = _shipping_receipt(call_index=1)
    extra["selected"].append({"outcome": "already_cold", "durable": _group("k9", 9)["durable"]})
    with pytest.raises(Issue1895ReadinessError):
        assert_sequential_tick_receipt(extra, ordered_keys=KEYS, call_index=1)
    g6 = " ".join(_gate_lines("G6"))
    assert "scripts/node27_issue1895_sequential_receipt.py" in g6
    assert 'len(receipt["selected"]) == 1 and not receipt["deferred"]' not in g6


@pytest.mark.parametrize("kind", ("symlink", "mode", "hardlink"))
@pytest.mark.parametrize("which", ("census", "receipt"))
def test_g6_sequential_receipt_cli_refuses_unsafe_census_or_receipt_identity(
    tmp_path: Path, kind: str, which: str
) -> None:
    private = _private_dir(tmp_path / "private")
    census_path = _write_private_json(private / "census.json", _census_artifact())
    receipt_path = _write_private_json(private / "receipt.json", _shipping_receipt(call_index=1))
    target = census_path if which == "census" else receipt_path
    _substitute_identity(target, kind)
    rc = sequential_receipt_cli.main(
        ["--receipt", str(receipt_path), "--census", str(census_path), "--call-index", "1"]
    )
    assert rc == 1


def test_g6_sequential_receipt_cli_accepts_valid_private_files(tmp_path: Path) -> None:
    private = _private_dir(tmp_path / "private")
    census_path = _write_private_json(private / "census.json", _census_artifact())
    receipt_path = _write_private_json(private / "receipt.json", _shipping_receipt(call_index=1))
    assert sequential_receipt_cli.main(
        ["--receipt", str(receipt_path), "--census", str(census_path), "--call-index", "1"]
    ) == 0


def test_g5_and_g6_owners_refuse_parent_mode_0755(tmp_path: Path) -> None:
    private = _private_dir(tmp_path / "private")
    current_path = _write_private_json(private / "current.json", _census_artifact())
    original_path = _write_private_json(private / "original.json", _census_artifact())
    bracket = _write_private(private / "bracket", "2026-09-04T04:00:00+00:00\n2026-09-04T04:01:00+00:00\n0\n")
    os.chmod(private, 0o755)
    with pytest.raises(Issue1895ReadinessError) as census:
        bind_pre_movement_census(
            current_path=current_path,
            original_path=original_path,
            expected_digest="abc",
            bracket_path=bracket,
            reviewed_sha=SHA,
        )
    assert census.value.code in {
        "CENSUS_JSON_INVALID",
        "CENSUS_BRACKET_INVALID",
        "READINESS_INPUT_IDENTITY",
        "READINESS_INPUT_INVALID",
        "INPUT_PARENT_MODE",
    }
    census_path = _write_private_json(private / "census.json", _census_artifact())
    receipt_path = _write_private_json(private / "receipt.json", _shipping_receipt(call_index=1))
    os.chmod(private, 0o755)
    assert sequential_receipt_cli.main(
        ["--receipt", str(receipt_path), "--census", str(census_path), "--call-index", "1"]
    ) == 1


def test_g6_preview_and_group_enumeration_use_held_reader_before_mutation() -> None:
    fences = [body for _opening, body in _gate_bash("G6")]
    preview = next(body for body in fences if "PREVIEW_RECEIPT" in body and "ORIGINAL_CENSUS" in body)
    assert "json.load(open" not in preview
    assert "open(bracket)" not in preview
    assert "read_held_private_json" in preview
    assert "read_held_private_text" in preview
    helper_at = min(preview.index("read_held_private_json"), preview.index("read_held_private_text"))
    assert helper_at < preview.index("assert_sequential_tick_receipt")

    loop = next(body for body in fences if "while IFS= read -r GROUP" in body and "--enforce" in body)
    assert "json.load(open" not in loop
    assert "< <(" in loop
    substitution = loop[loop.index("< <(") :]
    assert "read_held_private_json" in substitution
    assert "group_keys" in substitution
    assert "json.load(open" not in substitution
    assert loop.index("while IFS= read -r GROUP") < loop.index("--enforce")
    assert loop.index("< <(") > loop.index("--enforce")
    assert substitution.index("read_held_private_json") < substitution.index("group_keys")
    assert "IFS= read -r GROUP" in loop
    assert "for GROUP in" not in loop


def test_post_target_observation_refuses_unsafe_baseline_identity(tmp_path: Path) -> None:
    private = _private_dir(tmp_path / "private")
    baseline = _write_private_json(
        private / "baseline.json",
        {"groups": [_group(key, index) for index, key in enumerate(KEYS, start=1)]},
    )
    output = private / "observed.json"
    linked = tmp_path / "baseline-link.json"
    linked.symlink_to(baseline)
    with pytest.raises(Issue1895ReadinessError) as refused:
        run_post_target_observation(
            baseline_path=linked,
            output_path=output,
            reviewed_sha=SHA,
            lag_seconds=172800,
            execute=lambda *_args, **_kwargs: [],
            watermark=datetime(2026, 9, 6, tzinfo=UTC),
            dsn="postgresql://nhms_display_ro@127.0.0.1/nhms",
        )
    assert refused.value.code in {
        "POST_TARGET_BASELINE_INVALID",
        "READINESS_INPUT_IDENTITY",
        "READINESS_INPUT_INVALID",
        "POST_TARGET_IDENTITY",
        "POST_TARGET_NOT_REGULAR",
        "POST_TARGET_IDENTITY_DRIFT",
        "POST_TARGET_OPEN",
        "POST_TARGET_MISSING",
    }
    assert not output.exists()
    assert refused.value.code != "POST_TARGET_BASELINE_COUNT"


def test_post_target_observation_refuses_parent_mode_0755(tmp_path: Path) -> None:
    private = _private_dir(tmp_path / "private")
    baseline = _write_private_json(
        private / "baseline.json",
        {"groups": [_group(key, index) for index, key in enumerate(KEYS, start=1)]},
    )
    output = private / "observed.json"
    os.chmod(private, 0o755)
    with pytest.raises(Issue1895ReadinessError) as refused:
        run_post_target_observation(
            baseline_path=baseline,
            output_path=output,
            reviewed_sha=SHA,
            lag_seconds=172800,
            execute=lambda *_args, **_kwargs: [],
            watermark=datetime(2026, 9, 6, tzinfo=UTC),
            dsn="postgresql://nhms_display_ro@127.0.0.1/nhms",
        )
    assert refused.value.code in {
        "POST_TARGET_BASELINE_INVALID",
        "READINESS_INPUT_IDENTITY",
        "READINESS_INPUT_INVALID",
        "INPUT_PARENT_MODE",
    }
    assert not output.exists()


def test_filesystem_10mib_zero_reversed_and_tolerance_bounds() -> None:
    moved = 10 * 1024 * 1024
    members = [{"bytes": moved}]
    noise = 64 * 1024 * 1024
    ok = reconcile_moved_group_filesystem(
        members=members,
        hot_avail_before=100,
        hot_avail_after=100 + moved,
        cold_avail_before=200 + moved,
        cold_avail_after=200,
    )
    assert ok["approved"] is True
    assert ok["hot_residual_bytes"] == 0
    with pytest.raises(Issue1895ReadinessError) as zero:
        reconcile_moved_group_filesystem(
            members=members,
            hot_avail_before=100,
            hot_avail_after=100,
            cold_avail_before=200,
            cold_avail_after=200,
        )
    assert zero.value.code == "FS_HOT_NOT_POSITIVE"
    with pytest.raises(Issue1895ReadinessError) as reversed_delta:
        reconcile_moved_group_filesystem(
            members=members,
            hot_avail_before=100 + moved,
            hot_avail_after=100,
            cold_avail_before=200,
            cold_avail_after=200 + moved,
        )
    assert reversed_delta.value.code in {"FS_HOT_NOT_POSITIVE", "FS_COLD_NOT_POSITIVE"}
    boundary = reconcile_moved_group_filesystem(
        members=members,
        hot_avail_before=0,
        hot_avail_after=moved + noise + 4096,
        cold_avail_before=moved + noise + 4096,
        cold_avail_after=0,
    )
    assert boundary["approved"] is True
    with pytest.raises(Issue1895ReadinessError) as over:
        reconcile_moved_group_filesystem(
            members=members,
            hot_avail_before=0,
            hot_avail_after=moved + noise + 4097,
            cold_avail_before=moved,
            cold_avail_after=0,
        )
    assert over.value.code == "FS_RECONCILE_NO_GO"


def test_g8_natural_tick_uses_independent_pre_post_sets() -> None:
    baseline = [_group(key, index) for index, key in enumerate(KEYS, start=1)]
    assert_exact_cold_groups(baseline, baseline=baseline)
    replaced = [_group(key, index) for index, key in enumerate(KEYS, start=1)]
    replaced[0]["compressed"] = {"oid": 9999, "schema": "_timescaledb_internal", "name": "new_comp"}
    replaced[0]["members"][0]["oid"] = 4242
    assert_exact_cold_groups(replaced, baseline=baseline)
    k7 = durable_key(_group("k7", 7)["durable"])
    k8 = durable_key(_group("k8", 8)["durable"])
    no_op = {
        "outcome": "no_op",
        "selected": [{"outcome": "already_cold", "durable": _durable(1)}],
        "deferred": [],
    }
    assert_natural_tick_selection(
        no_op,
        remaining_complete_source_keys=(),
        newly_terminal_keys=(),
        baseline_keys=KEYS,
    )
    migrated = {
        "outcome": "clean",
        "selected": [
            {"outcome": "already_cold", "durable": _durable(1)},
            {"outcome": "migrated", "durable": _group("k7", 7)["durable"]},
        ],
        "deferred": [{"reason": "per_tick_bound", "durable": _group("k8", 8)["durable"]}],
    }
    assert_natural_tick_selection(
        migrated,
        remaining_complete_source_keys=(k8,),
        newly_terminal_keys=(k7,),
        baseline_keys=KEYS,
    )
    assert newly_terminal_keys(pre_target_keys=KEYS, post_target_keys=(*KEYS, k7)) == (k7,)
    with pytest.raises(Issue1895ReadinessError):
        assert_natural_tick_selection(
            migrated,
            remaining_complete_source_keys=("k9",),
            newly_terminal_keys=(k7,),
            baseline_keys=KEYS,
        )
    g8 = " ".join(_gate_lines("G8"))
    assert "scripts/node27_issue1895_post_target_observe.py" in g8
    assert "scripts/node27_cold_residency_census.py" not in g8
    assert "REMAINING_ALL_SOURCE" not in g8


def test_post_target_observer_accepts_mutable_sibling_when_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 8, tzinfo=UTC)
    durable = {
        "hypertable_schema": "hydro",
        "hypertable_name": "river_timeseries",
        "origin_schema": "_timescaledb_internal",
        "origin_name": "origin_1",
        "origin_oid": 1,
        "range_start": "2026-01-01T00:00:00Z",
        "range_end": "2026-01-08T00:00:00Z",
    }
    members = (
        ResidencyMember("origin_heap", 1, "_timescaledb_internal", "origin_1", "r", "nhms_cold", 10, None, 3),
        ResidencyMember("compressed_heap", 99, "_timescaledb_internal", "comp_new", "r", "nhms_cold", 4, None, 5),
        ResidencyMember("index", 2, "_timescaledb_internal", "idx", "i", "nhms_cold", 1, 1, None),
        ResidencyMember("toast_heap", 3, "_timescaledb_internal", "toast", "t", "nhms_cold", 1, 1, None),
        ResidencyMember("toast_index", 4, "_timescaledb_internal", "toast_idx", "i", "nhms_cold", 1, 3, None),
        ResidencyMember("toast_heap", 5, "_timescaledb_internal", "ctoast", "t", "nhms_cold", 1, 99, None),
        ResidencyMember("toast_index", 6, "_timescaledb_internal", "ctoast_idx", "i", "nhms_cold", 1, 5, None),
    )
    group = ResidencyGroup(
        "hydro",
        "river_timeseries",
        1,
        "_timescaledb_internal",
        "origin_1",
        99,
        "_timescaledb_internal",
        "comp_new",
        start,
        end,
        True,
        members,
    )
    chunk = CatalogChunk(
        "hydro",
        "river_timeseries",
        1,
        "_timescaledb_internal",
        "origin_1",
        99,
        "_timescaledb_internal",
        "comp_new",
        start,
        end,
        True,
    )
    inventory = HypertableInventory(
        schema="hydro",
        name="river_timeseries",
        columns=(),
        digest="inv",
    )
    inventories = BoundInventories(river=inventory, forcing=inventory, digest="inv")
    expected_parity = {
        "row_count": 1,
        "non_null_counts": {},
        "checksum": "x",
        "inventory_digest": "inv",
        "range_start": "2026-01-01T00:00:00Z",
        "range_end": "2026-01-08T00:00:00Z",
    }

    def fake_load(*_args: object, **_kwargs: object) -> CatalogChunk:
        return chunk

    def fake_collect(*_args: object, **_kwargs: object) -> ResidencyGroup:
        return group

    captured: list[tuple[object, object, object]] = []

    def fake_parity(execute: object, inventory: object, loaded: object) -> WindowParity:
        captured.append((execute, inventory, loaded))
        return WindowParity(
            row_count=1,
            non_null_counts=(),
            checksum="x",
            inventory_digest="inv",
            range_start=start,
            range_end=end,
        )

    execute = object()
    monkeypatch.setattr("packages.common.node27_issue1895_post_target.load_catalog_chunk", fake_load)
    monkeypatch.setattr("packages.common.node27_issue1895_post_target.collect_residency_group", fake_collect)
    monkeypatch.setattr("packages.common.node27_issue1895_post_target.compute_window_parity", fake_parity)
    observed = observe_named_group(
        execute,
        durable=durable,
        inventories=inventories,
        expected_parity=expected_parity,
    )
    assert observed["complete_target"] is True
    assert observed["compressed"]["oid"] == 99
    assert captured == [(execute, inventory, chunk)]


_POST_TARGET_PARITY_CODES = {
    "POST_TARGET_PARITY_MISSING",
    "POST_TARGET_PARITY_INVALID",
    "POST_TARGET_PARITY_DRIFT",
}


def _valid_window_parity() -> dict:
    return {
        "row_count": 1,
        "non_null_counts": {"run_id": 1},
        "checksum": "x",
        "inventory_digest": "inv",
        "range_start": "2026-01-01T00:00:00Z",
        "range_end": "2026-01-08T00:00:00Z",
    }


def _baseline_groups_with_parity(parity: object) -> list[dict]:
    groups = [_group(key, index) for index, key in enumerate(KEYS, start=1)]
    for group in groups:
        if parity is _MISSING:
            group.pop("parity", None)
        else:
            group["parity"] = parity
    return groups


def _named_group_result(durable: dict, parity: dict) -> dict:
    return {
        "key": durable_key(durable),
        "durable": durable,
        "residency": "already_target",
        "compressed": {"oid": durable["origin_oid"] + 1000},
        "members": [],
        "parity": parity,
        "inventory_digest": "inv",
        "complete_target": True,
    }


@pytest.mark.parametrize(
    ("parity", "code"),
    (
        (_MISSING, "POST_TARGET_PARITY_MISSING"),
        (None, "POST_TARGET_PARITY_MISSING"),
        ("not-an-object", "POST_TARGET_PARITY_INVALID"),
        ([], "POST_TARGET_PARITY_INVALID"),
        ({"row_count": 1, "checksum": "x"}, "POST_TARGET_PARITY_INVALID"),
        ({**_valid_window_parity(), "row_count": "1"}, "POST_TARGET_PARITY_INVALID"),
        ({**_valid_window_parity(), "non_null_counts": ["run_id"]}, "POST_TARGET_PARITY_INVALID"),
        ({**_valid_window_parity(), "extra": "not-a-window-field"}, "POST_TARGET_PARITY_INVALID"),
        ({**_valid_window_parity(), "non_null_counts": {"run_id": 2}}, "POST_TARGET_PARITY_INVALID"),
        ({**_valid_window_parity(), "checksum": "changed"}, "POST_TARGET_PARITY_DRIFT"),
    ),
    ids=(
        "missing",
        "null",
        "string",
        "list",
        "missing-required-field",
        "wrong-scalar-type",
        "wrong-container-type",
        "extra-field",
        "non-null-count-exceeds-row-count",
        "exact-mismatch",
    ),
)
def test_post_target_malformed_or_mismatched_baseline_parity_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    parity: object,
    code: str,
) -> None:
    private = _private_dir(tmp_path / "private")
    baseline_groups = _baseline_groups_with_parity(parity)
    baseline = _write_private_json(private / "baseline.json", {"groups": baseline_groups})
    output = private / "observed.json"
    current = _valid_window_parity() | {"checksum": "current"}
    named_calls: list[object] = []

    def fake_named(_execute: object, *, durable: dict, inventories: object, expected_parity: object = None) -> dict:
        named_calls.append(expected_parity)
        if expected_parity is not None and expected_parity != current:
            raise Issue1895ReadinessError(
                "business-window parity changed",
                code="POST_TARGET_PARITY_DRIFT",
                stage="post-target",
            )
        return _named_group_result(dict(durable), current)

    monkeypatch.setattr("packages.common.node27_issue1895_post_target.observe_named_group", fake_named)
    monkeypatch.setattr(
        "packages.common.node27_issue1895_post_target.derive_bound_inventories",
        lambda _execute: BoundInventories(
            river=HypertableInventory("hydro", "river_timeseries", (), "inv"),
            forcing=HypertableInventory("met", "forcing_station_timeseries", (), "inv"),
            digest="inv",
        ),
    )
    monkeypatch.setattr(
        "packages.common.node27_issue1895_post_target.classify_current_candidates",
        lambda *_args, **_kwargs: ((), tuple(KEYS)),
    )
    with pytest.raises(Issue1895ReadinessError) as refused:
        observe_post_target(
            baseline_groups=baseline_groups,
            execute=lambda *_args, **_kwargs: [],
            cutoff=datetime(2026, 1, 8, tzinfo=UTC),
            watermark=datetime(2026, 1, 15, tzinfo=UTC),
            lag_seconds=604800,
            reviewed_sha=SHA,
        )
    assert refused.value.code == code
    assert refused.value.code in _POST_TARGET_PARITY_CODES
    if code in {"POST_TARGET_PARITY_MISSING", "POST_TARGET_PARITY_INVALID"}:
        assert named_calls == []
    with pytest.raises(Issue1895ReadinessError) as published:
        run_post_target_observation(
            baseline_path=baseline,
            output_path=output,
            reviewed_sha=SHA,
            lag_seconds=604800,
            execute=lambda *_args, **_kwargs: [],
            watermark=datetime(2026, 1, 15, tzinfo=UTC),
            dsn="postgresql://nhms_display_ro@127.0.0.1/nhms",
        )
    assert published.value.code == code
    assert not output.exists()


def test_post_target_exact_equality_parity_is_accepted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    private = _private_dir(tmp_path / "private")
    parity = _valid_window_parity()
    baseline_groups = _baseline_groups_with_parity(parity)
    baseline = _write_private_json(private / "baseline.json", {"groups": baseline_groups})
    output = private / "observed.json"
    forwarded: list[object] = []

    def fake_named(_execute: object, *, durable: dict, inventories: object, expected_parity: object = None) -> dict:
        forwarded.append(expected_parity)
        assert expected_parity == parity
        return _named_group_result(dict(durable), parity)

    monkeypatch.setattr("packages.common.node27_issue1895_post_target.observe_named_group", fake_named)
    monkeypatch.setattr(
        "packages.common.node27_issue1895_post_target.derive_bound_inventories",
        lambda _execute: BoundInventories(
            river=HypertableInventory("hydro", "river_timeseries", (), "inv"),
            forcing=HypertableInventory("met", "forcing_station_timeseries", (), "inv"),
            digest="inv",
        ),
    )
    monkeypatch.setattr(
        "packages.common.node27_issue1895_post_target.classify_current_candidates",
        lambda *_args, **_kwargs: ((), tuple(KEYS)),
    )
    observed = observe_post_target(
        baseline_groups=baseline_groups,
        execute=lambda *_args, **_kwargs: [],
        cutoff=datetime(2026, 1, 8, tzinfo=UTC),
        watermark=datetime(2026, 1, 15, tzinfo=UTC),
        lag_seconds=604800,
        reviewed_sha=SHA,
    )
    assert all(item["complete_target"] is True for item in observed["groups"])
    assert None not in forwarded
    published = run_post_target_observation(
        baseline_path=baseline,
        output_path=output,
        reviewed_sha=SHA,
        lag_seconds=604800,
        execute=lambda *_args, **_kwargs: [],
        watermark=datetime(2026, 1, 15, tzinfo=UTC),
        dsn="postgresql://nhms_display_ro@127.0.0.1/nhms",
    )
    assert output.exists()
    assert published["groups"][0]["parity"] == parity


def test_post_target_caller_mutants_red_when_chunk_is_omitted_or_not_loaded_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 8, tzinfo=UTC)
    durable = {
        "hypertable_schema": "hydro",
        "hypertable_name": "river_timeseries",
        "origin_schema": "_timescaledb_internal",
        "origin_name": "origin_1",
        "origin_oid": 1,
        "range_start": "2026-01-01T00:00:00Z",
        "range_end": "2026-01-08T00:00:00Z",
    }
    members = (
        ResidencyMember("origin_heap", 1, "_timescaledb_internal", "origin_1", "r", "nhms_cold", 10, None, 3),
        ResidencyMember("compressed_heap", 99, "_timescaledb_internal", "comp_new", "r", "nhms_cold", 4, None, 5),
        ResidencyMember("index", 2, "_timescaledb_internal", "idx", "i", "nhms_cold", 1, 1, None),
        ResidencyMember("toast_heap", 3, "_timescaledb_internal", "toast", "t", "nhms_cold", 1, 1, None),
        ResidencyMember("toast_index", 4, "_timescaledb_internal", "toast_idx", "i", "nhms_cold", 1, 3, None),
        ResidencyMember("toast_heap", 5, "_timescaledb_internal", "ctoast", "t", "nhms_cold", 1, 99, None),
        ResidencyMember("toast_index", 6, "_timescaledb_internal", "ctoast_idx", "i", "nhms_cold", 1, 5, None),
    )
    group = ResidencyGroup(
        "hydro",
        "river_timeseries",
        1,
        "_timescaledb_internal",
        "origin_1",
        99,
        "_timescaledb_internal",
        "comp_new",
        start,
        end,
        True,
        members,
    )
    loaded = CatalogChunk(
        "hydro",
        "river_timeseries",
        1,
        "_timescaledb_internal",
        "origin_1",
        99,
        "_timescaledb_internal",
        "comp_new",
        start,
        end,
        True,
    )
    ranking = CatalogChunk(
        "hydro",
        "river_timeseries",
        7,
        "_timescaledb_internal",
        "origin_7",
        108,
        "_timescaledb_internal",
        "comp_rank",
        start,
        end,
        True,
    )
    inventory = HypertableInventory(schema="hydro", name="river_timeseries", columns=(), digest="inv")
    inventories = BoundInventories(river=inventory, forcing=inventory, digest="inv")
    execute = object()
    expected_parity = {
        "row_count": 1,
        "non_null_counts": {},
        "checksum": "x",
        "inventory_digest": "inv",
        "range_start": "2026-01-01T00:00:00Z",
        "range_end": "2026-01-08T00:00:00Z",
    }

    def fake_load(*_args: object, **_kwargs: object) -> CatalogChunk:
        return loaded

    def fake_collect(*_args: object, **_kwargs: object) -> ResidencyGroup:
        return group

    monkeypatch.setattr("packages.common.node27_issue1895_post_target.load_catalog_chunk", fake_load)
    monkeypatch.setattr("packages.common.node27_issue1895_post_target.collect_residency_group", fake_collect)

    def omitted(execute_arg: object, inventory_arg: object) -> WindowParity:
        del execute_arg, inventory_arg
        return WindowParity(1, (), "x", "inv", start, end)

    monkeypatch.setattr("packages.common.node27_issue1895_post_target.compute_window_parity", omitted)
    with pytest.raises(TypeError):
        observe_named_group(execute, durable=durable, inventories=inventories, expected_parity=expected_parity)

    captured: list[object] = []

    def wrong_chunk(execute_arg: object, inventory_arg: object, loaded_chunk: object) -> WindowParity:
        del execute_arg, inventory_arg
        captured.append(loaded_chunk)
        return WindowParity(1, (), "x", "inv", start, end)

    monkeypatch.setattr("packages.common.node27_issue1895_post_target.compute_window_parity", wrong_chunk)
    observe_named_group(execute, durable=durable, inventories=inventories, expected_parity=expected_parity)
    assert captured == [loaded]
    assert captured[0] is not ranking
    assert getattr(captured[0], "origin_oid") == 1


def test_w8_publication_is_exclusive_private_and_preserves_existing_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "w8.json"
    original = b'{"cutoff":"2026-09-04T00:00:00Z"}\n'

    monkeypatch.setattr(
        watermark_cli,
        "resolve_readonly_dsn",
        lambda **_kwargs: "postgresql://nhms_display_ro@127.0.0.1/nhms",
    )
    monkeypatch.setattr(
        watermark_cli,
        "observe_current_cutoff",
        lambda **_kwargs: {
            "watermark": "2026-09-06T12:00:00Z",
            "cutoff": "2026-09-04T12:00:00Z",
            "lag_seconds": 172800,
        },
    )
    assert watermark_cli.main(["--output", str(output), "--lag-seconds", "172800"]) == 0
    assert output.stat().st_mode & 0o777 == 0o600
    first = output.read_bytes()
    assert watermark_cli.main(["--output", str(output), "--lag-seconds", "172800"]) == 1
    assert output.read_bytes() == first

    output.unlink()
    symlink_target = tmp_path / "other.json"
    symlink_target.write_bytes(original)
    output.symlink_to(symlink_target)
    assert watermark_cli.main(["--output", str(output), "--lag-seconds", "172800"]) == 1
    assert output.is_symlink()


def test_post_target_observer_closes_display_watermark_failures_without_secret_or_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from packages.common.display_watermark import DisplayWatermarkError

    secret = "postgresql://nhms_display_ro:synthetic-secret@127.0.0.1/nhms"

    def unavailable(**_kwargs: object) -> dict[str, object]:
        try:
            raise RuntimeError(secret)
        except RuntimeError as cause:
            raise DisplayWatermarkError("watermark unavailable") from cause

    monkeypatch.setattr(post_target_observe_cli, "run_post_target_observation", unavailable)
    monkeypatch.setattr(
        post_target_observe_cli,
        "resolve_readonly_dsn",
        lambda **_kwargs: secret,
    )
    assert post_target_observe_cli.main(
        [
            "--baseline", str(tmp_path / "baseline.json"),
            "--output", str(tmp_path / "post.json"),
            "--reviewed-sha", SHA,
            "--lag-seconds", "172800",
        ]
    ) == 1
    captured = capsys.readouterr()
    assert captured.err.strip() == "WATERMARK_UNAVAILABLE"
    assert secret not in captured.err
    assert "Traceback" not in captured.err


def test_post_target_observer_propagates_programming_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(post_target_observe_cli, "resolve_readonly_dsn", lambda **_kwargs: "readonly-dsn")

    def programming_error(**_kwargs: object) -> None:
        raise RuntimeError("programming sentinel")

    monkeypatch.setattr(post_target_observe_cli, "run_post_target_observation", programming_error)
    with pytest.raises(RuntimeError, match="programming sentinel"):
        post_target_observe_cli.main(
            [
                "--baseline", str(tmp_path / "baseline.json"),
                "--output", str(tmp_path / "post.json"),
                "--reviewed-sha", SHA,
                "--lag-seconds", "172800",
            ]
        )


def test_w8_cli_closes_display_watermark_failures_without_secret_or_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from packages.common.display_watermark import DisplayWatermarkError

    secret = "postgresql://nhms_display_ro:synthetic-secret@127.0.0.1/nhms"
    monkeypatch.setattr(watermark_cli, "resolve_readonly_dsn", lambda **_kwargs: secret)

    def unavailable(**_kwargs: object) -> dict[str, object]:
        try:
            raise RuntimeError(secret)
        except RuntimeError as cause:
            raise DisplayWatermarkError("watermark unavailable") from cause

    monkeypatch.setattr(watermark_cli, "observe_current_cutoff", unavailable)
    assert watermark_cli.main(["--output", str(tmp_path / "w8.json"), "--lag-seconds", "172800"]) == 1
    captured = capsys.readouterr()
    assert captured.err.strip() == "WATERMARK_UNAVAILABLE"
    assert secret not in captured.err
    assert "Traceback" not in captured.err


def test_w8_cli_refuses_symlink_display_env_before_cutoff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    private = _private_dir(tmp_path / "private")
    real = private / "display.env"
    real.write_text("DATABASE_URL=postgresql://nhms_display_ro:display-secret@127.0.0.1/nhms\n", encoding="utf-8")
    os.chmod(real, 0o600)
    linked = tmp_path / "display.env"
    linked.symlink_to(real)
    called = {"n": 0}

    def observe(**_kwargs: object) -> dict[str, object]:
        called["n"] += 1
        raise AssertionError("observe_current_cutoff must not run on unsafe display.env")

    monkeypatch.setattr(watermark_cli, "observe_current_cutoff", observe)
    monkeypatch.delenv("NHMS_DISPLAY_READONLY_DATABASE_URL", raising=False)
    monkeypatch.delenv("NHMS_READONLY_DB_VALIDATION_DATABASE_URL", raising=False)
    output = private / "w8.json"
    assert watermark_cli.main(
        ["--output", str(output), "--lag-seconds", "172800", "--display-env", str(linked)]
    ) == 1
    assert called["n"] == 0
    assert not output.exists()


def _checkout_display_env(tmp_path: Path) -> Path:
    parent = tmp_path / "infra" / "env"
    parent.mkdir(parents=True)
    os.chmod(parent, 0o755)
    env = parent / "display.env"
    env.write_text("DATABASE_URL=postgresql://nhms_display_ro:display-secret@127.0.0.1/nhms\n", encoding="utf-8")
    os.chmod(env, 0o600)
    return env


def _w8_output(tmp_path: Path) -> Path:
    output = tmp_path / "out" / "w8.json"
    output.parent.mkdir()
    os.chmod(output.parent, 0o700)
    return output


def test_w8_and_post_target_accept_valid_display_env_under_parent_0755(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _checkout_display_env(tmp_path)
    expected = "postgresql://nhms_display_ro:display-secret@127.0.0.1/nhms"
    observed = {"dsn": None, "n": 0}

    def observe(**kwargs: object) -> dict[str, object]:
        observed["dsn"] = kwargs.get("dsn")
        observed["n"] += 1
        return {"watermark": "2026-09-06T12:00:00Z", "cutoff": "2026-09-04T12:00:00Z", "lag_seconds": 172800}

    monkeypatch.setattr(watermark_cli, "observe_current_cutoff", observe)
    monkeypatch.delenv("NHMS_DISPLAY_READONLY_DATABASE_URL", raising=False)
    monkeypatch.delenv("NHMS_READONLY_DB_VALIDATION_DATABASE_URL", raising=False)
    output = _w8_output(tmp_path)
    assert watermark_cli.main(["--output", str(output), "--lag-seconds", "172800", "--display-env", str(env)]) == 0
    assert observed == {"dsn": expected, "n": 1}
    assert oct(output.stat().st_mode & 0o777) == "0o600"
    captured: dict[str, object] = {}

    def capture(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(post_target_observe_cli, "run_post_target_observation", capture)
    baseline = _write_private_json(_private_dir(tmp_path / "census") / "baseline.json", {"groups": []})
    argv = [
        "--baseline",
        str(baseline),
        "--output",
        str(baseline.with_name("observed.json")),
        "--reviewed-sha",
        SHA,
        "--lag-seconds",
        "172800",
        "--display-env",
        str(env),
    ]
    assert post_target_observe_cli.main(argv) == 0
    assert captured["dsn"] == expected and captured["baseline_path"] == baseline


@pytest.mark.parametrize("kind", ("symlink", "mode", "hardlink", "parent-symlink"))
def test_w8_cli_refuses_unsafe_display_env_identity_before_cutoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    env = _checkout_display_env(tmp_path)
    display_env = env
    if kind == "parent-symlink":
        linked = tmp_path / "linked-env"
        linked.symlink_to(env.parent, target_is_directory=True)
        display_env = linked / "display.env"
    else:
        _substitute_identity(env, kind)
    called = {"n": 0}
    monkeypatch.setattr(watermark_cli, "observe_current_cutoff", lambda **_k: called.__setitem__("n", called["n"] + 1))
    monkeypatch.delenv("NHMS_DISPLAY_READONLY_DATABASE_URL", raising=False)
    monkeypatch.delenv("NHMS_READONLY_DB_VALIDATION_DATABASE_URL", raising=False)
    output = _w8_output(tmp_path)
    argv = ["--output", str(output), "--lag-seconds", "172800", "--display-env", str(display_env)]
    assert watermark_cli.main(argv) == 1
    assert called["n"] == 0 and not output.exists()


def test_fs_reconcile_cli_refuses_symlink_and_parent_0755_receipt_before_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private = _private_dir(tmp_path / "private")
    receipt = _write_private_json(private / "receipt.json", _shipping_receipt(call_index=1))
    called = {"n": 0}

    def approve(**_kwargs: object) -> dict[str, object]:
        called["n"] += 1
        return {"approved": True, "moved_member_bytes": 10}

    monkeypatch.setattr(fs_reconcile_cli, "reconcile_moved_group_filesystem", approve)
    linked = tmp_path / "receipt-link.json"
    linked.symlink_to(receipt)
    argv = [
        "--receipt",
        str(linked),
        "--hot-avail-before",
        "100",
        "--hot-avail-after",
        "110",
        "--cold-avail-before",
        "210",
        "--cold-avail-after",
        "200",
    ]
    assert fs_reconcile_cli.main(argv) == 1
    assert called["n"] == 0
    os.chmod(private, 0o755)
    argv[1] = str(receipt)
    assert fs_reconcile_cli.main(argv) == 1
    assert called["n"] == 0
    os.chmod(private, 0o700)
    assert fs_reconcile_cli.main(argv) == 0
    assert called["n"] == 1
