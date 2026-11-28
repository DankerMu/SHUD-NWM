"""#2910: the node-22 live user units are tracked byte-exactly with a manifest.

`infra/systemd/node22/` holds byte-exact copies of seven deployed files: six
recorded in the read-only receipt
`docs/runbooks/receipts/2026-10-16-node22-live-units-snapshot.md` and, since
#3359, the gateway's `20-runtime-path.conf` drop-in recorded in
`docs/runbooks/receipts/2026-11-26-w3-gateway-runtime-path-dropin-snapshot.md`.
Since #3427, `nhms-compute-scheduler.service` and `nhms-slurm-gateway.service`,
redeployed with a new `Description=`, are recorded again in
`docs/runbooks/receipts/2026-11-28-w3-scheduler-and-gateway-units-snapshot.md`.
The 2026-10-16 receipt also records `nhms-compute-api.service`, retired by
#3394: receipts are history and keep their rows, so the three receipts record
exactly the seven tracked files plus that one retired name, and the retired
name is neither a file under `node22/` nor a manifest entry. The receipts are
read in the order of the dates their file names start with, and where two
receipts record a path the later one holds the tracked bytes: the only paths
recorded twice are the two redeployed units, the latest receipt records
exactly those two, and each of its rows differs from the earlier row.
`infra/systemd/node22/MANIFEST.json` lists every user unit file deployed on the
compute control host (`{deployed_relative_path, tracked_path, sha256}`),
including the fourteen top-level oneshot units tracked at
`infra/systemd/<name>` (twelve until #3121) and, since #3345, the two
journal-backup units that exist only on w3, the compute role's control host.

The rule logic lives in small module-level functions so the synthetic line
cases at the bottom exercise exactly the code the real scan uses. Stdlib only,
and nothing is imported from `scripts/`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from datetime import date
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[1]
NODE22_DIR = REPO_ROOT / "infra/systemd/node22"
MANIFEST_PATH = NODE22_DIR / "MANIFEST.json"
RECEIPT_PATH = REPO_ROOT / "docs/runbooks/receipts/2026-10-16-node22-live-units-snapshot.md"
# #3359: the gateway runtime-path drop-in the 2026-10-16 snapshot did not record.
GATEWAY_DROPIN_RECEIPT_PATH = REPO_ROOT / "docs/runbooks/receipts/2026-11-26-w3-gateway-runtime-path-dropin-snapshot.md"
# #3427: the two units redeployed with a new `Description=`; this later receipt holds their tracked bytes.
REDEPLOYED_UNITS_RECEIPT_PATH = (
    REPO_ROOT / "docs/runbooks/receipts/2026-11-28-w3-scheduler-and-gateway-units-snapshot.md"
)
# Read in this order, which is the order of the dates the file names start with.
RECEIPT_PATHS = (RECEIPT_PATH, GATEWAY_DROPIN_RECEIPT_PATH, REDEPLOYED_UNITS_RECEIPT_PATH)
REDEPLOYED_UNITS = frozenset({"nhms-compute-scheduler.service", "nhms-slurm-gateway.service"})
ENV_README_PATH = REPO_ROOT / "infra/env/README.md"

MANIFEST_SCHEMA_VERSION = "nhms.node22.unit_manifest.v1"
NODE22_TRACKED_PREFIX = "infra/systemd/node22/"
SECRET_ENV_FILE = "/scratch/frd_muziyao/nhms-prod/secrets/slurm-gateway.env"
AUTHORITY_ROOT = "/scratch/frd_muziyao/"
AUTHORITY_HEADING = "## Unit → EnvironmentFile authority of the compute role (#1694)"
AUTHORITY_TOKEN_PREFIX = "NWM/infra/env/"
SECRET_NAME_SUFFIXES = ("TOKEN", "SECRET", "PASS", "PASSWORD")

NODE22_DEPLOYED_FILES = (
    "nhms-compute-scheduler.service",
    "nhms-compute-scheduler.timer",
    "nhms-compute-scheduler.service.d/10-slurm-gateway-token.conf",
    "nhms-compute-scheduler.service.d/20-runtime-path.conf",
    "nhms-slurm-gateway.service",
    "nhms-slurm-gateway.service.d/10-node22-live.conf",
    # #3359: deployed on w3, recorded by the 2026-11-26 snapshot receipt.
    "nhms-slurm-gateway.service.d/20-runtime-path.conf",
)
TOP_LEVEL_ONESHOTS = tuple(
    f"{name}.{suffix}"
    for name in (
        "nhms-node22-refresh-timer-health",
        "nhms-node22-scheduler-stall-health",
        "nhms-scheduler-evidence-retention",
        "nhms-scheduler-file-provider-refresh",
        "nhms-scheduler-journal-retention",
        "nhms-scheduler-state-index-capacity",
        "nhms-scheduler-state-index-prune",
    )
    for suffix in ("service", "timer")
)
W3_ONLY_UNITS = ("nhms-w3-journal-backup.service", "nhms-w3-journal-backup.timer")
# #3394: recorded by the 2026-10-16 receipt, no longer tracked or listed.
RETIRED_DEPLOYED_FILES = frozenset({"nhms-compute-api.service"})
EXPECTED_ENTRIES = frozenset(
    {(rel, NODE22_TRACKED_PREFIX + rel) for rel in NODE22_DEPLOYED_FILES}
    | {(name, f"infra/systemd/{name}") for name in TOP_LEVEL_ONESHOTS + W3_ONLY_UNITS}
)

_ENVIRONMENT_TOKEN = re.compile(r"""(?:[^\s"']+|"[^"]*"|'[^']*')+""")
_RECEIPT_ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*`([0-9a-f]{64})`\s*\|")
_BACKTICK_TOKEN = re.compile(r"`([^`]*)`")
_RECEIPT_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})-")


