"""The register of renderer-input river fact-row templates (#1980, #1981).

``packages/common/river_ts_render.py`` takes SQL text and has no registry of its
own — deliberately: a production helper that knew about every call site would
have to import them all. The register is therefore test-side, and it is what
makes the oracles exhaustive rather than anecdotal:

* every entry is rendered by the shape oracles, so a template that cannot
  survive the narrow rendering is red in the PR that writes it. It used to be
  rendered once per store; #1342's contract (task 6.3) left exactly one, and the
  renderer now refuses every other name rather than routing it;
* the frozen I1 golden retains its 20 historical keys. Three unchanged entries
  still compare against current raw inputs; eight raw sources the wave changed
  and two narrow-only writer reads have separate semantic owners;
* **registry closure** — for every production file, the canonical-table mentions
  of that file's entries plus its declared non-template mentions must equal the
  file's census. An unregistered read site is therefore red, which is the only
  thing that makes "every read template renders per store" checkable at all.

Layout
------

One block per reader module, in stable file order (path-sorted). Wave 2 of the
epic (#1981–#1984) appends whole blocks; keeping the blocks separate and ordered
is what lets four PRs touch this file without colliding on one tuple.

Executed forecast statements
---------------------------

``FORECAST_STORE_EXECUTIONS`` captures all eight segment queries plus the
known-run latest-product fallback, separately from the 13 raw ``REGISTRY``
inputs. Composed executed SQL is never passed wholesale to the renderer.
The capture harness remains in ``tests/test_river_ts_text_identity_cleanup.py``.
Imports stay inside callables because the owning test modules import this
register at module level.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The base commit the golden fixture was captured at (`git rev-parse HEAD` in
#: the #1980 worktree before the first template edit).
GOLDEN_BASE_SHA = "51f9d273"
GOLDEN_FIXTURE = REPO_ROOT / "tests" / "fixtures" / f"river_ts_templates_{GOLDEN_BASE_SHA}.json"
#: sha256 of the golden's BYTES. `base_sha` is a field inside the file, so a
#: regeneration carries it along unchanged and the provenance check cannot see
#: the re-capture; this pin can, and it makes any re-capture a one-line diff a
#: reviewer must approve on purpose (review #1996, C10).
#:
#: Moved for #2007, and that move is a DECLARED BEHAVIOURAL DELTA, not a
#: re-capture. #2007 binds the national discharge layer's run selection to the
#: requested `(source, cycle)` identity, which adds these two NULL-guarded
#: conjuncts to two of `mvt:postgis_tile_sql_hydro_national`'s chains:
#:
#:     (CAST(:source AS text) IS NULL OR lower(h.source_id) = :source)
#:     (CAST(:cycle AS timestamptz) IS NULL OR h.cycle_time = :cycle)
#:
#: chain 3 is the `latest_runs` data CTE and chain 21 is the
#: `source_identity_stats` probe's run-discovery sub-select — the two
#: run-selection sites inside that one statement, in the RENDERED statement's
#: order, which is the reverse of their order in `services/tiles/mvt.py` (the
#: probe is a local built earlier and interpolated later). Each index was
#: confirmed by deleting that site's conjunct and reading which chain reddened.
#:
#: The fixture was NOT regenerated. The change that moved this pin inserts four
#: JSON lines and deletes none: nothing was removed or rewritten, the entry
#: keeps its 54 chains and the file its 215, and all 19 other entries are
#: byte-identical to the `51f9d273` capture. `base_sha` is therefore left
#: alone — every other entry genuinely still comes from that base.
#:
#: NOT moved for #2451, and that is the point. #2451 changed
#: `_SEGMENT_ROWS_SOURCE_SQL`'s two identity conjuncts from `=` to a guarded
#: `IS NOT DISTINCT FROM` — the `IS NOT NULL` half is what keeps the pair
#: filtering exactly as `=` did, since `hydro.river_timeseries_legacy`'s key
#: columns are nullable:
#:
#:     rt.basin_version_key IS NOT NULL
#:     rt.basin_version_key IS NOT DISTINCT FROM (SELECT basin_version_key ...)
#:     rt.river_network_version_key IS NOT NULL
#:     rt.river_network_version_key IS NOT DISTINCT FROM (SELECT ...)
#:
#: The golden's eight `forecast_store:<label>` entries and
#: `forecast_store:segment_identity_predicates` still record the `= (` spelling.
#: That is not stale data: it is `51f9d273`'s own text (`git show
#: 51f9d273:packages/common/forecast_store.py`, lines 92 and 105), which is
#: exactly what a base-tree capture is supposed to say. Those nine keys are
#: RECORD-ONLY — they are not in `REGISTRY` at all (13 registered entries, 20
#: golden entries), so no `source()` can replay them and
#: `test_legacy_renderer_preserves_every_current_template_predicate` never
#: compares them. The live source for that SQL is
#: `forecast_store:segment_rows_source`, which is in `ROUTED_SOURCE_KEYS` and is
#: not in the golden.
#:
#: The fixture was NOT regenerated and the base did not move. Regenerating it
#: from the post-#2451 tree is the self-certifying capture the fixture's own
#: `note` field, `test_the_golden_was_captured_at_the_change_base` and this pin
#: exist to prevent; regenerating it from a pristine `51f9d273` tree — the
#: documented path (I1-1980 decisions 11/12) — reproduces the `= (` spelling
#: byte for byte, so it is a no-op for this change.
#: Moved for #1342's contract (task 6.3), and again this is a DECLARED
#: BEHAVIOURAL DELTA, not a re-capture — the fixture's own `delta_from` block
#: records the commit, the one entry touched and why. Task 6.3 deletes the river
#: legacy read path and with it every transitional pushdown aid, so
#: `hydro_display:mvt_source_identity_probe`'s first chain lost exactly three
#: conjuncts:
#:
#:     river_network_version_id = :river_network_version_id
#:     run_id = :run_id
#:     variable = :variable
#:
#: Each was the TEXT half of a pair whose key/enum half is still recorded in the
#: same chain, so the identity the chain asserts is unchanged — only the column
#: it reads it from. The delta removes three JSON lines from one chain and
#: rewrites none; the other 19 entries are byte-identical to the `51f9d273`
#: capture, the file still holds 20 entries / 215 chains, and `base_sha` is
#: therefore left alone.
GOLDEN_SHA256 = "df78289367d4b4b6f678495447d8e588fe701e2a7e192b34d63bb740e8baba61"


def golden_sha256() -> str:
    """The captured golden's actual content hash."""
    return hashlib.sha256(GOLDEN_FIXTURE.read_bytes()).hexdigest()


