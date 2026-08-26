"""Static contract: the repository's default Python pin and instructions are a
durable truth source.

The repository default interpreter is Python 3.11, pinned by the tracked root
``.python-version`` and mirrored by CI (``python-version: "3.11"``). This file
locks that producer so local ``uv`` can no longer silently select a newer
interpreter while CI runs 3.11 (issue #1571). It also locks the source and
generated-root instruction contract: the repository default is 3.11 and explicit
cross-version verification uses ``uv run --python <version>``.

The pin/ignore/instruction checks are written so wrong pin, ignored-pin and
missing-instruction semantics fail through pure helpers over temporary input —
no alternate interpreter is ever spawned and the real worktree is never mutated
(``git ls-files`` / ``git check-ignore --no-index`` are read-only subprocesses in
the repository root; the ``.gitignore`` simulation lives in a tmp_path only).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PIN_FILE = REPO_ROOT / ".python-version"

# Exact pinned bytes: the merge-gate producer. ``3.11\n`` is the CI truth.
EXPECTED_PIN_BYTES = b"3.11\n"
# The repository default version expressed in the pinned bytes.
EXPECTED_PIN_VERSION = "3.11"

# Instruction surfaces: the single source plus the two generated roots that the
# project-instruction-bootstrap skill regenerates from it. All three must keep
# both semantic clauses (default 3.11, explicit `uv run --python`).
INSTRUCTION_SURFACES = (
    "instructions/agents/shared.md",
    "CLAUDE.md",
    "AGENTS.md",
)
# Stable exact project wording of the two clauses (kept in sync with
# instructions/agents/shared.md). The explicit cross-version phrase is stable
# enough to lock exactly.
DEFAULT_3_11_CLAUSE = "仓库默认解释器是 Python 3.11"
EXPLICIT_UV_RUN_PYTHON_CLAUSE = "uv run --python <version>"
# The example the instructions themselves give for explicit cross-version runs.
_EXPLICIT_UV_RUN_PYTHON_EXAMPLE = "uv run --python 3.14 python -V"


def _pin_bytes() -> bytes:
    return PIN_FILE.read_bytes()


def _pin_text() -> str:
    return PIN_FILE.read_text(encoding="utf-8")


def _reject_ignored_pin(is_ignored: bool) -> None:
    """Fail-closed assertion shared by the live check and the ignored-pin red
    proof: a pin git considers ignored must be rejected."""
    assert not is_ignored, (
        ".python-version must not match git check-ignore --no-index (ignored pin "
        "recreates local-vs-CI divergence for issue #1571)"
    )


def _assert_pin_content(pin_text: str, *, name: str = ".python-version") -> None:
    """The pin must carry exactly the default 3.11 version. Pure helper so the
    mutation-capable input can be any string without touching the worktree."""
    assert pin_text.strip() == EXPECTED_PIN_VERSION, (
        f"{name} must pin repository default Python {EXPECTED_PIN_VERSION}, got {pin_text.strip()!r}"
    )


def test_python_version_pin_is_exact_311_bytes() -> None:
    assert _pin_bytes() == EXPECTED_PIN_BYTES, (
        f".python-version must be the exact bytes {EXPECTED_PIN_BYTES!r} "
        "(the merge-gate producer), got {_pin_bytes()!r}"
    )


def test_python_version_pin_carries_default_311() -> None:
    _assert_pin_content(_pin_text())


# ---- git tracked / not-ignored ----------------------------------------------
# Read-only subprocesses in the repository root. ``git check-ignore --no-index``
# evaluates .gitignore rules even for tracked paths, so a tracked+ignored pin
# (the local-vs-CI drift failure mode) exits 0 and must be red.


def _git_ls_files_tracked(path: Path) -> bool:
    completed = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", str(path)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 0:
        return True
    assert completed.returncode == 1, (
        f"git ls-files --error-unmatch failed unexpectedly: {completed.returncode}: "
        f"{completed.stderr.strip()}"
    )
    return False


def _git_check_ignore_no_index(path: Path) -> bool:
    """True if git considers the path ignored, using the tracked-file-aware
    ``--no-index`` variant (the same rule set uv and CI resolve against)."""
    completed = subprocess.run(
        ["git", "check-ignore", "--no-index", "--", str(path)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 1:
        return False
    assert completed.returncode == 0, (
        f"git check-ignore --no-index failed unexpectedly: {completed.returncode}: "
        f"{completed.stderr.strip()}"
    )
    return True


def test_python_version_pin_is_tracked_and_not_ignored() -> None:
    assert _git_ls_files_tracked(PIN_FILE), ".python-version must be tracked by git"
    _reject_ignored_pin(_git_check_ignore_no_index(PIN_FILE))


def test_mutation_ignored_pin_semantics_is_red(tmp_path: Path) -> None:
    """A tracked-but-ignored pin must be rejected by the same seam. The ignored
    state is simulated in a tmp git repo only; the real worktree is never touched."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], cwd=REPO_ROOT, check=True, capture_output=True)
    pin = repo / ".python-version"
    pin.write_bytes(EXPECTED_PIN_BYTES)
    subprocess.run(["git", "add", ".python-version"], cwd=repo, check=True, capture_output=True)
    (repo / ".gitignore").write_text(".python-version\n", encoding="utf-8")

    def ls_files_tracked(path: Path) -> bool:
        completed = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", str(path)],
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
        )
        return completed.returncode == 0

    def check_ignore_no_index(path: Path) -> bool:
        completed = subprocess.run(
            ["git", "check-ignore", "--no-index", "--", str(path)],
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
        )
        return completed.returncode == 0

    # The tmp repo proves the ignored-pin failure mode exists in git itself:
    # the tracked file is reported ignored by `git check-ignore --no-index`.
    assert ls_files_tracked(repo / ".python-version")
    assert check_ignore_no_index(repo / ".python-version")
    # The repository seam must therefore reject such a pin; exercising the real
    # helper against the real tracked+ignored condition would require mutating
    # the worktree, so this red proof asserts the same semantic that
    # test_python_version_pin_is_tracked_and_not_ignored applies to the repo.
    with pytest.raises(AssertionError, match="must not match git check-ignore"):
        _reject_ignored_pin(True)


