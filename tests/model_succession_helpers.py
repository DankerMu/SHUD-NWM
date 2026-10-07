"""Fixtures, a fake ``systemctl`` and builders shared by the model succession suites (#2739, #2740, #2756, #2757).

The production layout in ``tmp_path``: a compute store (the worker mirror, the
compute-side state index, the state objects and the packages the scheduler
runs) and a shared store (the canonical manifest, the canonical state index,
the receipts and the provisioned packages).  The clone tool and the publish
tool are the real ones; the packages are the recalibration clone suites' fake
packages with the manifest inside the package directory, as the provision step
writes them.  A suite that uses the ``space`` fixture imports it and the
autouse ``no_database`` fixture from here.  ``build_space`` takes the kind and
what the new packages hold, for the cold-start suite, and the sources of the
canonical manifest and a basin to add, for the add-basin suite, and a basin to
remove, for the remove-basin suite; its defaults are the recalibration space.

The fake ``systemctl`` is a script: it appends every call, ``--user`` included,
to a trace, keeps unit states in a file and, for a start of the provider
refresh service, writes a ``latest.json`` built by the refresh's own
``_receipt``.  Like the real unit, it skips the refresh without an error while
the scheduler service is active.
"""

from __future__ import annotations

import dataclasses
import json
import os
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import scripts.model_succession.scheduler as scheduler_steps
import scripts.node22_model_succession as tool
from packages.common.object_store import LocalObjectStore, sha256_bytes
from packages.common.state_manager import publish_state_snapshot_index, state_snapshot_id
from scripts.model_succession import plan as planning
from scripts.model_succession import run as succession_run
from scripts.model_succession.model import Inputs, Settings
from scripts.scheduler_refresh.receipt import _receipt as refresh_receipt
from tests.merged_registry_publish_helpers import (
    PREFIX,
    Clock,
    Workspace,
    _cli_environment,
    no_database,  # noqa: F401 - the autouse fixture the suites import from here
)
from tests.provider_mode_helpers import make_directory_with_explicit_mode
from tests.state_clone_recalibration_fixtures import (
    _CALIB_TABLE_V1,
    _CALIB_TABLE_V2,
    _CALIB_V1,
    _CALIB_V2,
    _IC_V1,
    _PARA_V1,
    _PARA_V2,
    _write_package,
)
from workers.data_adapters.base import cycle_id_for

REPO_ROOT = Path(__file__).resolve().parents[1]
TIMER = "nhms-compute-scheduler.timer"
SERVICE = "nhms-compute-scheduler.service"
REFRESH = "nhms-scheduler-file-provider-refresh.service"
SUCCESSION_ID = "recal-2026100512"
CUTOVER = "2026100512"
CUTOVER_TIME = datetime(2026, 10, 5, 12, tzinfo=UTC)
SOURCE = "gfs"
STEPS = ("copyback", "preflight", "begin", "clone", "publish", "refresh", "finish")
COLD_START_STEPS = ("copyback", "preflight", "begin", "publish", "refresh", "finish")
# A packaged initial condition the first-cycle audit qualifies: the header line has three numeric tokens.
QUALIFIED_IC = b"4\t1\t27000000.000000\n0.1\t0.2\n0.3\t0.4\n"
# What the new packages of a cold start hold: another mesh, a qualified IC, and the row names the SHUD input.
COLD_START_PACKAGE: dict[str, Any] = {
    "ic": QUALIFIED_IC,
    "core_overrides": {"huai.sp.mesh": b"mesh-topology-v2\n"},
    "shud_input_name": "huai",
}
# What the packages of an added basin hold: a qualified IC, and the row names the SHUD input.
ADD_BASIN_PACKAGE: dict[str, Any] = {"ic": QUALIFIED_IC, "shud_input_name": "huai"}

