#!/usr/bin/env python3
"""Read-only node-27 external-contract drift probe (#1089).

``packages/common/node27_container_contract.py`` pins three MEASURED host
contracts (``CONTAINER_PG_RESTORE_REALPATH``, ``SYSTEMD_UNSET_TIMESTAMP``,
``CLIENT_BACKEND_TYPE``).  Nothing in CI can observe the host they were measured
on, so a systemd/docker/PostgreSQL/TimescaleDB upgrade drifts them silently and
the drift surfaces first inside an authorized mutation window, burning an
irreversible arm attempt (launch 7 -> G9, launch 11 -> G14).  This tool moves
that discovery out of band: it re-measures the contracts live, read-only, and
diffs them against the committed snapshot fixture
``packages/common/node27_external_contract_snapshot.json``.

Two modes:

* ``--dump [--output PATH]`` -- measure and emit the snapshot JSON (stdout by
  default).  This path imports nothing outside the standard library, so a copy
  of this single file runs on node-27 without checking out a branch there.
* ``--check [--fixture PATH]`` -- assert the fixture's ``contract`` section
  equals the contract module's measured constants (lazy import, decided BEFORE
  any probe runs), then re-measure and diff the compared sections.  ``--check``
  never writes a file.

Read-only by construction: every spawned argv comes from the frozen ``PROBES``
table below (``systemctl --user show`` / ``systemctl --version`` /
``docker --version`` / ``docker inspect`` / ``docker exec <container> readlink``
/ ``docker exec <container> pg_restore --version`` / ``psql -c <SELECT|SHOW>``),
and every SQL string comes from the frozen ``PROBE_SQL`` tuple.  Host binaries
resolve through the ``SNAPSHOT_BIN_DIR`` seam (``SUPERVISOR_BIN_DIR``
precedent), so no PATH entry can substitute one and hermetic tests can repoint
it at stub binaries.  Connection settings come from the ambient environment
(the operator sources ``infra/env/node27-timeseries-compression-replay.env``);
no credential ever lives in this file or in the fixture.

Exit-code contract -- four states, judged in this order:

* ``0``  ok: fixture aligned with the contract module and every compared field
  matches the live observation.
* ``2``  usage/input error (argparse's own code): bad CLI, or a fixture that is
  missing, unparsable or structurally wrong.  Not a verdict about the host.
* ``4``  MISALIGNMENT: the fixture's ``contract`` section disagrees with
  ``node27_container_contract`` (or that module cannot be imported).  Decided
  first, so NO probe is spawned.
* ``3``  DRIFT: a compared fixture value disagrees with a real observed value.
* ``5``  PROBE-EXECUTION FAILURE: a probe cannot run, exits non-zero, exits 0
  with empty stdout, or exits 0 without the property/key it measures.  This is
  never reported as drift -- a drift report always carries a real observed
  value; a returned property line whose value changed IS drift.
"""

from __future__ import annotations

import argparse
import json
import shlex
import socket
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = REPO_ROOT / "packages" / "common" / "node27_external_contract_snapshot.json"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_DRIFT = 3
EXIT_MISALIGNMENT = 4
EXIT_PROBE_FAILURE = 5

# The supervisor pins an absolute argv[0] for every probe it owns, so no PATH
# entry can substitute one; this tool inherits that seam verbatim (supervisor
# `SUPERVISOR_BIN_DIR`).  Hermetic tests repoint it at a directory of stub
# binaries to execute the real probe/diff code paths offline; production must
# always read /usr/bin, asserted by
# `test_snapshot_bin_dir_defaults_to_the_pinned_system_path`.  Only host-side
# binaries resolve through it -- paths naming a binary INSIDE the DB container
# stay literal and absolute, because they are not this host's binaries.
SNAPSHOT_BIN_DIR = Path("/usr/bin")

DB_CONTAINER = "nhms-db"
DB_NAME = "nhms"

