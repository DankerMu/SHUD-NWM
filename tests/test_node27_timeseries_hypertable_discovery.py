"""Unit tests for the shared lifecycle hypertable discovery helper (issue #1985).

The helper is the single owner of "which hypertables does the lifecycle lane
govern": each canonical detail hypertable plus its ``_legacy`` sibling when
that sibling exists in ``timescaledb_information.hypertables``.
Eight tools consume it (compression runner, retention runner, compression
supervisor, capture, live-evidence replay validator, autopipeline statistics
guard, resource-governance collection, resource-governance audit's
policy-missing checks), so the rules live here once and every consumer test
pins that it really reads them.

The three-state expectation rule (OpenSpec change
``timeseries-narrow-store-expand-contract`` task 3.1) is the delicate part: a
canonical table WITH a sibling is key-shaped and its sibling is text-shaped; a
canonical table WITHOUT a sibling takes its entry in ``NO_SIBLING_SHAPE``, a
per-table constant that is text-shaped today and is flipped to the key shape by
that table's own CONTRACT migration (tasks 6.2 / 8.2) before the sibling is
dropped.  Nothing flips ahead of its own migration and nothing reverts after
it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from packages.common import node27_timeseries_hypertable_discovery as discovery

_ROOT = Path(__file__).resolve().parents[1]

_RIVER = ("hydro", "river_timeseries")
_RIVER_LEGACY = ("hydro", "river_timeseries_legacy")
_FORCING = ("met", "forcing_station_timeseries")
_FORCING_LEGACY = ("met", "forcing_station_timeseries_legacy")


def _row(schema: str, name: str, *, num_chunks: int = 0, compression_enabled: bool = True) -> dict:
    return {
        "hypertable_schema": schema,
        "hypertable_name": name,
        "num_chunks": num_chunks,
        "compression_enabled": compression_enabled,
    }


# ---------------------------------------------------------------------------
# Constants and naming
# ---------------------------------------------------------------------------


def test_canonical_set_is_the_two_detail_hypertables_in_catalog_order() -> None:
    assert discovery.CANONICAL_HYPERTABLES == (_RIVER, _FORCING)
    assert discovery.CANONICAL_KEYS == ("hydro.river_timeseries", "met.forcing_station_timeseries")


def test_candidate_set_is_canonical_plus_sibling_in_catalog_order() -> None:
    """Catalog order (schema, name) is what every consumer's ORDER BY produces,
    so the helper hands back the same order rather than a set."""
    assert discovery.CANDIDATE_HYPERTABLES == (_RIVER, _RIVER_LEGACY, _FORCING, _FORCING_LEGACY)


def test_legacy_sibling_and_is_legacy_are_inverse_facts() -> None:
    assert discovery.legacy_sibling(*_RIVER) == _RIVER_LEGACY
    assert discovery.legacy_sibling(*_FORCING) == _FORCING_LEGACY
    assert discovery.is_legacy("river_timeseries_legacy") is True
    assert discovery.is_legacy("river_timeseries") is False


def test_qualified_name_is_schema_dot_table() -> None:
    assert discovery.qualified(*_RIVER) == "hydro.river_timeseries"


# ---------------------------------------------------------------------------
# Discovery from catalog rows
# ---------------------------------------------------------------------------


def test_discovery_without_a_sibling_is_exactly_todays_two_tables() -> None:
    rows = [_row(*_RIVER), _row(*_FORCING)]
    assert discovery.discovery_set(rows) == (_RIVER, _FORCING)


def test_discovery_includes_a_legacy_sibling_that_exists() -> None:
    rows = [_row(*_RIVER), _row(*_RIVER_LEGACY), _row(*_FORCING)]
    assert discovery.discovery_set(rows) == (_RIVER, _RIVER_LEGACY, _FORCING)


def test_discovery_keeps_the_canonical_table_even_when_the_catalog_is_silent() -> None:
    """A canonical table missing from the catalog is a catastrophe for the
    tools that assert on it, not something discovery may silently drop: the
    set stays canonical-complete so the downstream expectation check is the
    one that raises, naming the missing table."""
    assert discovery.discovery_set([]) == (_RIVER, _FORCING)


def test_discovery_ignores_rows_outside_the_candidate_set() -> None:
    rows = [_row(*_RIVER), _row("ops", "ingest_recompute_decline"), _row("hydro", "river_timeseries_shadow")]
    assert discovery.discovery_set(rows) == (_RIVER, _FORCING)


def test_present_from_rows_reports_only_what_the_catalog_shows() -> None:
    rows = [_row(*_RIVER), _row(*_FORCING_LEGACY)]
    assert discovery.present_from_rows(rows) == (_RIVER, _FORCING_LEGACY)


# ---------------------------------------------------------------------------
# legacy_chunks mapping (the I9 / I14 contract entry gate)
# ---------------------------------------------------------------------------


def test_legacy_chunk_counts_is_absent_without_any_sibling() -> None:
    """"Present only when a sibling exists" — the retention receipt omits the
    key entirely so a no-sibling receipt stays byte-comparable with today's."""
    assert discovery.legacy_chunk_counts([_row(*_RIVER), _row(*_FORCING)]) is None


