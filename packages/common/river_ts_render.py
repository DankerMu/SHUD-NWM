"""Per-store rendering of the river fact-table read templates (#1980, epic #1979).

Issue #1342 replaces ``hydro.river_timeseries``'s text identity columns with
surrogate keys through an expand/contract pair: the expand migration renames the
text-shaped table to ``hydro.river_timeseries_legacy`` and creates a narrow,
key-only table under the canonical name. During the transition BOTH tables are
live, one run's rows in exactly one of them, and every read template therefore
has to be rendered twice:

``legacy``
    the template verbatim against ``hydro.river_timeseries_legacy``. Nothing
    else changes — in particular every transitional compressed-chunk pushdown
    aid stays, because the legacy table is still compressed with the text
    columns as its segmentby set and a pure-key predicate cannot be pushed into
    a compressed chunk there (000047).

``narrow``
    the template against the canonical name with every aid deleted: each
    :data:`PUSHDOWN_AID_MARKER` line and the single conjunct on the line
    immediately below it. The narrow
    table has no text identity column at all, so an aid left behind is not a
    slow query, it is ``column river_segment_id does not exist``.

Why a line-deletion renderer and not a SQL rewriter
---------------------------------------------------

The repo has no SQL parser and three placeholder dialects coexist in these
templates (``%s`` positional psycopg2, ``%(name)s`` named psycopg2, ``:name``
SQLAlchemy), so anything that had to understand the statements would have to
understand all three. It does not need to: #1980 first NORMALISES every template
so that an aid is exactly one conjunct on its own line with exactly one verbatim
marker line immediately above it, which turns "remove the aids" into a purely
textual, reviewable, line-addressed deletion.

That only works if the layout invariant is enforced rather than assumed, so this
module is fail-closed on it: if the line under a marker is not exactly one aid
conjunct — two conjuncts, a keyword line, the end of the template — it raises
:class:`RiverTemplateError` NAMING THE ENTRY instead of returning SQL. The
alternative (skip it, or delete it anyway) is a silently wrong statement shipped
to a production read path.

What is verified before any rendered SQL is returned
----------------------------------------------------

* **structural check** — the stand-in for "it still parses": balanced
  parentheses, no ``WHERE AND`` / ``FROM AND`` / ``ON AND``, no dangling ``AND``
  or ``OR (`` before a ``)`` or the end of the text, no marker left over.
* **table-scoped no-text-identity** (narrow only) — no text identity column of
  the FACT table survives. Table-scoped, not a grep: ``rt.run_key = (SELECT
  run_key FROM hydro.hydro_run WHERE run_id = %(scan_run_id)s)`` legitimately
  reads ``run_id`` on the AUTHORITY table (so a grep is wrong), and three
  registered statements give the fact table no alias at all (so an alias-only
  check is blind). Attribution therefore resolves the fact table's aliases from
  its own ``FROM`` / ``JOIN`` clauses and falls back to a bare-column
  comparison-position scan only where an unaliased reference exists.
* **key/enum predicate retention** (narrow only) — every conjunct of the legacy
  variant survives into the narrow variant, except the aid conjuncts themselves
  and the guards that textually CONTAIN one. Computed from the same chain
  normaliser the golden equivalence oracle uses, so "the deletion removed one
  line too many" is red here rather than in production.

Ownership note
--------------

The text-identity column vocabulary (:data:`SANCTIONED_TEXT_PUSHDOWN_COLUMNS`,
:data:`FORBIDDEN_TEXT_FACT_COLUMNS`, :data:`LATERAL_PROBE_TEXT_PUSHDOWN_COLUMNS`,
:data:`TEXT_IDENTITY_COLUMNS`, :data:`TEXT_AID_COUNTERPARTS`) and the SQL text
machinery that attributes a column reference to a table (the comment/sub-select
strippers and :func:`text_fact_columns`) live HERE, not in the shape-oracle test
module they came from: this module needs them and ``packages/`` cannot import
``tests/``. ``tests/test_sql_shape_helpers.py`` re-imports them, so there is one
definition of the boundary and one implementation of the attribution, consumed
by the renderer and by every oracle.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

RIVER_TABLE = "hydro.river_timeseries"
RIVER_TABLE_LEGACY = "hydro.river_timeseries_legacy"

#: The marker every retained transitional text predicate carries, verbatim.
#: #1341 introduced the wording, #1980 normalised it to one marker per aid on
#: the line immediately above it, and #1342 deletes both lines.
PUSHDOWN_AID_MARKER = "-- transitional compressed-chunk pushdown aid, remove with #1342"

#: The issue tag alone. Any comment carrying it that is not the verbatim marker
#: is a NON-normalised aid marker (mvt's pre-#1980 wording covered four
#: conjuncts under one comment), and the renderer must refuse it rather than
#: silently leave four text predicates in a narrow statement. Spelled as the tag
#: alone so this module contributes no non-verbatim marker line to the census.
_MARKER_TAG = "remove with #1342"

STORES: tuple[str, ...] = ("legacy", "narrow")

# ---------------------------------------------------------------------------
# Text identity vocabulary (moved here from tests/test_sql_shape_helpers.py by
# #1980, decision 2 of the issue fixture; the derivation moved with it).
# ---------------------------------------------------------------------------

# Sanctioned: kept as redundant transitional pushdown predicates because
# compression still segments/orders compressed chunks by them (000047:
# segmentby run_id, river_network_version_id, river_segment_id; orderby
# variable, valid_time) and TimescaleDB 2.10.2 cannot push an integer-key
# predicate through that. `river_segment_id` is a segmentby column too but is
# NOT sanctioned by this shared default: on the display surfaces #1341 owns it
# would be a text fact join, which the delta forbids outright.
SANCTIONED_TEXT_PUSHDOWN_COLUMNS: tuple[str, ...] = (
    "run_id",
    "river_network_version_id",
    "variable",
)
FORBIDDEN_TEXT_FACT_COLUMNS: tuple[str, ...] = (
    "basin_version_id",
    "river_segment_id",
    "unit",
    "quality_flag",
)
# Position-dependent extension (#1341 round-3 P1 remedy). The two national legs
# probe the fact table once per segment through a `CROSS JOIN LATERAL (... LIMIT
# 1)`; inside that probe the correlated `lr.` / `seg.` values are constants for
# the duration of one loop, so `river_segment_id` behaves like the other three
# aids rather than being an unpushable text fact join. Sanctioned in that
# position ONLY: it stays in FORBIDDEN_TEXT_FACT_COLUMNS so every constant-bound
# surface keeps rejecting it.
LATERAL_PROBE_TEXT_PUSHDOWN_COLUMNS: tuple[str, ...] = SANCTIONED_TEXT_PUSHDOWN_COLUMNS + ("river_segment_id",)
#: Every text identity column 000050 defines on the fact table. Derived, so a
#: column cannot be classified into neither group and quietly escape the census.
TEXT_IDENTITY_COLUMNS: tuple[str, ...] = SANCTIONED_TEXT_PUSHDOWN_COLUMNS + FORBIDDEN_TEXT_FACT_COLUMNS

# The key/enum column each text identity column is transitional FOR. A retained
# text aid is only ever allowed to narrow, which is exactly the statement "it is
# AND-ed with this column in the same conjunction".
TEXT_AID_COUNTERPARTS: dict[str, str] = {
    "run_id": "run_key",
    "river_network_version_id": "river_network_version_key",
    "river_segment_id": "river_segment_key",
    "basin_version_id": "basin_version_key",
    "variable": "variable_e",
    # Not identity predicates on any switched surface, but 000050 gives them a
    # twin too, and the map is asserted total against TEXT_IDENTITY_COLUMNS so a
    # column cannot be classified without naming what makes it redundant.
    "unit": "unit_e",
    "quality_flag": "quality_flag_e",
}


class RiverTemplateError(ValueError):
    """A template the renderer refuses, named by its registry entry.

    Never a bare ``AssertionError``: these checks run in production code paths
    (I2–I5 wire the readers to this module), where ``python -O`` would strip an
    assert and ship the very statement the check exists to stop.
    """


@dataclass(frozen=True)
class RenderedSql:
    """One rendered variant of a river read template.

    ``removed_placeholders`` lists the ZERO-BASED indices, in template order, of
    the positional ``%s`` placeholders that fell on a deleted aid line. Deleting
    an aid changes the caller's parameter tuple arity, and psycopg2 reports that
    only at execute time, so the arithmetic is returned rather than left to be
    rediscovered. Always empty for named-parameter templates and for ``legacy``.
    """

    sql: str
    removed_placeholders: tuple[int, ...] = ()


@dataclass(frozen=True)
class RenderedUnion:
    """A cross-store ``UNION ALL`` statement and the parameters it binds.

    ``params``
        the caller's own mapping, unchanged: every branch spells a given
        parameter with the SAME name, so one value binds all of them. Returned
        (as a copy) rather than left implicit so a caller can pass it straight
        to ``execute`` without having to know whether the combinator rewrote
        anything.
    ``branches``
        the store-binding form each branch got, in ``stores`` order — see
        :func:`store_binding_form`.
    """

    sql: str
    params: dict[str, object]
    branches: tuple[str, ...]


# ---------------------------------------------------------------------------
# SQL text traversal (moved here from tests/test_sql_shape_helpers.py with the
# constant group: the table-scoped attribution below is inert without it, and
# packages/ cannot import tests/).
# ---------------------------------------------------------------------------

_SUBQUERY_START = re.compile(r"\(\s*SELECT\b", re.IGNORECASE)

# The token immediately before a key-resolution sub-select is a comparison
# operator. The lookbehind keeps composite operators that merely END in one of
# these characters out: `->` / `->>` (json) and `=>` (named argument) are not
# comparisons, and treating them as such would widen the stripper again.
_COMPARISON_TAIL = re.compile(r"(?<![-=<>!])(<>|!=|<=|>=|=|<|>)\s*$")

_LINE_COMMENT = re.compile(r"--[^\n]*")

_WHITESPACE = re.compile(r"\s+")


def _scan_quoted(sql: str, start: int, quote: str) -> int:
    """Index just past the quoted run beginning at ``start`` (doubled quote escapes)."""
    index = start + 1
    length = len(sql)
    while index < length:
        if sql[index] == quote:
            if index + 1 < length and sql[index + 1] == quote:
                index += 2
                continue
            return index + 1
        index += 1
    return length


def _scan_block_comment(sql: str, start: int) -> int:
    end = sql.find("*/", start + 2)
    return len(sql) if end == -1 else end + 2


def _scan_line_comment(sql: str, start: int) -> int:
    end = sql.find("\n", start)
    return len(sql) if end == -1 else end


def _skip_balanced(sql: str, start: int) -> int:
    """Index just past the parenthesis group opening at ``start``.

    Parentheses inside strings, quoted identifiers and comments do not count, so
    ``(SELECT ... WHERE name = ')' ...)`` is consumed whole rather than cut at
    the literal.
    """
    depth = 0
    index = start
    length = len(sql)
    while index < length:
        character = sql[index]
        if character in "'\"":
            index = _scan_quoted(sql, index, character)
            continue
        if sql.startswith("--", index):
            index = _scan_line_comment(sql, index)
            continue
        if sql.startswith("/*", index):
            index = _scan_block_comment(sql, index)
            continue
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return length


def _in_comparison_value_position(kept: list[str]) -> bool:
    tail = _LINE_COMMENT.sub("", "".join(kept[-160:]))
    return _COMPARISON_TAIL.search(tail) is not None


def strip_scalar_subqueries(sql: str) -> str:
    """Return ``sql`` with every comparison-position sub-``SELECT`` removed.

    CTE openers (``x AS (SELECT ...)``), derived tables, ``EXISTS`` / ``IN``
    sub-selects and ordinary function calls are preserved on purpose: stripping
    a CTE opener deletes the very predicates the pins inspect, and the national
    identity probe reads the fact table INSIDE an ``EXISTS``. String literals,
    quoted identifiers and both comment styles are traversed rather than parsed.
    """
    kept: list[str] = []
    index = 0
    length = len(sql)
    while index < length:
        character = sql[index]
        if character in "'\"":
            end = _scan_quoted(sql, index, character)
            kept.append(sql[index:end])
            index = end
            continue
        if sql.startswith("--", index):
            end = _scan_line_comment(sql, index)
            kept.append(sql[index:end])
            index = end
            continue
        if sql.startswith("/*", index):
            end = _scan_block_comment(sql, index)
            kept.append(sql[index:end])
            index = end
            continue
        if character == "(" and _SUBQUERY_START.match(sql, index) and _in_comparison_value_position(kept):
            index = _skip_balanced(sql, index)
            continue
        kept.append(character)
        index += 1
    return "".join(kept)


def strip_all_subqueries(sql: str) -> str:
    """Return ``sql`` with EVERY parenthesized sub-``SELECT`` removed.

    The blunt companion to :func:`strip_scalar_subqueries`, for the bare-fragment
    surfaces that resolve identity with ``run_key IN (SELECT run_key FROM
    hydro.hydro_run WHERE run_id = %s)`` — not comparison position, so the
    precise stripper keeps it and the authority table's own ``WHERE run_id = %s``
    reads as a regression. Only safe where the fragment has no CTE and no derived
    table, because this deletes those too.
    """
    kept: list[str] = []
    index = 0
    length = len(sql)
    while index < length:
        character = sql[index]
        if character in "'\"":
            end = _scan_quoted(sql, index, character)
            kept.append(sql[index:end])
            index = end
            continue
        if sql.startswith("--", index):
            end = _scan_line_comment(sql, index)
            kept.append(sql[index:end])
            index = end
            continue
        if sql.startswith("/*", index):
            end = _scan_block_comment(sql, index)
            kept.append(sql[index:end])
            index = end
            continue
        if character == "(" and _SUBQUERY_START.match(sql, index):
            index = _skip_balanced(sql, index)
            continue
        kept.append(character)
        index += 1
    return "".join(kept)


def strip_comments(sql: str) -> str:
    """Return ``sql`` with both comment styles replaced by a single space.

    Uses the same traversal as :func:`strip_scalar_subqueries`, so a ``--``
    written inside a string literal is text, not a comment. Comments must go
    before any adjacency assertion: the production SQL puts the removal marker
    between a text predicate and its key counterpart, and a naive substring check
    would read the comment as separation.
    """
    kept: list[str] = []
    index = 0
    length = len(sql)
    while index < length:
        character = sql[index]
        if character in "'\"":
            end = _scan_quoted(sql, index, character)
            kept.append(sql[index:end])
            index = end
            continue
        if sql.startswith("--", index):
            index = _scan_line_comment(sql, index)
            kept.append(" ")
            continue
        if sql.startswith("/*", index):
            index = _scan_block_comment(sql, index)
            kept.append(" ")
            continue
        kept.append(character)
        index += 1
    return "".join(kept)


def outer_predicates(sql: str) -> str:
    """The outer query's own text: sub-selects gone, comments gone, one space.

    The canonical form the #1341 pins assert against. Key-resolution sub-selects
    contain ``WHERE run_id = :run_id`` against the AUTHORITY table by design, so
    a pin that inspects raw source cannot distinguish switched code from
    unswitched code.
    """
    return _WHITESPACE.sub(" ", strip_comments(strip_scalar_subqueries(sql))).strip()


def text_fact_columns(sql: str, alias: str) -> set[str]:
    """Text identity columns of the fact table referenced by the outer query.

    ``\\b`` after the column name is load-bearing: it keeps ``ts.variable`` from
    matching ``ts.variable_e`` (and ``ts.unit`` from ``ts.unit_e``), which would
    make every pin unsatisfiable rather than discriminating.
    """
    outer = outer_predicates(sql)
    return {
        column
        for column in TEXT_IDENTITY_COLUMNS
        if re.search(rf"\b{re.escape(alias)}\.{column}\b", outer) is not None
    }


# ---------------------------------------------------------------------------
# Table-scoped attribution
# ---------------------------------------------------------------------------

# `FROM`/`JOIN <fact table> [AS] [alias]`. The alias is optional and must not
# swallow the next keyword: three registered statements
# (hydro_display's existence probe, both mvt valid_times branches) name the fact
# table with no alias and follow it straight with `WHERE`.
_FACT_REFERENCE = re.compile(
    r"\b(?:FROM|JOIN)\s+hydro\.river_timeseries(?:_legacy)?\b"
    r"(?:\s+(?:AS\s+)?(?!(?:WHERE|ON|JOIN|CROSS|LEFT|RIGHT|INNER|FULL|OUTER|NATURAL|GROUP|ORDER|LIMIT|HAVING"
    r"|UNION|EXCEPT|INTERSECT|WINDOW|OFFSET|FETCH|USING|SET|RETURNING|VALUES|SELECT|WITH|AND|OR)\b)"
    r"([A-Za-z_][A-Za-z0-9_]*))?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FactTableAttribution:
    """How a statement names the river fact table."""

    aliases: frozenset[str]
    has_unaliased_reference: bool
    reference_count: int


def fact_table_attribution(sql: str) -> FactTableAttribution:
    """The aliases (and bare references) the statement gives the river fact table."""
    aliases: set[str] = set()
    unaliased = False
    count = 0
    for match in _FACT_REFERENCE.finditer(strip_comments(sql)):
        count += 1
        alias = match.group(1)
        if alias is None:
            unaliased = True
        else:
            aliases.add(alias)
    return FactTableAttribution(frozenset(aliases), unaliased, count)


def fact_table_text_identity_columns(sql: str) -> set[str]:
    """Text identity columns this statement predicates on THE FACT TABLE.

    Table-scoped, which is the whole point (#1980 orchestrator requirement):

    * ``rt.run_key = (SELECT run_key FROM hydro.hydro_run WHERE run_id = %s)``
      and display_coverage's ``rt.run_key = (SELECT run_key FROM
      hydro.hydro_run WHERE run_id = %(scan_run_id)s)`` read ``run_id`` on the
      AUTHORITY table. A grep answers "yes" and is wrong; :func:`outer_predicates`
      removes those sub-selects before anything is counted.
    * ``hydro_display``'s existence probe and both ``mvt`` valid_times branches
      give the fact table NO alias, so an alias-only check answers "no" and is
      equally wrong. Where an unaliased fact reference exists, an unqualified
      text identity column in comparison position is attributed to the fact
      table — safe because those statements are single-fact-table statements and
      their authority sub-selects are already stripped.
    """
    attribution = fact_table_attribution(sql)
    outer = outer_predicates(sql)
    found: set[str] = set()
    for alias in attribution.aliases:
        found |= {
            column
            for column in TEXT_IDENTITY_COLUMNS
            if re.search(rf"\b{re.escape(alias)}\.{column}\b", outer) is not None
        }
    if attribution.has_unaliased_reference:
        found |= {
            column
            for column in TEXT_IDENTITY_COLUMNS
            if re.search(rf"(?<![.\w]){column}\s*(=|<>|!=|\bIN\b|\bLIKE\b|=\s*ANY)", outer) is not None
        }
    return found


# ---------------------------------------------------------------------------
# Chain normalisation (the golden equivalence oracle's canonical form)
# ---------------------------------------------------------------------------

# A predicate region ends at the next same-level keyword that cannot be part of
# a conjunction. `)` ends it implicitly, because a region is only ever scanned
# inside one bracket level.
_REGION_STOP = re.compile(
    r"\b(?:GROUP|ORDER|LIMIT|HAVING|WINDOW|UNION|EXCEPT|INTERSECT|RETURNING|OFFSET|FETCH"
    r"|JOIN|LEFT|RIGHT|INNER|CROSS|FULL|NATURAL|WHERE|ON|FROM|SELECT|WITH|VALUES|SET)\b",
    re.IGNORECASE,
)
_REGION_START = re.compile(r"\b(WHERE|ON)\b", re.IGNORECASE)
_AND_SEPARATOR = re.compile(r"\bAND\b", re.IGNORECASE)
_TOP_LEVEL_SELECT = re.compile(r"\bSELECT\b", re.IGNORECASE)
_OPEN_BRACKET_PADDING = re.compile(r"\(\s+")
_CLOSE_BRACKET_PADDING = re.compile(r"\s+\)")


def _canonical(text: str) -> str:
    """One space between tokens, none against a bracket.

    Bracket padding is normalised, not just collapsed: moving a marker line into
    or out of a bracketed guard changes `(x` to `( x` once the comment is
    stripped, and that is exactly the kind of difference this form exists to
    ignore.
    """
    folded = _WHITESPACE.sub(" ", text).strip()
    return _CLOSE_BRACKET_PADDING.sub(")", _OPEN_BRACKET_PADDING.sub("(", folded))


def _top_level_spans(text: str, pattern: re.Pattern[str]) -> list[re.Match[str]]:
    """Matches of ``pattern`` that sit at bracket depth 0, outside strings/comments."""
    matches: list[re.Match[str]] = []
    depth = 0
    index = 0
    length = len(text)
    while index < length:
        character = text[index]
        if character in "'\"":
            index = _scan_quoted(text, index, character)
            continue
        if text.startswith("--", index):
            index = _scan_line_comment(text, index)
            continue
        if text.startswith("/*", index):
            index = _scan_block_comment(text, index)
            continue
        if character == "(":
            depth += 1
            index += 1
            continue
        if character == ")":
            depth -= 1
            index += 1
            continue
        if depth == 0:
            match = pattern.match(text, index)
            if match is not None:
                matches.append(match)
                index = match.end()
                continue
        index += 1
    return matches


def _top_level_groups(text: str) -> list[tuple[int, str]]:
    """``(content_start, content)`` for every bracket group at depth 0."""
    groups: list[tuple[int, str]] = []
    index = 0
    length = len(text)
    while index < length:
        character = text[index]
        if character in "'\"":
            index = _scan_quoted(text, index, character)
            continue
        if text.startswith("--", index):
            index = _scan_line_comment(text, index)
            continue
        if text.startswith("/*", index):
            index = _scan_block_comment(text, index)
            continue
        if character == "(":
            end = _skip_balanced(text, index)
            # `%(name)s` is a psycopg2 named placeholder, not an expression
            # group: treating its body as a conjunct would make `scan_run_id`
            # itself a predicate that must survive an aid deletion.
            if index == 0 or text[index - 1] != "%":
                groups.append((index + 1, text[index + 1 : end - 1]))
            index = end
            continue
        index += 1
    return groups


def _conjuncts(region: str) -> tuple[str, ...]:
    """``region`` split on its own top-level ``AND``, whitespace collapsed."""
    pieces: list[str] = []
    previous = 0
    for match in _top_level_spans(region, _AND_SEPARATOR):
        pieces.append(region[previous : match.start()])
        previous = match.end()
    pieces.append(region[previous:])
    return tuple(_canonical(piece) for piece in pieces if piece.strip())


def _collect_chains(text: str, *, top: bool) -> list[tuple[int, tuple[str, ...]]]:
    found: list[tuple[int, tuple[str, ...]]] = []
    starts = _top_level_spans(text, _REGION_START)
    stops = _top_level_spans(text, _REGION_STOP)
    for start in starts:
        end = len(text)
        for stop in stops:
            if stop.start() >= start.end():
                end = stop.start()
                break
        region = text[start.end() : end]
        conjuncts = _conjuncts(region)
        if conjuncts:
            found.append((start.start(), tuple(sorted(conjuncts))))
    for content_start, content in _top_level_groups(text):
        # A grouped expression that is not a sub-SELECT and holds its own
        # top-level AND is a chain of its own — this is what makes each
        # `OR ( aid AND key )` disjunct comparable (fixture decision 6).
        if _SUBQUERY_START.match(f"({content}") is None:
            conjuncts = _conjuncts(content)
            if len(conjuncts) > 1:
                found.append((content_start, tuple(sorted(conjuncts))))
        found.extend((content_start + offset, chain) for offset, chain in _collect_chains(content, top=False))
    if top and not starts and not _top_level_spans(text, _TOP_LEVEL_SELECT):
        # A fragment (`_SEGMENT_IDENTITY_PREDICATE_SQL`) has no keyword of its
        # own: it IS one chain. Guarded by "no top-level SELECT" so a STATEMENT
        # whose every predicate lives inside a CTE bracket (mvt's tile SQL) is
        # not mistaken for one — that would fold the whole query into a single
        # pseudo-conjunct and read any legal reorder inside a CTE as a change.
        conjuncts = _conjuncts(text)
        if conjuncts:
            found.append((-1, tuple(sorted(conjuncts))))
    return found


def _collect_conjuncts(text: str, *, top: bool) -> list[str]:
    """Every conjunct at every bracket level, single-conjunct groups included.

    Deliberately more inclusive than :func:`sql_chains`: a chain needs at least
    two conjuncts to be a chain, but the retention check must still see the ONE
    predicate a rewritten ``OR (…)`` disjunct is left holding once its aid has
    been deleted. Keeping the two collectors separate is what lets the golden
    form stay free of one-element noise chains while the retention check stays
    exhaustive.
    """
    found: list[str] = []
    starts = _top_level_spans(text, _REGION_START)
    stops = _top_level_spans(text, _REGION_STOP)
    for start in starts:
        end = len(text)
        for stop in stops:
            if stop.start() >= start.end():
                end = stop.start()
                break
        found.extend(_conjuncts(text[start.end() : end]))
    for _content_start, content in _top_level_groups(text):
        if _SUBQUERY_START.match(f"({content}") is None:
            found.extend(_conjuncts(content))
        found.extend(_collect_conjuncts(content, top=False))
    if top and not starts and not _top_level_spans(text, _TOP_LEVEL_SELECT):
        found.extend(_conjuncts(text))
    return found


def sql_conjunct_census(sql: str) -> Counter[str]:
    """Multiset of every conjunct the statement holds, at any bracket level."""
    return Counter(_collect_conjuncts(strip_comments(sql), top=True))


def sql_chains(sql: str) -> tuple[tuple[str, ...], ...]:
    """The statement's AND-chains, in document order, each a sorted conjunct multiset.

    A **chain** is one AND-chain delimited by its own opening keyword or bracket:
    each ``WHERE`` chain, each ``JOIN … ON`` chain, the body of each subquery /
    ``EXISTS`` / ``IN (SELECT …)`` / ``CROSS JOIN LATERAL (…)`` (its inner
    ``WHERE`` is its own chain, never merged with the outer one), and the inside
    of each ``OR (…)`` disjunct. A fragment with no keyword of its own is one
    chain.

    Comments are removed and whitespace is collapsed, and the conjuncts of one
    chain are sorted, so the form is invariant under exactly the three things
    #1980's normalisation is allowed to change — whitespace, marker placement and
    conjunct order WITHIN one chain — and sensitive to everything else. Chains
    are ordered positionally, so a conjunct migrating from a lateral body to the
    outer ``WHERE`` changes two chains and is red.
    """
    stripped = strip_comments(sql)
    found = _collect_chains(stripped, top=True)
    found.sort(key=lambda item: item[0])
    return tuple(chain for _offset, chain in found)


# ---------------------------------------------------------------------------
# Aid lines
# ---------------------------------------------------------------------------

_LEADING_AND = re.compile(r"^AND\b\s*", re.IGNORECASE)
_TRAILING_AND = re.compile(r"\s*\bAND$", re.IGNORECASE)
_AID_KEYWORDS = re.compile(
    r"\b(?:WHERE|FROM|JOIN|ON|SELECT|OR|GROUP|ORDER|LIMIT|HAVING|UNION|EXCEPT|INTERSECT|WITH|CASE|WHEN)\b",
    re.IGNORECASE,
)
_AID_PREDICATE = re.compile(
    rf"^(?:[A-Za-z_][A-Za-z0-9_]*\.)?(?P<column>{'|'.join(TEXT_IDENTITY_COLUMNS)})\b\s*(?:=|<>|!=)\s*(?P<value>\S.*)$"
)
_COMPARISON = re.compile(r"(?<![-=<>!])(<>|!=|<=|>=|=)")


def aid_conjunct(line: str) -> str | None:
    """The line's single text-identity conjunct, or ``None`` if it is not one.

    Accepts exactly the normalised shapes #1980 produces — ``AND rt.run_id = %s``
    (the ordinary conjunct), ``rt.run_id = %(scan_run_id)s AND`` (the first
    conjunct inside a rewritten ``OR (…)`` disjunct) and the bare form — and
    nothing else. The compared VALUE may be a literal, a placeholder of any of
    the three dialects, or another relation's column (mvt's correlated lateral
    probes bind ``lr.run_id`` / ``seg.river_segment_id``), so it is deliberately
    not restricted to constants; what is restricted is that the line carries one
    comparison and no keyword that would make deleting it change the statement's
    structure.
    """
    text = _WHITESPACE.sub(" ", strip_comments(line)).strip()
    if not text:
        return None
    text = _TRAILING_AND.sub("", _LEADING_AND.sub("", text)).strip()
    if not text or _AID_KEYWORDS.search(text) is not None:
        return None
    if len(_COMPARISON.findall(text)) != 1:
        return None
    if text.count("(") != text.count(")"):
        return None
    return text if _AID_PREDICATE.match(text) is not None else None


# ---------------------------------------------------------------------------
# Structural check
# ---------------------------------------------------------------------------

_STRUCTURAL_FAULTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("WHERE with no predicate", re.compile(r"\bWHERE\s+AND\b", re.IGNORECASE)),
    ("FROM followed by AND", re.compile(r"\bFROM\s+AND\b", re.IGNORECASE)),
    ("ON with no predicate", re.compile(r"\bON\s+AND\b", re.IGNORECASE)),
    ("dangling AND before a closing bracket", re.compile(r"\bAND\s*\)", re.IGNORECASE)),
    ("dangling AND at the end", re.compile(r"\bAND\s*$", re.IGNORECASE)),
    ("empty OR bracket", re.compile(r"\bOR\s*\(\s*\)", re.IGNORECASE)),
    ("dangling OR before a closing bracket", re.compile(r"\bOR\s*\)", re.IGNORECASE)),
    ("dangling OR ( at the end", re.compile(r"\bOR\s*\(?\s*$", re.IGNORECASE)),
    ("empty WHERE", re.compile(r"\bWHERE\s*(\)|$)", re.IGNORECASE)),
    ("empty ON", re.compile(r"\bON\s*(\)|$)", re.IGNORECASE)),
)


def assert_structurally_intact(sql: str, entry: str, *, allow_markers: bool = False) -> None:
    """The stand-in for "it still parses" — see the module docstring, decision 3.

    Not a parser: the repo has none and three placeholder dialects coexist in
    these templates. What is checked is exactly what a line deletion can break.

    ``allow_markers`` is for the legacy variant, which is the template verbatim
    and therefore KEEPS its markers; the narrow variant must have none left, and
    a leftover marker there means a whole aid block escaped the deletion.
    """
    folded = _WHITESPACE.sub(" ", strip_comments(sql)).strip()
    depth = 0
    index = 0
    while index < len(sql):
        character = sql[index]
        if character in "'\"":
            index = _scan_quoted(sql, index, character)
            continue
        if sql.startswith("--", index):
            index = _scan_line_comment(sql, index)
            continue
        if sql.startswith("/*", index):
            index = _scan_block_comment(sql, index)
            continue
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth < 0:
                raise RiverTemplateError(f"{entry}: unbalanced parentheses (closed one that was never opened)")
        index += 1
    if depth != 0:
        raise RiverTemplateError(f"{entry}: unbalanced parentheses ({depth} unclosed)")
    for label, pattern in _STRUCTURAL_FAULTS:
        match = pattern.search(folded)
        if match is not None:
            raise RiverTemplateError(f"{entry}: {label} -> ...{folded[max(0, match.start() - 60) : match.end() + 20]}")
    if _MARKER_TAG in sql and not allow_markers:
        raise RiverTemplateError(f"{entry}: a transitional aid marker survived rendering")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_CANONICAL_NAME = re.compile(rf"{re.escape(RIVER_TABLE)}\b")
_POSITIONAL_PLACEHOLDER = re.compile(r"%s")
_NAMED_PSYCOPG_PLACEHOLDER = re.compile(r"%\((?P<name>[A-Za-z_][A-Za-z0-9_]*)\)s")
_NAMED_SQLALCHEMY_PLACEHOLDER = re.compile(r"(?<![:\w]):(?P<name>[A-Za-z_][A-Za-z0-9_]*)\b")


def _rename_table(template: str, store: str) -> str:
    """The canonical table name replaced by the store's physical name.

    ``\\b`` refuses to match before ``_legacy``, so rendering an already-legacy
    text is idempotent rather than producing ``..._legacy_legacy``.
    """
    if store == "legacy":
        return _CANONICAL_NAME.sub(RIVER_TABLE_LEGACY, template)
    return template


def _strip_aids(template: str, entry: str) -> tuple[str, tuple[int, ...], tuple[str, ...]]:
    """Delete every marker line and the single aid conjunct beneath it.

    Fail-closed on every shape the normalisation forbids, because each of them
    means the deletion would change the statement rather than only remove a
    redundant conjunct: a non-verbatim marker (mvt's pre-#1980 one-marker-covers-
    four form), a marker with no line under it, and a next line that is not
    exactly one aid conjunct (two conjuncts, a keyword line, a key predicate).
    """
    lines = template.split("\n")
    placeholder_lines = _placeholder_line_indices(template)
    removed_lines: set[int] = set()
    removed_aids: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if _MARKER_TAG not in line:
            index += 1
            continue
        if line.strip() != PUSHDOWN_AID_MARKER:
            raise RiverTemplateError(
                f"{entry}: line {index + 1} carries a NON-VERBATIM aid marker {line.strip()!r}; "
                f"every aid must carry exactly {PUSHDOWN_AID_MARKER!r} on its own line"
            )
        if index + 1 >= len(lines):
            raise RiverTemplateError(f"{entry}: the aid marker on line {index + 1} is the last line of the template")
        aid = aid_conjunct(lines[index + 1])
        if aid is None:
            raise RiverTemplateError(
                f"{entry}: the line under the aid marker on line {index + 1} is not exactly one aid conjunct "
                f"-> {lines[index + 1].strip()!r}"
            )
        removed_lines.update({index, index + 1})
        removed_aids.append(aid)
        index += 2
    kept = "\n".join(line for number, line in enumerate(lines) if number not in removed_lines)
    removed_placeholders = tuple(
        position for position, line_number in enumerate(placeholder_lines) if line_number in removed_lines
    )
    return kept, removed_placeholders, tuple(removed_aids)


def _placeholder_line_indices(template: str) -> tuple[int, ...]:
    """Line number of each positional ``%s`` placeholder, in template order.

    ``%(name)s`` contains no ``%s`` substring, so the two psycopg2 dialects do
    not collide here.
    """
    return tuple(template.count("\n", 0, match.start()) for match in _POSITIONAL_PLACEHOLDER.finditer(template))


def _assert_no_fact_text_identity(sql: str, entry: str) -> None:
    found = fact_table_text_identity_columns(sql)
    if found:
        raise RiverTemplateError(
            f"{entry}: the narrow variant still predicates on fact-table text identity column(s) "
            f"{sorted(found)} — the narrow table has no such column"
        )


def _assert_key_predicates_retained(
    legacy_sql: str,
    narrow_sql: str,
    removed_aids: tuple[str, ...],
    entry: str,
) -> None:
    """Every legacy conjunct survives, except the aids and the guards holding one.

    Expressed against :func:`sql_chains` rather than against the "aid is adjacent
    to its counterpart" oracle on purpose: mvt's correlated lateral probes put
    three conjuncts between ``ts.run_key`` and ``ts.run_id``, so adjacency is not
    a property this check can assume. Conjunct survival is both stronger and
    independent of layout.

    A guard that CONTAINS an aid (``(%(scan_run_id)s IS NULL OR (rt.run_id = …
    AND rt.run_key = …))``) necessarily changes text when the aid goes, so it is
    exempted here — its inner chain is compared conjunct by conjunct like any
    other, and the golden oracle pins its legacy form.
    """
    narrow_conjuncts = sql_conjunct_census(narrow_sql)
    for conjunct, count in sql_conjunct_census(legacy_sql).items():
        if any(aid in conjunct for aid in removed_aids):
            continue
        if narrow_conjuncts[conjunct] < count:
            raise RiverTemplateError(
                f"{entry}: the narrow variant lost the predicate {conjunct!r} that is not a transitional aid"
            )
    # No "the aid text is gone" substring check here on purpose: the unaliased
    # statements spell their aid `run_id = :run_id`, which is ALSO the authority
    # sub-select's own required predicate (`SELECT run_key FROM hydro.hydro_run
    # WHERE run_id = :run_id`). Survival is asserted table-scoped instead, by
    # :func:`_assert_no_fact_text_identity`, which is the same distinction this
    # whole module exists to keep straight.


def render_river_ts_sql(template: str, store: str, *, entry: str = "<template>") -> RenderedSql:
    """Render one river read template for one timeseries store.

    ``legacy`` renames ``hydro.river_timeseries`` to ``hydro.river_timeseries_legacy``
    and changes NOTHING else — every transitional aid stays, because the legacy
    table keeps the text-column compression layout that makes them load-bearing
    for the plan.

    ``narrow`` keeps the canonical name and deletes every aid marker line
    together with the single aid conjunct on the line beneath it, then proves the
    result: structurally intact, free of every fact-table text identity column and
    of every marker, and still carrying every key/enum predicate the legacy
    variant had.

    Raises :class:`RiverTemplateError`, naming ``entry``, rather than returning
    SQL whenever any of that does not hold.
    """
    if store not in STORES:
        raise RiverTemplateError(f"{entry}: unknown timeseries store {store!r} (expected one of {list(STORES)})")
    if store == "legacy":
        sql = _rename_table(template, "legacy")
        assert_structurally_intact(sql, entry, allow_markers=True)
        return RenderedSql(sql)
    sql, removed_placeholders, removed_aids = _strip_aids(template, entry)
    assert_structurally_intact(sql, entry)
    _assert_no_fact_text_identity(sql, entry)
    _assert_key_predicates_retained(template, sql, removed_aids, entry)
    return RenderedSql(sql, removed_placeholders)


# ---------------------------------------------------------------------------
# Cross-store combinator
# ---------------------------------------------------------------------------

_HYDRO_RUN_REFERENCE = re.compile(
    r"\b(?:FROM|JOIN)\s+hydro\.hydro_run\b"
    r"(?:\s+(?:AS\s+)?(?!(?:WHERE|ON|JOIN|CROSS|LEFT|RIGHT|INNER|FULL|OUTER|NATURAL|GROUP|ORDER|LIMIT|HAVING"
    r"|UNION|EXCEPT|INTERSECT|WINDOW|OFFSET|FETCH|USING|SET|RETURNING|VALUES|SELECT|WITH|AND|OR)\b)"
    r"([A-Za-z_][A-Za-z0-9_]*))?",
    re.IGNORECASE,
)


def store_binding_form(template: str) -> str:
    """``"join"`` if the fact table's OWN bracket scope has a ``hydro_run`` alias, else ``"exists"``.

    Reported per registry entry rather than chosen silently: only a template that
    already joins ``hydro_run`` beside the fact table can bind
    ``hydro_run.timeseries_store`` by a column reference; every other one has to
    reach the authority table through a correlated ``EXISTS`` on ``run_key``,
    which is a different plan and a different cost.

    Same scope, not "anywhere in the statement", deliberately. A correlated body
    can see an OUTER alias — ``forcing_copyback_backfill``'s probe sits inside
    ``EXISTS (…)`` under ``FROM hydro.hydro_run h`` and could legally say
    ``h.timeseries_store`` — but deciding that requires resolving scope chains,
    and being wrong about it produces a statement that binds the wrong run. The
    conservative answer (``exists``) is always correct; a template that wants the
    cheaper form declares it by joining ``hydro_run`` in the same scope. So this
    reports ``exists`` for the copyback probe, which is a cost note, not a bug.
    """
    scope = _fact_table_scope(template, "<template>")
    return "join" if _hydro_run_alias_in(scope.text) is not None else "exists"


@dataclass(frozen=True)
class _FactScope:
    """The bracket level that holds the fact table's own ``FROM`` and ``WHERE``."""

    text: str
    offset: int
    where_end: int
    fact_alias: str | None
    #: the PHYSICAL table name as written in this scope, used to qualify
    #: ``run_key`` when the fact table has no alias — an unqualified ``run_key``
    #: inside the correlated sub-select would resolve to the sub-select's OWN
    #: ``hydro_run.run_key``, making the correlation ``x = x`` and the branch
    #: predicate a no-op that selects every run.
    fact_table: str


