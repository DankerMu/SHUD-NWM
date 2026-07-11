from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jsonschema
import pytest

from scripts import node27_storage_inventory_audit as audit

NOW = datetime(2026, 7, 11, 12, tzinfo=UTC)
START = datetime(2026, 5, 1, tzinfo=UTC)
END = datetime(2026, 5, 2, tzinfo=UTC)


def _subject(lane: str = "forcing", identifier: str = "forcing-a", **overrides: object) -> audit.InventorySubject:
    values: dict[str, object] = {
        "lane": lane,
        "subject_id": identifier,
        "source_id": "gfs",
        "cycle_time": START,
        "start": START,
        "end": END,
        "model_id": "model-a",
        "basin_version_id": "basin-a",
        "hot_uri": "missing",
        "checksum": "a" * 64,
    }
    if lane == "runs":
        values["hot_uri"] = json.dumps(
            {"manifest": f"runs/{identifier}/input/manifest.json", "output": f"runs/{identifier}/output/"}
        )
    if lane == "states":
        values.update(
            {
                "state_id": identifier,
                "start": START,
                "end": START,
                "hot_uri": "states/gfs/model-a/2026050100/state.cfg.ic",
            }
        )
    values.update(overrides)
    return audit.InventorySubject(**values)  # type: ignore[arg-type]


def _config(tmp_path: Path) -> audit.AuditConfig:
    object_root = tmp_path / "objects"
    archive_root = tmp_path / "archive"
    receipt = tmp_path / "receipt.json"
    object_root.mkdir()
    archive_root.mkdir()
    return audit.AuditConfig("postgresql://redacted", object_root, "s3://nhms", archive_root, 45, receipt)


def _receipt(subjects: list[audit.InventorySubject], *, product=None, salvage=(), hot=None):
    return audit.build_receipt(
        subjects,
        audit_time=NOW,
        archive_min_age_days=45,
        product_coverage=product or {},
        salvage_selectors=salvage,
        hot_coverage=hot or {},
    )


def test_verified_archive_is_complete_without_selector() -> None:
    subject = _subject()
    receipt = _receipt([subject], product={subject.stable_key: audit.Coverage("product-archive", ("verified",))})
    assert receipt["windows"][0]["verdict"] == "complete"
    assert receipt["windows"][0]["coverage"] == "product-archive"
    assert receipt["salvage_selectors"] == []


def test_aged_hot_only_is_pending_archive() -> None:
    subject = _subject()
    receipt = _receipt([subject], hot={subject.stable_key: audit.Coverage("hot-object-store")})
    assert receipt["windows"][0]["verdict"] == "pending-archive"
    assert receipt["salvage_selectors"] == []


def test_recent_hot_is_complete() -> None:
    subject = _subject(start=NOW - timedelta(days=2), end=NOW - timedelta(days=1))
    receipt = _receipt([subject], hot={subject.stable_key: audit.Coverage("hot-object-store")})
    assert receipt["windows"][0]["verdict"] == "complete"


@pytest.mark.parametrize(
    ("lane", "identifier", "table", "identity_key"),
    [
        ("forcing", "forcing-a", "met.forcing_station_timeseries", "forcing_version_id"),
        ("runs", "run-a", "hydro.river_timeseries", "run_id"),
    ],
)
def test_timeseries_gap_has_exact_selector(lane: str, identifier: str, table: str, identity_key: str) -> None:
    subject = _subject(lane, identifier)
    receipt = _receipt([subject])
    assert receipt["windows"][0]["verdict"] == "gap"
    assert receipt["salvage_selectors"] == [
        {"table": table, "identity": {identity_key: identifier}, "window": subject.window}
    ]


def test_state_gap_has_no_selector() -> None:
    receipt = _receipt([_subject("states", "state-a")])
    assert receipt["windows"][0]["verdict"] == "gap"
    assert receipt["salvage_selectors"] == []


def test_exact_salvage_covers_subject_but_near_match_does_not() -> None:
    subject = _subject()
    near = {**subject.selector, "window": {"start": audit._time(START), "end": audit._time(END + timedelta(hours=1))}}
    exact = _receipt([subject], salvage=[subject.selector])
    assert exact["windows"][0]["coverage"] == "db-export"
    near_receipt = _receipt([subject], salvage=[near])
    assert near_receipt["windows"][0]["verdict"] == "gap"


