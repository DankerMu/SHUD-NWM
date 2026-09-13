"""Reviewed original authority across census, G5, G6 and G8 public seams."""

from __future__ import annotations

import ast
import copy
import hashlib
import importlib
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from packages.common import node27_issue1895_census_bind as binding
from packages.common import node27_issue1895_post_target as post_target
from packages.common.node27_cold_residency_census_policy import CensusPolicyError, capacity_policy
from packages.common.node27_issue1895_receipt import assert_sequential_tick_receipt, durable_key
from packages.common.node27_issue1895_timer import (
    assert_exact_cold_groups,
    assert_natural_tick_selection,
    persist_baseline_groups,
)
from packages.common.node27_issue1895_types import Issue1895ReadinessError
from scripts import node27_cold_residency_census as census
from scripts import node27_issue1895_sequential_receipt as sequential_cli
from tests.cold_residency_fakes import CUTOFF, LAG, WATERMARK, chunk, complete_relations
from tests.test_node27_cold_residency_census import CensusConnection

SHA = "a" * 40


def population(count: int, *, cold: bool = False) -> CensusConnection:
    connection = CensusConnection()
    for index in range(count):
        add_group(connection, index, cold=cold)
    return connection


def add_group(connection: CensusConnection, index: int, *, cold: bool = False) -> None:
    item = chunk(
        origin_oid=10000 + index * 10,
        compressed_oid=20000 + index * 10,
        origin_name=f"_hyper_1_{index}_chunk",
        compressed_name=f"compress_2_{index}_chunk",
        range_start=CUTOFF - timedelta(days=100 - index),
        range_end=CUTOFF - timedelta(days=99 - index),
    )
    connection.load_group(
        item,
        complete_relations(
            origin_oid=item.origin_oid,
            compressed_oid=item.compressed_oid,
            origin_name=item.origin_name,
            compressed_name=item.compressed_name,
            origin_space="nhms_cold" if cold else "pg_default",
        ),
    )
    connection.compression_bytes[item.origin_name] = 1000 + index * 137


def document(count: int, connection: CensusConnection | None = None) -> dict:
    return census.observe_census(
        census.CensusObserver(connection if connection is not None else population(count)),
        require_count=count,
        lag_seconds=LAG,
        watermark=WATERMARK,
        now_utc=WATERMARK,
        head_sha=SHA,
        database_url="postgresql://readonly:secret@localhost/isolated",
    )


def private_json(path: Path, value: dict) -> tuple[Path, str]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    raw = json.dumps(value, separators=(",", ":")).encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    return path, hashlib.sha256(raw).hexdigest()


def frozen_files(root: Path, value: dict) -> tuple[Path, Path, Path, str]:
    original, frozen = private_json(root / "original.json", value)
    current, _ = private_json(root / "current.json", value)
    bracket = root / "bracket.txt"
    bracket.write_text("2026-07-11T11:59:59Z\n2026-07-11T12:00:01Z\n0\n")
    bracket.chmod(0o600)
    return original, current, bracket, frozen


def receipt(groups: list[dict], call: int, *, outcome: str = "migrated") -> dict:
    return {
        "schema_version": "1.1",
        "per_tick_bound": 1,
        "outcome": "clean",
        "selected": [{"outcome": "already_cold", "durable": group["durable"]} for group in groups[: call - 1]]
        + [
            {
                "outcome": outcome,
                "durable": groups[call - 1]["durable"],
                "after": {"members": groups[call - 1]["members"]},
            }
        ],
        "deferred": [{"reason": "per_tick_bound", "durable": group["durable"]} for group in groups[call:]],
    }


