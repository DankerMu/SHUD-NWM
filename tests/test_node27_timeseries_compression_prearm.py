"""Invariant tests for the issue #1088 committed pre-arm reset."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts import node27_timeseries_compression_prearm as prearm

UNIT = "nhms-node27-timeseries-compression-replay.service"
RECEIPT_BODY = b'{"schema_version": 1, "stage": "stale"}\n'
RECEIPT_SHA256 = hashlib.sha256(RECEIPT_BODY).hexdigest()


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


def _fake_systemctl(tmp_path: Path, state: str, *, name: str = "systemctl") -> Path:
    """A fake ``systemctl`` mirroring REAL ``is-active`` semantics.

    Real systemd prints the state and exits 0 ONLY for ``active``; every other
    state (``inactive``, ``failed``, ``activating``) prints the state text and
    exits 3.  Copying the always-exit-0 shape of other fakes in this repo would
    make the ``activating`` refusal test vacuous.
    """

    script = tmp_path / name
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"state = {state!r}\n"
        "args = [arg for arg in sys.argv[1:] if arg != '--user']\n"
        "if args[:1] != ['is-active']:\n"
        "    sys.stderr.write('unsupported systemctl invocation\\n')\n"
        "    raise SystemExit(2)\n"
        "print(state)\n"
        "raise SystemExit(0 if state == 'active' else 3)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _env_file(
    tmp_path: Path,
    *,
    expected_stale: str = RECEIPT_SHA256,
    extra_lines: tuple[str, ...] = (),
    mode: int = 0o600,
    name: str = "replay.env",
) -> Path:
    path = tmp_path / name
    lines = [
        "# replay env",
        "DATABASE_URL=postgresql://nwm:secret@127.0.0.1:55432/nhms",
        f"{prearm.EXPECTED_STALE_ENV_KEY}={expected_stale}",
        "NODE27_COMPRESSION_RUN_PLAN_SHA256=" + "b" * 64,
        *extra_lines,
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(mode)
    return path


def _run_plan_document(*, schema_dump: Path, capture_output: Path, workdir: Path) -> dict[str, Any]:
    """Plan shaped like the plan author's output for the fields the tool reads."""

    return {
        "plan_version": 1,
        "run_plan_id": "test-run-plan",
        "commands": [
            {
                "command_id": "cmd-dump",
                "kind": "pg-dump",
                "argv": ["/usr/bin/pg_dump", "--schema-only"],
                "artifact_associations": {"schema_dump": str(schema_dump)},
            },
            {
                "command_id": "cmd-decompress",
                "kind": "decompress",
                "argv": ["/usr/bin/true"],
                "artifact_associations": {
                    "recovery_receipt": str(workdir / "recovery-receipt.json"),
                    # Whitelisted members stay in place even when the plan names
                    # them: the receipt is the supervisor's expected-stale anchor.
                    "stale_receipt": str(workdir / prearm.RECEIPT_NAME),
                },
            },
        ],
        "captures": [
            {
                "capture_id": "cap-preflight",
                "kind": "preflight_activity",
                "argv": ["/usr/bin/true"],
                "output_path": str(capture_output),
            }
        ],
    }


def _seed_workdir(tmp_path: Path, *, with_receipt: bool = True) -> Path:
    workdir = tmp_path / "replay"
    workdir.mkdir()
    (workdir / prearm.RUN_PLAN_NAME).write_text(
        json.dumps({"commands": [], "captures": []}), encoding="utf-8"
    )
    if with_receipt:
        (workdir / prearm.RECEIPT_NAME).write_bytes(RECEIPT_BODY)
    return workdir


def _snapshot(root: Path) -> dict[str, tuple[str, Any]]:
    entries: dict[str, tuple[str, Any]] = {}
    for path in sorted(root.rglob("*")):
        key = str(path.relative_to(root))
        if path.is_symlink():
            entries[key] = ("symlink", os.readlink(path))
        elif path.is_dir():
            entries[key] = ("dir", None)
        else:
            entries[key] = ("file", path.read_bytes())
    return entries


def _archives(workdir: Path) -> list[Path]:
    root = workdir / prearm.ARCHIVE_DIR_NAME
    if not root.is_dir():
        return []
    return sorted(child for child in root.iterdir() if child.is_dir())


