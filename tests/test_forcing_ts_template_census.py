"""The forcing discovery-set census (#1990, task 7.2a).

What this module is FOR
-----------------------

``tests/test_river_ts_text_identity_cleanup.py`` and
``tests/test_river_ts_read_path_surrogate_keys.py`` both say, in as many words,
that nothing in the river oracles sweeps the repository: their censuses assert a
declared file list against itself, so a fact-table read in an unlisted file is
invisible to every assertion they make. ``surrogate_keys.py:929`` names the fix —
"that is the I11 discovery-set census (tasks 7.2a)" — and this is it, for the
forcing fact table.

The census answers one question per mention of ``met.forcing_station_timeseries``
anywhere under ``packages/ workers/ scripts/ services/ apps/ db/``: is it a
REGISTERED read template (rendered per store through
``packages.common.forcing_ts_render``) or is it on an explicit EXEMPTION list with
a named owner? Anything that is neither is red.

WHAT IS AND IS NOT PROVEN HERE — READ BEFORE TRUSTING A GREEN RUN
------------------------------------------------------------------

Cut (a) (task 7.2a) landed this census with an EMPTY register, which made its
closure assertion vacuous in the ``registered`` term. **Cut (b) (task 7.2) wired
all nine readers**, so:

* **the closure assertion bites on every SPELLING, and on nothing else.** Every
  mention the sweep finds must be either a registered template's own or an
  exempt row's, and ``set(discovered) == set(FORCING_TABLE_CENSUS)`` is what
  catches a wired reader that goes back to spelling the table name: its file
  re-enters the discovered set and the equality fails. What it does NOT catch,
  and a green run must not be read as catching, is a new TEMPLATE: D1's token
  means every registered entry carries ``mentions=0``, so ``registered + exempt
  == mentions`` is really ``exempt == mentions`` and the ``registered`` term
  constrains nothing. River keeps that grip because its entries carry
  ``mentions=1``; the forcing side buys it back structurally instead —
  :func:`test_every_forcing_template_pair_in_the_tree_is_registered` walks the
  same files for ``ForcingTemplatePair`` construction sites and demands the
  register name every one of them. A tenth pair beside the nine is red there,
  which is also what stops it from silently skipping ``test_i1_i2_*``,
  ``test_i5_*``, the byte-identity pins and the ``params`` claim.
* **the five wired reader files are GONE from the census, by design.** Their
  templates carry :data:`~packages.common.forcing_ts_render.FORCING_TABLE_TOKEN`
  rather than the name, so ``packages/common/display_coverage.py``,
  ``packages/common/best_available.py``,
  ``workers/model_registry/qhh_production_bootstrap.py`` and
  ``scripts/reset_qhh_smoke_db.py`` score 0 and drop out, and
  ``packages/common/forecast_store.py`` falls 7 → 2 (its two surviving mentions
  are the index/catalog metadata payloads, which are not SQL). Closure for the
  four departed files is ``0 + 0 == 0`` — trivially true, and the reason the
  weight of the assertion moved onto the renderer's own two constants and onto
  the exempt files. This is the execution split in ``fixtures/I11-1990.md``
  playing out exactly as recorded, not a census that quietly went blind.
* **what a green run still does NOT prove.** Invariant I7 — that a cross-store
  reader composes both rendered fact-row subrelations inside itself before any
  outer aggregate — is unprovable until task 7.3's migration exists. The narrow
  variants here are registered, text-pinned and **never executed**. Nothing in
  this suite or its siblings executes SQL.

Two known limits of the counter, both measured, both recorded rather than fixed
(the river census records the second one for itself at
``tests/test_river_ts_text_identity_cleanup.py:360-375``):

1. **grep over-counts by three files.** ``packages/common/timescale_write_guard.py``,
   ``scripts/node27_timeseries_compression.py`` and
   ``scripts/node27_timeseries_retention.py`` contain the qualified name and score
   **0** parsed — every one of their mentions is in a docstring or a ``#``
   comment. They are correctly absent from the declared census, and a reviewer
   reconciling this file against ``grep -rn`` must expect exactly that gap.
2. **split constants are invisible, and the blind spot is IN the sweep domain.**
   A file spelling the schema and the table as separate string constants —
   ``("met", "forcing_station_timeseries")`` — cannot match a qualified-spelling
   regex. The census cannot see such a site and therefore cannot force it into the
   register. That is the boundary of the closure claim, and it is not an academic
   one: at least six PRODUCTION files under the discovery roots are shaped that
   way — ``packages/common/timescale_write_guard.py:70``,
   ``packages/common/node27_timeseries_discovery.py:5``,
   ``services/production_closure/readonly_db_types.py:191``,
   ``scripts/node27_timeseries_compression_supervisor.py:1735-1738``,
   ``scripts/node27_timeseries_compression_live_evidence.py:1887-1890`` and
   ``scripts/node27_timeseries_compression_capture.py:392``. All six were read
   individually: every one is a catalog / hypertable-identity / permission probe
   that names the relation and reads none of its fact columns, so no READ escapes
   the census today. The next reader must re-check the shape, not this sentence.

There is deliberately NO third limit for the sweep's file selection: every ``.py``
file under the roots is parsed, because no raw-text prefilter can be a superset of
a counter that reads folded constant values
(:func:`test_the_sweep_parses_every_file_instead_of_prefiltering_on_raw_text`).
"""