def test_legacy_chunk_counts_maps_each_sibling_to_its_total_chunk_count() -> None:
    """The gate reads ``legacy_chunks["hydro.river_timeseries_legacy"] == 0``,
    so the number must be the table's TOTAL remaining chunks — not the count
    this tick happened to drop, and not the retention-eligible subset."""
    rows = [
        _row(*_RIVER, num_chunks=14),
        _row(*_RIVER_LEGACY, num_chunks=2),
        _row(*_FORCING, num_chunks=9),
    ]
    assert discovery.legacy_chunk_counts(rows) == {"hydro.river_timeseries_legacy": 2}


def test_legacy_chunk_counts_reports_a_drained_sibling_as_zero() -> None:
    rows = [_row(*_RIVER), _row(*_RIVER_LEGACY, num_chunks=0), _row(*_FORCING)]
    assert discovery.legacy_chunk_counts(rows) == {"hydro.river_timeseries_legacy": 0}


def test_legacy_chunk_counts_covers_both_siblings() -> None:
    rows = [
        _row(*_RIVER),
        _row(*_RIVER_LEGACY, num_chunks=1),
        _row(*_FORCING),
        _row(*_FORCING_LEGACY, num_chunks=3),
    ]
    assert discovery.legacy_chunk_counts(rows) == {
        "hydro.river_timeseries_legacy": 1,
        "met.forcing_station_timeseries_legacy": 3,
    }


# ---------------------------------------------------------------------------
# Per-table expectation: the three-state rule
# ---------------------------------------------------------------------------

_RIVER_TEXT = [
    ("hydro", "river_timeseries", "run_id", 1, None, None, None),
    ("hydro", "river_timeseries", "river_network_version_id", 2, None, None, None),
    ("hydro", "river_timeseries", "river_segment_id", 3, None, None, None),
    ("hydro", "river_timeseries", "variable", None, 1, True, False),
    ("hydro", "river_timeseries", "valid_time", None, 2, True, False),
]
_FORCING_TEXT = [
    ("met", "forcing_station_timeseries", "forcing_version_id", 1, None, None, None),
    ("met", "forcing_station_timeseries", "station_id", 2, None, None, None),
    ("met", "forcing_station_timeseries", "variable", None, 1, True, False),
    ("met", "forcing_station_timeseries", "valid_time", None, 2, True, False),
]
_RIVER_KEY = [
    ("hydro", "river_timeseries", "run_key", 1, None, None, None),
    ("hydro", "river_timeseries", "river_segment_key", 2, None, None, None),
    ("hydro", "river_timeseries", "variable_e", None, 1, True, False),
    ("hydro", "river_timeseries", "valid_time", None, 2, True, False),
]
_FORCING_KEY = [
    ("met", "forcing_station_timeseries", "forcing_version_key", 1, None, None, None),
    ("met", "forcing_station_timeseries", "station_key", 2, None, None, None),
    ("met", "forcing_station_timeseries", "variable_e", None, 1, True, False),
    ("met", "forcing_station_timeseries", "valid_time", None, 2, True, False),
]
_RIVER_LEGACY_TEXT = [
    ("hydro", "river_timeseries_legacy", name, sb, ob, asc, nf)
    for _s, _t, name, sb, ob, asc, nf in _RIVER_TEXT
]
_FORCING_LEGACY_TEXT = [
    ("met", "forcing_station_timeseries_legacy", name, sb, ob, asc, nf)
    for _s, _t, name, sb, ob, asc, nf in _FORCING_TEXT
]


def test_expectation_without_any_sibling_is_todays_nine_text_shaped_rows() -> None:
    """Must-preserve: on today's node-27 catalog the expectation is byte-for-byte
    the one the supervisor and the live-evidence validator already assert."""
    assert discovery.compression_settings_expectation(discovery.CANONICAL_KEYS) == [
        *_RIVER_TEXT,
        *_FORCING_TEXT,
    ]


def test_river_flips_to_key_shaped_only_when_its_own_sibling_appears() -> None:
    present = (*discovery.CANONICAL_KEYS, "hydro.river_timeseries_legacy")
    assert discovery.compression_settings_expectation(present) == [
        *_RIVER_KEY,
        *_RIVER_LEGACY_TEXT,
        *_FORCING_TEXT,
    ]


