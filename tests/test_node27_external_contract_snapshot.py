"""Hermetic tests for the node-27 external-contract snapshot probe (#1089).

Nothing here touches node-27, docker, systemd or a live database: the probes'
host binaries resolve through ``SNAPSHOT_BIN_DIR``, which every test repoints at
a directory of stub executables that print canned observations and record every
invocation.  The real probe/diff/report code paths run in-process.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from packages.common import node27_container_contract as contract
from scripts import node27_external_contract_snapshot as snapshot

FIXTURE_PATH = snapshot.DEFAULT_FIXTURE

_STUB_TEMPLATE = """#!{python}
import json
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_name = os.path.basename(__file__)
with open(os.path.join(_here, _name + ".responses.json"), encoding="utf-8") as _fh:
    _responses = json.load(_fh)
_argv = " ".join(sys.argv[1:])
with open(os.path.join(_here, "calls.log"), "a", encoding="utf-8") as _fh:
    _fh.write(_name + " " + _argv + "\\n")
for _response in _responses:
    if all(_token in _argv for _token in _response["match"]):
        sys.stdout.write(_response.get("stdout", ""))
        sys.stderr.write(_response.get("stderr", ""))
        sys.exit(_response.get("exit", 0))
sys.stderr.write("no stub response for argv: " + _argv + "\\n")
sys.exit(97)
"""


def _install_stub(bindir: Path, name: str, responses: list[dict[str, Any]]) -> None:
    script = bindir / name
    script.write_text(_STUB_TEMPLATE.replace("{python}", sys.executable), encoding="utf-8")
    script.chmod(0o755)
    (bindir / f"{name}.responses.json").write_text(json.dumps(responses), encoding="utf-8")


def _true_responses(document: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Stub responses that reproduce the committed fixture's observations exactly."""

    def value(section: str, key: str) -> str:
        return document[section][key]["value"]

    unset_timestamp = value("contract", "systemd_unset_timestamp")
    return {
        "systemctl": [
            {
                "match": [snapshot.RESERVED_WITNESS_UNIT],
                "stdout": f"LoadState=not-found\nExecMainStartTimestamp={unset_timestamp}\n",
            },
            {
                "match": [snapshot.RECURRING_UNIT],
                "stdout": "ActiveState=inactive\nSubState=dead\nExecMainStartTimestamp=Sun 2026-08-02 12:25:00 CST\n",
            },
            {
                "match": ["--version"],
                "stdout": f"{value('host_context', 'systemd_version')}\n+PAM +AUDIT +SELINUX +APPARMOR\n",
            },
        ],
        "docker": [
            {
                "match": ["pg_restore", "--version"],
                "stdout": f"{value('host_context', 'container_pg_restore_version')}\n",
            },
            {"match": ["readlink"], "stdout": f"{value('contract', 'container_pg_restore_realpath')}\n"},
            {
                "match": ["inspect"],
                "stdout": f"{value('host_context', 'nhms_db_image_ref')}|{value('host_context', 'nhms_db_image_id')}\n",
            },
            {"match": ["--version"], "stdout": f"{value('host_context', 'docker_version')}\n"},
        ],
        "psql": [
            {"match": ["SHOW server_version"], "stdout": f"{value('host_context', 'pg_server_version')}\n"},
            {"match": ["extversion"], "stdout": f"{value('host_context', 'timescaledb_version')}\n"},
            {"match": ["pg_backend_pid"], "stdout": f"{value('contract', 'client_backend_type')}\n"},
            {
                "match": ["GROUP BY backend_type"],
                "stdout": "autovacuum launcher|1\nclient backend|12\nparallel worker|9\nwalwriter|1\n",
            },
        ],
    }


@pytest.fixture
def committed() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def stub_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, committed: dict[str, Any]) -> Path:
    """A stub-binary directory whose observations equal the committed fixture."""

    bindir = tmp_path / "probe-bin"
    bindir.mkdir()
    for name, responses in _true_responses(committed).items():
        _install_stub(bindir, name, responses)
    monkeypatch.setattr(snapshot, "SNAPSHOT_BIN_DIR", bindir)
    return bindir


def _calls(bindir: Path) -> list[str]:
    log = bindir / "calls.log"
    if not log.exists():
        return []
    return [line for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def _override(bindir: Path, name: str, responses: list[dict[str, Any]]) -> None:
    """Prepend responses to one stub, shadowing the true ones."""

    path = bindir / f"{name}.responses.json"
    existing = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps([*responses, *existing]), encoding="utf-8")


def _tampered_copy(tmp_path: Path, document: dict[str, Any], mutate) -> Path:
    target = tmp_path / "tampered" / "snapshot.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    mutate(document)
    target.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


# --- (a) alignment guard, both directions ----------------------------------