# A name reserved for this probe and deliberately never installed as a unit.
#
# WHY A NEVER-EXISTING UNIT: `SYSTEMD_UNSET_TIMESTAMP` pins how `systemctl show`
# renders an UNSET `ExecMainStartTimestamp`.  No real unit witnesses that
# deterministically -- the recurring compression unit is `inactive/dead` but HAS
# run this boot (daily 04:25 UTC timer), so it renders a real timestamp.  For an
# unloaded unit systemd still renders the default properties, and the unset
# timestamp renders as the pinned literal, which makes this the only
# deterministic witness of the rendering contract.
#
# LIMITATION (issue #1089, fixture review P1-2): this witnesses systemd's
# RENDERING contract only.  It does NOT witness the loaded-but-never-started
# whole-dict shape that
# `scripts/node27_timeseries_compression_supervisor.py:1282-1293` and
# `scripts/node27_timeseries_compression_live_evidence.py:834-845` assert by
# dict equality.  A green `--check` therefore does NOT imply those two
# checkpoints pass -- today they demonstrably would not, because the recurring
# unit has run this boot.  That pre-existing consumer defect is tracked
# separately and is out of this tool's scope.
RESERVED_WITNESS_UNIT = "nhms-external-contract-snapshot-witness-does-not-exist.service"
RECURRING_UNIT = "nhms-node27-timeseries-compression.service"

# Every SQL string this tool can send, frozen here so a test can walk them and
# reject anything that is not a single SELECT/SHOW statement.
SQL_SERVER_VERSION = "SHOW server_version"
SQL_TIMESCALEDB_VERSION = "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'"
SQL_CLIENT_BACKEND_TYPE = "SELECT backend_type FROM pg_stat_activity WHERE pid = pg_backend_pid()"
SQL_BACKEND_TYPE_DISTRIBUTION = (
    "SELECT backend_type, count(*) FROM pg_stat_activity GROUP BY backend_type ORDER BY backend_type"
)
PROBE_SQL: tuple[str, ...] = (
    SQL_SERVER_VERSION,
    SQL_TIMESCALEDB_VERSION,
    SQL_CLIENT_BACKEND_TYPE,
    SQL_BACKEND_TYPE_DISTRIBUTION,
)

PROBE_TIMEOUT_SECONDS = 30.0
PROVENANCE_SOURCE = "issue #1089 baseline"
COMPARED_SECTIONS: tuple[str, ...] = ("contract", "host_context")
INFORMATIONAL_SECTION = "informational"


class ProbeFailure(RuntimeError):
    """A probe could not produce a real observation (never classified as drift)."""

    def __init__(self, probe: str, detail: str) -> None:
        super().__init__(f"{probe}: {detail}")
        self.probe = probe
        self.detail = detail


class FixtureError(RuntimeError):
    """The snapshot fixture is missing, unparsable or structurally wrong."""


@dataclass(frozen=True)
class Probe:
    """One frozen, read-only argv.

    ``binary`` is a host binary name resolved through :data:`SNAPSHOT_BIN_DIR`;
    ``args`` are literal and may contain absolute container-internal paths.
    """

    name: str
    binary: str
    args: tuple[str, ...]

    def argv(self) -> list[str]:
        return [str(SNAPSHOT_BIN_DIR / self.binary), *self.args]

    def command(self) -> str:
        """Human-readable command string for the fixture's ``_provenance``.

        Deliberately rendered from the binary NAME, not the resolved seam path,
        so a fixture generated under a repointed bin dir cannot record a path
        that does not exist on node-27.
        """

        return shlex.join([self.binary, *self.args])


def _psql_args(sql: str) -> tuple[str, ...]:
    return ("--dbname", DB_NAME, "--no-psqlrc", "-At", "-c", sql)


