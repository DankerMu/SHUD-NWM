"""The register of river fact-table READ templates, one entry per statement (#1980).

``packages/common/river_ts_render.py`` takes SQL text and has no registry of its
own — deliberately: a production helper that knew about every call site would
have to import them all. The register is therefore test-side, and it is what
makes the oracles exhaustive rather than anecdotal:

* every entry is rendered for BOTH stores by the shape oracles, so a template
  that cannot survive the narrow rendering is red in the PR that writes it, not
  in the migration window;
* the golden equivalence oracle (``tests/fixtures/river_ts_templates_51f9d273.json``)
  compares every entry's legacy variant against its base-SHA form, which is what
  makes #1980's layout normalisation provably behaviour-free;
* **registry closure** — for every production file, the canonical-table mentions
  of that file's entries plus its declared non-template mentions must equal the
  file's census. An unregistered read site is therefore red, which is the only
  thing that makes "every read template renders per store" checkable at all.

Layout
------

One block per reader module, in stable file order (path-sorted). Wave 2 of the
epic (#1981–#1984) appends whole blocks; keeping the blocks separate and ordered
is what lets four PRs touch this file without colliding on one tuple.

Assembled statements
--------------------

Six of forecast_store's entries only exist at runtime: the segment blocks
interpolate a scenario / identity filter, and the latest-product fallback's heavy
CTE is only built after a header prefetch. Those come through the capture-cursor
harness that already exists in ``tests/test_river_ts_text_identity_cleanup.py``
rather than through a second copy of it. The imports are INSIDE the callables on
purpose: the shape-oracle modules import this register at module level, and a
top-level import of a ``test_*`` module here would close that loop into a cycle.
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
GOLDEN_SHA256 = "1371c74b93b6bb6269ed4853b3de392243e868af10e8690c6fa7ba2c4b545d41"


def golden_sha256() -> str:
    """The captured golden's actual content hash."""
    return hashlib.sha256(GOLDEN_FIXTURE.read_bytes()).hexdigest()


@dataclass(frozen=True)
class TemplateEntry:
    """One registered river read template.

    ``kind``
        ``statement`` renders on its own; ``fragment`` is a WHERE-chain body
        embedded by a caller (``forecast_store._SEGMENT_IDENTITY_PREDICATE_SQL``),
        so it names no table — the rename is a no-op on it and the structural
        check applies to the fragment alone.
    ``params``
        ``positional`` (``%s``) or ``named`` (``%(name)s`` / ``:name``). Only
        named templates may be passed to ``render_union_all``: duplicating a
        positional template into two branches doubles the caller's tuple in an
        order that depends on which aids each branch dropped.
    ``expected_aids``
        how many transitional aid conjuncts the template carries after #1980's
        normalisation — the per-entry half of the 34/34 marker/aid census.
    ``mentions``
        canonical-table occurrences in the entry's own text, which the closure
        check sums per file.
    ``union_safe`` / ``union_unsafe_reason``
        whether a statement-level ``UNION ALL`` of this statement's two store
        branches means the same thing as the statement itself, and — when it does
        not — the one-line reason, read off THIS ENTRY'S CONSUMER.

        DECLARED, never inferred (fixture decision 13). Whether a result
        decomposes per store is not a property of the SQL text: ``SELECT DISTINCT
        … ORDER BY … LIMIT`` computes a per-branch top-N that concatenates into
        something that is not the global top-N, a single global aggregate row
        becomes two partial rows, a statement whose driving relation is NOT the
        fact table duplicates every driving row, and a ``.first()`` consumer
        reads one row out of two branches' worth. The renderer cannot see any of
        that, so it refuses a ``union_safe=False`` entry and says whose reason it
        is. The combination those entries actually need is a union INSIDE a CTE,
        which is designed by the wave-2 PR that has a caller for it — #1980 ships
        the statement-level combinator only.

        ``union_unsafe_reason`` is present iff ``union_safe`` is False.
    """

    key: str
    path: str
    kind: str
    params: str
    expected_aids: int
    mentions: int
    source: Callable[[], str]
    union_safe: bool = True
    union_unsafe_reason: str | None = None


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
    # The renderer's own two table-name constants. `_river_table_mentions` counts
    # substrings, so the `_legacy` literal contains the canonical name too.
    "packages/common/river_ts_render.py": 2,
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


def _hydro_display_identity_probe() -> str:
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
        expected_aids=3,
        mentions=1,
        source=_hydro_display_identity_probe,
        union_safe=False,
        union_unsafe_reason=(
            "top-level LIMIT 1 existence probe consumed by .first() "
            "(apps/api/routes/hydro_display.py:805, `if row is not None: return`): the statement returns at "
            "most one row and a UNION ALL of two LIMIT-1 branches returns up to two, so the union is not "
            "the statement's own LIMIT -- the equivalent cross-store form is one predicate with two EXISTS "
            "arms, not two operands"
        ),
    ),
)