@dataclass(frozen=True)
class TemplateEntry:
    """One registered river read template.

    ``kind``
        ``statement`` is a raw renderer input, never composed executed SQL.
    ``params``
        ``positional`` (``%s``) or ``named`` (``%(name)s`` / ``:name``).
    ``mentions``
        canonical-table occurrences in the entry's own text, which the closure
        check sums per file.
    ``source(store)``
        Raw input for the named store. After #1342's contract (task 6.3) the
        only store is ``narrow``; the argument is kept — and kept required —
        because ``render_river_ts_sql`` keeps it, so a caller that still believes
        in routing is refused by name rather than silently answered.
    """

    key: str
    path: str
    kind: str
    params: str
    mentions: int
    source: Callable[[str], str]


# ---------------------------------------------------------------------------
# Non-template mentions: occurrences of the canonical table name in a registered
# file that are NOT a read template, enumerated so the closure check can stay an
# exact equality instead of an inequality nobody would notice going slack.
# ---------------------------------------------------------------------------
NON_TEMPLATE_MENTIONS: dict[str, int] = {
    # The `_qhh_latest_query_indexes` index-metadata literal
    # (`"table": "hydro.river_timeseries"`), which names the table but is not SQL.
    "packages/common/forecast_store.py": 1,
    # The NULL-key residual-debt message text.
    "packages/common/display_coverage.py": 1,
    # The PublishError message naming the table q_down publication requires.
    "services/tile_publisher/publisher.py": 1,
    # The replace chain's two WRITE statements (DELETE + INSERT). #1980 registers
    # read templates only; the write side is #1985's (I7) narrow-write oracle.
    "workers/output_parser/parser.py": 2,
    # The renderer's own canonical table-name constant. One since #1342's
    # contract (task 6.3) deleted `RIVER_TABLE_LEGACY`; it used to be two because
    # `_river_table_mentions` matches a PREFIX of the name (the `_legacy` literal
    # opens with the canonical one), not a whole identifier.
    "packages/common/river_ts_render.py": 1,
    "apps/api/routes/hydro_display.py": 0,
    "services/tile_publisher/forcing_copyback_backfill.py": 0,
    "services/tiles/mvt.py": 0,
}


