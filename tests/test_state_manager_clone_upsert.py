from __future__ import annotations

import inspect
from datetime import UTC, datetime
from pathlib import Path

from packages.common.object_store import LocalObjectStore, sha256_bytes
from packages.common.state_manager import (
    FileStateSnapshotIndexRepository,
    PsycopgStateSnapshotRepository,
    StateSnapshot,
)


def test_clone_upsert_retry_preserves_usable_flag() -> None:
    source = inspect.getsource(PsycopgStateSnapshotRepository.upsert_state_snapshot)
    assert "usable_flag = EXCLUDED.usable_flag" in source
    assert "usable_flag = false" not in source


def test_file_index_satisfies_state_clone_lookup_protocol(tmp_path: Path) -> None:
    object_root = tmp_path / "objects"
    store = LocalObjectStore(object_root, "s3://nhms")
    index_path = object_root / "scheduler" / "state-index.json"
    content = b"2\t1\t27000000.0\n1\t0.1\t0.1\t0.1\t0.1\t0.1\n2\t0.1\t0.1\t0.1\t0.1\t0.1\n1\t0.5\n"
    state_uri = store.write_bytes_atomic("states/gfs/model_a/2026070600/state.cfg.ic", content)
    repository = FileStateSnapshotIndexRepository(
        str(index_path),
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        now=datetime(2026, 7, 6, 1, tzinfo=UTC),
        create_missing=True,
    )
    snapshot = StateSnapshot(
        state_id="state_gfs_model_a_2026070600",
        model_id="model_a",
        run_id="fcst_gfs_2026070512_model_a",
        valid_time=datetime(2026, 7, 6, tzinfo=UTC),
        state_uri=state_uri,
        checksum=f"sha256:{sha256_bytes(content)}",
        usable_flag=True,
        source_id="gfs",
        cycle_id="gfs_2026070512",
        lead_hours=12,
        model_package_version="s3://nhms/models/model_a/package/",
        model_package_checksum="package-sha",
    )
    repository.upsert_state_snapshot(snapshot)

    assert repository.get_state_snapshot_by_model_time(
        model_id="model_a",
        source_id="gfs",
        valid_time=snapshot.valid_time,
        lead_hours=12,
    ) == snapshot
    assert repository.get_state_snapshot_by_model_time(
        model_id="model_a",
        source_id="gfs",
        valid_time=snapshot.valid_time,
        lead_hours=6,
    ) is None
    assert repository.get_latest_state_before(
        model_id="model_a",
        source_id="gfs",
        before_time=datetime(2026, 7, 6, 12, tzinfo=UTC),
    ) == snapshot


def _upsert_sql_fragments(source: str) -> tuple[str, str, str, str]:
    """Split the ``upsert_state_snapshot`` SQL body into its four column-bearing parts.

    Returns ``(insert_column_list, values_clause, do_update_set, params_tuple)``
    sliced out of the module source text, so the check reads the SQL the
    repository actually emits rather than a copy maintained by the test.
    """

    # Anchor on the SQL itself: `state_manager.py` also declares the method on
    # a Protocol and on the file-index repository, neither of which emits SQL.
    start = source.index("INSERT INTO hydro.state_snapshot")
    end = source.index("return _snapshot_from_row(row)", start)
    body = source[start:end]
    insert_columns = body[
        body.index("INSERT INTO hydro.state_snapshot") : body.index("VALUES (")
    ]
    values_clause = body[body.index("VALUES (") : body.index("ON CONFLICT")]
    do_update = body[body.index("DO UPDATE SET") : body.index("RETURNING *")]
    params = body[body.index("RETURNING *") :]
    return insert_columns, values_clause, do_update, params


def test_state_snapshot_upsert_sql_names_clone_gate_kind_with_matching_arity() -> None:
    """The ``hydro.state_snapshot`` upsert SQL carries ``clone_gate_kind`` consistently.

    ``state_manager.py::PsycopgStateSnapshotRepository.upsert_state_snapshot`` is
    the single copy of the INSERT ... ON CONFLICT statement (#3189 deleted the
    second copy with the activation-time clone path; ADR 0005). It must name
    ``clone_gate_kind`` exactly once in the INSERT column list, once in the
    ``DO UPDATE SET`` list and once in the parameter tuple, with a ``VALUES``
    placeholder count matching the INSERT column count.
    """

    source = Path("packages/common/state_manager.py").read_text(encoding="utf-8")
    insert_columns, values_clause, do_update, params = _upsert_sql_fragments(source)
    assert insert_columns.count("clone_gate_kind") == 1
    assert do_update.count("clone_gate_kind = EXCLUDED.clone_gate_kind") == 1
    assert params.count("snapshot.clone_gate_kind") == 1

    # VALUES arity must match the INSERT column count: a column added without
    # its placeholder is a runtime SQL error, and a placeholder added without
    # its parameter shifts every later value.
    columns = [
        line.strip().rstrip(",")
        for line in insert_columns.splitlines()
        if line.strip()
        and not line.strip().startswith("INSERT INTO")
        and line.strip() not in ("(", ")")
    ]
    assert columns[-1] == "clone_gate_kind"
    assert values_clause.count("%s") == len(columns), len(columns)
    # And the params tuple carries one `snapshot.<attr>` per column.
    assert params.count("snapshot.") == len(columns), len(columns)
