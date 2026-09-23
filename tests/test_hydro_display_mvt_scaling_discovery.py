"""#2009: ``national_discharge_cycles`` / ``national_discharge_valid_times``.

Partition of ``tests/test_hydro_display_mvt_scaling.py`` (#2074): the
helper-level half of the discharge cycles catalog and its per-cycle valid times.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from scripts.node27_raw_retention import DEFAULT_RETENTION_DAYS
from services.tiles import mvt as mvt_module
from services.tiles.mvt import (
    canonical_mvt_time,
    layer_metadata,
    national_discharge_cycles,
    national_discharge_valid_times,
)
from tests.hydro_display_mvt_helpers import (
    _CYCLE,
    _PREVIOUS_CYCLE,
    _REAL_CYCLE_LOOKBACK_DAYS,
    _coverage_row,
    _full_coverage_rows,
    _keep_fixed_instant_fixtures_inside_the_cycle_lookback,  # noqa: F401
    _NationalDiscoverySession,
)

# ---------------------------------------------------------------------------
# #2009: the national discharge cycles catalog and its per-cycle valid times.
#
# The fixture below answers the two statements `_national_discharge_coverage_rows`
# runs and filters the coverage rows by the BOUND VALUES, never by the SQL text.
# That is deliberate: a fake that ignored `params` would keep every "the argument
# reaches the query" claim green while the call site dropped it.
# ---------------------------------------------------------------------------


def _cycle_days_ago(days: int) -> datetime:
    """A cycle `days` before now, aligned to the hour.

    Relative, because these are the cases the lookback bound actually moves, and
    hour-aligned because `_format_time` spells seconds and the rectangle check
    divides the coverage window by 3600.
    """
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    return now - timedelta(days=days)


def test_national_cycles_intersection_excludes_a_partially_covered_cycle() -> None:
    """38-networks-have-A / 37-have-B, shrunk to three and two."""
    session = _NationalDiscoverySession(
        _full_coverage_rows(_CYCLE) + _full_coverage_rows(_PREVIOUS_CYCLE, networks=("rn-a", "rn-b"))
    )

    result = national_discharge_cycles(session, source="gfs")

    assert [entry["cycle_time"] for entry in result["cycles"]] == ["2026-09-02T12:00:00Z"]
    assert result["default_cycle"] == "2026-09-02T12:00:00Z"
    assert result["source"] == "gfs"


def test_national_cycles_are_sorted_newest_first_and_default_to_the_newest() -> None:
    cycles = [_CYCLE, _PREVIOUS_CYCLE, datetime(2026, 9, 2, 0, tzinfo=UTC)]
    rows: list[dict[str, Any]] = []
    # Interleaved on purpose: sorted() must do the work, not the row order.
    for cycle in (cycles[1], cycles[2], cycles[0]):
        rows.extend(_full_coverage_rows(cycle))
    session = _NationalDiscoverySession(rows)

    result = national_discharge_cycles(session, source="gfs")

    assert [entry["cycle_time"] for entry in result["cycles"]] == [
        "2026-09-02T12:00:00Z",
        "2026-09-02T06:00:00Z",
        "2026-09-02T00:00:00Z",
    ]
    assert result["default_cycle"] == result["cycles"][0]["cycle_time"]


def test_national_cycle_lookback_leaves_a_day_of_precip_mirror_margin() -> None:
    """`canonical-precip-copyback`: `oldest_listed_cycle - 24h >= display_watermark - retention_days`.

    With lookback `L` and the raw-retention run's `retention_days` `R` that sentence
    reduces to `L <= R - 1` -- an INEQUALITY, which is why this asserts one instead
    of matching the retention default. The same run prunes the canonical
    precipitation mirror and the precipitation PNG cache on that cutoff, so `R` is
    the right constant to pin against even though the discharge tiles' own
    timeseries lane is wider.

    Raising the DEPLOYED retention (`NODE27_RAW_RETENTION_DAYS` / `--retention-days`) is the
    copyback spec's own remedy, but this test does not observe it: what it pins is the SOURCE
    default `DEFAULT_RETENTION_DAYS` (`scripts/node27_raw_retention.py`), and lowering THAT
    below 13 is what goes red. The inequality is the load-bearing half -- round 1 asserted
    `== 14` under a name claiming a coupling it never checked, and that green test passed.
    """
    assert _REAL_CYCLE_LOOKBACK_DAYS <= DEFAULT_RETENTION_DAYS - 1
    assert _REAL_CYCLE_LOOKBACK_DAYS == 12


def test_national_cycles_list_only_cycles_inside_the_lookback_window(monkeypatch: Any) -> None:
    """Both cycles are covered by EVERY active network; only the recent one is listed."""
    monkeypatch.setattr(
        mvt_module, "NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS", _REAL_CYCLE_LOOKBACK_DAYS
    )
    recent = _cycle_days_ago(1)
    stale = _cycle_days_ago(30)
    session = _NationalDiscoverySession(_full_coverage_rows(recent) + _full_coverage_rows(stale))

    result = national_discharge_cycles(session, source="gfs")

    assert [entry["cycle_time"] for entry in result["cycles"]] == [canonical_mvt_time(recent)]
    assert result["default_cycle"] == canonical_mvt_time(recent)
    # The fake filters on the BOUND value, so the behavioural assertions above stay
    # green if the predicate is deleted from the statement but the bind is left in
    # place. This pins the statement half of the same claim.
    coverage_sql = next(sql for sql, _ in session.executions if "hydro.run_display_coverage" in sql)
    assert "AND (CAST(:since AS timestamptz) IS NULL OR h.cycle_time >= :since)" in coverage_sql


def test_national_cycles_lookback_reads_the_module_constant(monkeypatch: Any) -> None:
    """The call site must consult `NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS`, not a literal.

    The other lookback cases put the REAL value back and use 20/30-day-old cycles,
    which are stale under any plausible literal too -- so hardcoding `days=14` at
    the `since=` call site -- the constant's value at that head -- left the whole
    suite green (measured `360 pass / 0 fail` at review round 2). This case moves
    the constant to 3, past the module's autouse widening, and puts one cycle at
    1 day and one at 5: both are inside any literal the window could be hardcoded
    to, so only a call site that really reads the constant drops the 5-day one.
    """
    monkeypatch.setattr(mvt_module, "NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS", 3)
    inside = _cycle_days_ago(1)
    outside = _cycle_days_ago(5)
    session = _NationalDiscoverySession(_full_coverage_rows(inside) + _full_coverage_rows(outside))

    result = national_discharge_cycles(session, source="gfs")

    assert [entry["cycle_time"] for entry in result["cycles"]] == [canonical_mvt_time(inside)]
    assert result["default_cycle"] == canonical_mvt_time(inside)


def test_national_cycles_are_empty_when_every_covered_cycle_predates_the_lookback(
    monkeypatch: Any,
) -> None:
    """An ingest stall longer than the window: fail-closed, not a stale advertisement."""
    monkeypatch.setattr(
        mvt_module, "NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS", _REAL_CYCLE_LOOKBACK_DAYS
    )
    session = _NationalDiscoverySession(
        _full_coverage_rows(_cycle_days_ago(20)) + _full_coverage_rows(_cycle_days_ago(30))
    )

    result = national_discharge_cycles(session, source="gfs")

    assert result["cycles"] == []
    assert result["default_cycle"] is None


def test_no_argument_national_valid_times_keep_a_network_whose_newest_run_predates_the_lookback(
    monkeypatch: Any,
) -> None:
    """The bound belongs to `cycles` alone; the no-argument branch stays unbounded.

    `rn-a`'s newest display-ready run is 20 days old and still covers the next few
    days. Master intersects it with the other two networks; binding the same
    `:since` here would drop `rn-a` from the intersection entirely and widen the
    advertised window to one `rn-a` cannot render.
    """
    monkeypatch.setattr(
        mvt_module, "NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS", _REAL_CYCLE_LOOKBACK_DAYS
    )
    stale = _cycle_days_ago(20)
    fresh = _cycle_days_ago(1)
    common_end = fresh + timedelta(days=3)
    session = _NationalDiscoverySession(
        [
            _coverage_row(network="rn-a", cycle=stale, start=stale, end=common_end),
            _coverage_row(network="rn-b", cycle=fresh, start=fresh, end=fresh + timedelta(days=6)),
            _coverage_row(network="rn-c", cycle=fresh, start=fresh, end=fresh + timedelta(days=6)),
        ]
    )

    discovery = national_discharge_valid_times(session)

    assert discovery.valid_times[0] == canonical_mvt_time(fresh)
    assert discovery.valid_times[-1] == canonical_mvt_time(common_end)
    assert discovery.observed_count == 73
    assert discovery.truncated is False


def test_national_cycles_fail_closed_when_a_network_activates_between_the_two_statements() -> None:
    """Equal cardinality, different membership -- the minimal fail-OPEN race.

    The denominator query and the coverage query are two statements with their own
    READ COMMITTED snapshots. The coverage read returns `{rn-a, rn-c1, rn-c2}` for
    the cycle; a version switch then deactivates `rn-a` and activates `rn-b`, which
    never had that cycle at all, so the active read returns `{rn-b, rn-c1, rn-c2}`.
    `3 == 3`, so a cardinality comparison lists a cycle the active network `rn-b`
    cannot render. The set comparison refuses it in EITHER statement order, which
    is why #2087's swap leaves this case alone.
    """
    session = _NationalDiscoverySession(
        _full_coverage_rows(_CYCLE, networks=("rn-a", "rn-c1", "rn-c2")),
        active_networks=["rn-b", "rn-c1", "rn-c2"],
    )

    result = national_discharge_cycles(session, source="gfs")

    # Non-vacuity: the two statements really do disagree at equal size.
    assert len(session.active_networks) == 3
    assert result["cycles"] == []
    assert result["default_cycle"] is None


def test_national_cycles_fail_closed_when_one_active_network_has_no_run_for_the_source() -> None:
    """The uncovered network contributes NO row, so only `core.model_instance` can see it."""
    session = _NationalDiscoverySession(
        _full_coverage_rows(_CYCLE, networks=("rn-a", "rn-b")),
        active_networks=["rn-a", "rn-b", "rn-c"],
    )

    result = national_discharge_cycles(session, source="gfs")

    assert result["cycles"] == []
    assert result["default_cycle"] is None


def test_national_cycles_treat_a_zero_segment_run_as_uncovered() -> None:
    rows = _full_coverage_rows(_CYCLE)
    rows[-1] = _coverage_row(
        network="rn-c",
        cycle=_CYCLE,
        start=_CYCLE,
        end=_CYCLE + timedelta(hours=168),
        segment_count=0,
        river_sample_count=0,
    )
    session = _NationalDiscoverySession(rows)

    assert national_discharge_cycles(session, source="gfs")["cycles"] == []


def test_national_cycles_list_only_the_requested_source() -> None:
    rows = _full_coverage_rows(_CYCLE)
    rows.extend(
        _coverage_row(
            network=network,
            cycle=_PREVIOUS_CYCLE,
            start=_PREVIOUS_CYCLE,
            end=_PREVIOUS_CYCLE + timedelta(hours=168),
            source="IFS",
            run_id=f"run-ifs-{network}",
        )
        for network in ("rn-a", "rn-b", "rn-c")
    )
    session = _NationalDiscoverySession(rows)

    gfs = national_discharge_cycles(session, source="gfs")
    ifs = national_discharge_cycles(session, source="ifs")

    assert [entry["cycle_time"] for entry in gfs["cycles"]] == ["2026-09-02T12:00:00Z"]
    # `lower(h.source_id)`: production stores `IFS` upper-case.
    assert [entry["cycle_time"] for entry in ifs["cycles"]] == ["2026-09-02T06:00:00Z"]


def test_national_cycles_skip_a_cycle_whose_window_holds_no_stride_instant() -> None:
    """`run_display_coverage` is HOURLY, so a covered window can miss the 3-hour grid."""
    session = _NationalDiscoverySession(
        [
            _coverage_row(
                network=network,
                cycle=_CYCLE,
                start=_CYCLE + timedelta(hours=1),
                end=_CYCLE + timedelta(hours=2),
            )
            for network in ("rn-a", "rn-b")
        ]
    )

    assert national_discharge_cycles(session, source="gfs")["cycles"] == []


def test_national_cycles_and_valid_times_agree_on_every_listed_window() -> None:
    """Cross-endpoint: the cycles row's endpoints ARE the endpoints of the list."""
    rows = _full_coverage_rows(_CYCLE)
    rows[0] = _coverage_row(
        network="rn-a", cycle=_CYCLE, start=_CYCLE + timedelta(hours=6), end=_CYCLE + timedelta(hours=96)
    )
    rows.extend(_full_coverage_rows(_PREVIOUS_CYCLE))
    session = _NationalDiscoverySession(rows)

    listed = national_discharge_cycles(session, source="gfs")["cycles"]

    assert len(listed) == 2
    for entry in listed:
        discovery = national_discharge_valid_times(
            session, source="gfs", cycle=datetime.fromisoformat(entry["cycle_time"])
        )
        assert discovery.valid_times[0] == entry["valid_time_start"]
        assert discovery.valid_times[-1] == entry["valid_time_end"]
    assert listed[0]["valid_time_start"] == "2026-09-02T18:00:00Z"
    assert listed[0]["valid_time_end"] == "2026-09-06T12:00:00Z"