def test_fixture_contract_section_is_aligned_with_the_contract_module(committed: dict[str, Any]) -> None:
    # Forward: every fixture contract value IS the pinned constant.  Read through
    # getattr so an in-memory flip of a constant reds this test.
    assert committed["contract"]["container_pg_restore_realpath"]["value"] == getattr(
        contract, "CONTAINER_PG_RESTORE_REALPATH"
    )
    assert committed["contract"]["systemd_unset_timestamp"]["value"] == getattr(contract, "SYSTEMD_UNSET_TIMESTAMP")
    assert committed["contract"]["client_backend_type"]["value"] == getattr(contract, "CLIENT_BACKEND_TYPE")
    assert snapshot.alignment_mismatches(committed) == []


def test_measured_constant_count_matches_the_fixture_contract_section() -> None:
    # Reverse direction: a FOURTH measured constant added to the contract module
    # reds this without anyone remembering to touch the snapshot tooling.
    source = Path(contract.__file__).read_text(encoding="utf-8")
    measured_blocks = [line for line in source.splitlines() if line.startswith("# MEASURED")]
    assert len(measured_blocks) == 3
    document = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert len(document["contract"]) == 3
    assert len(snapshot.CONTRACT_CONSTANTS) == 3


# --- (b) drift mutation, (b2) misalignment mutation ------------------------