from __future__ import annotations

from importlib import import_module

import pytest

from packages.common.forcing_ts_render import FORCING_STORES, render_forcing_ts_sql
from tests.forcing_ts_template_registry import (
    DISCOVERY_ROOTS,
    EXEMPT_MENTIONS,
    FORCING_REGISTRY,
    NON_READ_MENTIONS,
    RENDERER_CONSTANTS,
    REPO_ROOT,
    UNWIRED_READERS,
    discover_forcing_mentions,
    discover_forcing_template_pairs,
    exempt_by_path,
    forcing_table_mentions,
    registered_by_path,
    registered_template_pairs,
)

# ---------------------------------------------------------------------------
# The measured discovery set, RE-MEASURED after the nine readers were wired.
#
# Cut (a) declared sixteen files and 49 mentions: `fixtures/I11-1990.md` C3's
# measured 15 files / 47 mentions (9 of them reader sites) plus the renderer
# module D1 introduces, which contributes exactly two (`FORCING_TABLE` and
# `FORCING_TABLE_LEGACY`).
#
# Wiring moved the table name out of the readers and into those two constants,
# so the nine reader mentions are gone and four files left the discovery set
# altogether. 49 − 9 = 40 across 12 files. The five reader files went to
# 0 / 0 / 0 / 0 / 2 BY DESIGN — see the module docstring and the execution split
# in the fixture. `packages/common/forecast_store.py` keeps 2 because its two
# index/catalog metadata payloads name the table as DATA, not as SQL, and are
# exempt with `7.3 (I12) index pins` as their owner.
# ---------------------------------------------------------------------------
FORCING_TABLE_CENSUS: dict[str, int] = {
    "db/seeds/seed_demo.py": 3,
    "packages/common/forcing_domain_handoff.py": 3,
    "packages/common/forcing_domain_handoff_apply.py": 17,
    "packages/common/forcing_ts_render.py": 2,
    "packages/common/forecast_store.py": 2,
    "packages/common/node27_container_contract.py": 1,
    "scripts/node27_autopipeline.py": 1,
    "scripts/node27_timeseries_compression_capture.py": 2,
    "scripts/node27_timeseries_compression_live_evidence.py": 1,
    "services/production_closure/two_node_e2e_readonly_db_lane.py": 2,
    "workers/forcing_producer/file_store.py": 2,
    "workers/forcing_producer/store.py": 5,
}

#: C3's measured PRE-WIRING totals, pinned separately from the census dict so the
#: two cannot be "reconciled" by editing one of them. They are the fixture's
#: numbers and do not move; what moves is the arithmetic that has to reproduce
#: them from the post-wiring state (see
#: :func:`test_the_declared_census_reproduces_the_fixture_baseline`).
FIXTURE_BASELINE_FILES = 15
FIXTURE_BASELINE_MENTIONS = 47
FIXTURE_BASELINE_READER_MENTIONS = 9
FIXTURE_BASELINE_EXEMPT_MENTIONS = 38