def test_national_per_cycle_valid_times_are_fifty_seven_three_hour_entries() -> None:
    session = _NationalDiscoverySession(_full_coverage_rows(_CYCLE))

    discovery = national_discharge_valid_times(session, source="gfs", cycle=_CYCLE)

    assert len(discovery.valid_times) == 57
    assert discovery.valid_times[0] == "2026-09-02T12:00:00Z"
    assert discovery.valid_times[-1] == "2026-09-09T12:00:00Z"
    assert discovery.observed_count == 57
    assert discovery.truncated is False
    instants = [datetime.fromisoformat(value) for value in discovery.valid_times]
    assert {later - earlier for earlier, later in zip(instants, instants[1:])} == {timedelta(hours=3)}


def test_national_per_cycle_valid_times_stop_at_the_earliest_coverage_end() -> None:
    rows = _full_coverage_rows(_CYCLE)
    rows[1] = _coverage_row(network="rn-b", cycle=_CYCLE, start=_CYCLE, end=_CYCLE + timedelta(hours=96))
    session = _NationalDiscoverySession(rows)

    discovery = national_discharge_valid_times(session, source="gfs", cycle=_CYCLE)

    assert discovery.valid_times[0] == "2026-09-02T12:00:00Z"
    assert discovery.valid_times[-1] == "2026-09-06T12:00:00Z"
    assert len(discovery.valid_times) == 33


