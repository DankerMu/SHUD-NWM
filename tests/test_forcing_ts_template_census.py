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

This module lands with **cut (a) = task 7.2a**, which adds the renderer, the
register and this census and wires NO production reader. So:

* **the closure assertion is VACUOUS in its ``registered`` term.**
  ``FORCING_REGISTRY`` is empty, so ``registered`` is 0 for every file and
  ``registered + exempt == mentions`` reduces to ``exempt == mentions``. A green
  run here does NOT mean "every forcing read renders per store"; it means every
  mention is currently accounted for, and the nine reader mentions are accounted
  for as *unwired*. Cut (b) (task 7.2) registers the nine and makes the closure
  real.
* **the live pins in cut (a) are the discovery sweep and the per-file counts.**
  Those are not vacuous: a new file with a qualified mention, or a new mention in
  a listed file, reddens
  :func:`test_the_sweep_finds_exactly_the_declared_files` today.

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
    exempt_by_path,
    forcing_table_mentions,
    registered_by_path,
)

# ---------------------------------------------------------------------------
# The measured discovery set.
#
# Baseline from `fixtures/I11-1990.md` C3: 15 production files, 47 qualified
# mentions, 9 of them reader sites. Re-derived here and identical file for file.
#
# The sixteenth file and the two mentions above 47 are this change's own
# `packages/common/forcing_ts_render.py`: D1 puts the table name in two module
# constants, so the renderer contributes exactly two and every reader it wires in
# cut (b) then contributes zero. Cut (b) rewrites these numbers downwards for the
# five reader files (see the execution split in the fixture) — that drop is by
# design and must be declared as such, not discovered as a mystery.
# ---------------------------------------------------------------------------
FORCING_TABLE_CENSUS: dict[str, int] = {
    "db/seeds/seed_demo.py": 3,
    "packages/common/best_available.py": 1,
    "packages/common/display_coverage.py": 1,
    "packages/common/forcing_domain_handoff.py": 3,
    "packages/common/forcing_domain_handoff_apply.py": 16,
    "packages/common/forcing_ts_render.py": 2,
    "packages/common/forecast_store.py": 7,
    "packages/common/node27_container_contract.py": 1,
    "scripts/node27_autopipeline.py": 1,
    "scripts/node27_timeseries_compression_capture.py": 2,
    "scripts/node27_timeseries_compression_live_evidence.py": 1,
    "scripts/reset_qhh_smoke_db.py": 1,
    "services/production_closure/two_node_e2e_readonly_db_lane.py": 2,
    "workers/forcing_producer/file_store.py": 2,
    "workers/forcing_producer/store.py": 5,
    "workers/model_registry/qhh_production_bootstrap.py": 1,
}

#: C3's measured pre-change totals, pinned separately from the census dict so the
#: two cannot be "reconciled" by editing one of them.
FIXTURE_BASELINE_FILES = 15
FIXTURE_BASELINE_MENTIONS = 47
FIXTURE_BASELINE_READER_MENTIONS = 9
FIXTURE_BASELINE_EXEMPT_MENTIONS = 38


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


def test_the_sweep_finds_exactly_the_declared_files() -> None:
    """The live pin of cut (a): the tree itself, not a list checked against itself."""
    _assert_census_closes(discover_forcing_mentions())


def test_the_census_closes_over_registered_and_exempt_mentions() -> None:
    """``registered + exempt == mentions``, per file.

    VACUOUS IN ITS ``registered`` TERM IN CUT (a) — see the module docstring.
    ``FORCING_REGISTRY`` is empty, so this currently asserts ``exempt ==
    mentions``. It is kept as the full equality because cut (b) populates the
    register one reader at a time, and this is the assertion that then forces
    each wiring to move an ``UNWIRED_READERS`` row rather than simply drop it.

    Deliberately carries no "the register is empty" assertion of its own: that
    tripwire is :func:`test_cut_a_registers_no_forcing_template_yet`, and keeping
    it in exactly one place means cut (b) visits one test rather than finding
    this one red for a reason that has nothing to do with closure.
    """
    _assert_census_closes(discover_forcing_mentions())


def test_cut_a_registers_no_forcing_template_yet() -> None:
    """The register is empty ON PURPOSE, and this says so where it is checkable.

    Task 7.2 (cut b) deletes this test as it appends the first reader block. Its
    value is that until then nobody can read a green suite as evidence that a
    forcing read renders per store: this is the one assertion that states the
    opposite out loud.
    """
    assert FORCING_REGISTRY == ()


def test_the_declared_census_reproduces_the_fixture_baseline() -> None:
    """C3's 15/47/9/38, plus this change's own renderer constants and nothing else.

    Asserted as arithmetic against the declared rows so the two sides of the
    fixture cannot be silently "agreed" by editing one number: the reader rows
    and the non-read rows must still sum to C3's measured split, and the only
    admitted difference from C3's totals is the renderer module D1 introduces.
    """
    reader_mentions = sum(row.count for row in UNWIRED_READERS)
    non_read_mentions = sum(row.count for row in NON_READ_MENTIONS)
    renderer_mentions = sum(row.count for row in RENDERER_CONSTANTS)

    assert reader_mentions == FIXTURE_BASELINE_READER_MENTIONS
    assert non_read_mentions == FIXTURE_BASELINE_EXEMPT_MENTIONS
    assert reader_mentions + non_read_mentions == FIXTURE_BASELINE_MENTIONS
    assert len(FORCING_TABLE_CENSUS) == FIXTURE_BASELINE_FILES + 1
    assert sum(FORCING_TABLE_CENSUS.values()) == FIXTURE_BASELINE_MENTIONS + renderer_mentions
    assert set(RENDERER_CONSTANTS) == {
        row for row in EXEMPT_MENTIONS if row.path == "packages/common/forcing_ts_render.py"
    }


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
    new_statement["packages/common/best_available.py"] += 1
    with pytest.raises(AssertionError, match="census says"):
        _assert_census_closes(new_statement)


# ---------------------------------------------------------------------------
# Shape oracle: every registered template renders for BOTH stores.
#
# Empty in cut (a) by construction — pytest reports the parametrisation as a
# single skipped case, which is the honest signal. Cut (b)'s first reader block
# turns it into nine live renders.
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