def test_forcing_flips_independently_of_river() -> None:
    """I12 must need no code change here: the forcing key shape is already
    encoded, and river stays text-shaped while its own sibling is absent."""
    present = (*discovery.CANONICAL_KEYS, "met.forcing_station_timeseries_legacy")
    assert discovery.compression_settings_expectation(present) == [
        *_RIVER_TEXT,
        *_FORCING_KEY,
        *_FORCING_LEGACY_TEXT,
    ]


def test_both_siblings_present_flips_both_tables() -> None:
    present = (
        "hydro.river_timeseries",
        "hydro.river_timeseries_legacy",
        "met.forcing_station_timeseries",
        "met.forcing_station_timeseries_legacy",
    )
    assert discovery.compression_settings_expectation(present) == [
        *_RIVER_KEY,
        *_RIVER_LEGACY_TEXT,
        *_FORCING_KEY,
        *_FORCING_LEGACY_TEXT,
    ]


def test_no_sibling_shape_is_todays_text_shape_for_both_tables() -> None:
    """The flip is a constant, and today it is set to text for both tables — so
    #1985 changed no expectation on the live catalog."""
    assert discovery.NO_SIBLING_SHAPE[_RIVER] == discovery._TEXT_SHAPE[_RIVER]
    assert discovery.NO_SIBLING_SHAPE[_FORCING] == discovery._TEXT_SHAPE[_FORCING]
    assert set(discovery.NO_SIBLING_SHAPE) == set(discovery.CANONICAL_HYPERTABLES)