@pytest.mark.parametrize("count", [1, 3, 63])
def test_same_complete_frozen_file_crosses_all_gates(tmp_path: Path, count: int) -> None:
    value = document(count)
    assert value["verdict"] == "GO", value["blockers"]
    assert value["required_group_count"] == value["resolved_group_count"] == count
    assert all(len(group["members"]) == 8 and group["parity"]["row_count"] == 2 for group in value["groups"])
    original, current, bracket, frozen = frozen_files(tmp_path / "private", value)
    assert original.stat().st_size < 1024**2
    loaded, approved = binding.load_original_census(
        original,
        expected_original_sha256=frozen,
        reviewed_sha=SHA,
    )
    assert approved == count and loaded == value
    assert (
        binding.bind_pre_movement_census(
            original_path=original,
            current_path=current,
            bracket_path=bracket,
            expected_digest=value["census_digest"],
            reviewed_sha=SHA,
            expected_original_sha256=frozen,
        )
        == value
    )
    assert (
        importlib.import_module("scripts.node27_issue1895_census_bind").main(
            [
                "--original",
                str(original),
                "--current",
                str(current),
                "--bracket",
                str(bracket),
                "--digest",
                value["census_digest"],
                "--reviewed-sha",
                SHA,
                "--original-sha256",
                frozen,
            ]
        )
        == 0
    )
    for call in range(1, count + 1):
        shipping = receipt(loaded["groups"], call)
        migrated = assert_sequential_tick_receipt(
            shipping,
            ordered_keys=loaded["group_keys"],
            call_index=call,
            expected_count=approved,
        )
        assert durable_key(migrated["durable"]) == loaded["group_keys"][call - 1]
        path, _ = private_json(original.parent / "receipt.json", shipping)
        assert (
            sequential_cli.main(
                [
                    "--census",
                    str(original),
                    "--receipt",
                    str(path),
                    "--call-index",
                    str(call),
                    "--original-sha256",
                    frozen,
                    "--reviewed-sha",
                    SHA,
                ]
            )
            == 0
        )
    connection = population(count, cold=True)
    observed = post_target.run_post_target_observation(
        baseline_path=original,
        output_path=original.parent / "post.json",
        reviewed_sha=SHA,
        expected_original_sha256=frozen,
        lag_seconds=LAG,
        watermark=WATERMARK,
        execute=census.CensusObserver(connection).binder(),
    )
    baseline = persist_baseline_groups(loaded["groups"], expected_count=approved)
    assert tuple(item["key"] for item in baseline) == tuple(loaded["group_keys"])
    assert_exact_cold_groups(observed["groups"], baseline=baseline, expected_count=approved)
    assert observed["baseline_keys"] == loaded["group_keys"]
    assert set(observed["complete_target_keys"]) == set(loaded["group_keys"])
    assert observed["complete_source_keys"] == []
    assert_natural_tick_selection(
        {"outcome": "no_op", "selected": [], "deferred": []},
        baseline_keys=loaded["group_keys"],
        expected_count=approved,
        remaining_complete_source_keys=[],
        newly_terminal_keys=[],
    )
    reconcile_cli = importlib.import_module("scripts.node27_issue1895_group_reconcile")
    natural_path, _ = private_json(
        original.parent / "natural.json",
        {
            "schema_version": "1.1",
            "per_tick_bound": 1,
            "outcome": "no_op",
            "head_sha": SHA,
            "watermark": value["watermark"],
            "cutoff": value["cutoff"],
            "selected": [],
            "deferred": [],
        },
    )
    assert (
        reconcile_cli.main(
            [
                "--baseline",
                str(original),
                "--original-sha256",
                frozen,
                "--reviewed-sha",
                SHA,
                "--observed",
                str(original.parent / "post.json"),
                "--receipt",
                str(natural_path),
                "--expected-cutoff",
                value["cutoff"],
                "--expected-watermark",
                value["watermark"],
            ]
        )
        == 0
    )


@pytest.mark.parametrize("raw", ["0", "64", "01", "+1", "-1", "1.0", "1e0", " 1", "1 ", "", "١"])
def test_cli_rejects_noncanonical_reviewed_counts(raw: str) -> None:
    with pytest.raises(census.CensusError):
        census.require_count_from_arg(raw)


@pytest.mark.parametrize("count", [1, 63])
def test_cli_supported_edges_retain_extra_slot(count: int) -> None:
    assert census.require_count_from_arg(str(count)) == count
    assert census.per_table_catalog_limit(count) == count + 1


@pytest.mark.parametrize("bad", [True, False, 0, 64, "3", 3.0, None])
def test_original_counts_are_strict_integers(bad: object) -> None:
    value = document(3)
    with pytest.raises(Issue1895ReadinessError):
        binding.validate_original_census(value, expected_count=bad, reviewed_sha=SHA)
    value["required_group_count"] = bad
    with pytest.raises(Issue1895ReadinessError):
        binding.validate_original_census(value, expected_count=3, reviewed_sha=SHA)