def test_national_per_cycle_valid_times_start_at_the_latest_coverage_start() -> None:
    """Clamped below too: advertising an instant no basin can render is the bug."""
    rows = _full_coverage_rows(_CYCLE)
    rows[1] = _coverage_row(
        network="rn-b", cycle=_CYCLE, start=_CYCLE + timedelta(hours=6), end=_CYCLE + timedelta(hours=168)
    )
    session = _NationalDiscoverySession(rows)

    discovery = national_discharge_valid_times(session, source="gfs", cycle=_CYCLE)

    assert discovery.valid_times[0] == "2026-09-02T18:00:00Z"
    assert discovery.valid_times[-1] == "2026-09-09T12:00:00Z"
    assert len(discovery.valid_times) == 55


def test_national_per_cycle_valid_times_fail_closed_for_an_off_phase_coverage_grid() -> None:
    """`run_display_coverage` is an HOURLY grid, and this branch strides from the cycle.

    `rn-b` starts 90 minutes after the cycle, so its samples land at `:30` and it
    has NO sample at any `cycle + 3k h`. The rectangle check cannot see this --
    it is translation-invariant in the start instant, and this row is a perfectly
    complete 13-lead rectangle. The no-argument branch has carried the equivalent
    guarantee since before #2009 (`(common_end - start) % 3600`); moving the
    catalog's advertised list onto this branch must not lose it.
    """
    rows = [
        _coverage_row(network="rn-a", cycle=_CYCLE, start=_CYCLE, end=_CYCLE + timedelta(hours=12)),
        _coverage_row(
            network="rn-b",
            cycle=_CYCLE,
            start=_CYCLE + timedelta(minutes=90),
            end=_CYCLE + timedelta(hours=13, minutes=30),
        ),
    ]
    session = _NationalDiscoverySession(rows)

    assert national_discharge_valid_times(session, source="gfs", cycle=_CYCLE).valid_times == []
    # `national_discharge_cycles` computes its entries through the same helper, so
    # it inherits the guard: an unrenderable cycle is not listed either.
    assert national_discharge_cycles(session, source="gfs")["cycles"] == []