def _fact_table_name(reference: str) -> str:
    """``hydro.river_timeseries`` or ``hydro.river_timeseries_legacy``, as written."""
    return RIVER_TABLE_LEGACY if RIVER_TABLE_LEGACY.lower() in reference.lower() else RIVER_TABLE


def _hydro_run_alias_in(text: str) -> str | None:
    for match in _HYDRO_RUN_REFERENCE.finditer(strip_comments(text)):
        if match.group(1) is not None:
            return match.group(1)
    return None


def _fact_table_scope(template: str, entry: str) -> _FactScope:
    """The innermost bracket level whose own text declares the fact table."""

    def descend(text: str, offset: int) -> _FactScope | None:
        for content_start, content in _top_level_groups(text):
            deeper = descend(content, offset + content_start)
            if deeper is not None:
                return deeper
        match = _FACT_REFERENCE.search(strip_comments(text))
        if match is None:
            return None
        starts = [start for start in _top_level_spans(text, _REGION_START) if start.group(1).upper() == "WHERE"]
        starts = [start for start in starts if start.start() >= match.start()]
        if not starts:
            raise RiverTemplateError(f"{entry}: the fact table's scope has no WHERE chain to bind the store on")
        start = starts[0]
        end = len(text)
        for stop in _top_level_spans(text, _REGION_STOP):
            if stop.start() >= start.end():
                end = stop.start()
                break
        return _FactScope(text, offset, offset + end, match.group(1), _fact_table_name(match.group(0)))

    scope = descend(template, 0)
    if scope is None:
        raise RiverTemplateError(f"{entry}: the template does not read {RIVER_TABLE}")
    return scope