@pytest.mark.parametrize(
    "mutation",
    [
        "resolved",
        "config",
        "capacity_count",
        "capacity_type",
        "duplicate",
        "missing",
        "extra",
        "key_order",
        "group_order",
        "identity",
        "head",
        "verdict",
        "residency",
        "parity",
        "capacity_bytes",
    ],
)
def test_original_rejects_raw_count_identity_and_preimage_drift(mutation: str) -> None:
    value = document(3)
    if mutation == "resolved":
        value["resolved_group_count"] = 2
    elif mutation == "config":
        value["config"]["require_count"] = 2
    elif mutation == "capacity_count":
        value["capacity_policy"]["group_count"] = "2"
    elif mutation == "capacity_type":
        value["capacity_policy"]["group_count"] = 3
    elif mutation == "duplicate":
        value["groups"].append(copy.deepcopy(value["groups"][0]))
    elif mutation == "missing":
        value["groups"].pop()
    elif mutation == "extra":
        value["groups"].append(document(4)["groups"][-1])
    elif mutation == "key_order":
        value["group_keys"].reverse()
    elif mutation == "group_order":
        value["groups"].reverse()
    elif mutation == "identity":
        value["groups"][0]["durable"]["origin_oid"] += 1
    elif mutation == "head":
        value["head_sha"] = "b" * 40
    elif mutation == "verdict":
        value["verdict"] = "NO-GO"
    elif mutation == "residency":
        value["groups"][0]["residency"] = "mixed"
    elif mutation == "parity":
        value["groups"][0]["parity"] = {"row_count": 2}
    else:
        value["capacity_policy"]["rollback_headroom_bytes"] = "1"
    with pytest.raises(Issue1895ReadinessError):
        binding.validate_original_census(value, expected_count=3, reviewed_sha=SHA)


def test_self_consistent_replacement_cannot_reauthorize_original(tmp_path: Path) -> None:
    original, current, bracket, frozen = frozen_files(tmp_path / "private", document(3))
    replacement = document(4)
    private_json(original, replacement)
    private_json(current, replacement)
    with pytest.raises(Issue1895ReadinessError):
        binding.load_original_census(original, expected_original_sha256=frozen, reviewed_sha=SHA)
    with pytest.raises(Issue1895ReadinessError):
        binding.bind_pre_movement_census(
            original_path=original,
            current_path=current,
            bracket_path=bracket,
            expected_digest=replacement["census_digest"],
            reviewed_sha=SHA,
            expected_original_sha256=frozen,
        )
    opened = []
    with pytest.raises(Issue1895ReadinessError):
        post_target.run_post_target_observation(
            baseline_path=original,
            output_path=original.parent / "post.json",
            reviewed_sha=SHA,
            expected_original_sha256=frozen,
            lag_seconds=LAG,
            watermark=WATERMARK,
            connect=lambda dsn: opened.append(dsn),
            dsn="postgresql://secret@localhost/isolated",
        )
    assert opened == [] and not (original.parent / "post.json").exists()


def test_oversized_original_refuses_under_unchanged_held_limit(tmp_path: Path) -> None:
    value = document(63)
    value["padding"] = "x" * 1024**2
    original, frozen = private_json(tmp_path / "private" / "original.json", value)
    with pytest.raises(Issue1895ReadinessError):
        binding.load_original_census(original, expected_original_sha256=frozen, reviewed_sha=SHA)


@pytest.mark.parametrize(
    "mutation", ["suffix", "index_zero", "index_extra", "extra_migration", "prior", "bound", "duplicate_keys"]
)
def test_sequential_ticks_refuse_wrong_transition(mutation: str) -> None:
    value = document(3)
    shipping = receipt(value["groups"], 1)
    call = 1
    if mutation == "suffix":
        shipping["deferred"].reverse()
    elif mutation == "index_zero":
        call = 0
    elif mutation == "index_extra":
        call = 4
    elif mutation == "extra_migration":
        shipping["selected"].append({"outcome": "migrated", "durable": value["groups"][1]["durable"]})
    elif mutation == "prior":
        shipping["selected"].append({"outcome": "already_cold", "durable": value["groups"][2]["durable"]})
    elif mutation == "bound":
        shipping["per_tick_bound"] = 2
    else:
        value["group_keys"][1] = value["group_keys"][0]
    with pytest.raises(Issue1895ReadinessError):
        assert_sequential_tick_receipt(shipping, ordered_keys=value["group_keys"], call_index=call, expected_count=3)


