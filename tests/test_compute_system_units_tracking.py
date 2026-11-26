"""#3359: the compute hosts' system-level NWM files are tracked per host.

`infra/systemd/compute-system/<host>/` (`<host>` is `w3` or `w2`) holds byte-exact
copies of the system-level files the compute role installed by hand, each at its
absolute host path below the host directory. Next to each host directory,
`<host>.sha256` is the list root on that host checks with
`sha256sum -c --strict`: one `<64 lowercase hex>  <absolute path>` line per
tracked file, LF line ends, nothing else, no path twice.

The rule logic lives in small module-level functions so the synthetic cases at
the bottom exercise exactly the code the real scan uses. Dotfiles (`.DS_Store`)
under the tree are ignored, as `tracked_node22_files` in
`tests/test_node22_unit_tracking.py` ignores them. Stdlib only, nothing imported
from `scripts/`; the lists are checked with `hashlib`, no `sha256sum` binary.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPUTE_SYSTEM_DIR = REPO_ROOT / "infra/systemd/compute-system"

MOUNTS_ENSURE_FILES = (
    "/etc/systemd/system/nhms-mounts-ensure.service",
    "/etc/systemd/system/nhms-mounts-ensure.timer",
)
SLURMD_DROPIN = "/etc/systemd/system/slurmd.service.d/10-nhms-mounts.conf"
HOST_FILES = {
    "w3": frozenset({*MOUNTS_ENSURE_FILES, SLURMD_DROPIN}),
    "w2": frozenset({*MOUNTS_ENSURE_FILES, SLURMD_DROPIN, "/etc/tmpfiles.d/nwm-smt-off.conf"}),
}
RUNS_PATH = "/scratch/frd_muziyao/nhms-prod/workspace/runs"
W3_SLURMD_MOUNTS = ("/scratch/frd_muziyao", RUNS_PATH, "/ghdc/data")
SECRET_NAME_SUFFIXES = ("TOKEN", "SECRET", "PASS", "PASSWORD")

# `sha256sum -c --strict` line shape: hash, two spaces, an absolute path.
_LIST_LINE = re.compile(r"([0-9a-f]{64})  (/\S+)")
# A variable name followed by `=`; the value is read by a lookahead so that
# `Environment=X_TOKEN=abc` yields both `Environment` and `X_TOKEN`.
_ASSIGNMENT = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*)=(?=(\S*))")


# --- module-level rule functions ----------------------------------------------


def sha256_hex(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def below(root: Path, host_path: str) -> Path:
    """``host_path`` (absolute on the host) resolved below ``root``; a bare ``root / "/etc"`` would drop ``root``."""
    return root / host_path.lstrip("/")


def list_path(host: str) -> Path:
    return COMPUTE_SYSTEM_DIR / f"{host}.sha256"


def parse_checksum_list(data: bytes) -> tuple[list[tuple[str, str]], list[str]]:
    """``([(sha256, absolute path)], problems)`` for the bytes of a ``<host>.sha256`` list.

    Split on LF only: a CR left at a line end makes the line malformed, as it
    would under ``sha256sum -c --strict``. Every problem names its line.
    """
    problems = []
    if not data.endswith(b"\n"):
        problems.append("the list does not end with LF")
    text = data.decode("utf-8", errors="replace")
    lines = text.split("\n")
    if text.endswith("\n"):
        lines = lines[:-1]
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for number, line in enumerate(lines, start=1):
        match = _LIST_LINE.fullmatch(line)
        if match is None or ".." in PurePosixPath(match.group(2)).parts:
            problems.append(f"line {number}: malformed {line!r}")
            continue
        digest, path = match.groups()
        if path in seen:
            problems.append(f"line {number}: path listed twice {line!r}")
            continue
        seen.add(path)
        entries.append((digest, path))
    return entries, problems


def tracked_host_files(host_dir: Path) -> frozenset[str]:
    """Absolute host paths of the files under ``host_dir``; dotfiles (``.DS_Store``) are skipped."""
    return frozenset(
        "/" + path.relative_to(host_dir).as_posix()
        for path in host_dir.rglob("*")
        if path.is_file() and not path.name.startswith(".")
    )


def list_coverage(listed: frozenset[str], on_disk: frozenset[str]) -> tuple[list[str], list[str]]:
    """``(files on disk the list does not name, list paths with no file on disk)``."""
    return sorted(on_disk - listed), sorted(listed - on_disk)


def checksum_mismatches(entries: list[tuple[str, str]], root: Path) -> list[str]:
    """List paths whose file below ``root`` is missing or hashes to another value."""
    mismatches = []
    for digest, path in entries:
        target = below(root, path)
        if not target.is_file():
            mismatches.append(f"{path}: missing")
        elif sha256_hex(target) != digest:
            mismatches.append(f"{path}: sha256 differs")
    return mismatches


def requires_mounts_for(text: str) -> list[str]:
    """Every whitespace-separated path of the ``RequiresMountsFor=`` lines in ``[Unit]``."""
    section = ""
    paths = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            continue
        key, sep, value = stripped.partition("=")
        if section == "Unit" and sep and key.strip() == "RequiresMountsFor":
            paths.extend(value.split())
    return paths


def secret_assignment_violations(line: str) -> list[str]:
    """Secret-named variables ``line`` assigns a non-empty value to, quotes removed.

    Any ``NAME=value`` on the line counts (``Environment=`` pairs, shell
    assignments inside ``sh -c``, comments), not only ``Environment=``.
    """
    unquoted = line.replace('"', "").replace("'", "")
    return [
        match.group(1)
        for match in _ASSIGNMENT.finditer(unquoted)
        if match.group(1).endswith(SECRET_NAME_SUFFIXES) and match.group(2)
    ]


def host_entries(host: str) -> list[tuple[str, str]]:
    entries, _ = parse_checksum_list(list_path(host).read_bytes())
    return entries


# --- the tracked tree ----------------------------------------------------------


def test_the_tree_holds_the_two_host_directories_and_their_lists() -> None:
    top = {path.name for path in COMPUTE_SYSTEM_DIR.iterdir() if not path.name.startswith(".")}

    assert top == {"w3", "w2", "w3.sha256", "w2.sha256"}


def test_each_host_directory_equals_its_list_and_the_named_set() -> None:
    for host, expected in HOST_FILES.items():
        on_disk = tracked_host_files(COMPUTE_SYSTEM_DIR / host)
        listed = frozenset(path for _, path in host_entries(host))

        unlisted, missing = list_coverage(listed, on_disk)
        assert (unlisted, missing) == ([], []), (host, unlisted, missing)
        assert on_disk == expected, (host, sorted(on_disk ^ expected))


def test_every_list_line_has_the_strict_shape() -> None:
    for host in HOST_FILES:
        _, problems = parse_checksum_list(list_path(host).read_bytes())

        assert problems == [], (host, problems)


def test_every_list_checksum_equals_the_tracked_copy() -> None:
    for host in HOST_FILES:
        assert checksum_mismatches(host_entries(host), COMPUTE_SYSTEM_DIR / host) == [], host


def test_mounts_ensure_copies_are_identical_on_both_hosts() -> None:
    for path in MOUNTS_ENSURE_FILES:
        w3_copy, w2_copy = (below(COMPUTE_SYSTEM_DIR / host, path) for host in ("w3", "w2"))

        assert w3_copy.read_bytes() == w2_copy.read_bytes(), path


def test_each_slurmd_dropin_requires_the_runs_mount() -> None:
    for host in HOST_FILES:
        mounts = requires_mounts_for(below(COMPUTE_SYSTEM_DIR / host, SLURMD_DROPIN).read_text(encoding="utf-8"))

        assert RUNS_PATH in mounts, (host, mounts)


def test_w3_slurmd_dropin_requires_scratch_runs_and_ghdc() -> None:
    mounts = requires_mounts_for(below(COMPUTE_SYSTEM_DIR / "w3", SLURMD_DROPIN).read_text(encoding="utf-8"))

    assert set(W3_SLURMD_MOUNTS) <= set(mounts), mounts


def test_no_tracked_file_assigns_a_secret_value() -> None:
    violations = [
        (host, path, number, key)
        for host in HOST_FILES
        # The named set as well, so a missing tree cannot make this scan vacuous.
        for path in sorted(HOST_FILES[host] | tracked_host_files(COMPUTE_SYSTEM_DIR / host))
        for number, line in enumerate(
            below(COMPUTE_SYSTEM_DIR / host, path).read_text(encoding="utf-8").splitlines(), start=1
        )
        for key in secret_assignment_violations(line)
    ]

    assert violations == []


def test_lists_verify_against_a_scratch_root_that_mirrors_the_host_paths(tmp_path: Path) -> None:
    """What ``sha256sum -c --strict <host>.sha256`` does on the host, with ``hashlib`` below ``tmp_path``."""
    for host in HOST_FILES:
        root = tmp_path / host
        shutil.copytree(COMPUTE_SYSTEM_DIR / host, root)
        entries, problems = parse_checksum_list(list_path(host).read_bytes())

        assert problems == []
        assert all(below(root, path).resolve().is_relative_to(root.resolve()) for _, path in entries)
        assert checksum_mismatches(entries, root) == [], host

        _, path = entries[0]
        target = below(root, path)
        target.write_bytes(target.read_bytes() + b"\n")
        assert checksum_mismatches(entries, root) == [f"{path}: sha256 differs"]


# --- synthetic cases for the same rule functions -------------------------------

_HASH_A = "a" * 64
_HASH_B = "b" * 64


def test_list_parser_accepts_the_strict_shape() -> None:
    data = f"{_HASH_A}  /etc/a.conf\n{_HASH_B}  /etc/systemd/system/b.service\n".encode()

    assert parse_checksum_list(data) == (
        [(_HASH_A, "/etc/a.conf"), (_HASH_B, "/etc/systemd/system/b.service")],
        [],
    )


def test_list_parser_names_each_malformed_line() -> None:
    bad_lines = [
        f"{_HASH_A} /etc/one-space.conf",
        f"{_HASH_A}\t/etc/tab.conf",
        f"{'A' * 64}  /etc/upper.conf",
        f"{_HASH_A[:63]}  /etc/short.conf",
        f"{_HASH_A}  etc/relative.conf",
        f"{_HASH_A} */etc/binary-marker.conf",
        f"\\{_HASH_A}  /etc/escaped.conf",
        f"{_HASH_A}  /etc/../shadow",
        f"{_HASH_A}  /etc/cr.conf\r",
        "",
        "# comment",
    ]
    for line in bad_lines:
        entries, problems = parse_checksum_list(f"{_HASH_B}  /etc/ok.conf\n{line}\n".encode())

        assert entries == [(_HASH_B, "/etc/ok.conf")], line
        assert problems == [f"line 2: malformed {line!r}"], line


def test_list_parser_requires_a_final_lf() -> None:
    entries, problems = parse_checksum_list(f"{_HASH_A}  /etc/a.conf".encode())

    assert entries == [(_HASH_A, "/etc/a.conf")]
    assert problems == ["the list does not end with LF"]


def test_list_parser_names_a_path_listed_twice() -> None:
    line = f"{_HASH_B}  /etc/a.conf"
    entries, problems = parse_checksum_list(f"{_HASH_A}  /etc/a.conf\n{line}\n".encode())

    assert entries == [(_HASH_A, "/etc/a.conf")]
    assert problems == [f"line 2: path listed twice {line!r}"]


def test_checksum_check_names_a_wrong_checksum_and_a_missing_file(tmp_path: Path) -> None:
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc/a.conf").write_bytes(b"a\n")
    good = hashlib.sha256(b"a\n").hexdigest()

    assert checksum_mismatches([(good, "/etc/a.conf")], tmp_path) == []
    assert checksum_mismatches([(_HASH_A, "/etc/a.conf")], tmp_path) == ["/etc/a.conf: sha256 differs"]
    assert checksum_mismatches([(good, "/etc/gone.conf")], tmp_path) == ["/etc/gone.conf: missing"]


def test_coverage_names_an_unlisted_file_and_a_listed_file_that_does_not_exist(tmp_path: Path) -> None:
    (tmp_path / "etc/tmpfiles.d").mkdir(parents=True)
    (tmp_path / "etc/tmpfiles.d/listed.conf").write_bytes(b"x\n")
    (tmp_path / "etc/tmpfiles.d/unlisted.conf").write_bytes(b"y\n")
    (tmp_path / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")
    (tmp_path / "etc/.DS_Store").write_bytes(b"\x00")
    on_disk = tracked_host_files(tmp_path)

    assert on_disk == {"/etc/tmpfiles.d/listed.conf", "/etc/tmpfiles.d/unlisted.conf"}
    listed = frozenset({"/etc/tmpfiles.d/listed.conf", "/etc/tmpfiles.d/absent.conf"})
    assert list_coverage(listed, on_disk) == (["/etc/tmpfiles.d/unlisted.conf"], ["/etc/tmpfiles.d/absent.conf"])


def test_requires_mounts_for_reads_whole_paths_in_unit_only() -> None:
    text = (
        "[Unit]\n"
        "# RequiresMountsFor=/commented\n"
        "RequiresMountsFor=/scratch/frd_muziyao /scratch/frd_muziyao/nhms-prod/workspace/runs-old\n"
        "RequiresMountsFor = /ghdc/data\n"
        "[Service]\n"
        f"RequiresMountsFor={RUNS_PATH}\n"
    )

    assert requires_mounts_for(text) == [
        "/scratch/frd_muziyao",
        "/scratch/frd_muziyao/nhms-prod/workspace/runs-old",
        "/ghdc/data",
    ]
    assert RUNS_PATH not in requires_mounts_for(text)


def test_secret_rule_rejects_secret_named_assignments() -> None:
    assert secret_assignment_violations("Environment=SLURM_GATEWAY_TOKEN=x") == ["SLURM_GATEWAY_TOKEN"]
    assert secret_assignment_violations('Environment="FOO_PASSWORD=y" B_PASS=w') == ["FOO_PASSWORD", "B_PASS"]
    assert secret_assignment_violations("ExecStart=/bin/sh -c 'DB_SECRET=z exec /bin/true'") == ["DB_SECRET"]
    assert secret_assignment_violations("# NHMS_SMTP_PASS=hunter2") == ["NHMS_SMTP_PASS"]


def test_secret_rule_allows_empty_values_and_other_names() -> None:
    assert secret_assignment_violations("Environment=SLURM_GATEWAY_TOKEN=") == []
    assert secret_assignment_violations("Environment=SLURM_GATEWAY_TOKEN_FILE=/x/token") == []
    assert secret_assignment_violations("Environment=slurm_gateway_token=x") == []
    assert secret_assignment_violations("w /sys/devices/system/cpu/smt/control - - - - off") == []
    assert secret_assignment_violations("ExecStart=/bin/sh -c 'mount -a -t nfs4 2>/dev/null'") == []
