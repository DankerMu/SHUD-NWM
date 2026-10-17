"""#2911: the read-only, manifest-driven node-22 unit drift probe.

Every case builds a fake repository under ``tmp_path`` with the real
``MANIFEST.json`` and the nineteen tracked files it names copied to the same
relative paths (``infra/systemd/node22/MANIFEST.json`` sits at depth 3, so the
probe's ``parents[3]`` is the fake repo root), deploys byte-identical copies
into a fake ``systemd/user`` directory, then mutates one side per scenario.
Expected values come from the bytes the test writes and from ``hashlib`` /
``difflib``, never from the probe's own output.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import node22_unit_drift_check as probe

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts/node22_unit_drift_check.py"
REAL_MANIFEST = REPO_ROOT / "infra/systemd/node22/MANIFEST.json"
MANIFEST_REL = "infra/systemd/node22/MANIFEST.json"

RECEIPT_KEYS = {
    "schema_version",
    "generated_at",
    "status",
    "exit_code",
    "manifest_path",
    "manifest_sha256",
    "systemd_user_dir",
    "entries",
    "untracked",
    "errors",
}
ONESHOT = "nhms-scheduler-journal-retention.service"
DROPIN = "nhms-compute-scheduler.service.d/10-slurm-gateway-token.conf"


def real_entries() -> list[dict[str, str]]:
    return json.loads(REAL_MANIFEST.read_text(encoding="utf-8"))["entries"]


class Layout:
    def __init__(self, root: Path) -> None:
        self.repo = root / "repo"
        self.systemd = root / "home/.config/systemd/user"
        self.receipts = root / "receipts"
        self.manifest = self.repo / MANIFEST_REL
        self.receipt = self.receipts / "drift.json"

    def argv(self, *, receipt: Path | None = None) -> list[str]:
        return [
            "--manifest",
            str(self.manifest),
            "--systemd-user-dir",
            str(self.systemd),
            "--receipt",
            str(receipt if receipt is not None else self.receipt),
        ]

    def read_receipt(self) -> dict:
        return json.loads(self.receipt.read_text(encoding="utf-8"))

    def write_manifest(self, document: object) -> None:
        self.manifest.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


@pytest.fixture
def layout(tmp_path: Path) -> Layout:
    lay = Layout(tmp_path)
    lay.manifest.parent.mkdir(parents=True)
    shutil.copyfile(REAL_MANIFEST, lay.manifest)
    for entry in real_entries():
        tracked = lay.repo / entry["tracked_path"]
        tracked.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / entry["tracked_path"], tracked)
        deployed = lay.systemd / entry["deployed_relative_path"]
        deployed.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / entry["tracked_path"], deployed)
    lay.receipts.mkdir()
    return lay


def snapshot(root: Path) -> dict[str, tuple]:
    """(path -> mode, mtime_ns, bytes-or-link-target) for every node under ``root``."""
    state: dict[str, tuple] = {}
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(current) / name
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                payload: object = os.readlink(path)
            elif stat.S_ISREG(info.st_mode):
                try:
                    payload = path.read_bytes()
                except PermissionError:
                    payload = "<unreadable>"
            else:
                payload = None
            state[path.relative_to(root).as_posix()] = (info.st_mode, info.st_mtime_ns, payload)
    return state


def expected_entries(overrides: dict[str, str] | None = None) -> list[dict[str, str]]:
    overrides = overrides or {}
    return [
        {
            "deployed_relative_path": entry["deployed_relative_path"],
            "tracked_path": entry["tracked_path"],
            "status": overrides.get(entry["deployed_relative_path"], "same"),
        }
        for entry in real_entries()
    ]


# --- spec scenarios ----------------------------------------------------------


def test_all_manifest_entries_deployed_identically(layout: Layout, capsys: pytest.CaptureFixture[str]) -> None:
    assert probe.main(layout.argv()) == 0

    receipt = layout.read_receipt()
    assert set(receipt) == RECEIPT_KEYS
    assert receipt["schema_version"] == "nhms.node22.unit_drift_check.v1"
    assert receipt["status"] == "ok"
    assert receipt["exit_code"] == 0
    assert receipt["entries"] == expected_entries()
    assert receipt["untracked"] == []
    assert receipt["errors"] == []
    assert receipt["manifest_sha256"] == hashlib.sha256(layout.manifest.read_bytes()).hexdigest()
    assert receipt["manifest_path"] == str(layout.manifest.resolve())
    assert receipt["systemd_user_dir"] == str(layout.systemd.resolve())
    assert receipt["generated_at"].endswith("Z")
    assert capsys.readouterr().out == ""
    assert stat.S_IMODE(layout.receipt.stat().st_mode) == 0o600
    assert layout.receipt.read_text(encoding="utf-8").endswith("}\n")


def test_a_top_level_oneshot_differs(layout: Layout, capsys: pytest.CaptureFixture[str]) -> None:
    tracked = (layout.repo / "infra/systemd" / ONESHOT).read_text(encoding="utf-8")
    deployed_text = tracked + "# hotfix applied on host\n"
    (layout.systemd / ONESHOT).write_text(deployed_text, encoding="utf-8")

    assert probe.main(layout.argv()) == 1

    receipt = layout.read_receipt()
    assert receipt["status"] == "drift"
    assert receipt["exit_code"] == 1
    assert receipt["entries"] == expected_entries({ONESHOT: "differs"})
    expected_diff = "".join(
        difflib.unified_diff(
            tracked.splitlines(keepends=True),
            deployed_text.splitlines(keepends=True),
            fromfile=f"a/infra/systemd/{ONESHOT}",
            tofile=f"b/{ONESHOT}",
        )
    )
    out = capsys.readouterr().out
    assert out == expected_diff
    assert "+# hotfix applied on host\n" in out


def test_a_missing_trailing_newline_still_prints_a_hunk(layout: Layout, capsys: pytest.CaptureFixture[str]) -> None:
    tracked = (layout.repo / "infra/systemd" / ONESHOT).read_bytes()
    assert tracked.endswith(b"\n")
    (layout.systemd / ONESHOT).write_bytes(tracked[:-1])

    assert probe.main(layout.argv()) == 1

    assert layout.read_receipt()["entries"] == expected_entries({ONESHOT: "differs"})
    last_line = tracked[:-1].decode("utf-8").rsplit("\n", 1)[-1]
    out = capsys.readouterr().out
    assert out.startswith(f"--- a/infra/systemd/{ONESHOT}\n+++ b/{ONESHOT}\n@@ ")
    assert out.endswith(f"-{last_line}\n+{last_line}\n\\ No newline at end of file\n")


def test_undecodable_byte_drift_is_never_an_empty_diff(layout: Layout, capsys: pytest.CaptureFixture[str]) -> None:
    (layout.repo / "infra/systemd" / ONESHOT).write_bytes(b"[Service]\nX=\xff\n")
    (layout.systemd / ONESHOT).write_bytes(b"[Service]\nX=\xfe\n")
    _mutate_entry(layout, _index_of(ONESHOT), sha256=hashlib.sha256(b"[Service]\nX=\xff\n").hexdigest())

    assert probe.main(layout.argv()) == 1

    assert layout.read_receipt()["entries"] == expected_entries({ONESHOT: "differs"})
    assert capsys.readouterr().out == (
        f"--- a/infra/systemd/{ONESHOT}\n+++ b/{ONESHOT}\n@@ bytes differ but are not representable as UTF-8 text @@\n"
    )


def _index_of(deployed_relative_path: str) -> int:
    paths = [entry["deployed_relative_path"] for entry in real_entries()]
    return paths.index(deployed_relative_path)


def test_an_untracked_dropin_is_deployed(layout: Layout) -> None:
    (layout.systemd / "nhms-compute-scheduler.service.d/30-hotfix.conf").write_text(
        "[Service]\nEnvironment=X=1\n", encoding="utf-8"
    )

    assert probe.main(layout.argv()) == 1

    receipt = layout.read_receipt()
    assert receipt["status"] == "drift"
    assert receipt["entries"] == expected_entries()
    assert receipt["untracked"] == ["nhms-compute-scheduler.service.d/30-hotfix.conf"]


def test_untracked_top_level_units_are_listed_sorted(layout: Layout) -> None:
    (layout.systemd / "nhms-zz-new.timer").write_text("[Timer]\n", encoding="utf-8")
    (layout.systemd / "nhms-aa-new.service").write_text("[Service]\n", encoding="utf-8")
    (layout.systemd / "other.service").write_text("[Service]\n", encoding="utf-8")

    assert probe.main(layout.argv()) == 1

    assert layout.read_receipt()["untracked"] == ["nhms-aa-new.service", "nhms-zz-new.timer"]


def test_a_manifest_entry_is_missing_on_the_host(layout: Layout) -> None:
    (layout.systemd / DROPIN).unlink()

    assert probe.main(layout.argv()) == 1

    receipt = layout.read_receipt()
    assert receipt["status"] == "drift"
    assert receipt["entries"] == expected_entries({DROPIN: "missing"})
    assert receipt["untracked"] == []


def test_backup_directories_are_ignored(layout: Layout) -> None:
    """The real leftovers from docs/governance/entropy-audit-2026-10-16.md."""
    unit_text = (layout.systemd / "nhms-slurm-gateway.service").read_text(encoding="utf-8")
    leftovers = {
        "nhms-scheduler-file-provider-refresh.service.pre-no-private-tmp-20260718T101500Z": unit_text,
        "nhms-compute-scheduler.service.d/disabled/issue-874-concurrency.conf.20260705T120000Z": "[Service]\n",
        "gateway-rollout-backup/nhms-slurm-gateway.service": unit_text,
        "gateway-rollout-backups/nhms-slurm-gateway.service.d/10-node22-live.conf": "[Service]\n",
        "issue-1684-deploy-backups/nhms-compute-scheduler.service": unit_text,
        "retired-20260630/nhms-retired-lane.service": unit_text,
        "retired-20260630/nhms-retired-lane.timer": "[Timer]\n",
        "runtime-path-recovery/nhms-compute-scheduler.service.d/20-runtime-path.conf": "[Service]\n",
    }
    for rel, text in leftovers.items():
        path = layout.systemd / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    wants = layout.systemd / "timers.target.wants"
    wants.mkdir()
    (wants / "nhms-compute-scheduler.timer").symlink_to("../nhms-compute-scheduler.timer")
    # A top-level nhms-* unit that is a link, and a linked drop-in directory:
    # both skipped by the scan because they are symlinks.
    (layout.systemd / "nhms-retired-lane.service").symlink_to("retired-20260630/nhms-retired-lane.service")
    (layout.systemd / "nhms-retired-lane.service.d").symlink_to(
        "runtime-path-recovery/nhms-compute-scheduler.service.d"
    )

    assert probe.main(layout.argv()) == 0

    receipt = layout.read_receipt()
    assert receipt["status"] == "ok"
    assert receipt["entries"] == expected_entries()
    assert receipt["untracked"] == []
    text = layout.receipt.read_text(encoding="utf-8")
    for needle in (
        "pre-no-private-tmp",
        "disabled",
        "issue-874",
        "gateway-rollout-backup",
        "issue-1684-deploy-backups",
        "retired",
        "runtime-path-recovery",
        "wants",
    ):
        assert needle not in text, needle


def test_a_manifest_entry_deployed_as_a_symlink_is_classified_by_content(layout: Layout) -> None:
    target = layout.receipts.parent / "elsewhere.service"
    target.write_bytes((layout.systemd / ONESHOT).read_bytes())
    (layout.systemd / ONESHOT).unlink()
    (layout.systemd / ONESHOT).symlink_to(target)

    assert probe.main(layout.argv()) == 0

    target.write_text("[Service]\nchanged\n", encoding="utf-8")
    assert probe.main(layout.argv()) == 1
    assert layout.read_receipt()["entries"] == expected_entries({ONESHOT: "differs"})


# --- "Probe cannot run": exit 2 --------------------------------------------------


def _mutate_entry(lay: Layout, index: int, **changes: object) -> None:
    document = json.loads(lay.manifest.read_text(encoding="utf-8"))
    entry = document["entries"][index]
    for key, value in changes.items():
        if value is None:
            entry.pop(key)
        else:
            entry[key] = value
    lay.write_manifest(document)


def _set_manifest(lay: Layout, **changes: object) -> None:
    document = json.loads(lay.manifest.read_text(encoding="utf-8"))
    document.update(changes)
    lay.write_manifest(document)


def _duplicate_entry(lay: Layout) -> None:
    document = json.loads(lay.manifest.read_text(encoding="utf-8"))
    document["entries"].append(dict(document["entries"][0]))
    lay.write_manifest(document)


def _deep_deployed_path(lay: Layout) -> None:
    _mutate_entry(lay, 2, deployed_relative_path="nhms-compute-scheduler.service.d/sub/10-slurm-gateway-token.conf")


def _tracked_outside_infra_systemd(lay: Layout) -> None:
    moved = lay.repo / "infra/env/nhms-compute-api.service"
    moved.parent.mkdir(parents=True)
    shutil.copyfile(lay.repo / "infra/systemd/node22/nhms-compute-api.service", moved)
    _mutate_entry(lay, 0, tracked_path="infra/env/nhms-compute-api.service")


def _tracked_sha_mismatch(lay: Layout) -> None:
    (lay.repo / "infra/systemd" / ONESHOT).write_text("[Service]\nedited in repo\n", encoding="utf-8")


def _deployed_unreadable(lay: Layout) -> None:
    (lay.systemd / DROPIN).chmod(0)


def _drift_and_unreadable(lay: Layout) -> None:
    (lay.systemd / ONESHOT).write_text("[Service]\ndrift\n", encoding="utf-8")
    (lay.systemd / "nhms-compute-scheduler.service.d/99-untracked.conf").write_text("[Service]\n", encoding="utf-8")
    (lay.systemd / DROPIN).chmod(0)


def _deployed_is_directory(lay: Layout) -> None:
    (lay.systemd / ONESHOT).unlink()
    (lay.systemd / ONESHOT).mkdir()


def _deployed_is_fifo(lay: Layout) -> None:
    (lay.systemd / ONESHOT).unlink()
    os.mkfifo(lay.systemd / ONESHOT)


# (id, mutation, needs-non-root, errors must mention, the one unclassifiable
# entry -- None when the manifest or directory itself fails and nothing is classified)
EXIT_2_CASES = [
    ("manifest-missing", lambda lay: lay.manifest.unlink(), False, "manifest unreadable", None),
    ("entries-empty", lambda lay: _set_manifest(lay, entries=[]), False, "entries", None),
    ("invalid-json", lambda lay: lay.manifest.write_text("{not json", encoding="utf-8"), False, "not valid JSON", None),
    ("top-level-not-object", lambda lay: lay.write_manifest([]), False, "not an object", None),
    ("wrong-schema-version", lambda lay: _set_manifest(lay, schema_version="v0"), False, "schema_version", None),
    ("entry-missing-key", lambda lay: _mutate_entry(lay, 0, sha256=None), False, "missing=['sha256']", None),
    ("entry-extra-key", lambda lay: _mutate_entry(lay, 0, mode="0644"), False, "extra=['mode']", None),
    (
        "deployed-absolute",
        lambda lay: _mutate_entry(lay, 0, deployed_relative_path="/etc/systemd/user/nhms-compute-api.service"),
        False,
        "deployed_relative_path is absolute",
        None,
    ),
    (
        "tracked-absolute",
        lambda lay: _mutate_entry(lay, 0, tracked_path="/infra/systemd/node22/nhms-compute-api.service"),
        False,
        "tracked_path is absolute",
        None,
    ),
    (
        "deployed-dotdot",
        lambda lay: _mutate_entry(lay, 0, deployed_relative_path="../nhms-compute-api.service"),
        False,
        "deployed_relative_path contains a '..' segment",
        None,
    ),
    (
        "tracked-dotdot",
        lambda lay: _mutate_entry(lay, 0, tracked_path="infra/systemd/../systemd/node22/nhms-compute-api.service"),
        False,
        "tracked_path contains a '..' segment",
        None,
    ),
    ("tracked-outside-infra-systemd", _tracked_outside_infra_systemd, False, "outside infra/systemd/", None),
    ("duplicate-deployed", _duplicate_entry, False, "duplicate deployed_relative_path", None),
    ("deployed-too-deep", _deep_deployed_path, False, "more than one directory deep", None),
    ("tracked-sha-mismatch", _tracked_sha_mismatch, False, "does not match the manifest sha256", None),
    (
        "tracked-file-missing",
        lambda lay: (lay.repo / "infra/systemd" / ONESHOT).unlink(),
        False,
        "tracked file unreadable",
        None,
    ),
    ("systemd-dir-missing", lambda lay: shutil.rmtree(lay.systemd), False, "systemd directory does not exist", None),
    ("deployed-unreadable", _deployed_unreadable, True, "deployed file unreadable", DROPIN),
    ("deployed-is-directory", _deployed_is_directory, False, "not a regular file", ONESHOT),
    ("deployed-is-fifo", _deployed_is_fifo, False, "not a regular file", ONESHOT),
    ("drift-and-unreadable", _drift_and_unreadable, True, "deployed file unreadable", DROPIN),
]


@pytest.mark.parametrize(
    ("mutate", "needs_non_root", "message", "failed_entry"),
    [case[1:] for case in EXIT_2_CASES],
    ids=[case[0] for case in EXIT_2_CASES],
)
def test_probe_cannot_run_exits_2_with_a_probe_failed_receipt(
    layout: Layout,
    capsys: pytest.CaptureFixture[str],
    mutate,
    needs_non_root: bool,
    message: str,
    failed_entry: str | None,
) -> None:
    if needs_non_root and os.geteuid() == 0:
        pytest.skip("root reads chmod 000 files")
    mutate(layout)
    tree = layout.systemd.parent
    before = snapshot(tree)
    try:
        exit_code = probe.main(layout.argv())
        after = snapshot(tree)
    finally:
        if needs_non_root:
            (layout.systemd / DROPIN).chmod(0o644)

    assert exit_code == 2
    assert after == before
    receipt = layout.read_receipt()
    assert set(receipt) == RECEIPT_KEYS
    assert receipt["status"] == "probe_failed"
    assert receipt["exit_code"] == 2
    assert any(message in error for error in receipt["errors"]), receipt["errors"]
    if failed_entry is None:
        assert receipt["entries"] == []
        assert receipt["untracked"] == []
    else:
        expected_paths = [
            entry["deployed_relative_path"]
            for entry in real_entries()
            if entry["deployed_relative_path"] != failed_entry
        ]
        assert [entry["deployed_relative_path"] for entry in receipt["entries"]] == expected_paths
        assert {entry["status"] for entry in receipt["entries"]} <= {"same", "differs", "missing"}
    assert "status: probe_failed" in capsys.readouterr().err


def test_drift_and_unreadable_together_keep_the_drift_in_the_receipt(layout: Layout) -> None:
    if os.geteuid() == 0:
        pytest.skip("root reads chmod 000 files")
    _drift_and_unreadable(layout)
    try:
        assert probe.main(layout.argv()) == 2
    finally:
        (layout.systemd / DROPIN).chmod(0o644)

    receipt = layout.read_receipt()
    expected = [entry for entry in expected_entries({ONESHOT: "differs"}) if entry["deployed_relative_path"] != DROPIN]
    assert receipt["entries"] == expected
    assert receipt["untracked"] == ["nhms-compute-scheduler.service.d/99-untracked.conf"]
    assert receipt["status"] == "probe_failed"


def test_missing_manifest_receipt_has_null_sha(layout: Layout) -> None:
    layout.manifest.unlink()

    assert probe.main(layout.argv()) == 2

    receipt = layout.read_receipt()
    assert set(receipt) == RECEIPT_KEYS
    assert receipt["manifest_sha256"] is None


@pytest.mark.parametrize("where", ["parent-missing", "under-systemd-dir", "systemd-dir-itself", "link-into-systemd"])
def test_refused_receipt_writes_nothing_anywhere(
    layout: Layout, capsys: pytest.CaptureFixture[str], tmp_path: Path, where: str
) -> None:
    if where == "parent-missing":
        receipt = layout.receipts / "absent" / "drift.json"
    elif where == "under-systemd-dir":
        receipt = layout.systemd / "drift.json"
    elif where == "systemd-dir-itself":
        receipt = layout.systemd
    else:
        (layout.receipts / "into-systemd").symlink_to(layout.systemd)
        receipt = layout.receipts / "into-systemd" / "drift.json"
    (layout.systemd / ONESHOT).write_text("[Service]\ndrift\n", encoding="utf-8")
    before = snapshot(tmp_path)

    assert probe.main(layout.argv(receipt=receipt)) == 2

    assert snapshot(tmp_path) == before
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "status: probe_failed" in captured.err


def test_unwritable_receipt_target_reports_probe_failed_on_stderr(
    layout: Layout, capsys: pytest.CaptureFixture[str]
) -> None:
    layout.receipt.mkdir()  # os.replace cannot replace a directory with a file

    assert probe.main(layout.argv()) == 2

    assert layout.receipt.is_dir()
    assert list(layout.receipts.iterdir()) == [layout.receipt]  # temp file cleaned up
    assert "status: probe_failed" in capsys.readouterr().err


def test_existing_receipt_is_replaced_atomically_in_its_own_directory(layout: Layout) -> None:
    layout.receipt.write_text("stale\n", encoding="utf-8")
    layout.receipt.chmod(0o644)
    before = snapshot(layout.systemd)

    assert probe.main(layout.argv()) == 0

    assert layout.read_receipt()["status"] == "ok"
    assert stat.S_IMODE(layout.receipt.stat().st_mode) == 0o600
    assert sorted(path.name for path in layout.receipts.iterdir()) == ["drift.json"]
    assert snapshot(layout.systemd) == before


# --- read-only proofs ------------------------------------------------------------


def test_drift_run_leaves_the_systemd_tree_untouched(layout: Layout, capsys: pytest.CaptureFixture[str]) -> None:
    secret = "SUPERSECRET-sentinel-2911"
    dropin = layout.systemd / DROPIN
    dropin.write_text(dropin.read_text(encoding="utf-8") + f"Environment=SLURM_GATEWAY_TOKEN={secret}\n")
    (layout.systemd / "nhms-compute-scheduler.service.d/30-hotfix.conf").write_text("[Service]\n", encoding="utf-8")
    (layout.systemd / ONESHOT).unlink()
    before = snapshot(layout.systemd)

    assert probe.main(layout.argv()) == 1

    assert snapshot(layout.systemd) == before
    assert secret in capsys.readouterr().out
    assert secret not in layout.receipt.read_text(encoding="utf-8")
    assert layout.read_receipt()["entries"] == expected_entries({DROPIN: "differs", ONESHOT: "missing"})


def test_runs_with_an_empty_path_and_no_home(layout: Layout) -> None:
    (layout.systemd / ONESHOT).write_text("[Service]\ndrift\n", encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(SCRIPT), *layout.argv()],
        env={"PATH": ""},
        capture_output=True,
        text=True,
        check=False,
        cwd=layout.receipts,
    )

    assert completed.returncode == 1, completed.stderr
    assert completed.stdout.startswith(f"--- a/infra/systemd/{ONESHOT}\n+++ b/{ONESHOT}\n")
    assert layout.read_receipt()["status"] == "drift"


def test_script_imports_no_subprocess_and_no_repo_package() -> None:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])

    assert "subprocess" not in imported
    assert imported.isdisjoint({"nhms", "services", "packages", "apps", "scripts"})
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}, sorted(imported - set(sys.stdlib_module_names))


# --- the real manifest and defaults -------------------------------------------------


def test_the_real_manifest_passes_the_validator() -> None:
    manifest = probe.load_manifest(REAL_MANIFEST)

    assert manifest.errors == []
    assert [entry.deployed_relative_path for entry in manifest.entries] == [
        entry["deployed_relative_path"] for entry in real_entries()
    ]
    assert manifest.sha256 == hashlib.sha256(REAL_MANIFEST.read_bytes()).hexdigest()


def test_defaults_resolve_lazily(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert probe.default_manifest() == REAL_MANIFEST

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert probe.default_systemd_user_dir() == tmp_path / "xdg/systemd/user"
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/xdg")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert probe.default_systemd_user_dir() == tmp_path / "home/.config/systemd/user"
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    assert probe.default_systemd_user_dir() == tmp_path / "home/.config/systemd/user"


def test_receipt_flag_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        probe.main([])

    assert raised.value.code == 2
    assert "--receipt" in capsys.readouterr().err
