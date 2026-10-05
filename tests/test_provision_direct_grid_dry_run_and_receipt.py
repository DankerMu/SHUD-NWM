"""Provision is a dry-run unless applied, and leaves a succession receipt (#2737).

Drives ``scripts.provision_direct_grid_scheduler_registry.main`` end to end over
the keliya mapping-builder fixture, a ``tmp_path`` object store and a recording
fake connection that stands in for ``psycopg2.connect``.  The fake commits on a
clean ``with connection:`` exit exactly as psycopg2 does, so a dry-run that
borrowed the apply path's context manager would show up as a commit.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import struct
import tempfile
from pathlib import Path
from typing import Any

import psycopg2
import pytest

import scripts.provision_direct_grid_scheduler_registry as provision
from packages.common import provision_succession_receipt as succession
from packages.common.object_store import sha256_bytes
from tests.fixtures.mapping_builder.in_memory_grid_snapshot import make_regular_grid_cells, make_snapshot

KELIYA_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "mapping_builder" / "keliya"
PREFIX = "s3://nhms-test"
BASELINE_MODEL_ID = "basins_keliya_shud"
BASELINE_PACKAGE_KEY = f"models/{BASELINE_MODEL_ID}/v1/package"
BASIN_VERSION_ID = "basin-version-keliya-v1"
SOURCE_ID = "IFS"
GRID_ID = "grid_test_v1"
STATION_COUNT = 8  # the keliya mesh uses 8 cells of the 6x6 test grid
SELECTS = ("snapshot", "cells", "baseline", "resolve", "lookup")

_STATEMENT_KINDS = (
    ("superseded_at IS NULL", "snapshot"),
    ("FROM met.canonical_grid_cell", "cells"),
    ("SELECT river_network_version_id", "baseline"),
    ("WHERE grid_snapshot_id = %s", "resolve"),
    ("resource_profile->>'canonical_grid_key'", "lookup"),
    ("INSERT INTO core.model_instance", "insert_variant"),
    ("INSERT INTO met.met_station", "mirror"),
    ("UPDATE core.model_instance", "update_variant"),
)


class FakeDatabase:
    """Committed rows shared by every connection of one test."""

    def __init__(self) -> None:
        cells = make_regular_grid_cells(lon0=100.0, lat0=36.0, lon_step=0.1, lat_step=0.1, lon_count=6, lat_count=6)
        snapshot = make_snapshot(source_id=SOURCE_ID, grid_id=GRID_ID, cells=cells, bbox_pad=0.5)
        self.snapshot_row = dataclasses.asdict(snapshot)
        self.cell_rows = [dataclasses.asdict(cell) for cell in cells]
        self.variants: dict[tuple[str, ...], str] = {}
        # A row registered by someone else between a dry-run and its apply.
        self.concurrent_model_id: str | None = None
        self.connections: list[RecordingConnection] = []

    def connect(self, _database_url: str) -> RecordingConnection:
        connection = RecordingConnection(self)
        self.connections.append(connection)
        return connection


class RecordingConnection:
    def __init__(self, database: FakeDatabase) -> None:
        self.database = database
        self.events: list[str] = []
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.pending: dict[tuple[str, ...], str] = {}

    @property
    def kinds(self) -> list[str]:
        return [kind for kind, _params in self.statements]

    def set_session(self, **options: Any) -> None:
        assert not self.statements, "set_session must precede the first statement"
        self.events.append(f"set_session:{sorted(options.items())}")

    def cursor(self, cursor_factory: Any = None) -> RecordingCursor:
        return RecordingCursor(self)

    def commit(self) -> None:
        self.database.variants.update(self.pending)
        self.pending.clear()
        self.events.append("commit")

    def rollback(self) -> None:
        self.pending.clear()
        self.events.append("rollback")

    def close(self) -> None:
        self.events.append("close")

    def __enter__(self) -> RecordingConnection:
        return self

    def __exit__(self, exc_type: Any, *_exc: Any) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()


class RecordingCursor:
    def __init__(self, connection: RecordingConnection) -> None:
        self.connection = connection
        self.rowcount = -1
        self._rows: list[dict[str, Any]] = []

    def __enter__(self) -> RecordingCursor:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        kind = next((name for marker, name in _STATEMENT_KINDS if marker in sql), None)
        assert kind is not None, f"unexpected statement: {sql}"
        self.connection.statements.append((kind, tuple(params)))
        database = self.connection.database
        self.rowcount = 1
        if kind == "snapshot":
            self._rows = [database.snapshot_row] if params == (SOURCE_ID, GRID_ID) else []
        elif kind == "cells":
            self._rows = database.cell_rows
        elif kind == "baseline":
            self._rows = [
                {
                    "river_network_version_id": "rnv-1",
                    "mesh_version_id": "mesh-1",
                    "calibration_version_id": "cal-1",
                    "shud_code_version": "shud-1",
                }
            ]
        elif kind == "resolve":
            row = database.snapshot_row
            self._rows = [{key: row[key] for key in ("grid_snapshot_id", "canonical_grid_key")}]
        elif kind == "lookup":
            known = {**database.variants, **self.connection.pending}
            model_id = database.concurrent_model_id or known.get(tuple(str(value) for value in params))
            self._rows = [{"model_id": model_id}] if model_id else []
        elif kind == "insert_variant":
            profile = params[7].adapted
            contract = profile["direct_grid_forcing"]
            identity = (contract["model_input_package_id"], contract["binding_checksum"])
            self.connection.pending[(params[1], profile["canonical_grid_key"], *identity)] = params[0]

    def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)


@dataclasses.dataclass
class Workspace:
    root: Path
    store_root: Path
    baseline_registry: Path
    output_registry: Path
    build_tmp: Path
    database: FakeDatabase

    def argv(self, *extra: str) -> list[str]:
        return [
            "--baseline-registry",
            str(self.baseline_registry),
            "--output-registry",
            str(self.output_registry),
            "--object-store-root",
            str(self.store_root),
            "--object-store-prefix",
            PREFIX,
            "--database-url",
            "postgresql://fake/none",
            "--source-grid",
            f"{SOURCE_ID}={GRID_ID}",
            "--operator-id",
            "tester",
            *extra,
        ]

    @property
    def receipt_dir(self) -> Path:
        return self.store_root / "scheduler" / "succession"

    def receipt(self, succession_id: str, mode: str) -> dict[str, Any]:
        return json.loads((self.receipt_dir / succession_id / f"provision-{mode}.json").read_text(encoding="utf-8"))

    @property
    def last(self) -> RecordingConnection:
        return self.database.connections[-1]


def _stub_baseline(root: Path) -> None:
    shutil.copytree(KELIYA_FIXTURE_DIR, root)
    (root / "build.py").unlink()
    mesh_count = (root / "keliya.sp.mesh").read_text(encoding="utf-8").split()[0]
    (root / "keliya.cfg.ic").write_text(f"{mesh_count}\t6\t29714400.000000\n", encoding="utf-8")
    (root / "domain.shp").write_bytes(struct.pack(">i", 9994) + b"\x00" * 96)
    for name in ("cfg.para", "cfg.calib", "sp.riv", "sp.rivseg", "para.soil", "para.geol", "para.lc"):
        (root / f"keliya.{name}").write_bytes(f"stub:{name}\n".encode())


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    store_root = tmp_path / "store"
    _stub_baseline(store_root / BASELINE_PACKAGE_KEY)
    baseline_registry = tmp_path / "baseline-registry.json"
    baseline_registry.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "model_id": BASELINE_MODEL_ID,
                        "basin_id": "keliya",
                        "basin_version_id": BASIN_VERSION_ID,
                        "river_network_version_id": "rnv-1",
                        "model_package_uri": f"{PREFIX}/{BASELINE_PACKAGE_KEY}/",
                        "manifest_uri": f"{PREFIX}/{BASELINE_PACKAGE_KEY}/manifest.json",
                        "package_checksum": "baseline-package-checksum",
                        "resource_profile": {"cpus": 1},
                        "display_capabilities": {},
                        "shud_code_version": "shud-1",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    build_tmp = tmp_path / "build-tmp"
    build_tmp.mkdir()
    # The default temporary-build location is ``tempfile.gettempdir()``.
    monkeypatch.setattr(tempfile, "tempdir", str(build_tmp))
    database = FakeDatabase()
    monkeypatch.setattr(psycopg2, "connect", database.connect)
    (tmp_path / "out").mkdir()
    return Workspace(
        root=tmp_path,
        store_root=store_root,
        baseline_registry=baseline_registry,
        output_registry=tmp_path / "out" / "registry.json",
        build_tmp=build_tmp,
        database=database,
    )


def _tree(root: Path) -> dict[str, tuple[int, int, int]]:
    """Every path under ``root`` with its mode, mtime and size."""

    entries: dict[str, tuple[int, int, int]] = {}
    for directory, _names, files in os.walk(root):
        for path in (Path(directory), *(Path(directory) / name for name in files)):
            stat = path.lstat()
            entries[str(path.relative_to(root))] = (stat.st_mode, stat.st_mtime_ns, stat.st_size)
    return entries


def _changed(before: dict[str, Any], after: dict[str, Any]) -> set[str]:
    return {path for path in before.keys() | after.keys() if before.get(path) != after.get(path)}


def _refused(workspace: Workspace, *extra: str) -> str:
    with pytest.raises((provision.DirectGridProvisionError, succession.SuccessionReceiptError)) as raised:
        provision.main(workspace.argv(*extra))
    return str(raised.value)


def _plan_then_apply(workspace: Workspace, succession_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    assert provision.main(workspace.argv("--succession-id", succession_id)) == 0
    assert provision.main(workspace.argv("--succession-id", succession_id, "--apply")) == 0
    return workspace.receipt(succession_id, "dry-run"), workspace.receipt(succession_id, "apply")


# --- dry-run ----------------------------------------------------------------


def test_dry_run_of_an_unbuilt_variant_issues_only_selects_and_writes_no_file(workspace: Workspace) -> None:
    before = _tree(workspace.store_root)

    assert provision.main(workspace.argv()) == 0

    connection = workspace.last
    assert connection.kinds == list(SELECTS)
    assert connection.events == ["set_session:[('readonly', True)]", "rollback", "close"]
    assert _tree(workspace.store_root) == before
    assert not workspace.output_registry.exists()
    assert list(workspace.build_tmp.iterdir()) == []
    # No --succession-id: no receipt, the plan goes to stdout only.
    assert not workspace.receipt_dir.exists()


def test_dry_run_says_first_that_nothing_is_written_and_reports_planned(
    workspace: Workspace,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = workspace.root / "reports" / "provision.json"

    provision.main(workspace.argv("--output", str(output)))

    notice, _, rendered = capsys.readouterr().out.partition("\n")
    assert notice.startswith("DRY-RUN") and "nothing is written" in notice
    assert "--apply always writes" in notice and "inserted=false" in notice
    summary = json.loads(rendered)
    assert summary == json.loads(output.read_text(encoding="utf-8"))
    assert summary["status"] == "planned"
    assert summary["registry"] is None and summary["receipt"] is None
    assert [(model["inserted"], model["station_count"]) for model in summary["models"]] == [(True, STATION_COUNT)]
    assert summary["plan"]["models"][0]["model_id"] == summary["models"][0]["model_id"]


def test_dry_run_receipt_is_the_only_change_under_the_object_store(workspace: Workspace) -> None:
    """Production layout: the receipt root is inside the object-store root."""

    before = _tree(workspace.store_root)

    provision.main(workspace.argv("--succession-id", "s-1"))

    assert _changed(before, _tree(workspace.store_root)) == {
        ".",  # the store root's mtime: ``scheduler/`` was created in it
        "scheduler",
        "scheduler/succession",
        "scheduler/succession/s-1",
        "scheduler/succession/s-1/provision-dry-run.json",
    }
    receipt_file = workspace.receipt_dir / "s-1" / "provision-dry-run.json"
    assert receipt_file.stat().st_mode & 0o777 == 0o644
    receipt = workspace.receipt("s-1", "dry-run")
    snapshot = workspace.database.snapshot_row
    package_key = receipt["models"][0]["package_key"]
    assert package_key.startswith(f"models/direct_grid_variants/{BASELINE_MODEL_ID}/dg-ifs-")
    assert package_key.endswith("/package")
    assert not (workspace.store_root / package_key).exists()
    assert receipt == {
        "schema_version": "nhms.model_succession.provision_receipt.v1",
        "succession_id": "s-1",
        "step": "provision",
        "dry_run": True,
        "outcome": "planned",
        "generated_at": receipt["generated_at"],
        "operator_id": "tester",
        "host": receipt["host"],
        "git_commit": receipt["git_commit"],
        "object_store_root": str(workspace.store_root),
        "object_store_prefix": PREFIX,
        "selected_model_ids": [BASELINE_MODEL_ID],
        "baseline_registry": {
            "path": str(workspace.baseline_registry),
            "object_store_key": None,
            "sha256": sha256_bytes(workspace.baseline_registry.read_bytes()),
        },
        "output_registry": {"path": str(workspace.output_registry), "object_store_key": None, "sha256": None},
        "source_grids": [
            {
                "source_id": SOURCE_ID,
                "grid_id": GRID_ID,
                "grid_snapshot_id": str(snapshot["grid_snapshot_id"]),
                "grid_signature": snapshot["grid_signature"],
                "canonical_grid_key": snapshot["canonical_grid_key"],
            }
        ],
        "models": [
            {
                "baseline_model_id": BASELINE_MODEL_ID,
                "model_id": receipt["models"][0]["model_id"],
                "source_id": SOURCE_ID,
                "grid_id": GRID_ID,
                "basin_version_id": BASIN_VERSION_ID,
                "package_key": package_key,
                "model_package_uri": f"{PREFIX}/{package_key}/",
                "manifest_uri": f"{PREFIX}/{package_key}/manifest.json",
                "package_checksum": receipt["models"][0]["package_checksum"],
                "inserted": True,
                "package_prebuilt": False,
                "station_count": STATION_COUNT,
            }
        ],
    }
    assert receipt["host"] and receipt["generated_at"].endswith("Z")
    assert receipt["models"][0]["model_id"].startswith("dg_")
    assert workspace.last.kinds == list(SELECTS)
    assert "commit" not in workspace.last.events


def test_dry_run_of_a_prebuilt_registered_variant_touches_nothing(workspace: Workspace) -> None:
    _planned, applied = _plan_then_apply(workspace, "s-1")
    package = workspace.store_root / applied["models"][0]["package_key"]
    # A mode the apply path's ``_make_package_readable`` would rewrite to 0644.
    (package / "manifest.json").chmod(0o600)
    before = _tree(workspace.store_root)

    provision.main(workspace.argv("--succession-id", "s-2", "--receipt-root", str(workspace.root / "receipts")))

    assert _tree(workspace.store_root) == before
    assert workspace.last.kinds == list(SELECTS)
    assert workspace.last.events == ["set_session:[('readonly', True)]", "rollback", "close"]
    assert list(workspace.build_tmp.iterdir()) == []
    receipt = json.loads((workspace.root / "receipts" / "s-2" / "provision-dry-run.json").read_text("utf-8"))
    (model,) = receipt["models"]
    assert (model["inserted"], model["package_prebuilt"], model["station_count"]) == (False, True, STATION_COUNT)
    assert model["model_id"] == applied["models"][0]["model_id"]
    assert model["package_checksum"] == applied["models"][0]["package_checksum"]


def test_dry_run_removes_the_temporary_build_when_the_build_raises(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[Path] = []

    def failing_build(*, variant_root: Path, **_kwargs: Any) -> None:
        variant_root.mkdir()
        (variant_root / "partial.bin").write_bytes(b"partial")
        built.append(variant_root)
        raise RuntimeError("build blew up")

    monkeypatch.setattr(provision, "build_direct_grid_variant", failing_build)
    before = _tree(workspace.store_root)

    with pytest.raises(RuntimeError, match="build blew up"):
        provision.main(workspace.argv())

    assert built and workspace.build_tmp in built[0].parents
    assert list(workspace.build_tmp.iterdir()) == []
    assert _tree(workspace.store_root) == before
    assert workspace.last.kinds == ["snapshot", "cells"]
    assert workspace.last.events == ["set_session:[('readonly', True)]", "rollback", "close"]


def test_build_tmp_dir_inside_the_object_store_is_refused(workspace: Workspace) -> None:
    inside = workspace.store_root / "tmp"
    inside.mkdir()
    before = _tree(workspace.store_root)

    message = _refused(workspace, "--build-tmp-dir", str(inside))

    assert str(inside) in message and "inside the object-store root" in message
    assert workspace.database.connections == []
    assert _tree(workspace.store_root) == before


def test_build_tmp_dir_option_is_where_the_temporary_build_goes(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    elsewhere = workspace.root / "elsewhere"
    elsewhere.mkdir()
    roots: list[Path] = []
    real_build = provision.build_direct_grid_variant

    def recording_build(**kwargs: Any) -> Any:
        roots.append(kwargs["variant_root"])
        return real_build(**kwargs)

    monkeypatch.setattr(provision, "build_direct_grid_variant", recording_build)

    provision.main(workspace.argv("--build-tmp-dir", str(elsewhere)))

    assert len(roots) == 1 and elsewhere in roots[0].parents
    assert list(elsewhere.iterdir()) == []


# --- the temporary-build assumption -----------------------------------------


def test_dry_run_predicts_the_model_id_and_package_checksum_an_apply_registers(workspace: Workspace) -> None:
    planned, applied = _plan_then_apply(workspace, "s-1")

    (plan,) = planned["models"]
    (done,) = applied["models"]
    assert plan["package_prebuilt"] is False
    manifest = workspace.store_root / done["package_key"] / "manifest.json"
    registered_ids = [params[0] for kind, params in workspace.last.statements if kind == "insert_variant"]
    assert registered_ids == [plan["model_id"]] == [done["model_id"]]
    assert plan["package_checksum"] == done["package_checksum"] == sha256_bytes(manifest.read_bytes())
    registry = json.loads(workspace.output_registry.read_text(encoding="utf-8"))
    assert [(row["model_id"], row["package_checksum"]) for row in registry["models"]] == [
        (plan["model_id"], plan["package_checksum"])
    ]


# --- apply ------------------------------------------------------------------

_REGISTRATION_SELECTS = ["resolve", "lookup"]


def test_apply_insert_path_pins_statements_order_and_report(
    workspace: Workspace,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = workspace.root / "provision.json"
    provision.main(workspace.argv("--succession-id", "s-1"))
    capsys.readouterr()

    provision.main(workspace.argv("--succession-id", "s-1", "--apply", "--output", str(output)))

    connection = workspace.last
    assert connection.kinds == [
        "snapshot",
        "cells",
        "baseline",
        *_REGISTRATION_SELECTS,  # the plan compared against the dry-run receipt
        *_REGISTRATION_SELECTS,  # register_direct_grid_variant's own
        "insert_variant",
        *["mirror"] * STATION_COUNT,
        "update_variant",
    ]
    # One transaction, committed by ``with connection:``; never a read-only session.
    assert connection.events == ["commit", "close"]
    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert summary == json.loads(output.read_text(encoding="utf-8"))
    assert sorted(summary) == [
        "baseline_model_count",
        "direct_grid_model_count",
        "models",
        "registry",
        "schema_version",
        "source_grids",
        "status",
    ]
    assert summary["status"] == "published" and summary["registry"]["status"] == "published"
    assert sorted(summary["models"][0]) == [
        "baseline_model_id",
        "grid_id",
        "inserted",
        "model_id",
        "source_id",
        "station_count",
    ]
    applied = workspace.receipt("s-1", "apply")
    dry_run_file = workspace.receipt_dir / "s-1" / "provision-dry-run.json"
    assert str(workspace.receipt_dir / "s-1" / "provision-apply.json") in captured.err
    assert (applied["dry_run"], applied["outcome"]) == (False, "applied")
    assert applied["output_registry"]["sha256"] == sha256_bytes(workspace.output_registry.read_bytes())
    assert applied["dry_run_receipt"] == {
        "path": str(dry_run_file),
        "object_store_key": "scheduler/succession/s-1/provision-dry-run.json",
        "sha256": sha256_bytes(dry_run_file.read_bytes()),
    }
    (model,) = applied["models"]
    assert (model["inserted"], model["package_prebuilt"], model["station_count"]) == (True, False, STATION_COUNT)
    update_params = next(params for kind, params in connection.statements if kind == "update_variant")
    assert (update_params[0], update_params[2]) == (model["model_package_uri"], model["model_id"])


def test_apply_reuse_path_still_upserts_the_mirror_and_updates_the_row(workspace: Workspace) -> None:
    _planned, first = _plan_then_apply(workspace, "s-1")

    planned, second = _plan_then_apply(workspace, "s-2")

    assert workspace.last.kinds == [
        "snapshot",
        "cells",
        "baseline",
        *_REGISTRATION_SELECTS,
        *_REGISTRATION_SELECTS,
        *["mirror"] * STATION_COUNT,
        "update_variant",
    ]
    assert workspace.last.events == ["commit", "close"]
    for receipt in (planned, second):
        (model,) = receipt["models"]
        assert (model["inserted"], model["package_prebuilt"], model["station_count"]) == (False, True, STATION_COUNT)
        assert model["model_id"] == first["models"][0]["model_id"]


# --- apply requires its dry-run ---------------------------------------------


def test_apply_without_a_dry_run_receipt_is_refused_naming_the_expected_path(workspace: Workspace) -> None:
    before = _tree(workspace.store_root)

    message = _refused(workspace, "--succession-id", "s-1", "--apply")

    assert str(workspace.receipt_dir / "s-1" / "provision-dry-run.json") in message
    assert workspace.database.connections == []
    assert _tree(workspace.store_root) == before
    assert not workspace.output_registry.exists()


def test_apply_without_a_succession_id_is_refused(workspace: Workspace) -> None:
    assert "--succession-id" in _refused(workspace, "--apply")
    assert workspace.database.connections == []


def test_apply_with_a_different_baseline_registry_is_refused(workspace: Workspace) -> None:
    provision.main(workspace.argv("--succession-id", "s-1"))
    planned_sha = sha256_bytes(workspace.baseline_registry.read_bytes())
    workspace.baseline_registry.write_text(
        workspace.baseline_registry.read_text(encoding="utf-8") + "\n", encoding="utf-8"
    )
    connections = len(workspace.database.connections)

    message = _refused(workspace, "--succession-id", "s-1", "--apply")

    assert "baseline_registry.sha256" in message
    assert str(workspace.receipt_dir / "s-1" / "provision-dry-run.json") in message
    assert planned_sha in message and sha256_bytes(workspace.baseline_registry.read_bytes()) in message
    assert len(workspace.database.connections) == connections
    assert not (workspace.receipt_dir / "s-1" / "provision-apply.json").exists()


@pytest.mark.parametrize(
    ("extra", "field"),
    [
        (("--output-registry", "elsewhere/registry.json"), "output_registry.path"),
        (("--object-store-prefix", "s3://other-bucket"), "object_store_prefix"),
    ],
)
def test_apply_with_a_different_destination_is_refused(
    workspace: Workspace,
    extra: tuple[str, ...],
    field: str,
) -> None:
    provision.main(workspace.argv("--succession-id", "s-1"))
    connections = len(workspace.database.connections)

    # argparse keeps the last occurrence of a repeated option.
    assert field in _refused(workspace, "--succession-id", "s-1", "--apply", *extra)
    assert len(workspace.database.connections) == connections


def test_apply_with_a_different_source_grid_snapshot_is_refused(workspace: Workspace) -> None:
    provision.main(workspace.argv("--succession-id", "s-1"))
    planned_snapshot_id = str(workspace.database.snapshot_row["grid_snapshot_id"])
    workspace.database.snapshot_row["grid_snapshot_id"] = "00000000-0000-0000-0000-0000000000ff"

    message = _refused(workspace, "--succession-id", "s-1", "--apply")

    assert "source_grids" in message and planned_snapshot_id in message
    assert "00000000-0000-0000-0000-0000000000ff" in message
    assert str(workspace.receipt_dir / "s-1" / "provision-dry-run.json") in message
    assert workspace.last.kinds == ["snapshot", "cells"]
    assert workspace.last.events == ["rollback", "close"]
    assert not workspace.output_registry.exists()
    assert not (workspace.receipt_dir / "s-1" / "provision-apply.json").exists()
    assert not (workspace.store_root / "models" / "direct_grid_variants").exists()


def test_apply_whose_variant_differs_from_the_prediction_rolls_back(workspace: Workspace) -> None:
    provision.main(workspace.argv("--succession-id", "s-1"))
    (plan,) = workspace.receipt("s-1", "dry-run")["models"]
    workspace.database.concurrent_model_id = "dg_registered_by_someone_else"

    message = _refused(workspace, "--succession-id", "s-1", "--apply")

    assert plan["model_id"] in message and "dg_registered_by_someone_else" in message
    assert str(workspace.receipt_dir / "s-1" / "provision-dry-run.json") in message
    assert "rolled back" in message and "stays in place" in message
    # Compared before the registration: nothing after the plan's lookup was issued.
    assert workspace.last.kinds == list(SELECTS)
    assert workspace.last.events == ["rollback", "close"]
    assert workspace.database.variants == {}
    assert not workspace.output_registry.exists()
    assert not (workspace.receipt_dir / "s-1" / "provision-apply.json").exists()
    # The package this run built before the mismatch was detected stays.
    assert (workspace.store_root / plan["package_key"] / "direct_grid_build_receipt.json").is_file()


# --- receipt ----------------------------------------------------------------


@pytest.mark.parametrize("mode", ["dry-run", "apply"])
def test_a_second_run_with_the_same_id_and_mode_fails_first_and_keeps_the_receipt(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    flags = ("--succession-id", "s-1") if mode == "dry-run" else ("--succession-id", "s-1", "--apply")
    provision.main(workspace.argv("--succession-id", "s-1"))
    if mode == "apply":
        provision.main(workspace.argv(*flags))
    receipt_file = workspace.receipt_dir / "s-1" / f"provision-{mode}.json"
    first = receipt_file.read_bytes()
    connections = len(workspace.database.connections)
    monkeypatch.setattr(provision, "build_direct_grid_variant", lambda **_kwargs: pytest.fail("built"))
    before = _tree(workspace.store_root)

    message = _refused(workspace, *flags)

    assert str(receipt_file) in message and "never overwritten" in message
    assert len(workspace.database.connections) == connections
    assert receipt_file.read_bytes() == first
    assert _tree(workspace.store_root) == before


def test_write_receipt_never_replaces_an_existing_file(tmp_path: Path) -> None:
    """The ``O_EXCL`` race guard behind the up-front existence check."""

    target = tmp_path / "s-1" / "provision-dry-run.json"
    succession.write_receipt(target, {"first": True})

    with pytest.raises(FileExistsError):
        succession.write_receipt(target, {"second": True})

    assert json.loads(target.read_text(encoding="utf-8")) == {"first": True}


def _unwritable_root(tmp_path: Path, kind: str) -> Path:
    root = tmp_path / "receipt-root"
    if kind == "file":
        root.write_text("not a directory", encoding="utf-8")
    else:
        root.mkdir()
        root.chmod(0o500)
    return root


@pytest.mark.parametrize(
    "kind",
    [
        "file",
        pytest.param("read-only", marks=pytest.mark.skipif(os.geteuid() == 0, reason="root ignores modes")),
    ],
)
def test_an_unwritable_receipt_root_is_refused_with_the_setup_message_before_anything_else(
    workspace: Workspace,
    kind: str,
) -> None:
    root = _unwritable_root(workspace.root, kind)
    mode = root.stat().st_mode
    before = _tree(workspace.store_root)

    message = _refused(workspace, "--succession-id", "s-1", "--receipt-root", str(root))

    assert str(root / "s-1") in message
    assert f"chgrp nwmuser {root}" in message and f"chmod 2775 {root}" in message and "frd_muziyao" in message
    assert workspace.database.connections == []
    assert _tree(workspace.store_root) == before
    assert list(workspace.build_tmp.iterdir()) == []
    assert root.stat().st_mode == mode


@pytest.mark.parametrize("succession_id", ["..", ".", "a/b", "x" * 81, "", "空"])
def test_a_succession_id_that_is_not_a_plain_name_is_refused(workspace: Workspace, succession_id: str) -> None:
    assert "--succession-id" in _refused(workspace, "--succession-id", succession_id)
    assert workspace.database.connections == []
    assert not workspace.receipt_dir.exists()


def test_an_apply_whose_receipt_cannot_be_written_says_what_was_written(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provision.main(workspace.argv("--succession-id", "s-1"))

    def failing_write(path: Path, _receipt: Any) -> None:
        raise OSError(28, "No space left on device", str(path))

    monkeypatch.setattr(succession, "write_receipt", failing_write)

    message = _refused(workspace, "--succession-id", "s-1", "--apply")

    assert "committed" in message and "published" in message and str(workspace.output_registry) in message
    assert "could NOT be written" in message and "new --succession-id" in message
    assert workspace.last.events == ["commit", "close"]
    assert workspace.output_registry.is_file()


# --- preserved refusals -----------------------------------------------------


def test_preserved_input_refusals(workspace: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    assert "absent from the baseline registry" in _refused(workspace, "--model-id", "no-such-model")

    payload = json.loads(workspace.baseline_registry.read_text(encoding="utf-8"))
    payload["models"][0]["resource_profile"]["direct_grid_forcing"] = {"binding_checksum": "x"}
    workspace.baseline_registry.write_text(json.dumps(payload), encoding="utf-8")
    assert "baseline rows, not direct-grid variants" in _refused(workspace)

    for name in ("DATABASE_URL", "OBJECT_STORE_ROOT", "OBJECT_STORE_PREFIX"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(provision.DirectGridProvisionError, match="DATABASE_URL, OBJECT_STORE_ROOT"):
        provision.main(["--baseline-registry", "x", "--output-registry", "y", "--operator-id", "tester"])
    assert workspace.database.connections == []