def _sql_constant(module: tuple[str, ...], function: str, needle: str, index: int = 0) -> str:
    from tests.test_river_ts_text_identity_cleanup import _sql_constants

    return _sql_constants(module=module, function=function, needle=needle)[index]


# ---------------------------------------------------------------------------
# apps/api/routes/hydro_display.py
# ---------------------------------------------------------------------------


def _hydro_display_identity_probe(_store: str) -> str:
    return _sql_constant(
        ("apps", "api", "routes", "hydro_display.py"),
        "_require_hydro_mvt_source_identity",
        "FROM hydro.river_timeseries",
    )


HYDRO_DISPLAY_ENTRIES: tuple[TemplateEntry, ...] = (
    TemplateEntry(
        key="hydro_display:mvt_source_identity_probe",
        path="apps/api/routes/hydro_display.py",
        kind="statement",
        params="named",
        mentions=1,
        source=_hydro_display_identity_probe,
    ),
)


# ---------------------------------------------------------------------------
# packages/common/display_coverage.py
# ---------------------------------------------------------------------------


def _display_coverage_refresh(store: str) -> str:
    from packages.common import display_coverage

    return display_coverage._river_sample_rows_template(store)


DISPLAY_COVERAGE_ENTRIES: tuple[TemplateEntry, ...] = (
    TemplateEntry(
        key="display_coverage:refresh",
        path="packages/common/display_coverage.py",
        kind="statement",
        params="named",
        mentions=1,
        source=_display_coverage_refresh,
    ),
)


# ---------------------------------------------------------------------------
# packages/common/forecast_store.py
# ---------------------------------------------------------------------------

#: The eight segment-scoped blocks, keyed exactly as the capture harness keys
#: them so a renamed method is red here rather than silently unregistered.
FORECAST_STORE_SEGMENT_BLOCKS: tuple[str, ...] = (
    "latest_issue_time",
    "per_source_latest_cycles",
    "latest_analysis_issue_time",
    "analysis_segment_rows",
    "forecast_segment_rows_selected_cycles",
    "forecast_segment_rows",
    "latest_run_type_valid_time",
    "run_type_segment_rows",
)


def _segment_rows_source(store: str) -> str:
    from packages.common import forecast_store

    return forecast_store._segment_rows_source_template(store)


def _latest_product_river_source(store: str) -> str:
    from packages.common import forecast_store

    return forecast_store._latest_product_river_source_template(store)


def _segment_execution(label: str) -> Callable:
    def capture():
        from tests.test_river_ts_text_identity_cleanup import _segment_block_executions

        return _segment_block_executions()[label]

    return capture


def _latest_product_execution():
    from tests.test_river_ts_text_identity_cleanup import _latest_product_fallback_execution

    return _latest_product_fallback_execution()


# Executed statements are deliberately NOT renderer inputs.
FORECAST_STORE_EXECUTIONS = {
    **{label: _segment_execution(label) for label in FORECAST_STORE_SEGMENT_BLOCKS},
    "latest_product_fallback": _latest_product_execution,
}


FORECAST_STORE_ENTRIES: tuple[TemplateEntry, ...] = (
    TemplateEntry(
        key="forecast_store:segment_rows_source",
        path="packages/common/forecast_store.py",
        kind="statement",
        params="named",
        mentions=1,
        source=_segment_rows_source,
    ),
    TemplateEntry(
        key="forecast_store:latest_product_river_source",
        path="packages/common/forecast_store.py",
        kind="statement",
        params="named",
        mentions=1,
        source=_latest_product_river_source,
    ),
)


# ---------------------------------------------------------------------------
# services/tile_publisher/forcing_copyback_backfill.py
# ---------------------------------------------------------------------------


def _copyback_discovery(store: str) -> str:
    from services.tile_publisher import forcing_copyback_backfill

    return forcing_copyback_backfill._backfill_discovery_source_template(store)


COPYBACK_ENTRIES: tuple[TemplateEntry, ...] = (
    TemplateEntry(
        key="forcing_copyback_backfill:discover_backfill_runs",
        path="services/tile_publisher/forcing_copyback_backfill.py",
        kind="statement",
        params="named",
        mentions=1,
        source=_copyback_discovery,
    ),
)


