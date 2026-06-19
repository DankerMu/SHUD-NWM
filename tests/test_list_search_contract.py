"""Issue #313: river-segment / met-station list search + pagination + filter contract.

Requirement-driven coverage for the backend ``search`` predicate, retained
limit/offset pagination, advanced filters (stream_order / variable coverage),
and graceful degradation when a filter field is not reachable from the inventory
query (QC status). All search input is parameter-bound and wildcard-escaped, so
the tests also assert there is no SQL-injection face.
"""

from __future__ import annotations

from typing import Any

import pytest

from packages.common.forecast_store import PsycopgForecastStore, _escape_like
from packages.common.model_registry import PsycopgModelRegistryStore


class _RecordingCursor:
    """Captures every executed statement/params pair; returns scripted rows.

    PR 2: ``list_river_segments`` now runs a per-RNV crosswalk probe
    before its main reach query (RNV-id collect + per-RNV EXISTS probe).
    Those queries are absorbed as no-ops so the scripted results still
    map 1:1 with the legacy reach query path that these contract tests
    pin down. ``statements`` / ``parameters`` only capture the queries
    that consume scripted results, preserving the original index-based
    assertions.
    """

    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)
        self.statements: list[str] = []
        self.parameters: list[Any] = []
        self._last: Any = None
        self._absorbed_rnv_ids = False

    def execute(self, statement: str, parameters: Any = None) -> None:
        if (
            "SELECT DISTINCT rs.river_network_version_id" in statement
            and "core.river_segment" in statement
        ):
            # Hand back a single RNV so the per-RNV probe runs exactly once.
            self._last = [{"river_network_version_id": "rivnet_v01"}]
            self._absorbed_rnv_ids = True
            return
        if "river_segment_crosswalk" in statement and "EXISTS" in statement:
            # Force the legacy reach-level dispatch path.
            self._last = {"exists": False}
            self._absorbed_rnv_ids = False
            return
        self._absorbed_rnv_ids = False
        self.statements.append(statement)
        self.parameters.append(parameters)
        self._last = self._results.pop(0) if self._results else None

    def fetchone(self) -> Any:
        return self._last

    def fetchall(self) -> Any:
        if self._absorbed_rnv_ids and isinstance(self._last, list):
            return self._last
        return self._last if isinstance(self._last, list) else []


