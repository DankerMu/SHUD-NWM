"""#2227 explicit-cycle named-binding recorder and live-adapter contract."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from packages.common.node27_issue1895_performance_live import execute_explain, make_sql_probe
from packages.common.node27_issue1895_query import (
    _RecordingCursor,
    _validate_captured_explicit_cycle_query,
    query_digest,
    record_explicit_cycle_curve,
)
from packages.common.node27_issue1895_types import Issue1895ReadinessError

BV = "bv-1"
SEGMENT = "qhh_reach_000042"
TS_SEGMENT = "qhh_shud_riv_000042"
RNV = "rnv-1"
ISSUE = "2026-08-01T00:00:00Z"
RUN = "run-gfs-hot"
MODEL = "model-1"


class _Cursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def execute(self, sql: str, parameters: object) -> None:
        self.calls.append((sql, parameters))

    def fetchone(self) -> tuple[list[dict[str, object]]]:
        return ([{"Plan": {"Node Type": "Index Scan"}}],)

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _Connection:
    def __init__(self) -> None:
        self.cursor_instance = _Cursor()

    def cursor(self) -> _Cursor:
        return self.cursor_instance


def _identity() -> dict[str, str]:
    return {
        "issue_time": ISSUE,
        "run_id": RUN,
        "model_id": MODEL,
        "timeseries_segment_id": TS_SEGMENT,
    }


def _named_sql(
    *,
    cycle_key: str = "issue_time",
    run_key: str = "run_id",
    model_key: str = "model_id",
    segment_key: str = "river_segment_id",
) -> str:
    return f"""
        SELECT rt.valid_time
        FROM hydro.river_timeseries rt
        JOIN hydro.hydro_run h ON h.run_key = rt.run_key
        WHERE h.run_type = 'forecast'
          AND h.cycle_time = %({cycle_key})s
          AND rt.valid_time >= %(issue_time)s
          AND rt.valid_time <= %(end_time)s
          AND h.run_id = %({run_key})s
          AND h.model_id = %({model_key})s
          AND rt.river_segment_id = %({segment_key})s
    """


def _named_parameters() -> dict[str, Any]:
    return {
        "model_id": MODEL,
        "end_time": datetime(2026, 8, 8, tzinfo=UTC),
        "river_segment_id": TS_SEGMENT,
        "issue_time": datetime(2026, 8, 1, tzinfo=UTC),
        "run_id": RUN,
    }


def _positional_sql() -> str:
    return """
        SELECT rt.valid_time
        FROM hydro.river_timeseries rt
        JOIN hydro.hydro_run h ON h.run_key = rt.run_key
        WHERE h.run_type = 'forecast'
          AND h.cycle_time = %s
          AND rt.valid_time >= %s
          AND rt.valid_time <= %s
          AND h.run_id = %s
          AND h.model_id = %s
          AND rt.river_segment_id = %s
    """


def _positional_parameters() -> tuple[object, ...]:
    return (
        datetime(2026, 8, 1, tzinfo=UTC),
        datetime(2026, 8, 1, tzinfo=UTC),
        datetime(2026, 8, 8, tzinfo=UTC),
        RUN,
        MODEL,
        TS_SEGMENT,
    )


def _assert_code(call: object, code: str) -> None:
    with pytest.raises(Issue1895ReadinessError) as caught:
        call()  # type: ignore[operator]
    assert caught.value.code == code


def test_shipping_named_capture_preserves_unique_mapping_and_reaches_explain() -> None:
    recorded = record_explicit_cycle_curve(
        basin_version_id=BV,
        segment_id=SEGMENT,
        river_network_version_id=RNV,
        issue_time=ISSUE,
        run_id=RUN,
        model_id=MODEL,
        source="GFS",
    )

    parameters = recorded["parameters"]
    assert isinstance(parameters, Mapping)
    assert "h.cycle_time = %(issue_time)s" in recorded["sql"]
    assert recorded["sql"].count("%(issue_time)s") == 2
    assert set(parameters) == {
        "basin_version_id",
        "river_segment_id",
        "river_network_version_id",
        "issue_time",
        "end_time",
        "scenario_tokens",
        "scenario_ids",
        "run_id",
        "model_id",
    }
    assert parameters["issue_time"] == datetime(2026, 8, 1, tzinfo=UTC)
    assert parameters["run_id"] == RUN
    assert parameters["model_id"] == MODEL
    assert parameters["river_segment_id"] == TS_SEGMENT

    connection = _Connection()
    payload = execute_explain(connection, sql=recorded["explain_sql"], parameters=parameters)
    assert payload == [{"Plan": {"Node Type": "Index Scan"}}]
    passed = connection.cursor_instance.calls[-1][1]
    assert isinstance(passed, Mapping)
    assert passed == parameters
    assert passed is not parameters
    parameters["run_id"] = "mutated-after-explain"
    assert passed["run_id"] == RUN


def test_recording_cursor_defensively_preserves_native_container_semantics() -> None:
    mapping: dict[str, object] = {"issue_time": ["original"]}
    cursor = _RecordingCursor()
    cursor.execute("SELECT %(issue_time)s", mapping)
    mapping["issue_time"].append("mutated")  # type: ignore[union-attr]
    captured = cursor.calls[0][1]
    assert isinstance(captured, Mapping)
    assert captured == {"issue_time": ["original"]}
    assert captured is not mapping

    values = ["original"]
    cursor.execute("SELECT %s", values)
    values.append("mutated")
    assert cursor.calls[1][1] == ("original",)


def test_named_validator_accepts_arbitrary_order_and_repeated_issue_name() -> None:
    first = _named_parameters()
    second = {key: first[key] for key in reversed(tuple(first))}

    first_result = _validate_captured_explicit_cycle_query(_named_sql(), first, _identity())
    second_result = _validate_captured_explicit_cycle_query(_named_sql(), second, _identity())

    assert isinstance(first_result, Mapping)
    assert isinstance(second_result, Mapping)
    assert first_result == second_result == first
    assert first_result is not first
    assert second_result is not second


@pytest.mark.parametrize(
    ("sql", "parameters", "code"),
    [
        (
            _named_sql(),
            {key: value for key, value in _named_parameters().items() if key != "model_id"},
            "QUERY_BINDING_SHAPE",
        ),
        (_named_sql(), {**_named_parameters(), "extra": "value"}, "QUERY_BINDING_SHAPE"),
        (_named_sql(), tuple(_named_parameters().values()), "QUERY_BINDING_SHAPE"),
        (_named_sql(cycle_key="wrong_key"), {**_named_parameters(), "wrong_key": ISSUE}, "QUERY_IDENTITY_UNBOUND"),
        (
            _named_sql(cycle_key="wrong_key"),
            {**_named_parameters(), "wrong_key": "wrong", "other": ISSUE},
            "QUERY_BINDING_SHAPE",
        ),
        (
            _named_sql(run_key="other"),
            {key: value for key, value in {**_named_parameters(), "other": RUN}.items() if key != "run_id"},
            "QUERY_IDENTITY_UNBOUND",
        ),
        (
            _named_sql(model_key="other"),
            {key: value for key, value in {**_named_parameters(), "other": MODEL}.items() if key != "model_id"},
            "QUERY_IDENTITY_UNBOUND",
        ),
        (
            _named_sql(segment_key="other"),
            {
                key: value
                for key, value in {**_named_parameters(), "other": TS_SEGMENT}.items()
                if key != "river_segment_id"
            },
            "QUERY_SEGMENT_UNBOUND",
        ),
        (
            _named_sql().replace("h.run_id = %(run_id)s", "h.run_id = %(run_id)s AND h.run_id = %(other)s"),
            {**_named_parameters(), "other": RUN},
            "QUERY_IDENTITY_UNBOUND",
        ),
        (
            _named_sql().replace("h.cycle_time = %(issue_time)s", "h.cycle_time >= %(issue_time)s"),
            _named_parameters(),
            "QUERY_IDENTITY_UNBOUND",
        ),
        (
            _named_sql().replace("rt.river_segment_id", "rt.other_segment_id"),
            _named_parameters(),
            "QUERY_SEGMENT_UNBOUND",
        ),
        (_named_sql() + " AND rt.valid_time < %s", _named_parameters(), "QUERY_BINDING_SHAPE"),
        (_named_sql().replace("%(end_time)s", "%q"), _named_parameters(), "QUERY_BINDING_SHAPE"),
        (_named_sql().replace("%(end_time)s", "%(end_time)"), _named_parameters(), "QUERY_BINDING_SHAPE"),
        (_named_sql().replace("%(end_time)s", "%%"), _named_parameters(), "QUERY_BINDING_SHAPE"),
        (_named_sql() + " AND 1 %% 1 = 0", _named_parameters(), "QUERY_BINDING_SHAPE"),
        ("WITH selected_cycles AS (SELECT 1) " + _named_sql(), _named_parameters(), "QUERY_BRANCH_INVALID"),
    ],
)
def test_named_validator_rejects_binding_and_branch_drift_before_explain(
    sql: str,
    parameters: object,
    code: str,
) -> None:
    _assert_code(lambda: _validate_captured_explicit_cycle_query(sql, parameters, _identity()), code)


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda p: p.__setitem__("issue_time", datetime(1999, 1, 1, tzinfo=UTC)), "QUERY_IDENTITY_UNBOUND"),
        (lambda p: p.__setitem__("run_id", "wrong-run"), "QUERY_IDENTITY_UNBOUND"),
        (lambda p: p.__setitem__("model_id", "wrong-model"), "QUERY_IDENTITY_UNBOUND"),
        (lambda p: p.__setitem__("river_segment_id", "wrong-segment"), "QUERY_SEGMENT_UNBOUND"),
    ],
)
def test_named_validator_rejects_wrong_predicate_values(
    mutate: object,
    code: str,
) -> None:
    parameters = _named_parameters()
    mutate(parameters)  # type: ignore[operator]
    _assert_code(lambda: _validate_captured_explicit_cycle_query(_named_sql(), parameters, _identity()), code)


def test_positional_validator_and_live_adapter_preserve_ordered_tuple() -> None:
    accepted = _validate_captured_explicit_cycle_query(_positional_sql(), list(_positional_parameters()), _identity())
    assert accepted == _positional_parameters()
    assert isinstance(accepted, tuple)

    connection = _Connection()
    probe = make_sql_probe(
        connection,
        explain_sql="EXPLAIN " + _positional_sql(),
        parameters=list(_positional_parameters()),
        clock=lambda: 1.0,
    )
    assert probe(1)["explain_json"] == [{"Plan": {"Node Type": "Index Scan"}}]
    passed = connection.cursor_instance.calls[-1][1]
    assert passed == _positional_parameters()
    assert isinstance(passed, tuple)


@pytest.mark.parametrize(
    ("sql", "parameters", "code"),
    [
        (_positional_sql(), _positional_parameters()[:-1], "QUERY_BINDING_SHAPE"),
        (_positional_sql(), {"issue_time": ISSUE}, "QUERY_BINDING_SHAPE"),
        (
            _positional_sql().replace("h.cycle_time = %s", "h.cycle_time >= %s"),
            _positional_parameters(),
            "QUERY_IDENTITY_UNBOUND",
        ),
        (_positional_sql(), ("wrong", *_positional_parameters()[1:]), "QUERY_IDENTITY_UNBOUND"),
        (
            _positional_sql(),
            (*_positional_parameters()[:3], "wrong", *_positional_parameters()[4:]),
            "QUERY_IDENTITY_UNBOUND",
        ),
        (
            _positional_sql(),
            (*_positional_parameters()[:4], "wrong", _positional_parameters()[5]),
            "QUERY_IDENTITY_UNBOUND",
        ),
        (_positional_sql(), (*_positional_parameters()[:5], "wrong"), "QUERY_SEGMENT_UNBOUND"),
        (
            _positional_sql().replace("rt.river_segment_id = %s", "rt.other_segment_id = %s"),
            _positional_parameters(),
            "QUERY_SEGMENT_UNBOUND",
        ),
        (_positional_sql() + " AND rt.valid_time < %(end_time)s", _positional_parameters(), "QUERY_BINDING_SHAPE"),
    ],
)
def test_positional_validator_rejects_shape_and_identity_drift(
    sql: str,
    parameters: object,
    code: str,
) -> None:
    _assert_code(lambda: _validate_captured_explicit_cycle_query(sql, parameters, _identity()), code)


def test_query_digest_is_container_aware_order_stable_and_time_canonical() -> None:
    first = {
        "issue_time": datetime(2026, 8, 1, tzinfo=UTC),
        "nested": {"a": ["x", 1], "b": "y"},
    }
    second = {
        "nested": {"b": "y", "a": ["x", 1]},
        "issue_time": datetime(2026, 8, 1, tzinfo=timezone(timedelta(hours=0))),
    }
    assert query_digest(sql="SELECT  1", parameters=first) == query_digest(sql="SELECT 1", parameters=second)
    assert query_digest(sql="SELECT 1", parameters=("first", "second")) != query_digest(
        sql="SELECT 1", parameters=("second", "first")
    )
    assert query_digest(sql="SELECT 1", parameters=("first",)) != query_digest(
        sql="SELECT 1", parameters={"value": "first"}
    )
    changed = {**first, "issue_time": datetime(2026, 8, 2, tzinfo=UTC)}
    assert query_digest(sql="SELECT 1", parameters=first) != query_digest(sql="SELECT 1", parameters=changed)


def test_digest_and_validator_reject_unsafe_or_mutable_input_aliases() -> None:
    original = {"issue_time": datetime(2026, 8, 1, tzinfo=UTC), "nested": ["before"]}
    digest = query_digest(sql="SELECT 1", parameters=original)
    original["nested"].append("after")  # type: ignore[union-attr]
    assert digest != query_digest(sql="SELECT 1", parameters=original)

    _assert_code(lambda: query_digest(sql="SELECT 1", parameters="not-a-container"), "QUERY_BINDING_INVALID")
    _assert_code(lambda: query_digest(sql="SELECT 1", parameters=b"not-a-container"), "QUERY_BINDING_INVALID")
    _assert_code(lambda: query_digest(sql="SELECT 1", parameters={"bad": object()}), "QUERY_BINDING_INVALID")
    _assert_code(lambda: query_digest(sql="SELECT 1", parameters={"bad": b"bytes"}), "QUERY_BINDING_INVALID")
    _assert_code(
        lambda: _validate_captured_explicit_cycle_query(_named_sql(), b"wrong", _identity()), "QUERY_BINDING_SHAPE"
    )
