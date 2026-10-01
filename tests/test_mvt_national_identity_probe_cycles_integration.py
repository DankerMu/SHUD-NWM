"""#2009 (I5): the national discharge cycles catalog and its per-cycle valid times.

Partition of ``tests/test_mvt_national_identity_probe_integration.py`` (#2490,
pure move). The seeding rationale for these cases sits above
``_I5_PREFIX`` in ``tests/mvt_national_identity_probe_integration_helpers.py``;
the opt-in and the throwaway-database contract are the base file's.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.mvt_national_identity_probe_integration_helpers import (
    _CYCLE_TIME,
    _I5_PREFIX,
    _IFS_SOURCE_ID,
    _assert_coverage_segment_counts,
    _query,
    _refresh_coverage,
    _seed_hourly_display_ready_run,
    _seed_second_network,
    _seed_second_network_run,
    _stamp,
)
from tests.mvt_national_identity_probe_integration_helpers import (
    national_tile as national_tile,
)

pytestmark = pytest.mark.integration


# Cycle A is the base seed's cycle; B is newer, P is older. A 2-hour window holds
# exactly ONE 3-hour-stride instant (the cycle itself), which is what makes the
# per-cycle list assertions below single-valued and easy to read.
_I5_CYCLE_A = _CYCLE_TIME
_I5_CYCLE_B = _CYCLE_TIME + timedelta(hours=12)
_I5_CYCLE_P = _CYCLE_TIME - timedelta(hours=6)
_I5_IFS_CYCLE = _CYCLE_TIME + timedelta(hours=6)
_I5_WINDOW = timedelta(hours=2)

_WIDE_CYCLE_LOOKBACK = "services.tiles.mvt.NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS"


def _cycles(client: TestClient, source: str = "gfs") -> dict[str, Any]:
    response = client.get("/api/v1/layers/discharge/cycles", params={"source": source})
    assert response.status_code == 200, response.text
    return dict(response.json()["data"])


def _valid_times(client: TestClient, *, cycle: datetime, source: str = "gfs") -> list[str]:
    response = client.get(
        "/api/v1/layers/discharge/valid-times",
        params={"source": source, "cycle": _stamp(cycle)},
    )
    assert response.status_code == 200, response.text
    return list(response.json()["data"]["valid_times"])


def _assert_run_id_order(database_url: str, run_ids: tuple[str, ...]) -> None:
    """The DB's own collation puts ``run_ids`` in this order under ``ORDER BY run_id DESC``.

    ``ORDER BY h.run_id DESC`` is a TEXT sort, and which run wins ``rn = 1`` is
    the whole point of the cases that call this. Asserting the order in the
    database rather than in Python keeps the case honest under any collation.
    """
    observed = _query(
        database_url,
        "SELECT run_id FROM hydro.hydro_run WHERE run_id = ANY(%s) ORDER BY run_id DESC",
        (sorted(run_ids),),
    )
    assert [row["run_id"] for row in observed] == list(run_ids)


def test_national_cycles_list_only_cycles_covered_by_every_network(
    national_tile: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Matrix row 52: one ranked row per (network, cycle), not one per network.

    Network 1 holds cycles A and B (B newer AND its run id sorts higher);
    network 2 holds A only. Partitioning by network alone lets B win ``rn = 1``
    for network 1, after which NO cycle is covered by both networks and the
    catalog empties -- the fail-closed direction, but on a cycle that really is
    nationally covered.
    """
    database_url, client = national_tile
    monkeypatch.setattr(_WIDE_CYCLE_LOOKBACK, 100_000)
    n1_a = f"{_I5_PREFIX}_n1_run_1_at_a"
    n1_b = f"{_I5_PREFIX}_n1_run_2_at_b"
    n2_a = f"{_I5_PREFIX}_n2_run_1_at_a"
    _seed_second_network(database_url)
    _seed_hourly_display_ready_run(
        database_url,
        run_id=n1_a,
        cycle_time=_I5_CYCLE_A,
        window_start=_I5_CYCLE_A,
        window_end=_I5_CYCLE_A + _I5_WINDOW,
    )
    _seed_hourly_display_ready_run(
        database_url,
        run_id=n1_b,
        cycle_time=_I5_CYCLE_B,
        window_start=_I5_CYCLE_B,
        window_end=_I5_CYCLE_B + _I5_WINDOW,
    )
    _seed_second_network_run(
        database_url,
        run_id=n2_a,
        cycle_time=_I5_CYCLE_A,
        window_start=_I5_CYCLE_A,
        window_end=_I5_CYCLE_A + _I5_WINDOW,
    )
    for run_id in (n1_a, n1_b, n2_a):
        _refresh_coverage(database_url, run_id)
    # Without this the mutated partition would pick A for network 1 by accident
    # and the case would stay green for the wrong reason.
    _assert_run_id_order(database_url, (n1_b, n1_a))
    _assert_coverage_segment_counts(database_url, {n1_a: 2, n1_b: 2, n2_a: 2})
    assert _query(
        database_url,
        "SELECT count(DISTINCT river_network_version_id) AS n FROM core.model_instance WHERE active_flag",
        (),
    ) == [{"n": 2}]

    data = _cycles(client)

    assert data["cycles"] == [
        {
            "cycle_time": _stamp(_I5_CYCLE_A),
            "valid_time_start": _stamp(_I5_CYCLE_A),
            "valid_time_end": _stamp(_I5_CYCLE_A),
        }
    ]
    assert data["default_cycle"] == _stamp(_I5_CYCLE_A)