# ---------------------------------------------------------------------------
# packages/common/display_coverage.py
# ---------------------------------------------------------------------------


def _display_coverage_refresh() -> str:
    from packages.common import display_coverage

    return display_coverage._REFRESH_SQL


DISPLAY_COVERAGE_ENTRIES: tuple[TemplateEntry, ...] = (
    TemplateEntry(
        key="display_coverage:refresh",
        path="packages/common/display_coverage.py",
        kind="statement",
        params="named",
        expected_aids=3,
        mentions=1,
        source=_display_coverage_refresh,
        union_safe=False,
        union_unsafe_reason=(
            "data-modifying: WITH ... INSERT INTO hydro.run_display_coverage ... ON CONFLICT ... RETURNING "
            "run_id, executed by `_refresh` at packages/common/display_coverage.py:738 -- the worker "
            "`refresh_run_display_coverage` (:756) calls. A UNION "
            "ALL operand has to be a read; duplicating this into two branches would run the upsert twice. "
            "`kind` stays `statement` because the census counts statements, not union operands (decision 13)"
        ),
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


def _segment_identity_fragment() -> str:
    from packages.common import forecast_store

    return forecast_store._SEGMENT_IDENTITY_PREDICATE_SQL


def _segment_block(label: str) -> Callable[[], str]:
    def source() -> str:
        from tests.test_river_ts_text_identity_cleanup import _segment_block_statements

        rendered = _segment_block_statements()
        assert label in rendered, f"forecast_store segment block {label} is no longer rendered"
        return rendered[label]

    return source


def _latest_product_fallback() -> str:
    from tests.test_river_ts_text_identity_cleanup import _latest_product_fallback_statement

    return _latest_product_fallback_statement()


FORECAST_STORE_ENTRIES: tuple[TemplateEntry, ...] = (
    TemplateEntry(
        key="forecast_store:segment_identity_predicates",
        path="packages/common/forecast_store.py",
        kind="fragment",
        params="positional",
        expected_aids=3,
        mentions=0,
        source=_segment_identity_fragment,
    ),
    *(
        TemplateEntry(
            key=f"forecast_store:{label}",
            path="packages/common/forecast_store.py",
            kind="statement",
            params="positional",
            expected_aids=3,
            mentions=1,
            source=_segment_block(label),
        )
        for label in FORECAST_STORE_SEGMENT_BLOCKS
    ),
    TemplateEntry(
        key="forecast_store:latest_product_fallback",
        path="packages/common/forecast_store.py",
        kind="statement",
        params="named",
        expected_aids=3,
        mentions=1,
        source=_latest_product_fallback,
        union_safe=False,
        union_unsafe_reason=(
            "the fact table is not the driving relation: the read sits in the `river_sample_rows` CTE that "
            "feeds `hydro_coverage`, and the outer query drives off `FROM candidate_runs cr` and reaches the "
            "river side only through `LEFT JOIN station_coverage sc ... LEFT JOIN station_variable_coverage "
            "svc ... LEFT JOIN hydro_coverage hc`, so a statement-level UNION ALL emits EVERY candidate_runs "
            "row twice -- once per branch -- "
            "whichever store its river rows live in. This is the case whose correct union sits inside the "
            "CTE (retro round 3)"
        ),
    ),
)


# ---------------------------------------------------------------------------
# services/tile_publisher/forcing_copyback_backfill.py
# ---------------------------------------------------------------------------


def _copyback_discovery() -> str:
    from services.tile_publisher import forcing_copyback_backfill

    return forcing_copyback_backfill._DISCOVER_BACKFILL_RUNS_SQL


COPYBACK_ENTRIES: tuple[TemplateEntry, ...] = (
    TemplateEntry(
        key="forcing_copyback_backfill:discover_backfill_runs",
        path="services/tile_publisher/forcing_copyback_backfill.py",
        kind="statement",
        params="named",
        expected_aids=1,
        mentions=1,
        source=_copyback_discovery,
    ),
)


# ---------------------------------------------------------------------------
# services/tile_publisher/publisher.py
# ---------------------------------------------------------------------------


def _publisher_discovery() -> str:
    """The q_down discovery aggregate, PostgreSQL dialect.

    Registered once, not once per dialect: both dialects come out of the SAME
    f-string and differ only in the interpolated aggregate expressions, so the
    aid block — the thing this register exists to render per store — is shared.
    Registering both would also double the file's mention count and break the
    closure equality for no added coverage; the sqlite dialect's own shape stays
    pinned by the cleanup oracle's parametrised test.
    """
    from services.tile_publisher import publisher

    return publisher._qdown_discovery_sql(
        is_sqlite=False,
        optional={"select": "h.run_manifest_uri, h.output_uri,", "group": ", h.run_manifest_uri"},
        forcing={
            "select": "fv.forcing_version_id AS forcing_row_forcing_version_id,",
            "join": "LEFT JOIN met.forcing_version fv ON fv.forcing_version_id = h.forcing_version_id",
            "group": ", fv.forcing_version_id",
        },
        where_clauses=[
            "h.run_type = 'forecast'",
            "h.status IN ('succeeded', 'parsed', 'published')",
            "r.variable_e = 'q_down'",
            "lower(h.source_id) = :source_id",
        ],
    )


PUBLISHER_ENTRIES: tuple[TemplateEntry, ...] = (
    TemplateEntry(
        key="publisher:qdown_discovery",
        path="services/tile_publisher/publisher.py",
        kind="statement",
        params="named",
        expected_aids=1,
        mentions=1,
        source=_publisher_discovery,
    ),
)


# ---------------------------------------------------------------------------
# services/tiles/mvt.py
# ---------------------------------------------------------------------------


def _tile_sql(layer: str) -> Callable[[], str]:
    def source() -> str:
        from services.tiles.mvt import postgis_tile_sql

        return postgis_tile_sql(layer)

    return source


def _valid_times_branch(index: int) -> Callable[[], str]:
    """One of ``valid_times_for_layer``'s two branches (fixture decision 1).

    The function selects between them with an inline conditional, so they are two
    registered templates: the named-identity branch carries three aids, the
    no-identity branch one, and a store-rendering that only ever saw the first
    would leave the second unproven.
    """

    def source() -> str:
        from tests.test_sql_shape_helpers import sql_literals

        mvt_source = (REPO_ROOT / "services" / "tiles" / "mvt.py").read_text(encoding="utf-8")
        start = mvt_source.index("def valid_times_for_layer")
        end = mvt_source.index("def national_discharge_valid_times", start)
        literals = sql_literals(mvt_source[start:end])
        assert len(literals) == 2, literals
        return literals[index]

    return source


MVT_ENTRIES: tuple[TemplateEntry, ...] = (
    TemplateEntry(
        key="mvt:postgis_tile_sql_hydro",
        path="services/tiles/mvt.py",
        kind="statement",
        params="named",
        expected_aids=3,
        mentions=1,
        source=_tile_sql("hydro"),
        union_safe=False,
        union_unsafe_reason=(
            "one GLOBAL aggregate row per branch: the statement's final SELECT reads scalar sub-selects out "
            "of `source_identity_stats, source_stats, budget_stats, prefilter_stats` (ST_AsMVT plus "
            "COUNT/SUM/MAX over the whole tile), and the consumer takes .mappings().first() "
            "(apps/api/routes/hydro_display.py:514). Two branches produce two PARTIAL tiles and the consumer "
            "reads one of them"
        ),
    ),
    TemplateEntry(
        # One statement, three fact-table reads: the identity-existence probe and
        # the two national legs' correlated lateral probes.
        key="mvt:postgis_tile_sql_hydro_national",
        path="services/tiles/mvt.py",
        kind="statement",
        params="named",
        expected_aids=11,
        mentions=3,
        source=_tile_sql("hydro-national"),
        union_safe=False,
        union_unsafe_reason=(
            "one GLOBAL aggregate row per branch, same consumer as the single-network tile "
            "(apps/api/routes/hydro_display.py:514), and additionally a per-branch `DISTINCT ON "
            "(mi.river_network_version_id)` latest-run selection and a per-branch segment `LIMIT` budget: "
            "each branch would pick its own latest run and spend the whole budget"
        ),
    ),
    TemplateEntry(
        key="mvt:valid_times_named_identity",
        path="services/tiles/mvt.py",
        kind="statement",
        params="named",
        expected_aids=3,
        mentions=1,
        source=_valid_times_branch(0),
        union_safe=False,
        union_unsafe_reason=(
            "SELECT DISTINCT ... ORDER BY valid_time DESC LIMIT :limit -- a per-branch top-N. The union of "
            "two branch top-Ns is neither globally distinct nor the global top-N, and the consumer slices "
            "`formatted[:limit]` off the row order it was handed "
            "(services/tiles/mvt.py:1668-1670, _valid_time_discovery)"
        ),
    ),
    TemplateEntry(
        key="mvt:valid_times_any_identity",
        path="services/tiles/mvt.py",
        kind="statement",
        params="named",
        expected_aids=1,
        mentions=1,
        source=_valid_times_branch(1),
        union_safe=False,
        union_unsafe_reason=(
            "SELECT DISTINCT ... ORDER BY valid_time DESC LIMIT :limit with no run filter at all, so both "
            "branches really do return rows: the union is up to 2x:limit rows, not globally distinct and not "
            "the global top-N, and the consumer slices `formatted[:limit]` "
            "(services/tiles/mvt.py:1668-1670)"
        ),
    ),
)


# ---------------------------------------------------------------------------
# workers/output_parser/parser.py (read statements only; the DELETE and the
# dual-write INSERT are write surfaces and belong to I7)
# ---------------------------------------------------------------------------


def _parser_read(index: int) -> Callable[[], str]:
    def source() -> str:
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
        expected_aids=1,
        mentions=1,
        source=_parser_read(0),
    ),
    TemplateEntry(
        key="parser:replace_chain_window",
        path="workers/output_parser/parser.py",
        kind="statement",
        params="positional",
        expected_aids=1,
        mentions=1,
        source=_parser_read(1),
    ),
)