def test_national_per_cycle_valid_times_are_empty_for_a_cycle_outside_the_intersection() -> None:
    session = _NationalDiscoverySession(
        _full_coverage_rows(_CYCLE, networks=("rn-a", "rn-b")),
        active_networks=["rn-a", "rn-b", "rn-c"],
    )

    discovery = national_discharge_valid_times(session, source="gfs", cycle=_CYCLE)

    assert discovery.valid_times == []
    assert discovery.observed_count == 0


def test_national_per_cycle_valid_times_answer_for_the_requested_cycle() -> None:
    """Two cycles with DIFFERENT windows, so mixing them in changes the answer."""
    rows = _full_coverage_rows(_CYCLE)
    rows.extend(
        _coverage_row(
            network=network,
            cycle=_PREVIOUS_CYCLE,
            start=_PREVIOUS_CYCLE,
            end=_PREVIOUS_CYCLE + timedelta(hours=12),
        )
        for network in ("rn-a", "rn-b", "rn-c")
    )
    session = _NationalDiscoverySession(rows)

    older = national_discharge_valid_times(session, source="gfs", cycle=_PREVIOUS_CYCLE)

    assert older.valid_times[0] == "2026-09-02T06:00:00Z"
    assert older.valid_times[-1] == "2026-09-02T18:00:00Z"
    assert len(older.valid_times) == 5