PROBE_UNSET_TIMESTAMP = Probe(
    "systemd-unset-timestamp",
    "systemctl",
    ("--user", "show", RESERVED_WITNESS_UNIT, "-p", "LoadState", "-p", "ExecMainStartTimestamp"),
)
PROBE_SYSTEMD_VERSION = Probe("systemd-version", "systemctl", ("--version",))
PROBE_RECURRING_UNIT = Probe(
    "recurring-unit-state",
    "systemctl",
    ("--user", "show", RECURRING_UNIT, "-p", "ActiveState", "-p", "SubState", "-p", "ExecMainStartTimestamp"),
)
PROBE_DOCKER_VERSION = Probe("docker-version", "docker", ("--version",))
PROBE_DB_IMAGE = Probe(
    "nhms-db-image",
    "docker",
    ("inspect", "--format={{.Config.Image}}|{{.Image}}", DB_CONTAINER),
)
PROBE_PG_RESTORE_REALPATH = Probe(
    "container-pg-restore-realpath",
    "docker",
    ("exec", DB_CONTAINER, "/usr/bin/readlink", "-f", "/usr/bin/pg_restore"),
)
PROBE_PG_RESTORE_VERSION = Probe(
    "container-pg-restore-version",
    "docker",
    ("exec", DB_CONTAINER, "/usr/bin/pg_restore", "--version"),
)
PROBE_PG_SERVER_VERSION = Probe("pg-server-version", "psql", _psql_args(SQL_SERVER_VERSION))
PROBE_TIMESCALEDB_VERSION = Probe("timescaledb-version", "psql", _psql_args(SQL_TIMESCALEDB_VERSION))
PROBE_CLIENT_BACKEND_TYPE = Probe("client-backend-type", "psql", _psql_args(SQL_CLIENT_BACKEND_TYPE))
PROBE_BACKEND_TYPE_DISTRIBUTION = Probe(
    "backend-type-distribution",
    "psql",
    _psql_args(SQL_BACKEND_TYPE_DISTRIBUTION),
)

# THE complete set of argvs this module can ever spawn.
PROBES: tuple[Probe, ...] = (
    PROBE_UNSET_TIMESTAMP,
    PROBE_SYSTEMD_VERSION,
    PROBE_RECURRING_UNIT,
    PROBE_DOCKER_VERSION,
    PROBE_DB_IMAGE,
    PROBE_PG_RESTORE_REALPATH,
    PROBE_PG_RESTORE_VERSION,
    PROBE_PG_SERVER_VERSION,
    PROBE_TIMESCALEDB_VERSION,
    PROBE_CLIENT_BACKEND_TYPE,
    PROBE_BACKEND_TYPE_DISTRIBUTION,
)

# Compared field -> the probe that measures it.  Doubles as the expected key set
# of each compared fixture section and as the probe name a report cites.
COMPARED_PROBES: dict[str, dict[str, Probe]] = {
    "contract": {
        "container_pg_restore_realpath": PROBE_PG_RESTORE_REALPATH,
        "systemd_unset_timestamp": PROBE_UNSET_TIMESTAMP,
        "client_backend_type": PROBE_CLIENT_BACKEND_TYPE,
    },
    "host_context": {
        "systemd_version": PROBE_SYSTEMD_VERSION,
        "docker_version": PROBE_DOCKER_VERSION,
        "nhms_db_image_ref": PROBE_DB_IMAGE,
        "nhms_db_image_id": PROBE_DB_IMAGE,
        "container_pg_restore_version": PROBE_PG_RESTORE_VERSION,
        "pg_server_version": PROBE_PG_SERVER_VERSION,
        "timescaledb_version": PROBE_TIMESCALEDB_VERSION,
    },
}

# Fixture ``contract`` key -> the ``node27_container_contract`` constant it must
# equal.  The 1:1 seam the alignment guard enforces in both directions.
CONTRACT_CONSTANTS: dict[str, str] = {
    "container_pg_restore_realpath": "CONTAINER_PG_RESTORE_REALPATH",
    "systemd_unset_timestamp": "SYSTEMD_UNSET_TIMESTAMP",
    "client_backend_type": "CLIENT_BACKEND_TYPE",
}


