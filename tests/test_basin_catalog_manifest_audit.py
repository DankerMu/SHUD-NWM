"""#2621 basin-catalog / scheduler-manifest audit, without a database.

The audit's database edge is the production ``list_basins`` (paged) plus one
active-model count; both are replaced here by an in-memory catalog at that
boundary, so these tests pin the verdict rules, the exit codes, the receipt
shape and the paging loop. The real ``PsycopgModelRegistryStore`` path runs in
``tests/test_basin_catalog_manifest_audit_integration.py``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import psycopg2
import pytest
from psycopg2.extensions import parse_dsn

from packages.common.model_registry_contracts import ModelRegistryError
from scripts.basin_catalog_manifest_audit import (
    EXIT_CONFIG_ERROR,
    EXIT_PASS,
    EXIT_VIOLATION,
    load_manifest_basin_ids,
    main,
    read_only_dsn,
    run_audit,
)

RECEIPT_FIELDS = {
    "verdict",
    "default_count",
    "display_count",
    "manifest_count",
    "display_minus_manifest",
    "manifest_minus_display",
    "catalog_extras",
    "violations",
    "manifest_path",
    "page_size",
    "snapshot",
    "generated_at",
}


class _Catalog:
    """``list_basins`` with the production paging contract (ORDER BY name, id; LIMIT/OFFSET)."""

    def __init__(self, default: Sequence[str], display: Sequence[str]) -> None:
        self._default = sorted(default)
        self._display = sorted(display)
        self.calls: list[tuple[int, int, bool]] = []

    def list_basins(self, *, limit: int, offset: int, has_display_product: bool = False) -> list[dict[str, Any]]:
        self.calls.append((limit, offset, has_display_product))
        rows = self._display if has_display_product else self._default
        return [{"basin_id": basin_id, "basin_name": basin_id} for basin_id in rows[offset : offset + limit]]


def _active(counts: Mapping[str, int]) -> Any:
    def count(basin_ids: Sequence[str]) -> dict[str, int]:
        return {basin_id: counts.get(basin_id, 0) for basin_id in basin_ids}

    return count


def _manifest(tmp_path: Path, basin_ids: Sequence[str], *, rows_per_basin: int = 2) -> Path:
    path = tmp_path / "manifest-last.json"
    models = [
        {"model_id": f"{basin_id}_m{index}", "basin_id": basin_id, "active_flag": True}
        for basin_id in basin_ids
        for index in range(rows_per_basin)
    ]
    path.write_text(json.dumps({"schema_version": 1, "models": models}), encoding="utf-8")
    return path


def _manifest_args(tmp_path: Path, basin_ids: Sequence[str]) -> dict[str, Any]:
    """``run_audit``'s manifest arguments: the basin ids as ``main`` reads them once, and their path."""
    path = _manifest(tmp_path, basin_ids)
    return {"manifest": load_manifest_basin_ids(path), "manifest_path": path}


def test_retired_catalog_extras_without_active_models_pass(tmp_path: Path) -> None:
    catalog = _Catalog(default=["b_a", "b_b", "b_retired", "b_old"], display=["b_a", "b_b"])
    receipt = run_audit(
        catalog, _active({"b_a": 2, "b_b": 1}), **_manifest_args(tmp_path, ["b_a", "b_b"]), page_size=500
    )

    assert receipt["verdict"] == "pass"
    assert receipt["violations"] == []
    assert (receipt["default_count"], receipt["display_count"], receipt["manifest_count"]) == (4, 2, 2)
    assert receipt["catalog_extras"] == [
        {"basin_id": "b_old", "active_models": 0},
        {"basin_id": "b_retired", "active_models": 0},
    ]
    assert receipt["display_minus_manifest"] == [] and receipt["manifest_minus_display"] == []


def test_an_active_basin_absent_from_the_manifest_is_a_violation(tmp_path: Path) -> None:
    catalog = _Catalog(default=["b_a", "b_live", "b_retired"], display=["b_a"])
    receipt = run_audit(catalog, _active({"b_a": 1, "b_live": 3}), **_manifest_args(tmp_path, ["b_a"]), page_size=500)

    assert receipt["verdict"] == "violation"
    assert receipt["catalog_extras"] == [
        {"basin_id": "b_live", "active_models": 3},
        {"basin_id": "b_retired", "active_models": 0},
    ]
    assert receipt["violations"] == [{"kind": "active_catalog_extra", "basin_id": "b_live", "active_models": 3}]