def test_national_per_cycle_valid_times_keep_the_first_entries_when_truncated() -> None:
    """Unlike the no-argument branch, which keeps the TAIL: this list starts at the cycle."""
    session = _NationalDiscoverySession(_full_coverage_rows(_CYCLE))

    discovery = national_discharge_valid_times(session, source="gfs", cycle=_CYCLE, limit=5)

    assert discovery.valid_times == [
        "2026-09-02T12:00:00Z",
        "2026-09-02T15:00:00Z",
        "2026-09-02T18:00:00Z",
        "2026-09-02T21:00:00Z",
        "2026-09-03T00:00:00Z",
    ]
    assert discovery.observed_count == 57
    assert discovery.limit == 5
    assert discovery.truncated is True


def test_no_argument_national_valid_times_discard_an_older_malformed_cycle() -> None:
    """Selection BEFORE validation: one bad historical row must not blank the catalog."""
    rows = [
        _coverage_row(network="rn-a", cycle=_CYCLE, start=_CYCLE, end=_CYCLE + timedelta(hours=3)),
        _coverage_row(
            network="rn-a",
            cycle=_PREVIOUS_CYCLE,
            start=_PREVIOUS_CYCLE,
            end=_PREVIOUS_CYCLE + timedelta(hours=3),
            river_sample_count=3,  # not segment_count * lead_count -> not a rectangle
        ),
    ]
    session = _NationalDiscoverySession(rows)

    discovery = national_discharge_valid_times(session)

    assert discovery.valid_times == [
        "2026-09-02T12:00:00Z",
        "2026-09-02T13:00:00Z",
        "2026-09-02T14:00:00Z",
        "2026-09-02T15:00:00Z",
    ]