def test_equal_windows_keep_distinct_subjects() -> None:
    receipt = _receipt([_subject(identifier="a"), _subject(identifier="b")])
    assert [item["subject"] for item in receipt["windows"]] == [
        {"forcing_version_id": "a"},
        {"forcing_version_id": "b"},
    ]


@pytest.mark.parametrize("mutation", ["duplicate", "omit", "extra_selector", "wrong_bounds"])
def test_semantic_validation_rejects_invalid_set_shapes(mutation: str) -> None:
    subject = _subject()
    receipt = _receipt([subject])
    if mutation == "duplicate":
        receipt["windows"].append(receipt["windows"][0])
    elif mutation == "omit":
        receipt["windows"] = []
    elif mutation == "extra_selector":
        receipt["salvage_selectors"].append({**subject.selector, "identity": {"forcing_version_id": "other"}})
    else:
        receipt["coverage_bounds"]["end"] = audit._time(NOW)
    with pytest.raises(audit.AuditBlocked):
        audit.validate_receipt_semantics(receipt, [subject])


def test_inverted_subject_window_is_blocked() -> None:
    with pytest.raises(audit.AuditBlocked, match="inverted"):
        _subject(start=END, end=START)


def test_product_archive_checksum_mismatch_is_absent_and_reported(tmp_path: Path) -> None:
    config = _config(tmp_path)
    subject = _subject()
    paths = audit.archive_provenance_paths(config.archive_root, identity=subject.archive_identity)
    paths.archive.parent.mkdir(parents=True)
    paths.archive.write_bytes(b"bad")
    relative_archive = paths.archive.relative_to(config.archive_root).as_posix()
    relative_manifest = paths.manifest.relative_to(config.archive_root).as_posix()
    manifest = {
        "schema_version": "1.0",
        "provenance": "product-archive",
        "identity": {
            "lane": "forcing",
            "source": "gfs",
            "cycle_identity": "2026050100",
            "cycle_time": "2026-05-01T00:00:00Z",
            "basin_version_id": "basin-a",
            "model_id": "model-a",
        },
        "archive": {"path": relative_archive, "manifest_path": relative_manifest, "sha256": "0" * 64, "size_bytes": 3},
        "files": [{"path": "forcing.csv", "sha256": "1" * 64, "size_bytes": 1}],
        "created_at": "2026-07-11T00:00:00Z",
        "tool_version": "test/1",
    }
    paths.manifest.write_text(json.dumps(manifest), encoding="utf-8")
    coverage = audit.verify_product_archive(subject, config.archive_root)
    assert coverage == audit.Coverage("none", ("product archive size/sha256 mismatch",))
    receipt = _receipt([subject], product={subject.stable_key: coverage})
    assert "mismatch" in receipt["windows"][0]["evidence"][0]


def test_missing_archive_root_is_ordinary_absence(tmp_path: Path) -> None:
    assert audit.verify_product_archive(_subject(), tmp_path / "missing") is None
    assert audit.discover_salvage(tmp_path / "missing") == ()