def test_planned_preview_preserves_one_group_bound() -> None:
    value = document(3)
    result = assert_sequential_tick_receipt(
        receipt(value["groups"], 1, outcome="planned"),
        ordered_keys=value["group_keys"],
        call_index=1,
        expected_count=3,
        migrate_outcome="planned",
    )
    assert durable_key(result["durable"]) == value["group_keys"][0]


def test_newly_terminal_identity_remains_outside_original_population() -> None:
    value = document(3)
    connection = population(3, cold=True)
    add_group(connection, 3, cold=True)
    add_group(connection, 4)
    observed = post_target.observe_post_target(
        baseline_groups=value["groups"],
        expected_count=3,
        execute=census.CensusObserver(connection).binder(),
        cutoff=CUTOFF,
        watermark=WATERMARK,
        lag_seconds=LAG,
        reviewed_sha=SHA,
    )
    assert observed["baseline_keys"] == value["group_keys"]
    extra = document(5)["groups"]
    newly = post_target.newly_terminal_keys(
        pre_target_keys=value["group_keys"],
        post_target_keys=observed["complete_target_keys"],
    )
    assert newly == (extra[3]["key"],)
    assert observed["complete_source_keys"] == [extra[4]["key"]]
    shipping = {
        "outcome": "clean",
        "selected": [{"outcome": "migrated", "durable": extra[3]["durable"]}],
        "deferred": [{"reason": "per_tick_bound", "durable": extra[4]["durable"]}],
    }
    assert_natural_tick_selection(
        shipping,
        baseline_keys=value["group_keys"],
        expected_count=3,
        remaining_complete_source_keys=observed["complete_source_keys"],
        newly_terminal_keys=newly,
    )
    with pytest.raises(Issue1895ReadinessError):
        assert_natural_tick_selection(
            shipping,
            baseline_keys=value["group_keys"],
            expected_count=3,
            remaining_complete_source_keys=[],
            newly_terminal_keys=newly,
        )
    with pytest.raises(Issue1895ReadinessError):
        assert_exact_cold_groups(observed["groups"] + [extra[3]], baseline=value["groups"], expected_count=3)


def test_measured_capacity_uses_every_group_not_sample_times_count() -> None:
    policy = capacity_policy(expansions=[101, 307, 211], retained=[11, 23, 47], group_count=3)
    assert {
        key: policy[key]
        for key in (
            "status",
            "group_count",
            "E",
            "S",
            "cold_reserve_bytes",
            "wal_reserve_bytes",
            "install_required_bytes",
            "rollback_headroom_bytes",
            "installer_required_cold_free_bytes",
        )
    } == {
        "status": "resolved",
        "group_count": "3",
        "E": "307",
        "S": "81",
        "cold_reserve_bytes": "307",
        "wal_reserve_bytes": "307",
        "install_required_bytes": "81",
        "rollback_headroom_bytes": "614",
        "installer_required_cold_free_bytes": "695",
    }
    assert policy["expansion_values"] == ["101", "307", "211"]
    assert policy["retained_values"] == ["11", "23", "47"]


@pytest.mark.parametrize(
    "expansions,retained",
    [
        ([0], [1]),
        ([1], [-1]),
        ([2**62], [1]),
        ([1, 1], [2**62, 2**62]),
        ([2**62 - 1], [2]),
        ([True], [1]),
        ([1], [False]),
    ],
)
def test_capacity_positive_and_overflow_guards(expansions: list, retained: list) -> None:
    with pytest.raises(CensusPolicyError):
        capacity_policy(expansions=expansions, retained=retained, group_count=len(expansions))


def test_capacity_exact_signed_bigint_boundary() -> None:
    policy = capacity_policy(expansions=[2**62 - 1], retained=[1], group_count=1)
    assert policy["installer_required_cold_free_bytes"] == "9223372036854775807"