def _sole_archive(workdir: Path) -> Path:
    archives = _archives(workdir)
    assert len(archives) == 1, archives
    return archives[0]


def _invoke(
    workdir: Path,
    env_path: Path,
    systemctl: Path,
) -> int:
    return prearm.main(
        [
            "--workdir",
            str(workdir),
            "--replay-env-path",
            str(env_path),
            "--systemctl",
            str(systemctl),
        ]
    )


def _refusal_case(
    workdir: Path,
    env_path: Path,
    systemctl: Path,
    capsys: pytest.CaptureFixture[str],
) -> str:
    """Run, assert exit 1, and assert the working directory is byte-identical."""

    before = _snapshot(workdir)
    code = _invoke(workdir, env_path, systemctl)
    captured = capsys.readouterr()
    assert code == 1, captured
    assert _snapshot(workdir) == before
    assert not (workdir / prearm.ARCHIVE_DIR_NAME).exists()
    assert captured.err.startswith("pre-arm reset refused: ")
    return captured.err


# --------------------------------------------------------------------------- #
# (a) whitelist survives / non-whitelist archived with bytes preserved
# --------------------------------------------------------------------------- #


def test_residue_is_archived_and_the_next_arm_stays_viable(tmp_path, capsys):
    workdir = _seed_workdir(tmp_path)
    evidence_root = tmp_path / "nhms-evidence"
    evidence_root.mkdir()
    schema_dump = evidence_root / "schema-before.dump"
    schema_dump.write_bytes(b"PGDMP-stale")
    plan_root = tmp_path / "plan-root"
    plan_root.mkdir()
    capture_output = plan_root / "preflight-activity.json"
    capture_output.write_bytes(b'{"activity": "stale"}')
    (workdir / prearm.RUN_PLAN_NAME).write_text(
        json.dumps(_run_plan_document(schema_dump=schema_dump, capture_output=capture_output, workdir=workdir)),
        encoding="utf-8",
    )
    (workdir / "benchmark-before.json").write_bytes(b'{"benchmark": "stale"}')
    (workdir / "compression.lock").write_bytes(b"")
    stale_dir = workdir / "checkpoints"
    stale_dir.mkdir()
    (stale_dir / "checkpoint-1.json").write_bytes(b'{"checkpoint": 1}')

    env_path = _env_file(tmp_path)
    code = _invoke(workdir, env_path, _fake_systemctl(tmp_path, "inactive"))
    out = capsys.readouterr().out
    assert code == 0

    archive = _sole_archive(workdir)

    # Whitelist survives, named explicitly.
    assert (workdir / prearm.RUN_PLAN_NAME).is_file()
    assert (workdir / prearm.RECEIPT_NAME).read_bytes() == RECEIPT_BODY

    # Non-whitelist relocated with bytes intact.
    assert not (workdir / "benchmark-before.json").exists()
    assert (archive / "benchmark-before.json").read_bytes() == b'{"benchmark": "stale"}'
    assert (archive / "checkpoints" / "checkpoint-1.json").read_bytes() == b'{"checkpoint": 1}'
    assert (archive / "compression.lock").is_file()

    # Out-of-workdir plan-owned residue relocated with bytes intact.
    assert not schema_dump.exists()
    assert not capture_output.exists()
    associations = archive / prearm.ASSOCIATIONS_DIR_NAME
    assert (associations / "schema_dump-schema-before.dump").read_bytes() == b"PGDMP-stale"
    assert (associations / "cap-preflight-preflight-activity.json").read_bytes() == b'{"activity": "stale"}'

    manifest = json.loads((archive / prearm.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["unit_state"] == "inactive"
    assert manifest["receipt_sha256"] == RECEIPT_SHA256
    assert sorted(manifest["retained"]) == [prearm.RUN_PLAN_NAME, prearm.RECEIPT_NAME]
    moved_from = {move["from"] for move in manifest["moves"]}
    assert str(schema_dump) in moved_from
    assert str(capture_output) in moved_from
    assert str(workdir / "benchmark-before.json") in moved_from
    for move in manifest["moves"]:
        assert Path(move["to"]).exists()

    assert str(archive) in out
    assert f"next: systemctl --user start {UNIT}" in out


def test_plan_named_whitelisted_member_is_exempt_from_the_association_pass(tmp_path, capsys):
    workdir = _seed_workdir(tmp_path)
    schema_dump = tmp_path / "schema-before.dump"
    schema_dump.write_bytes(b"PGDMP-stale")
    capture_output = tmp_path / "preflight-activity.json"
    capture_output.write_bytes(b"{}")
    (workdir / prearm.RUN_PLAN_NAME).write_text(
        json.dumps(_run_plan_document(schema_dump=schema_dump, capture_output=capture_output, workdir=workdir)),
        encoding="utf-8",
    )

    code = _invoke(workdir, _env_file(tmp_path), _fake_systemctl(tmp_path, "failed"))
    capsys.readouterr()
    assert code == 0

    archive = _sole_archive(workdir)
    assert (workdir / prearm.RECEIPT_NAME).read_bytes() == RECEIPT_BODY
    assert not list((archive / prearm.ASSOCIATIONS_DIR_NAME).glob(f"*{prearm.RECEIPT_NAME}"))


# --------------------------------------------------------------------------- #
# (b) named next-arm-viability assertions
# --------------------------------------------------------------------------- #


def test_finalizer_state_and_supervisor_ledger_are_swept(tmp_path, capsys):
    """Both would abort the next arm on their exclusive-create refusals.

    ``_write_finalizer_state`` raises "finalizer state path already exists" and
    ``AppendOnlyLedger`` opens with ``O_CREAT|O_EXCL``.
    """

    workdir = _seed_workdir(tmp_path)
    (workdir / "finalizer-state.json").write_bytes(b'{"finalizer": "stale"}')
    (workdir / "supervisor-ledger.jsonl").write_bytes(b'{"event_id": "stale"}\n')
    (workdir / ".finalizer-state.json.9f0c.consumed").write_bytes(b"")

    code = _invoke(workdir, _env_file(tmp_path), _fake_systemctl(tmp_path, "inactive"))
    capsys.readouterr()
    assert code == 0

    archive = _sole_archive(workdir)
    assert not (workdir / "finalizer-state.json").exists()
    assert not (workdir / "supervisor-ledger.jsonl").exists()
    assert not (workdir / ".finalizer-state.json.9f0c.consumed").exists()
    assert (archive / "finalizer-state.json").read_bytes() == b'{"finalizer": "stale"}'
    assert (archive / "supervisor-ledger.jsonl").read_bytes() == b'{"event_id": "stale"}\n'
    assert (archive / ".finalizer-state.json.9f0c.consumed").is_file()

    # And exactly what the next arm needs is still in place.
    assert (workdir / prearm.RUN_PLAN_NAME).is_file()
    assert (workdir / prearm.RECEIPT_NAME).is_file()


# --------------------------------------------------------------------------- #
# (d) refusal matrix — workdir byte-identical, no archive directory
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("state", ["active", "activating", "reloading", "unknown", ""])
def test_refuses_when_the_unit_is_not_inactive_or_failed(tmp_path, capsys, state):
    workdir = _seed_workdir(tmp_path)
    (workdir / "benchmark-before.json").write_bytes(b"{}")
    stderr = _refusal_case(workdir, _env_file(tmp_path), _fake_systemctl(tmp_path, state), capsys)
    assert UNIT in stderr
    assert "inactive" in stderr and "failed" in stderr


def test_refuses_when_the_systemctl_binary_is_missing(tmp_path, capsys):
    workdir = _seed_workdir(tmp_path)
    (workdir / "benchmark-before.json").write_bytes(b"{}")
    stderr = _refusal_case(workdir, _env_file(tmp_path), tmp_path / "absent-systemctl", capsys)
    assert "replay unit state is unavailable" in stderr


def test_refuses_when_the_receipt_digest_differs_from_the_pinned_expected_stale(tmp_path, capsys):
    workdir = _seed_workdir(tmp_path)
    (workdir / "benchmark-before.json").write_bytes(b"{}")
    env_path = _env_file(tmp_path, expected_stale="a" * 64)
    stderr = _refusal_case(workdir, env_path, _fake_systemctl(tmp_path, "inactive"), capsys)
    assert prearm.EXPECTED_STALE_ENV_KEY in stderr
    assert RECEIPT_SHA256 in stderr


def test_refuses_when_the_pinned_expected_stale_digest_is_malformed(tmp_path, capsys):
    workdir = _seed_workdir(tmp_path)
    (workdir / "benchmark-before.json").write_bytes(b"{}")
    env_path = _env_file(tmp_path, expected_stale="REPLACE_WITH_64_HEX_DIGEST")
    stderr = _refusal_case(workdir, env_path, _fake_systemctl(tmp_path, "inactive"), capsys)
    assert "64-hex" in stderr


def test_refuses_when_the_run_plan_is_not_valid_json(tmp_path, capsys):
    workdir = _seed_workdir(tmp_path)
    (workdir / prearm.RUN_PLAN_NAME).write_text("{not json", encoding="utf-8")
    (workdir / "benchmark-before.json").write_bytes(b"{}")
    stderr = _refusal_case(workdir, _env_file(tmp_path), _fake_systemctl(tmp_path, "inactive"), capsys)
    assert "run plan is not valid JSON" in stderr


def test_refuses_when_a_failure_intent_directory_is_pending(tmp_path, capsys):
    workdir = _seed_workdir(tmp_path)
    intent = workdir / f".{prearm.RECEIPT_NAME}.failure-intent"
    intent.mkdir()
    (intent / "intent.json").write_bytes(b'{"intent": "failure"}')
    (workdir / "benchmark-before.json").write_bytes(b"{}")
    stderr = _refusal_case(workdir, _env_file(tmp_path), _fake_systemctl(tmp_path, "failed"), capsys)
    assert "unresolved failure intent" in stderr
    assert "--finalize-only" in stderr


def test_refuses_when_a_consumed_intent_sibling_is_present(tmp_path, capsys):
    workdir = _seed_workdir(tmp_path)
    consumed = workdir / f".{prearm.RECEIPT_NAME}.failure-intent.consumed-0123abcd"
    consumed.mkdir()
    (workdir / "benchmark-before.json").write_bytes(b"{}")
    stderr = _refusal_case(workdir, _env_file(tmp_path), _fake_systemctl(tmp_path, "failed"), capsys)
    assert "unresolved consumed failure intent" in stderr
    assert "--finalize-only" in stderr


@pytest.mark.parametrize("state", ["consuming", "pending", "committed_cleanup"])
def test_refuses_when_the_intent_gate_state_is_not_idle(tmp_path, capsys, state):
    workdir = _seed_workdir(tmp_path)
    gate = workdir / f".{prearm.RECEIPT_NAME}.intent-gate.json"
    gate.write_text(json.dumps({"schema_version": 1, "state": state}), encoding="utf-8")
    (workdir / "benchmark-before.json").write_bytes(b"{}")
    stderr = _refusal_case(workdir, _env_file(tmp_path), _fake_systemctl(tmp_path, "failed"), capsys)
    assert state in stderr
    assert "idle" in stderr


def test_refuses_when_the_intent_gate_state_is_unreadable(tmp_path, capsys):
    workdir = _seed_workdir(tmp_path)
    gate = workdir / f".{prearm.RECEIPT_NAME}.intent-gate.json"
    gate.write_text("{not json", encoding="utf-8")
    (workdir / "benchmark-before.json").write_bytes(b"{}")
    stderr = _refusal_case(workdir, _env_file(tmp_path), _fake_systemctl(tmp_path, "failed"), capsys)
    assert "terminal intent gate state is not readable JSON" in stderr


# --------------------------------------------------------------------------- #
# (e) permissive-but-safe paths
# --------------------------------------------------------------------------- #


def test_missing_receipt_proceeds_as_the_first_arm(tmp_path, capsys):
    workdir = _seed_workdir(tmp_path, with_receipt=False)
    (workdir / "supervisor-ledger.jsonl").write_bytes(b"{}\n")

    code = _invoke(workdir, _env_file(tmp_path, expected_stale="a" * 64), _fake_systemctl(tmp_path, "inactive"))
    capsys.readouterr()
    assert code == 0

    archive = _sole_archive(workdir)
    assert (archive / "supervisor-ledger.jsonl").is_file()
    manifest = json.loads((archive / prearm.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["receipt_sha256"] is None


def test_missing_run_plan_sweeps_in_notice_bearing_sweep_only_mode(tmp_path, capsys):
    workdir = tmp_path / "replay"
    workdir.mkdir()
    (workdir / prearm.RECEIPT_NAME).write_bytes(RECEIPT_BODY)
    (workdir / "finalizer-state.json").write_bytes(b"{}")

    code = _invoke(workdir, _env_file(tmp_path), _fake_systemctl(tmp_path, "inactive"))
    out = capsys.readouterr().out
    assert code == 0
    assert "sweep-only mode" in out

    archive = _sole_archive(workdir)
    assert (archive / "finalizer-state.json").is_file()
    manifest = json.loads((archive / prearm.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["run_plan_path"] is None


# --------------------------------------------------------------------------- #
# (f) resolved intent residue is swept as a whole family
# --------------------------------------------------------------------------- #


def test_resolved_intent_residue_is_swept_as_a_family(tmp_path, capsys):
    workdir = _seed_workdir(tmp_path)
    family = {
        f".{prearm.RECEIPT_NAME}.publish.lock": b"",
        f".{prearm.RECEIPT_NAME}.intent-gate.lock": b"",
        f".{prearm.RECEIPT_NAME}.intent-gate.json": json.dumps(
            {"schema_version": 1, "state": "idle"}
        ).encode("utf-8"),
    }
    for name, body in family.items():
        (workdir / name).write_bytes(body)

    code = _invoke(workdir, _env_file(tmp_path), _fake_systemctl(tmp_path, "inactive"))
    capsys.readouterr()
    assert code == 0

    archive = _sole_archive(workdir)
    for name, body in family.items():
        assert not (workdir / name).exists(), name
        assert (archive / name).read_bytes() == body


# --------------------------------------------------------------------------- #
# (g) rerun safety
# --------------------------------------------------------------------------- #


def test_second_run_preserves_the_first_archive(tmp_path, capsys):
    workdir = _seed_workdir(tmp_path)
    (workdir / "benchmark-before.json").write_bytes(b"first")
    env_path = _env_file(tmp_path)
    systemctl = _fake_systemctl(tmp_path, "inactive")

    assert _invoke(workdir, env_path, systemctl) == 0
    capsys.readouterr()
    first = _sole_archive(workdir)
    first_snapshot = _snapshot(first)

    (workdir / "benchmark-before.json").write_bytes(b"second")
    assert _invoke(workdir, env_path, systemctl) == 0
    capsys.readouterr()

    archives = _archives(workdir)
    assert len(archives) == 2, archives
    assert first in archives
    assert _snapshot(first) == first_snapshot
    assert (first / "benchmark-before.json").read_bytes() == b"first"
    second = next(path for path in archives if path != first)
    assert (second / "benchmark-before.json").read_bytes() == b"second"
    # The archive root never sweeps itself.
    assert not (second / prearm.ARCHIVE_DIR_NAME).exists()


def test_clean_workdir_is_a_noop_that_still_prints_the_next_step(tmp_path, capsys):
    workdir = _seed_workdir(tmp_path)
    before = _snapshot(workdir)

    code = _invoke(workdir, _env_file(tmp_path), _fake_systemctl(tmp_path, "inactive"))
    out = capsys.readouterr().out
    assert code == 0
    assert _snapshot(workdir) == before
    assert not (workdir / prearm.ARCHIVE_DIR_NAME).exists()
    assert "nothing to archive" in out
    assert f"next: systemctl --user start {UNIT}" in out


# --------------------------------------------------------------------------- #
# (h) no-delete guarantee
# --------------------------------------------------------------------------- #


def test_module_source_uses_no_deletion_api():
    source = Path(prearm.__file__).read_text(encoding="utf-8")
    for token in ("unlink", "rmtree", "os.remove", "rmdir", ".remove("):
        assert token not in source, token
    assert "shutil.move" in source


def test_every_pre_sweep_byte_is_findable_after_the_sweep(tmp_path, capsys):
    workdir = _seed_workdir(tmp_path)
    schema_dump = tmp_path / "schema-before.dump"
    schema_dump.write_bytes(b"PGDMP-stale")
    capture_output = tmp_path / "preflight-activity.json"
    capture_output.write_bytes(b'{"activity": "stale"}')
    (workdir / prearm.RUN_PLAN_NAME).write_text(
        json.dumps(_run_plan_document(schema_dump=schema_dump, capture_output=capture_output, workdir=workdir)),
        encoding="utf-8",
    )
    payloads = {
        "benchmark-before.json": b'{"benchmark": "stale"}',
        "finalizer-state.json": b'{"finalizer": "stale"}',
        "supervisor-ledger.jsonl": b'{"event_id": "stale"}\n',
        "recovery-receipt.json": b'{"recovery": "stale"}',
    }
    for name, body in payloads.items():
        (workdir / name).write_bytes(body)
    expected = {*payloads.values(), b"PGDMP-stale", b'{"activity": "stale"}'}

    assert _invoke(workdir, _env_file(tmp_path), _fake_systemctl(tmp_path, "inactive")) == 0
    capsys.readouterr()

    archive = _sole_archive(workdir)
    found = {path.read_bytes() for path in archive.rglob("*") if path.is_file()}
    assert expected <= found


# --------------------------------------------------------------------------- #
# (i) env-file refusals
# --------------------------------------------------------------------------- #


def test_refuses_a_symlinked_env_file(tmp_path, capsys):
    workdir = _seed_workdir(tmp_path)
    (workdir / "benchmark-before.json").write_bytes(b"{}")
    real = _env_file(tmp_path, name="real.env")
    link = tmp_path / "linked.env"
    link.symlink_to(real)
    stderr = _refusal_case(workdir, link, _fake_systemctl(tmp_path, "inactive"), capsys)
    assert "replay env file is unreadable" in stderr


def test_refuses_a_world_readable_env_file(tmp_path, capsys):
    workdir = _seed_workdir(tmp_path)
    (workdir / "benchmark-before.json").write_bytes(b"{}")
    env_path = _env_file(tmp_path, mode=0o644)
    stderr = _refusal_case(workdir, env_path, _fake_systemctl(tmp_path, "inactive"), capsys)
    assert "mode must be 0600" in stderr


def test_refuses_a_duplicate_key_env_file(tmp_path, capsys):
    workdir = _seed_workdir(tmp_path)
    (workdir / "benchmark-before.json").write_bytes(b"{}")
    env_path = _env_file(tmp_path, extra_lines=(f"{prearm.EXPECTED_STALE_ENV_KEY}={'c' * 64}",))
    stderr = _refusal_case(workdir, env_path, _fake_systemctl(tmp_path, "inactive"), capsys)
    assert "duplicate environment key" in stderr


def test_refuses_an_absent_env_file(tmp_path, capsys):
    workdir = _seed_workdir(tmp_path)
    (workdir / "benchmark-before.json").write_bytes(b"{}")
    stderr = _refusal_case(workdir, tmp_path / "absent.env", _fake_systemctl(tmp_path, "inactive"), capsys)
    assert "replay env file is absent" in stderr


def test_refuses_a_non_key_value_env_line(tmp_path, capsys):
    workdir = _seed_workdir(tmp_path)
    (workdir / "benchmark-before.json").write_bytes(b"{}")
    env_path = _env_file(tmp_path, extra_lines=("NOT_A_PAIR",))
    stderr = _refusal_case(workdir, env_path, _fake_systemctl(tmp_path, "inactive"), capsys)
    assert "expected KEY=VALUE" in stderr


# --------------------------------------------------------------------------- #
# CLI surface
# --------------------------------------------------------------------------- #


def test_help_exits_zero_and_names_the_production_defaults(capsys):
    with pytest.raises(SystemExit) as excinfo:
        prearm.main(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert prearm.DEFAULT_WORKDIR in out
    assert "--replay-env-path" in out
    assert "--systemctl" in out


def test_defaults_bind_the_production_literals():
    assert prearm.DEFAULT_WORKDIR == "/home/nwm/node27-timeseries-compression-replay"
    assert prearm.DEFAULT_SYSTEMCTL == "/usr/bin/systemctl"
    assert prearm.DEFAULT_ENV_PATH == Path(prearm.__file__).resolve().parents[1] / (
        "infra/env/node27-timeseries-compression-replay.env"
    )
    assert prearm.UNIT_NAME == UNIT
    assert set(prearm.WHITELIST) == {"run-plan.json", "terminal-evidence.json"}