def sha256_hex(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def logical_lines(text: str) -> list[tuple[int, str]]:
    """``(first physical line number, line)`` with ``\\`` continuations joined.

    systemd concatenates a line ending in a backslash with the next one and
    replaces the backslash with a space, so a value split across lines must be
    scanned as one directive.  Comment lines (``#`` or ``;`` after leading
    whitespace) are dropped first: a comment ending in a backslash starts no
    continuation, and a comment inside a pending continuation neither joins
    nor ends it.
    """
    joined: list[tuple[int, str]] = []
    pending: str | None = None
    start = 0
    for number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith(("#", ";")):
            continue
        if pending is None:
            start, pending = number, ""
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        joined.append((start, pending + line))
        pending = None
    if pending is not None:
        joined.append((start, pending))
    return joined


def directive_value(line: str, directive: str) -> str | None:
    """Value of ``directive=`` on ``line``, or None if the line is another key.

    Split at the first ``=`` with both sides stripped, as systemd does, so
    ``Environment = X`` is the same directive as ``Environment=X``.
    """
    key, sep, value = line.partition("=")
    if not sep or key.strip() != directive:
        return None
    return value.strip()


def environment_pairs(line: str) -> list[tuple[str, str]]:
    """Every ``KEY=VALUE`` pair of an ``Environment=`` line, quotes removed.

    Only ``Environment=`` is parsed; ``EnvironmentFile=`` and any other bare
    ``NAME=value`` line yield nothing.
    """
    value = directive_value(line, "Environment")
    if value is None:
        return []
    pairs = []
    for token in _ENVIRONMENT_TOKEN.findall(value):
        unquoted = token.replace('"', "").replace("'", "")
        key, sep, assigned = unquoted.partition("=")
        if sep:
            pairs.append((key, assigned))
    return pairs


def secret_assignment_violations(line: str) -> list[str]:
    """Keys on ``line`` that assign a non-empty value to a secret-named variable."""
    return [key for key, assigned in environment_pairs(line) if key.endswith(SECRET_NAME_SUFFIXES) and assigned]


def environment_file_violation(line: str, allowed: frozenset[str]) -> str | None:
    """The offending ``EnvironmentFile=`` path on ``line``, or None if it passes."""
    value = directive_value(line, "EnvironmentFile")
    if value is None or value == "":
        return None
    path = value.removeprefix("-")
    return None if path in allowed else path


def authority_environment_files(readme_text: str) -> frozenset[str]:
    """Absolute ``NWM/infra/env/...`` paths from column 2 of the authority table.

    Only table rows inside the authority section (up to the next ``##``
    heading) count; prose elsewhere in the README, such as the retired
    ``compute.env`` notes, never widens the allowlist.
    """
    lines = readme_text.splitlines()
    start = lines.index(AUTHORITY_HEADING) + 1
    paths = set()
    for line in lines[start:]:
        if line.startswith("##"):
            break
        if not line.startswith("|"):
            continue
        cells = line.split("|")
        if len(cells) < 3:
            continue
        for token in _BACKTICK_TOKEN.findall(cells[2]):
            if token.startswith(AUTHORITY_TOKEN_PREFIX):
                paths.add(AUTHORITY_ROOT + token)
    return frozenset(paths)


def allowed_environment_files(readme_text: str) -> frozenset[str]:
    return authority_environment_files(readme_text) | {SECRET_ENV_FILE}


def receipt_sha256_table(receipt_text: str) -> dict[str, str]:
    """Receipt table: deployed relative path -> sha256, backticks stripped."""
    table = {}
    for line in receipt_text.splitlines():
        match = _RECEIPT_ROW.match(line)
        if match:
            table[match.group(1)] = match.group(2)
    return table


def union_of_receipt_tables(tables: list[dict[str, str]]) -> tuple[dict[str, str], set[str]]:
    """The receipts' rows merged, and every path recorded by more than one receipt."""
    union: dict[str, str] = {}
    overlap = set()
    for table in tables:
        overlap |= set(union) & set(table)
        union.update(table)
    return union, overlap


def receipt_tables(paths: Sequence[Path] = RECEIPT_PATHS) -> list[dict[str, str]]:
    """The receipt tables of ``paths``, in the order given."""
    return [receipt_sha256_table(path.read_text(encoding="utf-8")) for path in paths]


def receipt_date_order_violations(names: Sequence[str | Path]) -> list[str]:
    """Receipt names that break "each starts with ``YYYY-MM-DD-`` and the dates strictly increase".

    A name without a valid date is reported and does not move the date the
    next name is compared with; a name whose date is not after the last valid
    date before it (equal or earlier) is reported.
    """
    violations = []
    previous: date | None = None
    for name in names:
        match = _RECEIPT_DATE.match(PurePosixPath(name).name)
        try:
            current = date.fromisoformat(match.group(1)) if match else None
        except ValueError:
            current = None
        if current is None:
            violations.append(str(name))
            continue
        if previous is not None and current <= previous:
            violations.append(str(name))
        previous = current
    return violations


def redeploy_overlap_violations(tables: list[dict[str, str]], redeployed: frozenset[str]) -> dict[str, list[str]]:
    """How the receipts fail "only ``redeployed`` is recorded twice, and the latest receipt records exactly it".

    ``unexpected_overlap`` are paths more than one receipt records that are not
    in ``redeployed``; ``missing_overlap`` are paths of ``redeployed`` that no
    two receipts record; ``latest_extra`` and ``latest_missing`` compare the
    last table of ``tables`` with ``redeployed``.  Every list is empty when the
    receipts overlap in exactly ``redeployed`` and the latest one records
    nothing else.
    """
    _, overlap = union_of_receipt_tables(tables)
    latest = set(tables[-1]) if tables else set()
    return {
        "unexpected_overlap": sorted(overlap - redeployed),
        "missing_overlap": sorted(redeployed - overlap),
        "latest_extra": sorted(latest - redeployed),
        "latest_missing": sorted(redeployed - latest),
    }


NO_REDEPLOY_OVERLAP_VIOLATIONS = {
    "unexpected_overlap": [],
    "missing_overlap": [],
    "latest_extra": [],
    "latest_missing": [],
}


def unchanged_redeployed_rows(tables: list[dict[str, str]]) -> list[str]:
    """Paths a later receipt records with the sha256 an earlier receipt already holds for them.

    A redeploy that records the old bytes is not a redeploy: it would let the
    old bytes pass as the latest receipt's.
    """
    earlier: dict[str, str] = {}
    unchanged = set()
    for table in tables:
        unchanged |= {path for path, sha in table.items() if earlier.get(path) == sha}
        earlier.update(table)
    return sorted(unchanged)


def receipt_name_violations(recorded: set[str], tracked: set[str]) -> dict[str, list[str]]:
    """How the receipts' names fail "the tracked files plus the retired names".

    ``unrecorded`` are tracked names no receipt records; ``untracked_unretired``
    are recorded names that are neither tracked nor retired; ``retired_missing``
    are retired names the receipts no longer record or that are tracked again.
    Every list is empty when the receipts record exactly ``tracked`` plus
    ``RETIRED_DEPLOYED_FILES`` and ``tracked`` holds no retired name.
    """
    recorded_untracked = recorded - tracked
    return {
        "unrecorded": sorted(tracked - recorded),
        "untracked_unretired": sorted(recorded_untracked - RETIRED_DEPLOYED_FILES),
        "retired_missing": sorted(RETIRED_DEPLOYED_FILES - recorded_untracked),
    }


NO_RECEIPT_NAME_VIOLATIONS = {"unrecorded": [], "untracked_unretired": [], "retired_missing": []}


def load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def tracked_node22_files(root: Path = NODE22_DIR) -> list[Path]:
    """Every tracked file under ``root`` except the manifest and dotfiles (``.DS_Store``)."""
    manifest = root / MANIFEST_PATH.name
    return sorted(
        path for path in root.rglob("*") if path.is_file() and path != manifest and not path.name.startswith(".")
    )


# --- (a) manifest structure and known entries ---------------------------------


def test_manifest_shape_and_order() -> None:
    manifest = load_manifest()

    assert manifest["schema_version"] == MANIFEST_SCHEMA_VERSION
    entries = manifest["entries"]
    for entry in entries:
        assert set(entry) == {"deployed_relative_path", "tracked_path", "sha256"}, entry
        assert re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]), entry
        assert entry["tracked_path"].startswith("infra/systemd/"), entry
        for key in ("deployed_relative_path", "tracked_path"):
            assert not PurePosixPath(entry[key]).is_absolute(), entry
            assert ".." not in PurePosixPath(entry[key]).parts, entry
    keys = [entry["deployed_relative_path"].encode() for entry in entries]
    assert keys == sorted(keys), "entries must be sorted by deployed_relative_path"
    assert len(set(keys)) == len(keys), "duplicate deployed_relative_path"