_FAKE_SYSTEMCTL = r'''
import json, os, sys
from datetime import UTC, datetime, timedelta

here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "state.json"), encoding="utf-8") as handle:
    state = json.load(handle)
with open(os.path.join(here, "config.json"), encoding="utf-8") as handle:
    config = json.load(handle)
line = " ".join(sys.argv[1:])
with open(os.path.join(here, "trace"), "a", encoding="utf-8") as trace:
    trace.write(line + "\n")
args = [arg for arg in sys.argv[1:] if arg != "--user"]
verb, unit = args[0], args[-1]
key = " ".join(args)
counts = state.setdefault("counts", {})
counts[key] = counts.get(key, 0) + 1
units = state["units"]
rc = 0
if key in config.get("fail", []):
    rc = 1
    print(f"fake systemctl: {key}: injected failure", file=sys.stderr)
elif verb == "is-active":
    queue = state.setdefault("scripted", {}).get(unit) or []
    if queue:
        units[unit] = queue.pop(0)
    value = config.get("print", {}).get(unit, units.get(unit, "inactive"))
    if value:
        print(value)
    rc = 0 if value == "active" else 3
elif verb == "stop":
    units[unit] = "inactive"
elif verb == "start" and unit == config["refresh_unit"]:
    refresh = config.get("refresh", {})
    rc = int(refresh.get("rc", 0))
    if refresh.get("hang_seconds"):
        import time

        time.sleep(float(refresh["hang_seconds"]))
    # The unit's start condition: skipped without an error while the scheduler service is active.
    if rc == 0 and units.get(config["service_unit"]) != "active" and refresh.get("mode", "run") == "run":
        sys.path.insert(0, config["repo_root"])
        from scripts.scheduler_refresh.receipt import _receipt

        classification = json.loads(json.dumps(config["classification"]))
        for name, total in refresh.get("totals", {}).items():
            classification[name]["total"] = total
        started = datetime.now(UTC) - timedelta(seconds=float(refresh.get("age_seconds", 0)))
        receipt = _receipt(
            run_id=f"refresh_{counts[key]}",
            started=started,
            outcome=refresh.get("outcome", "published"),
            reason=refresh.get("reason", "success"),
            phase="complete",
            providers=config["providers"],
            registry_classification=None if refresh.get("drop_classification") else classification,
            cutover_gate=config["cutover_gate"],
        )
        with open(os.path.join(config["refresh_receipt_root"], "latest.json"), "w", encoding="utf-8") as handle:
            json.dump(receipt, handle)
elif verb == "start":
    units[unit] = "active"
else:
    rc = 2
for name, value in config.get("after", {}).get(key, {}).items():
    units[name] = value
with open(os.path.join(here, "state.json"), "w", encoding="utf-8") as handle:
    json.dump(state, handle)
sys.exit(rc)
'''


def classification(model_ids: list[str]) -> dict[str, Any]:
    """The ``registry_classification`` of a pure renewal of these rows, as the refresh records it."""

    def group(items: list[Any]) -> dict[str, Any]:
        return {"items": items, "total": len(items), "truncated": False}

    return {
        "previous_registry_sha256": "a" * 64,
        "new_registry_sha256": "a" * 64,
        "previous_model_count": len(model_ids),
        "prospective_model_count": len(model_ids),
        "added": group([]),
        "unchanged": group(model_ids),
        "removed": group([]),
        "package_changed": group([]),
        "refused": group([]),
        "declared_cutovers": group([]),
    }


def _provider(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "before_sha256": "1" * 64,
        "before_inode": None,
        "before_schema_version": "v1",
        "before_generated_at": "2026-10-05T00:00:00Z",
        "before_payload_checksum": "sha256:" + "b" * 64,
        "after_sha256": "c" * 64,
        "after_schema_version": "v1",
        "after_generated_at": "2026-10-05T01:00:00Z",
        "after_payload_checksum": "sha256:" + "d" * 64,
        "entry_count": 1,
    }


PROVIDERS = [_provider(name) for name in ("registry", "readiness", "state")]
CUTOVER_GATE = {
    "mode": "enforced",
    "declaration_env": "NHMS_REGISTRY_CUTOVER_DECLARATION_PATH",
    "declaration_present": False,
}


