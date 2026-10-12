"""The #1993 forcing shape-gate probe and judge, without a database.

``receipts/2026-10-11-i14-forcing-shape-gate/probe.py`` and ``gate.py`` (fixture
``I14-1993-task82.md`` D5) are run once, by hand, read-only, against node-27,
and their verdict gates an irreversible ``DROP``. Nothing else executes them, so
the parts that decide the verdict are pinned here: what the judge calls a
fact-table Seq Scan, what it refuses to call a pass, and that the probe records
a statement before PostgreSQL can refuse it.

Located by glob, not by a fixed path: an OpenSpec change directory moves under
``openspec/changes/archive/`` when it is archived.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_REPO = Path(__file__).resolve().parents[1]
_RECEIPT = "2026-10-11-i14-forcing-shape-gate"


def _load(name: str) -> ModuleType:
    matches = sorted((_REPO / "openspec" / "changes").glob(f"**/receipts/{_RECEIPT}/{name}.py"))
    assert len(matches) == 1, f"expected exactly one {_RECEIPT}/{name}.py, found {matches}"
    spec = importlib.util.spec_from_file_location(f"forcing_shape_gate_{name}", matches[0])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load("gate")
probe = _load("probe")

CHUNK = "_hyper_12_345_chunk"
FACT = {"hypertable": "met.forcing_station_timeseries", "relations": ["forcing_station_timeseries", CHUNK],
        "chunks": 1, "compressed_chunks": 0}  # fmt: skip

INDEX_SCAN = {"depth": 1, "node": "Index Scan", "relation": CHUNK, "index": "narrow_pkey", "actual_rows": 6}
SEQ_ON_CHUNK = {"depth": 1, "node": "Seq Scan", "relation": CHUNK, "actual_rows": 6, "rows_removed_by_filter": 900}
SEQ_ON_COMPRESSED = {"depth": 2, "node": "Seq Scan", "relation": "compress_hyper_13_346_chunk",
                     "parent_relation": CHUNK}  # fmt: skip
SEQ_ON_STATION = {"depth": 1, "node": "Seq Scan", "relation": "met_station"}


def _leg(nodes: list[dict[str, Any]], *, execution_ms: float | None = None) -> dict[str, Any]:
    return {"plan_nodes": nodes, "execution_ms": execution_ms, "planning_ms": 1.0, "shared_hit": 10, "shared_read": 0}


def _statement(nodes: list[dict[str, Any]], *, execution_ms: float = 5.0) -> dict[str, Any]:
    return {"analyze_statement": "product", "plan_only": _leg(nodes), "analyze": _leg(nodes, execution_ms=execution_ms)}


def _report(**overrides: dict[str, Any]) -> dict[str, Any]:
    """A receipt in which every expected statement index-scans the one fact chunk."""
    statements = {key: _statement([INDEX_SCAN]) for key in gate.EXPECTED_STATEMENTS}
    statements.update(overrides)
    return {"fact": FACT, "identity": {"run_id": "r"}, "server": {}, "statements": statements, "not_probed": []}


def _verdict(report: dict[str, Any]) -> tuple[int, str]:
    code, lines = gate.judge(report)
    return code, "\n".join(lines)


SOME_KEY = "forecast_store.station_series_rows"


def test_the_gate_is_green_when_every_reader_index_scans_the_fact_table() -> None:
    code, text = _verdict(_report())
    assert code == gate.EXIT_PASS
    assert text.count("PASS ") == len(gate.EXPECTED_STATEMENTS) == 8
    assert "SHAPE GATE: GREEN" in text


@pytest.mark.parametrize(
    ("node", "named"),
    [
        (SEQ_ON_CHUNK, f"Seq Scan on {CHUNK}"),
        ({"depth": 0, "node": "Seq Scan", "relation": "forcing_station_timeseries"},
         "Seq Scan on forcing_station_timeseries"),
        ({**SEQ_ON_CHUNK, "node": "Parallel Seq Scan"}, f"Seq Scan on {CHUNK}"),
        # DecompressChunk over a fact chunk reading every compressed batch of it.
        (SEQ_ON_COMPRESSED, f"compress_hyper_13_346_chunk (compressed batches of {CHUNK})"),
    ],
    ids=["chunk", "hypertable_parent", "parallel", "compressed_batches_of_a_fact_chunk"],
)  # fmt: skip
def test_a_seq_scan_on_the_fact_table_or_a_chunk_of_it_fails_the_gate(node: dict[str, Any], named: str) -> None:
    code, text = _verdict(_report(**{SOME_KEY: _statement([INDEX_SCAN, node])}))
    assert code == gate.EXIT_VIOLATION
    assert f"FAIL {SOME_KEY}: fact_seq_scans=2" in text  # once per leg: plan-only and analyze
    assert named in text
    assert "SHAPE GATE: RED" in text


def test_a_seq_scan_in_the_plan_only_leg_alone_still_fails_the_gate() -> None:
    entry = _statement([INDEX_SCAN])
    entry["plan_only"] = _leg([SEQ_ON_CHUNK])
    assert _verdict(_report(**{SOME_KEY: entry}))[0] == gate.EXIT_VIOLATION


@pytest.mark.parametrize(
    "node",
    [
        SEQ_ON_STATION,
        # Another hypertable's compressed chunk (the legacy sibling, river): not this fact table.
        {"depth": 2, "node": "Seq Scan", "relation": "compress_hyper_7_99_chunk",
         "parent_relation": "_hyper_3_9_chunk"},
    ],
    ids=["authority_table", "another_hypertables_compressed_chunk"],
)  # fmt: skip
def test_a_seq_scan_on_another_relation_is_reported_but_not_judged(node: dict[str, Any]) -> None:
    code, text = _verdict(_report(**{SOME_KEY: _statement([INDEX_SCAN, node])}))
    assert code == gate.EXIT_PASS
    assert f"info: Seq Scan on non-fact relation(s): {node['relation']}" in text


def _without(key: str) -> dict[str, Any]:
    report = _report()
    del report["statements"][key]
    return report


@pytest.mark.parametrize(
    ("report", "reason"),
    [
        (_without(SOME_KEY), "missing from the probe output"),
        (_report(**{SOME_KEY: {"capture_error": "NOT_RECORDED: x", "driver_error": None}}), "NOT_RECORDED"),
        (_report(**{SOME_KEY: {**_statement([INDEX_SCAN]), "analyze": {"error": {"type": "QueryCanceled"}}}}),
         "analyze EXPLAIN failed"),
        (_report(**{SOME_KEY: {**_statement([INDEX_SCAN]), "analyze": {"skipped": "--plan-only-for"}}}),
         "analyze EXPLAIN skipped"),
        (_report(**{SOME_KEY: {"plan_only": _leg([INDEX_SCAN])}}), "analyze EXPLAIN absent"),
        # The 0918 lesson: a judge that walked no fact node reported "0 violations".
        (_report(**{SOME_KEY: _statement([SEQ_ON_STATION])}), "a zero walk is not a pass"),
        ({**_report(), "error": "NO_IDENTITY: no recent published forecast run"}, "NO_IDENTITY"),
        ({**_report(), "fact": {"relations": []}}, "no fact relations"),
    ],
    ids=["statement_missing", "not_recorded", "analyze_failed", "analyze_skipped", "analyze_absent",
         "no_fact_node_walked", "probe_error", "no_fact_relations"],
)  # fmt: skip
def test_an_incomplete_receipt_is_not_a_pass(report: dict[str, Any], reason: str) -> None:
    code, text = _verdict(report)
    assert code == gate.EXIT_INCOMPLETE
    assert reason in text


def test_a_violation_outranks_an_incomplete_statement() -> None:
    report = _without("best_available.forcing_inputs")
    report["statements"][SOME_KEY] = _statement([SEQ_ON_CHUNK])
    assert _verdict(report)[0] == gate.EXIT_VIOLATION


@pytest.mark.parametrize(
    ("execution_ms", "expected"),
    # Baseline 1612.573 ms (2026-09-20 legacy leg); an order of magnitude is 16125.73 ms.
    [(408.7, 0), (16125.0, 0), (16126.0, 1)],
)
def test_the_qhh_fallback_is_held_to_one_order_of_magnitude_of_its_legacy_baseline(
    execution_ms: float, expected: int
) -> None:
    entry = _statement([INDEX_SCAN], execution_ms=execution_ms)
    code, text = _verdict(_report(**{gate.QHH_FALLBACK_KEY: entry}))
    assert code == expected
    assert "vs legacy baseline 1612.573 ms" in text
    # Only the QHH fallback has a baseline; the same time on another reader is an absolute, not a verdict.
    assert _verdict(_report(**{SOME_KEY: _statement([INDEX_SCAN], execution_ms=99999.0)}))[0] == gate.EXIT_PASS


def test_probe_and_judge_agree_on_the_eight_reader_statements() -> None:
    assert tuple(key for key, *_ in probe.READERS) == gate.EXPECTED_STATEMENTS
    assert len(gate.EXPECTED_STATEMENTS) == 8
    assert [row["key"] for row in probe.NOT_PROBED] == ["reset_qhh_smoke_db.forcing_timeseries_delete"]


def test_every_reader_statement_is_rendered_by_the_products_own_renderer() -> None:
    """The text the probe searches the recording for names the narrow fact table."""
    for key, module, attribute, _driver in probe.READERS:
        rendered = probe.rendered_reader_sql(module, attribute, key)
        assert "met.forcing_station_timeseries" in rendered, key
        assert "forcing_station_timeseries_legacy" not in rendered, key
        assert probe.find_recorded([{"sql": "SELECT 1", "params": None}], rendered) is None
        wrapped = {"sql": f"WITH x AS ({rendered}) SELECT 1", "params": ("p",)}
        assert probe.find_recorded([{"sql": "SELECT 1", "params": None}, wrapped], rendered) is wrapped


def test_the_connection_is_opened_read_only_with_a_statement_timeout() -> None:
    assert probe.CONNECT_OPTIONS == "-c default_transaction_read_only=on -c statement_timeout=120s"


class _RefusingCursor:
    closed = False

    def execute(self, statement: str, parameters: Any = None) -> None:
        raise RuntimeError("cannot execute INSERT in a read-only transaction")

    def close(self) -> None:
        self.closed = True


def test_a_statement_is_recorded_before_the_database_can_refuse_it() -> None:
    sink: list[dict[str, Any]] = []
    inner = _RefusingCursor()
    with pytest.raises(RuntimeError), probe.RecordingCursor(inner, sink) as cursor:
        cursor.execute("INSERT INTO t SELECT %(a)s", {"a": 1})
    assert sink == [{"sql": "INSERT INTO t SELECT %(a)s", "params": {"a": 1}}]
    assert inner.closed


class _Connection:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def cursor(self, **kwargs: Any) -> _RefusingCursor:
        return _RefusingCursor()

    def commit(self) -> None:
        self.calls.append("commit")

    def rollback(self) -> None:
        self.calls.append("rollback")


def test_the_recording_connection_never_commits() -> None:
    inner = _Connection()
    connection = probe.RecordingConnection(inner, [])
    connection.commit()
    connection.rollback()
    assert inner.calls == ["rollback", "rollback"]


def test_the_plan_walk_carries_the_nearest_ancestor_relation_down() -> None:
    plan = {
        "Node Type": "Custom Scan", "Custom Plan Provider": "ChunkAppend",
        "Relation Name": "forcing_station_timeseries",
        "Plans": [{
            "Node Type": "Custom Scan", "Custom Plan Provider": "DecompressChunk", "Relation Name": CHUNK,
            "Plans": [{"Node Type": "Sort", "Plans": [
                {"Node Type": "Seq Scan", "Relation Name": "compress_hyper_13_346_chunk", "Actual Rows": 3}]}],
        }],
    }  # fmt: skip
    nodes = probe.walk_plan(plan, [])
    assert [(node["node"], node.get("relation"), node.get("parent_relation")) for node in nodes] == [
        ("Custom Scan", "forcing_station_timeseries", None),
        ("Custom Scan", CHUNK, "forcing_station_timeseries"),
        ("Sort", None, CHUNK),
        ("Seq Scan", "compress_hyper_13_346_chunk", CHUNK),
    ]
    fact, other = gate.seq_scans(nodes, frozenset(FACT["relations"]))
    assert len(fact) == 1 and other == []


def test_the_coverage_refresh_is_measured_as_its_cte_chain_without_the_insert() -> None:
    refresh = "WITH coverage AS (SELECT 1) INSERT INTO hydro.run_display_coverage (run_id) SELECT 1 FROM coverage"
    assert probe.derive_coverage_select(refresh) == "WITH coverage AS (SELECT 1) SELECT * FROM coverage"
    assert probe.derive_coverage_select("SELECT 1") is None


def test_the_probe_takes_its_dsn_from_the_environment_and_never_echoes_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "probe.json"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert probe.main(["--out", str(out)]) == 2
    assert "DATABASE_URL is unset or empty" in capsys.readouterr().err

    # Nothing listens on port 1: the connect fails, and only the error CLASS is printed.
    monkeypatch.setenv("PROBE_DSN", "postgresql://probe_user:s3cret-do-not-print@127.0.0.1:1/nhms?connect_timeout=2")
    assert probe.main(["--out", str(out), "--dsn-env", "PROBE_DSN"]) == 2
    captured = capsys.readouterr()
    assert "could not connect (OperationalError)" in captured.err
    for leaked in ("s3cret-do-not-print", "probe_user", "127.0.0.1"):
        assert leaked not in captured.out + captured.err
    assert not out.exists()