def test_national_valid_times_are_empty_for_a_cycle_outside_the_intersection(
    national_tile: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Matrix row 55: ``:cycle`` narrows the per-cycle query in SQL, not only in the fake.

    Both networks hold cycle A. P (= A - 6h) is a cycle NO run is ever seeded at
    -- the same production shape ``_PRUNED_CYCLE_TIME`` above stands for -- so it
    is outside the intersection and its timeline must be empty.

    P must be EARLIER than A, and P must hold no rows of its own. Dropping the
    ``:cycle`` conjunct does NOT collapse the result to one row per network (the
    window still partitions by ``(network, cycle)``, matrix row 52); it returns
    every ranked row, so the mutated call for P sees exactly the two A-rows,
    ``covered == active``, and the clamp from P lands on ``A`` -- non-empty, which
    is the red. Seeding P a run of its own would instead put ``[P, P+2h]`` into the
    intersection, ``window_end < window_start``, and ``mvt.py:2129`` would empty the
    mutated result too; a LATER cycle fails the same way. Either variant is green
    under the mutation and is NOT an oracle.
    """
    database_url, client = national_tile
    monkeypatch.setattr(_WIDE_CYCLE_LOOKBACK, 100_000)
    n1_a = f"{_I5_PREFIX}_n1_run_1_at_a"
    n2_a = f"{_I5_PREFIX}_n2_run_1_at_a"
    _seed_second_network(database_url)
    _seed_hourly_display_ready_run(
        database_url,
        run_id=n1_a,
        cycle_time=_I5_CYCLE_A,
        window_start=_I5_CYCLE_A,
        window_end=_I5_CYCLE_A + _I5_WINDOW,
    )
    _seed_second_network_run(
        database_url,
        run_id=n2_a,
        cycle_time=_I5_CYCLE_A,
        window_start=_I5_CYCLE_A,
        window_end=_I5_CYCLE_A + _I5_WINDOW,
    )
    for run_id in (n1_a, n2_a):
        _refresh_coverage(database_url, run_id)
    _assert_coverage_segment_counts(database_url, {n1_a: 2, n2_a: 2})
    # Non-vacuity: P really is unseeded, so the empty answer below is about the
    # intersection and not about a run that happens to be missing coverage.
    assert _query(
        database_url,
        "SELECT count(*) AS n FROM hydro.hydro_run WHERE cycle_time = %s",
        (_I5_CYCLE_P,),
    ) == [{"n": 0}]

    assert _valid_times(client, cycle=_I5_CYCLE_P) == []
    assert _valid_times(client, cycle=_I5_CYCLE_A) == [_stamp(_I5_CYCLE_A)]


def test_national_cycles_ignore_an_inactive_network(
    national_tile: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Matrix row 50: only ACTIVE instances form the intersection denominator.

    The second network is inactive and has no runs at all. With
    ``WHERE mi.active_flag`` widened to ``WHERE TRUE`` it joins the denominator,
    nothing covers it, and a nationally covered cycle disappears.
    """
    database_url, client = national_tile
    monkeypatch.setattr(_WIDE_CYCLE_LOOKBACK, 100_000)
    n1_a = f"{_I5_PREFIX}_n1_run_1_at_a"
    _seed_second_network(database_url, active=False)
    _seed_hourly_display_ready_run(
        database_url,
        run_id=n1_a,
        cycle_time=_I5_CYCLE_A,
        window_start=_I5_CYCLE_A,
        window_end=_I5_CYCLE_A + _I5_WINDOW,
    )
    _refresh_coverage(database_url, n1_a)
    # Two networks exist; exactly one of them is active.
    assert _query(
        database_url,
        "SELECT count(*) AS n FROM core.river_network_version",
        (),
    ) == [{"n": 2}]
    assert _query(
        database_url,
        "SELECT count(DISTINCT river_network_version_id) AS n FROM core.model_instance WHERE active_flag",
        (),
    ) == [{"n": 1}]

    data = _cycles(client)

    assert [entry["cycle_time"] for entry in data["cycles"]] == [_stamp(_I5_CYCLE_A)]
    assert data["default_cycle"] == _stamp(_I5_CYCLE_A)


def test_national_cycles_keep_a_cycle_whose_zero_segment_rival_run_sorts_first(
    national_tile: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Matrix row 3: ``AND rdc.segment_count > 0`` sits upstream of ``ROW_NUMBER()``.

    One network, two runs at cycle A: a complete hourly one and a rival with no
    river timeseries at all, whose run id sorts ABOVE it. Unmutated the JOIN
    predicate drops the zero-segment run before the ranking, so the complete run
    wins ``rn = 1`` and A is listed. Delete the predicate and the zero-segment run
    wins instead, ``_national_coverage_window`` rejects its NULL window, and a
    covered cycle vanishes.

    A SINGLE zero-segment run is not an oracle here: the cycle is unlisted either
    way. The rival pair is what makes the predicate observable.
    """
    database_url, client = national_tile
    monkeypatch.setattr(_WIDE_CYCLE_LOOKBACK, 100_000)
    complete_run = f"{_I5_PREFIX}_n1_run_1_complete"
    zero_segment_run = f"{_I5_PREFIX}_n1_run_2_zero_segment"
    _seed_hourly_display_ready_run(
        database_url,
        run_id=complete_run,
        cycle_time=_I5_CYCLE_A,
        window_start=_I5_CYCLE_A,
        window_end=_I5_CYCLE_A + _I5_WINDOW,
    )
    _seed_hourly_display_ready_run(
        database_url,
        run_id=zero_segment_run,
        cycle_time=_I5_CYCLE_A,
        window_start=_I5_CYCLE_A,
        window_end=_I5_CYCLE_A + _I5_WINDOW,
        seed_river_rows=False,
    )
    _refresh_coverage(database_url, complete_run)
    _refresh_coverage(database_url, zero_segment_run)
    _assert_run_id_order(database_url, (zero_segment_run, complete_run))
    _assert_coverage_segment_counts(database_url, {complete_run: 2, zero_segment_run: 0})

    data = _cycles(client)

    assert [entry["cycle_time"] for entry in data["cycles"]] == [_stamp(_I5_CYCLE_A)]
    assert data["default_cycle"] == _stamp(_I5_CYCLE_A)


def test_national_cycles_skip_a_cycle_older_than_the_lookback(national_tile: Any) -> None:
    """Matrix row 36: the lookback predicate really bounds the DB scan.

    The ONE case here that must run against the REAL
    ``NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS`` -- no widening. Both networks cover
    the seed cycle A (months old) and a recent cycle R, so the intersection alone
    would list both; only the ``h.cycle_time >= :since`` conjunct drops A.
    """
    database_url, client = national_tile
    recent = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(days=1)
    n1_a = f"{_I5_PREFIX}_n1_run_1_at_a"
    n1_r = f"{_I5_PREFIX}_n1_run_2_at_r"
    n2_a = f"{_I5_PREFIX}_n2_run_1_at_a"
    n2_r = f"{_I5_PREFIX}_n2_run_2_at_r"
    _seed_second_network(database_url)
    for run_id, cycle_time in ((n1_a, _I5_CYCLE_A), (n1_r, recent)):
        _seed_hourly_display_ready_run(
            database_url,
            run_id=run_id,
            cycle_time=cycle_time,
            window_start=cycle_time,
            window_end=cycle_time + _I5_WINDOW,
        )
    for run_id, cycle_time in ((n2_a, _I5_CYCLE_A), (n2_r, recent)):
        _seed_second_network_run(
            database_url,
            run_id=run_id,
            cycle_time=cycle_time,
            window_start=cycle_time,
            window_end=cycle_time + _I5_WINDOW,
        )
    for run_id in (n1_a, n1_r, n2_a, n2_r):
        _refresh_coverage(database_url, run_id)
    _assert_coverage_segment_counts(database_url, {n1_a: 2, n1_r: 2, n2_a: 2, n2_r: 2})

    data = _cycles(client)

    # A is fully covered by both networks and still absent: age is the only
    # difference between it and R.
    assert [entry["cycle_time"] for entry in data["cycles"]] == [_stamp(recent)]
    assert data["default_cycle"] == _stamp(recent)


def test_national_cycles_match_an_uppercase_source_id_from_a_lowercase_query(
    national_tile: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Matrix row 54: ``lower(h.source_id) = :source`` narrows the query in SQL.

    Production stores ``gfs`` lower-case and ``IFS`` UPPER-case. The two sources
    sit at different cycles, so dropping the conjunct (keeping the bind) makes
    each request list both cycles instead of its own.
    """
    database_url, client = national_tile
    monkeypatch.setattr(_WIDE_CYCLE_LOOKBACK, 100_000)
    gfs_run = f"{_I5_PREFIX}_n1_run_1_gfs"
    ifs_run = f"{_I5_PREFIX}_n1_run_2_ifs"
    _seed_hourly_display_ready_run(
        database_url,
        run_id=gfs_run,
        cycle_time=_I5_CYCLE_A,
        window_start=_I5_CYCLE_A,
        window_end=_I5_CYCLE_A + _I5_WINDOW,
    )
    _seed_hourly_display_ready_run(
        database_url,
        run_id=ifs_run,
        source_id=_IFS_SOURCE_ID,
        cycle_time=_I5_IFS_CYCLE,
        window_start=_I5_IFS_CYCLE,
        window_end=_I5_IFS_CYCLE + _I5_WINDOW,
        value_base=700.0,
        new_data_source_name="IFS 2009",
    )
    _refresh_coverage(database_url, gfs_run)
    _refresh_coverage(database_url, ifs_run)
    _assert_coverage_segment_counts(database_url, {gfs_run: 2, ifs_run: 2})
    assert _query(
        database_url,
        "SELECT source_id FROM hydro.hydro_run WHERE run_id = %s",
        (ifs_run,),
    ) == [{"source_id": "IFS"}], "the case is about case folding; the seed must be upper-case"

    assert [entry["cycle_time"] for entry in _cycles(client, "ifs")["cycles"]] == [_stamp(_I5_IFS_CYCLE)]
    assert [entry["cycle_time"] for entry in _cycles(client, "gfs")["cycles"]] == [_stamp(_I5_CYCLE_A)]


def test_national_cycles_take_the_newest_run_at_a_cycle(
    national_tile: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Matrix rows 53 and 56: ``ORDER BY h.run_id DESC`` picks the winner, ``rn = 1`` keeps it.

    Two runs at cycle A on one network: the older covers ``A … A+2h``, the newer
    ``A … A+5h``. The listed window's END is the discriminator -- ``A+3h`` from the
    newer run, ``A`` from the older one. ``ORDER BY ... ASC`` picks the older run;
    ``WHERE rn >= 1`` keeps BOTH rows and the intersection clamp then takes the
    shorter window. Both mutations land on ``A``.
    """
    database_url, client = national_tile
    monkeypatch.setattr(_WIDE_CYCLE_LOOKBACK, 100_000)
    older_run = f"{_I5_PREFIX}_n1_run_1_short_window"
    newer_run = f"{_I5_PREFIX}_n1_run_2_long_window"
    _seed_hourly_display_ready_run(
        database_url,
        run_id=older_run,
        cycle_time=_I5_CYCLE_A,
        window_start=_I5_CYCLE_A,
        window_end=_I5_CYCLE_A + timedelta(hours=2),
    )
    _seed_hourly_display_ready_run(
        database_url,
        run_id=newer_run,
        cycle_time=_I5_CYCLE_A,
        window_start=_I5_CYCLE_A,
        window_end=_I5_CYCLE_A + timedelta(hours=5),
        value_base=800.0,
    )
    _refresh_coverage(database_url, older_run)
    _refresh_coverage(database_url, newer_run)
    _assert_run_id_order(database_url, (newer_run, older_run))
    _assert_coverage_segment_counts(database_url, {older_run: 2, newer_run: 2})

    data = _cycles(client)

    assert data["cycles"] == [
        {
            "cycle_time": _stamp(_I5_CYCLE_A),
            "valid_time_start": _stamp(_I5_CYCLE_A),
            "valid_time_end": _stamp(_I5_CYCLE_A + timedelta(hours=3)),
        }
    ]
    assert data["default_cycle"] == _stamp(_I5_CYCLE_A)