def test_display_and_manifest_drift_is_reported_in_both_directions(tmp_path: Path) -> None:
    # b_new is onboarding (in the manifest, no ready run yet); b_gone still shows a
    # display product but has left the manifest.
    catalog = _Catalog(default=["b_a", "b_gone", "b_new"], display=["b_a", "b_gone"])
    receipt = run_audit(
        catalog, _active({"b_a": 1, "b_new": 1}), **_manifest_args(tmp_path, ["b_a", "b_new"]), page_size=500
    )

    assert receipt["verdict"] == "violation"
    assert receipt["display_minus_manifest"] == ["b_gone"]
    assert receipt["manifest_minus_display"] == ["b_new"]
    assert {"kind": "display_not_in_manifest", "basin_id": "b_gone"} in receipt["violations"]
    assert {"kind": "manifest_not_in_display", "basin_id": "b_new"} in receipt["violations"]
    # b_gone is a catalog extra with 0 active models: explained, not a third violation.
    assert len(receipt["violations"]) == 2


def test_both_catalog_sets_are_read_to_the_last_page(tmp_path: Path) -> None:
    default = [f"b_{index:02d}" for index in range(7)]
    catalog = _Catalog(default=default, display=default[:5])
    receipt = run_audit(catalog, _active({}), **_manifest_args(tmp_path, default[:5]), page_size=3)

    assert (receipt["default_count"], receipt["display_count"]) == (7, 5)
    assert [basin["basin_id"] for basin in receipt["catalog_extras"]] == ["b_05", "b_06"]
    assert receipt["verdict"] == "pass"
    assert catalog.calls == [
        (3, 0, False),
        (3, 3, False),
        (3, 6, False),
        (3, 0, True),
        (3, 3, True),
    ]


def test_an_exactly_full_last_page_is_followed_by_an_empty_one(tmp_path: Path) -> None:
    catalog = _Catalog(default=["b_1", "b_2", "b_3", "b_4"], display=["b_1", "b_2"])
    run_audit(catalog, _active({}), **_manifest_args(tmp_path, ["b_1", "b_2"]), page_size=2)

    assert catalog.calls == [(2, 0, False), (2, 2, False), (2, 4, False), (2, 0, True), (2, 2, True)]


def test_the_receipt_carries_every_field_and_the_per_page_snapshot_note(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, ["b_a"])
    receipt = run_audit(
        _Catalog(["b_a"], ["b_a"]), _active({"b_a": 1}), manifest={"b_a"}, manifest_path=manifest, page_size=10
    )

    assert set(receipt) == RECEIPT_FIELDS
    assert receipt["manifest_path"] == str(manifest)
    assert receipt["page_size"] == 10
    assert "per page" in receipt["snapshot"]
    json.dumps(receipt)  # the CLI prints it as JSON


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (None, "not found"),
        ("{not json", "not valid JSON"),
        ('{"models": {"basin_id": "b_a"}}', "models"),
        ('{"schema_version": 1}', "models"),
        ('{"models": [{"model_id": "m"}]}', "basin_id"),
        ('{"models": [{"basin_id": ""}]}', "basin_id"),
        ('{"models": []}', "no basin ids"),
    ],
    ids=["missing", "unparseable", "models-not-a-list", "no-models-key", "row-without-basin", "blank-basin", "empty"],
)
def test_an_unusable_manifest_exits_2_without_a_pass_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    content: str | None,
    message: str,
) -> None:
    manifest = tmp_path / "manifest-last.json"
    if content is not None:
        manifest.write_text(content, encoding="utf-8")
    # The manifest is judged before any connection: this DSN points nowhere.
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@127.0.0.1:1/none")

    assert main(["--manifest", str(manifest)]) == EXIT_CONFIG_ERROR

    receipt = json.loads(capsys.readouterr().out)
    assert receipt["verdict"] == "config_error"
    assert message in receipt["error"]


def test_a_missing_database_url_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert main(["--manifest", str(_manifest(tmp_path, ["b_a"]))]) == EXIT_CONFIG_ERROR

    output = capsys.readouterr().out
    receipt = json.loads(output)
    assert receipt["verdict"] == "config_error"
    assert "DATABASE_URL" in receipt["error"]