def test_manifest_contains_every_known_node22_file() -> None:
    entries = {(entry["deployed_relative_path"], entry["tracked_path"]) for entry in load_manifest()["entries"]}

    assert EXPECTED_ENTRIES <= entries, sorted(EXPECTED_ENTRIES - entries)


# --- (b) manifest sha256 equals the tracked file -------------------------------


def test_manifest_sha256_matches_tracked_files() -> None:
    for entry in load_manifest()["entries"]:
        tracked = REPO_ROOT / entry["tracked_path"]
        assert tracked.is_file(), entry["tracked_path"]
        assert sha256_hex(tracked) == entry["sha256"], entry["tracked_path"]


def test_every_node22_file_is_listed_in_the_manifest() -> None:
    listed = {entry["tracked_path"] for entry in load_manifest()["entries"]}
    on_disk = {path.relative_to(REPO_ROOT).as_posix() for path in tracked_node22_files()}

    assert on_disk == {NODE22_TRACKED_PREFIX + rel for rel in NODE22_DEPLOYED_FILES}
    assert on_disk <= listed, sorted(on_disk - listed)


# --- (c) node22/ copies equal the receipt --------------------------------------


def test_the_receipts_are_listed_in_the_order_of_their_dates() -> None:
    assert receipt_date_order_violations(RECEIPT_PATHS) == []


