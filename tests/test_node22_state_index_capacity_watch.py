"""#2653: node-22 state-index capacity watch (``prune-retention`` dry-run only).

Seams: the wrapper's ``main`` (its exit code, stdout verdict and receipt files)
and, for the end-to-end cases, the real ``repair_state_index`` over real
temporary reference/destination indexes built with the #2548 retention suite's
``Lanes`` fixture. Only the repair call is faked, and only where a summary shape
(capacity warning, missing lane) or an error class cannot be produced cheaply
from a real index. Expected values come from the design's alert/exit table and
from the fixture's construction (81/71 entries; K1-K5 remove ``entries[1:58]``
from reference and ``entries[11:58]`` from destination), never from the wrapper.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from packages.common import safe_fs
from packages.common.object_store import sha256_bytes
from scripts import node22_state_index_capacity_watch as watch
from scripts import scheduler_state_index_repair as repair
from tests.test_state_index_retention import Lanes, _fs_fingerprint, _prunable

REPO_ROOT = Path(__file__).resolve().parents[1]
PREIMAGE = "provider_preimage_changed"


@pytest.fixture(name="lanes")
def lanes_fixture(tmp_path: Path) -> Lanes:
    return Lanes(tmp_path / "lanes")


@pytest.fixture(name="watch_root")
def watch_root_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "capacity-watch"
    root.mkdir()
    os.chmod(root, 0o700)
    monkeypatch.setenv(watch.RECEIPT_ROOT_ENV, str(root))
    monkeypatch.setattr(watch.time, "sleep", lambda _seconds: None)
    return root


def _capacity(*, warning: bool, entries: int = 10) -> dict[str, Any]:
    return {"entry_count": entries, "utilization_ratio": 0.75 if warning else 0.1, "warning": warning}


def _lane(
    name: str,
    *,
    checksum_valid: Any = True,
    entries: int = 10,
    warning_before: bool = False,
    warning_after: bool = False,
) -> dict[str, Any]:
    return {
        "root": f"/roots/{name}",
        "index": f"/roots/{name}/scheduler/state-index/index-last.json",
        "checksum_valid": checksum_valid,
        "action": "skip",
        "untouched_reason": "nothing_to_prune",
        "retention": {
            "entry_count_before": entries,
            "removed_count": 0,
            "removed_state_ids": [],
            "removed_state_ids_sha256": "sha256:" + "0" * 64,
            "retention_days": 21,
            "cycle_lag_hours": 16,
            "capacity_before": _capacity(warning=warning_before, entries=entries),
            "capacity_after": _capacity(warning=warning_after, entries=entries),
            "groups": [{"model_id": "m", "removed_count": 0}],
        },
    }


def _summary(**lanes: dict[str, Any] | None) -> dict[str, Any]:
    base: dict[str, Any] = {"reference": _lane("reference"), "destination": _lane("destination")}
    base.update(lanes)
    return {"mode": "dry_run", "lanes": base}


def _fake_repair(monkeypatch: pytest.MonkeyPatch, *outcomes: Any) -> list[dict[str, Any]]:
    """Replace the repair call; each outcome is returned or, if an exception, raised."""
    calls: list[dict[str, Any]] = []
    queue = list(outcomes)

    def fake(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        outcome = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(repair, "repair_state_index", fake)
    return calls


def _run(capsys: pytest.CaptureFixture[str], argv: list[str] | None = None) -> tuple[int, dict[str, Any], str]:
    code = watch.main([] if argv is None else argv)
    captured = capsys.readouterr()
    return code, json.loads(captured.out.strip().splitlines()[-1]), captured.err


def _latest(root: Path) -> dict[str, Any]:
    return json.loads((root / "latest.json").read_text(encoding="utf-8"))


def _alerts(receipt: dict[str, Any]) -> list[tuple[str, str]]:
    return [(alert["lane"], alert["alert"]) for alert in receipt["alerts"]]


# --- 1.2 verdicts -----------------------------------------------------------


def test_healthy_real_dry_run_exits_0_with_receipt_and_leaves_indexes_byte_identical(
    lanes: Lanes,
    watch_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entries = _prunable(lanes)
    lanes.publish(entries, destination=entries[10:])
    lanes.apply_env(monkeypatch)  # also sets the repair archive and repair receipt roots
    reference_before = lanes.reference_index.read_bytes()
    destination_before = lanes.destination_index.read_bytes()
    lanes_before = _fs_fingerprint(lanes.root)

    code, verdict, _ = _run(capsys)

    assert code == 0
    assert verdict["status"] == "healthy" and verdict["exit_code"] == 0 and verdict["alerts"] == []
    assert verdict["attempts"] == 1
    assert _latest(watch_root) == verdict
    assert lanes.reference_index.read_bytes() == reference_before
    assert lanes.destination_index.read_bytes() == destination_before
    # Dry-run only: nothing appears under the repair archive or repair receipt roots.
    assert _fs_fingerprint(lanes.root) == lanes_before
    assert list(lanes.archive_root.iterdir()) == [] and list(lanes.receipt_root.iterdir()) == []
    expected = {
        "reference": (lanes.reference_root, 81, entries[1:58]),
        "destination": (lanes.destination_root, 71, entries[11:58]),
    }
    for name, (root, count, removed) in expected.items():
        lane = verdict["lanes"][name]
        ids = sorted(entry["state_id"] for entry in removed)
        digest = "sha256:" + sha256_bytes(json.dumps(ids, separators=(",", ":")).encode("utf-8"))
        assert lane["root"] == str(root.resolve())
        assert lane["checksum_valid"] is True
        assert lane["action"] == "prune-retention" and lane["untouched_reason"] is None
        assert lane["entry_count_before"] == count
        assert lane["removed_count"] == len(removed)
        assert lane["removed_state_ids_sha256"] == digest
        assert lane["retention_days"] == 21 and lane["cycle_lag_hours"] == 16
        assert lane["capacity_before"]["entry_count"] == count and lane["capacity_before"]["warning"] is False
        assert lane["capacity_after"]["entry_count"] == 24 and lane["capacity_after"]["warning"] is False
    stamped = [path for path in watch_root.iterdir() if path.name != "latest.json"]
    assert len(stamped) == 1 and json.loads(stamped[0].read_text(encoding="utf-8")) == verdict
    for path in watch_root.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(watch_root.stat().st_mode) == 0o700


@pytest.mark.parametrize("lane_name", ["reference", "destination"])
def test_capacity_warning_on_either_lane_exits_1_with_receipt(
    lane_name: str,
    watch_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _fake_repair(monkeypatch, _summary(**{lane_name: _lane(lane_name, warning_before=True)}))

    code, verdict, _ = _run(capsys)

    assert code == 1
    assert verdict["status"] == "alert" and verdict["exit_code"] == 1
    assert _alerts(verdict) == [(lane_name, "capacity_warning")]
    assert _latest(watch_root) == verdict


def test_unprunable_warning_is_reported_distinctly(
    watch_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    summary = _summary(destination=_lane("destination", warning_before=True, warning_after=True))
    _fake_repair(monkeypatch, summary)

    code, verdict, _ = _run(capsys)

    assert code == 1
    assert _alerts(verdict) == [
        ("destination", "capacity_warning"),
        ("destination", "capacity_warning_unprunable"),
    ]
    assert _latest(watch_root)["alerts"] == verdict["alerts"]


def test_real_invalid_checksum_exits_1(
    lanes: Lanes,
    watch_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entries = _prunable(lanes)
    lanes.publish(entries)
    payload = json.loads(lanes.destination_index.read_text(encoding="utf-8"))
    payload["checksum"] = "sha256:" + "0" * 64  # an out-of-band edit
    lanes.destination_index.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    lanes.apply_env(monkeypatch)
    before = lanes.destination_index.read_bytes()

    code, verdict, _ = _run(capsys)

    assert code == 1
    assert _alerts(verdict) == [("destination", "checksum_invalid")]
    assert verdict["lanes"]["destination"]["checksum_valid"] is False
    assert lanes.destination_index.read_bytes() == before


@pytest.mark.parametrize("checksum_valid", [False, None])
def test_checksum_not_true_alerts(
    checksum_valid: Any, watch_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_repair(monkeypatch, _summary(reference=_lane("reference", checksum_valid=checksum_valid)))

    code, verdict, _ = _run(capsys)

    assert code == 1 and _alerts(verdict) == [("reference", "checksum_invalid")]


def test_real_empty_index_exits_1_lane_empty(
    lanes: Lanes,
    watch_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entries = _prunable(lanes)
    lanes.publish(entries, destination=[])
    lanes.apply_env(monkeypatch)

    code, verdict, _ = _run(capsys)

    assert code == 1
    assert _alerts(verdict) == [("destination", "lane_empty")]
    assert verdict["lanes"]["destination"]["entry_count_before"] == 0


@pytest.mark.parametrize("missing", [None, {}, {"checksum_valid": True}])
def test_missing_lane_or_retention_block_exits_1(
    missing: Any, watch_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_repair(monkeypatch, _summary(reference=missing))

    code, verdict, _ = _run(capsys)

    assert code == 1
    assert _alerts(verdict) == [("reference", "lane_summary_missing")]
    assert verdict["lanes"]["reference"] is None


def test_real_unset_cycle_lag_refuses_exit_2_without_touching_indexes(
    lanes: Lanes,
    watch_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entries = _prunable(lanes)
    lanes.publish(entries)
    lanes.apply_env(monkeypatch)
    monkeypatch.delenv(repair.CYCLE_LAG_HOURS_ENV)
    before = _fs_fingerprint(lanes.root)

    code, verdict, _ = _run(capsys)

    assert code == 2
    assert verdict["status"] == "refused" and verdict["exit_code"] == 2
    assert verdict["error"]["reason"] == "repair_cycle_lag_unset"
    assert verdict["error"]["field"] == "cycle_lag_hours"
    assert _fs_fingerprint(lanes.root) == before
    assert _latest(watch_root) == verdict


def test_repair_incomplete_maps_to_exit_3(
    watch_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    error = repair.RepairIncompleteError(PREIMAGE, {"field": "index"}, summary={})
    calls = _fake_repair(monkeypatch, error)

    code, verdict, _ = _run(capsys)

    assert code == 3 and verdict["status"] == "refused" and verdict["exit_code"] == 3
    assert verdict["error"] == {"reason": PREIMAGE, "field": "index"}
    assert len(calls) == 1  # an incomplete result is never retried, whatever its reason



def test_preimage_race_once_then_success_exits_0_with_two_attempts(
    watch_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(watch.time, "sleep", sleeps.append)
    calls = _fake_repair(monkeypatch, repair.RepairCliError(PREIMAGE, {"field": "destination"}), _summary())

    code, verdict, _ = _run(capsys)

    assert code == 0 and verdict["status"] == "healthy" and verdict["attempts"] == 2
    assert len(calls) == 2 and sleeps == [5]
    assert _latest(watch_root)["attempts"] == 2


def test_persistent_preimage_race_exits_2_after_three_attempts(
    watch_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _fake_repair(monkeypatch, repair.RepairCliError(PREIMAGE, {"field": "destination"}))

    code, verdict, _ = _run(capsys)

    assert code == 2 and verdict["attempts"] == 3 and len(calls) == 3
    assert verdict["error"]["reason"] == PREIMAGE
    assert _latest(watch_root)["attempts"] == 3


def test_other_refusals_are_not_retried(
    watch_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _fake_repair(monkeypatch, repair.RepairCliError("root_unavailable", {"field": "destination_root"}))

    code, verdict, _ = _run(capsys)

    assert code == 2 and len(calls) == 1 and verdict["attempts"] == 1
    assert verdict["error"] == {"reason": "root_unavailable", "field": "destination_root"}


@pytest.mark.parametrize("case", ["unset", "relative", "missing", "symlink", "group", "other", "not_owned"])
def test_unsafe_receipt_root_refuses_exit_2(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _fake_repair(monkeypatch, _summary())
    monkeypatch.delenv(watch.RECEIPT_ROOT_ENV, raising=False)
    root = tmp_path / "watch"
    root.mkdir()
    os.chmod(root, 0o700)
    expected = {
        "unset": "receipt_root_unset",
        "relative": "receipt_root_invalid",
        "missing": "receipt_root_missing",
        "symlink": "receipt_root_invalid",
        "group": "receipt_root_not_private",
        "other": "receipt_root_not_private",
        "not_owned": "receipt_root_not_private",
    }[case]
    if case == "relative":
        monkeypatch.setenv(watch.RECEIPT_ROOT_ENV, "relative/watch")
    elif case == "missing":
        monkeypatch.setenv(watch.RECEIPT_ROOT_ENV, str(tmp_path / "absent"))
    elif case == "symlink":
        (tmp_path / "link").symlink_to(root)
        monkeypatch.setenv(watch.RECEIPT_ROOT_ENV, str(tmp_path / "link"))
    elif case in ("group", "other"):
        os.chmod(root, 0o750 if case == "group" else 0o705)
        monkeypatch.setenv(watch.RECEIPT_ROOT_ENV, str(root))
    elif case == "not_owned":
        real_uid = os.geteuid()
        monkeypatch.setenv(watch.RECEIPT_ROOT_ENV, str(root))
        monkeypatch.setattr(repair.os, "geteuid", lambda: real_uid + 1)

    code, verdict, _ = _run(capsys)

    assert code == 2 and verdict["status"] == "refused"
    assert verdict["error"] == {"reason": expected, "field": watch.RECEIPT_ROOT_ENV}
    assert calls == []  # the dry-run never starts without a safe receipt root
    assert list(root.iterdir()) == [] and not (tmp_path / "absent").exists()


def test_receipt_root_flag_overrides_env(
    tmp_path: Path, watch_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_repair(monkeypatch, _summary())
    flag_root = tmp_path / "flag-root"
    flag_root.mkdir()
    os.chmod(flag_root, 0o700)

    code, verdict, _ = _run(capsys, ["--receipt-root", str(flag_root)])

    assert code == 0 and _latest(flag_root) == verdict
    assert list(watch_root.iterdir()) == []


@pytest.mark.parametrize("fail_on", ["stamped", "latest"])
def test_receipt_write_failure_exits_2_and_keeps_verdict_on_stdout(
    fail_on: str, watch_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_repair(monkeypatch, _summary(reference=_lane("reference", warning_before=True)))
    real_write = safe_fs.atomic_write_bytes_no_follow

    def failing_write(path: Path, content: bytes, **kwargs: Any) -> Any:
        if (Path(path).name == "latest.json") == (fail_on == "latest"):
            raise OSError("disk full")
        return real_write(path, content, **kwargs)

    monkeypatch.setattr(safe_fs, "atomic_write_bytes_no_follow", failing_write)

    code, verdict, err = _run(capsys)

    assert code == 2
    assert verdict["status"] == "alert" and _alerts(verdict) == [("reference", "capacity_warning")]
    failure = json.loads(err.strip().splitlines()[-1])
    assert failure["error"] == {"reason": "receipt_write_failed", "error_type": "OSError"}


def test_unexpected_exception_exits_4_with_refused_receipt(
    watch_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (watch_root / "latest.json").write_text('{"status": "healthy"}\n', encoding="utf-8")
    os.chmod(watch_root / "latest.json", 0o600)
    _fake_repair(monkeypatch, KeyError("lanes"))

    code, verdict, _ = _run(capsys)

    assert code == 4
    assert verdict["status"] == "refused" and verdict["exit_code"] == 4
    assert verdict["error"] == {"reason": "unexpected_exception", "error_type": "KeyError"}
    assert _latest(watch_root) == verdict  # yesterday's latest.json never looks current


def test_import_failure_exits_4_not_alert(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # Break the import chain: the package attribute and the module cache both go.
    monkeypatch.delattr(sys.modules["scripts"], "scheduler_state_index_repair")
    monkeypatch.setitem(sys.modules, "scripts.scheduler_state_index_repair", None)

    code, verdict, _ = _run(capsys)

    assert code == 4 and verdict["status"] == "refused"
    assert verdict["error"]["error_type"] in {"ImportError", "ModuleNotFoundError"}


def test_module_help_runs_from_repo_root_through_the_import_chain() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "scripts.node22_state_index_capacity_watch", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--receipt-root" in result.stdout and "--enforce" not in result.stdout


# --- 1.3 dry-run-only guarantee and bounded receipt -------------------------


def test_repair_is_always_called_dry_run_with_no_selectors(
    watch_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _fake_repair(monkeypatch, repair.RepairCliError(PREIMAGE, {}), _summary())

    assert _run(capsys)[0] == 0

    assert len(calls) == 2
    for call in calls:
        assert call["operation"] == "prune-retention"
        assert call["enforce"] is False
        for key in ("lane", "state_id", "run_id", "model_id", "source_id", "valid_time"):
            assert call[key] is None, key
        assert call["allow_missing_reference"] is False and call["allow_missing_destination"] is False
        assert call["reference_root"] is None and call["destination_root"] is None
        assert call["object_store_prefix"] is None and call["cycle_lag_hours"] is None


def test_parser_has_no_enforce_flag(capsys: pytest.CaptureFixture[str]) -> None:
    options = {option for action in watch.build_parser()._actions for option in action.option_strings}
    assert options == {"-h", "--help", "--receipt-root"}
    with pytest.raises(SystemExit) as raised:
        watch.main(["--enforce"])
    assert raised.value.code == 2


def test_receipt_is_bounded_and_rotation_keeps_newest_30(
    lanes: Lanes,
    watch_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entries = _prunable(lanes)
    lanes.publish(entries, destination=entries[10:])
    lanes.apply_env(monkeypatch)
    old = [f"202601{day:02d}T000000Z.json" for day in range(1, 31)]
    for name in (*old, "notes.txt", "20260101T000000Z.json.bak"):
        (watch_root / name).write_text("{}\n", encoding="utf-8")

    code, verdict, _ = _run(capsys)

    assert code == 0
    for text in (json.dumps(verdict), (watch_root / "latest.json").read_text(encoding="utf-8")):
        assert '"removed_state_ids":' not in text
        assert '"groups":' not in text
        assert '"entries":' not in text
    names = sorted(path.name for path in watch_root.iterdir())
    stamped = [name for name in names if watch._RECEIPT_NAME.match(name)]
    assert len(stamped) == 30
    assert old[0] not in names and old[1] in names  # only the oldest one rotated out
    assert "notes.txt" in names and "20260101T000000Z.json.bak" in names and "latest.json" in names