REGISTRY: tuple[TemplateEntry, ...] = (
    *HYDRO_DISPLAY_ENTRIES,
    *DISPLAY_COVERAGE_ENTRIES,
    *FORECAST_STORE_ENTRIES,
    *COPYBACK_ENTRIES,
    *PUBLISHER_ENTRIES,
    *MVT_ENTRIES,
    *PARSER_ENTRIES,
)

#: Every production file the register covers, path-sorted (the block order).
REGISTERED_TEMPLATE_PATHS: tuple[str, ...] = tuple(dict.fromkeys(entry.path for entry in REGISTRY))


def entry_by_key(key: str) -> TemplateEntry:
    for entry in REGISTRY:
        if entry.key == key:
            return entry
    raise KeyError(key)


# ---------------------------------------------------------------------------
# Marker / aid census (#1980, task 1.1)
#
# The counting itself is shared; the NUMBERS are pinned in each file's owning
# oracle (fixture decision 7: the cleanup oracle owns forecast_store, publisher,
# forcing_copyback_backfill and parser; the surrogate-keys oracle owns mvt,
# hydro_display and display_coverage), so exactly one test reddens per file.
#
# Counted on the SOURCE FILE, not on rendered templates: `grep -rn "remove with
# #1342"` is the number #1342 deletes, and `forecast_store`'s three-aid fragment
# is written once and rendered nine times.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarkerCensus:
    """What a source file's ``#1342`` marker lines look like."""

    #: line numbers (1-based) whose stripped content IS the verbatim marker
    marker_lines: tuple[int, ...]
    #: line numbers carrying the issue tag in any form — prose, constants, a
    #: non-verbatim marker. Everything here that is not a marker line has to be
    #: declared by the owning oracle, so a 1:N marker cannot come back unnoticed.
    tag_lines: tuple[int, ...]
    #: the aid conjunct under each marker; ``None`` where the next line is not
    #: exactly one aid conjunct, which is the layout violation itself.
    aids: tuple[str | None, ...]


