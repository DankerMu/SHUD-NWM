"""Zero-text-identity oracle for the out-of-boundary consumers (#1442).

Issue #1342 drops ``hydro.river_timeseries``'s text identity columns and the
drop is irreversible. This file is the machine-checkable statement of "no
registered consumer still filters, joins, aggregates or emits identity through
those columns", so #1342 can be a pure migration.

Why an oracle instead of the two greps the issue proposed
--------------------------------------------------------

Both greps were shown to be false-negative on this very register: they look for
text identity inside triple-quoted SQL blocks, and four of the registered call
sites (``db/seeds/seed_demo.py``, ``tests/integration_helpers.py``,
``scripts/reset_qhh_smoke_db.py``, ``publisher.py``'s ``where_clauses``) do not
spell their predicates in a triple-quoted block at all. A grep over the whole
source is worse, not better: the key-resolution sub-selects legitimately contain
``WHERE run_id = %s`` against the AUTHORITY tables, so a raw-text scan goes
green on code that never switched and red on code that did.

So the register is split by how each call site's SQL can be observed:

* **alias-qualified surfaces** (``forecast_store.py``'s nine query blocks,
  ``publisher.py``, ``forcing_copyback_backfill.py``, ``node27_autopipeline.py``,
  ``parser.py``) — the SQL is rendered (by driving the real code with a
  capturing cursor, by calling the real pure renderer, or by reading the real
  statement constant) and asserted with the shared machinery in
  ``tests/test_sql_shape_helpers.py``: sub-selects stripped, comments removed,
  the referenced text fact columns EQUAL to the per-group sanctioned set.
* **bare-column / fragment surfaces** (``summarize_qhh_smoke_results.py``,
  ``seed_demo.py``, ``reset_qhh_smoke_db.py``'s ``_delete`` WHERE fragment,
  ``integration_helpers.py``'s IN predicate, ``publisher.py``'s
  ``where_clauses`` elements) — no alias to qualify, so each call site is
  asserted individually against its own string constant.

Per-group sanctioned aids (design D1). A transitional TEXT predicate survives
only where the identity is a literal/bound parameter AND the plan can reach a
compressed chunk:

===========================  ==========================================
group                        allowed text fact columns
===========================  ==========================================
A, eight segment blocks      river_segment_id, river_network_version_id,
                             variable
A9, latest-product fallback  run_id, river_network_version_id, variable
B, publisher discovery       variable
C, copyback EXISTS           variable
D, autopipeline (2)          none
E, seed/smoke/helpers        none
F, parser replace chain (3)  none
===========================  ==========================================

Scope is exactly this issue's register. The display four (``mvt.py``,
``hydro_display.py``, ``display_coverage.py``, ``production_closure``) are
watched by ``tests/test_river_ts_read_path_surrogate_keys.py`` and are
deliberately NOT re-asserted here, so #1341's grouped-comment style cannot be
false-flagged by a second, differently-worded oracle. ``db/migrations/**`` and
``scripts/node27_river_identity_backfill.py`` read the text columns by
definition and are out of register too.

Two invariants are checked on every registered surface beyond "which text
columns are referenced" (design D10.5/D10.6, added after cross-review):

* **adjacency** — a sanctioned aid must be AND-ed in the same conjunction as the
  key/enum predicate that supersedes it, and its ``remove with #1342`` marker
  must be on its own line or the one immediately above. Both are what make the
  aid deletable: an aid separated from its counterpart is load-bearing, and a
  marker separated from its aid does not say which line to delete.
* **census** — each registered file declares how many ``hydro.river_timeseries``
  mentions it has, so a NEW statement in a registered file is red until it is
  registered and given a shape pin. Without it the register only ever checked
  the statements it already knew about.

The oracle's own contract is pinned by the counter-example tests at the bottom:
a forbidden predicate, an unmarked aid, a detached marker, a text fact join, an
aid whose counterpart vanished or drifted out of its conjunction, and a
bare-column regression each have to turn it red.
"""

from __future__ import annotations

import ast
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from packages.common import forecast_store
from packages.common.forecast_store import PsycopgForecastStore, _ScenarioFilter
from services.tile_publisher import forcing_copyback_backfill as backfill_module
from services.tile_publisher import publisher as publisher_module
from tests.test_sql_shape_helpers import (
    FORBIDDEN_TEXT_FACT_COLUMNS,
    SANCTIONED_TEXT_PUSHDOWN_COLUMNS,
    TEXT_IDENTITY_COLUMNS,
    assert_aid_is_conjoined_with_its_counterpart,
    assert_text_fact_columns,
    outer_predicates,
    strip_all_subqueries,
    strip_comments,
    strip_scalar_subqueries,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# The register: every production file whose ``hydro.river_timeseries`` SQL this
# oracle owns. Module-level rather than local to the register test because two
# other guards derive from it — the statement census below, and
# ``tests/test_select_ci_tests.py``'s wiring meta-test, which asserts that a diff
# to each of these paths selects THIS file in the CI lane. A new registered file
# therefore cannot be added without also being wired and censused.
REGISTERED_SOURCES: tuple[str, ...] = (
    "packages/common/forecast_store.py",
    "services/tile_publisher/publisher.py",
    "services/tile_publisher/forcing_copyback_backfill.py",
    "scripts/node27_autopipeline.py",
    "scripts/summarize_qhh_smoke_results.py",
    "scripts/reset_qhh_smoke_db.py",
    "db/seeds/seed_demo.py",
    "workers/output_parser/parser.py",
    "tests/integration_helpers.py",
)

RIVER_TABLE = "hydro.river_timeseries"

# Statement census (design D10.6). Every register entry above pins the SHAPE of
# the statements it knows about; nothing made a registered file declare how many
# statements it has, so a NEW ``hydro.river_timeseries`` statement — a tenth
# forecast_store method, a third autopipeline call site — landed with the whole
# oracle green. The census closes that: the count of non-docstring string
# mentions of the table is pinned per file, so any addition is red until the
# register (and this number) is updated.
#
# Non-docstring string constants, not raw source occurrences: prose in a
# docstring or a ``#`` comment that names the table is not a statement, and
# making an explanatory paragraph red the census is noise nobody would keep.
# Counted per OCCURRENCE, so two statements inside one constant both register.
#
# Known limitation: it counts the LITERAL text "hydro.river_timeseries" inside
# string constants. A statement that builds the table name through a variable or
# an f-string placeholder, or that splits it into a ("hydro", "river_timeseries")
# tuple, is invisible to the census and would land without moving any number
# here. The census is a cheap tripwire for the ordinary case, not a guarantee;
# the per-surface shape pins remain the primary defense.
#
# The breakdown, so an intentional change can be re-derived rather than guessed:
#
# * forecast_store.py 10 = the eight segment blocks + the latest-product
#   fallback's river scan (A9) + ``_qhh_latest_query_indexes``'s
#   ``"table": "hydro.river_timeseries"`` index-metadata literal.
# * publisher.py 2 = the discovery aggregate + the PublishError message naming
#   the required table.
# * forcing_copyback_backfill.py 1 = the correlated EXISTS probe.
# * node27_autopipeline.py 2 = the ingest and publish per-tick criteria.
# * summarize_qhh_smoke_results.py 1 / reset_qhh_smoke_db.py 1 = one statement
#   each; the reset one is the table NAME passed to its ``_delete`` helper,
#   which is why the census counts bare mentions and not just SQL-shaped ones.
# * seed_demo.py 5 = the seed INSERT + two verification counts + the two
#   human-readable count LABELS.
# * parser.py 4 = probe, window, DELETE, INSERT.
# * integration_helpers.py 3 = the dual-write INSERT + the cleanup DELETE +
#   the #1640/#1654 min/max valid_time probe that bounds that DELETE.
RIVER_TABLE_CENSUS: dict[str, int] = {
    "packages/common/forecast_store.py": 10,
    "services/tile_publisher/publisher.py": 2,
    "services/tile_publisher/forcing_copyback_backfill.py": 1,
    "scripts/node27_autopipeline.py": 2,
    "scripts/summarize_qhh_smoke_results.py": 1,
    "scripts/reset_qhh_smoke_db.py": 1,
    "db/seeds/seed_demo.py": 5,
    "workers/output_parser/parser.py": 4,
    "tests/integration_helpers.py": 3,
}

# The marker every retained transitional text predicate must carry, verbatim.
# #1341 introduced the wording; #1342 removes every line that has it, so a
# retained aid without the marker is an aid nobody will remember to delete.
PUSHDOWN_AID_MARKER = "-- transitional compressed-chunk pushdown aid, remove with #1342"

# Per-group ceilings, as adjudicated in design D1 (A segment blocks amended by
# D10.7). The segment blocks are the one group whose ceiling exceeds the shared
# SANCTIONED_TEXT_PUSHDOWN_COLUMNS, and for exactly one measured reason: without
# a literal `rt.river_segment_id` the COMPRESSED leg loses 000047's third
# segmentby column and decompresses the whole network (32660 batches / Rows
# Removed 3,292,128 / 18549ms, against 40 / 4032 / 1085ms with it). The
# uncompressed leg gains nothing from the aid — its Index Cond and Rows Removed
# are identical either way; it runs on 000051's key index plus a heap filter.
# The switch's accepted residual, from the quiet-database shipped-SQL receipts
# (node-27 `/home/nwm/nwm-1442-e4/final/`, warm second run, superseding the
# contended earlier figures): uncompressed 6.2ms -> 226ms, buffers 387 ->
# 15,439; compressed 3.0ms -> 1085ms, where the old number came from run-level
# segmentby pruning through the text PK loop parameter — structurally
# unavailable to a keyed join, since a text fact join is forbidden. The cure is
# #1342's `(river_segment_key, variable_e, valid_time DESC)` successor index
# plus a compression-layout re-cut, not a text predicate. The
# widening is expressed HERE, per call site, and deliberately not folded into
# the shared constant — #1341's display oracles consume that constant and must
# keep rejecting the column.
A_SEGMENT_BLOCK_ALLOWED_AIDS: tuple[str, ...] = SANCTIONED_TEXT_PUSHDOWN_COLUMNS + ("river_segment_id",)
A_SEGMENT_BLOCK_AIDS = {"river_segment_id", "river_network_version_id", "variable"}
A_FALLBACK_AIDS = {"run_id", "river_network_version_id", "variable"}
PUBLISHER_AIDS = {"variable"}
COPYBACK_AIDS = {"variable"}
NO_AIDS: set[str] = set()

_IDENTITY = {
    "basin_version_id": "basin_v1",
    "segment_id": "seg_001",
    "river_network_version_id": "rivnet_v1",
}
_T0 = datetime(2026, 5, 7, tzinfo=UTC)
_T1 = datetime(2026, 5, 14, tzinfo=UTC)
_NO_FILTER = _ScenarioFilter("", ())


def _source(*parts: str) -> str:
    return REPO_ROOT.joinpath(*parts).read_text(encoding="utf-8")


def _sql_constants(
    *,
    module: tuple[str, ...],
    function: str,
    needle: str,
    cls: str | None = None,
) -> tuple[str, ...]:
    """String constants of one function that mention ``needle``, in source order.

    Two deliberate departures from ``tests.test_sql_shape_helpers.sql_literals``:

    * it keys on the table name, not on the word ``SELECT``. A bare ``DELETE``
      contains no ``SELECT``, and the parser's DELETE is one of the three
      statements this file exists to watch — keying on ``SELECT`` would skip it
      and every parser assertion downstream would pass on unswitched code.
    * it locates the function through the module's own AST instead of slicing
      text between anchors. Most of these call sites are METHODS, whose slice is
      not parseable Python on its own (leading indentation, a trailing
      decorator), and an anchor that silently stops matching after a refactor
      would make the pins vacuous rather than red.

    Python's parser does the finding either way: running a SQL tokenizer over raw
    Python lets an apostrophe in a prose comment swallow the statement.

    ``cls`` narrows the search to one class body, which the parser needs: three
    repository classes implement ``upsert_river_timeseries`` (the abstract base,
    the JSONL file repository and the psycopg one), and only the psycopg one
    talks SQL.
    """
    tree: ast.AST = ast.parse(REPO_ROOT.joinpath(*module).read_text(encoding="utf-8"))
    if cls is not None:
        classes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name == cls]
        assert len(classes) == 1, f"{'/'.join(module)}: expected exactly one class {cls}"
        tree = classes[0]
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == function
    ]
    assert len(matches) == 1, f"{'/'.join(module)}: expected exactly one {function}"
    found = [
        node
        for node in ast.walk(matches[0])
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and needle in node.value
    ]
    found.sort(key=lambda node: (node.lineno, node.col_offset))
    return tuple(node.value for node in found)