def test_the_receipts_overlap_only_in_the_redeployed_units_and_together_equal_the_seven_and_the_retired_unit() -> None:
    tables = receipt_tables()
    snapshot, gateway_dropin = tables[:2]
    union, _ = union_of_receipt_tables(tables)

    assert len(NODE22_DEPLOYED_FILES) == 7
    assert (
        set(snapshot)
        == (set(NODE22_DEPLOYED_FILES) - {"nhms-slurm-gateway.service.d/20-runtime-path.conf"}) | RETIRED_DEPLOYED_FILES
    )
    assert set(gateway_dropin) == {"nhms-slurm-gateway.service.d/20-runtime-path.conf"}
    assert redeploy_overlap_violations(tables, REDEPLOYED_UNITS) == NO_REDEPLOY_OVERLAP_VIOLATIONS
    assert unchanged_redeployed_rows(tables) == []
    assert receipt_name_violations(set(union), set(NODE22_DEPLOYED_FILES)) == NO_RECEIPT_NAME_VIOLATIONS


def test_the_retired_unit_is_neither_a_node22_file_nor_a_manifest_entry() -> None:
    """#3394: the receipt keeps its row; the repository no longer carries the unit."""
    on_disk = {path.name for path in tracked_node22_files()}
    listed = {
        name
        for entry in load_manifest()["entries"]
        for name in (entry["deployed_relative_path"], PurePosixPath(entry["tracked_path"]).name)
    }

    assert RETIRED_DEPLOYED_FILES.isdisjoint(NODE22_DEPLOYED_FILES)
    assert RETIRED_DEPLOYED_FILES.isdisjoint(on_disk), sorted(RETIRED_DEPLOYED_FILES & on_disk)
    assert RETIRED_DEPLOYED_FILES.isdisjoint(listed), sorted(RETIRED_DEPLOYED_FILES & listed)