def test_the_exit_code_follows_the_verdict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.basin_catalog_manifest_audit as audit

    manifest = _manifest(tmp_path, ["b_a"])
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@127.0.0.1:1/none")
    catalog = _Catalog(["b_a", "b_live"], ["b_a"])
    monkeypatch.setattr(audit, "_connect_catalog", lambda database_url: (catalog, _active({"b_live": 1})))
    assert main(["--manifest", str(manifest)]) == EXIT_VIOLATION

    catalog = _Catalog(["b_a", "b_retired"], ["b_a"])
    monkeypatch.setattr(audit, "_connect_catalog", lambda database_url: (catalog, _active({})))
    assert main(["--manifest", str(manifest)]) == EXIT_PASS


def test_the_dsn_is_forced_read_only_and_keeps_existing_options() -> None:
    merged = parse_dsn(
        read_only_dsn("postgresql://ro:secret@db.example:5433/nhms?options=-c%20statement_timeout%3D5s&sslmode=disable")
    )
    assert merged["options"] == "-c statement_timeout=5s -c default_transaction_read_only=on"
    assert (merged["user"], merged["password"], merged["host"], merged["port"], merged["dbname"]) == (
        "ro",
        "secret",
        "db.example",
        "5433",
        "nhms",
    )
    assert merged["sslmode"] == "disable"

    bare = parse_dsn(read_only_dsn("host=127.0.0.1 dbname=nhms user=nhms_display_ro"))
    assert bare["options"] == "-c default_transaction_read_only=on"


class _FailingCatalog:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def list_basins(self, *, limit: int, offset: int, has_display_product: bool = False) -> list[dict[str, Any]]:
        raise self._error


def _raise_on_connect(database_url: str) -> Any:
    raise psycopg2.OperationalError('connection to server at "127.0.0.1", port 1 failed: Connection refused\n')


def _catalog_raising_on_query(database_url: str) -> Any:
    error = ModelRegistryError("Model registry database operation failed: relation core.basin does not exist\nLINE 1")
    return _FailingCatalog(error), _active({})


@pytest.mark.parametrize(
    ("connect", "expected"),
    [(_raise_on_connect, "OperationalError"), (_catalog_raising_on_query, "ModelRegistryError")],
    ids=["connect-fails", "query-fails"],
)
def test_a_database_error_exits_2_with_one_stderr_line_and_no_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    connect: Any,
    expected: str,
) -> None:
    import scripts.basin_catalog_manifest_audit as audit

    dsn = "postgresql://audit_user:s3cr3t-pw@127.0.0.1:1/none"
    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.setattr(audit, "_connect_catalog", connect)

    assert main(["--manifest", str(_manifest(tmp_path, ["b_a"]))]) == EXIT_CONFIG_ERROR

    captured = capsys.readouterr()
    assert captured.out == ""  # no receipt: nothing was judged
    error_line = captured.err.strip()
    assert error_line and "\n" not in error_line
    assert expected in error_line
    assert dsn not in captured.err and "s3cr3t-pw" not in captured.err


def test_a_malformed_database_url_exits_2_without_echoing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The real _connect_catalog: libpq rejects the URI while parsing it and quotes it
    # whole in the error, password included. No database is reached.
    dsn = "postgresql://audit_user:s3cr3t-pw@[::1/nhms"
    monkeypatch.setenv("DATABASE_URL", dsn)

    assert main(["--manifest", str(_manifest(tmp_path, ["b_a"]))]) == EXIT_CONFIG_ERROR

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ProgrammingError" in captured.err and "\n" not in captured.err.strip()
    assert "s3cr3t-pw" not in captured.err and dsn not in captured.err


def test_the_manifest_is_read_once_so_a_rewrite_while_connecting_cannot_crash_the_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The scheduler may rewrite manifest-last.json between the pre-connect check and the audit."""
    import scripts.basin_catalog_manifest_audit as audit

    manifest = _manifest(tmp_path, ["b_a"])

    def connect_while_the_manifest_is_rewritten(database_url: str) -> Any:
        manifest.write_text("{half-written", encoding="utf-8")
        return _Catalog(["b_a", "b_retired"], ["b_a"]), _active({"b_a": 1})

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@127.0.0.1:1/none")
    monkeypatch.setattr(audit, "_connect_catalog", connect_while_the_manifest_is_rewritten)

    assert main(["--manifest", str(manifest)]) == EXIT_PASS

    receipt = json.loads(capsys.readouterr().out)
    assert (receipt["verdict"], receipt["manifest_count"]) == ("pass", 1)
    assert receipt["catalog_extras"] == [{"basin_id": "b_retired", "active_models": 0}]