#: Mentions #1991 (task 7.3) ADDED, declared so the baseline arithmetic above
#: keeps reproducing C3's measured numbers instead of being quietly re-agreed.
#: Exactly one: the narrow writer in
#: ``packages/common/forcing_domain_handoff_apply.py`` resolves station surrogate
#: keys in Python, and the shape-conflict reason it raises when a station has no
#: ``met.met_station`` row names the fact table as the ``table`` field of that
#: reason payload. It is a name, not SQL, and it joins the file's
#: "handoff protocol table-name key" exemption row.
#:
#: The twelve write-side mentions did NOT move: task 7.3 converted those
#: statements to narrow, key-predicated SQL in place, so the counts are unchanged
#: and only the exemption rows' notes are.
TASK_73_ADDED_MENTIONS = 1

#: The five files whose forcing readers this task wired. Four of them left the
#: discovery set outright; `forecast_store.py` stayed for its two non-SQL index
#: payloads. Named explicitly so the "went vacuous by design" claim is a pin and
#: not a sentence in a PR body.
WIRED_READER_FILES: tuple[str, ...] = (
    "packages/common/best_available.py",
    "packages/common/display_coverage.py",
    "packages/common/forecast_store.py",
    "scripts/reset_qhh_smoke_db.py",
    "workers/model_registry/qhh_production_bootstrap.py",
)

#: The nine registered reader keys, path-sorted exactly as the register lists
#: them. A frozen list beside a derived one is usually waste; here it is the
#: only thing that can catch a reader being dropped from the register and its
#: exemption row never being restored, which the closure equality alone cannot
#: see (both terms fall together).
REGISTERED_READER_KEYS: tuple[str, ...] = (
    "best_available.forcing_inputs",
    "display_coverage.station_sample_rows",
    "forecast_store.forcing_readiness_overall",
    "forecast_store.forcing_readiness_variable_rows",
    "forecast_store.latest_product_station_source",
    "forecast_store.station_forcing_membership",
    "forecast_store.station_series_rows",
    "reset_qhh_smoke_db.forcing_timeseries_delete",
    "qhh_production_bootstrap.dynamic_forcing_count",
)


def _assert_census_closes(discovered: dict[str, int]) -> None:
    """Every discovered mention is registered or exempt, and nothing else is declared.

    Factored out of the test so the guard can be exercised against a planted
    offender (:func:`test_the_census_rejects_an_unregistered_unexempted_site`)
    rather than only against a tree that is green by construction.
    """
    assert set(discovered) == set(FORCING_TABLE_CENSUS), (
        "the discovery sweep and the declared census must list the same files; "
        f"only discovered: {sorted(set(discovered) - set(FORCING_TABLE_CENSUS))}; "
        f"only declared: {sorted(set(FORCING_TABLE_CENSUS) - set(discovered))}"
    )
    registered = registered_by_path()
    exempt = exempt_by_path()
    for path, mentions in sorted(discovered.items()):
        assert mentions == FORCING_TABLE_CENSUS[path], (
            f"{path}: {mentions} qualified mentions, census says {FORCING_TABLE_CENSUS[path]}. "
            "Register the new statement through the forcing renderer (or give it an exemption row "
            "with a named owner) before updating this number."
        )
        assert registered.get(path, 0) + exempt.get(path, 0) == mentions, (
            f"{path}: {registered.get(path, 0)} mentions in registered templates + "
            f"{exempt.get(path, 0)} declared exempt mentions != {mentions} in the source"
        )


def _assert_every_pair_is_registered(discovered: dict[str, tuple[str, ...]]) -> None:
    """Every ``ForcingTemplatePair`` under the discovery roots is a registered one.

    Factored out for the same reason :func:`_assert_census_closes` is: the live
    tree is green by construction, so the only way to show this guard BITES is to
    hand it a planted offender.
    """
    registered = registered_template_pairs()
    found = {(path, name) for path, names in discovered.items() for name in names}
    declared = {(path, name) for path, names in registered.items() for name in names}
    assert found == declared, (
        "every ForcingTemplatePair constructed under the discovery roots must be in FORCING_REGISTRY; "
        f"unregistered: {sorted(found - declared)}; "
        f"registered but not found in the tree: {sorted(declared - found)}"
    )


