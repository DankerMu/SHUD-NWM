"""Focused #2224 contract for the bounded G0/G1 retry fence."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "tier-node27-timeseries-storage.md"
SECTION_START = "## #1895 controlled live rollout"
NEXT_SECTION = "## Timer cadence order (UTC)"
GATE_ORDER = ("G0", "G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _section_from_text(text: str) -> str:
    start = text.index(SECTION_START)
    end = text.index(NEXT_SECTION)
    return text[start:end]


def _gate_from_section(section: str, gate: str) -> str:
    start = section.index(f"### {gate} ")
    candidates = [
        section.index(f"### {candidate} ") for candidate in GATE_ORDER if section.find(f"### {candidate} ") > start
    ]
    return section[start : min(candidates) if candidates else len(section)]


def _assert_origin_chunk_retry_fence(text: str) -> None:
    section = _section_from_text(text)
    g0 = _gate_from_section(section, "G0")
    g1 = _gate_from_section(section, "G1")
    normalized = _norm(section)
    assert "#2224" in g0
    assert "#2224 origin-chunk parity must merge before a G1 retry" in normalized
    assert "a8db554d6402bec642e9a05627eae64b2b79aec3" in normalized
    assert re.search(r"G1 census/bracket.{0,80}finite parity timeout", normalized)
    assert re.search(r"produced no census, capacity policy, or valid-times.{0,20}baseline", normalized)
    assert re.search(r"fresh.{0,80}G0.{0,80}(new|merged).{0,80}SHA", normalized, flags=re.IGNORECASE)
    assert re.search(r"durable.{0,80}origin", _norm(g1), flags=re.IGNORECASE)
    expected_parity_source = (
        "its one aggregate reads the exact quoted origin relation, never the parent "
        "hypertable or current compressed sibling"
    )
    assert expected_parity_source in _norm(g1)
    assert "finite timeout remains unchanged" in normalized


def test_g0_g1_origin_chunk_retry_fence_is_bounded_and_load_bearing() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    _assert_origin_chunk_retry_fence(text)
    for old, new in (
        ("#2224 origin-chunk parity must merge before a G1 retry", "#2224 origin-chunk parity may merge after G1"),
        ("a8db554d6402bec642e9a05627eae64b2b79aec3", "b" * 40),
        (
            "produced no census, capacity policy, or valid-times\n> baseline",
            "produced reusable census, capacity policy, and valid-times\n> baseline",
        ),
        (
            "relation, never the parent\nhypertable or current compressed sibling",
            "relation, the parent hypertable and current compressed sibling",
        ),
    ):
        mutant = text.replace(old, new, 1)
        assert mutant != text, old
        with pytest.raises(AssertionError):
            _assert_origin_chunk_retry_fence(mutant)