def test_census_reports_surplus_without_truncating() -> None:
    value = document(3, population(4))
    assert value["verdict"] == "NO-GO"
    assert value["required_group_count"] == 3 and value["resolved_group_count"] == 4
    assert len(value["groups"]) == len(value["group_keys"]) == 4


@pytest.mark.parametrize(
    "module",
    [
        "node27_issue1895_census_bind",
        "node27_issue1895_sequential_receipt",
        "node27_issue1895_post_target_observe",
        "node27_issue1895_group_reconcile",
        "node27_issue1895_cutoff_count",
    ],
)
@pytest.mark.parametrize("flag", ["--original-sha256", "--reviewed-sha"])
def test_every_original_cli_requires_both_external_anchors(module: str, flag: str) -> None:
    parser = importlib.import_module(f"scripts.{module}").build_parser()
    inputs = {
        "node27_issue1895_census_bind": [
            "--current",
            "current",
            "--original",
            "original",
            "--digest",
            "digest",
            "--bracket",
            "bracket",
        ],
        "node27_issue1895_sequential_receipt": ["--receipt", "receipt", "--census", "original", "--call-index", "1"],
        "node27_issue1895_post_target_observe": ["--baseline", "original", "--output", "output", "--lag-seconds", "1"],
        "node27_issue1895_group_reconcile": [
            "--baseline",
            "original",
            "--observed",
            "observed",
            "--receipt",
            "receipt",
            "--expected-cutoff",
            "cutoff",
            "--expected-watermark",
            "watermark",
        ],
        "node27_issue1895_cutoff_count": ["--original", "original"],
    }
    argv = inputs[module] + ["--original-sha256", "b" * 64, "--reviewed-sha", SHA]
    parser.parse_args(argv)
    index = argv.index(flag)
    with pytest.raises(SystemExit) as refused:
        parser.parse_args(argv[:index] + argv[index + 2 :])
    assert refused.value.code == 2


def test_pinned_runtime_invokes_count_discriminator() -> None:
    tree = ast.parse(Path("tests/test_compressed_chunk_cold_runtime_integration.py").read_text())
    harness = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "test_isolated_cluster_production_runtime_not_probe_executor"
    )
    calls = [
        node
        for node in ast.walk(harness)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_assert_reviewed_count_discriminator"
    ]
    assert len(calls) == 1


def test_named_observation_rejects_duplicate_before_query() -> None:
    value = document(3)
    groups = [value["groups"][0], value["groups"][0], value["groups"][2]]
    queried = []
    with pytest.raises(Issue1895ReadinessError):
        post_target.observe_post_target(
            baseline_groups=groups,
            expected_count=3,
            execute=lambda *args: queried.append(args),
            cutoff=CUTOFF,
            watermark=WATERMARK,
            lag_seconds=LAG,
            reviewed_sha=SHA,
        )
    assert queried == []


def test_current_order_and_duplicates_cannot_hide_in_maps(tmp_path: Path) -> None:
    value = document(3)
    original, current, bracket, frozen = frozen_files(tmp_path / "private", value)
    for changed in (list(reversed(value["groups"])), value["groups"] + [value["groups"][0]]):
        drift = copy.deepcopy(value)
        drift["groups"] = changed
        private_json(current, drift)
        with pytest.raises(Issue1895ReadinessError):
            binding.bind_pre_movement_census(
                original_path=original,
                current_path=current,
                bracket_path=bracket,
                expected_digest=value["census_digest"],
                reviewed_sha=SHA,
                expected_original_sha256=frozen,
            )


@pytest.mark.parametrize("observed_count", [0, 3, 4])
def test_cutoff_count_observes_catalog_not_original_n(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    observed_count: int,
) -> None:
    cutoff_cli = importlib.import_module("scripts.node27_issue1895_cutoff_count")
    original, frozen = private_json(tmp_path / "private" / "original.json", document(3))
    connection = population(observed_count)
    connection.fail_sql = "checksum_xor"
    connection.fail_exc = AssertionError("G3 attempted full parity")
    result = cutoff_cli.main(
        ["--original", str(original), "--original-sha256", frozen, "--reviewed-sha", SHA],
        env={"DATABASE_URL": "postgresql://readonly:secret@localhost/isolated"},
        connect=lambda dsn: connection,
        watermark_fetcher=lambda dsn, connect=None: WATERMARK,
    )
    assert result == 0
    assert capsys.readouterr().out.strip() == str(observed_count)