def test_salvage_discovery_verifies_object_and_rejects_duplicate(tmp_path: Path) -> None:
    config = _config(tmp_path)
    subject = _subject()
    content = b"rows"
    object_path = config.archive_root / "db-export/forcing/a/data.csv.zst"
    object_path.parent.mkdir(parents=True)
    object_path.write_bytes(content)
    export = {
        "selector": subject.selector,
        "exported_row_count": 1,
        "columns": ["forcing_version_id"],
        "object": {
            "path": object_path.relative_to(config.archive_root).as_posix(),
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
        },
    }
    manifest = {
        "schema_version": "1.0",
        "provenance": "db-export",
        "generated_at": audit._time(NOW),
        "source_database": {"database": "nhms", "instance_id": "node27"},
        "exports": [export],
    }
    manifest_path = object_path.parent / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert audit.discover_salvage(config.archive_root) == (subject.selector,)
    second = config.archive_root / "db-export/forcing/b"
    second.mkdir(parents=True)
    (second / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(audit.AuditBlocked, match="duplicate"):
        audit.discover_salvage(config.archive_root)


def test_salvage_checksum_mismatch_is_absent_and_reported(tmp_path: Path) -> None:
    config = _config(tmp_path)
    subject = _subject()
    object_path = config.archive_root / "db-export/forcing/a/data.csv.zst"
    object_path.parent.mkdir(parents=True)
    object_path.write_bytes(b"bad")
    manifest = {
        "schema_version": "1.0",
        "provenance": "db-export",
        "generated_at": audit._time(NOW),
        "source_database": {"database": "nhms", "instance_id": "node27"},
        "exports": [
            {
                "selector": subject.selector,
                "exported_row_count": 1,
                "columns": ["forcing_version_id"],
                "object": {
                    "path": object_path.relative_to(config.archive_root).as_posix(),
                    "sha256": "0" * 64,
                    "size_bytes": 3,
                },
            }
        ],
    }
    (object_path.parent / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    mismatches: dict[str, str] = {}
    assert audit.discover_salvage(config.archive_root, mismatch_evidence=mismatches) == ()
    receipt = audit.build_receipt(
        [subject],
        audit_time=NOW,
        archive_min_age_days=45,
        product_coverage={},
        salvage_selectors=(),
        hot_coverage={},
        salvage_mismatches=mismatches,
    )
    assert "db-export object size/sha256 mismatch" in receipt["windows"][0]["evidence"]


def test_salvage_symlink_and_depth_are_blocked(tmp_path: Path) -> None:
    config = _config(tmp_path)
    base = config.archive_root / "db-export"
    base.mkdir()
    (base / "link").symlink_to(tmp_path)
    with pytest.raises(audit.AuditBlocked, match="symlink"):
        audit.discover_salvage(config.archive_root)
    (base / "link").unlink()
    current = base
    for index in range(audit.MAX_SALVAGE_DEPTH + 1):
        current = current / str(index)
        current.mkdir()
    with pytest.raises(audit.AuditBlocked, match="depth"):
        audit.discover_salvage(config.archive_root)


def test_forcing_hot_binds_manifest_and_files(tmp_path: Path) -> None:
    config = _config(tmp_path)
    key = "forcing/gfs/2026050100/basin-a/model-a"
    package = config.object_store_root / key
    package.mkdir(parents=True)
    data = b"forcing"
    data_path = package / "data.csv"
    data_path.write_bytes(data)
    manifest = {
        "forcing_version_id": "forcing-a",
        "source_id": "gfs",
        "cycle_time": audit._time(START),
        "start_time": audit._time(START),
        "end_time": audit._time(END),
        "model_id": "model-a",
        "basin_version_id": "basin-a",
        "files": [{"uri": f"s3://nhms/{key}/data.csv", "checksum": hashlib.sha256(data).hexdigest()}],
    }
    manifest_path = package / "forcing_package.json"
    raw = json.dumps(manifest).encode()
    manifest_path.write_bytes(raw)
    subject = _subject(hot_uri=f"s3://nhms/{key}", checksum=hashlib.sha256(raw).hexdigest())
    assert audit.verify_hot(subject, config).mechanism == "hot-object-store"
    bad = replace(subject, basin_version_id="other")
    with pytest.raises(audit.AuditBlocked, match="URI identity"):
        audit.verify_hot(bad, config)


def test_run_hot_requires_row_bound_manifest_and_output(tmp_path: Path) -> None:
    config = _config(tmp_path)
    subject = _subject("runs", "run-a")
    manifest_path = config.object_store_root / "runs/run-a/input/manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "run_id": "run-a",
                "source_id": "gfs",
                "cycle_time": audit._time(START),
                "start_time": audit._time(START),
                "end_time": audit._time(END),
                "model": {"model_id": "model-a", "basin_version_id": "basin-a"},
                "outputs": {
                    "run_manifest_uri": "s3://nhms/runs/run-a/input/manifest.json",
                    "output_uri": "s3://nhms/runs/run-a/output/",
                },
            }
        ),
        encoding="utf-8",
    )
    output = config.object_store_root / "runs/run-a/output/result.csv"
    output.parent.mkdir(parents=True)
    output.write_text("x", encoding="utf-8")
    assert audit.verify_hot(subject, config).mechanism == "hot-object-store"
    output.unlink()
    with pytest.raises(audit.AuditBlocked, match="no regular product"):
        audit.verify_hot(subject, config)