@dataclasses.dataclass
class Systemctl:
    """The fake ``systemctl``: its trace, its unit states and the knobs of one test."""

    root: Path

    @property
    def binary(self) -> Path:
        return self.root / "systemctl"

    def _read(self, name: str) -> dict[str, Any]:
        return json.loads((self.root / name).read_text(encoding="utf-8"))

    def _write(self, name: str, payload: dict[str, Any]) -> None:
        (self.root / name).write_text(json.dumps(payload), encoding="utf-8")

    def configure(self, **knobs: Any) -> None:
        self._write("config.json", {**self._read("config.json"), **knobs})

    def set_units(self, **states: str) -> None:
        state = self._read("state.json")
        state["units"].update({{"timer": TIMER, "service": SERVICE}[name]: value for name, value in states.items()})
        self._write("state.json", state)

    def script(self, unit: str, states: list[str]) -> None:
        """The states the next ``is-active`` queries of ``unit`` find, one per query; the last one stays."""

        state = self._read("state.json")
        state.setdefault("scripted", {})[unit] = states
        self._write("state.json", state)

    def state(self, unit: str) -> str:
        return str(self._read("state.json")["units"][unit])

    def calls(self) -> list[str]:
        trace = self.root / "trace"
        return trace.read_text(encoding="utf-8").splitlines() if trace.exists() else []

    def mutating(self) -> list[str]:
        """Every recorded call that is not an ``is-active`` query, in order."""

        return [call for call in self.calls() if not call.startswith("--user is-active ")]

    def clear(self) -> None:
        (self.root / "trace").unlink(missing_ok=True)