def _docstring_constants(tree: ast.AST) -> set[int]:
    """Identities of the ``ast.Constant`` nodes that are docstrings, not data."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            found.add(id(body[0].value))
    return found


def _river_table_mentions(source: str) -> int:
    """Occurrences of ``hydro.river_timeseries`` in non-docstring string constants.

    Parsed rather than grepped for the same reason ``_sql_constants`` is: raw
    source counts every ``#`` comment and docstring paragraph that names the
    table, which turns a prose edit into a census failure and teaches everyone
    to bump the number without reading it.
    """
    tree = ast.parse(source)
    docstrings = _docstring_constants(tree)
    return sum(
        node.value.count(RIVER_TABLE)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
    )


def _text_identity_predicate(sql: str, column: str, *, resolves_keys_inline: bool = False) -> re.Match[str] | None:
    """The first bare comparison of ONE text identity column, or ``None``.

    The single-column half of :func:`_assert_no_text_identity_predicate`, split
    out (#1681) because the parser's probe and window read now retain exactly
    one sanctioned aid (``run_id``) and must still be pinned zero-text on every
    OTHER identity column. Sharing the regex keeps the two statements of "no
    text predicate here" from drifting apart: a loosened pattern reddens both.
    """
    reduced = strip_all_subqueries(sql) if resolves_keys_inline else strip_scalar_subqueries(sql)
    outer = re.sub(r"\s+", " ", strip_comments(reduced))
    return re.search(rf"(?<![.\w]){re.escape(column)}\s*(=|<>|!=|\bIN\b|\bLIKE\b|=\s*ANY)", outer)


def _assert_no_text_identity_predicate(sql: str, label: str, *, resolves_keys_inline: bool = False) -> None:
    """No unaliased text identity column is compared to anything.

    For the surfaces that give the fact table no alias (the parser's three
    statements, and the bare-column register), ``text_fact_columns`` has nothing
    to qualify on, so the check is positional instead: a text identity column
    immediately followed by a comparison or membership operator. Column LISTS
    (``INSERT INTO ... (run_id, basin_version_id, ...)``) are unaffected, which
    is what keeps the dual-write INSERT out of scope here.

    ``resolves_keys_inline`` picks the stripper. The bare-fragment sites resolve
    identity with ``run_key IN (SELECT run_key FROM hydro.hydro_run WHERE run_id
    = %s)``: that authority ``WHERE`` is REQUIRED and must not read as a
    regression, and because ``IN`` is not comparison position the precise
    stripper keeps it — hence ``strip_all_subqueries``. The parser's statements
    must NOT use it: one of them is a CTE, whose body that stripper would delete
    along with the very predicates under test.
    """
    for column in TEXT_IDENTITY_COLUMNS:
        match = _text_identity_predicate(sql, column, resolves_keys_inline=resolves_keys_inline)
        assert match is None, f"{label}: text identity predicate on {column} -> {match.group(0)!r}"


def _assert_aids_are_marked(sql: str, alias: str, expected_aids: set[str], label: str) -> None:
    """Every sanctioned text conjunct of ``alias`` sits under the removal marker.

    Asserted per aid on the RAW sql (markers are comments, so they are gone from
    ``outer_predicates``): the marker must be ADJACENT to the aid — on the aid's
    own line or on the line immediately above it — and there must be at least
    one marker per aid so a single marker cannot cover a predicate that quietly
    grew a second text column.

    Adjacency, not "anywhere above" (#1442 round-2, ride-along F6). #1342's
    removal is a per-LINE deletion driven by this marker: a marker two lines up,
    or at the top of a constant, tells the reader nothing about which line to
    delete, and deleting the marker's own line leaves the aid behind — a text
    predicate on a column that no longer exists, i.e. a migration that fails at
    the first query rather than at the ALTER. The looser rule also went green on
    a statement whose aid had drifted away from its marker entirely.

    Alias-scoped, because these statements read more than one table: the
    latest-product fallback's station CTE carries ``fst.variable = ANY(...)`` on
    a met table that this change does not touch, and an unscoped check would
    demand a #1342 marker on it.
    """
    if not expected_aids:
        assert PUSHDOWN_AID_MARKER not in sql, f"{label}: marker present but no aid is sanctioned here"
        return
    lines = sql.splitlines()
    marker_lines = {index for index, line in enumerate(lines) if PUSHDOWN_AID_MARKER in line}
    assert len(marker_lines) >= len(expected_aids), f"{label}: fewer markers than sanctioned aids"
    for aid in expected_aids:
        aid_lines = [
            index
            for index, line in enumerate(lines)
            if re.search(rf"\b{re.escape(alias)}\.{re.escape(aid)}\b\s*(=|<>|!=)", strip_comments(line))
        ]
        assert aid_lines, f"{label}: sanctioned aid {aid} is not present at all"
        for aid_line in aid_lines:
            assert marker_lines & {aid_line - 1, aid_line}, (
                f"{label}: aid {aid} on line {aid_line + 1} carries no ADJACENT marker "
                f"(markers on lines {sorted(index + 1 for index in marker_lines)})"
            )


def _assert_no_text_fact_join(sql: str, alias: str, label: str) -> None:
    """No text identity column of the fact table is equated to another column.

    This is the delta's flat prohibition: an aid may bind a literal or a
    parameter, never another relation's column. ``rt.run_id = h.run_id`` is the
    canonical offender — it reads as a pushdown aid and is not one, because the
    planner has no constant to push.
    """
    outer = outer_predicates(sql)
    for column in TEXT_IDENTITY_COLUMNS:
        for direction in (
            rf"\b{re.escape(alias)}\.{column}\s*=\s*([A-Za-z_][A-Za-z0-9_]*)\.",
            rf"\b([A-Za-z_][A-Za-z0-9_]*)\.\w+\s*=\s*{re.escape(alias)}\.{column}\b",
        ):
            match = re.search(direction, outer)
            assert match is None, f"{label}: text fact join on {column} -> {match.group(0)!r}"


def _assert_switched_surface(
    sql: str,
    alias: str,
    expected_aids: set[str],
    label: str,
    allowed: tuple[str, ...] = SANCTIONED_TEXT_PUSHDOWN_COLUMNS,
) -> None:
    """The full per-surface contract, in one call.

    ``allowed`` is the ceiling the expectation is measured against; it defaults
    to the shared sanctioned set and is widened only by the A segment blocks
    (``A_SEGMENT_BLOCK_ALLOWED_AIDS``, design D10.7), so a future surface cannot
    grow a ``river_segment_id`` predicate just by inheriting a default.
    """
    assert_text_fact_columns(sql, alias, expected_aids, label, allowed=allowed)
    _assert_no_text_fact_join(sql, alias, label)
    _assert_aids_are_marked(sql, alias, expected_aids, label)
    # Adjacency (#1442 round-2, design D10.5): presence of the aid is not
    # enough — it must be AND-ed to the key/enum predicate that supersedes it,
    # or it is load-bearing rather than redundant and #1342 cannot delete it.
    for aid in sorted(expected_aids):
        assert_aid_is_conjoined_with_its_counterpart(sql, alias, aid, label)


# ---------------------------------------------------------------------------
# A — packages/common/forecast_store.py, nine query blocks
# ---------------------------------------------------------------------------


class _CaptureCursor:
    """Records every rendered statement; returns nothing, like an empty table.

    Rendering by execution rather than by reading source is what makes these
    assertions non-vacuous: the eight segment blocks interpolate a scenario /
    identity filter, and the fallback's heavy statement only exists after the
    header prefetch has run.
    """

    def __init__(self, rows_by_call: list[list[dict[str, Any]]] | None = None) -> None:
        self.executed: list[tuple[str, Any]] = []
        self._rows_by_call = list(rows_by_call or [])
        self._pending: list[dict[str, Any]] = []

    def execute(self, statement: str, parameters: Any = None) -> None:
        self.executed.append((statement, parameters))
        self._pending = self._rows_by_call.pop(0) if self._rows_by_call else []

    def fetchall(self) -> list[dict[str, Any]]:
        rows, self._pending = self._pending, []
        return rows

    def fetchone(self) -> dict[str, Any] | None:
        rows = self.fetchall()
        return rows[0] if rows else None

    @property
    def statements(self) -> list[str]:
        return [statement for statement, _parameters in self.executed]


def _store() -> PsycopgForecastStore:
    return PsycopgForecastStore("postgresql://unit-test")


def _segment_block_statements() -> dict[str, str]:
    """The eight segment-scoped blocks, rendered by their real call sites."""
    store = _store()
    rendered: dict[str, str] = {}

    def capture(label: str, call: Any) -> None:
        cursor = _CaptureCursor()
        call(cursor)
        assert len(cursor.statements) == 1, label
        rendered[label] = cursor.statements[0]

    capture(
        "latest_issue_time",
        lambda cursor: store._latest_issue_time(cursor, **_IDENTITY, scenario_filter=_NO_FILTER),
    )
    capture(
        "per_source_latest_cycles",
        lambda cursor: store._per_source_latest_cycles(
            cursor, **_IDENTITY, scenario_filter=_NO_FILTER, identity_filter=_NO_FILTER
        ),
    )
    capture(
        "latest_analysis_issue_time",
        lambda cursor: store._latest_analysis_issue_time(cursor, **_IDENTITY),
    )
    capture(
        "analysis_segment_rows",
        lambda cursor: store._fetch_analysis_segment_rows(cursor, **_IDENTITY, start_time=_T0, end_time=_T1),
    )
    capture(
        "forecast_segment_rows_selected_cycles",
        lambda cursor: store._fetch_forecast_segment_rows(
            cursor,
            **_IDENTITY,
            issue_time=_T0,
            scenario_filter=_NO_FILTER,
            identity_filter=_NO_FILTER,
            cycle_times_by_scenario={"forecast_gfs_deterministic": _T0},
        ),
    )
    capture(
        "forecast_segment_rows",
        lambda cursor: store._fetch_forecast_segment_rows(
            cursor,
            **_IDENTITY,
            issue_time=_T0,
            scenario_filter=_NO_FILTER,
            identity_filter=_NO_FILTER,
        ),
    )
    capture(
        "latest_run_type_valid_time",
        lambda cursor: store._latest_run_type_valid_time(cursor, **_IDENTITY, run_types=["hindcast"]),
    )
    capture(
        "run_type_segment_rows",
        lambda cursor: store._fetch_run_type_segment_rows(
            cursor, **_IDENTITY, run_types=["hindcast"], end_time=_T1
        ),
    )
    return rendered


def _latest_product_fallback_statement() -> str:
    """The latest-product fallback's heavy statement (A9), rendered."""
    header = {
        "run_id": "qhh_gfs_2026050700",
        "forcing_version_id": "forc_qhh_gfs_2026050700",
        "basin_version_id": "basins_qhh_vbasins",
        "river_network_version_id": "basins_qhh_rivnet_vbasins",
        "source_id_lower": "gfs",
        "display_start_time": _T0,
        "display_end_time": _T1,
    }
    # to_regclass probe -> no coverage table -> header prefetch -> heavy CTE.
    cursor = _CaptureCursor([[{"reg": None}], [header], []])
    _store()._fetch_latest_qhh_display_candidates(cursor, basin_id="basins_qhh", source_id="GFS")
    heavy = [statement for statement in cursor.statements if "river_sample_rows AS" in statement]
    assert len(heavy) == 1
    return heavy[0]


def test_forecast_store_segment_blocks_carry_only_their_sanctioned_aids() -> None:
    for label, sql in _segment_block_statements().items():
        _assert_switched_surface(
            sql,
            "rt",
            A_SEGMENT_BLOCK_AIDS,
            f"forecast_store {label}",
            allowed=A_SEGMENT_BLOCK_ALLOWED_AIDS,
        )


def test_forecast_store_segment_blocks_keep_the_measured_segment_pushdown_aid() -> None:
    """Presence pin for the D10.7 aid, verbatim, in all eight blocks.

    The ceiling check above is an equality and would already redden on removal,
    but this states the requirement in the form the next reader will search for:
    a "finish the cleanup, drop the last text predicate" edit is the regression
    E4(ii) caught on node-27 — the compressed leg loses segmentby pruning and
    decompresses the whole network (32660 batches / 18549ms, against 40 / 1085ms
    with the aid) — and it must fail here rather than in a plan nobody
    re-measures.
    """
    rendered = _segment_block_statements()
    assert len(rendered) == 8
    for label, sql in rendered.items():
        assert "AND rt.river_segment_id = %s" in sql, label


def test_forecast_store_segment_blocks_resolve_identity_through_the_authority_tables() -> None:
    """Non-vacuity: every block really predicates on the four keys and the enum."""
    for label, sql in _segment_block_statements().items():
        assert "rt.basin_version_key = (" in sql, label
        assert "SELECT basin_version_key FROM core.basin_version" in sql, label
        assert "rt.river_segment_key = (" in sql, label
        # The segment resolution binds the network too: core.river_segment's
        # primary key is (river_segment_id, river_network_version_id), so a bare
        # segment lookup could return more than one row and raise at runtime.
        assert "SELECT river_segment_key FROM core.river_segment" in sql, label
        assert "AND river_network_version_id = %s" in sql, label
        assert "rt.river_network_version_key = (" in sql, label
        assert "rt.variable_e = 'q_down'::hydro.river_variable" in sql, label
        # The run is reached by key, never by its text.
        assert "JOIN hydro.hydro_run h ON h.run_key = rt.run_key" in sql, label


def test_forecast_store_segment_blocks_bind_every_placeholder_they_grew() -> None:
    """Placeholder arithmetic, with a non-empty filter interpolated.

    Switching to keys turned three text bindings into six placeholders per
    block: the segment binds twice (key resolution, D10.7 pushdown aid) and the
    network three times (segment resolution, own pushdown aid, own key
    resolution). psycopg2 raises at execute time on a count mismatch, so a
    dropped binding is a production-only crash that a text-shape assertion never
    sees — and the scenario / identity filters append their own ``%s`` after the
    identity block, which is where an off-by-one would actually land.
    """
    store = _store()
    scenarios = _ScenarioFilter(" AND h.scenario_id = ANY(%s)", (["GFS"],))
    identity = _ScenarioFilter(" AND h.run_id = %s", ("run_a",))
    calls = (
        ("latest_issue_time", lambda cursor: store._latest_issue_time(cursor, **_IDENTITY, scenario_filter=scenarios)),
        (
            "per_source_latest_cycles",
            lambda cursor: store._per_source_latest_cycles(
                cursor, **_IDENTITY, scenario_filter=scenarios, identity_filter=identity
            ),
        ),
        ("latest_analysis_issue_time", lambda cursor: store._latest_analysis_issue_time(cursor, **_IDENTITY)),
        (
            "analysis_segment_rows",
            lambda cursor: store._fetch_analysis_segment_rows(cursor, **_IDENTITY, start_time=_T0, end_time=_T1),
        ),
        (
            "forecast_segment_rows_selected_cycles",
            lambda cursor: store._fetch_forecast_segment_rows(
                cursor,
                **_IDENTITY,
                issue_time=_T0,
                scenario_filter=scenarios,
                identity_filter=identity,
                cycle_times_by_scenario={"a": _T0, "b": _T1},
            ),
        ),
        (
            "forecast_segment_rows",
            lambda cursor: store._fetch_forecast_segment_rows(
                cursor, **_IDENTITY, issue_time=_T0, scenario_filter=scenarios, identity_filter=identity
            ),
        ),
        (
            "latest_run_type_valid_time",
            lambda cursor: store._latest_run_type_valid_time(cursor, **_IDENTITY, run_types=["hindcast"]),
        ),
        (
            "run_type_segment_rows",
            lambda cursor: store._fetch_run_type_segment_rows(
                cursor, **_IDENTITY, run_types=["hindcast"], end_time=_T1
            ),
        ),
    )
    for label, call in calls:
        cursor = _CaptureCursor()
        call(cursor)
        statement, parameters = cursor.executed[0]
        assert len(re.findall("%s", statement)) == len(parameters), label
        # The six identity bindings, in text order, are the caller's three
        # values with the segment and the network repeated — not a key, and not
        # reordered. Order is dictated by the constant: basin, then the
        # segment-key sub-select's (segment, network), then the segment aid, the
        # network aid, and the network's own resolution.
        expected = (
            _IDENTITY["basin_version_id"],
            _IDENTITY["segment_id"],
            _IDENTITY["river_network_version_id"],
            _IDENTITY["segment_id"],
            _IDENTITY["river_network_version_id"],
            _IDENTITY["river_network_version_id"],
        )
        # The selected-cycles branch prepends its VALUES list: two bindings
        # (scenario, cycle_time) per selected cycle, two cycles in this fixture.
        offset = 4 if label == "forecast_segment_rows_selected_cycles" else 0
        assert tuple(parameters[offset : offset + 6]) == expected, label


def test_forecast_store_segment_blocks_emit_unit_and_network_without_the_text_columns() -> None:
    """Output side, not just predicates: the wire values come back restored."""
    rendered = _segment_block_statements()
    for label in ("analysis_segment_rows", "forecast_segment_rows", "run_type_segment_rows"):
        assert "rt.unit_e::text AS unit" in rendered[label], label
    for label in (
        "forecast_segment_rows_selected_cycles",
        "forecast_segment_rows",
        "run_type_segment_rows",
    ):
        assert "rnv.river_network_version_id" in rendered[label], label
        assert "JOIN core.river_network_version rnv" in rendered[label], label


def test_latest_product_fallback_river_chain_is_keyed_end_to_end() -> None:
    sql = _latest_product_fallback_statement()

    _assert_switched_surface(sql, "rt", A_FALLBACK_AIDS, "forecast_store latest-product fallback")
    # The join to candidate_runs is key-only on all three identities.
    for key in ("run_key", "basin_version_key", "river_network_version_key"):
        assert f"cr.{key} = rt.{key}" in sql, key
    # The whole downstream chain groups by keys...
    river_chain = sql[sql.index("river_sample_rows AS") : sql.index("hydro_coverage AS")]
    assert "GROUP BY run_key, basin_version_key, river_network_version_key, river_segment_key" in river_chain
    assert "COUNT(DISTINCT river_segment_key)" in river_chain
    for text_column in ("basin_version_id", "river_segment_id", "river_network_version_id"):
        assert f"GROUP BY {text_column}" not in river_chain, text_column
    # ...and the text identity is reconstructed exactly once, at the rollup.
    rollup = sql[sql.index("hydro_coverage AS") :]
    assert "JOIN hydro.hydro_run rollup_run" in rollup
    assert "JOIN core.basin_version rollup_basin" in rollup
    assert "JOIN core.river_network_version rollup_network" in rollup
    assert "rollup_network.river_network_version_id" in rollup


def test_latest_product_fallback_scan_guards_still_fold_away_on_a_null_binding() -> None:
    """Each scan_* guard keeps its ``IS NULL`` escape, so an unpinned refresh is unchanged.

    Pinned as five WHOLE-GUARD exact substrings (#1442 round-2, F5). Asserting
    only that ``(%(scan_x)s IS NULL`` appears somewhere in the CTE is satisfied
    by a guard whose OR branch has drifted to a different column, so the
    fold-away contract — bind NULL, the guard disappears, the scan reverts to
    the full window — could be broken with nothing red. Written against
    ``outer_predicates`` (sub-selects stripped, comments removed, whitespace
    collapsed) so re-indenting the CTE cannot break the pin, which is what makes
    the verbatim form maintainable (#1341 idiom).

    These verbatim pins are the ONLY structural defense for the guards' shape:
    the computed adjacency invariant deliberately does not parse parentheses,
    ``OR`` branches or ``NOT`` (see
    :func:`tests.test_sql_shape_helpers.assert_aid_is_conjoined_with_its_counterpart`),
    so it cannot see a guard whose escape branch has drifted. Do not simplify or
    delete these substrings on the grounds that the adjacency check covers them.
    """
    sql = _latest_product_fallback_statement()
    river_cte = sql[sql.index("river_sample_rows AS") : sql.index("river_identity_coverage AS")]
    outer = outer_predicates(river_cte)

    # run_id / river_network_version_id keep their transitional text conjunct
    # INSIDE the guard, AND-ed with the key predicate that supersedes it; the
    # `= )` tail is the key-resolution sub-select the stripper removed.
    assert "AND (%(scan_run_id)s IS NULL OR (rt.run_id = %(scan_run_id)s AND rt.run_key = ))" in outer
    assert (
        "AND (%(scan_river_network_version_id)s IS NULL "
        "OR (rt.river_network_version_id = %(scan_river_network_version_id)s "
        "AND rt.river_network_version_key = ))"
    ) in outer
    # basin_version_id is not a sanctioned aid, so its guard is key-only: it no
    # longer appears as a fact predicate at all, only inside the authority
    # resolution the stripper removes.
    assert "AND (%(scan_basin_version_id)s IS NULL OR rt.basin_version_key = )" in outer
    assert "AND (%(scan_display_start)s IS NULL OR rt.valid_time >= %(scan_display_start)s)" in outer
    assert "AND (%(scan_display_end)s IS NULL OR rt.valid_time <= %(scan_display_end)s)" in outer
    # Non-vacuity for the stripped tails: the resolutions really are there.
    assert "rt.run_key = (SELECT run_key FROM hydro.hydro_run" in river_cte
    assert "rt.basin_version_key = (SELECT basin_version_key FROM core.basin_version" in river_cte
    assert "rt.river_network_version_key = (SELECT river_network_version_key" in river_cte


# ---------------------------------------------------------------------------
# B — services/tile_publisher/publisher.py
# ---------------------------------------------------------------------------

_PUBLISHER_OPTIONAL = {"select": "h.run_manifest_uri, h.output_uri,", "group": ", h.run_manifest_uri"}
_PUBLISHER_FORCING = {
    "select": "fv.forcing_version_id AS forcing_row_forcing_version_id,",
    "join": "LEFT JOIN met.forcing_version fv ON fv.forcing_version_id = h.forcing_version_id",
    "group": ", fv.forcing_version_id",
}
_PUBLISHER_WHERE = [
    "h.run_type = 'forecast'",
    "h.status IN ('succeeded', 'parsed', 'published')",
    "r.variable_e = 'q_down'",
    "lower(h.source_id) = :source_id",
]


def _publisher_sql(*, is_sqlite: bool) -> str:
    return publisher_module._qdown_discovery_sql(
        is_sqlite=is_sqlite,
        optional=_PUBLISHER_OPTIONAL,
        forcing=_PUBLISHER_FORCING,
        where_clauses=_PUBLISHER_WHERE,
    )


@pytest.mark.parametrize("is_sqlite", [False, True], ids=["postgresql", "sqlite"])
def test_publisher_discovery_carries_only_the_variable_aid_on_both_dialects(is_sqlite: bool) -> None:
    sql = _publisher_sql(is_sqlite=is_sqlite)

    _assert_switched_surface(sql, "r", PUBLISHER_AIDS, f"publisher (sqlite={is_sqlite})")
    assert "ON r.run_key = h.run_key" in sql


def test_publisher_discovery_counts_segments_by_key_on_each_dialect() -> None:
    """The production leg counts the key tuple; sqlite gets the equivalent it can run.

    sqlite rejects ``COUNT(DISTINCT (a, b))`` outright ("row value misused"), so
    the two legs cannot share one expression. Both count the same thing: the
    integer concatenation cannot collide the way a text one could.
    """
    postgres_sql = _publisher_sql(is_sqlite=False)
    sqlite_sql = _publisher_sql(is_sqlite=True)

    assert "COUNT(DISTINCT (r.river_network_version_key, r.river_segment_key))" in postgres_sql
    assert "COUNT(DISTINCT r.river_network_version_key || ':' || r.river_segment_key)" in sqlite_sql
    for sql in (postgres_sql, sqlite_sql):
        assert "r.river_network_version_id || '::' || r.river_segment_id" not in sql


def test_publisher_discovery_aggregates_enums_and_restores_the_network_text() -> None:
    postgres_sql = _publisher_sql(is_sqlite=False)
    sqlite_sql = _publisher_sql(is_sqlite=True)

    assert "STRING_AGG(DISTINCT r.unit_e::text, ',')" in postgres_sql
    assert "STRING_AGG(DISTINCT r.quality_flag_e::text, ',')" in postgres_sql
    assert "GROUP_CONCAT(DISTINCT r.unit_e)" in sqlite_sql
    assert "GROUP_CONCAT(DISTINCT r.quality_flag_e)" in sqlite_sql
    for sql in (postgres_sql, sqlite_sql):
        # layer_id is assembled from this restored text, so it must be the
        # authority's value and it must be grouped by the key.
        assert "JOIN core.river_network_version rnv" in sql
        assert "rnv.river_network_version_id" in sql
        assert "r.river_network_version_key," in sql


def test_publisher_where_clause_fragments_name_no_text_identity_column() -> None:
    """Bare-fragment face: ``_discover_qdown_runs``'s own ``where_clauses`` list.

    The fragments are joined onto one line, so this is also where a stray ``--``
    marker would comment out every clause after it — hence the aid lives in the
    ON clause and this list stays text-free.
    """
    fragments = _sql_constants(
        module=("services", "tile_publisher", "publisher.py"),
        function="_discover_qdown_runs",
        needle="r.",
    )
    fact_fragments = [fragment for fragment in fragments if fragment.startswith("r.")]

    assert fact_fragments == ["r.variable_e = 'q_down'"]
    for fragment in fact_fragments:
        assert PUSHDOWN_AID_MARKER not in fragment, fragment
        assert_text_fact_columns(fragment, "r", set(), f"publisher fragment {fragment!r}")


# ---------------------------------------------------------------------------
# C — services/tile_publisher/forcing_copyback_backfill.py
# ---------------------------------------------------------------------------


def test_copyback_discovery_probe_correlates_on_the_run_key() -> None:
    sql = backfill_module._DISCOVER_BACKFILL_RUNS_SQL

    _assert_switched_surface(sql, "rt", COPYBACK_AIDS, "copyback discovery")
    assert "WHERE rt.run_key = h.run_key" in sql
    assert "AND rt.variable_e = 'q_down'" in sql


# ---------------------------------------------------------------------------
# D — scripts/node27_autopipeline.py (the two per-tick criteria)
# ---------------------------------------------------------------------------


def _autopipeline_statement(function: str) -> str:
    statements = _sql_constants(
        module=("scripts", "node27_autopipeline.py"),
        function=function,
        needle="hydro.river_timeseries",
    )
    assert len(statements) == 1, f"{function}: expected exactly one river_timeseries statement"
    return statements[0]


def test_autopipeline_ingest_criterion_joins_by_key_with_no_aid() -> None:
    sql = _autopipeline_statement("_already_ingested_runs")

    _assert_switched_surface(sql, "rt", NO_AIDS, "autopipeline _already_ingested_runs")
    assert "ON rt.run_key = h.run_key" in sql


def test_autopipeline_ingest_criterion_is_authority_state_first() -> None:
    """#1674: 'published' is complete on its own; 'parsed' still needs key rows.

    Sits beside the #1442 shape pin rather than in a new file because
    ``scripts/select_ci_tests.py`` maps the autopipeline script to exactly this
    oracle (``test_select_ci_tests.py`` asserts that set by equality), so a new
    file would either never run on the PR that changes the script or redden the
    selector's own guard.

    The negatives are the load-bearing half. ``COALESCE`` would mean someone
    gave ``parsed_at`` a fallback, and ``updated_at`` would mean that fallback
    is ``hydro_run.updated_at`` -- which every tick's register upsert bumps, so
    it is a false parse timestamp that would make recompute detection claim a
    currency it does not have (design D1).
    """
    sql = _autopipeline_statement("_already_ingested_runs")

    assert "LEFT JOIN hydro.river_timeseries rt" in sql
    assert "ON rt.run_key = h.run_key" in sql
    assert "HAVING h.status = 'published' OR COUNT(rt.run_key) > 0" in sql
    assert "MAX(rt.created_at) AS parsed_at" in sql
    assert "COALESCE" not in sql
    assert "updated_at" not in sql


def test_autopipeline_publish_criterion_correlates_by_key_with_no_aid() -> None:
    sql = _autopipeline_statement("_publish_display_runs")

    _assert_switched_surface(sql, "rt", NO_AIDS, "autopipeline _publish_display_runs")
    assert "WHERE rt.run_key = h.run_key" in sql


# ---------------------------------------------------------------------------
# F — workers/output_parser/parser.py, all three replace-chain statements
# ---------------------------------------------------------------------------


def _parser_river_statements() -> tuple[str, ...]:
    return _sql_constants(
        module=("workers", "output_parser", "parser.py"),
        cls="PsycopgOutputParserRepository",
        function="upsert_river_timeseries",
        needle="hydro.river_timeseries",
    )


def test_parser_replace_chain_has_exactly_three_read_statements_plus_the_insert() -> None:
    """Non-vacuity guard for the two tests below.

    The DELETE contains no ``SELECT``, so the shared ``sql_literals`` helper
    would silently skip it and every parser assertion downstream would pass on
    unswitched code — the exact failure class this file's docstring records.
    """
    statements = _parser_river_statements()

    assert len(statements) == 4
    assert statements[0].strip().startswith("SELECT 1 FROM hydro.river_timeseries")
    assert statements[1].strip().startswith("WITH existing AS MATERIALIZED")
    assert statements[2].strip().startswith("DELETE FROM hydro.river_timeseries")
    assert statements[3].strip().startswith("INSERT INTO hydro.river_timeseries")


# The three-line shape the probe and the window read must both carry (#1681):
# the key predicate, the removal marker, then the aid — so the aid is separated
# from `run_key` by exactly one AND and the marker is on the line immediately
# above the line #1342 has to delete. Whitespace between lines is free; the
# ORDER is not.
_PARSER_AID_ADJACENCY = re.compile(
    rf"WHERE run_key = %s[ \t]*\n[ \t]*{re.escape(PUSHDOWN_AID_MARKER)}[ \t]*\n[ \t]*AND run_id = %s"
)
# Text identity columns the read statements must STILL have no predicate on:
# every text identity column other than the sanctioned `run_id` aid, which is
# asserted positively above. DERIVED from the shared register rather than
# listed, so this test keeps the same coverage
# `_assert_no_text_identity_predicate` gave these statements before #1681 split
# `run_id` out of it, and a column added to 000050's text set is covered here
# the day it is classified. (A hand-written list dropped `unit` and
# `quality_flag` — the two columns the pre-#1681 check did cover.)
_PARSER_READ_FORBIDDEN_TEXT_COLUMNS: tuple[str, ...] = tuple(
    column for column in TEXT_IDENTITY_COLUMNS if column != "run_id"
)


def test_parser_probe_and_window_locate_rows_by_key_with_one_marked_run_id_aid() -> None:
    """Probe + window read: key predicates plus the sanctioned `run_id` aid (#1681).

    #1442 left these two statements key-only on the reasoning that
    ``check_batch_targets_uncompressed`` guarantees the target chunk is
    uncompressed. That argument holds for the DELETE (which is valid_time
    bounded) and NOT for these two: they carry no valid_time constraint by
    design — their whole job is to find this key's rows OUTSIDE the incoming
    window — so their plan necessarily reaches compressed chunks, where
    ``run_key`` is not a segmentby column and has no access path at all. The
    retained ``run_id`` is a bound parameter that reaches 000047's segmentby
    index, i.e. exactly the "literal/bound parameter AND the plan can reach a
    compressed chunk" condition this file's header sanctions.

    Asserted as a bare-column surface (these statements give the fact table no
    alias, so the shared alias machinery has nothing to qualify on):

    * the aid is conjoined with its counterpart AND carries an adjacent marker,
      pinned in one cross-line regex — the bare-column equivalent of
      ``assert_aid_is_conjoined_with_its_counterpart`` plus
      ``_assert_aids_are_marked``;
    * exactly one ``run_id`` occurrence and exactly one marker, so a second,
      unconjoined predicate on the same column cannot hide behind the first;
    * every text identity column OTHER than the sanctioned `run_id` aid is
      still predicate-free — the whole set 000050 defines, minus that one aid.
    """
    probe, window, _delete, _insert = _parser_river_statements()

    # Self-check on the derivation: the forbidden set is the complete text
    # identity register minus the single sanctioned aid. Pinned here so the
    # tuple above cannot silently shrink back to a partial hand-written list
    # (which is how `unit` and `quality_flag` fell out of this test's reach).
    assert set(_PARSER_READ_FORBIDDEN_TEXT_COLUMNS) == set(TEXT_IDENTITY_COLUMNS) - {"run_id"}

    for label, sql in (("probe", probe), ("window", window)):
        assert "WHERE run_key = %s" in sql, label
        assert "AND river_network_version_key = %s" in sql, label
        assert "AND variable_e = %s" in sql, label
        assert _PARSER_AID_ADJACENCY.search(sql) is not None, (
            f"parser {label}: expected `WHERE run_key = %s` / {PUSHDOWN_AID_MARKER!r} / "
            f"`AND run_id = %s` on three consecutive lines, got:\n{sql}"
        )
        assert len(re.findall(r"(?<![.\w])run_id\b", sql)) == 1, (
            f"parser {label}: `run_id` must appear exactly once — the marked aid"
        )
        assert sql.count(PUSHDOWN_AID_MARKER) == 1, f"parser {label}: exactly one removal marker"
        for column in _PARSER_READ_FORBIDDEN_TEXT_COLUMNS:
            match = _text_identity_predicate(sql, column)
            assert match is None, f"parser {label}: text identity predicate on {column} -> {match.group(0)!r}"
    # The MATERIALIZED fence stays: without it the planner turns the window read
    # back into a per-chunk min/max walk.
    assert "WITH existing AS MATERIALIZED" in window


def test_parser_delete_locates_rows_by_key_with_no_aid() -> None:
    """The DELETE stays zero-text: the guard really does close it (#1442 D1, F).

    Unlike the two read statements it is valid_time bounded and runs only after
    ``check_batch_targets_uncompressed`` passed, so every chunk it can touch is
    uncompressed and 000051's key index is a complete access path. No aid is
    sanctioned here, and the marker's absence is asserted so #1681's aid cannot
    be copied into this statement without a decision.
    """
    _probe, _window, delete, _insert = _parser_river_statements()

    assert "WHERE run_key = %s" in delete
    assert "AND river_network_version_key = %s" in delete
    assert "AND variable_e = %s" in delete
    _assert_no_text_identity_predicate(delete, "parser delete")
    assert PUSHDOWN_AID_MARKER not in delete, "parser delete: no aid is sanctioned here"
    # The window predicate is untouched: replace semantics depend on it, and the
    # guard's compressed-chunk verdict is computed from what this returns.
    assert "AND valid_time >= %s" in delete
    assert "AND valid_time <= %s" in delete


def test_parser_dual_write_insert_still_writes_both_representations() -> None:
    """The replace chain switched; the INSERT must NOT — #1340's dual write stands.

    If this ever goes red because the text columns were dropped from the INSERT,
    the rows stop being readable by anything still on text and #1342's ordering
    has been violated from the write side.

    Asserted against the INSERT's column-list PARENTHETICAL, not against the
    statement text (#1442 round-2, F4). A bare ``"run_id" in insert`` is
    satisfied by the ``ON CONFLICT (run_id, ...)`` clause further down, so the
    text columns could be deleted from the write list — the very regression this
    test names — while every one of these assertions stayed green.
    """
    insert = _parser_river_statements()[3]
    match = re.search(r"INSERT INTO hydro\.river_timeseries\s*\((?P<columns>[^)]*)\)\s*VALUES", insert, re.S)
    assert match is not None, insert
    columns = [column.strip() for column in match.group("columns").split(",") if column.strip()]

    # Non-vacuity: the parenthetical really is the write list, not the conflict
    # target (which names five columns, all of them also here).
    assert len(columns) == 17, columns
    for column in ("run_id", "basin_version_id", "river_network_version_id", "river_segment_id"):
        assert column in columns, column
    for column in ("run_key", "basin_version_key", "river_network_version_key", "river_segment_key"):
        assert column in columns, column
    for column in ("variable_e", "unit_e", "quality_flag_e"):
        assert column in columns, column


# ---------------------------------------------------------------------------
# E — bare-column / fragment surfaces, one assertion per call site
# ---------------------------------------------------------------------------


def test_smoke_summary_counts_segments_and_filters_by_key() -> None:
    statements = _sql_constants(
        module=("scripts", "summarize_qhh_smoke_results.py"),
        function="main",
        needle="hydro.river_timeseries",
    )
    assert len(statements) == 1
    sql = statements[0]

    assert "count(DISTINCT river_segment_key) AS segment_count" in sql
    assert "WHERE run_key = (SELECT run_key FROM hydro.hydro_run WHERE run_id = %s)" in sql
    _assert_no_text_identity_predicate(sql, "summarize_qhh_smoke_results", resolves_keys_inline=True)
    assert "count(DISTINCT river_segment_id)" not in sql


def test_seed_demo_verification_counts_by_run_key() -> None:
    statements = _sql_constants(
        module=("db", "seeds", "seed_demo.py"),
        function="collect_counts",
        # "FROM ..." rather than the bare table name: the same list carries the
        # human-readable LABEL "hydro.river_timeseries" for each count.
        needle="FROM hydro.river_timeseries",
    )
    assert len(statements) == 2

    for sql in statements:
        assert "SELECT run_key FROM hydro.hydro_run" in sql
        _assert_no_text_identity_predicate(sql, "seed_demo verification", resolves_keys_inline=True)
    assert "WHERE run_key = (" in statements[0]
    assert "WHERE run_key IN (" in statements[1]


def test_reset_smoke_db_delete_fragment_targets_the_run_key() -> None:
    """``_delete`` takes its WHERE as a bare fragment — no statement to parse."""
    fragments = _sql_constants(
        module=("scripts", "reset_qhh_smoke_db.py"),
        function="main",
        needle="FROM hydro.hydro_run",
    )
    river_fragments = [fragment for fragment in fragments if fragment.startswith("run_key")]
    assert len(river_fragments) == 1

    assert river_fragments[0] == "run_key IN (SELECT run_key FROM hydro.hydro_run WHERE run_id = ANY(%s))"
    _assert_no_text_identity_predicate(river_fragments[0], "reset fragment", resolves_keys_inline=True)


def test_integration_helpers_delete_targets_the_run_key() -> None:
    statements = _sql_constants(
        module=("tests", "integration_helpers.py"),
        function="_clear_issue_126_rows",
        needle="hydro.river_timeseries",
    )
    # #1640/#1654 put a min/max valid_time probe in front of the DELETE, so
    # select each statement by CONTENT: the probe precedes the DELETE, and an
    # index-based pick would silently start asserting about the wrong one.
    assert len(statements) == 2

    deletes = [statement for statement in statements if "DELETE FROM" in statement]
    assert len(deletes) == 1
    assert "WHERE run_key IN (" in deletes[0]
    _assert_no_text_identity_predicate(deletes[0], "integration_helpers cleanup", resolves_keys_inline=True)

    probes = [statement for statement in statements if "min(valid_time)" in statement]
    assert len(probes) == 1
    assert "WHERE run_key IN (" in probes[0]
    _assert_no_text_identity_predicate(probes[0], "integration_helpers cleanup probe", resolves_keys_inline=True)


# ---------------------------------------------------------------------------
# Counter-examples: the oracle's own contract (tasks 2.2)
#
# Every assertion helper above must be shown to bite. A cleanup oracle that
# cannot be made red is indistinguishable from no oracle at all, and round-1 of
# #1341 shipped exactly that.
# ---------------------------------------------------------------------------

_SWITCHED_SPECIMEN = f"""
    SELECT rt.valid_time, rt.unit_e::text AS unit
    FROM hydro.river_timeseries rt
    JOIN hydro.hydro_run h ON h.run_key = rt.run_key
    WHERE rt.basin_version_key = (
              SELECT basin_version_key FROM core.basin_version WHERE basin_version_id = %s
          )
      AND rt.river_segment_key = (
              SELECT river_segment_key FROM core.river_segment
              WHERE river_segment_id = %s AND river_network_version_id = %s
          )
      {PUSHDOWN_AID_MARKER}
      AND rt.river_segment_id = %s
      {PUSHDOWN_AID_MARKER}
      AND rt.river_network_version_id = %s
      AND rt.river_network_version_key = (
              SELECT river_network_version_key FROM core.river_network_version
              WHERE river_network_version_id = %s
          )
      {PUSHDOWN_AID_MARKER}
      AND rt.variable = 'q_down'
      AND rt.variable_e = 'q_down'::hydro.river_variable
"""


def _assert_specimen_surface(sql: str, label: str) -> None:
    """``_assert_switched_surface`` at the A segment blocks' widened ceiling."""
    _assert_switched_surface(sql, "rt", A_SEGMENT_BLOCK_AIDS, label, allowed=A_SEGMENT_BLOCK_ALLOWED_AIDS)


def test_the_specimen_the_counter_examples_mutate_is_itself_green() -> None:
    """Without this, a counter-example could be red for the wrong reason."""
    _assert_specimen_surface(_SWITCHED_SPECIMEN, "specimen")


def test_a_forbidden_text_predicate_turns_the_oracle_red() -> None:
    """``basin_version_id`` stays forbidden on every surface in this register.

    ``river_segment_id`` used to be injected here too; since D10.7 it is the
    group's third sanctioned aid, so the forbidden case is basin alone — and the
    ceiling that admits the segment aid still has to reject this one.
    """
    mutated = _SWITCHED_SPECIMEN.replace(
        "WHERE rt.basin_version_key = (",
        "WHERE rt.basin_version_id = %s AND rt.basin_version_key = (",
    )

    with pytest.raises(AssertionError):
        _assert_specimen_surface(mutated, "forbidden predicate")


def test_an_unmarked_aid_turns_the_oracle_red() -> None:
    mutated = _SWITCHED_SPECIMEN.replace(PUSHDOWN_AID_MARKER, "-- keep this one", 1)

    with pytest.raises(AssertionError):
        _assert_specimen_surface(mutated, "unmarked aid")


def test_a_text_fact_join_turns_the_oracle_red() -> None:
    """The forbidden shape that most looks like a legitimate pushdown aid."""
    mutated = _SWITCHED_SPECIMEN.replace(
        "JOIN hydro.hydro_run h ON h.run_key = rt.run_key",
        "JOIN hydro.hydro_run h ON h.run_key = rt.run_key AND rt.run_id = h.run_id",
    )

    # It is caught twice over: as an out-of-ceiling column for this group, and
    # as a text fact join regardless of group.
    with pytest.raises(AssertionError):
        _assert_specimen_surface(mutated, "text fact join")
    with pytest.raises(AssertionError):
        _assert_no_text_fact_join(mutated, "rt", "text fact join")


def test_a_text_fact_join_is_red_even_where_that_column_is_a_sanctioned_aid() -> None:
    """``run_id`` is sanctioned for the fallback — but only as a bound constant."""
    mutated = _SWITCHED_SPECIMEN.replace(
        "JOIN hydro.hydro_run h ON h.run_key = rt.run_key",
        f"JOIN hydro.hydro_run h ON h.run_key = rt.run_key\n      {PUSHDOWN_AID_MARKER}\n"
        "      AND rt.run_id = h.run_id",
    )

    assert_text_fact_columns(
        mutated,
        "rt",
        A_FALLBACK_AIDS | {"river_segment_id"},
        "widened group",
        allowed=A_SEGMENT_BLOCK_ALLOWED_AIDS,
    )
    with pytest.raises(AssertionError):
        _assert_no_text_fact_join(mutated, "rt", "widened group")


def test_an_aid_whose_counterpart_disappears_turns_the_oracle_red() -> None:
    """M10/M30: delete the enum conjunct, keep the text one.

    Every other check stays green — the aid set is unchanged, nothing forbidden
    appeared, the marker is still adjacent — so before the adjacency invariant
    this mutation shipped a surface that reads identity from the text column and
    would break outright when #1342 drops it.
    """
    mutated = _SWITCHED_SPECIMEN.replace("\n      AND rt.variable_e = 'q_down'::hydro.river_variable", "")

    assert_text_fact_columns(
        mutated, "rt", A_SEGMENT_BLOCK_AIDS, "counterpart dropped", allowed=A_SEGMENT_BLOCK_ALLOWED_AIDS
    )
    _assert_no_text_fact_join(mutated, "rt", "counterpart dropped")
    _assert_aids_are_marked(mutated, "rt", A_SEGMENT_BLOCK_AIDS, "counterpart dropped")
    with pytest.raises(AssertionError):
        _assert_specimen_surface(mutated, "counterpart dropped")


def test_the_segment_aid_without_its_key_counterpart_turns_the_oracle_red() -> None:
    """D10.7's own M-case: keep the restored text aid, drop ``rt.river_segment_key``.

    The aid is only sanctioned because the key predicate that supersedes it sits
    in the same conjunction; without it the block reads segment identity from
    the text column and #1342's column drop would break it outright — while the
    aid set, the marker and the fact-join checks all stay green.
    """
    mutated = re.sub(
        r"\n      AND rt\.river_segment_key = \([^)]*\)\n",
        "\n",
        _SWITCHED_SPECIMEN,
        flags=re.DOTALL,
    )

    assert "rt.river_segment_key" not in mutated
    assert "AND rt.river_segment_id = %s" in mutated
    assert_text_fact_columns(
        mutated, "rt", A_SEGMENT_BLOCK_AIDS, "segment counterpart dropped", allowed=A_SEGMENT_BLOCK_ALLOWED_AIDS
    )
    _assert_aids_are_marked(mutated, "rt", A_SEGMENT_BLOCK_AIDS, "segment counterpart dropped")
    with pytest.raises(AssertionError):
        _assert_specimen_surface(mutated, "segment counterpart dropped")


def test_an_aid_separated_from_its_counterpart_turns_the_oracle_red() -> None:
    """Both columns still present, but no longer the same conjunction."""
    mutated = _SWITCHED_SPECIMEN.replace(
        "AND rt.variable = 'q_down'\n",
        "AND rt.variable = 'q_down'\n      AND rt.valid_time >= %s\n",
    )

    with pytest.raises(AssertionError):
        _assert_specimen_surface(mutated, "separated aid")


def test_a_marker_detached_from_its_aid_turns_the_oracle_red() -> None:
    """F6: #1342 deletes BY LINE, so a marker that floats free is not a marker.

    The mutation keeps all three markers and all three aids — it only hoists the
    markers to the top of the block, which is exactly the shape the pre-#1442
    "somewhere above" rule accepted. The count is kept whole on purpose: one
    marker fewer than sanctioned aids would redden the arithmetic check instead
    of the adjacency one this test is about.
    """
    without_markers = _SWITCHED_SPECIMEN.replace(f"      {PUSHDOWN_AID_MARKER}\n", "")
    hoisted = f"    {PUSHDOWN_AID_MARKER}\n" * len(A_SEGMENT_BLOCK_AIDS)
    mutated = f"{hoisted}{without_markers}"

    assert mutated.count(PUSHDOWN_AID_MARKER) == len(A_SEGMENT_BLOCK_AIDS) == 3
    assert_text_fact_columns(
        mutated, "rt", A_SEGMENT_BLOCK_AIDS, "detached marker", allowed=A_SEGMENT_BLOCK_ALLOWED_AIDS
    )
    with pytest.raises(AssertionError):
        _assert_aids_are_marked(mutated, "rt", A_SEGMENT_BLOCK_AIDS, "detached marker")


def test_a_marker_on_the_aids_own_line_is_accepted() -> None:
    """Adjacency is "same line or the line above", not "the line above" only."""
    mutated = _SWITCHED_SPECIMEN.replace(
        f"      {PUSHDOWN_AID_MARKER}\n      AND rt.variable = 'q_down'",
        f"      AND rt.variable = 'q_down' {PUSHDOWN_AID_MARKER}",
    )

    _assert_aids_are_marked(mutated, "rt", A_SEGMENT_BLOCK_AIDS, "inline marker")


def test_a_dropped_pushdown_aid_turns_the_oracle_red() -> None:
    """The other direction: losing an aid silently reinstates the chunk collapse.

    Both aids the E4(ii) receipts measured are covered: the network one (000047's
    second segmentby column) and the segment one (its third — the whole-network
    decompression D10.7 reverses, 18549ms against 1085ms). Both are
    compressed-leg pruning; neither is an uncompressed-leg index prefix.
    """
    for dropped in ("AND rt.river_network_version_id = %s\n", "AND rt.river_segment_id = %s\n"):
        mutated = _SWITCHED_SPECIMEN.replace(dropped, "")
        assert mutated != _SWITCHED_SPECIMEN, dropped
        with pytest.raises(AssertionError):
            _assert_specimen_surface(mutated, f"dropped aid {dropped.strip()}")


def test_a_bare_column_regression_turns_the_oracle_red() -> None:
    """The fragment face: no alias to qualify, so the positional check must bite."""
    for regressed in (
        "run_id = ANY(%s)",
        "WHERE run_id = %s",
        "WHERE run_id IN (%s, %s)",
        "count(DISTINCT river_segment_id) AS segment_count",  # not a predicate...
    ):
        if "DISTINCT" in regressed:
            # ...so it is the call-site assertion, not the generic one, that
            # catches an aggregate regression. Pinned here so the division of
            # labour is explicit rather than an accident.
            _assert_no_text_identity_predicate(regressed, "aggregate")
            continue
        with pytest.raises(AssertionError):
            _assert_no_text_identity_predicate(regressed, "bare column")


def test_the_bare_column_check_does_not_fire_on_a_key_resolution_subquery() -> None:
    """Non-vacuity for the fragment face: the switched form must stay green."""
    _assert_no_text_identity_predicate(
        "run_key IN (SELECT run_key FROM hydro.hydro_run WHERE run_id = ANY(%s))",
        "switched fragment",
        resolves_keys_inline=True,
    )


def test_the_register_covers_every_file_this_change_switched() -> None:
    """A file that leaves the register silently loses its oracle.

    Listed as paths rather than as a docstring so a rename is a failure here
    rather than a quiet gap. The display four are deliberately absent — they are
    #1341's register, watched by tests/test_river_ts_read_path_surrogate_keys.py.
    """
    registered = tuple(tuple(path.split("/")) for path in REGISTERED_SOURCES)
    for parts in registered:
        assert REPO_ROOT.joinpath(*parts).is_file(), "/".join(parts)

    unregistered = (
        ("services", "tiles", "mvt.py"),
        ("apps", "api", "routes", "hydro_display.py"),
        ("packages", "common", "display_coverage.py"),
        ("services", "production_closure", "scale_validation.py"),
    )
    for parts in unregistered:
        assert parts not in registered, "/".join(parts)


def test_every_registered_file_declares_its_river_timeseries_statement_count() -> None:
    """A new statement in a registered file must force a register update (D10.6).

    Without this, the register is only as good as its own list of statements: a
    tenth ``forecast_store`` method or a third ``node27_autopipeline`` call site
    that reads the fact table by text identity is invisible to every shape
    assertion above, because no assertion ever looks at it. The census makes the
    ADDITION itself red, so the author has to come here, register the statement
    and give it a shape pin.
    """
    assert set(RIVER_TABLE_CENSUS) == set(REGISTERED_SOURCES), "census and register must list the same files"
    for path, expected in RIVER_TABLE_CENSUS.items():
        source = REPO_ROOT.joinpath(*path.split("/")).read_text(encoding="utf-8")
        assert _river_table_mentions(source) == expected, (
            f"{path}: {_river_table_mentions(source)} mentions of {RIVER_TABLE}, census says {expected}. "
            "Register the new statement (and pin its shape) before updating this number."
        )


def test_the_census_counts_statements_and_ignores_prose() -> None:
    """The census's own contract, on a synthetic module.

    Two halves, because either failure makes it useless: a new statement must
    raise the count (or the guard never bites), and a docstring / comment that
    names the table must not (or the count is bumped reflexively).
    """
    prose_only = (
        '"""Module docstring naming hydro.river_timeseries."""\n'
        "# comment naming hydro.river_timeseries\n"
        "def f():\n"
        '    """Reads hydro.river_timeseries."""\n'
        "    return 1\n"
    )
    assert _river_table_mentions(prose_only) == 0

    with_statement = prose_only + 'SQL = "SELECT 1 FROM hydro.river_timeseries WHERE run_id = %s"\n'
    assert _river_table_mentions(with_statement) == 1

    # Two statements inside ONE constant are two, not one: a call site that
    # grows a second statement in the same f-string is exactly the addition the
    # per-file count exists to catch.
    both = with_statement + (
        'MORE = """\nDELETE FROM hydro.river_timeseries;\nSELECT 1 FROM hydro.river_timeseries;\n"""\n'
    )
    assert _river_table_mentions(both) == 3


def test_the_sanctioned_ceiling_is_the_shared_one_not_a_private_copy() -> None:
    """Every per-group ceiling in this file is the shared vocabulary, plus one adjudicated column.

    The A segment blocks are the single exception (design D10.7) and it is
    stated here as an exact difference, mirroring how #1341 pins its
    lateral-probe extension: the local ceiling may add ``river_segment_id`` and
    nothing else, and every other group stays at the shared set.
    """
    for group in (A_FALLBACK_AIDS, PUBLISHER_AIDS, COPYBACK_AIDS, NO_AIDS):
        assert group <= set(SANCTIONED_TEXT_PUSHDOWN_COLUMNS)
    assert A_FALLBACK_AIDS == set(SANCTIONED_TEXT_PUSHDOWN_COLUMNS)
    assert set(SANCTIONED_TEXT_PUSHDOWN_COLUMNS) < set(A_SEGMENT_BLOCK_ALLOWED_AIDS)
    assert set(A_SEGMENT_BLOCK_ALLOWED_AIDS) - set(SANCTIONED_TEXT_PUSHDOWN_COLUMNS) == {"river_segment_id"}
    assert A_SEGMENT_BLOCK_AIDS <= set(A_SEGMENT_BLOCK_ALLOWED_AIDS)
    # The widening is local: the shared constant #1341's display oracles consume
    # still classifies river_segment_id as a forbidden text fact column, and
    # basin_version_id is forbidden on EVERY surface, this register included.
    assert {"basin_version_id", "river_segment_id"} <= set(FORBIDDEN_TEXT_FACT_COLUMNS)
    assert "river_segment_id" not in set(SANCTIONED_TEXT_PUSHDOWN_COLUMNS)
    assert forecast_store is not None  # import is load-bearing for the capture harness