def test_provider_legacy_and_clone_state_identity(tmp_path: Path) -> None:
    config = _config(tmp_path)
    content = b"state"
    checksum = hashlib.sha256(content).hexdigest()
    provider = _subject("states", "provider", checksum=checksum)
    provider_path = config.object_store_root / provider.hot_uri
    provider_path.parent.mkdir(parents=True)
    provider_path.write_bytes(content)
    assert audit.verify_hot(provider, config).mechanism == "hot-object-store"
    legacy = _subject(
        "states", "legacy", source_id=None, hot_uri="states/model-a/2026050100/state.cfg.ic", checksum=checksum
    )
    legacy_path = config.object_store_root / legacy.hot_uri
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_bytes(content)
    assert legacy.archive_identity.source == "legacy-unqualified"
    assert audit.verify_hot(legacy, config).mechanism == "hot-object-store"
    clone = _subject(
        "states",
        "clone",
        model_id="model-b",
        hot_uri=provider.hot_uri,
        checksum=checksum,
        cloned_from_state_id="provider",
        cloned_from_model_id="model-a",
        clone_gate_fingerprint="f" * 64,
    )
    assert audit.verify_hot(clone, config).mechanism == "hot-object-store"
    with pytest.raises(audit.AuditBlocked, match="identity mismatch"):
        audit.verify_hot(replace(clone, cloned_from_model_id="model-c"), config)


def test_state_provider_path_preserves_canonical_source_case(tmp_path: Path) -> None:
    config = _config(tmp_path)
    content = b"state"
    checksum = hashlib.sha256(content).hexdigest()
    subject = _subject(
        "states",
        "era5-state",
        source_id="ERA5",
        hot_uri="states/ERA5/model-a/2026050100/state.cfg.ic",
        checksum=checksum,
    )
    path = config.object_store_root / subject.hot_uri
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    assert audit.verify_hot(subject, config).mechanism == "hot-object-store"


class _Cursor:
    def __init__(self, result_sets: list[list[dict[str, object]]]):
        self.result_sets = iter(result_sets)
        self.rows: list[dict[str, object]] = []
        self.executed: list[str] = []
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql: str):
        self.executed.append(sql)
        if sql.lstrip().startswith("SELECT"):
            self.rows = next(self.result_sets)

    def fetchone(self):
        return self.rows[0]

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, sets):
        self.cursor_value = _Cursor(sets)
        self.rolled_back = False

    def cursor(self):
        return self.cursor_value

    def rollback(self):
        self.rolled_back = True


def test_inventory_transaction_filters_zero_detail_by_identity_lateral_presence() -> None:
    forcing = {
        "forcing_version_id": "forcing-a",
        "model_id": "model-a",
        "source_id": "gfs",
        "cycle_time": START,
        "start_time": START,
        "end_time": END,
        "forcing_package_uri": "missing",
        "checksum": "a" * 64,
        "basin_version_id": "basin-a",
    }
    connection = _Connection([[{"audit_time": NOW}], [forcing], [], []])
    captured, subjects = audit.load_inventory(connection)
    assert captured == NOW and len(subjects) == 1 and connection.rolled_back
    sql = "\n".join(connection.cursor_value.executed)
    assert "REPEATABLE READ READ ONLY" in sql
    assert "20000ms" in sql
    assert "CROSS JOIN LATERAL" in audit.FORCING_INVENTORY_SQL
    assert "LIMIT 1" in audit.FORCING_INVENTORY_SQL


def test_empty_inventory_and_partial_clone_provenance_are_blocked() -> None:
    with pytest.raises(audit.AuditBlocked, match="empty"):
        audit.load_inventory(_Connection([[{"audit_time": NOW}], [], [], []]))
    state = {
        "state_id": "s",
        "model_id": "m",
        "run_id": "r",
        "source_id": "gfs",
        "valid_time": START,
        "state_uri": "states/gfs/m/2026050100/state.cfg.ic",
        "checksum": "a" * 64,
        "cloned_from_state_id": "x",
        "cloned_from_model_id": None,
        "clone_gate_fingerprint": None,
    }
    with pytest.raises(audit.AuditBlocked, match="incomplete clone"):
        audit.load_inventory(_Connection([[{"audit_time": NOW}], [], [], [state]]))


def test_publish_is_mode_0600_atomic_and_preserves_old_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "receipt.json"
    path.write_bytes(b"old")
    receipt = _receipt([_subject()])
    audit.publish_receipt(path, receipt)
    assert json.loads(path.read_text()) == receipt
    assert path.stat().st_mode & 0o777 == 0o600
    before = path.read_bytes()
    monkeypatch.setattr(os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError, match="boom"):
        audit.publish_receipt(path, receipt)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".*.tmp"))