@pytest.mark.parametrize("state", ["mixed", "incomplete", "inventory", "overscan", "hash"])
def test_cutoff_count_refuses_without_integer_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    state: str,
) -> None:
    cutoff_cli = importlib.import_module("scripts.node27_issue1895_cutoff_count")
    original, frozen = private_json(tmp_path / "private" / "original.json", document(3))
    connection = population(6 if state == "overscan" else 3)
    if state == "mixed":
        relation = connection.relations[20000]
        connection.relations[20000] = replace(relation, tablespace="nhms_cold")
    elif state == "incomplete":
        del connection.relations[20000]
    elif state == "inventory":
        connection.parent_oids[("hydro", "river_timeseries")] += 1
    elif state == "hash":
        private_json(original, document(4))
    opened = []

    def connect(dsn):
        opened.append(dsn)
        return connection

    result = cutoff_cli.main(
        ["--original", str(original), "--original-sha256", frozen, "--reviewed-sha", SHA],
        env={"DATABASE_URL": "postgresql://readonly:secret@localhost/isolated"},
        connect=connect,
        watermark_fetcher=lambda dsn, connect=None: WATERMARK,
    )
    captured = capsys.readouterr()
    assert result != 0 and captured.out == ""
    assert "secret" not in captured.err and "Traceback" not in captured.err
    if state == "hash":
        assert opened == []


def test_g1_executable_policy_freezes_external_count_and_held_hash(tmp_path: Path, monkeypatch) -> None:
    from tests.test_issue1895_runbook_contract import _gate_bash

    value = document(3)
    original, _current, bracket, frozen = frozen_files(tmp_path / "private", value)
    policy = original.parent / "policy.env"
    for key, setting in {
        "CENSUS_ARTIFACT": str(original),
        "POLICY_FILE": str(policy),
        "REQUIRE_COUNT": "3",
        "REVIEWED_SHA": SHA,
        "CENSUS_BRACKET": str(bracket),
        "BRACKET_FILE": str(bracket),
    }.items():
        monkeypatch.setenv(key, setting)
    blocks = [body for _opening, body in _gate_bash("G1") if "O_EXCL" in body and "POLICY_FILE" in body]
    assert len(blocks) == 1
    program = blocks[0].split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
    exec(compile(program, "<runbook-G1-policy>", "exec"), {})
    frozen_values = dict(line.split("=", 1) for line in policy.read_text().splitlines())
    assert frozen_values["REQUIRE_COUNT"] == "3"
    assert frozen_values["ORIGINAL_CENSUS_SHA256"] == frozen
    assert frozen_values["CENSUS_DIGEST"] == value["census_digest"]
    assert policy.stat().st_mode & 0o777 == 0o600
    saved = policy.read_bytes()
    private_json(original, document(4))
    with pytest.raises((FileExistsError, AssertionError, Issue1895ReadinessError, CensusPolicyError)):
        exec(compile(program, "<runbook-G1-policy>", "exec"), {})
    assert policy.read_bytes() == saved


@pytest.mark.parametrize(
    "mutation", ["duplicate_keys", "missing_keys", "extra_keys", "same_length_duplicate", "measured"]
)
def test_original_raw_identity_set_and_measured_capacity_are_authoritative(mutation: str) -> None:
    value = document(3)
    if mutation == "duplicate_keys":
        value["group_keys"][1] = value["group_keys"][0]
    elif mutation == "missing_keys":
        value["group_keys"].pop()
    elif mutation == "extra_keys":
        value["group_keys"].append(document(4)["group_keys"][-1])
    elif mutation == "same_length_duplicate":
        value["groups"][1] = copy.deepcopy(value["groups"][0])
    else:
        value["groups"][1]["before_compression_total_bytes"] = 9000
    with pytest.raises(Issue1895ReadinessError):
        binding.validate_original_census(value, expected_count=3, reviewed_sha=SHA)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "mode", "parent"])