def test_tampered_host_context_value_exits_drift_and_names_that_field(
    tmp_path: Path,
    stub_bin: Path,
    committed: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    def mutate(document: dict[str, Any]) -> None:
        document["host_context"]["docker_version"]["value"] = "Docker version 99.0.0, build deadbee"

    fixture = _tampered_copy(tmp_path, committed, mutate)

    code = snapshot.main(["--check", "--fixture", str(fixture)])

    out = capsys.readouterr().out
    assert code == snapshot.EXIT_DRIFT == 3
    drift_lines = [line for line in out.splitlines() if line.startswith("DRIFT ")]
    assert len(drift_lines) == 1
    assert "host_context.docker_version" in drift_lines[0]
    assert "Docker version 99.0.0, build deadbee" in drift_lines[0]
    assert "Docker version 28.2.2, build e6534b4" in drift_lines[0]
    assert _calls(stub_bin), "the drift verdict must rest on real spawned observations"


def test_tampered_contract_value_exits_misalignment_without_spawning_a_probe(
    tmp_path: Path,
    stub_bin: Path,
    committed: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    def mutate(document: dict[str, Any]) -> None:
        document["contract"]["systemd_unset_timestamp"]["value"] = "-"

    fixture = _tampered_copy(tmp_path, committed, mutate)

    code = snapshot.main(["--check", "--fixture", str(fixture)])

    out = capsys.readouterr().out
    assert code == snapshot.EXIT_MISALIGNMENT == 4
    assert code != snapshot.EXIT_DRIFT
    assert "MISALIGNMENT contract.systemd_unset_timestamp" in out
    assert "node27_container_contract.SYSTEMD_UNSET_TIMESTAMP" in out
    assert "DRIFT" not in out
    # The misalignment verdict is reached BEFORE any probe exists.
    assert _calls(stub_bin) == []


# --- (c) clean pass --------------------------------------------------------


def test_live_observations_matching_the_committed_fixture_exit_zero(
    stub_bin: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = snapshot.main(["--check"])

    out = capsys.readouterr().out
    assert code == snapshot.EXIT_OK == 0
    assert "OK, no drift" in out
    # Every probe in the frozen table really ran.
    assert len(_calls(stub_bin)) == len(snapshot.PROBES)


# --- (d) read-only argv/SQL whitelist --------------------------------------


def test_every_spawnable_argv_matches_the_frozen_read_only_whitelist() -> None:
    # Independent oracle: the exact argv tails this tool is allowed to spawn.
    allowed_tails = {
        (
            "systemctl",
            ("--user", "show", snapshot.RESERVED_WITNESS_UNIT, "-p", "LoadState", "-p", "ExecMainStartTimestamp"),
        ),
        ("systemctl", ("--version",)),
        (
            "systemctl",
            (
                "--user",
                "show",
                snapshot.RECURRING_UNIT,
                "-p",
                "ActiveState",
                "-p",
                "SubState",
                "-p",
                "ExecMainStartTimestamp",
            ),
        ),
        ("docker", ("--version",)),
        ("docker", ("inspect", "--format={{.Config.Image}}|{{.Image}}", "nhms-db")),
        ("docker", ("exec", "nhms-db", "/usr/bin/readlink", "-f", "/usr/bin/pg_restore")),
        ("docker", ("exec", "nhms-db", "/usr/bin/pg_restore", "--version")),
        *[("psql", ("--dbname", "nhms", "--no-psqlrc", "-At", "-c", sql)) for sql in snapshot.PROBE_SQL],
    }
    assert {(probe.binary, probe.args) for probe in snapshot.PROBES} == allowed_tails

    for probe in snapshot.PROBES:
        argv = probe.argv()
        assert argv[0] == str(snapshot.SNAPSHOT_BIN_DIR / probe.binary)
        assert probe.binary in {"systemctl", "docker", "psql"}
        verb = probe.args[0]
        if probe.binary == "systemctl":
            assert verb in {"--user", "--version"}
            if verb == "--user":
                assert probe.args[1] == "show"
        elif probe.binary == "docker":
            assert verb in {"--version", "inspect", "exec"}
            if verb == "exec":
                # Container-internal argv[0] stays literal and absolute: it is
                # not this host's binary and must not resolve through the seam.
                assert probe.args[1] == snapshot.DB_CONTAINER
                assert probe.args[2].startswith("/")
                assert probe.args[2] in {"/usr/bin/readlink", "/usr/bin/pg_restore"}
                if probe.args[2] == "/usr/bin/readlink":
                    assert probe.args[3] == "-f"
                else:
                    assert probe.args[3:] == ("--version",)
        else:
            assert probe.args[:5] == ("--dbname", "nhms", "--no-psqlrc", "-At", "-c")
            assert probe.args[5] in snapshot.PROBE_SQL


def test_every_sql_string_is_a_single_select_or_show_statement() -> None:
    assert len(snapshot.PROBE_SQL) == 4
    for sql in snapshot.PROBE_SQL:
        assert re.match(r"^(SELECT|SHOW)\b", sql), sql
        # No statement terminator at all, so nothing can be chained after it.
        assert ";" not in sql, sql
        assert "--" not in sql, sql


# --- (e) production bin-dir default ----------------------------------------


def test_snapshot_bin_dir_defaults_to_the_pinned_system_path() -> None:
    # The seam MUST default to the production location, or a live run would look
    # for its probes in a test directory that does not exist on node-27.
    assert snapshot.SNAPSHOT_BIN_DIR == Path("/usr/bin")
    assert snapshot.PROBE_UNSET_TIMESTAMP.argv()[0] == "/usr/bin/systemctl"
    assert snapshot.PROBE_DOCKER_VERSION.argv()[0] == "/usr/bin/docker"
    assert snapshot.PROBE_PG_SERVER_VERSION.argv()[0] == "/usr/bin/psql"


# --- (f) informational is never compared -----------------------------------


def test_informational_tampering_still_exits_zero(
    tmp_path: Path,
    stub_bin: Path,
    committed: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    def mutate(document: dict[str, Any]) -> None:
        document["informational"]["measured_at"] = "1999-01-01T00:00:00Z"
        document["informational"]["hostname"] = "not-the-host"
        document["informational"]["backend_type_distribution"] = {"client backend": 4321}
        document["informational"]["recurring_unit"]["ActiveState"] = "active"

    fixture = _tampered_copy(tmp_path, committed, mutate)

    code = snapshot.main(["--check", "--fixture", str(fixture)])

    assert code == snapshot.EXIT_OK
    assert "DRIFT" not in capsys.readouterr().out


# --- (g) probe-execution failure classification ----------------------------


def test_probe_exiting_non_zero_is_a_probe_failure_not_drift(
    stub_bin: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _override(stub_bin, "docker", [{"match": [], "exit": 1, "stderr": "Cannot connect to the Docker daemon\n"}])

    code = snapshot.main(["--check"])

    out = capsys.readouterr().out
    assert code == snapshot.EXIT_PROBE_FAILURE == 5
    assert code not in {snapshot.EXIT_DRIFT, snapshot.EXIT_MISALIGNMENT}
    assert "PROBE-FAILURE nhms-db-image" in out
    assert "Cannot connect to the Docker daemon" in out
    assert "DRIFT" not in out


def test_probe_exiting_zero_with_empty_output_is_a_probe_failure(
    stub_bin: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _override(stub_bin, "psql", [{"match": ["pg_backend_pid"], "exit": 0, "stdout": ""}])

    code = snapshot.main(["--check"])

    out = capsys.readouterr().out
    assert code == snapshot.EXIT_PROBE_FAILURE
    assert "PROBE-FAILURE client-backend-type" in out
    assert "empty stdout" in out
    assert "DRIFT" not in out


def test_probe_exiting_zero_without_the_measured_property_is_a_probe_failure(
    stub_bin: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _override(
        stub_bin,
        "systemctl",
        [{"match": [snapshot.RESERVED_WITNESS_UNIT], "exit": 0, "stdout": "ExecMainStartTimestamp=n/a\n"}],
    )

    code = snapshot.main(["--check"])

    out = capsys.readouterr().out
    assert code == snapshot.EXIT_PROBE_FAILURE
    # Names the failing PROBE, not a drifted field.
    assert "PROBE-FAILURE systemd-unset-timestamp" in out
    assert "LoadState" in out
    assert "DRIFT" not in out


def test_reserved_witness_unit_that_exists_fails_closed(
    stub_bin: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # If the reserved name ever becomes a real unit, the rendering it returns is
    # no longer the unset-timestamp witness and must never be trusted as one.
    _override(
        stub_bin,
        "systemctl",
        [
            {
                "match": [snapshot.RESERVED_WITNESS_UNIT],
                "exit": 0,
                "stdout": "LoadState=loaded\nExecMainStartTimestamp=n/a\n",
            }
        ],
    )

    code = snapshot.main(["--check"])

    out = capsys.readouterr().out
    assert code == snapshot.EXIT_PROBE_FAILURE
    assert "PROBE-FAILURE systemd-unset-timestamp" in out
    assert "LoadState='loaded'" in out
    assert "DRIFT" not in out


# --- (h) provenance never participates -------------------------------------


def test_provenance_only_edit_changes_no_verdict(
    tmp_path: Path,
    stub_bin: Path,
    committed: dict[str, Any],
) -> None:
    def mutate(document: dict[str, Any]) -> None:
        for section in snapshot.COMPARED_SECTIONS:
            for entry in document[section].values():
                entry["_provenance"] = {"command": "rewritten", "date": "1999-01-01", "source": "hand edit"}

    fixture = _tampered_copy(tmp_path, committed, mutate)
    edited = json.loads(fixture.read_text(encoding="utf-8"))

    assert snapshot.main(["--check", "--fixture", str(fixture)]) == snapshot.EXIT_OK
    assert snapshot.alignment_mismatches(edited) == []


# --- (i) --check never writes ----------------------------------------------


def test_check_writes_nothing_next_to_the_fixture(
    tmp_path: Path,
    stub_bin: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture_dir = tmp_path / "fixture-dir"
    fixture_dir.mkdir()
    fixture = fixture_dir / "snapshot.json"
    fixture.write_bytes(FIXTURE_PATH.read_bytes())
    before_bytes = fixture.read_bytes()
    before_entries = sorted(path.name for path in fixture_dir.iterdir())

    workdir = tmp_path / "cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    assert snapshot.main(["--check", "--fixture", str(fixture)]) == snapshot.EXIT_OK

    assert fixture.read_bytes() == before_bytes
    assert sorted(path.name for path in fixture_dir.iterdir()) == before_entries
    assert sorted(path.name for path in workdir.iterdir()) == []

    # --check has no writing flag at all: asking for one is a usage error.
    capsys.readouterr()
    with pytest.raises(SystemExit) as raised:
        snapshot.main(["--check", "--fixture", str(fixture), "--output", str(tmp_path / "nope.json")])
    assert raised.value.code == snapshot.EXIT_USAGE
    assert not (tmp_path / "nope.json").exists()


# --- (j) dump shape --------------------------------------------------------


def test_dump_emits_the_three_sections_with_wrapped_compared_entries(
    stub_bin: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert snapshot.main(["--dump"]) == snapshot.EXIT_OK

    document = json.loads(capsys.readouterr().out)
    assert set(document) == {"contract", "host_context", "informational"}
    for section in snapshot.COMPARED_SECTIONS:
        assert set(document[section]) == set(snapshot.COMPARED_PROBES[section])
        for key, entry in document[section].items():
            assert set(entry) == {"value", "_provenance"}, f"{section}.{key}"
            assert isinstance(entry["value"], str) and entry["value"]
            assert set(entry["_provenance"]) == {"command", "date", "source"}
            # Provenance records the command by binary NAME, never a stub path.
            assert entry["_provenance"]["command"].startswith(snapshot.COMPARED_PROBES[section][key].binary)
            assert str(stub_bin) not in entry["_provenance"]["command"]
    informational = document["informational"]
    assert set(informational) == {"measured_at", "hostname", "backend_type_distribution", "recurring_unit"}
    assert informational["backend_type_distribution"]["client backend"] == 12
    assert informational["recurring_unit"]["unit"] == snapshot.RECURRING_UNIT


def test_dump_to_output_path_writes_the_same_document(stub_bin: Path, tmp_path: Path) -> None:
    target = tmp_path / "dumped.json"

    assert snapshot.main(["--dump", "--output", str(target)]) == snapshot.EXIT_OK

    document = json.loads(target.read_text(encoding="utf-8"))
    assert set(document) == {"contract", "host_context", "informational"}
    assert document["contract"]["client_backend_type"]["value"] == contract.CLIENT_BACKEND_TYPE
