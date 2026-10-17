"""#2910: the node-22 live user units are tracked byte-exactly with a manifest.

`infra/systemd/node22/` holds byte-exact copies of the seven deployed files in
the read-only receipt `docs/runbooks/receipts/2026-10-16-node22-live-units-snapshot.md`,
and `infra/systemd/node22/MANIFEST.json` lists every file deployed on node-22
(`{deployed_relative_path, tracked_path, sha256}`), including the twelve
top-level oneshot units tracked at `infra/systemd/<name>`.

The rule logic lives in small module-level functions so the synthetic line
cases at the bottom exercise exactly the code the real scan uses. Stdlib only,
and nothing is imported from `scripts/`.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[1]
NODE22_DIR = REPO_ROOT / "infra/systemd/node22"
MANIFEST_PATH = NODE22_DIR / "MANIFEST.json"
RECEIPT_PATH = REPO_ROOT / "docs/runbooks/receipts/2026-10-16-node22-live-units-snapshot.md"
ENV_README_PATH = REPO_ROOT / "infra/env/README.md"

MANIFEST_SCHEMA_VERSION = "nhms.node22.unit_manifest.v1"
NODE22_TRACKED_PREFIX = "infra/systemd/node22/"
SECRET_ENV_FILE = "/scratch/frd_muziyao/nhms-prod/secrets/slurm-gateway.env"
AUTHORITY_ROOT = "/scratch/frd_muziyao/"
AUTHORITY_HEADING = "## node-22 unit → EnvironmentFile authority (#1694)"
AUTHORITY_TOKEN_PREFIX = "NWM/infra/env/"
SECRET_NAME_SUFFIXES = ("TOKEN", "SECRET", "PASS", "PASSWORD")

NODE22_DEPLOYED_FILES = (
    "nhms-compute-api.service",
    "nhms-compute-scheduler.service",
    "nhms-compute-scheduler.timer",
    "nhms-compute-scheduler.service.d/10-slurm-gateway-token.conf",
    "nhms-compute-scheduler.service.d/20-runtime-path.conf",
    "nhms-slurm-gateway.service",
    "nhms-slurm-gateway.service.d/10-node22-live.conf",
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
    )
    for suffix in ("service", "timer")
)
EXPECTED_ENTRIES = frozenset(
    {(rel, NODE22_TRACKED_PREFIX + rel) for rel in NODE22_DEPLOYED_FILES}
    | {(name, f"infra/systemd/{name}") for name in TOP_LEVEL_ONESHOTS}
)

_ENVIRONMENT_TOKEN = re.compile(r"""(?:[^\s"']+|"[^"]*"|'[^']*')+""")
_RECEIPT_ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*`([0-9a-f]{64})`\s*\|")
_BACKTICK_TOKEN = re.compile(r"`([^`]*)`")


def sha256_hex(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def logical_lines(text: str) -> list[tuple[int, str]]:
    """``(first physical line number, line)`` with ``\\`` continuations joined.

    systemd concatenates a line ending in a backslash with the next one and
    replaces the backslash with a space, so a value split across lines must be
    scanned as one directive.
    """
    joined: list[tuple[int, str]] = []
    pending: str | None = None
    start = 0
    for number, line in enumerate(text.splitlines(), start=1):
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


def test_node22_copies_match_the_receipt_table() -> None:
    receipt = receipt_sha256_table(RECEIPT_PATH.read_text(encoding="utf-8"))
    assert set(receipt) == set(NODE22_DEPLOYED_FILES), sorted(set(receipt) ^ set(NODE22_DEPLOYED_FILES))

    node22_entries = [
        entry for entry in load_manifest()["entries"] if entry["tracked_path"].startswith(NODE22_TRACKED_PREFIX)
    ]
    assert {entry["deployed_relative_path"] for entry in node22_entries} == set(NODE22_DEPLOYED_FILES)
    for entry in node22_entries:
        rel = entry["deployed_relative_path"]
        assert entry["tracked_path"] == NODE22_TRACKED_PREFIX + rel
        assert sha256_hex(REPO_ROOT / entry["tracked_path"]) == receipt[rel], rel


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


def test_tracked_file_walk_skips_dotfiles(tmp_path: Path) -> None:
    (tmp_path / "unit.service.d").mkdir()
    (tmp_path / "unit.service").write_text("[Unit]\n", encoding="utf-8")
    (tmp_path / "unit.service.d" / "10-x.conf").write_text("[Service]\n", encoding="utf-8")
    (tmp_path / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")
    (tmp_path / "unit.service.d" / ".DS_Store").write_bytes(b"\x00")
    (tmp_path / "MANIFEST.json").write_text("{}", encoding="utf-8")

    assert tracked_node22_files(tmp_path) == [tmp_path / "unit.service", tmp_path / "unit.service.d" / "10-x.conf"]