def test_original_loader_retains_private_held_identity_rules(tmp_path: Path, kind: str) -> None:
    import os

    original, frozen = private_json(tmp_path / "private" / "original.json", document(3))
    if kind == "symlink":
        held = original.with_name("held.json")
        original.rename(held)
        original.symlink_to(held)
    elif kind == "hardlink":
        os.link(original, original.with_name("linked.json"))
    elif kind == "mode":
        original.chmod(0o644)
    else:
        original.parent.chmod(0o755)
    with pytest.raises(Issue1895ReadinessError):
        binding.load_original_census(original, expected_original_sha256=frozen, reviewed_sha=SHA)


def test_g6_executable_key_extraction_cannot_authorize_replacement(tmp_path: Path, monkeypatch, capsys) -> None:
    import re
    import sys

    from tests.test_issue1895_runbook_contract import _gate_bash

    value = document(3)
    original, frozen = private_json(tmp_path / "private" / "original.json", value)
    loop = next(body for _opening, body in _gate_bash("G6") if "while IFS= read -r GROUP" in body)
    line = next(line for line in loop.splitlines() if line.startswith("done < <("))
    match = re.search(r"python -c '([^']+)'", line)
    assert match is not None
    monkeypatch.setattr(sys, "argv", ["-c", str(original), frozen, SHA])
    program = compile(match.group(1), "<runbook-G6-keys>", "exec")
    exec(program, {})
    assert capsys.readouterr().out.splitlines() == value["group_keys"]
    private_json(original, document(4))
    with pytest.raises(Issue1895ReadinessError):
        exec(program, {})
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "gate,module,occurrences",
    [
        ("G3", "node27_issue1895_cutoff_count", 1),
        ("G5", "node27_issue1895_census_bind", 1),
        ("G6", "node27_issue1895_sequential_receipt", 1),
        ("G8", "node27_issue1895_post_target_observe", 2),
        ("G8", "node27_issue1895_group_reconcile", 1),
    ],
)
def test_documented_original_cli_reaches_validation_without_pythonpath(
    tmp_path: Path,
    gate: str,
    module: str,
    occurrences: int,
) -> None:
    import os
    import shlex
    import subprocess
    import sys

    from tests.test_issue1895_runbook_contract import _gate

    # A held, hash-matching original reaches the shared validator (including its
    # post-target import), but invalid N prevents every database connection.
    invalid = document(3)
    invalid["required_group_count"] = 0
    original, frozen = private_json(tmp_path / "private" / "original.json", invalid)
    missing = str(original.with_name("missing.json"))
    output = original.with_name("output.json")
    inputs = {
        "node27_issue1895_cutoff_count": ["--original", str(original)],
        "node27_issue1895_census_bind": [
            "--original",
            str(original),
            "--current",
            missing,
            "--bracket",
            missing,
            "--digest",
            invalid["census_digest"],
        ],
        "node27_issue1895_sequential_receipt": [
            "--census",
            str(original),
            "--receipt",
            missing,
            "--call-index",
            "1",
        ],
        "node27_issue1895_post_target_observe": [
            "--baseline",
            str(original),
            "--output",
            str(output),
            "--lag-seconds",
            str(LAG),
        ],
        "node27_issue1895_group_reconcile": [
            "--baseline",
            str(original),
            "--observed",
            missing,
            "--receipt",
            missing,
            "--expected-cutoff",
            invalid["cutoff"],
            "--expected-watermark",
            invalid["watermark"],
        ],
    }
    prefix = "uv run --no-sync python "
    launches = [
        shlex.split(line.split(prefix, 1)[1].strip().removesuffix("\\").strip())
        for line in _gate(gate).splitlines()
        if prefix in line and module in line
    ]
    assert len(launches) == occurrences
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "NHMS_DISPLAY_READONLY_DATABASE_URL": "postgresql://nhms_display_ro@127.0.0.1:1/nhms",
    }
    for launch in launches:
        completed = subprocess.run(
            [sys.executable, *launch, *inputs[module], "--original-sha256", frozen, "--reviewed-sha", SHA],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert completed.returncode == 1, completed.stderr
        assert "CENSUS_KEYS_INVALID" in completed.stderr
        assert "Traceback" not in completed.stderr and "ModuleNotFoundError" not in completed.stderr
        assert completed.stdout == "" and not output.exists()