class _Transaction:
    def __init__(self, cursor: _RecordingCursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> _RecordingCursor:
        return self._cursor

    def __exit__(self, *_args: object) -> bool:
        return False


def _river_store(monkeypatch: pytest.MonkeyPatch, cursor: _RecordingCursor) -> PsycopgModelRegistryStore:
    monkeypatch.setattr(PsycopgModelRegistryStore, "_transaction", lambda _self: _Transaction(cursor))
    return PsycopgModelRegistryStore("postgresql://example")


def _forecast_store(monkeypatch: pytest.MonkeyPatch, cursor: _RecordingCursor) -> PsycopgForecastStore:
    monkeypatch.setattr(PsycopgForecastStore, "_transaction", lambda _self: _Transaction(cursor))
    return PsycopgForecastStore("postgresql://example")


# --------------------------------------------------------------------------- #
# River segments: search hits + pagination is not full-scan
# --------------------------------------------------------------------------- #


def test_river_segment_search_emits_ilike_predicate_and_paginates(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _RecordingCursor([{"total": 3, "feature_total": 1}, []])
    store = _river_store(monkeypatch, cursor)

    store.list_river_segments(
        basin_version_id="basin_v01",
        river_network_version_id="rivnet_v01",
        search="trib-7",
        limit=25,
        offset=50,
    )

    page_statement = cursor.statements[1]
    page_params = cursor.parameters[1]
    # search predicate is present and parameter-bound (no literal value spliced in).
    assert "ILIKE %s ESCAPE" in page_statement
    assert "trib-7" not in page_statement
    assert "%trib-7%" in page_params
    # pagination retained: filter first, then LIMIT/OFFSET (not full-scan).
    assert "LIMIT %s OFFSET %s" in page_statement
    assert page_params[-2:] == (25, 50)


def test_river_segment_search_omitted_adds_no_predicate(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _RecordingCursor([{"total": 1, "feature_total": 1}, []])
    store = _river_store(monkeypatch, cursor)

    store.list_river_segments(
        basin_version_id="basin_v01",
        river_network_version_id="rivnet_v01",
        limit=10,
        offset=0,
    )

    assert "ILIKE" not in cursor.statements[1]


# --------------------------------------------------------------------------- #
# River segments: stream_order filter lands on segment_order
# --------------------------------------------------------------------------- #


def test_river_segment_stream_order_filter_lands_on_segment_order(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _RecordingCursor([{"total": 1, "feature_total": 1}, []])
    store = _river_store(monkeypatch, cursor)

    store.list_river_segments(
        basin_version_id="basin_v01",
        river_network_version_id="rivnet_v01",
        stream_order_min=2,
        stream_order_max=5,
        limit=10,
        offset=0,
    )

    page_statement = cursor.statements[1]
    page_params = cursor.parameters[1]
    assert "rs.segment_order >= %s" in page_statement
    assert "rs.segment_order <= %s" in page_statement
    assert 2 in page_params
    assert 5 in page_params


# --------------------------------------------------------------------------- #
# River segments: parameterised safety (special chars never break SQL)
# --------------------------------------------------------------------------- #


def test_river_segment_search_escapes_wildcards_and_quotes(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _RecordingCursor([{"total": 0, "feature_total": 0}, []])
    store = _river_store(monkeypatch, cursor)

    malicious = "100%_'; DROP TABLE core.river_segment;--"
    store.list_river_segments(
        basin_version_id="basin_v01",
        river_network_version_id="rivnet_v01",
        search=malicious,
        limit=10,
        offset=0,
    )

    page_statement = cursor.statements[1]
    page_params = cursor.parameters[1]
    # the raw value never appears in the SQL text; it is bound as a parameter.
    assert "DROP TABLE" not in page_statement
    # every %/_ in the user value is escaped so it matches literally (no widening).
    bound = next(p for p in page_params if isinstance(p, str) and "DROP TABLE" in p)
    assert bound == "%100\\%\\_'; DROP TABLE core.river\\_segment;--%"


def test_escape_like_neutralizes_metacharacters() -> None:
    assert _escape_like("a%b_c\\d") == "a\\%b\\_c\\\\d"


# --------------------------------------------------------------------------- #
# Met stations: search + variable coverage filter
# --------------------------------------------------------------------------- #


def test_met_station_search_emits_parameterized_ilike(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _RecordingCursor([{"total_count": 2}, []])
    store = _forecast_store(monkeypatch, cursor)

    store.list_met_stations(
        basin_version_id="basin_v01",
        model_id=None,
        search="prox",
        limit=20,
        offset=0,
    )

    count_statement = cursor.statements[0]
    count_params = cursor.parameters[0]
    assert "ILIKE %s ESCAPE" in count_statement
    assert "prox" not in count_statement
    assert "%prox%" in count_params
    # pagination retained on the row query.
    assert "LIMIT %s OFFSET %s" in cursor.statements[1]
    assert cursor.parameters[1][-2:] == (20, 0)


def test_met_station_variable_coverage_filter_lands_with_model_id(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _RecordingCursor([{"total_count": 1}, []])
    store = _forecast_store(monkeypatch, cursor)

    result = store.list_met_stations(
        basin_version_id="basin_v01",
        model_id="model_v01",
        variables="PRCP,TEMP",
        limit=10,
        offset=0,
    )

    count_statement = cursor.statements[0]
    count_params = cursor.parameters[0]
    assert "met.interp_weight" in count_statement
    assert "variable = ANY(%s)" in count_statement
    assert "HAVING COUNT(DISTINCT variable) = %s" in count_statement
    assert ["PRCP", "TEMP"] in count_params
    assert 2 in count_params  # required distinct-variable count
    assert result["filters"]["available"]["variables"] is True
    assert result["filters"]["applied"]["variables"] == ["PRCP", "TEMP"]


def test_met_station_variable_filter_degrades_without_model_id(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _RecordingCursor([{"total_count": 1}, []])
    store = _forecast_store(monkeypatch, cursor)

    result = store.list_met_stations(
        basin_version_id="basin_v01",
        model_id=None,
        variables="PRCP",
        limit=10,
        offset=0,
    )

    # No interp_weight join reachable -> filter not applied, reported unavailable.
    assert "met.interp_weight" not in cursor.statements[0]
    assert result["filters"]["available"]["variables"] is False
    assert "variables" not in result["filters"]["applied"]


# --------------------------------------------------------------------------- #
# Met stations: QC status degrades gracefully (field not in inventory)
# --------------------------------------------------------------------------- #


def test_met_station_qc_status_degrades_not_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _RecordingCursor([{"total_count": 1}, []])
    store = _forecast_store(monkeypatch, cursor)

    result = store.list_met_stations(
        basin_version_id="basin_v01",
        model_id="model_v01",
        qc_status="ok",
        limit=10,
        offset=0,
    )

    # No quality_flag predicate anywhere; request is echoed back as unavailable.
    assert "quality_flag" not in cursor.statements[0]
    assert "quality_flag" not in cursor.statements[1]
    qc = result["filters"]["qc_status"]
    assert qc["available"] is False
    assert qc["requested"] == "ok"
    assert qc["reason"]


def test_met_station_search_escapes_wildcards(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _RecordingCursor([{"total_count": 0}, []])
    store = _forecast_store(monkeypatch, cursor)

    store.list_met_stations(
        basin_version_id="basin_v01",
        model_id=None,
        search="x%_'; DROP TABLE met.met_station;--",
        limit=10,
        offset=0,
    )

    count_statement = cursor.statements[0]
    assert "DROP TABLE" not in count_statement
    assert any("\\%\\_" in str(p) for p in cursor.parameters[0])


def test_met_station_invalid_variable_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _RecordingCursor([{"total_count": 0}, []])
    store = _forecast_store(monkeypatch, cursor)

    from packages.common.forecast_store import ForecastStoreError

    with pytest.raises(ForecastStoreError) as excinfo:
        store.list_met_stations(
            basin_version_id="basin_v01",
            model_id="model_v01",
            variables="NOPE",
            limit=10,
            offset=0,
        )
    assert excinfo.value.status_code == 422