def _store_predicate(template: str, store: str, entry: str) -> tuple[str, str]:
    scope = _fact_table_scope(template, entry)
    alias = _hydro_run_alias_in(scope.text)
    if alias is not None:
        return "join", f"{alias}.timeseries_store = '{store}'"
    qualifier = f"{scope.fact_alias or scope.fact_table}."
    return (
        "exists",
        "EXISTS (SELECT 1 FROM hydro.hydro_run store_route "
        f"WHERE store_route.run_key = {qualifier}run_key AND store_route.timeseries_store = '{store}')",
    )


def _bind_store(template: str, store: str, entry: str) -> tuple[str, str]:
    form, predicate = _store_predicate(template, store, entry)
    scope = _fact_table_scope(template, entry)
    cut = scope.where_end
    return form, f"{template[:cut]}\n  AND {predicate}\n{template[cut:]}"


def _named_parameters(template: str) -> tuple[str, tuple[str, ...]]:
    psycopg = tuple(match.group("name") for match in _NAMED_PSYCOPG_PLACEHOLDER.finditer(template))
    sqlalchemy = tuple(
        match.group("name")
        for match in _NAMED_SQLALCHEMY_PLACEHOLDER.finditer(_NAMED_PSYCOPG_PLACEHOLDER.sub("", template))
        if not match.group(0).startswith("::")
    )
    if psycopg and sqlalchemy:
        return "mixed", tuple(dict.fromkeys(psycopg + sqlalchemy))
    if psycopg:
        return "psycopg", tuple(dict.fromkeys(psycopg))
    return "sqlalchemy", tuple(dict.fromkeys(sqlalchemy))