def test_every_forcing_template_pair_in_the_tree_is_registered() -> None:
    """The exhaustiveness guard D1 owes the census.

    ``registered + exempt == mentions`` cannot see a new template any more —
    every entry carries ``mentions=0`` — so this is the assertion that forces a
    tenth pair into the register, and through it into the shape oracles
    (``test_i1_i2_*``, ``test_i3_i4_*``, ``test_i5_*``, ``params``) and the M6
    AST sweep, whose ``WIRED_READER_PATHS`` is itself DERIVED from the register
    and therefore blind to a sixth file on its own.

    Task 7.3 is where this stops being hypothetical: the write-side row-count
    payloads (``forcing_ts_render.py`` docstring, "become templates in task
    7.3") land in ``workers/forcing_producer/store.py`` and
    ``packages/common/forcing_domain_handoff_apply.py``, neither of which the
    register covers today.
    """
    _assert_every_pair_is_registered(discover_forcing_template_pairs())


def test_the_registered_nine_are_exactly_the_pairs_the_tree_holds() -> None:
    """The per-file shape of the same equality, which the set form flattens away.

    Nine pairs across five files, five of them in ``forecast_store.py``. Pinned
    per file because "the register names nine things" and "the tree holds nine
    pairs in the files the register names" are different claims, and only the
    second one is what the guard above rests on.
    """
    assert discover_forcing_template_pairs() == {
        "packages/common/best_available.py": ("_FORCING_INPUTS_TEMPLATES",),
        "packages/common/display_coverage.py": ("_STATION_SAMPLE_ROWS_TEMPLATES",),
        "packages/common/forecast_store.py": (
            "_FORCING_READINESS_OVERALL_TEMPLATES",
            "_FORCING_READINESS_VARIABLE_ROWS_TEMPLATES",
            "_LATEST_PRODUCT_STATION_SOURCE_TEMPLATES",
            "_STATION_FORCING_MEMBERSHIP_TEMPLATES",
            "_STATION_SERIES_ROWS_TEMPLATES",
        ),
        "scripts/reset_qhh_smoke_db.py": ("_FORCING_TIMESERIES_DELETE_TEMPLATES",),
        "workers/model_registry/qhh_production_bootstrap.py": ("_DYNAMIC_FORCING_COUNT_TEMPLATES",),
    }
    assert registered_template_pairs() == discover_forcing_template_pairs()


def test_an_unregistered_template_pair_is_red() -> None:
    """The guard against a planted offender, in both directions that matter.

    A tenth pair in an ALREADY REGISTERED file is the case D1 made invisible to
    the closure check (``forecast_store.py`` scores 0 registered mentions, so a
    tenth entry would not move a single number there), and a pair in a BRAND NEW
    file is the case ``WIRED_READER_PATHS`` cannot see because it is derived from
    the register. Both must name the offender.
    """
    tenth_in_a_registered_file = dict(discover_forcing_template_pairs())
    tenth_in_a_registered_file["packages/common/forecast_store.py"] += ("_FORCING_QUANTILE_ROWS_TEMPLATES",)
    with pytest.raises(AssertionError, match="_FORCING_QUANTILE_ROWS_TEMPLATES"):
        _assert_every_pair_is_registered(tenth_in_a_registered_file)

    brand_new_file = dict(discover_forcing_template_pairs())
    brand_new_file["workers/forcing_producer/store.py"] = ("_FORCING_ROW_COUNT_TEMPLATES",)
    with pytest.raises(AssertionError, match="workers/forcing_producer/store.py"):
        _assert_every_pair_is_registered(brand_new_file)

    deregistered = dict(discover_forcing_template_pairs())
    del deregistered["packages/common/best_available.py"]
    with pytest.raises(AssertionError, match="registered but not found in the tree"):
        _assert_every_pair_is_registered(deregistered)