def test_post_contract_river_is_red_today_and_green_once_the_constant_flips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The catalog AFTER the river contract migration: the sibling is gone and
    the canonical table is physically key-shaped.

    Sibling presence alone cannot answer this — the catalog looks exactly like
    today's — so the expectation must come from the per-table constant. Red
    here today is correct and is the pin task 6.2 satisfies by flipping the
    river entry to the key shape and deploying that BEFORE the DROP.
    """

    post_contract = [*_RIVER_KEY, *_FORCING_TEXT]
    assert discovery.compression_settings_expectation(discovery.CANONICAL_KEYS) != post_contract

    monkeypatch.setitem(discovery.NO_SIBLING_SHAPE, _RIVER, discovery._KEY_SHAPE[_RIVER])
    assert discovery.compression_settings_expectation(discovery.CANONICAL_KEYS) == post_contract


def test_flipping_river_does_not_flip_forcing(monkeypatch: pytest.MonkeyPatch) -> None:
    """One constant per contract: I9 must not drag the forcing table with it."""
    monkeypatch.setitem(discovery.NO_SIBLING_SHAPE, _RIVER, discovery._KEY_SHAPE[_RIVER])
    assert discovery.compression_settings_expectation(discovery.CANONICAL_KEYS) == [
        *_RIVER_KEY,
        *_FORCING_TEXT,
    ]


def test_a_sibling_still_wins_over_the_no_sibling_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """During the expand window the sibling decides; the constant is only the
    answer to "no sibling in the catalog"."""
    monkeypatch.setitem(discovery.NO_SIBLING_SHAPE, _RIVER, discovery._KEY_SHAPE[_RIVER])
    present = (*discovery.CANONICAL_KEYS, "hydro.river_timeseries_legacy")
    assert discovery.compression_settings_expectation(present) == [
        *_RIVER_KEY,
        *_RIVER_LEGACY_TEXT,
        *_FORCING_TEXT,
    ]


def test_expectation_rows_are_in_catalog_order() -> None:
    """The supervisor orders by (schema, name, segmentby idx NULLS LAST,
    orderby idx NULLS LAST); the expectation must be produced in that order or
    the exact-list comparison fails for a reason that is not drift."""
    rows = discovery.compression_settings_expectation(
        (*discovery.CANONICAL_KEYS, "hydro.river_timeseries_legacy")
    )
    keys = [
        (schema, name, 1 if segmentby is None else 0, segmentby or 0, orderby or 0)
        for schema, name, _att, segmentby, orderby, _asc, _nf in rows
    ]
    assert keys == sorted(keys)


def test_expected_hypertable_flags_require_compression_on_every_present_table() -> None:
    present = (*discovery.CANONICAL_KEYS, "hydro.river_timeseries_legacy")
    assert discovery.expected_hypertable_flags(present) == {
        "hydro.river_timeseries": True,
        "hydro.river_timeseries_legacy": True,
        "met.forcing_station_timeseries": True,
    }


def test_expectation_refuses_a_catalog_missing_a_canonical_table() -> None:
    """A missing canonical table is never "the legacy set" — it is the failure
    the lane exists to shout about."""
    with pytest.raises(ValueError, match="canonical"):
        discovery.compression_settings_expectation(("met.forcing_station_timeseries",))


def test_expectation_refuses_an_unknown_table() -> None:
    with pytest.raises(ValueError, match="unknown"):
        discovery.compression_settings_expectation((*discovery.CANONICAL_KEYS, "hydro.river_ts_v2"))


# ---------------------------------------------------------------------------
# SQL fragments
# ---------------------------------------------------------------------------


def test_discovery_sql_reads_only_the_hypertable_catalog() -> None:
    sql = discovery.DISCOVERY_SQL
    assert "timescaledb_information.hypertables" in sql
    assert "num_chunks" in sql
    for schema, name in discovery.CANDIDATE_HYPERTABLES:
        assert f"'{schema}', '{name}'" in sql or f"'{schema}','{name}'" in sql
    # Catalog-only: no fact-table scan may hide in the discovery probe.
    assert "hydro.river_timeseries" not in sql.replace("'hydro', 'river_timeseries'", "")


def test_candidate_tuple_list_sql_is_a_compact_in_list() -> None:
    assert discovery.candidate_tuple_list_sql() == (
        "(('hydro','river_timeseries'),('hydro','river_timeseries_legacy'),"
        "('met','forcing_station_timeseries'),('met','forcing_station_timeseries_legacy'))"
    )


def test_candidate_in_list_sql_is_indentable_for_block_queries() -> None:
    rendered = discovery.candidate_in_list_sql(indent="    ")
    assert rendered.splitlines() == [
        "    ('hydro', 'river_timeseries'),",
        "    ('hydro', 'river_timeseries_legacy'),",
        "    ('met', 'forcing_station_timeseries'),",
        "    ('met', 'forcing_station_timeseries_legacy')",
    ]


# ---------------------------------------------------------------------------
# Single-owner pin: every consumer must read the helper, not its own literals
# ---------------------------------------------------------------------------

_CONSUMERS = (
    "scripts/node27_timeseries_compression.py",
    "scripts/node27_timeseries_retention.py",
    "scripts/node27_timeseries_compression_supervisor.py",
    "scripts/node27_timeseries_compression_capture.py",
    "scripts/node27_timeseries_compression_live_evidence.py",
    "scripts/node27_autopipeline.py",
    "packages/common/node27_cold_governance_collection.py",
    # #1985 round-1: the audit's own policy-missing checks were still keyed on
    # two bare table names after the collection side had been converted.
    "scripts/node27_resource_governance.py",
)


@pytest.mark.parametrize("relative", _CONSUMERS)
def test_every_lifecycle_consumer_imports_the_shared_helper(relative: str) -> None:
    """One helper, eight call sites. A tool that re-derives the set from its own
    literals is exactly the second code change D7 promises not to need."""
    source = (_ROOT / relative).read_text(encoding="utf-8")
    # The IMPORT STATEMENT, not the bare module name: a comment naming the
    # helper used to satisfy the substring form of this pin, which would have
    # let a consumer keep its own literals and still look converted.
    assert (
        "from packages.common.node27_timeseries_hypertable_discovery import" in source
    ), relative


def _documented_consumers() -> list[str]:
    """Consumer paths the helper's own docstring lists, in bullet form.

    Bullets only: the module docstring also names
    ``node27_timeseries_compression_capture.py`` in prose, as the canonical-only
    ownership probe, and that mention is not a consumer entry.
    """
    doc = discovery.__doc__ or ""
    return re.findall(r"^\* ``([^`]+)`` —", doc, flags=re.MULTILINE)


@pytest.mark.parametrize("relative", _CONSUMERS)
def test_the_helper_docstring_names_every_consumer(relative: str) -> None:
    """The docstring is the map a maintainer reads before adding the ninth
    consumer; a stale one sends them looking for seven call sites when there
    are eight."""
    assert relative in _documented_consumers(), relative


def test_the_helper_docstring_names_exactly_the_consumers_it_has() -> None:
    """Count as well as membership: a path left behind after a consumer is
    removed is the same stale map in the other direction."""
    documented = _documented_consumers()
    assert len(documented) == len(_CONSUMERS)
    assert set(documented) == set(_CONSUMERS)
    assert "Eight call sites" in (discovery.__doc__ or "")


def test_the_ci_selector_comment_carries_the_current_consumer_count() -> None:
    """Round-3 review: the third place the count is written down.

    `scripts/select_ci_tests.py` explains WHY the helper needs an explicit
    fan-out rule by citing the consumer count. Round-1 added the eighth
    consumer and updated the helper docstring and `_CONSUMERS`, but not this
    comment — so the file a maintainer reads when deciding whether the rule
    still covers everything claimed seven.
    """
    source = (_ROOT / "scripts" / "select_ci_tests.py").read_text(encoding="utf-8")
    assert "SEVEN consumers" not in source
    assert "EIGHT consumers" in source
    assert len(_CONSUMERS) == 8
    # Round-4: the fourth place the count is written down — THIS file's own
    # module docstring, which round-1 left at seven while adding the eighth.
    assert "Eight tools consume it" in (__doc__ or "")
    assert "Seven" not in (__doc__ or "")
