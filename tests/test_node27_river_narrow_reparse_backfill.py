"""DB-free contract of the #2382 narrow reparse backfill runner."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts import node27_river_narrow_reparse_backfill as backfill

_ENV = {
    "DATABASE_URL": "postgresql://example.invalid/nhms",
    "OBJECT_STORE_ROOT": "/srv/object-store",
    "OBJECT_STORE_PREFIX": "s3://nhms",
    "NODE27_TIMESERIES_RETENTION_WINDOW_DAYS": "21",
}


def test_settings_require_database_object_store_and_window() -> None:
    for name in ("DATABASE_URL", "OBJECT_STORE_ROOT", "NODE27_TIMESERIES_RETENTION_WINDOW_DAYS"):
        env = dict(_ENV, **{name: " "})
        with pytest.raises(backfill.RefusedError) as caught:
            backfill.settings_from_env(env)
        assert caught.value.code == "CONFIG_MISSING"
        assert "example.invalid" not in str(caught.value)
    for bad in ("0", "-3", "21d"):
        with pytest.raises(backfill.RefusedError) as caught:
            backfill.settings_from_env(dict(_ENV, NODE27_TIMESERIES_RETENTION_WINDOW_DAYS=bad))
        assert caught.value.code == "CONFIG_INVALID"
    settings = backfill.settings_from_env(dict(_ENV, OUTPUT_PARSER_MAX_FLOW_M3S="5", OUTPUT_PARSER_BATCH_SIZE="7"))
    assert (settings.window_days, settings.max_flow_m3s, settings.batch_size) == (21, 5.0, 7)


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (None, "missing"),
        ({"timeseries_store": "narrow", "in_window": True, "status": "published", "parsed_at": 1}, "already_narrow"),
        ({"timeseries_store": "legacy", "in_window": False, "status": "published", "parsed_at": 1}, "aged_out"),
        ({"timeseries_store": "legacy", "in_window": True, "status": "parsed", "parsed_at": 1}, "ineligible"),
        ({"timeseries_store": "legacy", "in_window": True, "status": "published", "parsed_at": None}, "ineligible"),
        ({"timeseries_store": "legacy", "in_window": True, "status": "published", "parsed_at": 1}, None),
        ({"timeseries_store": "legacy", "in_window": True, "status": "superseded", "parsed_at": 1}, None),
    ],
)
def test_skip_disposition_is_evaluated_under_the_row_lock(row: dict | None, expected: str | None) -> None:
    assert backfill._skip_disposition(row) == expected


def test_post_parse_verification_rejects_every_inconsistency() -> None:
    before = {"status": "published"}
    good = {"status": "published", "timeseries_store": "narrow", "narrow_rows": 12}
    assert backfill._post_parse_problem(before, good, 12) is None
    assert "no rows" in backfill._post_parse_problem(before, dict(good, narrow_rows=0), 0)
    assert "!=" in backfill._post_parse_problem(before, dict(good, narrow_rows=11), 12)
    assert "status changed" in backfill._post_parse_problem(before, dict(good, status="parsed"), 12)
    assert "route" in backfill._post_parse_problem(before, dict(good, timeseries_store="legacy"), 12)


def test_estimate_rows_is_segments_times_hours() -> None:
    start = datetime(2026, 9, 1, tzinfo=UTC)
    rows = [
        {"start_time": start, "end_time": start + timedelta(days=7), "segment_count": 10},
        {"start_time": start, "end_time": start + timedelta(hours=3), "segment_count": 4},
    ]
    assert backfill.estimate_rows(rows) == 10 * 168 + 12


def test_dispatch_stops_on_signal_deadline_and_failure_budget() -> None:
    future = datetime.now(UTC) + timedelta(hours=1)
    past = datetime.now(UTC) - timedelta(seconds=1)
    assert backfill._may_dispatch({"reason": None}, future, Counter(failed=2), 3)
    assert not backfill._may_dispatch({"reason": "signal_15"}, future, Counter(), 3)
    assert not backfill._may_dispatch({"reason": None}, past, Counter(), 3)
    assert not backfill._may_dispatch({"reason": None}, None, Counter(failed=3), 3)
    assert backfill._stop_reason(None, Counter(), 3, []) == "exhausted"
    assert backfill._stop_reason(past, Counter(failed=3), 3, ["r"]) == "failure_budget"
    assert backfill._stop_reason(past, Counter(), 3, ["r"]) == "deadline"


def test_run_without_go_token_is_refused_before_any_connection(tmp_path: Path, monkeypatch) -> None:
    def _no_connect(_url: str) -> None:
        raise AssertionError("must not connect")

    monkeypatch.setattr(backfill, "connect", _no_connect)
    monkeypatch.setattr(backfill, "execute_run", _no_connect)
    code = backfill.main(["run", "--receipt-dir", str(tmp_path), "--go", "yes"], env=_ENV)
    assert code == backfill.EXIT_REFUSED
    (refused,) = tmp_path.glob("refused-*.json")
    payload = json.loads(refused.read_text())
    assert payload["code"] == "GO_MISSING"
    assert "example.invalid" not in refused.read_text()
    code = backfill.main(["run", "--receipt-dir", str(tmp_path / "c"), "--go", "Danker", "--concurrency", "0"],
                         env=_ENV)
    assert code == backfill.EXIT_REFUSED


def test_verify_samples_only_reparsed_runs_evenly(tmp_path: Path) -> None:
    lines = [{"run_id": f"r{index}", "disposition": "reparsed"} for index in range(10)]
    lines.insert(3, {"run_id": "bad", "disposition": "failed"})
    (tmp_path / "runs.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines))
    assert backfill._sample_reparsed(tmp_path, 5) == ["r0", "r2", "r4", "r6", "r8"]
    assert len(backfill._sample_reparsed(tmp_path, 50)) == 10
    assert backfill._sample_reparsed(tmp_path, 0) == []


def test_seed_runs_cover_every_missing_chunk_day_with_few_runs() -> None:
    base = datetime(2026, 9, 1, 12, tzinfo=UTC)
    candidates = [
        {"run_id": f"r{index:02d}", "start_time": base + timedelta(hours=12 * index),
         "end_time": base + timedelta(hours=12 * index, days=7)}
        for index in range(20)
    ]
    needed = set().union(*(backfill._utc_days(row["start_time"], row["end_time"]) for row in candidates))
    seeds = backfill.select_seed_runs(candidates, set())
    by_id = {row["run_id"]: row for row in candidates}
    covered = set().union(*(backfill._utc_days(by_id[r]["start_time"], by_id[r]["end_time"]) for r in seeds))
    assert covered == needed
    assert len(seeds) <= 3
    assert backfill.select_seed_runs(candidates, needed) == []
    assert backfill.select_seed_runs([], set()) == []
    partial = needed - {min(needed)}
    (only,) = backfill.select_seed_runs(candidates, partial)
    assert min(needed) in backfill._utc_days(by_id[only]["start_time"], by_id[only]["end_time"])


def test_transient_pgcode_walks_the_wrapped_cause_chain() -> None:
    class PgError(Exception):
        def __init__(self, code: str) -> None:
            super().__init__(code)
            self.pgcode = code

    for code in ("40P01", "55P03"):
        try:
            try:
                raise PgError(code)
            except PgError as inner:
                raise RuntimeError("wrapped by parser") from inner
        except RuntimeError as outer:
            assert backfill.transient_pgcode(outer) == code
    assert backfill.transient_pgcode(PgError("23505")) is None
    assert backfill.transient_pgcode(OSError("missing")) is None


def test_dispatch_cap_serializes_while_seeds_remain() -> None:
    assert backfill._dispatch_cap(4, {"s"}, ["s", "a"], {}) == 1
    assert backfill._dispatch_cap(4, {"s"}, ["a"], {object(): "s"}) == 1
    assert backfill._dispatch_cap(4, {"s"}, ["a"], {object(): "b"}) == 4


def test_timestamps_must_carry_a_timezone() -> None:
    with pytest.raises(Exception):
        backfill._parse_time("2026-09-15T00:00:00")
    assert backfill._parse_time("2026-09-15T08:00:00+08:00") == datetime(2026, 9, 15, tzinfo=UTC)


def test_source_never_deletes_drops_or_demotes() -> None:
    source = Path(backfill.__file__).read_text()
    for forbidden in ("DELETE FROM", "DROP ", "TRUNCATE", "SET timeseries_store = 'legacy'", "reset-failed"):
        assert forbidden not in source
