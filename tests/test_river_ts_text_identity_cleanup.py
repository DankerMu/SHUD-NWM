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
D, autopipeline (0)          none — neither per-tick criterion reads the
                             fact table at all (#1789, #1779)
E, seed/smoke/helpers        none
F, parser replace chain (3)  none
===========================  ==========================================

Scope is exactly this issue's register. The display four (``mvt.py``,
``hydro_display.py``, ``display_coverage.py``, ``production_closure``) are
watched by ``tests/test_river_ts_read_path_surrogate_keys.py`` and are
deliberately NOT re-asserted here, so #1341's grouped-comment style cannot be
false-flagged by a second, differently-worded oracle. ``db/migrations/**`` reads
the text columns by definition and is out of register too (so did
``scripts/node27_river_identity_backfill.py``, deleted by #1342's contract,
task 6.3).

Two invariants are checked on every registered surface beyond "which text
columns are referenced" (design D10.5/D10.6, added after cross-review):

* **adjacency** (RETIRED by #1342's contract, task 6.3) — a sanctioned aid had
  to be AND-ed in the same conjunction as the key/enum predicate that superseded
  it, with its marker comment on its own line immediately above. Both were what
  made the aid deletable. There are no aids left, so what this oracle now checks
  is that the count is ZERO, per registered file.
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
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from packages.common import forecast_store
from packages.common.forecast_store import (
    PsycopgForecastStore,
    _ResolvedRuns,
    _run_identity_filter,
    _ScenarioFilter,
)
from services.tile_publisher import forcing_copyback_backfill as backfill_module
from services.tile_publisher import publisher as publisher_module
from tests.river_ts_template_registry import (
    AID_COMMENT_PHRASE,
    NON_TEMPLATE_MENTIONS,
    REGISTRY,
    aid_comment_lines,
    assert_marker_census,
)
from tests.test_sql_shape_helpers import (
    FORBIDDEN_TEXT_FACT_COLUMNS,
    SANCTIONED_TEXT_PUSHDOWN_COLUMNS,
    TEXT_IDENTITY_COLUMNS,
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
    # #1980: the shared renderer holds the two table-name constants and MUST hold
    # no statement. Registering it is what makes a statement sneaking into the
    # helper red — the census below pins its mention count at exactly the two
    # constants, and the selector wiring meta-test then forces a diff to it to
    # run this oracle.
    "packages/common/river_ts_render.py",
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
# * forecast_store.py 3 = two raw routed sources + the index-metadata literal.
# * publisher.py 2 = routed fact source + the PublishError table name.
# * forcing_copyback_backfill.py 1 = routed fact source below EXISTS.
# * node27_autopipeline.py 0 = neither per-tick criterion reads the fact table
#   any more. #1789 deleted the ingest criterion's join (the parse timestamp it
#   derived now lives on hydro_run.parsed_at) and #1779 deleted the publish
#   criterion's EXISTS probe for the same reason — hydro_run.parsed_at already
#   records that a parse finished, and run_key is not a segmentby column, so the
#   probe cost a Seq Scan of every compressed chunk per tick. Any return to a
#   non-zero count means a fact-table read came back to the tick.
# * summarize_qhh_smoke_results.py 2 / reset_qhh_smoke_db.py 2 = canonical
#   and legacy branches; PREFIX matching includes the _legacy table name.
#   Reset passes each table NAME to its ``_delete`` helper.
# * seed_demo.py 5 = the seed INSERT + two verification counts + the two
#   human-readable count LABELS.
# * parser.py 4 = probe, window, DELETE, INSERT.
# * integration_helpers.py 9 = narrow INSERT, cleanup DELETE/window,
#   two mixed-store INSERT/source pairs + two decoy UPDATEs.
#   The copy/decoy SQL has its scoped key/enum owner below.
RIVER_TABLE_CENSUS: dict[str, int] = {
    "packages/common/forecast_store.py": 3,
    "services/tile_publisher/publisher.py": 2,
    "services/tile_publisher/forcing_copyback_backfill.py": 1,
    "scripts/node27_autopipeline.py": 0,
    # Both were 2 until #1342's contract (task 6.3): each had a twin statement
    # against `hydro.river_timeseries_legacy`, and `_river_table_mentions`
    # matches a PREFIX of the name, so the retired table counted here too.
    "scripts/summarize_qhh_smoke_results.py": 1,
    "scripts/reset_qhh_smoke_db.py": 1,
    "db/seeds/seed_demo.py": 5,
    "workers/output_parser/parser.py": 4,
    "tests/integration_helpers.py": 9,
    # river_ts_render.py 1 = RIVER_TABLE. It was two until #1342's contract
    # (task 6.3) deleted `RIVER_TABLE_LEGACY`; `_river_table_mentions` matches a
    # PREFIX of the name (the `_legacy` literal opened with the canonical one),
    # not a whole identifier. Any second mention means a statement (or a second
    # name) arrived in the shared helper — including one spelled in a refusal
    # MESSAGE, which is why that module's messages describe the fact table
    # rather than naming it.
    "packages/common/river_ts_render.py": 1,
}

# Per-file transitional-aid census (#1980 task 1.1, zeroed by #1342's contract,
# task 6.3). The four production files this oracle owns (fixture decision 7);
# mvt, hydro_display and display_coverage are censused by
# tests/test_river_ts_read_path_surrogate_keys.py, and the two registers stay
# mutually exclusive so exactly one test reddens per file.
#
# Every number is 0, and 0 is asserted rather than skipped: this is the tripwire
# against a text pushdown aid coming back to a table that has no text column to
# predicate on, where the failure mode is not a slow query but
# `column river_segment_id does not exist`.
MARKER_AID_CENSUS: dict[str, int] = {
    "packages/common/forecast_store.py": 0,
    "services/tile_publisher/publisher.py": 0,
    "services/tile_publisher/forcing_copyback_backfill.py": 0,
    "scripts/node27_autopipeline.py": 0,
    "scripts/summarize_qhh_smoke_results.py": 0,
    "scripts/reset_qhh_smoke_db.py": 0,
    "db/seeds/seed_demo.py": 0,
    "workers/output_parser/parser.py": 0,
    "tests/integration_helpers.py": 0,
    "packages/common/river_ts_render.py": 0,
}

_IDENTITY = {
    "basin_version_id": "basin_v1",
    "segment_id": "seg_001",
    "river_network_version_id": "rivnet_v1",
}
_T0 = datetime(2026, 5, 7, tzinfo=UTC)
_T1 = datetime(2026, 5, 14, tzinfo=UTC)
_NO_FILTER = _ScenarioFilter("", {})


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


#: A SCHEMA-QUALIFIED mention of the fact table, however it is quoted, spaced or
#: cased. The census's closure is over this class and not over one spelling:
#: ``str.count("hydro.river_timeseries")`` counted ``"hydro"."river_timeseries"``
#: and ``HYDRO . river_timeseries`` zero times, so a new read site could arrive
#: in a registered file without moving the census and therefore without ever
#: being forced into the template register (#2018 round-2, lane-1 F1).
#:
#: Deliberately NOT the renderer's bare-name token: registered sources spell the
#: table unqualified on purpose — as a value passed to a delete helper, as a
#: column list, in a table name constant — and counting those would move 8 of the
#: 10 per-file numbers for no gain. The unqualified search_path spelling of a
#: READ is refused by ``render_river_ts_sql`` at render time (its counter counts
#: the bare token, the FROM/JOIN walk does not model it, and the disagreement
#: refuses); what this census cannot do is FORCE such a read into the register,
#: and that is the recorded limit of the closure.
_RIVER_TABLE_SPELLING = re.compile(r'(?:"hydro"|\bhydro)\s*\.\s*"?river_timeseries', re.IGNORECASE)


def _river_table_mentions(source: str) -> int:
    """Schema-qualified mentions of the fact table in non-docstring string constants.

    Parsed rather than grepped for the same reason ``_sql_constants`` is: raw
    source counts every ``#`` comment and docstring paragraph that names the
    table, which turns a prose edit into a census failure and teaches everyone
    to bump the number without reading it.

    Matched with :data:`_RIVER_TABLE_SPELLING` rather than counted as a
    substring, so the count is over the class of qualified spellings and not
    over one of them.
    """
    tree = ast.parse(source)
    docstrings = _docstring_constants(tree)
    return sum(
        len(_RIVER_TABLE_SPELLING.findall(node.value))
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


def _assert_no_aid_comment(sql: str, label: str) -> None:
    """No transitional-aid comment survives on this surface.

    #1342's contract (task 6.3) deleted every aid together with its marker line,
    so this is a tripwire rather than a layout rule: an aid comment coming back
    means a text predicate came back with it, on a table whose text columns no
    longer exist.
    """
    found = aid_comment_lines(sql)
    assert found == (), f"{label}: transitional-aid comment on lines {list(found)}"


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


def _assert_switched_surface(sql: str, alias: str, label: str) -> None:
    """The full per-surface contract, in one call.

    The expectation is EXACTLY the empty set on every surface since #1342's
    contract (task 6.3): the fact table is key/enum-only, so any text identity
    reference through ``alias`` is a regression rather than a sanctioned
    pushdown aid. The per-group ceilings and the aid-adjacency invariant went
    with the aids they governed.
    """
    assert_text_fact_columns(sql, alias, set(), label)
    _assert_no_text_fact_join(sql, alias, label)
    _assert_no_aid_comment(sql, label)


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


def _segment_block_executions() -> dict[str, tuple[str, Any]]:
    """Capture SQL and bindings from every real spanning execution owner."""
    store = _store()
    rendered: dict[str, tuple[str, Any]] = {}

    def capture(label: str, call: Any) -> None:
        cursor = _CaptureCursor()
        call(cursor)
        assert len(cursor.statements) == 1, label
        rendered[label] = cursor.executed[0]

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


def _segment_block_statements() -> dict[str, str]:
    return {label: sql for label, (sql, _params) in _segment_block_executions().items()}


#: The run set #2417's resolve-then-push call sites converge on, seeded here
#: because this harness calls the segment-block methods DIRECTLY and therefore
#: never sees what `forecast_series` resolves one layer up.
PUSHED_RUN_KEYS: tuple[int, ...] = (101, 202)
PUSHED_RUNS = _ResolvedRuns(PUSHED_RUN_KEYS)
#: Two scenarios on two DISTINCT cycles, so the `:718` owner's pushed window is
#: an envelope and not one scenario's cycle used as both bounds.
PUSHED_CYCLES: dict[str, datetime] = {"forecast_gfs_deterministic": _T0, "forecast_ifs_deterministic": _T1}
#: The bound-run variant supplies its identity through the filter, not a key set:
#: its push is the self-contained in-SQL scalar sub-select.
PUSHED_IDENTITY_FILTER = _run_identity_filter(run_id="qhh_gfs_2026050700", model_id="basins_qhh_shud")
#: A REAL scenario filter on the latest-forecast shape, so task 2.7's "no half of
#: the scenario filter leaked into a branch" assertion is not vacuously true of a
#: rendering that had no scenario filter to leak in the first place.
PUSHED_SCENARIO_FILTER = forecast_store._scenario_filter(["GFS"])


def _pushed_segment_block_executions() -> dict[str, tuple[str, Any]]:
    """The three pushed variants of the forecast segment-block owners (#2417).

    Kept OUT of ``_segment_block_executions``: that register is the eight
    execution owners of the shared segment source, and its default rendering is
    the un-pushed baseline. These are the same owners driven the way
    ``forecast_series`` drives them in production, which is the only rendering
    against which the per-branch pushdown assertions are not vacuous.
    """
    store = _store()
    rendered: dict[str, tuple[str, Any]] = {}

    def capture(label: str, call: Any) -> None:
        cursor = _CaptureCursor()
        call(cursor)
        assert len(cursor.statements) == 1, label
        rendered[label] = cursor.executed[0]

    capture(
        "forecast_segment_rows_bound_run",
        lambda cursor: store._fetch_forecast_segment_rows(
            cursor,
            **_IDENTITY,
            issue_time=_T0,
            scenario_filter=_NO_FILTER,
            identity_filter=PUSHED_IDENTITY_FILTER,
        ),
    )
    capture(
        "forecast_segment_rows_resolved_runs",
        lambda cursor: store._fetch_forecast_segment_rows(
            cursor,
            **_IDENTITY,
            issue_time=_T0,
            scenario_filter=_NO_FILTER,
            identity_filter=_NO_FILTER,
            resolved_runs=PUSHED_RUNS,
        ),
    )
    capture(
        "forecast_segment_rows_selected_cycles_resolved_runs",
        lambda cursor: store._fetch_forecast_segment_rows(
            cursor,
            **_IDENTITY,
            issue_time=_T0,
            scenario_filter=PUSHED_SCENARIO_FILTER,
            identity_filter=_NO_FILTER,
            cycle_times_by_scenario=PUSHED_CYCLES,
            resolved_runs=PUSHED_RUNS,
        ),
    )
    return rendered


def _latest_product_fallback_execution() -> tuple[str, Any]:
    """The known-run heavy execution, distinct from its raw renderer input.

    The ``store`` parameter this helper carried went with #1342's contract
    (task 6.3) together with ``hydro.hydro_run.timeseries_store``: the header
    prefetch no longer projects a routing column, so there is nothing to vary.
    """
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
    heavy = [(sql, params) for sql, params in cursor.executed if "river_sample_rows AS" in sql]
    assert len(heavy) == 1
    return heavy[0]


def _latest_product_fallback_statement() -> str:
    return _latest_product_fallback_execution()[0]


def test_forecast_store_segment_blocks_carry_only_their_sanctioned_aids() -> None:
    for label, sql in _segment_block_statements().items():
        _assert_switched_surface(sql, "rt", f"forecast_store {label}")


def test_forecast_store_pushed_segment_blocks_carry_only_their_sanctioned_aids() -> None:
    """The pushed variants add exactly one sanctioned aid column: ``run_id``.

    Same ceiling as the un-pushed owners, so a pushdown that reached for a second
    text column — or dropped the marker off the one it does use — is red here.
    """
    pushed = _pushed_segment_block_executions()
    assert len(pushed) == 3
    for label, (sql, _params) in pushed.items():
        _assert_switched_surface(sql, "rt", f"forecast_store pushed {label}")


#: Per-owner: what the UNION branches of a pushed rendering must contain, and what
#: they must never contain. Spelled as explicit substring lists so no case can
#: pass by asserting nothing (#2417 task 3.3).
_PUSHED_BRANCH_REQUIRED: dict[str, tuple[str, ...]] = {
    "forecast_segment_rows_bound_run": (
        # The text twin this conjunct used to carry was the transitional
        # compressed-chunk pushdown aid; #1342's contract (task 6.3) deleted it
        # with the column it pushed into.
        "AND rt.run_key = (SELECT run_key FROM hydro.hydro_run WHERE run_id = %(run_id)s)",
    ),
    "forecast_segment_rows_resolved_runs": ("AND rt.run_key = ANY(%(pushdown_run_keys)s)",),
    "forecast_segment_rows_selected_cycles_resolved_runs": (
        "AND rt.run_key = ANY(%(pushdown_run_keys)s)",
        "AND rt.valid_time >= %(pushdown_window_start)s",
        "AND rt.valid_time <= %(pushdown_window_end)s",
    ),
}

#: The reads that deliberately span runs, and the run-identity text none of their
#: branches may acquire. `latest_analysis_issue_time`/`analysis_segment_rows`
#: splice across every analysis run; the two run-type owners report over every run
#: of a run type. Converging either to a run set would delete rows.
#: ``rt.run_key =`` rather than ``run_key =``: the branch carries the routing
#: join ``h.run_key = rt.run_key``, which is the key-only join this whole epic
#: exists to have. What must not appear is a CONSTRAINT on the fact side.
_SPANNING_BRANCH_FORBIDDEN: dict[str, tuple[str, ...]] = {
    "latest_analysis_issue_time": ("rt.run_key =", "rt.run_id"),
    "analysis_segment_rows": ("rt.run_key =", "rt.run_id"),
    "latest_run_type_valid_time": ("rt.run_key =", "rt.run_id", "cycle_time", "scenario_id"),
    "run_type_segment_rows": ("rt.run_key =", "rt.run_id", "cycle_time", "scenario_id"),
}

_PROJECTION = "SELECT rt.run_key, rt.river_network_version_key, rt.valid_time, rt.value, rt.unit_e"


def _fact_branch(sql: str) -> str:
    """The fact-table branch text of one composed segment statement.

    It was ``_union_branches`` and returned a (legacy, narrow) pair until #1342's
    contract (task 6.3) retired the routed store: the composed source is now a
    single leg, so there is exactly one branch and no ``UNION ALL`` to split on.
    The ``UNION ALL``-free shape is asserted rather than assumed — a second leg
    returning would make every per-branch pin below read only the first one.
    """
    start = sql.index(_PROJECTION)
    end = sql.index(") rt", start)
    branch = sql[start:end]
    assert "UNION ALL" not in branch, "the segment source is a single narrow leg"
    return branch


def test_forecast_store_pushed_branches_converge_run_identity_inside_the_union() -> None:
    """The positive half of the over-pushdown regression.

    Without it the negative half below passes for a reason unrelated to this
    change — the un-pushed default rendering contains none of these predicates
    either, so "the spanning branch has no run_key" would be vacuously true.
    """
    for label, (sql, _params) in _pushed_segment_block_executions().items():
        branch = _fact_branch(sql)
        for predicate in _PUSHED_BRANCH_REQUIRED[label]:
            assert predicate in branch, (label, predicate)
        # The fact table has no text identity column left, so the branch keeps
        # the key-side push and carries no `rt.run_id` constraint at all.
        assert "rt.run_id" not in branch, label
        # Task 2.7: a filter fragment is pushed WHOLE or not at all. The scenario
        # filter is one conjunct whose interior is an OR, so half of it inside a
        # branch would be unsound; none of it is pushed — the resolved run set
        # already carries what it selected.
        assert "LOWER(h.source_id)" not in branch, label
        assert "scenario_tokens" not in branch, label
        if label == "forecast_segment_rows_selected_cycles_resolved_runs":
            # …and that owner really was rendered WITH a scenario filter, so the
            # two assertions above cannot pass for want of anything to leak.
            assert "LOWER(h.source_id)" in sql, label
            assert "scenario_tokens" in sql, label


def test_forecast_store_pushed_segment_blocks_still_execute_exactly_one_statement() -> None:
    """Task 3.6: the resolve lives in ``forecast_series``, not in these methods.

    A second statement inside a segment-block method does not fail cleanly — it
    makes every consumer of ``FORECAST_STORE_EXECUTIONS`` read the wrong SQL. The
    capture helper asserts the count per owner; this states it as a requirement
    over both registers at once.
    """
    store = _store()
    calls = (
        (
            "forecast_segment_rows_bound_run",
            lambda cursor: store._fetch_forecast_segment_rows(
                cursor,
                **_IDENTITY,
                issue_time=_T0,
                scenario_filter=_NO_FILTER,
                identity_filter=PUSHED_IDENTITY_FILTER,
            ),
        ),
        (
            "forecast_segment_rows_resolved_runs",
            lambda cursor: store._fetch_forecast_segment_rows(
                cursor,
                **_IDENTITY,
                issue_time=_T0,
                scenario_filter=_NO_FILTER,
                identity_filter=_NO_FILTER,
                resolved_runs=PUSHED_RUNS,
            ),
        ),
        (
            "forecast_segment_rows_selected_cycles_resolved_runs",
            lambda cursor: store._fetch_forecast_segment_rows(
                cursor,
                **_IDENTITY,
                issue_time=_T0,
                scenario_filter=PUSHED_SCENARIO_FILTER,
                identity_filter=_NO_FILTER,
                cycle_times_by_scenario=PUSHED_CYCLES,
                resolved_runs=PUSHED_RUNS,
            ),
        ),
    )
    for label, call in calls:
        cursor = _CaptureCursor()
        call(cursor)
        assert len(cursor.statements) == 1, label
        assert "FROM hydro.hydro_run h\n" not in cursor.statements[0], label
    # The eight un-pushed owners are asserted the same way by the shared capture
    # helper, which refuses any owner that issues more than one statement.
    assert len(_segment_block_executions()) == 8
    assert len(_pushed_segment_block_executions()) == 3


def test_forecast_store_deliberately_spanning_branches_push_nothing_that_pins_a_run() -> None:
    """A read that reports across runs must not converge to one — task 3.3.

    The run-type owners may bind their ``run_type`` set inside the branch; that is
    the constraint that holds for every run they report. Anything that names a
    single run, cycle or scenario would silently delete rows that used to come
    back, and no row-count or digest oracle in this suite could see it.
    """
    rendered = _segment_block_statements()
    for label, forbidden in _SPANNING_BRANCH_FORBIDDEN.items():
        branch = _fact_branch(rendered[label])
        for needle in forbidden:
            assert needle not in branch, (label, needle)
    for label in ("latest_run_type_valid_time", "run_type_segment_rows"):
        assert "AND LOWER(h.run_type::text) = ANY(%(run_types)s)" in _fact_branch(rendered[label]), label
    for label in ("latest_analysis_issue_time", "analysis_segment_rows"):
        assert "AND h.scenario_id = 'analysis_true_field'" in _fact_branch(rendered[label]), label


def test_forecast_store_segment_blocks_carry_no_text_pushdown_aid() -> None:
    """The inverse of the D10.7 presence pin this test used to be.

    Until #1342's contract (task 6.3) all eight blocks were REQUIRED to carry
    ``AND rt.river_segment_id = %(river_segment_id)s``: on the routed store the
    compressed leg lost segmentby pruning without it and decompressed the whole
    network (measured on node-27: 32660 batches / 18549ms, against 40 / 1085ms
    with the aid). The narrow table segments by ``river_segment_key`` instead, so
    that column is gone and the predicate would now raise ``column
    river_segment_id does not exist``. The requirement flipped sign; it did not
    disappear, which is why it is still asserted over all eight.
    """
    rendered = _segment_block_statements()
    assert len(rendered) == 8
    for label, sql in rendered.items():
        assert "rt.river_segment_id" not in sql, label
        assert "AND rt.river_segment_key = (" in sql, label


def _assert_segment_block_identity_predicates(sql: str, label: str) -> None:
    """The oracle design.md F4b names, factored so it can be proven to bite.

    Asserted on the two UNION BRANCH texts rather than on the composed statement,
    because sargability is a property of the BRANCH SCAN: the outer layer's
    ``JOIN core.river_network_version rnv ON rnv.river_network_version_key =
    rt.river_network_version_key`` is a different predicate on the same column
    and must not be able to answer for the branch's.

    #2451 C1 changed the spelling of two of these conjuncts, deliberately and
    visibly (``packages/common/forecast_store.py``): ``basin_version_key`` and
    ``river_network_version_key`` are compared with ``IS NOT NULL AND … IS NOT
    DISTINCT FROM`` so they cannot form an index condition on
    ``river_ts_run_discovery_key_idx``'s 2nd and 3rd columns; what changed is
    sargability, not enforcement.

    The ``IS NOT NULL`` half is part of the spelling, not decoration, and is
    pinned as a CONTIGUOUS PAIR with the conjunct it guards. ``IS NOT DISTINCT
    FROM`` on its own is NOT equivalent to ``=``: with NULL on both sides it is
    TRUE where ``=`` is UNKNOWN, so the row is RETURNED instead of excluded. The
    two columns are ``NOT NULL`` only on ``hydro.river_timeseries``
    (``db/migrations/000059_river_timeseries_narrow_expand.sql:14-15``); on the
    routing target #1342's contract retired they were NULLABLE, and ONE template
    rendered both. The second rendering is gone and the spelling deliberately
    stays: with the left side guarded non-NULL the pair filters exactly as ``=``
    does on every input, so the branch predicate does not depend on a nullability
    fact stated in another file, and reverting to ``=`` would re-enable the
    discovery index prefix this spelling exists to suppress. ``test_the_null_guard_is_what_makes_
    the_c1_spelling_filter_like_equality`` is the executable proof.

    Each of the two is pinned TWICE — the new spelling present AND the plain
    ``=`` spelling absent. Presence alone would pass a half-revert that adds
    ``rt.basin_version_key = (…)`` back BESIDE the ``IS NOT DISTINCT FROM`` one,
    which re-enables the discovery index's prefix and reinstates the defect while
    still "keeping the predicate". ``rt.river_segment_key`` keeps its ``=`` and is
    deliberately NOT swept into the absence check: it is the conjunct that must
    stay sargable, and it is pinned as an equality here.
    """
    for branch in (_fact_branch(sql),):
        where = (label, "narrow")
        assert "rt.basin_version_key IS NOT DISTINCT FROM (" in branch, where
        assert "rt.basin_version_key = (" not in branch, where
        # The leading keyword is deliberately NOT part of the pin: #1342's
        # contract (task 6.3) deleted the store predicate that used to open this
        # chain, so the guard is now the `WHERE` conjunct in some owners and an
        # `AND` conjunct in others. What must hold is the ADJACENCY of the guard
        # to the conjunct it guards, and that is what is spelled.
        assert (
            "rt.basin_version_key IS NOT NULL\n  AND rt.basin_version_key IS NOT DISTINCT FROM (\n"
        ) in branch, where
        assert "SELECT basin_version_key FROM core.basin_version" in branch, where
        assert "rt.river_segment_key = (" in branch, where
        # The segment resolution binds the network too: core.river_segment's
        # primary key is (river_segment_id, river_network_version_id), so a bare
        # segment lookup could return more than one row and raise at runtime.
        assert "SELECT river_segment_key FROM core.river_segment" in branch, where
        assert "AND river_network_version_id = %(river_network_version_id)s" in branch, where
        assert "rt.river_network_version_key IS NOT DISTINCT FROM (" in branch, where
        assert "rt.river_network_version_key = (" not in branch, where
        assert (
            "rt.river_network_version_key IS NOT NULL\n"
            "  AND rt.river_network_version_key IS NOT DISTINCT FROM (\n"
        ) in branch, where
        assert "rt.variable_e = 'q_down'::hydro.river_variable" in branch, where
        # The run is reached by key, never by its text.
        assert "JOIN hydro.hydro_run h ON h.run_key = rt.run_key" in branch, where


def test_forecast_store_segment_blocks_resolve_identity_through_the_authority_tables() -> None:
    """Non-vacuity: every block really predicates on the four keys and the enum."""
    rendered = _segment_block_statements()
    assert len(rendered) == 8
    for label, sql in rendered.items():
        _assert_segment_block_identity_predicates(sql, label)


def test_the_identity_predicate_pin_reddens_if_the_c1_spelling_is_reverted() -> None:
    """The pin above may not be satisfiable by loosening it (tasks.md §3.2).

    ``_assert_key_predicates_retained`` (``packages/common/river_ts_render.py``)
    compares a template against a rendering derived from that same template, so a
    symmetric rewrite of a conjunct leaves it silent — its silence is not evidence
    the spelling survived. This pin is the one that bites, and this case is the
    proof that it does: reverting exactly C1's substitution, on every one of the
    eight blocks, must make it RED. If this ever passes, the pin above has been
    weakened to something a plain ``=`` also satisfies.
    """
    rendered = _segment_block_statements()
    assert len(rendered) == 8
    for label, sql in rendered.items():
        reverted = sql.replace(" IS NOT DISTINCT FROM (", " = (")
        assert reverted != sql, label
        with pytest.raises(AssertionError):
            _assert_segment_block_identity_predicates(reverted, label)
        # And the same for a HALF revert, which keeps the C1 spelling and adds the
        # sargable one back beside it — the shape a presence-only pin would miss.
        half = sql.replace(
            "  AND rt.basin_version_key IS NOT DISTINCT FROM (\n",
            "  AND rt.basin_version_key = (\n      SELECT basin_version_key FROM core.basin_version\n"
            "      WHERE basin_version_id = %(basin_version_id)s\n  )\n"
            "  AND rt.basin_version_key IS NOT DISTINCT FROM (\n",
        )
        assert half != sql, label
        with pytest.raises(AssertionError):
            _assert_segment_block_identity_predicates(half, label)
        # And an UNGUARDED revert, which drops only the `IS NOT NULL` line. Every
        # other pin above still passes on it — the C1 spelling is present, the
        # plain `=` is absent, the authority sub-select is intact — so the
        # contiguous-pair pin is the only thing standing between this branch and
        # `IS NOT DISTINCT FROM`'s NULL-vs-NULL divergence from `=`.
        for column in ("basin_version_key", "river_network_version_key"):
            # The guard is removed WITH the connective that followed it, so the
            # chain stays well formed whether the guard opened it with `WHERE` or
            # continued it with `AND` — #1342's contract (task 6.3) deleted the
            # store predicate that used to be the chain's first conjunct.
            unguarded = re.sub(rf"rt\.{column} IS NOT NULL\n  AND ", "", sql)
            assert unguarded != sql, (label, column)
            with pytest.raises(AssertionError):
                _assert_segment_block_identity_predicates(unguarded, label)


#: One ``IS NOT DISTINCT FROM`` identity conjunct of ``_SEGMENT_ROWS_SOURCE_SQL``,
#: SLICED OUT of the shipped template instead of copied, so the executable
#: semantics case below cannot drift away from the SQL the product runs.
#:
#: Deliberately anchored on the COMPARISON and not on the guard: the guard's
#: presence is an assertion this test makes, so it may not also be a precondition
#: for the test running. Anchored on the guard, deleting the guard from
#: ``forecast_store.py`` would make the slice come up empty and the failure would
#: read "found 0 conjuncts" instead of naming the semantics that were lost.
_IDENTITY_COMPARISON_CONJUNCT = re.compile(
    r"  AND rt\.(?P<column>[a-z_]+) IS NOT DISTINCT FROM \(\n(?P<subselect>(?:      [^\n]*\n)+)  \)\n"
)

#: The authority resolution inside such a conjunct, parsed so the fixture tables
#: below are built from the template's own names rather than from a guess.
_AUTHORITY_SUBSELECT = re.compile(
    r"\s*SELECT (?P<key>\w+) FROM (?P<schema>\w+)\.(?P<table>\w+)\s+WHERE (?P<member>\w+) = %\((?P<param>\w+)\)s\s*"
)

#: The five NULL/non-NULL combinations the two sides of an identity comparison can
#: take, with what PostgreSQL's ``=`` does with each inside a ``WHERE`` conjunct.
#: ``authority_seeded`` False binds an id with no authority row, which is what
#: makes the scalar sub-select yield NULL.
_IDENTITY_NULL_CELLS: tuple[tuple[str, int | None, bool, int], ...] = (
    ("both_bound_match", 7, True, 1),
    ("both_bound_mismatch", 9, True, 0),
    ("fact_null_authority_bound", None, True, 0),
    ("fact_bound_authority_null", 7, False, 0),
    # The only cell where the guard changes the answer.
    ("both_null", None, False, 0),
)


def _identity_comparison_conjuncts() -> dict[str, tuple[str, bool, re.Match[str]]]:
    """``{column: (comparison_sql, guarded, authority_match)}`` off the shipped template.

    ``guarded`` is whether the line immediately ABOVE the comparison is that same
    column's ``IS NOT NULL``. Read here, asserted by the caller.

    The guard's own leading keyword is not part of the match: #1342's contract
    (task 6.3) deleted the store predicate that used to open this chain, so the
    first guard is now the ``WHERE`` conjunct and the second is still an ``AND``
    one. Adjacency is the property; which keyword introduces the chain is not.
    """
    template = forecast_store._SEGMENT_ROWS_SOURCE_SQL
    found: dict[str, tuple[str, bool, re.Match[str]]] = {}
    for match in _IDENTITY_COMPARISON_CONJUNCT.finditer(template):
        column = match.group("column")
        authority = _AUTHORITY_SUBSELECT.fullmatch(match.group("subselect"))
        assert authority is not None, match.group("subselect")
        guard = f"rt.{column} IS NOT NULL\n"
        found[column] = (match.group(0), template[: match.start()].endswith(guard), authority)
    return found


def _sqlite_row_count(connection: sqlite3.Connection, predicate: str, params: dict[str, Any]) -> int:
    """Rows a one-row fact table returns under ``predicate``, in SQLite's 3VL."""
    sql = re.sub(r"%\((\w+)\)s", r":\1", f"SELECT COUNT(*) FROM rt WHERE {predicate}")
    return int(connection.execute(sql, params).fetchone()[0])


def test_the_null_guard_is_what_makes_the_c1_spelling_filter_like_equality() -> None:
    """The guard is LOAD-BEARING, proven by executing the three spellings.

    Not a restatement of the text pin and not a model of SQL: the two guarded
    conjuncts are sliced out of ``_SEGMENT_ROWS_SOURCE_SQL`` and RUN, against a
    fact column declared NULLABLE exactly as
    ``hydro.river_timeseries_legacy``'s three key columns really are
    (``db/migrations/000050_river_identity_normalization.sql:216-222``; the
    narrow table's are ``NOT NULL``, and one template renders both branches).
    SQLite is the engine because it implements the same three-valued logic for
    ``=`` and for ``IS NOT DISTINCT FROM`` and needs no server — the oracle is
    the ``=`` column, which is the pre-#2451 spelling this change may not alter
    the meaning of.

    What it proves, per cell:

    * the shipped guarded spelling returns exactly what ``=`` returns, on all
      five NULL/non-NULL combinations of the two sides;
    * dropping the ``IS NOT NULL`` makes ``both_null`` return the row that ``=``
      excludes — a fail-open on identity verification, the class design.md F4
      forbids reopening.

    So if the guard is deleted from ``forecast_store.py``, this reddens on the
    per-column ``guarded`` assertion, AFTER the cells above have run and pinned
    what that deletion costs — the failure names the divergent cell rather than
    reporting an empty slice.
    """
    conjuncts = _identity_comparison_conjuncts()
    assert set(conjuncts) == {"basin_version_key", "river_network_version_key"}

    for column, (comparison_sql, guarded_in_template, authority) in conjuncts.items():
        schema, table = authority["schema"], authority["table"]
        key, member, param = authority["key"], authority["member"], authority["param"]
        # Built from the sliced comparison, INDEPENDENTLY of what the template
        # currently spells, so all three columns of the table below are measured
        # whether or not the guard is in the file right now.
        unguarded = comparison_sql.strip().removeprefix("AND ").strip()
        guarded = f"rt.{column} IS NOT NULL AND {unguarded}"
        equality = unguarded.replace("IS NOT DISTINCT FROM", "=", 1)
        assert equality != unguarded != guarded

        connection = sqlite3.connect(":memory:")
        try:
            connection.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
            connection.execute(f"CREATE TABLE {schema}.{table} ({member} TEXT NOT NULL, {key} INTEGER NOT NULL)")
            connection.execute(f"INSERT INTO {schema}.{table} VALUES ('seeded-id', 7)")
            # NULLABLE on purpose: this is the legacy table's real column shape.
            connection.execute(f"CREATE TABLE rt ({column} INTEGER)")
            divergent: list[str] = []
            for cell, fact_key, authority_seeded, expected in _IDENTITY_NULL_CELLS:
                connection.execute("DELETE FROM rt")
                connection.execute("INSERT INTO rt VALUES (?)", (fact_key,))
                params = {param: "seeded-id" if authority_seeded else "absent-id"}
                where = (column, cell)
                assert _sqlite_row_count(connection, equality, params) == expected, where
                assert _sqlite_row_count(connection, guarded, params) == expected, where
                # The unguarded spelling agrees everywhere EXCEPT both-NULL, where
                # it returns the row `=` excludes. That single divergence is the
                # whole reason the guard is in the template.
                fail_open = 1 if cell == "both_null" else expected
                assert _sqlite_row_count(connection, unguarded, params) == fail_open, where
                if fail_open != expected:
                    divergent.append(cell)
            assert divergent == ["both_null"], (column, divergent)
        finally:
            connection.close()

        assert guarded_in_template, (
            f"rt.{column}'s IS NOT DISTINCT FROM conjunct lost its `IS NOT NULL` guard; "
            f"without it the branch predicate diverges from `=` on {divergent} and returns "
            f"a row that identity verification excludes"
        )


def test_forecast_store_segment_blocks_bind_every_placeholder_they_grew() -> None:
    """Every named binding retains its identity, filter and window meaning."""
    store = _store()
    scenarios = forecast_store._scenario_filter(["GFS"])
    identity = forecast_store._run_identity_filter(run_id="run_a", model_id="model_a")
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
        assert "%s" not in statement, label
        assert set(re.findall(r"%\((\w+)\)s", statement)) == set(parameters), label
        assert parameters["basin_version_id"] == "basin_v1", label
        assert parameters["river_segment_id"] == "seg_001", label
        assert parameters["river_network_version_id"] == "rivnet_v1", label
        if "scenario_tokens" in parameters:
            assert parameters["scenario_tokens"] == ["gfs"]
            assert parameters["scenario_ids"] == ["forecast_gfs_deterministic", "gfs"]
        if "run_id" in parameters:
            assert parameters["run_id"] == "run_a"
            assert parameters["model_id"] == "model_a"
        if label == "forecast_segment_rows_selected_cycles":
            assert parameters["selected_scenario_0"] == "a"
            assert parameters["selected_cycle_0"] == _T0
            assert parameters["selected_scenario_1"] == "b"
            assert parameters["selected_cycle_1"] == _T1


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

    _assert_switched_surface(sql, "rt", "forecast_store latest-product fallback")
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

    # run_id / river_network_version_id used to carry a transitional text
    # conjunct INSIDE the guard, AND-ed with the key predicate that superseded
    # it. #1342's contract (task 6.3) deleted both the conjunct and the marker
    # above it, so the disjunct holds its key resolution alone; the `= )` tail is
    # that sub-select, which the stripper removed. The inner bracket is kept
    # verbatim rather than tidied — it is still the escape branch's own group,
    # and re-spelling production SQL for cosmetics is how a fold-away guard loses
    # a branch unnoticed.
    assert "AND (%(scan_run_id)s IS NULL OR ( rt.run_key = ))" in outer
    assert (
        "AND (%(scan_river_network_version_id)s IS NULL "
        "OR ( rt.river_network_version_key = ))"
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

    _assert_switched_surface(sql, "r", f"publisher (sqlite={is_sqlite})")
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

    The fragments are joined onto one line and stay text-free. The transitional
    aid comment that used to live in the raw fact source went with #1342's
    contract (task 6.3); its absence is asserted here too, because a fragment
    list is exactly where a reintroduced aid would hide from the renderer.
    """
    fragments = _sql_constants(
        module=("services", "tile_publisher", "publisher.py"),
        function="_discover_qdown_runs",
        needle="r.",
    )
    fact_fragments = [fragment for fragment in fragments if fragment.startswith("r.")]

    assert fact_fragments == ["r.variable_e = 'q_down'"]
    for fragment in fact_fragments:
        _assert_no_aid_comment(fragment, f"publisher fragment {fragment!r}")
        assert_text_fact_columns(fragment, "r", set(), f"publisher fragment {fragment!r}")


# ---------------------------------------------------------------------------
# C — services/tile_publisher/forcing_copyback_backfill.py
# ---------------------------------------------------------------------------


def test_copyback_discovery_probe_correlates_on_the_run_key() -> None:
    sql = backfill_module._DISCOVER_BACKFILL_RUNS_SQL

    _assert_switched_surface(sql, "rt", "copyback discovery")
    assert "WHERE rt.run_key = h.run_key" in sql
    # The enum predicate lives in the interpolated fact source, which is one
    # narrow leg since #1342's contract (task 6.3) — it used to be a per-store
    # render spliced into the same EXISTS.
    assert "WHERE rt.variable_e = 'q_down'" in sql
    assert "UNION ALL" not in sql
    assert "hydro.river_timeseries_legacy" not in sql


# ---------------------------------------------------------------------------
# D — scripts/node27_autopipeline.py (the two per-tick criteria, both now
#     fact-table-free: ingest by #1789, publish by #1779)
# ---------------------------------------------------------------------------


def _autopipeline_statements(function: str, *, needle: str = RIVER_TABLE, expected: int) -> tuple[str, ...]:
    """The ``needle``-bearing string constants of one autopipeline function.

    ``expected`` is a parameter and not a hard-coded ``1`` because #1789 drove
    one of the two call sites to ZERO such statements (``_already_ingested_runs``)
    and #1779 drove the other (``_publish_display_runs``) there too. With the
    count baked in, the helper itself raised before the negative pins below could
    run — a guard that fails for the very shape it is supposed to certify is not
    a guard. It stays a parameter, rather than being fixed at zero, because the
    same helper is used with ``needle="FROM hydro.hydro_run h"`` to reach the
    authority statement each criterion DOES carry.
    """
    statements = _sql_constants(
        module=("scripts", "node27_autopipeline.py"),
        function=function,
        needle=needle,
    )
    assert len(statements) == expected, (
        f"{function}: expected exactly {expected} statement(s) mentioning {needle!r}, "
        f"found {len(statements)}"
    )
    return statements


def _autopipeline_statement(function: str, *, needle: str = RIVER_TABLE) -> str:
    return _autopipeline_statements(function, needle=needle, expected=1)[0]


def test_autopipeline_ingest_criterion_touches_no_fact_table() -> None:
    """#1789: the completeness criterion must not reference the fact table at all.

    Was a key-only-join shape pin. The join existed to derive one per-run
    timestamp (``MAX(rt.created_at)``) out of a compressed hypertable whose
    ``run_key`` is neither a segmentby column nor indexed, so every tick
    decompressed every compressed chunk to compute it. ``hydro_run.parsed_at``
    now carries that timestamp at the point it is produced, and the join is
    gone. Zero occurrences, not "no forbidden aid": an aid-free join would still
    be the whole regression.
    """
    _autopipeline_statements("_already_ingested_runs", expected=0)


def test_autopipeline_ingest_criterion_is_authority_state_first() -> None:
    """#1674: 'published' is complete on its own; 'parsed' needs a parse timestamp.

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

    The positive pin is the #1789 half: the gate must stay
    ``h.status = 'published' OR h.parsed_at IS NOT NULL``. A bare
    ``h.parsed_at IS NOT NULL`` would judge the NULL-key legacy cohort — which
    the backfill leaves at NULL — incomplete and re-trigger exactly the
    per-cycle handoff #1674 removed.
    """
    sql = _autopipeline_statement("_already_ingested_runs", needle="FROM hydro.hydro_run h")

    assert "h.parsed_at" in sql
    assert "(h.status = 'published' OR h.parsed_at IS NOT NULL)" in sql
    assert "GROUP BY" not in sql
    assert "HAVING" not in sql
    assert "COALESCE" not in sql
    assert "updated_at" not in sql


def test_autopipeline_publish_criterion_touches_no_fact_table() -> None:
    """#1779: publish keys on authority state, so it must not name the fact table.

    Was a key-only-join shape pin with ``NO_AIDS``. The ``EXISTS (SELECT 1 FROM
    <fact table> rt WHERE rt.run_key = h.run_key)`` probe existed to learn that a
    parse had produced rows — something ``hydro_run.parsed_at`` already records,
    stamped by the parser in the transaction that sets ``status = 'parsed'``. And
    ``run_key`` is not a compression segmentby column, so on the compressed side
    the planner had no access path: every tick sequentially scanned every
    compressed chunk to answer a question the authority table had already
    answered.

    Zero occurrences, not "no forbidden aid": an aid-free probe would still be
    the whole regression. The statement's own SET/WHERE shape is pinned by
    ``tests/test_display_publish_status_only.py`` (the ``updated_at`` and
    ``parsed_at`` write negatives live there with their sibling assertions).
    """
    _autopipeline_statements("_publish_display_runs", expected=0)


def test_autopipeline_publish_criterion_reads_only_the_authority_table() -> None:
    """The positive half: the one statement publish keeps is an authority UPDATE.

    Without this, the zero-count pin above goes green on a publish step that
    stopped publishing — or that grew a second authority statement — because a
    count of zero fact-table mentions says nothing about what IS there.
    """
    sql = _autopipeline_statement("_publish_display_runs", needle="UPDATE hydro.hydro_run h")

    assert "SET status = 'published'" in sql
    assert "WHERE h.status = 'parsed'" in sql
    assert "AND h.parsed_at IS NOT NULL" in sql
    assert RIVER_TABLE not in sql
    _assert_no_aid_comment(sql, "autopipeline publish")


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


def test_parser_probe_and_window_locate_rows_by_key_without_text_aids() -> None:
    probe, window, _delete, _insert = _parser_river_statements()
    for label, sql in (("probe", probe), ("window", window)):
        assert "WHERE run_key = %s" in sql, label
        assert "AND river_network_version_key = %s" in sql, label
        assert "AND variable_e = %s" in sql, label
        _assert_no_aid_comment(sql, f"parser {label}")
        _assert_no_text_identity_predicate(sql, label)
    assert "WITH existing AS MATERIALIZED" in window


def test_parser_delete_locates_rows_by_key_with_no_aid() -> None:
    """The DELETE stays zero-text: the guard really does close it (#1442 D1, F).

    Unlike the two read statements it is valid_time bounded and runs only after
    ``check_batch_targets_uncompressed`` passed, so every chunk it can touch is
    uncompressed and 000051's key index is a complete access path. No aid is
    sanctioned here, and the aid comment's absence is asserted so #1681's aid
    cannot be copied into this statement without a decision.
    """
    _probe, _window, delete, _insert = _parser_river_statements()

    assert "WHERE run_key = %s" in delete
    assert "AND river_network_version_key = %s" in delete
    assert "AND variable_e = %s" in delete
    _assert_no_text_identity_predicate(delete, "parser delete")
    _assert_no_aid_comment(delete, "parser delete")
    # The window predicate is untouched: replace semantics depend on it, and the
    # guard's compressed-chunk verdict is computed from what this returns.
    assert "AND valid_time >= %s" in delete
    assert "AND valid_time <= %s" in delete


def test_parser_insert_writes_only_narrow_columns() -> None:
    """Canonical storage has no text identity columns after expand."""
    insert = _parser_river_statements()[3]
    match = re.search(r"INSERT INTO hydro\.river_timeseries\s*\((?P<columns>[^)]*)\)\s*VALUES", insert, re.S)
    assert match is not None, insert
    columns = [column.strip() for column in match.group("columns").split(",") if column.strip()]

    assert len(columns) == 10, columns
    assert not set(columns) & set(TEXT_IDENTITY_COLUMNS)
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
    # ONE statement: the second, against `hydro.river_timeseries_legacy`, went
    # with the table in #1342's contract (task 6.3).
    assert [sql for sql in statements if "hydro.river_timeseries_legacy" in sql] == []
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
    # ONE fragment: the twin that deleted from the retired physical table went
    # with it in #1342's contract (task 6.3).
    river_fragments = [fragment for fragment in fragments if fragment.startswith("run_key")]
    assert len(river_fragments) == 1
    for fragment in river_fragments:
        assert fragment == "run_key IN (SELECT run_key FROM hydro.hydro_run WHERE run_id = ANY(%s))"
        _assert_no_text_identity_predicate(fragment, "reset fragment", resolves_keys_inline=True)


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


def test_integration_helpers_post_expand_copy_and_decoys_use_keys_and_enums() -> None:
    statements = _sql_constants(
        module=("tests", "integration_helpers.py"),
        function="post_expand_forecast_database",
        needle="hydro.river_timeseries",
    )
    assert len(statements) == 3
    narrow_copy, copy, decoys = statements
    assert "FROM hydro.river_timeseries_legacy" in narrow_copy
    assert "INSERT INTO hydro.river_timeseries_legacy" in copy
    assert "FROM hydro.river_timeseries rt" in copy
    assert "RENAME" not in copy
    assert_text_fact_columns(copy, "rt", set(), "fixture narrow source")
    for store in ("legacy", "narrow"):
        assert f"h.timeseries_store = '{store}'" in decoys


# ---------------------------------------------------------------------------
# Counter-examples: the oracle's own contract (tasks 2.2)
#
# Every assertion helper above must be shown to bite. A cleanup oracle that
# cannot be made red is indistinguishable from no oracle at all, and round-1 of
# #1341 shipped exactly that.
# ---------------------------------------------------------------------------

#: The specimen is AID-FREE since #1342's contract (task 6.3). It used to carry
#: three marked text aids (`river_segment_id`, `river_network_version_id`,
#: `variable`) so the marker/adjacency counter-examples had something to mutate;
#: those columns no longer exist on the fact table, so a specimen that still
#: spelled them would be proving the oracle bites on a shape production can no
#: longer produce. What remains is a plain key/enum surface — which is what every
#: registered surface now looks like, so a counter-example built from it reddens
#: for the same reason a real regression would.
_SWITCHED_SPECIMEN = """
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
      AND rt.river_network_version_key = (
              SELECT river_network_version_key FROM core.river_network_version
              WHERE river_network_version_id = %s
          )
      AND rt.variable_e = 'q_down'::hydro.river_variable
"""


def _assert_specimen_surface(sql: str, label: str) -> None:
    """``_assert_switched_surface`` at the register's one remaining ceiling: empty."""
    _assert_switched_surface(sql, "rt", label)


def test_the_specimen_the_counter_examples_mutate_is_itself_green() -> None:
    """Without this, a counter-example could be red for the wrong reason."""
    _assert_specimen_surface(_SWITCHED_SPECIMEN, "specimen")


def test_a_forbidden_text_predicate_turns_the_oracle_red() -> None:
    """Every text identity column is forbidden on every surface in this register.

    ``river_segment_id`` was this group's third SANCTIONED aid under D10.7 and is
    now forbidden like the rest; both it and ``basin_version_id`` are asserted so
    the widened-ceiling era cannot come back by accident.
    """
    for mutation in (
        ("WHERE rt.basin_version_key = (", "WHERE rt.basin_version_id = %s AND rt.basin_version_key = ("),
        ("      AND rt.variable_e =", "      AND rt.river_segment_id = %s\n      AND rt.variable_e ="),
        ("      AND rt.variable_e =", "      AND rt.river_network_version_id = %s\n      AND rt.variable_e ="),
        ("      AND rt.variable_e =", "      AND rt.variable = 'q_down'\n      AND rt.variable_e ="),
    ):
        mutated = _SWITCHED_SPECIMEN.replace(*mutation)
        assert mutated != _SWITCHED_SPECIMEN, mutation
        with pytest.raises(AssertionError):
            _assert_specimen_surface(mutated, f"forbidden predicate {mutation[1]!r}")


def test_a_reintroduced_transitional_aid_comment_turns_the_oracle_red() -> None:
    """The zero-valued tripwire #1342's contract left behind (task 6.3).

    The marker/adjacency counter-examples this file used to carry proved that a
    MALFORMED aid was red. There is no well-formed aid any more, so the property
    that replaces them is simpler and stricter: the aid comment itself must not
    reappear on a registered surface. It is the cheap signal that someone is
    re-adding a text pushdown to a table whose text columns are gone, where the
    failure mode is ``column river_segment_id does not exist`` at runtime.

    Asserted on BOTH halves of the old marker comment — the descriptive phrase
    (which is what the tripwire keys on) and a hand-written aid that carries only
    the phrase — so the check cannot be satisfied by renaming the comment.
    """
    assert AID_COMMENT_PHRASE, "the tripwire needs a phrase to key on"
    mutated = _SWITCHED_SPECIMEN.replace(
        "      AND rt.variable_e =",
        f"      -- {AID_COMMENT_PHRASE}, remove later\n      AND rt.variable_e =",
    )
    assert mutated != _SWITCHED_SPECIMEN
    assert aid_comment_lines(mutated) != ()
    with pytest.raises(AssertionError):
        _assert_no_aid_comment(mutated, "reintroduced aid comment")
    with pytest.raises(AssertionError):
        _assert_specimen_surface(mutated, "reintroduced aid comment")


def test_a_text_fact_join_turns_the_oracle_red() -> None:
    """The forbidden shape that most looks like a legitimate pushdown aid."""
    mutated = _SWITCHED_SPECIMEN.replace(
        "JOIN hydro.hydro_run h ON h.run_key = rt.run_key",
        "JOIN hydro.hydro_run h ON h.run_key = rt.run_key AND rt.run_id = h.run_id",
    )

    # It is caught twice over: as an out-of-ceiling column, and as a text fact
    # join regardless of ceiling.
    with pytest.raises(AssertionError):
        _assert_specimen_surface(mutated, "text fact join")
    with pytest.raises(AssertionError):
        _assert_no_text_fact_join(mutated, "rt", "text fact join")


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


def test_every_registered_file_declares_its_transitional_aid_count() -> None:
    """Zero aids across the REGISTERED files, asserted rather than skipped.

    It was 34/34 (ten of them this register's) until #1342's contract (task 6.3)
    deleted every aid together with its marker line. The census stays, and stays
    per file, for one reason: an aid comment reappearing is the cheap signal that
    a text pushdown came back to a table whose text columns no longer exist,
    where the failure mode is ``column river_segment_id does not exist`` at
    runtime rather than a slow plan.

    The total is over ``REGISTERED_SOURCES`` plus the display oracle's
    ``DISPLAY_MARKER_AID_CENSUS``, not over the tree: nothing here sweeps the
    repository, so a NEW reader that is in neither register is invisible to this
    assertion. Finding one is the I11 discovery-set census's job (tasks 7.2a).

    Asserted through the shared counter in ``tests/river_ts_template_registry.py``
    so the display oracle's half uses the same definition of "an aid comment".
    """
    assert set(MARKER_AID_CENSUS) == set(REGISTERED_SOURCES), "census and register must list the same files"
    for path, expected in MARKER_AID_CENSUS.items():
        assert_marker_census(path, expected)


def test_this_registers_transitional_aid_total_is_zero() -> None:
    """Ten until #1342's contract (task 6.3); zero is the terminal state.

    Stated as its own assertion rather than folded into the per-file loop because
    the number is the thing task 6.3 delivered: the migration in task 6.2 can only
    DROP the text columns while nothing predicates on them.
    """
    assert sum(MARKER_AID_CENSUS.values()) == 0


def test_every_registered_read_template_is_registered_in_the_template_registry() -> None:
    """Registry closure for this oracle's files (#1980 orchestrator requirement).

    The statement census says how many times a file names the fact table; the
    template registry says which of those are READ TEMPLATES that render per
    store. Closure is the equality between them, with the non-template mentions
    enumerated: a new read site is then red twice over — once because the census
    moved, once because the sum no longer closes — and it cannot be silenced by
    bumping only the census.
    """
    for path in sorted(set(entry.path for entry in REGISTRY) & set(RIVER_TABLE_CENSUS)):
        registered = sum(entry.mentions for entry in REGISTRY if entry.path == path)
        assert registered + NON_TEMPLATE_MENTIONS[path] == RIVER_TABLE_CENSUS[path], (
            f"{path}: {registered} mentions in registered templates + "
            f"{NON_TEMPLATE_MENTIONS[path]} declared non-template mentions != census {RIVER_TABLE_CENSUS[path]}"
        )
    # The shared helper is registered with no template at all, which is the whole
    # point of registering it.
    assert RIVER_TABLE_CENSUS["packages/common/river_ts_render.py"] == (
        NON_TEMPLATE_MENTIONS["packages/common/river_ts_render.py"]
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


def test_the_census_counts_the_qualified_spelling_however_it_is_quoted_spaced_or_cased() -> None:
    """The census's closure, on the spellings a substring count is blind to.

    ``str.count("hydro.river_timeseries")`` reads exactly one of the many legal
    ways to name the table: a call site that wrote ``"hydro"."river_timeseries"``
    or ``HYDRO . river_timeseries`` was counted zero times, so a new read site
    could arrive in a registered file without moving the census and without ever
    being forced into the template register (#2018 round-2 lane-1 F1, sibling of
    the renderer's own counter). Prose stays excluded, because the reason the
    census is parsed rather than grepped has not changed.
    """
    source = (
        '"""Module docstring naming hydro.river_timeseries."""\n'
        "QUOTED = 'SELECT 1 FROM \"hydro\".\"river_timeseries\" WHERE run_key = %s'\n"
        "SPACED = 'SELECT 1 FROM HYDRO . river_timeseries WHERE run_key = %s'\n"
    )

    assert _river_table_mentions(source) == 2


def test_this_register_holds_no_private_text_ceiling_any_more() -> None:
    """Every surface in this register is at the empty ceiling (task 6.3).

    This file used to carry five per-group ceilings — ``A_SEGMENT_BLOCK_AIDS``
    and its D10.7-widened ``A_SEGMENT_BLOCK_ALLOWED_AIDS``, ``A_FALLBACK_AIDS``,
    ``PUBLISHER_AIDS``, ``COPYBACK_AIDS``, ``NO_AIDS`` — and a pin that the
    widening was exactly ``river_segment_id``. #1342's contract deleted the aids,
    so the ceilings went with them and ``_assert_switched_surface`` passes the
    empty set unconditionally.

    The SHARED vocabulary is a different thing and deliberately survives: it is
    what ``assert_text_fact_columns`` and ``_assert_no_text_identity_predicate``
    use to decide what counts as a text identity column at all. What is asserted
    here is that this register adds nothing to it.
    """
    assert set(TEXT_IDENTITY_COLUMNS) == (
        set(SANCTIONED_TEXT_PUSHDOWN_COLUMNS) | set(FORBIDDEN_TEXT_FACT_COLUMNS)
    )
    # Non-vacuity: the helper really is called with the empty set on a surface
    # that would otherwise be allowed to keep a sanctioned column.
    sanctioned = sorted(SANCTIONED_TEXT_PUSHDOWN_COLUMNS)
    assert sanctioned, "the shared vocabulary must still name sanctioned columns to make this bite"
    for column in sanctioned:
        widened = _SWITCHED_SPECIMEN.replace(
            "      AND rt.variable_e =", f"      AND rt.{column} = %s\n      AND rt.variable_e ="
        )
        with pytest.raises(AssertionError):
            _assert_switched_surface(widened, "rt", f"formerly sanctioned {column}")
    # basin_version_id was forbidden on EVERY surface before and still is.
    assert {"basin_version_id", "river_segment_id"} <= set(FORBIDDEN_TEXT_FACT_COLUMNS)