# --- probe execution -------------------------------------------------------


def run_probe(probe: Probe, *, timeout: float = PROBE_TIMEOUT_SECONDS) -> str:
    """Run one probe and return its stripped stdout, or raise :class:`ProbeFailure`."""

    argv = probe.argv()
    try:
        completed = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as error:
        raise ProbeFailure(probe.name, f"timed out after {timeout:g}s") from error
    except OSError as error:
        raise ProbeFailure(probe.name, f"cannot execute {argv[0]}: {error}") from error
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", "replace").strip().splitlines()
        tail = stderr[-1] if stderr else "<no stderr>"
        raise ProbeFailure(probe.name, f"exited {completed.returncode}: {tail}")
    text = completed.stdout.decode("utf-8", "replace").strip()
    if not text:
        # Zero exit with nothing to read is a broken probe, NOT a changed host.
        raise ProbeFailure(probe.name, "exited 0 with empty stdout")
    return text


def _first_line(probe: Probe) -> str:
    return run_probe(probe).splitlines()[0].strip()


def _properties(probe: Probe, required: Sequence[str]) -> dict[str, str]:
    """Parse ``key=value`` lines of a ``systemctl show`` output, fail-closed on gaps."""

    text = run_probe(probe)
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    missing = [key for key in required if key not in values]
    if missing:
        raise ProbeFailure(probe.name, f"exited 0 without the measured property/properties: {', '.join(missing)}")
    return values


def _single_value(probe: Probe) -> str:
    text = run_probe(probe)
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise ProbeFailure(probe.name, f"expected exactly one output line, got {len(lines)}")
    return lines[0].strip()


def collect_observations() -> dict[str, dict[str, Any]]:
    """Run every probe and return the raw (unwrapped) observation sections."""

    witness = _properties(PROBE_UNSET_TIMESTAMP, ("LoadState", "ExecMainStartTimestamp"))
    if witness["LoadState"] != "not-found":
        # Fail closed: if the reserved name ever becomes a real unit, this probe
        # stops witnessing the unset rendering and must not be trusted.
        raise ProbeFailure(
            PROBE_UNSET_TIMESTAMP.name,
            f"reserved witness unit {RESERVED_WITNESS_UNIT} is loaded (LoadState={witness['LoadState']!r}); "
            "it must never exist for the unset-timestamp rendering to be observable",
        )
    image = _single_value(PROBE_DB_IMAGE).split("|")
    if len(image) != 2 or not all(field.strip() for field in image):
        raise ProbeFailure(PROBE_DB_IMAGE.name, f"expected '<image ref>|<image id>', got {'|'.join(image)!r}")
    recurring = _properties(PROBE_RECURRING_UNIT, ("ActiveState", "SubState", "ExecMainStartTimestamp"))

    return {
        "contract": {
            "container_pg_restore_realpath": _single_value(PROBE_PG_RESTORE_REALPATH),
            "systemd_unset_timestamp": witness["ExecMainStartTimestamp"],
            "client_backend_type": _single_value(PROBE_CLIENT_BACKEND_TYPE),
        },
        "host_context": {
            "systemd_version": _first_line(PROBE_SYSTEMD_VERSION),
            "docker_version": _first_line(PROBE_DOCKER_VERSION),
            "nhms_db_image_ref": image[0].strip(),
            "nhms_db_image_id": image[1].strip(),
            "container_pg_restore_version": _first_line(PROBE_PG_RESTORE_VERSION),
            "pg_server_version": _single_value(PROBE_PG_SERVER_VERSION),
            "timescaledb_version": _single_value(PROBE_TIMESCALEDB_VERSION),
        },
        # NEVER compared: volatile facts recorded for diagnosis only.  The
        # recurring unit's state is the counter-evidence for the P1-2 limitation
        # documented at RESERVED_WITNESS_UNIT.
        INFORMATIONAL_SECTION: {
            "measured_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "hostname": socket.gethostname(),
            "backend_type_distribution": _backend_type_distribution(),
            "recurring_unit": {
                "unit": RECURRING_UNIT,
                "ActiveState": recurring["ActiveState"],
                "SubState": recurring["SubState"],
                "ExecMainStartTimestamp": recurring["ExecMainStartTimestamp"],
            },
        },
    }