# ---------------------------------------------------------------------------
# services/tile_publisher/publisher.py
# ---------------------------------------------------------------------------


def _publisher_discovery(store: str) -> str:
    from services.tile_publisher import publisher

    return publisher._qdown_discovery_source_template(store)


PUBLISHER_ENTRIES: tuple[TemplateEntry, ...] = (
    TemplateEntry(
        key="publisher:qdown_discovery",
        path="services/tile_publisher/publisher.py",
        kind="statement",
        params="named",
        mentions=1,
        source=_publisher_discovery,
    ),
)


# ---------------------------------------------------------------------------
# services/tiles/mvt.py
# ---------------------------------------------------------------------------


def _hydro_source(store: str) -> str:
    from services.tiles.mvt import _hydro_source_template

    return _hydro_source_template(store)


def _hydro_national_identity_source(store: str) -> str:
    from services.tiles.mvt import _hydro_national_identity_source_template

    return _hydro_national_identity_source_template(store)


def _hydro_national_data_source(store: str) -> str:
    from services.tiles.mvt import _hydro_national_data_source_template

    return _hydro_national_data_source_template(store)


def _valid_times_named_source(store: str) -> str:
    from services.tiles.mvt import _valid_times_named_source_template

    return _valid_times_named_source_template(store)


def _valid_times_any_source(store: str) -> str:
    from services.tiles.mvt import _valid_times_any_source_template

    return _valid_times_any_source_template(store)


MVT_ENTRIES: tuple[TemplateEntry, ...] = (
    TemplateEntry(
        key="mvt:postgis_tile_sql_hydro",
        path="services/tiles/mvt.py",
        kind="statement",
        params="named",
        mentions=1,
        source=_hydro_source,
    ),
    TemplateEntry(
        key="mvt:hydro_national_identity_source",
        path="services/tiles/mvt.py",
        kind="statement",
        params="named",
        mentions=1,
        source=_hydro_national_identity_source,
    ),
    TemplateEntry(
        key="mvt:hydro_national_data_source",
        path="services/tiles/mvt.py",
        kind="statement",
        params="named",
        mentions=1,
        source=_hydro_national_data_source,
    ),
    TemplateEntry(
        key="mvt:valid_times_named_identity",
        path="services/tiles/mvt.py",
        kind="statement",
        params="named",
        mentions=1,
        source=_valid_times_named_source,
    ),
    TemplateEntry(
        key="mvt:valid_times_any_identity",
        path="services/tiles/mvt.py",
        kind="statement",
        params="named",
        mentions=1,
        source=_valid_times_any_source,
    ),
)


# ---------------------------------------------------------------------------
# workers/output_parser/parser.py (narrow-only read statements; DELETE and
# INSERT remain non-template write surfaces owned by I7)
# ---------------------------------------------------------------------------


def _parser_read(index: int) -> Callable[[str], str]:
    def source(_store: str) -> str:
        from tests.test_river_ts_text_identity_cleanup import _sql_constants

        return _sql_constants(
            module=("workers", "output_parser", "parser.py"),
            cls="PsycopgOutputParserRepository",
            function="upsert_river_timeseries",
            needle="hydro.river_timeseries",
        )[index]

    return source


PARSER_ENTRIES: tuple[TemplateEntry, ...] = (
    TemplateEntry(
        key="parser:replace_chain_probe",
        path="workers/output_parser/parser.py",
        kind="statement",
        params="positional",
        mentions=1,
        source=_parser_read(0),
    ),
    TemplateEntry(
        key="parser:replace_chain_window",
        path="workers/output_parser/parser.py",
        kind="statement",
        params="positional",
        mentions=1,
        source=_parser_read(1),
    ),
)

#: I7 task 4.3 deliberately removes the historical run_id aids and their
#: positional bindings from the narrow-only writer. These are not routed
#: reader sources: production executes them directly against canonical narrow.
#: The frozen golden and hash stay untouched. Key/window and census owners:
#: test_river_ts_text_identity_cleanup.py; executed compressed-key/replay owners:
#: test_river_ts_dual_write_integration.py. Both-store renderer sweeps remain
#: grammar/predicate checks, not a claim that the writer reads legacy storage.
PARSER_NARROW_WRITER_KEYS = frozenset({
    "parser:replace_chain_probe",
    "parser:replace_chain_window",
})


