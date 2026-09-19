"""Derive the production forecast-series curve statement from its public owner.

Two consumers, and they must agree byte for byte or the evidence they exchange
means nothing:

* ``scripts/node27_timeseries_compression_benchmark.py`` records the statement a
  live capture is about to time, then measures exactly that text against
  node-27;
* ``scripts/node27_timeseries_compression_live_evidence.py`` re-derives the same
  statement OFFLINE from a recorded bundle and refuses the bundle if it differs.

The derivation lives here rather than in the benchmark script so the offline
verifier stops importing a capture CLI to get at it (#2417 fix pass 1). Nothing
in this module opens a connection: the owner is driven over a recording cursor,
and the one statement that genuinely needs a database — the #2417 run-identity
resolve — is supplied by an injected ``resolve_cursor`` factory. The benchmark
injects a short-lived live cursor; the offline verifier injects
:func:`seeded_resolve_cursor` replaying the run set the bundle already recorded.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

from packages.common.forecast_store import ForecastStoreError, PsycopgForecastStore


class CurveCaptureError(RuntimeError):
    """A fail-closed capture or publication error.

    ``scripts/node27_timeseries_compression_benchmark.py`` imports this under its
    historical name ``BenchmarkCaptureError``; it is the same class object, so
    every existing ``except``/``pytest.raises`` keeps its meaning.
    """


#: Exact placeholder set of the production curve statement. `pushdown_run_keys`
#: arrived with #2417's run-identity convergence; its `pushdown_run_ids` twin
#: bound the legacy table's text segmentby aid and went with #1342's contract
#: (task 6.3). The check is an exact equality, so additive bindings must be
#: declared here.
CURVE_PLACEHOLDER_NAMES = {
    "basin_version_id",
    "end_time",
    "issue_time",
    "pushdown_run_keys",
    "river_network_version_id",
    "river_segment_id",
    "scenario_ids",
    "scenario_tokens",
}


class _RecordingCursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, Any]]] = []

    def execute(self, statement: str, parameters: Mapping[str, Any]) -> None:
        if not isinstance(parameters, Mapping):
            raise CurveCaptureError("production curve SQL requires named bindings")
        self.calls.append((statement, dict(parameters)))

    def fetchall(self) -> list[dict[str, Any]]:
        return []


class _CaptureForecastStore(PsycopgForecastStore):
    """Recording adapter that exercises the public forecast-series owner."""

    def __init__(self, cursor: _RecordingCursor, resolve_cursor: Callable[[], Any]) -> None:
        super().__init__("recording-only")
        object.__setattr__(self, "_capture_cursor", cursor)
        object.__setattr__(self, "_resolve_cursor", resolve_cursor)

    @contextmanager
    def _transaction(self):  # type: ignore[no-untyped-def]
        yield self._capture_cursor

    def _validate_series_target(self, *args: Any, **kwargs: Any) -> None:
        # Target existence is a separate production query. The benchmark is
        # recording the public curve-owner's primary timeseries statement.
        return None

    def _resolve_run_identity(self, cursor: Any, **kwargs: Any) -> Any:
        """Resolve the curve's run set against the INJECTED cursor, not the recorder.

        The owner drives the unbound explicit-cycle read, which converges run
        identity before touching the fact table. The recording cursor returns no
        rows, so the production resolve would yield an empty key set and the
        benchmark would end up timing ``= ANY('{}')`` — a phantom win. The
        override therefore resolves through the caller's factory (the render site
        holds no connection of its own: :func:`curve_query_and_binding` runs
        before the benchmark's capture acquires any).
        """
        with self._resolve_cursor() as live_cursor:
            return super()._resolve_run_identity(live_cursor, **kwargs)


class _SeededResolveCursor:
    """A cursor that answers the resolve from a fixed row list, offline.

    Two callers: the shape pass seeds it empty (no database, so every
    statement-shape refusal still precedes connection acquisition), and the
    offline verifier in ``node27_timeseries_compression_live_evidence`` seeds it
    with the run identity a recorded bundle already carries, so the bundle's
    curve statement can be re-derived from the public owner EXACTLY rather than
    approximately.
    """

    def __init__(self, rows: Sequence[Mapping[str, Any]]) -> None:
        self._rows = [dict(row) for row in rows]

    def execute(self, statement: str, parameters: Any) -> None:
        return None

    def fetchall(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._rows]


@contextmanager
def empty_resolve_cursor():  # type: ignore[no-untyped-def]
    """A resolve-cursor factory that resolves no run and touches no database."""

    yield _SeededResolveCursor(())


def seeded_resolve_cursor(rows: Sequence[Mapping[str, Any]]) -> Callable[[], Any]:
    """A resolve-cursor factory replaying ``rows`` (``run_key`` maps).

    ``run_id`` was part of the replayed row until #1342's contract (task 6.3)
    removed the text pushdown it fed; the resolve now reads ``run_key`` alone and
    an extra key in a replayed row is simply ignored.
    """

    @contextmanager
    def acquire():  # type: ignore[no-untyped-def]
        yield _SeededResolveCursor(rows)

    return acquire


def record_curve_query(
    *,
    basin_version_id: str,
    river_segment_id: str,
    river_network_version_id: str,
    issue_time: datetime,
    scenario: str,
    resolve_cursor: Callable[[], Any],
) -> tuple[str, Mapping[str, Any]]:
    cursor = _RecordingCursor()
    try:
        _CaptureForecastStore(cursor, resolve_cursor).forecast_series(
            basin_version_id=basin_version_id,
            segment_id=river_segment_id,
            river_network_version_id=river_network_version_id,
            issue_time=issue_time.isoformat(),
            variables=["q_down"],
            scenarios=[scenario],
            include_analysis=False,
            run_types=["forecast"],
        )
    except ForecastStoreError as error:
        # The recording adapter intentionally returns no result rows. The
        # public owner raises after issuing its production query; only that
        # expected no-published-run outcome is admissible here.
        if error.code != "RUN_NOT_PUBLISHED":
            raise
    primary = [
        call
        for call in cursor.calls
        if "FROM hydro.river_timeseries rt" in call[0] and "h.run_type = 'forecast'" in call[0]
    ]
    if len(primary) != 1:
        raise CurveCaptureError("production curve path did not yield exactly one primary SQL call")
    query_text, parameters = primary[0]
    placeholder_names = set(re.findall(r"(?<!%)%\(([^)]+)\)s", query_text))
    if (
        re.search(r"(?<!%)%s", query_text)
        or placeholder_names != CURVE_PLACEHOLDER_NAMES
        or any(not isinstance(name, str) or not name for name in parameters)
        or placeholder_names != set(parameters)
    ):
        raise CurveCaptureError("production curve SQL named binding coverage changed")
    return query_text, parameters


def curve_query_and_binding(
    *,
    basin_version_id: str,
    river_segment_id: str,
    river_network_version_id: str,
    issue_time: datetime,
    end_time: datetime,
    scenario: str,
    resolve_cursor: Callable[[], Any],
) -> tuple[str, list[str], tuple[Any, ...]]:
    if end_time != issue_time + timedelta(days=7):
        raise CurveCaptureError("public curve owner supports the frozen seven-day window only")
    # Two passes, and the ordering is the point (#2417). The first runs with a
    # seeded-empty resolver and touches NO database, so every statement-shape
    # refusal still happens before a connection is acquired — the property
    # `test_capture_refuses_*_before_connecting` pins. The SQL TEXT and the
    # placeholder set do not depend on the resolved values, so the second pass,
    # which does reach the database through the caller's `connect` factory,
    # produces the identical statement with the real bindings.
    shape_query, _shape_parameters = record_curve_query(
        basin_version_id=basin_version_id,
        river_segment_id=river_segment_id,
        river_network_version_id=river_network_version_id,
        issue_time=issue_time,
        scenario=scenario,
        resolve_cursor=empty_resolve_cursor,
    )
    query_text, parameters = record_curve_query(
        basin_version_id=basin_version_id,
        river_segment_id=river_segment_id,
        river_network_version_id=river_network_version_id,
        issue_time=issue_time,
        scenario=scenario,
        resolve_cursor=resolve_cursor,
    )
    if query_text != shape_query:
        raise CurveCaptureError("production curve SQL text depends on the resolved run set")
    if not parameters["pushdown_run_keys"]:
        # An empty key set is a legitimate production answer, but it makes the
        # measured statement `= ANY('{}')` — zero rows and a phantom improvement.
        raise CurveCaptureError("curve run-identity resolution matched no production run")
    names = sorted(parameters)
    return query_text, names, tuple(parameters[name] for name in names)