def marker_census(source: str) -> MarkerCensus:
    from packages.common.river_ts_render import PUSHDOWN_AID_MARKER, aid_conjunct

    tag = "remove with #1342"
    lines = source.split("\n")
    marker_lines = tuple(number for number, line in enumerate(lines, 1) if line.strip() == PUSHDOWN_AID_MARKER)
    tag_lines = tuple(number for number, line in enumerate(lines, 1) if tag in line)
    aids = tuple(aid_conjunct(lines[number]) if number < len(lines) else None for number in marker_lines)
    return MarkerCensus(marker_lines, tag_lines, aids)


def assert_marker_census(
    path: str,
    expected_markers: int,
    *,
    non_aid_tag_lines: int = 0,
) -> None:
    """Every marker in ``path`` is verbatim, on its own line, over exactly one aid.

    ``non_aid_tag_lines`` is the number of lines that carry the issue tag WITHOUT
    being an aid marker (a constant, a docstring). Declared per file rather than
    tolerated globally: the pre-#1980 mvt wording was a tag line that was not a
    verbatim marker, and an unbounded allowance would let it back in.
    """
    source = (REPO_ROOT / path).read_text(encoding="utf-8")
    census = marker_census(source)

    assert len(census.marker_lines) == expected_markers, (
        f"{path}: {len(census.marker_lines)} verbatim aid markers, census says {expected_markers}"
    )
    assert len(census.tag_lines) == expected_markers + non_aid_tag_lines, (
        f"{path}: {len(census.tag_lines)} lines carry the #1342 tag but only "
        f"{expected_markers} are verbatim markers and {non_aid_tag_lines} are declared non-aid mentions "
        f"(lines {[number for number in census.tag_lines if number not in census.marker_lines]})"
    )
    for line_number, aid in zip(census.marker_lines, census.aids, strict=True):
        assert aid is not None, (
            f"{path}:{line_number}: the line under this aid marker is not exactly one aid conjunct"
        )