REGISTRY: tuple[TemplateEntry, ...] = (
    *HYDRO_DISPLAY_ENTRIES,
    *DISPLAY_COVERAGE_ENTRIES,
    *FORECAST_STORE_ENTRIES,
    *COPYBACK_ENTRIES,
    *PUBLISHER_ENTRIES,
    *MVT_ENTRIES,
    *PARSER_ENTRIES,
)

#: The raw sources this wave changed, not the set of renderer callers. They were
#: store-qualified until #1342's contract (task 6.3) left one store; the set is
#: kept because what it names is "the wave's own routed sources", whose live
#: owners are composed statements the golden records separately. The MVT identity
#: probe stays OUT: it executes through the renderer and remains the one entry
#: compared chain-for-chain against the golden — with the declared `delta_from`
#: above, which is why it must stay comparable rather than be reclassified.
ROUTED_SOURCE_KEYS = frozenset({
    "publisher:qdown_discovery",
    "forcing_copyback_backfill:discover_backfill_runs",
    "display_coverage:refresh",
    "forecast_store:segment_rows_source",
    "forecast_store:latest_product_river_source",
    "mvt:postgis_tile_sql_hydro",
    "mvt:hydro_national_identity_source",
    "mvt:hydro_national_data_source",
    "mvt:valid_times_named_identity",
    "mvt:valid_times_any_identity",
})

#: Every production file the register covers, path-sorted (the block order).
REGISTERED_TEMPLATE_PATHS: tuple[str, ...] = tuple(dict.fromkeys(entry.path for entry in REGISTRY))


def entry_by_key(key: str) -> TemplateEntry:
    for entry in REGISTRY:
        if entry.key == key:
            return entry
    raise KeyError(key)


# ---------------------------------------------------------------------------
# Aid-marker tripwire (#1980 task 1.1, collapsed to zero by #1342's contract)
#
# The transitional aid markers are gone from every file, so what is left is a
# TRIPWIRE: a reintroduced aid comment must be red here, not discovered when the
# narrow table answers `column river_segment_id does not exist`. The numbers are
# still pinned in each file's owning oracle (fixture decision 7: the cleanup
# oracle owns forecast_store, publisher, forcing_copyback_backfill and parser;
# the surrogate-keys oracle owns mvt, hydro_display and display_coverage), so
# exactly one test reddens per file.
#
# The marker's own issue tag is deliberately NOT spelled as a literal anywhere
# in this repository any more: a repo-wide grep for it is the contract's own
# acceptance criterion (task 6.3, criterion 1) and must come back empty, so a
# test that spelled it in order to assert its absence would be the only thing
# keeping the criterion from passing. Both halves of the marker are therefore
# COMPOSED here, from the issue number, and every call site imports them.
#
# The tripwire keys on the DESCRIPTIVE half, which every spelling of the marker
# (verbatim or the pre-#1980 one-comment-covers-four form) carried; the tag half
# is what the absence assertions elsewhere search for.
# ---------------------------------------------------------------------------

#: The expand/contract issue whose transitional aids these constants describe.
AID_ISSUE = 1342

#: The aid comment's descriptive half, as #1341 worded it.
AID_COMMENT_PHRASE = "transitional compressed-chunk pushdown aid"

#: The aid comment's tag half, composed rather than spelled — see above.
AID_MARKER_TAG = f"remove with #{AID_ISSUE}"


def aid_comment_lines(source: str) -> tuple[int, ...]:
    """1-based line numbers of every transitional-aid comment in ``source``."""
    return tuple(
        number for number, line in enumerate(source.split("\n"), 1) if AID_COMMENT_PHRASE in line
    )


def assert_marker_census(path: str, expected_markers: int) -> None:
    """``path`` carries exactly ``expected_markers`` transitional-aid comments.

    Every owner passes ``0``: #1342's contract removed the aids and the renderer
    machinery that deleted them. The parameter stays so the call sites keep
    naming the number they assert instead of asserting an implicit zero.
    """
    source = (REPO_ROOT / path).read_text(encoding="utf-8")
    found = aid_comment_lines(source)
    assert len(found) == expected_markers, (
        f"{path}: {len(found)} transitional-aid comments (lines {list(found)}), census says {expected_markers}"
    )