@dataclasses.dataclass
class Space:
    ws: Workspace
    systemctl: Systemctl
    canonical_index: Path
    mirror_index: Path
    refresh_receipt_root: Path
    old_rows: list[dict[str, Any]]
    new_rows: list[dict[str, Any]]
    registry: Path | None  # None when nothing was provisioned: a remove-basin space
    kind: str = "recalibration"
    removes: list[str] = dataclasses.field(default_factory=list)  # the ids a remove-basin space names

    @property
    def directory(self) -> Path:
        return self.ws.receipt_root / SUCCESSION_ID

    @property
    def pairs(self) -> list[tuple[str, str]]:
        if not self.new_rows:
            return []
        return [(old["model_id"], new["model_id"]) for old, new in zip(self.old_rows, self.new_rows, strict=True)]

    def argv(
        self,
        *extra: str,
        pairs: list[tuple[str, str]] | None = None,
        cutover: str | None = CUTOVER,
        adds: list[str] | None = None,
        removes: list[str] | None = None,
    ) -> list[str]:
        """The command line of this space's kind; ``cutover=None`` leaves ``--cutover-time`` out.

        An ``add_basin`` space names its new rows with ``--add`` and gives neither ``--pair`` nor
        ``--cutover-time``; ``adds`` replaces the ids it names.  A ``remove_basin`` space names the rows of
        its removed basin with ``--remove`` and gives none of ``--pair``, ``--add`` and ``--cutover-time``;
        ``removes`` replaces the ids it names.
        """

        arguments = ["--succession-id", SUCCESSION_ID, "--kind", self.kind, "--operator-id", "operator-1"]
        if self.kind == "remove_basin":
            for model_id in self.removes if removes is None else removes:
                arguments += ["--remove", model_id]
            return [*arguments, "--pass-wait-seconds", "30", *extra]
        if self.kind == "add_basin":
            for model_id in [str(row["model_id"]) for row in self.new_rows] if adds is None else adds:
                arguments += ["--add", model_id]
            return [*arguments, "--pass-wait-seconds", "30", *extra]
        for old, new in self.pairs if pairs is None else pairs:
            arguments += ["--pair", f"{old}:{new}"]
        if cutover is not None:
            arguments += ["--cutover-time", cutover]
        return [*arguments, "--pass-wait-seconds", "30", *extra]

    def main(self, *extra: str, **overrides: Any) -> int:
        return tool.main(self.argv(*extra, **overrides))

    def main_as(self, succession_id: str, *extra: str, provisioned: bool = True) -> int:
        """The command of another succession id over the same pairs, provisioned by this space's succession.

        ``provisioned=False`` leaves ``--provision-succession-id`` out: a removal has no provision.
        """

        arguments = [succession_id if value == SUCCESSION_ID else value for value in self.argv(*extra)]
        if not provisioned:
            return tool.main(arguments)
        return tool.main([*arguments, "--provision-succession-id", SUCCESSION_ID])

    def settings(self, *extra: str) -> Settings:
        return tool.settings_from_arguments(tool._parse_args(self.argv(*extra)))

    def inputs(self, settings: Settings) -> Inputs:
        return planning.check_inputs(settings)

    def run_steps(self, *steps: str) -> tuple[Settings, Inputs]:
        """Run the named steps through the step runner, as an apply does, and return what it ran with."""

        settings = self.settings("--apply")
        inputs = self.inputs(settings)
        if not planning.compare_with_plan(settings, inputs):
            planning.write_plan(settings, inputs)
        for step in steps:
            succession_run.run_step(settings, inputs, step)
        return settings, inputs

    def receipt(self, name: str) -> dict[str, Any]:
        return json.loads((self.directory / name).read_text(encoding="utf-8"))

    def names(self) -> list[str]:
        return sorted(path.name for path in self.directory.iterdir()) if self.directory.exists() else []

    def failures(self) -> list[dict[str, Any]]:
        paths = sorted(self.directory.glob("succession-failed-*"))
        return [json.loads(path.read_text(encoding="utf-8")) for path in paths]

    def model_ids(self, path: Path) -> list[str]:
        return [str(row["model_id"]) for row in self.ws.models(path)]

    def clone_rows(self, index: Path) -> list[dict[str, Any]]:
        entries = json.loads(index.read_text(encoding="utf-8"))["entries"]
        return [entry for entry in entries if entry.get("cloned_from_model_id")]

    def package(self, row: dict[str, Any], root: Path) -> Path:
        return root / "models" / str(row["model_id"]) / "package"

    def seed_refresh_receipt(self, *, age_seconds: float = 3600.0) -> Path:
        """A ``latest.json`` of an earlier refresh, as the daily timer leaves it."""

        receipt = refresh_receipt(
            run_id="refresh_earlier",
            started=datetime.now(UTC) - timedelta(seconds=age_seconds),
            outcome="published",
            reason="success",
            phase="complete",
            providers=PROVIDERS,
            registry_classification=classification([str(row["model_id"]) for row in self.ws.rows]),
            cutover_gate=CUTOVER_GATE,
        )
        target = self.refresh_receipt_root / "latest.json"
        target.write_text(json.dumps(receipt), encoding="utf-8")
        return target


def _row(
    ws: Workspace,
    basin: str,
    version: str,
    *,
    roots: tuple[Path, ...],
    ic: bytes = _IC_V1,
    core_overrides: dict[str, bytes] | None = None,
    source: str = SOURCE,
    **profile: Any,
) -> dict[str, Any]:
    """A direct-grid registry row whose package, manifest inside, is present under ``roots``.

    ``ic`` and ``core_overrides`` are the package's ``cfg.ic`` and the core files that differ from the
    recalibration fixtures; ``source`` is the forcing source of the row; ``profile`` goes into the row's
    ``resource_profile``.
    """

    row = ws.row(basin, source, version, **profile)
    model_id = str(row["model_id"])
    new = version != "v1"
    for root in roots:
        _write_package(
            root / "models" / model_id / "package",
            model_id=model_id,
            calib=_CALIB_V2 if new else _CALIB_V1,
            calib_table=_CALIB_TABLE_V2 if new else _CALIB_TABLE_V1,
            para=_PARA_V2 if new else _PARA_V1,
            ic=ic,
            core_overrides=core_overrides,
        )
    for root in (ws.store, ws.shared):
        # ``Workspace.row`` wrote a manifest beside the package under both roots; the provision step writes it inside.
        (root / "models" / model_id / "manifest.json").unlink()
        if root not in roots:
            (root / "models" / model_id).rmdir()
    manifest = (roots[0] / "models" / model_id / "package" / "manifest.json").read_bytes()
    return {
        **row,
        "manifest_uri": f"{PREFIX}/models/{model_id}/package/manifest.json",
        "package_checksum": f"sha256:{sha256_bytes(manifest)}",
    }


