"""Tests for the checked-in #2005 task 2.4/2.5 measurement harness (#2017 7.2)."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pytest

from apps.api.routes.hydro_display import _postgis_tile_params
from scripts import node27_river_tile_coordinate_evidence as evidence
from services.tiles.mvt import MVT_MAX_COORDINATES, collection_coordinate_limit


class _FakeResult:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def mappings(self) -> "_FakeResult":
        return self

    def first(self) -> dict[str, Any] | None:
        return self._row


class _FakeSession:
    """Records every bind it is handed so the two passes can be asserted."""

    def __init__(self, rows: list[dict[str, Any] | None]) -> None:
        self._rows = list(rows)
        self.binds: list[dict[str, Any]] = []
        self.statements: list[Any] = []
        self.read_only_calls = 0
        self.rollbacks = 0
        self.closed = False

    def execute(self, statement: Any, bind: dict[str, Any] | None = None) -> _FakeResult:
        text_value = str(getattr(statement, "text", statement))
        if text_value == "SET TRANSACTION READ ONLY":
            self.read_only_calls += 1
            return _FakeResult(None)
        self.statements.append(statement)
        self.binds.append(dict(bind or {}))
        return _FakeResult(self._rows.pop(0) if self._rows else None)

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


def _row(**overrides: Any) -> dict[str, Any]:
    row = {
        "feature_count": 12,
        "feature_coordinate_count": 4962,
        "feature_coordinate_overflow_count": 0,
        "coordinate_count": 52729,
        "intersecting_feature_count": 12,
        "intersecting_coordinate_count": 52729,
        "coordinate_dimension_overflow_count": 0,
        "invalid_property_count": 0,
        "source_identity_count": 1,
        "tile": b"\x1a" * 64,
    }
    row.update(overrides)
    return row


def test_parse_zooms_accepts_list_and_rejects_out_of_range() -> None:
    assert evidence.parse_zooms("3,4, 5") == (3, 4, 5)
    with pytest.raises(ValueError):
        evidence.parse_zooms("15")
    with pytest.raises(ValueError):
        evidence.parse_zooms(" , ")


def test_measure_tile_runs_production_then_unbounded_bind_with_layer() -> None:
    session = _FakeSession([_row(), _row(coordinate_count=52729)])
    record = evidence.measure_tile(
        session,
        "SQL",
        layer="river-network-national",
        z=5,
        x=26,
        y=12,
        unbounded_limit=1_000_000_000,
    )

    assert len(session.binds) == 2
    production, unbounded = session.binds
    expected = _postgis_tile_params({}, z=5, x=26, y=12, layer="river-network-national")
    assert production == expected
    # The layer bind is the whole point: dropping it silently binds 50000.
    assert production["collection_coordinate_limit"] == collection_coordinate_limit(
        "river-network-national"
    )
    assert unbounded["collection_coordinate_limit"] == 1_000_000_000
    assert {k: v for k, v in unbounded.items() if k != "collection_coordinate_limit"} == {
        k: v for k, v in production.items() if k != "collection_coordinate_limit"
    }
    assert record["truncated"] is False
    assert record["failures"] == []
    assert record["tile_bytes"] == 64


def test_measure_tile_flags_truncation_when_the_two_binds_disagree() -> None:
    session = _FakeSession([_row(coordinate_count=120000), _row(coordinate_count=131072)])
    record = evidence.measure_tile(
        session, "SQL", layer="river-network-national", z=7, x=1, y=1, unbounded_limit=10**9
    )
    assert record["coordinate_count_unbounded"] == 131072
    assert record["truncated"] is True
    assert "truncated_by_collection_budget" in record["failures"]


def test_missing_row_is_zero_not_a_crash() -> None:
    session = _FakeSession([None, None])
    record = evidence.measure_tile(
        session, "SQL", layer="river-network-national", z=3, x=0, y=0, unbounded_limit=10**9
    )
    assert record["feature_count"] == 0
    assert record["coordinate_count"] == 0
    assert record["tile_bytes"] == 0
    assert record["failures"] == []


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"feature_coordinate_count": MVT_MAX_COORDINATES}, "feature_coordinate_count_at_or_above_limit"),
        ({"feature_coordinate_overflow_count": 3}, "feature_coordinate_overflow"),
        ({"coordinate_count": 10**9}, "coordinate_count_over_collection_limit"),
        ({"invalid_property_count": 1}, "invalid_properties"),
    ],
)
def test_each_go_condition_has_its_own_failure_token(overrides: dict[str, Any], expected: str) -> None:
    row = _row(**overrides)
    session = _FakeSession([row, _row(coordinate_count=row["coordinate_count"])])
    record = evidence.measure_tile(
        session, "SQL", layer="river-network-national", z=4, x=2, y=2, unbounded_limit=10**9
    )
    assert expected in record["failures"]


def test_feature_coordinate_go_limit_tracks_the_shipped_constant() -> None:
    assert evidence.FEATURE_COORDINATE_GO_LIMIT == MVT_MAX_COORDINATES


def test_summarize_reports_per_zoom_maxima_and_go_verdict() -> None:
    records = [
        {
            "z": 3,
            "x": 1,
            "y": 1,
            "feature_coordinate_count": 100,
            "feature_coordinate_overflow_count": 0,
            "coordinate_count": 1000,
            "tile_bytes": 10,
            "seconds": 0.5,
            "truncated": False,
            "failures": [],
        },
        {
            "z": 3,
            "x": 2,
            "y": 1,
            "feature_coordinate_count": 900,
            "feature_coordinate_overflow_count": 1,
            "coordinate_count": 500,
            "tile_bytes": 40,
            "seconds": 1.5,
            "truncated": True,
            "failures": ["feature_coordinate_overflow", "truncated_by_collection_budget"],
        },
    ]
    summary = evidence.summarize(records, layer="river-network-national", unbounded_limit=10**9)
    zoom3 = summary["per_zoom"]["3"]
    assert zoom3["tiles"] == 2
    assert zoom3["max_feature_coordinate_count"] == 900
    assert zoom3["max_feature_coordinate_tile"] == [3, 2, 1]
    assert zoom3["max_coordinate_count_tile"] == [3, 1, 1]
    assert zoom3["overflow_tiles"] == 1
    assert zoom3["truncated_tiles"] == 1
    assert zoom3["failed_tiles"] == 1
    assert summary["go"] is False
    assert summary["tiles_failed"] == 1
    assert summary["failed_tiles"][0]["failures"] == records[1]["failures"]
    assert summary["schema"] == evidence.SUMMARY_SCHEMA


def test_summarize_go_true_when_every_tile_passes() -> None:
    records = [
        {
            "z": 4,
            "x": 1,
            "y": 1,
            "feature_coordinate_count": 10,
            "feature_coordinate_overflow_count": 0,
            "coordinate_count": 20,
            "tile_bytes": 5,
            "seconds": 0.1,
            "truncated": False,
            "failures": [],
        }
    ]
    summary = evidence.summarize(records, layer="river-network-national", unbounded_limit=10**9)
    assert summary["go"] is True
    assert summary["failed_tiles"] == []


def test_write_csv_emits_the_declared_columns(tmp_path: Path) -> None:
    target = tmp_path / "tiles.csv"
    evidence.write_csv(
        target,
        [
            {
                "z": 3,
                "x": 1,
                "y": 2,
                "feature_count": 4,
                "feature_coordinate_count": 5,
                "feature_coordinate_overflow_count": 0,
                "coordinate_count": 6,
                "coordinate_count_unbounded": 6,
                "truncated": False,
                "tile_bytes": 7,
                "seconds": 0.25,
                "ignored": "dropped",
            }
        ],
    )
    rows = list(csv.DictReader(target.read_text(encoding="utf-8").splitlines()))
    assert list(rows[0]) == list(evidence.CSV_COLUMNS)
    assert rows[0]["coordinate_count_unbounded"] == "6"


def test_run_without_database_url_exits_two(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert evidence.main(["--zooms", "3"]) == 2
    assert "no --database-url" in capsys.readouterr().err


def test_run_measures_every_tile_read_only_and_writes_outputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tiles = evidence.xyz_tiles(evidence.CHINA_BOUNDS, (3,))
    session = _FakeSession([_row() for _ in range(2 * len(tiles))])
    monkeypatch.setattr(evidence, "_build_session", lambda url: session)

    csv_path = tmp_path / "tiles.csv"
    json_path = tmp_path / "summary.json"
    rc = evidence.main(
        [
            "--zooms",
            "3",
            "--database-url",
            "postgresql://nhms_display_ro@127.0.0.1:55432/nhms",
            "--csv",
            str(csv_path),
            "--json",
            str(json_path),
            "--progress-every",
            "0",
        ]
    )

    assert rc == 0
    assert len(session.binds) == 2 * len(tiles)
    assert session.read_only_calls == len(tiles)
    assert session.rollbacks == len(tiles)
    assert session.closed is True
    summary = json.loads(json_path.read_text(encoding="utf-8"))
    assert summary["tiles_total"] == len(tiles)
    assert summary["go"] is True
    assert summary["csv_path"] == str(csv_path)
    assert len(list(csv.DictReader(csv_path.read_text(encoding="utf-8").splitlines()))) == len(tiles)
    assert json.loads(capsys.readouterr().out)["tiles_total"] == len(tiles)


def test_run_returns_one_when_a_tile_fails_a_go_condition(monkeypatch: pytest.MonkeyPatch) -> None:
    tiles = evidence.xyz_tiles(evidence.CHINA_BOUNDS, (3,))
    rows: list[dict[str, Any] | None] = []
    for index in range(len(tiles)):
        rows.append(_row(feature_coordinate_overflow_count=1 if index == 0 else 0))
        rows.append(_row())
    session = _FakeSession(rows)
    monkeypatch.setattr(evidence, "_build_session", lambda url: session)
    assert evidence.main(["--zooms", "3", "--database-url", "postgresql://x", "--progress-every", "0"]) == 1