def test_node22_copies_match_the_receipt_table() -> None:
    tables = receipt_tables()
    # Later rows replace earlier ones: each path maps to the sha256 of the latest receipt that records it.
    receipt, _ = union_of_receipt_tables(tables)
    assert redeploy_overlap_violations(tables, REDEPLOYED_UNITS) == NO_REDEPLOY_OVERLAP_VIOLATIONS
    assert receipt_name_violations(set(receipt), set(NODE22_DEPLOYED_FILES)) == NO_RECEIPT_NAME_VIOLATIONS

    node22_entries = [
        entry for entry in load_manifest()["entries"] if entry["tracked_path"].startswith(NODE22_TRACKED_PREFIX)
    ]
    assert {entry["deployed_relative_path"] for entry in node22_entries} == set(NODE22_DEPLOYED_FILES)
    for entry in node22_entries:
        rel = entry["deployed_relative_path"]
        assert entry["tracked_path"] == NODE22_TRACKED_PREFIX + rel
        assert sha256_hex(REPO_ROOT / entry["tracked_path"]) == receipt[rel], rel
        assert entry["sha256"] == receipt[rel], rel


# --- (d) no secret value, (e) EnvironmentFile authority ------------------------


def test_node22_files_assign_no_secret_values() -> None:
    violations = [
        (path.relative_to(REPO_ROOT).as_posix(), number, key)
        for path in tracked_node22_files()
        for number, line in logical_lines(path.read_text(encoding="utf-8"))
        for key in secret_assignment_violations(line)
    ]

    assert violations == []


def test_node22_environment_files_are_authority_or_secret_paths() -> None:
    allowed = allowed_environment_files(ENV_README_PATH.read_text(encoding="utf-8"))
    violations = [
        (path.relative_to(REPO_ROOT).as_posix(), number, bad)
        for path in tracked_node22_files()
        for number, line in logical_lines(path.read_text(encoding="utf-8"))
        if (bad := environment_file_violation(line, allowed)) is not None
    ]

    assert violations == []