def render_union_all(
    template: str,
    stores: tuple[str, ...],
    params: dict[str, object],
    *,
    entry: str = "<template>",
) -> RenderedUnion:
    """One ``UNION ALL`` branch per store, each bound to that store's runs.

    Named-parameter templates only. A positional (``%s``) template is refused
    outright rather than duplicated: the branches would silently require the
    caller's tuple twice, in an order that depends on which aids each branch
    dropped, and psycopg2 reports the arity mismatch only at execute time.

    Each branch binds ``hydro_run.timeseries_store`` inside the fact table's own
    scope — by column reference where that scope already joins ``hydro_run``,
    otherwise by a correlated ``EXISTS`` on ``run_key``. :func:`store_binding_form`
    reports which form a template gets, so the choice is a documented property of
    the template rather than an invisible decision of the renderer.

    Parameter names are SHARED across the branches: both placeholder dialects
    allow a name to repeat, so the caller's mapping goes through unchanged and
    every branch reads the same window. Per-branch renaming was rejected
    deliberately (orchestrator ruling on #1980 D1) — it would leak a
    ``<name>_<store>`` binding contract into every caller, for a per-branch
    window nothing asks for. A caller that genuinely needs two windows renders
    the branches itself.
    """
    if _POSITIONAL_PLACEHOLDER.search(template) is not None:
        raise RiverTemplateError(
            f"{entry}: render_union_all accepts named-parameter templates only; this one uses positional %s"
        )
    if not stores:
        raise RiverTemplateError(f"{entry}: render_union_all needs at least one store")
    unknown = [store for store in stores if store not in STORES]
    if unknown:
        raise RiverTemplateError(f"{entry}: unknown timeseries store(s) {unknown}")
    dialect, names = _named_parameters(template)
    if dialect == "mixed":
        raise RiverTemplateError(f"{entry}: the template mixes %(name)s and :name placeholders")
    missing = [name for name in names if name not in params]
    if missing:
        raise RiverTemplateError(f"{entry}: render_union_all was not given values for {missing}")
    branches: list[str] = []
    forms: list[str] = []
    for store in stores:
        rendered = render_river_ts_sql(template, store, entry=f"{entry} [{store}]")
        form, with_store = _bind_store(rendered.sql, store, f"{entry} [{store}]")
        forms.append(form)
        branches.append(with_store)
    sql = "\nUNION ALL\n".join(f"(\n{branch}\n)" for branch in branches)
    return RenderedUnion(sql, dict(params), tuple(forms))