def test_publish_rejects_relative_or_symlinked_paths(tmp_path: Path) -> None:
    receipt = _receipt([_subject()])
    with pytest.raises(audit.AuditBlocked, match="absolute"):
        audit.publish_receipt(Path("receipt.json"), receipt)
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(audit.AuditBlocked, match="symlink"):
        audit.publish_receipt(linked / "receipt.json", receipt)


def test_main_failure_is_json_stderr_and_does_not_print_dsn(capsys: pytest.CaptureFixture[str]) -> None:
    assert audit.main([]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.err)["status"] == "blocked"
    assert "postgresql" not in captured.err


def test_main_redacts_dsn_from_runtime_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dsn = "postgresql://user:secret@db/nhms"
    config = _config(tmp_path)
    config = replace(config, database_url=dsn)
    monkeypatch.setattr(audit, "config_from_args", lambda _args: config)
    monkeypatch.setattr(audit, "run_audit", lambda _config: (_ for _ in ()).throw(RuntimeError(f"failed {dsn}")))
    assert audit.main([]) == 1
    captured = capsys.readouterr()
    assert dsn not in captured.err and "[DATABASE_URL]" in captured.err


def test_symlinked_object_root_blocks_without_path_walk_loop(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    target = real / "states/gfs/model-a/2026050100/state.cfg.ic"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"state")
    with pytest.raises(audit.AuditBlocked, match="symlink path component"):
        audit._contained_file(linked / "states/gfs/model-a/2026050100/state.cfg.ic", linked)


def test_pinned_example_passes_schema_and_runtime_invariants() -> None:
    example = json.loads((Path("schemas/examples/archive_completeness_receipt.example.json")).read_text())
    schema = json.loads(Path("schemas/archive_completeness_receipt.schema.json").read_text())
    jsonschema.Draft7Validator(schema, format_checker=jsonschema.FormatChecker()).validate(example)
    audit.validate_receipt_semantics(example)


def test_sql_has_only_one_identity_leading_presence_probe() -> None:
    cases = (
        (audit.FORCING_INVENTORY_SQL, "forcing_version_id", ") fst_presence"),
        (audit.RUN_INVENTORY_SQL, "run_id", ") rt_presence"),
    )
    for sql, identity, presence_alias in cases:
        assert sql.count(f"x.{identity} =") == 1
        assert sql.count("\n  LIMIT 1\n") == 1
        assert sql.count("CROSS JOIN LATERAL (") == 1
        presence_probe = sql.split("CROSS JOIN LATERAL (", maxsplit=1)[1].split(presence_alias, maxsplit=1)[0]
        assert f"x.{identity} =" in presence_probe
        assert "LIMIT 1" in presence_probe
        assert "ORDER BY" not in presence_probe
        assert "valid_time" not in presence_probe
        assert "detail_min" not in sql and "detail_max" not in sql
        assert "EXISTS" not in sql.upper()
        assert "before_window" not in sql and "after_window" not in sql and "identity_drift" not in sql
        assert "ORDER BY x.valid_time" not in sql
        assert "MIN(" not in sql.upper() and "MAX(" not in sql.upper()
        assert "GROUP BY" not in sql.upper()


def test_constants_are_fixed() -> None:
    assert audit.STATEMENT_TIMEOUT_MS == 20_000
    assert audit.MAX_MANIFEST_BYTES == 16 * 1024 * 1024
    assert audit.MAX_SALVAGE_MANIFESTS == 10_000
    assert audit.MAX_SALVAGE_DEPTH == 8
    assert audit.MAX_SUBJECTS == 100_000
    assert "LIMIT 100001" in audit.FORCING_INVENTORY_SQL
    assert "LIMIT 100001" in audit.RUN_INVENTORY_SQL
    assert "LIMIT 100001" in audit.STATE_INVENTORY_SQL


def test_audit_root_preflight_rejects_symlink_object_root(tmp_path: Path) -> None:
    config = _config(tmp_path)
    real = tmp_path / "real-objects"
    real.mkdir()
    config.object_store_root.rmdir()
    config.object_store_root.symlink_to(real, target_is_directory=True)
    with pytest.raises(audit.AuditBlocked, match="symlink"):
        audit._validate_audit_roots(config)
