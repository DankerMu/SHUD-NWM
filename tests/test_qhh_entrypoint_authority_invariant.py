"""QHH current invocation authority (live entrypoint headers).

The QHH diagnostic chain runs only from a detached diagnostic worktree with that
worktree's exact interpreter; the authority for how to invoke it is the Run
Boundary in ``scripts/diagnostic/qhh/README.md``. The two live QHH entrypoint
headers must carry their exact pointer to that README, must not use the stale
"documented bring-up invocation" phrase, and must name no runbook that does not
exist: every repository-relative ``*.md`` path a header cites has to resolve
under the repository root, so a pointer to a deleted runbook turns red.

This dedicated file holds the authority seam apart from
``tests/test_qhh_scripts_static.py``: one green contract test (with a vacuity
guard on the path extractor), six in-memory red mutations each pinned to the
assertion it targets, and a non-degenerate mutant check.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

_CYCLE = "scripts/run_qhh_cycle.sh"
_CONTINUOUS = "scripts/run_qhh_continuous.py"
_README = "scripts/diagnostic/qhh/README.md"
# A made-up runbook path the mutants cite; it must never exist.
_MISSING_RUNBOOK = "docs/runbooks/qhh-retired-bringup-example.md"


def _qhh_live_entrypoint_texts() -> dict[str, str]:
    """Full text of the two live QHH entrypoints."""
    return {
        _CYCLE: (REPO_ROOT / _CYCLE).read_text(encoding="utf-8"),
        _CONTINUOUS: (REPO_ROOT / _CONTINUOUS).read_text(encoding="utf-8"),
    }


# The exact current-authority pointer sentence required in each live header. It
# is anchored per file (exact wording + line break) so a bare "Run boundary:"
# README mention elsewhere cannot satisfy the seam, and deleting the authority
# sentence is a genuine red.
def _qhh_required_authority(relative: str) -> str:
    if relative == _CYCLE:
        return "Current\n# invocation authority is scripts/diagnostic/qhh/README.md (Run Boundary)."
    if relative == _CONTINUOUS:
        return "Current invocation authority is\n``scripts/diagnostic/qhh/README.md`` (Run Boundary)."
    raise AssertionError(f"unknown live QHH entrypoint: {relative}")


# Stale invocation language that must never appear in a live entrypoint.
_STALE_INVOCATION_PHRASE = "documented bring-up invocation"

# A repository-relative ``*.md`` path: plain in ``# `` shell comments, or inside
# double backticks in the Python docstring (backticks are not path characters).
_MD_PATH_RE = re.compile(r"(?<![\w./-])((?:[\w.-]+/)*[\w-][\w.-]*\.md)(?![\w/-])")


def _qhh_live_header(relative: str, text: str) -> str:
    """The header of a live entrypoint: the leading ``#`` comment block of the
    shell script (after the shebang), or the module docstring of the Python one."""
    if relative.endswith(".sh"):
        header: list[str] = []
        for line in text.splitlines()[1:]:
            if line.startswith("#"):
                header.append(line)
            elif header and line.strip():
                break
        return "\n".join(header)
    if relative.endswith(".py"):
        return ast.get_docstring(ast.parse(text), clean=False) or ""
    raise AssertionError(f"unknown live QHH entrypoint: {relative}")


def _qhh_header_md_paths(relative: str, text: str) -> list[str]:
    """Every repository-relative ``*.md`` path the live header cites, in order."""
    return _MD_PATH_RE.findall(_qhh_live_header(relative, text))


def _assert_qhh_current_authority(
    relative: str,
    text: str,
    *,
    required_authority: str,
    repo_root: Path = REPO_ROOT,
) -> None:
    """A live header names no runbook that does not exist, carries its exact
    current-authority pointer to the README Run Boundary, and never uses the
    stale "documented bring-up invocation" phrase."""
    missing = [path for path in _qhh_header_md_paths(relative, text) if not (repo_root / path).is_file()]
    assert not missing, f"live QHH entrypoint header cites a runbook that does not exist: {missing}"
    assert required_authority in text, (
        "live QHH entrypoint must point current invocation authority at the "
        "diagnostic README Run Boundary"
    )
    assert _STALE_INVOCATION_PHRASE not in text, (
        "live QHH entrypoint must not cite the stale 'documented bring-up invocation' phrase"
    )


def test_qhh_live_entrypoints_cite_diagnostic_readme_as_current_authority() -> None:
    for relative, text in _qhh_live_entrypoint_texts().items():
        # Vacuity guard: an extractor that finds nothing would pass the
        # existence check trivially.
        assert _README in _qhh_header_md_paths(relative, text), (
            f"{relative}: header path extractor must find the {_README} citation"
        )
        _assert_qhh_current_authority(relative, text, required_authority=_qhh_required_authority(relative))
        assert "Run Boundary" in text, f"{relative}: must name the README Run Boundary"


_EXISTENCE_RED = r"cites a runbook that does not exist: \['docs/runbooks/qhh-retired-bringup-example\.md'\]"
_AUTHORITY_RED = r"must point current invocation authority at the diagnostic README Run Boundary"
_STALE_RED = r"must not cite the stale 'documented bring-up invocation' phrase"


def _qhh_authority_mutants() -> dict[str, str]:
    """In-memory regressions against the current-authority seam (parent untouched);
    each must turn the validator red on the assertion it targets."""
    texts = _qhh_live_entrypoint_texts()
    cycle, continuous = texts[_CYCLE], texts[_CONTINUOUS]
    cycle_authority = _qhh_required_authority(_CYCLE)
    continuous_authority = _qhh_required_authority(_CONTINUOUS)
    return {
        "cycle_restores_pointer_to_deleted_runbook": cycle.replace(
            cycle_authority,
            f"Current\n# invocation authority is {_MISSING_RUNBOOK} (Run Boundary).",
        ),
        "continuous_restores_pointer_to_deleted_runbook": continuous.replace(
            continuous_authority,
            f"Current invocation authority is\n``{_MISSING_RUNBOOK}`` (Run Boundary).",
        ),
        "cycle_deletes_current_pointer": cycle.replace(cycle_authority, ""),
        "continuous_deletes_current_pointer": continuous.replace(continuous_authority, ""),
        "cycle_cites_stale_phrase": cycle.replace(
            cycle_authority,
            f"{cycle_authority}\n# See the README for the {_STALE_INVOCATION_PHRASE}.",
        ),
        "continuous_cites_stale_phrase": continuous.replace(
            continuous_authority,
            f"{continuous_authority}\nSee the README for the {_STALE_INVOCATION_PHRASE}.",
        ),
    }


@pytest.mark.parametrize(
    ("mutation", "expected_red"),
    [
        pytest.param(mutation, expected_red, id=mutation)
        for mutation, expected_red in (
            ("cycle_restores_pointer_to_deleted_runbook", _EXISTENCE_RED),
            ("continuous_restores_pointer_to_deleted_runbook", _EXISTENCE_RED),
            ("cycle_deletes_current_pointer", _AUTHORITY_RED),
            ("continuous_deletes_current_pointer", _AUTHORITY_RED),
            ("cycle_cites_stale_phrase", _STALE_RED),
            ("continuous_cites_stale_phrase", _STALE_RED),
        )
    ],
)
def test_qhh_live_entrypoint_authority_mutations_are_red(mutation: str, expected_red: str) -> None:
    """Pointing a header at a deleted runbook, deleting the current README
    pointer, or restoring the stale phrase turns the seam red on its own check."""
    source = _qhh_authority_mutants()[mutation]
    relative = _CYCLE if mutation.startswith("cycle_") else _CONTINUOUS
    with pytest.raises(AssertionError, match=expected_red):
        _assert_qhh_current_authority(relative, source, required_authority=_qhh_required_authority(relative))


def test_qhh_authority_mutants_cover_each_mutation() -> None:
    """The authority battery is non-degenerate: every mutant differs from its
    source, and the made-up runbook the mutants cite does not exist."""
    texts = _qhh_live_entrypoint_texts()
    for mutation, mutant in _qhh_authority_mutants().items():
        relative = _CYCLE if mutation.startswith("cycle_") else _CONTINUOUS
        assert mutant != texts[relative], f"{mutation}: mutant must differ from {relative}"
    assert not (REPO_ROOT / _MISSING_RUNBOOK).exists()
