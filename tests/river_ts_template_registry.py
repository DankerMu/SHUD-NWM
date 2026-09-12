"""The register of renderer-input river fact-row templates (#1980, #1981).

``packages/common/river_ts_render.py`` takes SQL text and has no registry of its
own — deliberately: a production helper that knew about every call site would
have to import them all. The register is therefore test-side, and it is what
makes the oracles exhaustive rather than anecdotal:

* every entry is rendered for BOTH stores by the shape oracles, so a template
  that cannot survive the narrow rendering is red in the PR that writes it, not
  in the migration window;
* the frozen I1 golden retains its 20 historical keys. Three unchanged entries
  still compare against current raw inputs; eight store-qualified raw sources
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

``FORECAST_STORE_EXECUTIONS`` captures all eight spanning segment queries plus
the known-run latest-product fallback, separately from the 13 raw ``REGISTRY``
inputs. A composed store union is never passed wholesale to the narrow renderer.
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
GOLDEN_SHA256 = "d104d1c69cea55cdb86bd8a44d90d8f93c621580b43f74744f6b5cca7cd5a453"


def golden_sha256() -> str:
    """The captured golden's actual content hash."""
    return hashlib.sha256(GOLDEN_FIXTURE.read_bytes()).hexdigest()


def historical_display_coverage_sql() -> str:
    """Immutable pre-store DML: historical oracle and pre-transition seed only."""
    data = (REPO_ROOT / "tests/fixtures/display_coverage_pre_store_b7cdce63.sql").read_bytes()
    assert hashlib.sha256(data).hexdigest() == (
        "17283e0277c6b8d2047ffa8aacc36e2b4bab4bb67063e9a15d44668f25490952"
    ), "pre-store display coverage snapshot changed"
    return data.decode("utf-8")


@dataclass(frozen=True)
class TemplateEntry:
    """One registered river read template.

    ``kind``
        ``statement`` is a raw renderer input, never composed executed SQL.
    ``params``
        ``positional`` (``%s``) or ``named`` (``%(name)s`` / ``:name``).
    ``expected_aids``
        how many transitional aid conjuncts the template carries after #1980's
        normalisation — the per-entry half of the 34/34 marker/aid census.
    ``mentions``
        canonical-table occurrences in the entry's own text, which the closure
        check sums per file.
    ``source(store)``
        Raw input for that store, including caller-owned routing literals.
        A store-independent raw input is the same authored SQL for either store.
        The store is required: there is no implicit legacy input for a narrow render.
    """

    key: str
    path: str
    kind: str
    params: str
    expected_aids: int
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
    # The renderer's own two table-name constants. Two, not one, because
    # `_river_table_mentions` matches a PREFIX of the name (the `_legacy` literal
    # opens with the canonical one), not a whole identifier.
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
        expected_aids=3,
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
        expected_aids=3,
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
        expected_aids=3,
        mentions=1,
        source=_segment_rows_source,
    ),
    TemplateEntry(
        key="forecast_store:latest_product_river_source",
        path="packages/common/forecast_store.py",
        kind="statement",
        params="named",
        expected_aids=3,
        mentions=1,
        source=_latest_product_river_source,
    ),
)


# ---------------------------------------------------------------------------
# services/tile_publisher/forcing_copyback_backfill.py
# ---------------------------------------------------------------------------


def _copyback_discovery(_store: str) -> str:
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


def _publisher_discovery(_store: str) -> str:
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
        expected_aids=3,
        mentions=1,
        source=_hydro_source,
    ),
    TemplateEntry(
        key="mvt:hydro_national_identity_source",
        path="services/tiles/mvt.py",
        kind="statement",
        params="named",
        expected_aids=3,
        mentions=1,
        source=_hydro_national_identity_source,
    ),
    TemplateEntry(
        key="mvt:hydro_national_data_source",
        path="services/tiles/mvt.py",
        kind="statement",
        params="named",
        expected_aids=4,
        mentions=1,
        source=_hydro_national_data_source,
    ),
    TemplateEntry(
        key="mvt:valid_times_named_identity",
        path="services/tiles/mvt.py",
        kind="statement",
        params="named",
        expected_aids=3,
        mentions=1,
        source=_valid_times_named_source,
    ),
    TemplateEntry(
        key="mvt:valid_times_any_identity",
        path="services/tiles/mvt.py",
        kind="statement",
        params="named",
        expected_aids=1,
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
        expected_aids=0,
        mentions=1,
        source=_parser_read(0),
    ),
    TemplateEntry(
        key="parser:replace_chain_window",
        path="workers/output_parser/parser.py",
        kind="statement",
        params="positional",
        expected_aids=0,
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

#: Changed store-qualified raw sources, not the set of renderer callers.
#: The MVT identity probe executes through the renderer but keeps its unchanged,
#: store-independent raw input and therefore remains historical-comparable.
ROUTED_SOURCE_KEYS = frozenset({
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
# Marker / aid census (#1980, task 1.1)
#
# The counting itself is shared; the NUMBERS are pinned in each file's owning
# oracle (fixture decision 7: the cleanup oracle owns forecast_store, publisher,
# forcing_copyback_backfill and parser; the surrogate-keys oracle owns mvt,
# hydro_display and display_coverage), so exactly one test reddens per file.
#
# Counted on the SOURCE FILE, not on executed statements. Forecast's two raw
# sources each carry three aids; the segment source has eight execution owners.
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