def test_authority_allowlist_is_read_from_the_table_only() -> None:
    allowed = authority_environment_files(ENV_README_PATH.read_text(encoding="utf-8"))

    assert {
        "/scratch/frd_muziyao/NWM/infra/env/compute.host.env",
        "/scratch/frd_muziyao/NWM/infra/env/compute.scheduler-dbfree.env",
        "/scratch/frd_muziyao/NWM/infra/env/compute.scheduler-provider-refresh.env",
    } <= allowed
    assert all(path.startswith(AUTHORITY_ROOT + AUTHORITY_TOKEN_PREFIX) for path in allowed), sorted(allowed)
    # The README prose names the retired compose-lane file; it must not leak in.
    assert "/scratch/frd_muziyao/NWM/infra/env/compute.env" not in allowed


# --- (f) synthetic line cases for the same rule functions ----------------------


def test_secret_rule_rejects_secret_named_assignments() -> None:
    assert secret_assignment_violations("Environment=SLURM_GATEWAY_TOKEN=x") == ["SLURM_GATEWAY_TOKEN"]
    assert secret_assignment_violations('Environment="FOO_PASSWORD=y"') == ["FOO_PASSWORD"]
    assert secret_assignment_violations('Environment=A=1 "DB_SECRET=z" B_PASS=w') == ["DB_SECRET", "B_PASS"]


def test_secret_rule_allows_non_secret_names_and_empty_values() -> None:
    assert secret_assignment_violations("Environment=SLURM_GATEWAY_TOKEN_FILE=/x/token") == []
    assert secret_assignment_violations("Environment=SLURM_GATEWAY_TOKEN=") == []
    assert secret_assignment_violations("Environment=slurm_gateway_token=x") == []
    assert secret_assignment_violations("Environment=SLURM_GATEWAY_SLURM_BIN_PATH=/usr/bin") == []
    # Only `Environment=` is scanned: bare assignments and EnvironmentFile= are out of scope.
    assert secret_assignment_violations("SLURM_GATEWAY_TOKEN=x") == []
    assert secret_assignment_violations("EnvironmentFile=/x/SLURM_GATEWAY_TOKEN=x") == []


def test_secret_rule_ignores_token_in_file_names() -> None:
    assert secret_assignment_violations("10-slurm-gateway-token.conf") == []
    assert (
        secret_assignment_violations("Environment=DROPIN=nhms-compute-scheduler.service.d/10-slurm-gateway-token.conf")
        == []
    )


def test_environment_file_rule_against_the_real_readme_allowlist() -> None:
    allowed = allowed_environment_files(ENV_README_PATH.read_text(encoding="utf-8"))

    assert environment_file_violation("EnvironmentFile=", allowed) is None
    assert (
        environment_file_violation("EnvironmentFile=-/scratch/frd_muziyao/NWM/infra/env/compute.host.env", allowed)
        is None
    )
    assert environment_file_violation(f"EnvironmentFile={SECRET_ENV_FILE}", allowed) is None
    assert (
        environment_file_violation("EnvironmentFile=/scratch/frd_muziyao/NWM/infra/env/compute.env", allowed)
        == "/scratch/frd_muziyao/NWM/infra/env/compute.env"
    )
    assert environment_file_violation("Environment=FOO=/anything", allowed) is None


# --- #2911: hardening deferred from the #2966 review ----------------------------


def test_directive_split_strips_spaces_around_the_first_equals() -> None:
    allowed = allowed_environment_files(ENV_README_PATH.read_text(encoding="utf-8"))

    assert secret_assignment_violations("Environment = X_TOKEN=abc") == ["X_TOKEN"]
    assert (
        environment_file_violation("EnvironmentFile = /scratch/frd_muziyao/NWM/infra/env/compute.env", allowed)
        == "/scratch/frd_muziyao/NWM/infra/env/compute.env"
    )
    # The empty reset line stays allowed with or without spaces.
    assert environment_file_violation("EnvironmentFile = ", allowed) is None