def test_no_argument_national_valid_times_rank_a_null_cycle_first_like_the_tile_ctes() -> None:
    """PostgreSQL's `ORDER BY h.cycle_time DESC` implies NULLS FIRST.

    Pre-#2009 that ordering lived entirely in SQL, so a display-ready run with
    `cycle_time IS NULL` won `rn = 1` for its network. The two national tile CTEs
    still rank in raw SQL and are untouched by #2009, so ranking such a row LAST
    in Python would make the discovery endpoint advertise one run's window while
    the tile route paints a different run's geometry.
    """
    rows = [
        _coverage_row(
            network="rn-a",
            cycle=_CYCLE,
            start=_CYCLE,
            end=_CYCLE + timedelta(hours=3),
            run_id="run-a-null-cycle",
            cycle_time=None,
        ),
        _coverage_row(
            network="rn-a",
            cycle=_CYCLE,
            start=_CYCLE + timedelta(hours=10),
            end=_CYCLE + timedelta(hours=12),
            run_id="run-a-cycled",
        ),
    ]
    session = _NationalDiscoverySession(rows)

    discovery = national_discharge_valid_times(session)

    assert discovery.valid_times == [
        "2026-09-02T12:00:00Z",
        "2026-09-02T13:00:00Z",
        "2026-09-02T14:00:00Z",
        "2026-09-02T15:00:00Z",
    ]
    # The cycles list goes the OTHER way on purpose: a NULL cycle has no spelling
    # to put in `cycles[].cycle_time` or a tile URL's `{cycle}`, so it is skipped
    # and only the real cycle is advertised.
    listed = national_discharge_cycles(session, source="gfs")["cycles"]
    assert [entry["cycle_time"] for entry in listed] == ["2026-09-02T12:00:00Z"]


@pytest.mark.parametrize(
    ("case", "covering", "active"),
    [
        ("partial", ("rn-a", "rn-b"), ["rn-a", "rn-b", "rn-c"]),
        ("equal-size-mismatch", ("rn-a", "rn-c1", "rn-c2"), ["rn-b", "rn-c1", "rn-c2"]),
        ("covered-superset", ("rn-a", "rn-b", "rn-c"), ["rn-a", "rn-b"]),
    ],
)
def test_no_argument_national_valid_times_fail_closed_unless_the_covered_set_equals_the_active_set(
    case: str, covering: tuple[str, ...], active: list[str]
) -> None:
    """#2458: the no-argument branch judges the helper's ACTIVE set, as a SET.

    Before #2458 the branch discarded the active set and intersected whoever had
    rows, so an active network with no display-ready run was invisible (partial),
    and so was every other membership difference. The three shapes are the three
    ways a set comparison can differ: subset, equal size with different members
    (a cardinality compare would pass it), and strict superset (a "covers at least
    the active set" compare would pass it).
    """
    rows = _full_coverage_rows(_CYCLE, networks=covering)

    discovery = national_discharge_valid_times(_NationalDiscoverySession(rows, active_networks=active))

    # Non-vacuity: the very same rows judged against their OWN networks yield a
    # timeline, so the empty answer below is the set rule and not the rows.
    assert national_discharge_valid_times(_NationalDiscoverySession(rows)).valid_times, case
    assert frozenset(covering) != frozenset(active), case
    assert discovery.valid_times == [], case
    assert discovery.observed_count == 0, case
    assert discovery.truncated is False, case


def test_national_valid_times_reject_half_an_identity() -> None:
    session = _NationalDiscoverySession(_full_coverage_rows(_CYCLE))

    with pytest.raises(ValueError):
        national_discharge_valid_times(session, source="gfs")
    with pytest.raises(ValueError):
        national_discharge_valid_times(session, cycle=_CYCLE)


def test_layer_source_refs_refuses_the_discharge_layer() -> None:
    """`layer_metadata` short-circuits discharge to `source_refs={}`; this is the backstop.

    A future refactor that wires discharge back through this helper would put
    `run_id` into the metadata version hash input again and split the runless and
    run-scoped catalogs' ETags. The entry assertion has existed since PR #602 with
    no test at all.
    """
    with pytest.raises(AssertionError):
        mvt_module._layer_source_refs(
            layer_id="discharge",
            run_id="run_1",
            source_version="v1",
            basin_version_id="bv_a",
            river_network_version_id="rnv_a",
        )