def _backend_type_distribution() -> dict[str, int]:
    distribution: dict[str, int] = {}
    for line in run_probe(PROBE_BACKEND_TYPE_DISTRIBUTION).splitlines():
        if not line.strip():
            continue
        backend_type, _, count = line.rpartition("|")
        if not backend_type or not count.strip().isdigit():
            raise ProbeFailure(PROBE_BACKEND_TYPE_DISTRIBUTION.name, f"unparsable row: {line!r}")
        distribution[backend_type.strip()] = int(count)
    if not distribution:
        raise ProbeFailure(PROBE_BACKEND_TYPE_DISTRIBUTION.name, "exited 0 with no backend_type rows")
    return distribution


# --- snapshot document -----------------------------------------------------


def build_snapshot(observations: Mapping[str, Mapping[str, Any]], *, date: str) -> dict[str, Any]:
    """Wrap raw observations into the committed fixture shape."""

    snapshot: dict[str, Any] = {}
    for section in COMPARED_SECTIONS:
        snapshot[section] = {
            key: {
                "value": observations[section][key],
                "_provenance": {
                    "command": probe.command(),
                    "date": date,
                    "source": PROVENANCE_SOURCE,
                },
            }
            for key, probe in COMPARED_PROBES[section].items()
        }
    # Informational entries carry no provenance wrapper on purpose: they are
    # dump-recorded diagnostics, never compared, and the shape difference makes
    # "compared" visible at a glance in the committed fixture.
    snapshot[INFORMATIONAL_SECTION] = dict(observations[INFORMATIONAL_SECTION])
    return snapshot