def test_continuation_lines_are_joined_before_the_secret_scan() -> None:
    text = "[Service]\nEnvironment=A=1 \\\nX_TOKEN=abc\nExecStart=/bin/true\n"
    lines = logical_lines(text)

    assert lines == [(1, "[Service]"), (2, "Environment=A=1  X_TOKEN=abc"), (4, "ExecStart=/bin/true")]
    assert [key for _, line in lines for key in secret_assignment_violations(line)] == ["X_TOKEN"]


def test_a_comment_ending_in_a_backslash_starts_no_continuation() -> None:
    text = "[Service]\n#ExecStart=/old \\\nEnvironment=X_TOKEN=abc\n"
    lines = logical_lines(text)

    assert lines == [(1, "[Service]"), (3, "Environment=X_TOKEN=abc")]
    assert [key for _, line in lines for key in secret_assignment_violations(line)] == ["X_TOKEN"]


def test_a_comment_inside_a_continuation_is_skipped_not_a_terminator() -> None:
    text = "[Service]\nEnvironment=A=1 \\\n  ; note\n# note\nX_TOKEN=abc\nExecStart=/bin/true\n"
    lines = logical_lines(text)

    assert lines == [(1, "[Service]"), (2, "Environment=A=1  X_TOKEN=abc"), (6, "ExecStart=/bin/true")]
    assert [key for _, line in lines for key in secret_assignment_violations(line)] == ["X_TOKEN"]


