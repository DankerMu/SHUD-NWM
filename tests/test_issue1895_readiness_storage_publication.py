"""Systemd-facts / group-reconcile / publication discriminators. Helpers imported from the storage core suite."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from packages.common.node27_issue1895_publication import prove_gfs_ifs_products
from packages.common.node27_issue1895_receipt import durable_key
from packages.common.node27_issue1895_types import Issue1895ReadinessError
from packages.common.node27_issue1895_watermark import (
    assert_independent_receipt_horizon,
    assert_systemd_invocation_facts,
    observe_current_cutoff,
    parse_systemctl_show,
)
from scripts import node27_issue1895_group_reconcile as group_reconcile_cli
from scripts import node27_issue1895_systemd_facts as systemd_facts_cli
from tests.test_issue1895_readiness_c14 import _group
from tests.test_issue1895_readiness_storage import KEYS, SHA, _durable, _substitute_identity, _write_private_json
from tests.test_issue1895_runbook_contract import _gate, _gate_bash, _gate_lines


def _canonical_systemd_facts() -> tuple[dict[str, str], dict[str, str]]:
    timer = parse_systemctl_show(
        "\n".join(
            (
                "Id=nhms-node27-timeseries-compression.timer",
                "Unit=nhms-node27-timeseries-compression.service",
                "FragmentPath=/home/nwm/NWM/infra/systemd/nhms-node27-timeseries-compression.timer",
                "",
            )
        )
    )
    service = parse_systemctl_show(
        "\n".join(
            (
                "Id=nhms-node27-timeseries-compression.service",
                "FragmentPath=/home/nwm/NWM/infra/systemd/nhms-node27-timeseries-compression.service",
                "ExecStart={ path=/home/nwm/NWM/scripts/node27_timeseries_compression_once.sh ; "
                "argv[]=/home/nwm/NWM/scripts/node27_timeseries_compression_once.sh --enforce }",
                "ExecStart={ path=/home/nwm/NWM/scripts/node27_cold_residency_once.sh ; "
                "argv[]=/home/nwm/NWM/scripts/node27_cold_residency_once.sh --enforce }",
                "InvocationID=abc123",
                "ExecMainStartTimestamp=Fri 2026-09-04 04:25:00 UTC",
                "ExecMainExitTimestamp=Fri 2026-09-04 04:26:00 UTC",
                "Result=success",
                "",
            )
        )
    )
    return timer, service


@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    (
        (lambda timer, _service: timer.__setitem__("Id", "other.timer"), "SYSTEMD_TIMER_ID"),
        (lambda _timer, service: service.__setitem__("Id", "other.service"), "SYSTEMD_SERVICE_ID"),
        (lambda _timer, service: service.pop("InvocationID"), "SYSTEMD_FACT_MISSING"),
        (
            lambda _timer, service: service.__setitem__(
                "ExecStart", "\n".join(reversed(str(service["ExecStart"]).splitlines()))
            ),
            "SYSTEMD_EXEC_ORDER",
        ),
        (lambda _timer, service: service.__setitem__("Result", "failed"), "SYSTEMD_RESULT"),
    ),
)
def test_systemd_facts_refuse_minimal_canonical_negative_matrix(
    mutate,
    expected_code: str,
) -> None:
    timer, service = _canonical_systemd_facts()
    mutate(timer, service)

    with pytest.raises(Issue1895ReadinessError) as raised:
        assert_systemd_invocation_facts(timer=timer, service=service)

    assert raised.value.code == expected_code


def test_systemd_facts_publication_is_exclusive_private_and_preserves_existing_bytes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private = tmp_path / "private"
    private.mkdir()
    os.chmod(private, 0o700)
    timer_show = private / "timer-show.txt"
    service_show = private / "service-show.txt"
    output = private / "systemd-facts.json"
    target = private / "symlink-target.json"
    timer_show.write_text(
        "\n".join(
            (
                "Id=nhms-node27-timeseries-compression.timer",
                "Unit=nhms-node27-timeseries-compression.service",
                "FragmentPath=/home/nwm/NWM/infra/systemd/nhms-node27-timeseries-compression.timer",
                "",
            )
        ),
        encoding="utf-8",
    )
    service_show.write_text(
        "\n".join(
            (
                "Id=nhms-node27-timeseries-compression.service",
                "FragmentPath=/home/nwm/NWM/infra/systemd/nhms-node27-timeseries-compression.service",
                "ExecStart={ path=/home/nwm/NWM/scripts/node27_timeseries_compression_once.sh ; "
                "argv[]=/home/nwm/NWM/scripts/node27_timeseries_compression_once.sh --enforce }",
                "ExecStart={ path=/home/nwm/NWM/scripts/node27_cold_residency_once.sh ; "
                "argv[]=/home/nwm/NWM/scripts/node27_cold_residency_once.sh --enforce }",
                "InvocationID=abc123",
                "ExecMainStartTimestamp=Fri 2026-09-04 04:25:00 UTC",
                "ExecMainExitTimestamp=Fri 2026-09-04 04:26:00 UTC",
                "Result=success",
                "",
            )
        ),
        encoding="utf-8",
    )
    os.chmod(timer_show, 0o600)
    os.chmod(service_show, 0o600)
    argv = ["--timer-show", str(timer_show), "--service-show", str(service_show), "--output", str(output)]

    assert systemd_facts_cli.main(argv) == 0
    assert output.stat().st_mode & 0o777 == 0o600
    first = output.read_bytes()
    assert systemd_facts_cli.main(argv) == 1
    assert capsys.readouterr().err.strip() == "SYSTEMD_FACTS_REFUSED"
    assert output.read_bytes() == first

    output.unlink()
    target.write_bytes(b"old target bytes\n")
    output.symlink_to(target)
    assert systemd_facts_cli.main(argv) == 1
    assert capsys.readouterr().err.strip() == "SYSTEMD_FACTS_REFUSED"
    assert output.is_symlink()
    assert target.read_bytes() == b"old target bytes\n"


def test_systemd_facts_closes_unreadable_inputs_without_path_or_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret_path = tmp_path / "synthetic-secret-timer-show.txt"
    rc = systemd_facts_cli.main(
        [
            "--timer-show", str(secret_path),
            "--service-show", str(tmp_path / "missing-service-show.txt"),
            "--output", str(tmp_path / "systemd-facts.json"),
        ]
    )
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.err.strip() == "SYSTEMD_FACTS_UNAVAILABLE"
    assert str(secret_path) not in captured.err
    assert "Traceback" not in captured.err


def _write_systemd_show(path: Path, *, service: bool) -> Path:
    if service:
        text = "\n".join(
            (
                "Id=nhms-node27-timeseries-compression.service",
                "FragmentPath=/home/nwm/NWM/infra/systemd/nhms-node27-timeseries-compression.service",
                "ExecStart={ path=/home/nwm/NWM/scripts/node27_timeseries_compression_once.sh ; "
                "argv[]=/home/nwm/NWM/scripts/node27_timeseries_compression_once.sh --enforce }",
                "ExecStart={ path=/home/nwm/NWM/scripts/node27_cold_residency_once.sh ; "
                "argv[]=/home/nwm/NWM/scripts/node27_cold_residency_once.sh --enforce }",
                "InvocationID=abc123",
                "ExecMainStartTimestamp=Fri 2026-09-04 04:25:00 UTC",
                "ExecMainExitTimestamp=Fri 2026-09-04 04:26:00 UTC",
                "Result=success",
                "",
            )
        )
    else:
        text = "\n".join(
            (
                "Id=nhms-node27-timeseries-compression.timer",
                "Unit=nhms-node27-timeseries-compression.service",
                "FragmentPath=/home/nwm/NWM/infra/systemd/nhms-node27-timeseries-compression.timer",
                "",
            )
        )
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def test_systemd_facts_refuses_symlink_and_parent_0755_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    private = tmp_path / "private"
    private.mkdir()
    os.chmod(private, 0o700)
    timer_show = _write_systemd_show(private / "timer-show.txt", service=False)
    service_show = _write_systemd_show(private / "service-show.txt", service=True)
    output = private / "systemd-facts.json"
    called = {"n": 0}

    def refuse_assert(*_args: object, **_kwargs: object) -> dict[str, str]:
        called["n"] += 1
        raise AssertionError("assert_systemd_invocation_facts must not run on unsafe inputs")

    monkeypatch.setattr(systemd_facts_cli, "assert_systemd_invocation_facts", refuse_assert)
    linked = tmp_path / "timer-link.txt"
    linked.symlink_to(timer_show)
    rc = systemd_facts_cli.main(
        ["--timer-show", str(linked), "--service-show", str(service_show), "--output", str(output)]
    )
    assert rc == 1
    assert called["n"] == 0
    assert not output.exists()
    captured = capsys.readouterr()
    assert "SYSTEMD_FACTS_UNAVAILABLE" in captured.err or "READINESS_INPUT" in captured.err
    os.chmod(private, 0o755)
    rc = systemd_facts_cli.main(
        ["--timer-show", str(timer_show), "--service-show", str(service_show), "--output", str(output)]
    )
    assert rc == 1
    assert called["n"] == 0
    assert not output.exists()
    os.chmod(private, 0o700)
    monkeypatch.setattr(systemd_facts_cli, "assert_systemd_invocation_facts", assert_systemd_invocation_facts)
    assert systemd_facts_cli.main(
        ["--timer-show", str(timer_show), "--service-show", str(service_show), "--output", str(output)]
    ) == 0
    assert output.exists()


def test_independent_w8_and_systemd_facts_refuse_self_bind() -> None:
    pre_tick_watermark = datetime(2026, 9, 4, 12, tzinfo=UTC)
    post_tick_watermark = datetime(2026, 9, 5, 12, tzinfo=UTC)

    def connect(_dsn: str, **_kwargs: object) -> object:
        raise AssertionError("must not connect when watermark is injected")

    pre_tick_horizon = observe_current_cutoff(
        dsn="postgresql://nhms_display_ro@127.0.0.1/nhms",
        lag_seconds="172800",
        connect=connect,
        watermark=pre_tick_watermark,
    )
    post_tick_horizon = observe_current_cutoff(
        dsn="postgresql://nhms_display_ro@127.0.0.1/nhms",
        lag_seconds="172800",
        connect=connect,
        watermark=post_tick_watermark,
    )
    assert pre_tick_horizon["watermark"] == "2026-09-04T12:00:00Z"
    assert post_tick_horizon["watermark"] == "2026-09-05T12:00:00Z"
    assert post_tick_horizon["cutoff"] == (post_tick_watermark - timedelta(seconds=172800)).isoformat().replace(
        "+00:00", "Z"
    )
    assert post_tick_horizon["lag_seconds"] == 172800
    tick_receipt = {
        "watermark": post_tick_horizon["watermark"],
        "cutoff": post_tick_horizon["cutoff"],
        "lag_seconds": 172800,
        "per_tick_bound": 1,
    }
    assert_independent_receipt_horizon(
        tick_receipt,
        expected_watermark=post_tick_horizon["watermark"],
        expected_cutoff=post_tick_horizon["cutoff"],
        expected_lag_seconds=172800,
    )
    from packages.common.node27_issue1895_timer import assert_natural_receipt_identity

    assert_natural_receipt_identity(
        {
            **tick_receipt,
            "schema_version": "1.1",
            "head_sha": SHA,
            "outcome": "no_op",
        },
        reviewed_sha=SHA,
        expected_watermark=post_tick_horizon["watermark"],
        expected_cutoff=post_tick_horizon["cutoff"],
        invoked_unit="nhms-node27-timeseries-compression.service",
    )
    with pytest.raises(Issue1895ReadinessError) as mismatch:
        assert_independent_receipt_horizon(
            tick_receipt,
            expected_watermark=pre_tick_horizon["watermark"],
            expected_cutoff=pre_tick_horizon["cutoff"],
            expected_lag_seconds=172800,
        )
    assert mismatch.value.code == "TICK_WATERMARK_MISMATCH"
    timer = parse_systemctl_show(
        "\n".join(
            [
                "Id=nhms-node27-timeseries-compression.timer",
                "Unit=nhms-node27-timeseries-compression.service",
                "FragmentPath=/home/nwm/NWM/infra/systemd/nhms-node27-timeseries-compression.timer",
                "",
            ]
        )
    )
    service = parse_systemctl_show(
        "\n".join(
            [
                "Id=nhms-node27-timeseries-compression.service",
                "FragmentPath=/home/nwm/NWM/infra/systemd/nhms-node27-timeseries-compression.service",
                "ExecStart={ path=/home/nwm/NWM/scripts/node27_timeseries_compression_once.sh ; "
                "argv[]=/home/nwm/NWM/scripts/node27_timeseries_compression_once.sh --enforce }",
                "ExecStart={ path=/home/nwm/NWM/scripts/node27_cold_residency_once.sh ; "
                "argv[]=/home/nwm/NWM/scripts/node27_cold_residency_once.sh --enforce }",
                "InvocationID=abc123",
                "ExecMainStartTimestamp=Fri 2026-09-04 04:25:00 UTC",
                "ExecMainExitTimestamp=Fri 2026-09-04 04:26:00 UTC",
                "Result=success",
                "",
            ]
        )
    )
    proven = assert_systemd_invocation_facts(timer=timer, service=service)
    assert proven["invocation_id"] == "abc123"
    g8 = " ".join(_gate_lines("G8"))
    assert 'test ! -e "$W8_PATH"' in g8
    assert "scripts/node27_issue1895_watermark.py" in g8
    assert "scripts/node27_issue1895_systemd_facts.py" in g8
    assert "post-tick external independent horizon" in _gate("G8")
    assert "expected_cutoff=receipt[\"cutoff\"]" not in g8
    assert "expected_watermark=receipt[\"watermark\"]" not in g8


def _group_reconcile_documents(*, receipt: dict) -> tuple[dict, dict, dict]:
    groups = [_group(KEYS[index - 1], index) for index in range(1, 7)]
    return {"groups": groups}, {"groups": groups}, receipt


def _write_group_reconcile_documents(tmp_path: Path, *, receipt: dict) -> tuple[Path, Path, Path]:
    baseline, observed, receipt_document = _group_reconcile_documents(receipt=receipt)
    private = tmp_path / "private"
    private.mkdir(parents=True, exist_ok=True)
    private.chmod(0o700)
    baseline_path = _write_private_json(private / "baseline.json", baseline)
    observed_path = _write_private_json(private / "observed.json", observed)
    receipt_path = _write_private_json(private / "receipt.json", receipt_document)
    return baseline_path, observed_path, receipt_path


def _group_reconcile_argv(
    baseline_path: Path,
    observed_path: Path,
    receipt_path: Path,
    *,
    remaining: str = "",
    newly: str = "",
) -> list[str]:
    return [
        "--baseline",
        str(baseline_path),
        "--observed",
        str(observed_path),
        "--receipt",
        str(receipt_path),
        "--reviewed-sha",
        SHA,
        "--expected-cutoff",
        "2026-09-04T00:00:00Z",
        "--expected-watermark",
        "2026-09-06T00:00:00Z",
        "--remaining-all-source-keys",
        remaining,
        "--newly-terminal-key",
        newly,
    ]


def _natural_group_receipt(*, outcome: str, selected: list[dict], deferred: list[dict]) -> dict:
    return {
        "schema_version": "1.1",
        "head_sha": SHA,
        "cutoff": "2026-09-04T00:00:00Z",
        "watermark": "2026-09-06T00:00:00Z",
        "per_tick_bound": 1,
        "outcome": outcome,
        "selected": selected,
        "deferred": deferred,
    }


def test_group_reconcile_cli_accepts_truthful_noop_with_empty_cli_sets(tmp_path: Path) -> None:
    baseline_path, observed_path, receipt_path = _write_group_reconcile_documents(
        tmp_path,
        receipt=_natural_group_receipt(outcome="no_op", selected=[], deferred=[]),
    )

    assert group_reconcile_cli.main(_group_reconcile_argv(baseline_path, observed_path, receipt_path)) == 0


@pytest.mark.parametrize("kind", ("symlink", "mode", "hardlink"))
@pytest.mark.parametrize("which", ("baseline", "observed", "receipt"))
def test_g8_group_reconcile_cli_refuses_unsafe_baseline_observed_or_receipt_identity(
    tmp_path: Path, kind: str, which: str
) -> None:
    baseline_path, observed_path, receipt_path = _write_group_reconcile_documents(
        tmp_path,
        receipt=_natural_group_receipt(outcome="no_op", selected=[], deferred=[]),
    )
    target = {"baseline": baseline_path, "observed": observed_path, "receipt": receipt_path}[which]
    _substitute_identity(target, kind)
    assert group_reconcile_cli.main(_group_reconcile_argv(baseline_path, observed_path, receipt_path)) == 1


def test_g8_group_reconcile_cli_refuses_parent_mode_0755(tmp_path: Path) -> None:
    baseline_path, observed_path, receipt_path = _write_group_reconcile_documents(
        tmp_path,
        receipt=_natural_group_receipt(outcome="no_op", selected=[], deferred=[]),
    )
    os.chmod(baseline_path.parent, 0o755)
    assert group_reconcile_cli.main(_group_reconcile_argv(baseline_path, observed_path, receipt_path)) == 1


def test_g8_owner_inputs_use_held_reader_before_parameter_derivation() -> None:
    fences = [body for _opening, body in _gate_bash("G8")]
    natural = next(
        body
        for body in fences
        if "NATURAL_RECEIPT" in body and "assert_natural_receipt_identity" in body and "W8_PATH" in body
    )
    assert "json.load(open" not in natural
    assert "read_held_private_json" in natural
    assert natural.index("read_held_private_json") < natural.index("assert_natural_receipt_identity")

    derive = next(body for body in fences if "newly_terminal_keys" in body and "group_reconcile.py" in body)
    assert "json.load(open" not in derive
    assert "read_held_private_json" in derive
    newly_cmd = derive[derive.index("NEWLY=") : derive.index("REMAINING=")]
    remaining_cmd = derive[derive.index("REMAINING=") : derive.index("group_reconcile.py")]
    assert "read_held_private_json" in newly_cmd
    assert "read_held_private_json" in remaining_cmd
    cutoff_cmd = derive[derive.index("--expected-cutoff") : derive.index("--expected-watermark")]
    watermark_cmd = derive[derive.index("--expected-watermark") : derive.index("--invoked-unit")]
    assert "read_held_private_json" in cutoff_cmd
    assert "read_held_private_json" in watermark_cmd
    assert derive.index("NEWLY=") < derive.index("group_reconcile.py")
    assert derive.index("REMAINING=") < derive.index("group_reconcile.py")
    assert "while IFS= read -r GROUP" not in derive


def test_group_reconcile_cli_accepts_migrated_newline_sets_and_deferred_suffix(tmp_path: Path) -> None:
    migrated = _durable(7)
    deferred = _durable(8)
    migrated_key = durable_key(migrated)
    deferred_key = durable_key(deferred)
    baseline_path, observed_path, receipt_path = _write_group_reconcile_documents(
        tmp_path,
        receipt=_natural_group_receipt(
            outcome="clean",
            selected=[{"outcome": "migrated", "durable": migrated}],
            deferred=[{"reason": "per_tick_bound", "durable": deferred}],
        ),
    )

    assert group_reconcile_cli.main(
        _group_reconcile_argv(
            baseline_path,
            observed_path,
            receipt_path,
            remaining=f"\n{deferred_key}\n",
            newly=f"\n{migrated_key}\n",
        )
    ) == 0


def test_group_reconcile_cli_redacts_domain_failures_without_traceback_or_raw_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    baseline_path, observed_path, receipt_path = _write_group_reconcile_documents(
        tmp_path,
        receipt=_natural_group_receipt(outcome="no_op", selected=[], deferred=[]),
    )
    secret = "postgresql://nhms_display_ro:synthetic-secret@127.0.0.1/nhms"
    raw_path = str(receipt_path)

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise Issue1895ReadinessError(
            f"reconcile failed for {raw_path} with {secret}",
            code="TICK_SELECTION_INVALID",
            stage="timer",
        )

    monkeypatch.setattr(group_reconcile_cli, "assert_natural_tick_selection", refuse)
    assert group_reconcile_cli.main(_group_reconcile_argv(baseline_path, observed_path, receipt_path)) == 1
    captured = capsys.readouterr()
    assert "TICK_SELECTION_INVALID" in captured.err
    assert secret not in captured.err
    assert raw_path not in captured.err
    assert "Traceback" not in captured.err


def test_publication_compares_registry_sets_and_utc_instants() -> None:
    expected = {("m1", "b1")}
    gfs = {
        "source_id": "GFS",
        "run_id": "r1",
        "cycle_time": "2026-09-04T00:00:00+00:00",
        "model_id": "m1",
        "basin_id": "b1",
        "run_status": "published",
    }
    ifs = {
        "source_id": "IFS",
        "run_id": "r2",
        "cycle_time": "2026-09-04T12:00:00Z",
        "model_id": "m1",
        "basin_id": "b1",
        "run_status": "published",
    }
    rows = [
        {
            "source_id": "gfs",
            "cycle_time": datetime(2026, 9, 4, tzinfo=UTC),
            "run_status": "published",
            "model_id": "m1",
            "basin_id": "b1",
        }
    ]
    ifs_rows = [
        {
            "source_id": "IFS",
            "cycle_time": "2026-09-04T12:00:00+00:00",
            "run_status": "published",
            "model_id": "m1",
            "basin_id": "b1",
        }
    ]
    proved = prove_gfs_ifs_products(
        gfs=gfs,
        ifs=ifs,
        gfs_rows=rows,
        ifs_rows=ifs_rows,
        expected_gfs=expected,
        expected_ifs=expected,
        current_valid_times=["2026-09-04T18:00:00+00:00"],
        baseline_valid_times=["2026-09-04T12:00:00Z"],
    )
    assert proved["gfs_count"] == 1
    short = prove_gfs_ifs_products
    with pytest.raises(Issue1895ReadinessError) as mismatch:
        short(
            gfs=gfs,
            ifs=ifs,
            gfs_rows=rows,
            ifs_rows=ifs_rows,
            expected_gfs={("m1", "b1"), ("m2", "b1")},
            expected_ifs=expected,
            current_valid_times=["2026-09-04T18:00:00Z"],
            baseline_valid_times=["2026-09-04T12:00:00Z"],
        )
    assert mismatch.value.code == "PUBLICATION_IDENTITY_MISMATCH"
    g7 = " ".join(_gate_lines("G7"))
    assert "scripts/node27_issue1895_publication_current.py" in g7
    assert "scripts/node27_issue1895_publication_prove.py" not in g7
    assert "gfs_expected_count=sum" not in g7