def _load_fixture(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise FixtureError(f"cannot read fixture {path}: {error}") from error
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as error:
        raise FixtureError(f"fixture {path} is not valid JSON: {error}") from error
    if not isinstance(document, dict):
        raise FixtureError(f"fixture {path} is not a JSON object")
    for section in COMPARED_SECTIONS:
        entries = document.get(section)
        if not isinstance(entries, dict):
            raise FixtureError(f"fixture {path} has no {section!r} object")
        expected_keys = set(COMPARED_PROBES[section])
        if set(entries) != expected_keys:
            raise FixtureError(
                f"fixture {path} section {section!r} keys {sorted(entries)} "
                f"do not match the probe set {sorted(expected_keys)}"
            )
        for key, entry in entries.items():
            if not isinstance(entry, dict) or "value" not in entry:
                raise FixtureError(f"fixture {path} entry {section}.{key} is not a {{'value': ...}} object")
    return document


def fixture_value(document: Mapping[str, Any], section: str, key: str) -> Any:
    """Read a compared entry's ``value``.  ``_provenance`` never participates."""

    return document[section][key]["value"]


# --- verdicts --------------------------------------------------------------


def alignment_mismatches(document: Mapping[str, Any]) -> list[str]:
    """Report lines for every fixture ``contract`` entry that is not its constant."""

    try:
        from packages.common import node27_container_contract as contract_module
    except ImportError as error:  # pragma: no cover - exercised on-node only
        return [
            "MISALIGNMENT contract: cannot import packages.common.node27_container_contract "
            f"({error}); run from the repo root or set PYTHONPATH=/home/nwm/NWM"
        ]
    mismatches: list[str] = []
    for key, constant_name in CONTRACT_CONSTANTS.items():
        expected = getattr(contract_module, constant_name, None)
        observed = fixture_value(document, "contract", key)
        if expected != observed:
            mismatches.append(
                f"MISALIGNMENT contract.{key}: fixture {observed!r} != "
                f"node27_container_contract.{constant_name} {expected!r}"
            )
    return mismatches


def drift_report(document: Mapping[str, Any], observations: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Report lines for every compared field whose live observation moved."""

    drifts: list[str] = []
    for section in COMPARED_SECTIONS:
        for key in COMPARED_PROBES[section]:
            expected = fixture_value(document, section, key)
            observed = observations[section][key]
            if expected != observed:
                drifts.append(f"DRIFT {section}.{key}: expected {expected!r} observed {observed!r}")
    return drifts


def _emit(stream: TextIO, lines: Iterable[str]) -> None:
    for line in lines:
        print(line, file=stream)


def run_check(fixture_path: Path, *, stream: TextIO) -> int:
    """Never writes anything: reads the fixture, spawns read-only probes, reports."""

    try:
        document = _load_fixture(fixture_path)
    except FixtureError as error:
        print(f"USAGE {error}", file=stream)
        return EXIT_USAGE

    # 1. Alignment first, so a fixture that disagrees with the contract module
    #    is refused BEFORE a single probe is spawned.
    mismatches = alignment_mismatches(document)
    if mismatches:
        _emit(stream, mismatches)
        print(f"--check: MISALIGNMENT ({len(mismatches)} constant(s)) against {fixture_path}", file=stream)
        return EXIT_MISALIGNMENT

    # 2. Observe.  A probe that cannot produce a real value is never drift.
    try:
        observations = collect_observations()
    except ProbeFailure as error:
        print(f"PROBE-FAILURE {error.probe}: {error.detail}", file=stream)
        print(f"--check: PROBE-EXECUTION FAILURE against {fixture_path}", file=stream)
        return EXIT_PROBE_FAILURE

    # 3. Diff the compared sections only; `informational` is never compared.
    drifts = drift_report(document, observations)
    if drifts:
        _emit(stream, drifts)
        print(f"--check: DRIFT ({len(drifts)} field(s)) against {fixture_path}", file=stream)
        return EXIT_DRIFT

    print(f"--check: OK, no drift against {fixture_path}", file=stream)
    return EXIT_OK


def run_dump(output: Path | None, *, stream: TextIO, error_stream: TextIO) -> int:
    try:
        observations = collect_observations()
    except ProbeFailure as error:
        print(f"PROBE-FAILURE {error.probe}: {error.detail}", file=error_stream)
        return EXIT_PROBE_FAILURE
    document = build_snapshot(observations, date=datetime.now(UTC).strftime("%Y-%m-%d"))
    text = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if output is None:
        stream.write(text)
        return EXIT_OK
    try:
        output.write_text(text, encoding="utf-8")
    except OSError as error:
        print(f"USAGE cannot write {output}: {error}", file=error_stream)
        return EXIT_USAGE
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="node27_external_contract_snapshot",
        description="Read-only node-27 external-contract snapshot dump / drift check (#1089).",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dump", action="store_true", help="measure and emit the snapshot JSON")
    mode.add_argument("--check", action="store_true", help="diff live observations against the committed fixture")
    parser.add_argument("--output", type=Path, default=None, help="--dump only: write the snapshot here")
    parser.add_argument(
        "--fixture",
        type=Path,
        default=DEFAULT_FIXTURE,
        help=f"--check only: snapshot fixture to compare against (default: {DEFAULT_FIXTURE})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.check and args.output is not None:
        parser.error("--output is a --dump option; --check never writes a file")
    if args.dump:
        return run_dump(args.output, stream=sys.stdout, error_stream=sys.stderr)
    return run_check(args.fixture, stream=sys.stdout)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
