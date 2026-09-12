"""Faithful no-network tests for the four-lane product-curve live adapters and CLI."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from email.message import Message
from io import BytesIO
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from urllib.response import addinfourl

import pytest

from packages.common.compressed_chunk_cold_residency import ResidencyGroup, ResidencyMember
from packages.common.node27_issue1895_lanes import LANE_NAMES
from packages.common.node27_issue1895_performance import (
    HTTP_BODY_LIMIT_BYTES,
    HTTP_TIMEOUT_SECONDS,
    run_warmup_and_accepted,
)
from packages.common.node27_issue1895_performance_live import (
    LOCK_TIMEOUT,
    STATEMENT_TIMEOUT_MS,
    _NoRedirect,
    close_performance_connection,
    default_opener,
    discover_four_lanes,
    fetch_local_api,
    make_sql_probe,
    mapping_rows_from_cursor,
    open_readonly_performance_connection,
    prove_readonly_session,
)
from packages.common.node27_issue1895_query import record_explicit_cycle_curve
from packages.common.node27_issue1895_types import Issue1895ReadinessError

SHA = "a" * 40
SEGMENT = "qhh_reach_000042"
BASIN = "basins_qhh"
BV = "bv-1"
RNV = "rnv-1"
ORIGIN = "http://127.0.0.1:8080"
DSN = "postgresql://nhms_display_ro:secret@127.0.0.1:55432/nhms"
API_PATH = f"/api/v1/basin-versions/{BV}/river-segments/{SEGMENT}/forecast-series"
IDENTITIES = {
    "GFS": {
        "hot": {
            "run_id": "run-gfs-hot",
            "model_id": "model-1",
            "basin_id": BASIN,
            "basin_version_id": BV,
            "river_network_version_id": RNV,
            "source_id": "GFS",
            "cycle_time": "2026-08-01T00:00:00Z",
            "status": "published",
        },
        "cold": {
            "run_id": "run-gfs-cold",
            "model_id": "model-1",
            "basin_id": BASIN,
            "basin_version_id": BV,
            "river_network_version_id": RNV,
            "source_id": "GFS",
            "cycle_time": "2026-06-01T00:00:00Z",
            "status": "published",
        },
    },
    "IFS": {
        "hot": {
            "run_id": "run-ifs-hot",
            "model_id": "model-1",
            "basin_id": BASIN,
            "basin_version_id": BV,
            "river_network_version_id": RNV,
            "source_id": "IFS",
            "cycle_time": "2026-08-02T00:00:00Z",
            "status": "published",
        },
        "cold": {
            "run_id": "run-ifs-cold",
            "model_id": "model-1",
            "basin_id": BASIN,
            "basin_version_id": BV,
            "river_network_version_id": RNV,
            "source_id": "IFS",
            "cycle_time": "2026-06-02T00:00:00Z",
            "status": "published",
        },
    },
}


def _plan(*, decompress: list[str] | None = None, buffers: int = 12, child_buffers: int = 0) -> list[dict]:
    root = {
        "Node Type": "Index Scan",
        "Relation Name": "river_timeseries",
        "Index Cond": "(river_segment_id = 'qhh_shud_riv_000042')",
        "Shared Read Blocks": buffers,
        "Shared Hit Blocks": 0,
        "Actual Rows": 8,
        "Actual Loops": 1,
    }
    current = root
    for name in decompress or []:
        child = {
            "Node Type": "Custom Scan",
            "Custom Plan Provider": "DecompressChunk",
            "Schema": "_timescaledb_internal",
            "Relation Name": name,
            "Index Cond": "(river_segment_id = 'qhh_shud_riv_000042')",
            "Shared Read Blocks": child_buffers,
            "Shared Hit Blocks": 0,
            "Actual Rows": 8,
            "Actual Loops": 1,
        }
        current["Plans"] = [child]
        current = child
    return [{"Plan": root}]


def _series_body(*, source: str, segment_id: str = SEGMENT, issue_time: str) -> bytes:
    scenario = "forecast_gfs_deterministic" if source == "GFS" else "forecast_ifs_deterministic"
    start = datetime.fromisoformat(issue_time.replace("Z", "+00:00"))
    start_ms = int(start.timestamp() * 1000)
    return json.dumps(
        {
            "segment_id": segment_id,
            "issue_time": issue_time,
            "unit": "m3/s",
            "series": [
                {
                    "scenario_id": scenario,
                    "source_id": source,
                    "cycle_time": issue_time,
                    "points": [[start_ms, 1.0], [start_ms + 3600000, 2.0]],
                }
            ],
        }
    ).encode("utf-8")


def _identity_body(source: str) -> bytes:
    identity = IDENTITIES[source]["hot"]
    return json.dumps(
        {
            "status": "ok",
            "data": {
                "status": "ready",
                "availability": {"ready": True},
                "run_id": identity["run_id"],
                "model_id": identity["model_id"],
                "basin_id": identity["basin_id"],
                "basin_version_id": identity["basin_version_id"],
                "river_network_version_id": identity["river_network_version_id"],
                "source_id": source,
                "cycle_time": identity["cycle_time"],
                "run_status": "published",
            },
        }
    ).encode("utf-8")


def _member(*, kind: str, oid: int, name: str, tablespace: str, heap_oid: int | None = None) -> ResidencyMember:
    return ResidencyMember(
        kind=kind,  # type: ignore[arg-type]
        oid=oid,
        schema="_timescaledb_internal",
        name=name,
        relkind="i" if "index" in kind else "r",
        tablespace=tablespace,
        bytes=8,
        heap_oid=heap_oid,
    )


def _fake_load_chunk(execute, **kwargs):
    from packages.common.compressed_chunk_cold_residency import CatalogChunk

    name = str(kwargs["origin_name"])
    compressed = name.endswith("_cold") or "2026-06" in name
    oid = 11 if "gfs" in name or name.endswith("1_chunk") else 21
    return CatalogChunk(
        hypertable_schema="hydro",
        hypertable_name="river_timeseries",
        origin_oid=oid,
        origin_schema="_timescaledb_internal",
        origin_name=name,
        compressed_oid=oid + 1 if compressed else None,
        compressed_schema="_timescaledb_internal" if compressed else None,
        compressed_name=f"{name}_c" if compressed else None,
        range_start=datetime(2026, 6, 1, tzinfo=UTC) if compressed else datetime(2026, 8, 1, tzinfo=UTC),
        range_end=datetime(2026, 6, 8, tzinfo=UTC) if compressed else datetime(2026, 8, 8, tzinfo=UTC),
        is_compressed=compressed,
    )


def _fake_collect_group(_execute, chunk):
    space = "nhms_cold" if chunk.is_compressed else "pg_default"
    members = [
        _member(kind="origin_heap", oid=chunk.origin_oid, name=chunk.origin_name, tablespace=space),
        _member(kind="index", oid=chunk.origin_oid + 10, name=f"{chunk.origin_name}_idx", tablespace=space),
    ]
    if chunk.is_compressed and chunk.compressed_oid is not None:
        toast_oid = chunk.compressed_oid + 20
        members.insert(
            1,
            ResidencyMember(
                kind="compressed_heap",
                oid=chunk.compressed_oid,
                schema="_timescaledb_internal",
                name=str(chunk.compressed_name),
                relkind="r",
                tablespace=space,
                bytes=8,
                toast_oid=toast_oid,
            ),
        )
        members.append(
            _member(
                kind="toast_heap",
                oid=toast_oid,
                name=f"{chunk.compressed_name}_toast",
                tablespace=space,
            )
        )
        members.append(
            _member(
                kind="toast_index",
                oid=toast_oid + 1,
                name=f"{chunk.compressed_name}_toast_idx",
                tablespace=space,
                heap_oid=toast_oid,
            )
        )
    return ResidencyGroup(
        hypertable_schema=chunk.hypertable_schema,
        hypertable_name=chunk.hypertable_name,
        origin_oid=chunk.origin_oid,
        origin_schema=chunk.origin_schema,
        origin_name=chunk.origin_name,
        compressed_oid=chunk.compressed_oid,
        compressed_schema=chunk.compressed_schema,
        compressed_name=chunk.compressed_name,
        range_start=chunk.range_start,
        range_end=chunk.range_end,
        is_compressed=chunk.is_compressed,
        members=tuple(members),
    )


class FakeCursor:
    def __init__(self, owner: FakeConnection) -> None:
        self.owner = owner
        self._result: object = None
        self._rows: list[object] = []

    def execute(self, sql: object, params: object = None) -> None:
        if not self.owner.readonly_session:
            raise RuntimeError("cursor SQL before set_session(readonly=True)")
        text = str(sql)
        self.owner.executed.append((text, params))
        self.description = None
        if "FROM pg_attribute" in text or "timescaledb_information.dimensions" in text:
            from tests.cold_residency_fakes import FakeConnection as CatalogConnection

            self._rows, names = CatalogConnection().dispatch(text, params)
            self.description = [(name,) for name in names]
            return
        if str(text).lstrip().startswith("EXPLAIN"):
            if isinstance(params, dict):
                values = params.values()
            elif isinstance(params, (tuple, list)):
                values = params
            else:
                values = ()
            compressed = any(str(value).startswith("2026-06") for value in values)
            chunk_name = "_hyper_1_1_chunk_cold" if compressed else "_hyper_1_1_chunk"
            self.owner.last_chunk_name = chunk_name
            decompress = [chunk_name] if compressed else []
            self._result = _plan(decompress=decompress, buffers=9)
            self._rows = [(self._result,)]
            self.owner.explains += 1
            return
        if "transaction_read_only" in text:
            self._result = "on"
            self._rows = [("on",)]
        elif "pg_roles" in text:
            self._result = ("nhms_display_ro", False)
            self._rows = [("nhms_display_ro", False)]
        elif "h.run_id = %s" in text and "h.model_id = %s" in text and "h.cycle_time = %s" in text:
            source = str(params[-1]).upper() if isinstance(params, tuple) else "GFS"
            self._rows = [dict(IDENTITIES[source]["hot"])]
        elif (
            "h.run_id = %(run_id)s" in text
            and "h.model_id = %(model_id)s" in text
            and "h.cycle_time = %(issue_time)s" in text
        ):
            source = "IFS" if isinstance(params, dict) and "ifs" in str(params.get("run_id", "")).lower() else "GFS"
            self._rows = [dict(IDENTITIES[source]["hot"])]
        elif "h.run_id <>" in text:
            source = str(params[1]).upper() if isinstance(params, tuple) else "GFS"
            self._rows = [dict(IDENTITIES[source]["cold"])]
        elif "timescaledb_information.chunks" in text:
            window = str(params[2]) if isinstance(params, tuple) else "2026-08-01T00:00:00Z"
            compressed = window.startswith("2026-06")
            chunk_name = "_hyper_1_1_chunk_cold" if compressed else "_hyper_1_1_chunk"
            self.owner.last_chunk_name = chunk_name
            self._rows = [
                {
                    "chunk_schema": "_timescaledb_internal",
                    "chunk_name": chunk_name,
                    "range_start": window,
                    "range_end": window,
                    "is_compressed": compressed,
                }
            ]
        elif "COUNT(*)" in text:
            self._rows = [{"row_count": 12}]
        else:
            self._result = None
            self._rows = []

    def fetchone(self) -> object:
        if self._rows:
            return self._rows[0]
        return self._result

    def fetchall(self) -> list[object]:
        return list(self._rows)

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class FakeConnection:
    def __init__(
        self,
        *,
        fail_after: int | None = None,
        candidate_rows: list[object] | None = None,
    ) -> None:
        self.readonly_session = False
        self.autocommit = True
        self.executed: list[tuple[str, object]] = []
        self.explains = 0
        self.rolled_back = False
        self.closed = False
        self.fail_after = fail_after
        self.candidate_rows = candidate_rows
        self.last_chunk_name = "_hyper_1_1_chunk"
        self.cursors: list[FakeCursor] = []

    def set_session(self, **kwargs: object) -> None:
        self.readonly_session = bool(kwargs.get("readonly") is True)
        self.autocommit = bool(kwargs.get("autocommit"))
        self.executed.append(("set_session", kwargs))

    def cursor(self) -> FakeCursor:
        if self.fail_after is not None and self.explains >= self.fail_after:
            raise RuntimeError("injected explain failure")
        cursor = FakeCursor(self)
        self.cursors.append(cursor)
        return cursor

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200, *, fail_read: bool = False) -> None:
        self.body = body
        self.status = status
        self.code = status
        self.closed = False
        self.reads: list[int] = []
        self.offset = 0
        self.fail_read = fail_read
        self.headers: dict[str, str] = {}

    def read(self, size: int = -1) -> bytes:
        self.reads.append(size)
        if self.fail_read:
            raise OSError("stream exploded")
        if size < 0:
            chunk = self.body[self.offset :]
            self.offset = len(self.body)
            return chunk
        chunk = self.body[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


class FakeOpener:
    def __init__(self, response: FakeResponse | None = None, *, error: Exception | None = None) -> None:
        self.fixed = response is not None or error is not None
        self.response = response or FakeResponse(_series_body(source="GFS", issue_time="2026-08-01T00:00:00Z"))
        self.error = error
        self.requests: list[Request] = []
        self.timeouts: list[object] = []

    def open(self, request: Request, timeout: object = None) -> FakeResponse:
        self.requests.append(request)
        self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        if self.fixed:
            return self.response
        parsed = urlparse(request.full_url)
        query = parse_qs(parsed.query)
        if parsed.path.endswith("/latest-product"):
            source = (query.get("source") or ["GFS"])[0].upper()
            self.response = FakeResponse(_identity_body(source))
            return self.response
        source = "IFS" if "forecast_ifs_deterministic" in unquote(request.full_url) else "GFS"
        issue = (query.get("issue_time") or ["2026-08-01T00:00:00Z"])[0]
        self.response = FakeResponse(_series_body(source=source, issue_time=issue))
        return self.response


def _query(source: str, run_id: str, issue: str) -> dict:
    return record_explicit_cycle_curve(
        basin_version_id=BV,
        segment_id=SEGMENT,
        river_network_version_id=RNV,
        issue_time=issue,
        run_id=run_id,
        model_id="model-1",
        source=source,
    )


def test_readonly_session_is_set_before_any_cursor_sql() -> None:
    connection = FakeConnection()
    opened = open_readonly_performance_connection(DSN, connect=lambda _dsn: connection)
    assert opened is connection
    assert connection.readonly_session is True
    assert connection.autocommit is False
    assert connection.executed[0][0] == "set_session"
    assert connection.executed[1][0] == f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"
    assert connection.executed[2][0] == f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"
    proof = prove_readonly_session(connection)
    assert proof == {"transaction_read_only": True, "current_user": "nhms_display_ro"}
    lanes = discover_four_lanes(
        connection,
        basin_id=BASIN,
        segment_id=SEGMENT,
        origin=ORIGIN,
        opener=FakeOpener(),
        load_chunk=_fake_load_chunk,
        collect_group=_fake_collect_group,
    )
    assert set(lanes) == set(LANE_NAMES)
    assert lanes["gfs_hot"]["state"] == "hot_uncompressed_source"
    assert lanes["gfs_cold"]["state"] == "cold_compressed_target"
    assert "h.cycle_time = %(issue_time)s" in lanes["gfs_hot"]["query"]["sql"]
    assert "selected_cycles" not in lanes["gfs_hot"]["query"]["sql"]
    query = lanes["gfs_hot"]["query"]
    assert isinstance(query["parameters"], dict)
    assert query["parameters"]["issue_time"] == datetime(2026, 8, 1, tzinfo=UTC)
    assert query["parameters"]["run_id"] == IDENTITIES["GFS"]["hot"]["run_id"]
    assert query["parameters"]["model_id"] == IDENTITIES["GFS"]["hot"]["model_id"]
    assert query["parameters"]["river_segment_id"] == "qhh_shud_riv_000042"
    probe = make_sql_probe(
        connection,
        explain_sql=query["explain_sql"],
        parameters=query["parameters"],
        clock=lambda: 1.0,
    )
    warmup, accepted = run_warmup_and_accepted(probe)
    assert warmup["discarded"] is True
    assert len(accepted) == 20
    close_performance_connection(connection)
    assert connection.rolled_back is True
    assert connection.closed is True


def test_set_session_failure_still_closes() -> None:
    class BrokenSession(FakeConnection):
        def set_session(self, **kwargs: object) -> None:
            super().set_session(**kwargs)
            raise RuntimeError("set_session exploded")

    connection = BrokenSession()
    with pytest.raises(Issue1895ReadinessError) as caught:
        open_readonly_performance_connection(DSN, connect=lambda _dsn: connection)
    assert caught.value.code == "SQL_CONNECT_FAILED"
    assert connection.rolled_back is True
    assert connection.closed is True


def test_http_get_is_bounded_uncredentialed_and_no_redirect() -> None:
    opener = FakeOpener()
    query = _query("GFS", "run-gfs-hot", "2026-08-01T00:00:00Z")
    sample = fetch_local_api(
        origin=ORIGIN,
        path=query["api_path"],
        query=query["api_query"],
        segment_id=SEGMENT,
        issue_time=query["issue_time"],
        scenario=query["scenario"],
        source=query["source"],
        window_start=query["window_start"],
        window_end=query["window_end"],
        opener=opener,
        clock=lambda: 1.0,
    )
    assert sample["status"] == 200
    request = opener.requests[0]
    assert isinstance(request, Request)
    assert request.get_method() == "GET"
    assert "/forecast-series?" in request.full_url
    assert "variables=q_down" in request.full_url
    assert "include_analysis=false" in request.full_url
    assert "/layers/discharge/valid-times" not in request.full_url
    assert request.has_header("Authorization") is False
    assert opener.timeouts == [HTTP_TIMEOUT_SECONDS]
    assert opener.response.reads[0] == HTTP_BODY_LIMIT_BYTES + 1
    assert opener.response.closed is True


def test_default_opener_disables_proxies_and_refuses_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "urllib.request.getproxies",
        lambda: {"http": "http://127.0.0.1:9", "https": "http://127.0.0.1:9"},
    )
    env_opener = build_opener()
    opener = default_opener()

    def proxy_maps(candidate: object) -> list[dict[str, str]]:
        return [
            dict(handler.proxies)
            for handler in getattr(candidate, "handlers", ())
            if isinstance(handler, ProxyHandler) and handler.proxies
        ]

    assert proxy_maps(env_opener)
    assert proxy_maps(opener) == []
    handlers = getattr(opener, "handlers", ())
    assert any(isinstance(handler, HTTPRedirectHandler) for handler in handlers)
    assert any(isinstance(handler, _NoRedirect) for handler in handlers)
    request = Request(f"{ORIGIN}{API_PATH}", method="GET")
    headers = Message()
    headers["Location"] = "http://127.0.0.1:8080/elsewhere"
    fp = BytesIO(b"")
    handler = _NoRedirect()
    handler.add_parent(opener)
    with pytest.raises(Issue1895ReadinessError) as caught:
        handler.redirect_request(request, fp, 302, "moved", headers, "http://127.0.0.1:8080/elsewhere")
    assert caught.value.code == "API_REDIRECT"
    info = addinfourl(fp, headers, ORIGIN, code=302)
    with pytest.raises(Issue1895ReadinessError) as http_error:
        handler.http_error_302(request, info, 302, "moved", headers)
    assert http_error.value.code == "API_REDIRECT"


def test_http_rejects_redirect_non_2xx_oversize_and_read_errors() -> None:
    query = _query("GFS", "run-gfs-hot", "2026-08-01T00:00:00Z")
    kwargs = dict(
        origin=ORIGIN,
        path=query["api_path"],
        query=query["api_query"],
        segment_id=SEGMENT,
        issue_time=query["issue_time"],
        scenario=query["scenario"],
        source=query["source"],
        window_start=query["window_start"],
        window_end=query["window_end"],
    )
    redirect = FakeOpener(error=HTTPError(ORIGIN, 302, "moved", hdrs=None, fp=None))
    with pytest.raises(Issue1895ReadinessError) as moved:
        fetch_local_api(**kwargs, opener=redirect)
    assert moved.value.code == "API_REDIRECT"
    denied = FakeOpener(error=HTTPError(ORIGIN, 403, "no", hdrs=None, fp=None))
    with pytest.raises(Issue1895ReadinessError) as status:
        fetch_local_api(**kwargs, opener=denied)
    assert status.value.code == "API_STATUS_INVALID"
    network = FakeOpener(error=URLError("down"))
    with pytest.raises(Issue1895ReadinessError) as failed:
        fetch_local_api(**kwargs, opener=network)
    assert failed.value.code == "API_REQUEST_FAILED"
    huge = FakeOpener(FakeResponse(b"x" * (HTTP_BODY_LIMIT_BYTES + 2)))
    with pytest.raises(Issue1895ReadinessError) as oversize:
        fetch_local_api(**kwargs, opener=huge)
    assert oversize.value.code == "API_BODY_LIMIT"
    broken = FakeOpener(FakeResponse(b"ok", fail_read=True))
    with pytest.raises(Issue1895ReadinessError) as read_error:
        fetch_local_api(**kwargs, opener=broken)
    assert read_error.value.code == "API_BODY_READ_FAILED"


class _Named:
    def __init__(self, name: str) -> None:
        self.name = name


class TupleCursor:
    def __init__(self, rows: list[tuple], description: list[object]) -> None:
        self._rows = rows
        self.description = description

    def execute(self, sql: object, params: object = None) -> None:
        del sql, params

    def fetchall(self) -> list[tuple]:
        return list(self._rows)


class TupleConnection(FakeConnection):
    def cursor(self) -> FakeCursor:
        cursor = super().cursor()
        original = cursor.execute

        def execute(sql: object, params: object = None) -> None:
            original(sql, params)
            if cursor._rows and isinstance(cursor._rows[0], dict):
                keys = list(cursor._rows[0].keys())
                cursor.description = [(key,) for key in keys]
                cursor._rows = [tuple(row[key] for key in keys) for row in cursor._rows]  # type: ignore[misc]

        cursor.execute = execute  # type: ignore[method-assign]
        return cursor


def test_tuple_cursor_description_maps_named_rows() -> None:
    identity = IDENTITIES["GFS"]["hot"]
    keys = list(identity)
    rows = [tuple(identity[key] for key in keys)]
    mapped = mapping_rows_from_cursor(TupleCursor(rows, [_Named(key) for key in keys]), rows)
    assert mapped == [identity]
    with pytest.raises(Issue1895ReadinessError) as missing:
        mapping_rows_from_cursor(TupleCursor(rows, None), rows)  # type: ignore[arg-type]
    assert missing.value.code == "SQL_DESCRIPTION_MISSING"
    with pytest.raises(Issue1895ReadinessError) as duplicate:
        mapping_rows_from_cursor(TupleCursor(rows, [(_Named("run_id"),), (_Named("run_id"),)]), rows)
    assert duplicate.value.code == "SQL_DESCRIPTION_DUPLICATE"
    with pytest.raises(Issue1895ReadinessError) as width:
        mapping_rows_from_cursor(TupleCursor(rows, [(_Named("run_id"),)]), rows)
    assert width.value.code == "SQL_ROW_WIDTH"


def test_discover_four_lanes_accepts_default_psycopg2_tuples() -> None:
    connection = TupleConnection()
    open_readonly_performance_connection(DSN, connect=lambda _dsn: connection)
    lanes = discover_four_lanes(
        connection,
        basin_id=BASIN,
        segment_id=SEGMENT,
        origin=ORIGIN,
        opener=FakeOpener(),
        load_chunk=_fake_load_chunk,
        collect_group=_fake_collect_group,
    )
    assert lanes["gfs_hot"]["identity"]["run_id"] == "run-gfs-hot"
    assert lanes["gfs_cold"]["identity"]["run_id"] == "run-gfs-cold"


def test_attributed_connect_requests_mapping_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    class Dummy:
        def set_session(self, **kwargs: object) -> None:
            del kwargs

        def cursor(self) -> FakeCursor:
            owner = FakeConnection()
            owner.readonly_session = True
            return FakeCursor(owner)

        def rollback(self) -> None:
            return None

        def close(self) -> None:
            return None

    def connect(dsn: str, **kwargs: object) -> Dummy:
        seen["dsn"] = dsn
        seen.update(kwargs)
        return Dummy()

    monkeypatch.setattr("psycopg2.connect", connect)
    from packages.common.node27_issue1895_performance_live import _attributed_connect

    opened = _attributed_connect(DSN)
    assert seen["cursor_factory"].__name__ == "RealDictCursor"
    assert opened is not None
