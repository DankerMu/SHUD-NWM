"""#2451 §2.1: the C1/C2 spike switch, and the default that must not move.

``tasks.md`` §2.1 needs C1 and C2 measured across design.md's condition cross
product before either is committed to. Both therefore live behind one
environment switch read at render time
(``packages/common/forecast_store.py``: ``NWM_RIVER_TS_SEGMENT_SPIKE``), so the
bench can measure all three variants in one process against one seeded database.

The load-bearing claim is the DEFAULT one: with the variable unset — and with it
set to ``""`` — the segment-rows source renders byte-identically to the
pre-spike tree. It is pinned by sha256 taken from the tree at ``bba67ec85``
BEFORE the switch was added, for each of the five pushdown fragments the eight
call sites actually use. A hash is the right pin here because the golden fixture
records ``sql_chains`` — WHERE/ON/HAVING predicates — and would not notice a
changed SELECT list, a moved conjunct or reflowed whitespace, all of which the
two candidates do.
"""

from __future__ import annotations

import hashlib

import pytest

from packages.common import forecast_store
from packages.common.forecast_store import (
    SEGMENT_SPIKE_C1,
    SEGMENT_SPIKE_C2,
    SEGMENT_SPIKE_ENV_VAR,
    SEGMENT_SPIKE_VARIANTS,
    _segment_rows_source_sql,
    _segment_rows_source_template,
)

#: The five fragments the eight call sites pass into the shared source
#: (``forecast_store.py`` :729, :759, :792, :825, :898, :950, :986, :1024).
_FRAGMENTS: dict[str, str] = {
    "empty": "",
    "bound_run": forecast_store._BOUND_RUN_PUSHDOWN_SQL,
    "resolved_run": forecast_store._RESOLVED_RUN_PUSHDOWN_SQL,
    "resolved_run_cycle_window": (
        forecast_store._RESOLVED_RUN_PUSHDOWN_SQL + forecast_store._CYCLE_WINDOW_PUSHDOWN_SQL
    ),
    "analysis_scenario": forecast_store._ANALYSIS_SCENARIO_PUSHDOWN_SQL,
    "run_type": forecast_store._RUN_TYPE_PUSHDOWN_SQL,
}