def test_the_pair_sweep_sees_every_callee_form_and_refuses_an_unnameable_pair(tmp_path) -> None:
    """The sweep's own contract, on a synthetic tree.

    Three escapes a ``ast.Name``-only, module-level-only walk would hand an
    author for free, all closed: the ``Attribute`` callee form, the aliased
    import, and — fail-closed rather than ignored — a pair built where no
    module-level name can hold it, which
    :func:`registered_template_pairs` could never resolve and which would
    therefore read as "no template here".
    """
    (tmp_path / "packages").mkdir(parents=True)
    (tmp_path / "packages" / "attribute_form.py").write_text(
        "from packages.common import forcing_ts_render\n"
        'PAIR = forcing_ts_render.ForcingTemplatePair(legacy="a", narrow="b")\n',
        encoding="utf-8",
    )
    (tmp_path / "packages" / "aliased.py").write_text(
        "from packages.common.forcing_ts_render import ForcingTemplatePair as Pair\n"
        'ALIASED: Pair = Pair(legacy="a", narrow="b")\n',
        encoding="utf-8",
    )
    (tmp_path / "packages" / "no_pairs.py").write_text("X = 1\n", encoding="utf-8")

    assert discover_forcing_template_pairs(tmp_path, ("packages",)) == {
        "packages/aliased.py": ("ALIASED",),
        "packages/attribute_form.py": ("PAIR",),
    }

    (tmp_path / "packages" / "built_in_a_function.py").write_text(
        "from packages.common.forcing_ts_render import ForcingTemplatePair\n"
        "def templates():\n"
        '    return ForcingTemplatePair(legacy="a", narrow="b")\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="built_in_a_function.py"):
        discover_forcing_template_pairs(tmp_path, ("packages",))


def test_the_sweep_finds_exactly_the_declared_files() -> None:
    """The live pin: the tree itself, not a list checked against itself."""
    _assert_census_closes(discover_forcing_mentions())


def test_the_census_closes_over_registered_and_exempt_mentions() -> None:
    """``registered + exempt == mentions``, per file — non-vacuous since cut (b).

    Every wired reader contributes ``0`` registered mentions because its
    templates carry the renderer's token instead of the table's name, so this
    reads as ``exempt == mentions`` over the twelve surviving files. That is NOT
    the vacuity cut (a) had: the file-set equality inside
    :func:`_assert_census_closes` is what now carries the weight, and a wired
    reader that reverts to spelling the name re-enters the discovered set and
    fails it. :func:`test_a_wired_reader_that_respells_the_table_name_is_red`
    exercises exactly that, against a planted offender.
    """
    _assert_census_closes(discover_forcing_mentions())


def test_the_nine_readers_are_registered_and_render_from_their_own_modules() -> None:
    """Cut (b)'s headline, where it is checkable.

    Cut (a) carried the opposite assertion (`FORCING_REGISTRY == ()`), whose
    whole value was that nobody could read a green suite as evidence that a
    forcing read rendered per store. This replaces it rather than deleting it:
    the register is now the nine, keyed and path-sorted, and every entry's
    ``source`` really reaches into the production module (a registry that
    returned locally authored text would render perfectly and prove nothing).
    """
    assert tuple(entry.key for entry in FORCING_REGISTRY) == REGISTERED_READER_KEYS
    assert {entry.path for entry in FORCING_REGISTRY} == set(WIRED_READER_FILES)
    for entry in FORCING_REGISTRY:
        module = import_module(entry.path.removesuffix(".py").replace("/", "."))
        pair = entry.source("legacy")
        assert any(value is pair for value in vars(module).values()), (
            f"{entry.key}: source() must return the pair the production module holds, not a copy"
        )


def test_the_nine_readers_left_the_exemption_ledger() -> None:
    """The ``reader, unwired`` rows are GONE, not zeroed and not still claimed.

    Deleting an exemption row and registering the reader are two edits, and the
    closure equality cannot tell "both happened" from "neither did" — both terms
    move together. This is the assertion that pins the direction.
    """
    assert UNWIRED_READERS == ()
    still_exempt = {row.path for row in EXEMPT_MENTIONS} & set(WIRED_READER_FILES)
    assert still_exempt == {"packages/common/forecast_store.py"}, (
        "only forecast_store.py may keep an exemption row among the wired reader files, "
        "and only for its two non-SQL index/catalog metadata payloads"
    )


def test_the_declared_census_reproduces_the_fixture_baseline() -> None:
    """C3's 15/47/9/38 must still be reconstructible from the post-wiring state.

    Asserted as arithmetic rather than as a second copy of the numbers, so the
    two sides cannot be silently "agreed" by editing one of them. The
    reconstruction is: the pre-wiring total is what survives today, plus the nine
    reader mentions the wiring removed, minus the renderer module D1 introduced.
    """
    non_read_mentions = sum(row.count for row in NON_READ_MENTIONS)
    renderer_mentions = sum(row.count for row in RENDERER_CONSTANTS)
    declared = sum(FORCING_TABLE_CENSUS.values())

    assert UNWIRED_READERS == ()
    assert len(FORCING_REGISTRY) == FIXTURE_BASELINE_READER_MENTIONS
    assert non_read_mentions - TASK_73_ADDED_MENTIONS == FIXTURE_BASELINE_EXEMPT_MENTIONS
    assert declared == non_read_mentions + renderer_mentions
    # C3's 47 = the 38 that are not reads + the 9 that were, and task 7.3's own
    # addition is subtracted rather than folded in, so the historical measurement
    # stays a fixed point.
    assert (
        declared - renderer_mentions - TASK_73_ADDED_MENTIONS + len(FORCING_REGISTRY)
        == FIXTURE_BASELINE_MENTIONS
    )
    # Four of the five reader files left the set outright; forecast_store.py
    # stayed for its two index payloads, and the renderer module joined.
    assert len(FORCING_TABLE_CENSUS) == FIXTURE_BASELINE_FILES - 4 + 1
    assert set(RENDERER_CONSTANTS) == {
        row for row in EXEMPT_MENTIONS if row.path == "packages/common/forcing_ts_render.py"
    }


def test_the_wired_reader_files_dropped_out_of_the_census_by_design() -> None:
    """The execution split's predicted drop, measured rather than asserted in prose.

    ``fixtures/I11-1990.md`` predicts `display_coverage.py` 1→0,
    `forecast_store.py` 7→2, `best_available.py` 1→0,
    `qhh_production_bootstrap.py` 1→0, `reset_qhh_smoke_db.py` 1→0. Four files
    going to zero is indistinguishable, from the closure equality alone, from
    four files nobody ever swept — so the drop is pinned against the LIVE counter
    over each file's current source.
    """
    discovered = discover_forcing_mentions()
    for path in WIRED_READER_FILES:
        source = REPO_ROOT.joinpath(*path.split("/")).read_text(encoding="utf-8")
        counted = forcing_table_mentions(source, filename=path)
        expected = 2 if path == "packages/common/forecast_store.py" else 0
        assert counted == expected, f"{path}: {counted} qualified mentions, expected {expected}"
        assert discovered.get(path, 0) == expected


def test_a_wired_reader_that_respells_the_table_name_is_red() -> None:
    """The regression the drop above would otherwise hide.

    Once a reader file is out of the census, its absence is normal — so a
    reviewer needs to know that a NEW literal spelling in it is still caught. It
    is, by the file-set equality, and this exercises that against a planted
    offender rather than trusting the reading.
    """
    respelled = dict(discover_forcing_mentions())
    respelled["packages/common/display_coverage.py"] = 1
    with pytest.raises(AssertionError, match="must list the same files"):
        _assert_census_closes(respelled)


def test_every_exemption_row_names_a_shape_and_an_owner() -> None:
    """An exemption without an owner is an exemption nobody will ever revisit.

    ``tasks.md`` 7.2a requires the named owner; this is what stops the list
    degrading into "things we decided not to look at".
    """
    for row in EXEMPT_MENTIONS:
        assert row.path in FORCING_TABLE_CENSUS, f"{row.path}: exempt but not in the census"
        assert row.count > 0, f"{row.path}: a zero-count exemption row says nothing"
        assert row.shape.strip(), f"{row.path}: exemption row with no shape"
        assert row.owner.strip(), f"{row.path}: exemption row with no named owner"
        assert row.note.strip(), f"{row.path}: exemption row with no note"


def test_every_censused_file_exists() -> None:
    """A rename must be red here rather than a quiet gap in the sweep."""
    for path in FORCING_TABLE_CENSUS:
        assert REPO_ROOT.joinpath(*path.split("/")).is_file(), path


# ---------------------------------------------------------------------------
# The counter's own contract, on synthetic modules. Either failure makes the
# census useless: a new statement must raise the count (or the guard never
# bites), and a docstring or comment naming the table must not (or the number is
# bumped reflexively).
# ---------------------------------------------------------------------------


def test_the_counter_counts_string_constants_and_ignores_prose() -> None:
    prose_only = (
        '"""Module docstring naming met.forcing_station_timeseries."""\n'
        "# comment naming met.forcing_station_timeseries\n"
        "def f():\n"
        '    """Reads met.forcing_station_timeseries."""\n'
        "    return 1\n"
    )
    assert forcing_table_mentions(prose_only) == 0

    with_statement = prose_only + 'SQL = "SELECT 1 FROM met.forcing_station_timeseries WHERE forcing_version_id = %s"\n'
    assert forcing_table_mentions(with_statement) == 1


def test_the_counter_matches_the_class_of_qualified_spellings() -> None:
    """Not ``str.count`` of one spelling — that is how a new read site hides.

    All four of these are the same relation to PostgreSQL, and a substring count
    of the canonical spelling sees only the first.
    """
    source = (
        'A = "FROM met.forcing_station_timeseries"\n'
        'B = "FROM \\"met\\".\\"forcing_station_timeseries\\""\n'
        'C = "FROM MET . forcing_station_timeseries"\n'
        'D = "FROM met.forcing_station_timeseries_legacy"\n'
    )
    assert forcing_table_mentions(source) == 4


def test_the_counter_counts_every_occurrence_in_one_constant() -> None:
    """A self-join names the table twice in one string; both must count."""
    source = (
        'SQL = """\n'
        "SELECT 1 FROM met.forcing_station_timeseries a\n"
        "JOIN met.forcing_station_timeseries b ON a.station_id = b.station_id\n"
        '"""\n'
    )
    assert forcing_table_mentions(source) == 2


def test_the_sweep_sees_a_planted_file_and_skips_pruned_directories(tmp_path) -> None:
    """The sweep's own contract, on a synthetic tree.

    Without this the sweep could be silently walking nothing at all and every
    assertion above would still pass on an empty discovered set — except that
    :func:`_assert_census_closes` compares sets, which is why the real tree's
    green run is already evidence. This makes the mechanism testable in
    isolation, including the pruning that keeps vendored ``node_modules``
    Python out.
    """
    (tmp_path / "packages" / "sub").mkdir(parents=True)
    (tmp_path / "packages" / "probe.py").write_text(
        'SQL = "SELECT 1 FROM met.forcing_station_timeseries"\n', encoding="utf-8"
    )
    (tmp_path / "packages" / "sub" / "prose.py").write_text(
        '"""Only a docstring naming met.forcing_station_timeseries."""\n', encoding="utf-8"
    )
    (tmp_path / "packages" / "node_modules").mkdir()
    (tmp_path / "packages" / "node_modules" / "vendored.py").write_text(
        'SQL = "SELECT 1 FROM met.forcing_station_timeseries"\n', encoding="utf-8"
    )

    assert discover_forcing_mentions(tmp_path, ("packages",)) == {"packages/probe.py": 1}


def test_the_sweep_parses_every_file_instead_of_prefiltering_on_raw_text(tmp_path) -> None:
    """No file is skipped on a raw-text look: the sweep sees what the COUNTER sees.

    The sweep used to skip any file whose RAW SOURCE did not match the qualified
    spelling, on the claim that the raw look was a superset of the parsed count.
    It is not, and the gap is accident-class rather than adversarial: the counter
    reads FOLDED constant values, so every one of the three files below scores 1
    while its source text does not contain the qualified spelling at all. Such a
    file was skipped WHOLE — its read site neither registered nor exempt, and the
    census still green. That is exactly the ``column forcing_version_id does not
    exist``-after-7.3 failure this census exists to prevent, and this repository
    writes implicitly concatenated SQL in the write path already
    (``workers/forcing_producer/store.py``,
    ``packages/common/forcing_domain_handoff_apply.py``).

    All three cases, not just the first: a prefilter on the BARE token catches
    the concatenation at the dot and still misses the other two, so a test
    carrying only the first case would certify a "superset" claim that is still
    false. No check over raw text can be a superset of a regex over folded
    constant values, which is why there is no prefilter left to test.
    """
    (tmp_path / "packages").mkdir(parents=True)
    # Split at the schema dot: the qualified spelling exists only after folding.
    (tmp_path / "packages" / "concat_at_the_dot.py").write_text(
        'SQL = (\n    "SELECT 1 FROM met."\n    "forcing_station_timeseries WHERE forcing_version_id = %s"\n)\n',
        encoding="utf-8",
    )
    # Split INSIDE the table's name: not even the bare token is in the source.
    (tmp_path / "packages" / "concat_inside_the_name.py").write_text(
        'SQL = (\n    "SELECT 1 FROM met.forcing_station_time"\n    "series WHERE forcing_version_id = %s"\n)\n',
        encoding="utf-8",
    )
    # Spelled through an escape: the source has no `s` where the value has one.
    (tmp_path / "packages" / "escaped_spelling.py").write_text(
        'SQL = "SELECT 1 FROM met.forcing_station_timeserie\\x73 WHERE forcing_version_id = %s"\n',
        encoding="utf-8",
    )

    assert discover_forcing_mentions(tmp_path, ("packages",)) == {
        "packages/concat_at_the_dot.py": 1,
        "packages/concat_inside_the_name.py": 1,
        "packages/escaped_spelling.py": 1,
    }


def test_the_sweep_names_the_file_it_fails_closed_on(tmp_path) -> None:
    """Fail-closed is only a feature if the failure says WHICH file.

    Removing the raw-text prefilter put every ``.py`` file under the roots
    through :func:`forcing_table_mentions`, which turned "a file that does not
    parse" from unreachable (the prefilter screened it out) into a standing
    outcome of a routine run. The sweep is DOCUMENTED as failing closed there
    rather than skipping silently, and a fail-closed sweep whose diagnostic reads
    ``invalid syntax (<unknown>, line 1)`` over a ~400-file walk hands the reader
    a bisect instead of an answer.

    Both ways a file can refuse to be counted are covered, because they fail at
    different call sites and only one of them is inside the counter: a source
    that does not parse raises out of :func:`ast.parse`, and a source that is not
    UTF-8 raises out of ``read_text`` BEFORE the counter is entered, so threading
    a ``filename`` into the parser cannot cover it.

    Asserted on ``.filename`` and not on ``str(...)`` for the parse case, and
    that is not a weaker check: CPython's ``SyntaxError.__str__`` basenames the
    file, so ``str()`` reads ``invalid syntax (__init__.py, line 1)`` — which
    over this tree is not an answer either. The FULL path is what ``.filename``
    holds and what the traceback header prints, so that is what is pinned. Do not
    "strengthen" this into a ``str()`` containment check; it is unsatisfiable
    without re-raising, which would cost the caret and the offending source line.
    """
    (tmp_path / "packages").mkdir(parents=True)
    unparseable = tmp_path / "packages" / "broken.py"
    unparseable.write_text("def (\n", encoding="utf-8")

    with pytest.raises(SyntaxError) as syntax_error:
        discover_forcing_mentions(tmp_path, ("packages",))
    assert syntax_error.value.filename == str(unparseable)

    unparseable.unlink()
    undecodable = tmp_path / "packages" / "not_utf8.py"
    undecodable.write_bytes(b'SQL = "\xff\xfe met.forcing_station_timeseries"\n')

    with pytest.raises(ValueError) as decode_error:
        discover_forcing_mentions(tmp_path, ("packages",))
    assert str(undecodable) in str(decode_error.value)


def test_the_census_rejects_an_unregistered_unexempted_site() -> None:
    """``tasks.md`` 7.2a's acceptance clause: the guard bites.

    Both halves, because they fail differently: a brand new FILE (the case the
    river census structurally cannot see) and a new mention in an ALREADY
    DECLARED file.
    """
    new_file = dict(discover_forcing_mentions())
    new_file["packages/common/brand_new_forcing_reader.py"] = 1
    with pytest.raises(AssertionError, match="must list the same files"):
        _assert_census_closes(new_file)

    new_statement = dict(discover_forcing_mentions())
    new_statement["workers/forcing_producer/store.py"] += 1
    with pytest.raises(AssertionError, match="census says"):
        _assert_census_closes(new_statement)


# ---------------------------------------------------------------------------
# Shape oracle: every registered template renders for BOTH stores.
#
# Nine live renders since cut (b). The narrow half is DEAD TEXT until task 7.3 —
# it names columns no deployed table has — so this is the only thing that
# exercises it at all, and the reason the fixture insists the narrow variants be
# text-pinned rather than sketched.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entry", FORCING_REGISTRY, ids=lambda entry: entry.key)
@pytest.mark.parametrize("store", FORCING_STORES)
def test_every_registered_template_renders_for_both_stores(entry, store) -> None:
    rendered = render_forcing_ts_sql(entry.source(store), store, entry=entry.key)
    assert rendered.store == store
    assert rendered.sql.strip()


def test_the_discovery_roots_are_the_ones_the_task_names() -> None:
    """``db/`` is in the set and is not decorative — ``db/seeds/seed_demo.py`` is in it."""
    assert DISCOVERY_ROOTS == ("packages", "workers", "scripts", "services", "apps", "db")
    assert "db/seeds/seed_demo.py" in FORCING_TABLE_CENSUS
