"""QHH current invocation authority (live entrypoint headers).

The two live QHH entrypoint headers must point current invocation guidance only
at the diagnostic README Run Boundary, never at the historical bring-up baseline
(``docs/runbooks/qhh-22-business-bringup.md`` §3). The historical runbook is
retained evidence; routing an operator there as "the documented bring-up
invocation" lets the active-root
``uv run python scripts/run_qhh_continuous.py`` recipe escape the detached
exact-interpreter boundary before #1831.

This dedicated file keeps ``tests/test_qhh_scripts_static.py`` under the
repository 1000-line threshold while preserving the authority seam (one green
contract test, four in-memory red mutations, and non-degenerate mutant checks).
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _qhh_live_entrypoint_texts() -> dict[str, str]:
    """Current-invocation text of the two live QHH entrypoints."""
    return {
        "scripts/run_qhh_cycle.sh": (REPO_ROOT / "scripts/run_qhh_cycle.sh").read_text(encoding="utf-8"),
        "scripts/run_qhh_continuous.py": (
            REPO_ROOT / "scripts/run_qhh_continuous.py"
        ).read_text(encoding="utf-8"),
    }


# The exact current-authority pointer sentence required in each live header. It
# is anchored per file (exact wording + line break) so a bare "Run boundary:"
# README mention elsewhere cannot satisfy the seam, and deleting the authority
# sentence is a genuine red.
def _qhh_required_authority(relative: str) -> str:
    if relative == "scripts/run_qhh_cycle.sh":
        return "# invocation authority is scripts/diagnostic/qhh/README.md (Run Boundary); the"
    if relative == "scripts/run_qhh_continuous.py":
        return "Current invocation authority is\n``scripts/diagnostic/qhh/README.md`` (Run Boundary);"
    raise AssertionError(f"unknown live QHH entrypoint: {relative}")


# Historical-baseline and stale invocation language that must not be used as the
# current invocation pointer in the live headers.
_HISTORICAL_BASELINE = "qhh-22-business-bringup.md"
_HISTORICAL_SECTION = "§3"
_STALE_INVOCATION_PHRASE = "documented bring-up invocation"
# Disclaimer that lets a live header name the historical baseline as retained
# evidence without turning it into current invocation guidance.
_RETAINED_EVIDENCE_DISCLAIMER = "retained evidence only"


def _assert_qhh_current_authority(text: str, *, required_authority: str) -> None:
    """The live header must carry its exact current-authority pointer to the
    diagnostic README Run Boundary and must not use historical language as the
    current invocation. The stale "documented bring-up invocation" phrase is
    rejected unconditionally. ``§3`` and the baseline filename may appear only
    inside a "retained evidence only" disclaimer (an operator pointed at ``§3``
    without that disclaimer lands on the active-root ``uv run`` recipe).
    """
    assert required_authority in text, (
        "live QHH entrypoint must point current invocation authority at the "
        "diagnostic README Run Boundary"
    )
    assert _STALE_INVOCATION_PHRASE not in text, (
        "live QHH entrypoint must not cite the stale 'documented bring-up "
        "invocation' phrase; the historical bring-up baseline is retained "
        "evidence only"
    )
    if _HISTORICAL_SECTION in text or _HISTORICAL_BASELINE in text:
        assert _RETAINED_EVIDENCE_DISCLAIMER in text, (
            "live QHH entrypoint may name the historical baseline (§3) only while "
            "disclaiming it as retained evidence, never as current invocation "
            "guidance"
        )


def test_qhh_live_entrypoints_cite_diagnostic_readme_as_current_authority() -> None:
    for relative, text in _qhh_live_entrypoint_texts().items():
        _assert_qhh_current_authority(text, required_authority=_qhh_required_authority(relative))
        assert "Run Boundary" in text, f"{relative}: must name the README Run Boundary"


def _qhh_authority_mutants() -> dict[str, str]:
    """In-memory regressions against the current-authority seam (parent untouched);
    each must turn the validator red."""
    cycle = (REPO_ROOT / "scripts/run_qhh_cycle.sh").read_text(encoding="utf-8")
    continuous = (REPO_ROOT / "scripts/run_qhh_continuous.py").read_text(encoding="utf-8")
    cycle_authority = _qhh_required_authority("scripts/run_qhh_cycle.sh")
    continuous_authority = _qhh_required_authority("scripts/run_qhh_continuous.py")
    return {
        "cycle_restores_historical_pointer": cycle.replace(
            cycle_authority,
            "# See docs/runbooks/qhh-22-business-bringup.md §3 for the documented bring-up invocation.",
        ),
        "continuous_restores_historical_pointer": continuous.replace(
            continuous_authority,
            "See\n``docs/runbooks/qhh-22-business-bringup.md`` §3 for the documented bring-up invocation.",
        ),
        "cycle_deletes_current_pointer": cycle.replace(cycle_authority, ""),
        "continuous_deletes_current_pointer": continuous.replace(continuous_authority, ""),
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "cycle_restores_historical_pointer",
        "continuous_restores_historical_pointer",
        "cycle_deletes_current_pointer",
        "continuous_deletes_current_pointer",
    ],
)
def test_qhh_live_entrypoint_authority_mutations_are_red(mutation: str) -> None:
    """Restoring the historical §3 pointer or deleting the current README pointer
    must turn the current-authority seam red."""
    source = _qhh_authority_mutants()[mutation]
    relative = "scripts/run_qhh_cycle.sh" if mutation.startswith("cycle_") else "scripts/run_qhh_continuous.py"
    with pytest.raises(AssertionError):
        _assert_qhh_current_authority(source, required_authority=_qhh_required_authority(relative))


def test_qhh_authority_mutants_cover_each_mutation() -> None:
    """The authority battery is non-degenerate: every mutant differs from its source."""
    cycle = (REPO_ROOT / "scripts/run_qhh_cycle.sh").read_text(encoding="utf-8")
    continuous = (REPO_ROOT / "scripts/run_qhh_continuous.py").read_text(encoding="utf-8")
    mutants = _qhh_authority_mutants()
    assert mutants["cycle_restores_historical_pointer"] != cycle
    assert mutants["cycle_deletes_current_pointer"] != cycle
    assert mutants["continuous_restores_historical_pointer"] != continuous
    assert mutants["continuous_deletes_current_pointer"] != continuous