def test_national_discharge_metadata_advertises_exactly_one_identity() -> None:
    metadata = layer_metadata(
        "discharge",
        run_id="run_1",
        source_version="national-hydro-v1",
        valid_times=["2026-09-02T12:00:00Z"],
        national=True,
        default_cycle="2026-09-02T12:00:00Z",
    )

    assert metadata["tile_url_template"] == (
        "/api/v1/tiles/hydro-national/{source}/{cycle}/q_down/{valid_time}/{z}/{x}/{y}.pbf"
    )
    assert metadata["url_template"] == metadata["tile_url_template"]
    assert metadata["required_placeholders"] == ["source", "cycle", "valid_time", "z", "x", "y"]
    assert "{run_id}" not in metadata["tile_url_template"]
    assert metadata["default_source"] == "gfs"
    assert metadata["default_cycle"] == "2026-09-02T12:00:00Z"
    assert metadata["cycles_url_template"] == "/api/v1/layers/discharge/cycles?source={source}"
    assert metadata["valid_times_url_template"] == (
        "/api/v1/layers/discharge/valid-times?source={source}&cycle={cycle}"
    )
    assert metadata["maplibre_source_layer"] == "hydro"
    assert "basin_id" in metadata["property_schema"]["required"]
    assert metadata["source_refs"] == {}


@pytest.mark.parametrize("field", ["default_source", "cycles_url_template", "valid_times_url_template"])
def test_national_discharge_metadata_version_hashes_every_identity_field(monkeypatch: Any, field: str) -> None:
    """Each of the four is in the `_stable_json_hash` input, not only in the payload.

    The version is the ETag input, so a contract field that the payload advertises
    and the hash ignores would let a client keep a cached catalog whose identity
    has changed underneath it.
    """
    kwargs: dict[str, Any] = {
        "source_version": "national-hydro-v1",
        "valid_times": ["2026-09-02T12:00:00Z"],
        "national": True,
        "default_cycle": "2026-09-02T12:00:00Z",
    }
    baseline = layer_metadata("discharge", **kwargs)["cache_version"]

    assert layer_metadata("discharge", **{**kwargs, "default_cycle": "2026-09-02T06:00:00Z"})[
        "cache_version"
    ] != baseline
    monkeypatch.setitem(mvt_module._NATIONAL_DISCHARGE_METADATA, field, "/moved")
    assert layer_metadata("discharge", **kwargs)["cache_version"] != baseline


def test_sibling_layer_metadata_versions_are_untouched_by_the_discharge_identity() -> None:
    """Frozen against the values master emits: the four new fields are discharge-only.

    Adding them unconditionally to the hash input would rotate every layer's
    `cache_version` and therefore every layer's metadata ETag, for a contract
    that only the discharge entry changed.
    """
    national_river = layer_metadata("river-network", source_version="generation-a", national=True)
    run_scoped_river = layer_metadata(
        "river-network",
        run_id="run_1",
        basin_version_id="bv_a",
        river_network_version_id="rnv_a",
        source_version="run-source-v1",
    )

    assert national_river["tile_url_template"] == "/api/v1/tiles/river-network-national/{z}/{x}/{y}.pbf"
    assert national_river["required_placeholders"] == ["z", "x", "y"]
    assert national_river["cache_version"] == (
        "3781c3e391aa38d7dc3072c5f50ee9ec07396277c83e9d9aa95ec8e5b3e91679"
    )
    assert run_scoped_river["tile_url_template"] == (
        "/api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf"
    )
    assert run_scoped_river["required_placeholders"] == ["basin_version_id", "z", "x", "y"]
    assert run_scoped_river["cache_version"] == (
        "73a44576017f0956ff4e93f0d9b99e0e6315b32b2801b06cc0eb305b4a5ca733"
    )
    for metadata in (national_river, run_scoped_river):
        for field in ("default_source", "default_cycle", "cycles_url_template", "valid_times_url_template"):
            assert field not in metadata