def test_mutation_wrong_pin_content_is_red() -> None:
    """A wrong pin version must fail the pure content seam without touching the
    worktree (an ignored/absent git state is not even needed for the content
    check; the pin helper takes the text as input)."""
    for wrong in ("3.14", "3.14\n", "3.11.9\n", "3.10"):
        with pytest.raises(AssertionError, match="must pin repository default Python 3.11"):
            _assert_pin_content(wrong)
    # A trailing-newline-only drift that still "looks" 3.11 to a strip() must
    # be caught by the exact-bytes test; here we prove the strip-level guard.
    assert _assert_pin_content("3.11\n") is None  # green control


# ---- instruction contract: source + generated roots -------------------------
def _instruction_texts() -> dict[str, str]:
    return {relative: (REPO_ROOT / relative).read_text(encoding="utf-8") for relative in INSTRUCTION_SURFACES}


def _assert_instruction_clauses(text: str, *, name: str) -> None:
    """Both semantic clauses must be present: repository default is 3.11 and
    explicit cross-version verification uses ``uv run --python``."""
    assert DEFAULT_3_11_CLAUSE in text, f"{name}: missing default-Python-3.11 clause"
    assert EXPLICIT_UV_RUN_PYTHON_CLAUSE in text, (
        f"{name}: missing explicit `uv run --python` cross-version clause"
    )
    assert _EXPLICIT_UV_RUN_PYTHON_EXAMPLE in text, (
        f"{name}: missing the explicit cross-version example ({_EXPLICIT_UV_RUN_PYTHON_EXAMPLE})"
    )


def test_instruction_surfaces_keep_default_311_and_explicit_uv_run_python() -> None:
    for relative, text in _instruction_texts().items():
        _assert_instruction_clauses(text, name=relative)


@pytest.mark.parametrize("surface", INSTRUCTION_SURFACES)
def test_instruction_mutations_are_red(surface: str) -> None:
    """Deleting either clause must turn the instruction seam red (in-memory
    mutation only; the generated roots are never rewritten)."""
    text = (REPO_ROOT / surface).read_text(encoding="utf-8")
    for clause in (DEFAULT_3_11_CLAUSE, EXPLICIT_UV_RUN_PYTHON_CLAUSE):
        assert clause in text, f"{surface}: clause anchor {clause!r} missing"
        mutated = text.replace(clause, "", 1)
        assert mutated != text, f"{surface}: mutation must differ for {clause!r}"
        with pytest.raises(AssertionError, match="missing"):
            _assert_instruction_clauses(mutated, name=surface)