def _state_entry(store: LocalObjectStore, row: dict[str, Any]) -> dict[str, Any]:
    """The qualified +12h state of an old model at the cutover time, its object on the compute store."""

    model_id = str(row["model_id"])
    content = b"2\t1\t27000000.000000\n0.1\t0.2\n0.3\t0.4\n"
    cycle_id = cycle_id_for(SOURCE, CUTOVER_TIME - timedelta(hours=12))
    created = CUTOVER_TIME.isoformat().replace("+00:00", "Z")
    return {
        "state_id": state_snapshot_id(model_id, CUTOVER_TIME, source_id=SOURCE, cycle_id=cycle_id, lead_hours=12),
        "model_id": model_id,
        "run_id": f"fcst_{SOURCE}_{cycle_id}_{model_id}",
        "source_id": SOURCE,
        "valid_time": created,
        "state_uri": store.write_bytes_atomic(f"states/{SOURCE}/{model_id}/{CUTOVER}/state.cfg.ic", content),
        "checksum": f"sha256:{sha256_bytes(content)}",
        "usable_flag": True,
        "created_at": created,
        "cycle_id": cycle_id,
        "lead_hours": 12,
        "model_package_version": row["model_package_uri"],
        "model_package_checksum": row["package_checksum"],
    }


@pytest.fixture(name="space")
def space_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Space:
    return build_space(tmp_path, monkeypatch)