#: sha256 of ``_segment_rows_source_sql(fragment)`` captured on ``bba67ec85``,
#: before the spike switch existed. Re-capturing these is a deliberate edit on a
#: reviewed line, never a side effect of the switch.
_PRE_SPIKE_SHA256: dict[str, str] = {
    "empty": "c92f5fbf6cba58c7aa65ca54140c0ff6d04d08e349c2bfff7a69db300efbc060",
    "bound_run": "cec77a496b8ac524d931ea9e20a153d405887084505114ccd9025990e7ed2b78",
    "resolved_run": "4848572d1b2e4d59735b36ece1707ab3629eda0e43421b4b596e120b020d6ea7",
    "resolved_run_cycle_window": "dea450958907ff3fc28c47bece57e36a15874ddcb893be48a2cf3339781c3744",
    "analysis_scenario": "c357299e32f9d29e822cce9f281032b521573ad0fe4f591e592c8163f0313bf5",
    "run_type": "e9e0eb8fb1bf909b79b56bdfc502314f828aa7a461f516014e233764c104bcb6",
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The default
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label", sorted(_FRAGMENTS))
def test_the_unset_rendering_is_byte_identical_to_the_pre_spike_tree(
    label: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(SEGMENT_SPIKE_ENV_VAR, raising=False)
    assert _sha(_segment_rows_source_sql(_FRAGMENTS[label])) == _PRE_SPIKE_SHA256[label], (
        f"{label}: the default segment-rows rendering moved. The spike's default must be the pre-spike "
        "bytes; if this changed for a reason other than the spike, re-capture the pin deliberately"
    )


@pytest.mark.parametrize("label", sorted(_FRAGMENTS))
def test_the_empty_value_and_the_unset_variable_render_the_same_bytes(
    label: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``""`` is base, and it is the same base the unset variable selects."""
    monkeypatch.delenv(SEGMENT_SPIKE_ENV_VAR, raising=False)
    unset = _segment_rows_source_sql(_FRAGMENTS[label])
    monkeypatch.setenv(SEGMENT_SPIKE_ENV_VAR, "")
    assert _segment_rows_source_sql(_FRAGMENTS[label]) == unset


def test_the_registered_template_the_golden_reads_is_unchanged_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``forecast_store:segment_rows_source`` is sourced from this function.

    ``tests/river_ts_template_registry.py:224`` calls it, and the golden suite
    renders whatever it returns. The switch must be invisible there unless it is
    set.
    """
    monkeypatch.delenv(SEGMENT_SPIKE_ENV_VAR, raising=False)
    for store in ("legacy", "narrow"):
        template = _segment_rows_source_template(store)
        assert "AND rt.basin_version_key = (\n" in template, store
        assert "AND rt.river_network_version_key = (\n" in template, store
        assert "IS NOT DISTINCT FROM" not in template, store
        assert template.splitlines()[1].startswith(
            "SELECT rt.run_key, rt.river_network_version_key, rt.valid_time, rt.value, rt.unit_e"
        ), store


def test_an_unrecognised_variant_is_refused_rather_than_rendered_as_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A silent fallback would let a bench run measure base under a C3 label."""
    monkeypatch.setenv(SEGMENT_SPIKE_ENV_VAR, "c3")
    with pytest.raises(ValueError, match="is not a #2451 spike variant"):
        _segment_rows_source_sql()
    monkeypatch.setenv(SEGMENT_SPIKE_ENV_VAR, "C1")
    with pytest.raises(ValueError, match="is not a #2451 spike variant"):
        _segment_rows_source_sql()


def test_the_variant_set_is_exactly_base_c1_c2() -> None:
    assert SEGMENT_SPIKE_VARIANTS == ("", "c1", "c2")
    assert SEGMENT_SPIKE_ENV_VAR == "NWM_RIVER_TS_SEGMENT_SPIKE"


# ---------------------------------------------------------------------------
# C1 — the two conjuncts become non-sargable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label", sorted(_FRAGMENTS))
def test_c1_rewrites_both_conjuncts_on_both_branches_and_nothing_else(
    label: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(SEGMENT_SPIKE_ENV_VAR, raising=False)
    base = _segment_rows_source_sql(_FRAGMENTS[label])
    monkeypatch.setenv(SEGMENT_SPIKE_ENV_VAR, SEGMENT_SPIKE_C1)
    spiked = _segment_rows_source_sql(_FRAGMENTS[label])

    assert spiked != base
    # One template renders both branches (design.md F1c), so both are rewritten.
    assert spiked.count("rt.basin_version_key IS NOT DISTINCT FROM (") == 2
    assert spiked.count("rt.river_network_version_key IS NOT DISTINCT FROM (") == 2
    assert "rt.basin_version_key = (" not in spiked
    assert "rt.river_network_version_key = (" not in spiked
    # The predicate is EQUIVALENT, not weaker: both columns are NOT NULL, and the
    # authority sub-selects are untouched.
    assert spiked.count("SELECT basin_version_key FROM core.basin_version") == 2
    assert spiked.count("SELECT river_network_version_key FROM core.river_network_version") == 2
    # Undoing exactly C1's substitution reproduces the base rendering byte for
    # byte, which is what pins "and nothing else".
    assert spiked.replace(" IS NOT DISTINCT FROM (", " = (") == base


def test_c1_keeps_the_segment_key_and_the_transitional_aids(monkeypatch: pytest.MonkeyPatch) -> None:
    """C1 must not touch the conjunct #2451 exists to protect, nor the aids."""
    monkeypatch.setenv(SEGMENT_SPIKE_ENV_VAR, SEGMENT_SPIKE_C1)
    spiked = _segment_rows_source_sql(forecast_store._BOUND_RUN_PUSHDOWN_SQL)
    assert spiked.count("rt.river_segment_key = (") == 2
    assert "AND rt.river_segment_id = %(river_segment_id)s" in spiked


# ---------------------------------------------------------------------------
# C2 — the two conjuncts move out of the branch scan
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label", sorted(_FRAGMENTS))
def test_c2_removes_both_conjuncts_from_every_branch(label: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SEGMENT_SPIKE_ENV_VAR, SEGMENT_SPIKE_C2)
    spiked = _segment_rows_source_sql(_FRAGMENTS[label])
    branches = spiked.split("\nUNION ALL\n")
    assert len(branches) == 2
    for branch in branches:
        assert "rt.basin_version_key = (" not in branch
        assert "rt.river_network_version_key = (" not in branch
        # The segment key, the enum and the per-call-site pushdown stay INSIDE.
        assert "rt.river_segment_key = (" in branch
        assert "rt.variable_e = 'q_down'::hydro.river_variable" in branch


def test_c2_verifies_both_identities_in_the_layer_above_the_union(monkeypatch: pytest.MonkeyPatch) -> None:
    """The predicates survive — outside the branch scan, on the authority tables.

    Spelled as joins rather than as an outer ``WHERE``: a ``WHERE`` predicate on
    a pulled-up ``UNION ALL`` is distributed back onto every child by
    ``set_append_rel_size``, which would land the conjunct back in the branch
    scan and make C2 indistinguishable from base.
    """
    monkeypatch.setenv(SEGMENT_SPIKE_ENV_VAR, SEGMENT_SPIKE_C2)
    spiked = _segment_rows_source_sql()
    assert "JOIN core.basin_version spike_bv" in spiked
    assert "AND spike_bv.basin_version_id = %(basin_version_id)s" in spiked
    assert "JOIN core.river_network_version spike_rnv" in spiked
    assert "AND spike_rnv.river_network_version_id = %(river_network_version_id)s" in spiked
    assert "WHERE rt_src." not in spiked


def test_c2_projects_the_same_five_columns_the_callers_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """The eight call sites alias this source ``rt`` and name its columns.

    ``basin_version_key`` is added to the BRANCH projection only, because the
    outer layer has to see it; the source's own output columns are unchanged, so
    no caller needs editing.
    """
    monkeypatch.delenv(SEGMENT_SPIKE_ENV_VAR, raising=False)
    base_columns = ("run_key", "river_network_version_key", "valid_time", "value", "unit_e")
    monkeypatch.setenv(SEGMENT_SPIKE_ENV_VAR, SEGMENT_SPIKE_C2)
    spiked = _segment_rows_source_sql()
    outer = spiked.split("\nFROM (\n", 1)[0]
    assert outer.replace("\n", " ").split("SELECT", 1)[1].split() == [
        f"rt_src.{name}," if name != base_columns[-1] else f"rt_src.{name}" for name in base_columns
    ]
    assert spiked.count("rt.basin_version_key,") == 2


# ---------------------------------------------------------------------------
# Drift
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("variant", [SEGMENT_SPIKE_C1, SEGMENT_SPIKE_C2])
def test_a_template_edit_that_moves_a_conjunct_refuses_instead_of_rendering_base(
    variant: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exact-substring surgery must fail loudly, never silently no-op."""
    monkeypatch.setattr(
        forecast_store,
        "_SEGMENT_ROWS_SOURCE_SQL",
        forecast_store._SEGMENT_ROWS_SOURCE_SQL.replace("rt.basin_version_key", "rt.bv_key"),
    )
    monkeypatch.setenv(SEGMENT_SPIKE_ENV_VAR, variant)
    with pytest.raises(ValueError, match="no longer contains"):
        _segment_rows_source_sql()