def test_tracked_file_walk_skips_dotfiles(tmp_path: Path) -> None:
    (tmp_path / "unit.service.d").mkdir()
    (tmp_path / "unit.service").write_text("[Unit]\n", encoding="utf-8")
    (tmp_path / "unit.service.d" / "10-x.conf").write_text("[Service]\n", encoding="utf-8")
    (tmp_path / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")
    (tmp_path / "unit.service.d" / ".DS_Store").write_bytes(b"\x00")
    (tmp_path / "MANIFEST.json").write_text("{}", encoding="utf-8")

    assert tracked_node22_files(tmp_path) == [tmp_path / "unit.service", tmp_path / "unit.service.d" / "10-x.conf"]


def test_receipt_union_reports_a_path_recorded_twice() -> None:
    first = receipt_sha256_table(f"| `a.service` | `{'0' * 64}` | t |\n| `b.service` | `{'1' * 64}` | t |\n")
    second = receipt_sha256_table(f"| `relative path` | sha256 | mtime |\n| `b.service` | `{'2' * 64}` | t |\n")

    assert union_of_receipt_tables([first, {}]) == (first, set())
    assert union_of_receipt_tables([first, second]) == ({"a.service": "0" * 64, "b.service": "2" * 64}, {"b.service"})


def test_receipt_dates_must_lead_the_names_and_strictly_increase() -> None:
    assert receipt_date_order_violations(["2026-10-16-a.md", "2026-11-26-b.md", "2026-11-28-c.md"]) == []
    assert receipt_date_order_violations([Path("x/2026-10-16-a.md"), Path("x/2026-11-28-b.md")]) == []
    # Equal dates, descending dates.
    assert receipt_date_order_violations(["2026-11-28-a.md", "2026-11-28-b.md"]) == ["2026-11-28-b.md"]
    assert receipt_date_order_violations([Path("x/2026-11-28-a.md"), Path("x/2026-10-16-b.md")]) == [
        "x/2026-10-16-b.md"
    ]
    # A name without a date, or with a date that is no calendar day, or with the date not leading the name.
    assert receipt_date_order_violations(["2026-10-16-a.md", "snapshot.md", "2026-11-28-b.md"]) == ["snapshot.md"]
    assert receipt_date_order_violations(["2026-13-01-a.md"]) == ["2026-13-01-a.md"]
    assert receipt_date_order_violations(["a-2026-10-16-b.md"]) == ["a-2026-10-16-b.md"]


def test_the_receipts_may_overlap_only_in_the_redeployed_paths() -> None:
    redeployed = frozenset({"a.service", "b.service"})
    old = {"a.service": "0" * 64, "b.service": "1" * 64, "c.service": "2" * 64}
    new = {"a.service": "3" * 64, "b.service": "4" * 64}

    assert redeploy_overlap_violations([old, new], redeployed) == NO_REDEPLOY_OVERLAP_VIOLATIONS
    # The overlap holds a path that was not redeployed.
    assert redeploy_overlap_violations([old, {"c.service": "5" * 64}, new], redeployed) == {
        **NO_REDEPLOY_OVERLAP_VIOLATIONS,
        "unexpected_overlap": ["c.service"],
    }
    # The overlap lacks a redeployed path: no earlier receipt records it.
    assert redeploy_overlap_violations([{"a.service": "0" * 64}, new], redeployed) == {
        **NO_REDEPLOY_OVERLAP_VIOLATIONS,
        "missing_overlap": ["b.service"],
    }
    # The latest receipt has a third row: a path recorded before, or a new one.
    assert redeploy_overlap_violations([old, {**new, "c.service": "5" * 64}], redeployed) == {
        **NO_REDEPLOY_OVERLAP_VIOLATIONS,
        "unexpected_overlap": ["c.service"],
        "latest_extra": ["c.service"],
    }
    assert redeploy_overlap_violations([old, {**new, "d.service": "5" * 64}], redeployed) == {
        **NO_REDEPLOY_OVERLAP_VIOLATIONS,
        "latest_extra": ["d.service"],
    }
    # The latest receipt has one row.
    assert redeploy_overlap_violations([old, {"a.service": "3" * 64}], redeployed) == {
        **NO_REDEPLOY_OVERLAP_VIOLATIONS,
        "missing_overlap": ["b.service"],
        "latest_missing": ["b.service"],
    }


def test_a_redeployed_row_must_differ_from_the_earlier_row() -> None:
    old = {"a.service": "0" * 64, "b.service": "1" * 64}

    assert unchanged_redeployed_rows([old, {"a.service": "3" * 64, "b.service": "4" * 64}]) == []
    assert unchanged_redeployed_rows([old, {"a.service": "0" * 64, "b.service": "4" * 64}]) == ["a.service"]
    # A path recorded once, even with a sha256 another path has, is not a redeploy.
    assert unchanged_redeployed_rows([old, {"c.service": "1" * 64}]) == []


def test_receipt_name_comparison_refuses_every_other_untracked_or_unrecorded_name() -> None:
    """#3394: the receipts cannot be edited, so the comparison is shown on synthetic names."""
    tracked = {"a.service", "a.service.d/10-x.conf"}
    retired = set(RETIRED_DEPLOYED_FILES)

    assert receipt_name_violations(tracked | retired, tracked) == NO_RECEIPT_NAME_VIOLATIONS
    # A receipt that records another name nobody tracks.
    assert receipt_name_violations(tracked | retired | {"b.service"}, tracked) == {
        **NO_RECEIPT_NAME_VIOLATIONS,
        "untracked_unretired": ["b.service"],
    }
    # A tracked file no receipt records.
    assert receipt_name_violations({"a.service"} | retired, tracked) == {
        **NO_RECEIPT_NAME_VIOLATIONS,
        "unrecorded": ["a.service.d/10-x.conf"],
    }
    # The retired name tracked again, or no longer recorded.
    assert receipt_name_violations(tracked | retired, tracked | retired) == {
        **NO_RECEIPT_NAME_VIOLATIONS,
        "retired_missing": sorted(retired),
    }
    assert receipt_name_violations(tracked, tracked) == {
        **NO_RECEIPT_NAME_VIOLATIONS,
        "retired_missing": sorted(retired),
    }