def build_space(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    kind: str = "recalibration",
    new_packages: tuple[dict[str, Any], dict[str, Any]] = ({}, {}),
    old_ic: bytes = _IC_V1,
    sources: tuple[str, ...] = (SOURCE,),
    added_basin: str | None = None,
    removed_basin: str | None = None,
) -> Space:
    """The production layout with two pairs; ``new_packages`` are the ``_row`` options of the two new models
    and ``old_ic`` is the ``cfg.ic`` of the two old packages.

    ``sources`` are the sources of the canonical manifest: every basin gets one row per source, and the pairs
    stay those of the first.  With ``added_basin`` the new models are not successors: they are the rows of that
    basin, one per source, each with its ``new_packages`` options.  With ``removed_basin`` there are no new
    models and no provision: the receipt root is left empty, ``registry`` is ``None`` and the space
    names the rows of that basin, one per source, with ``--remove``.
    """

    store, shared = tmp_path / "compute-store", tmp_path / "shared-store"
    canonical = shared / "scheduler" / "registry" / "manifest-last.json"
    mirror = store / "scheduler" / "registry" / "manifest-last.json"
    canonical_index = shared / "scheduler" / "state-index" / "index-last.json"
    mirror_index = store / "scheduler" / "state-index" / "index-last.json"
    refresh_root = tmp_path / "provider-refresh"
    for directory in (canonical.parent, mirror.parent, canonical_index.parent, mirror_index.parent, refresh_root):
        make_directory_with_explicit_mode(directory)
    (refresh_root / "receipts").mkdir()
    ws = Workspace(
        root=tmp_path,
        store=store,
        shared=shared,
        canonical=canonical,
        mirror=mirror,
        refresh_lock=refresh_root / "refresh",
        rows=[],
        clock=Clock(),
    )
    both = (store, shared)
    old_rows = [_row(ws, "a", "v1", roots=both, ic=old_ic), _row(ws, "b", "v1", roots=both, ic=old_ic)]
    other_sources = [_row(ws, basin, "v1", roots=both, source=source) for source in sources[1:] for basin in "abc"]
    ws.seed([*old_rows, _row(ws, "c", "v1", roots=both), *other_sources])
    # The provision step leaves the new packages on the shared store only; the copyback step brings them over.
    registry: Path | None = None
    if removed_basin is not None:
        new_rows = []
    elif added_basin is None:
        new_rows = [
            _row(ws, basin, "v2", roots=(shared,), **options)
            for basin, options in zip(("a", "b"), new_packages, strict=True)
        ]
    else:
        new_rows = [
            _row(ws, added_basin, "v1", roots=(shared,), source=source, **options)
            for source, options in zip(sources, new_packages, strict=True)
        ]
    if removed_basin is None:
        registry = ws.provision(SUCCESSION_ID, new_rows)
    else:
        # The receipt root of production holds earlier successions; here it is there and empty.
        ws.receipt_root.mkdir(parents=True)

    entries = [_state_entry(LocalObjectStore(store, PREFIX), row) for row in old_rows]
    for index, root in ((canonical_index, shared), (mirror_index, store)):
        publish_state_snapshot_index(
            entries, index, object_store_root=root, object_store_prefix=PREFIX, verify_objects=False
        )

    fake = tmp_path / "systemctl-fake"
    fake.mkdir()
    systemctl = Systemctl(fake)
    systemctl.binary.write_text(f"#!{sys.executable}\n{_FAKE_SYSTEMCTL}", encoding="utf-8")
    systemctl.binary.chmod(systemctl.binary.stat().st_mode | stat.S_IXUSR)
    systemctl._write("state.json", {"units": {TIMER: "active", SERVICE: "inactive"}})
    systemctl._write(
        "config.json",
        {
            "repo_root": str(REPO_ROOT),
            "refresh_unit": REFRESH,
            "service_unit": SERVICE,
            "refresh_receipt_root": str(refresh_root / "receipts"),
            "classification": classification([str(row["model_id"]) for row in ws.rows]),
            "providers": PROVIDERS,
            "cutover_gate": CUTOVER_GATE,
        },
    )

    _cli_environment(monkeypatch, ws)
    monkeypatch.setenv("NHMS_SCHEDULER_STATE_INDEX", str(canonical_index))
    monkeypatch.setenv("NHMS_SCHEDULER_PROVIDER_REFRESH_RECEIPT_ROOT", str(refresh_root / "receipts"))
    monkeypatch.setenv(tool.SYSTEMCTL_ENV, str(systemctl.binary))
    # The begin step polls; the tests do not wait between two queries.
    monkeypatch.setattr(scheduler_steps, "sleep", lambda _seconds: None)
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    monkeypatch.setattr("tempfile.tempdir", str(scratch))
    return Space(
        ws=ws,
        systemctl=systemctl,
        canonical_index=canonical_index,
        mirror_index=mirror_index,
        refresh_receipt_root=refresh_root / "receipts",
        old_rows=old_rows,
        new_rows=new_rows,
        registry=registry,
        kind=kind,
        removes=[str(row["model_id"]) for row in ws.rows if row["basin_id"] == f"basins_{removed_basin}"],
    )


def tree(root: Path) -> dict[str, tuple[int, int, int]]:
    """Every path under ``root`` with its mode, mtime and size."""

    entries: dict[str, tuple[int, int, int]] = {}
    for directory, _names, files in os.walk(root):
        for path in (Path(directory), *(Path(directory) / name for name in files)):
            status = path.lstat()
            entries[str(path.relative_to(root))] = (status.st_mode, status.st_mtime_ns, status.st_size)
    return entries


def changed(before: dict[str, Any], after: dict[str, Any]) -> set[str]:
    """The paths whose entry differs, the fake ``systemctl``'s own bookkeeping aside.

    ``tmp`` is the private temporary directory itself: a scratch directory made and removed inside it
    changes its mtime, and the suites assert separately that it is left empty.
    """

    paths = {path for path in before.keys() | after.keys() if before.get(path) != after.get(path)}
    return {path for path in paths if not path.startswith("systemctl-fake") and path != "tmp"}


def report(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    """The JSON report the last command printed on stdout."""

    return json.loads(capsys.readouterr().out)


def stores(space: Space) -> dict[str, bytes]:
    """The bytes of both manifests and both state indexes."""

    paths = (space.ws.canonical, space.ws.mirror, space.canonical_index, space.mirror_index)
    return {str(path): path.read_bytes() for path in paths}
