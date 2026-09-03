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

* **structural check** — NOT a proof that the SQL parses; the repo has no SQL
  parser and three placeholder dialects coexist. It is a fixed list of the
  breakages a line deletion OR a store-predicate splice can cause, enumerated in
  ``_STRUCTURAL_FAULTS``: unbalanced parentheses, ``WHERE AND`` / ``FROM AND`` /
  ``ON AND``, a dangling ``AND`` / ``OR (`` before a ``)`` or the end of the
  text, a connective or a bare ``WHERE`` / ``ON`` immediately before a keyword of
  ``_KEYWORD_FAMILY`` (which includes the row-locking ``FOR UPDATE`` family), a
  doubled connective, a connective stranded before the statement terminator
  (``AND ;``) or a predicate spliced behind it (``; AND``), a conjunct spliced
  into a row-count clause (``LIMIT 1 AND …``), and a leftover marker. The
  patterns run over CODE: comments and string bodies are blanked first
  (:func:`non_code_spans`), so a literal that spells a fault is data rather than
  a refusal, and a fault cannot hide inside one. A statement can be malformed in
  a way this list does not name; what it guarantees is that these specific
  deletion and splice artefacts do not reach a database.
* **table-scoped no-text-identity** (narrow only) — no text identity column of
  the FACT table survives. Table-scoped, not a grep: ``rt.run_key = (SELECT
  run_key FROM hydro.hydro_run WHERE run_id = %(scan_run_id)s)`` legitimately
  reads ``run_id`` on the AUTHORITY table (so a grep is wrong), and three
  registered statements give the fact table no alias at all (so an alias-only
  check is blind). Attribution therefore resolves the fact table's aliases from
  its own ``FROM`` / ``JOIN`` clauses and falls back to a bare-column
  comparison-position scan only where an unaliased reference exists.
* **key/enum predicate retention** (narrow only) — every conjunct of the legacy
  variant survives into the narrow variant. A conjunct is exempt ONLY when it
  EQUALS a removed aid; a conjunct that merely holds one (``EXISTS (… AND aid AND
  …)``, the ``OR (aid AND key)`` guard) is not exempt — it must reappear in its
  aid-deleted form, which is computed and looked up. Containment was the earlier
  rule and it let the authority predicate ``run_key = (SELECT … WHERE run_id =
  :run_id)`` exempt itself, because the aid of an unaliased statement is a
  substring of it. Computed from the same chain normaliser the golden
  equivalence oracle uses, so "the deletion removed one line too many" is red
  here rather than in production.

Reading BOTH stores in one statement
------------------------------------

During the transition a reader that must see every run needs both tables, and
:func:`render_union_all` is the one combinator this module ships for that: one
``UNION ALL`` branch per store, each branch's fact-table reads bound by
``hydro_run.timeseries_store``. It is a POSITIVE WHITELIST — a read is bound only
where the binding is provably semantics-preserving under ``UNION ALL``, and every
shape the walk cannot classify is refused naming the entry and the reason. The
five conditions, what they do NOT cover, and every refusal are in that function's
own docstring; the reasoning behind each one is in the section comment above
:func:`store_binding_plan`. Three review rounds found the same class of defect
here (a read bound in a non-equivalent context) while the rule was an accretion
of refused spellings, which is why it is now stated as an admission rule.

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

    ``removed_aids`` is the canonical text of each deleted aid conjunct, in
    template order. Returned so a caller — or an oracle — can check the
    placeholder arithmetic against the aids THEMSELVES rather than against
    ``count('%s')`` before minus after, which is an identity of the deletion and
    therefore cannot go red (round-2 H7-a). Empty for ``legacy``.
    """

    sql: str
    removed_placeholders: tuple[int, ...] = ()
    removed_aids: tuple[str, ...] = ()


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
        per branch, in ``stores`` order, the store-binding form
        (``"alias"`` / ``"exists"`` / ``"physical"``) of EVERY fact-table
        reference in that branch, in document order — see
        :func:`store_binding_forms`. Nested because a statement can read the
        fact table more than once and the forms can differ between the reads; a
        single word per branch could only be a summary, and a summary is what
        let three unbound reads report themselves bound (review #1996, C1).
    ``branch_sql``
        each branch on its own, in ``stores`` order, before they were joined by
        ``UNION ALL`` — each already store-bound, aid-stripped (narrow) or
        aid-preserving (legacy), and structurally checked. What a caller
        debugging ONE branch's plan needs, and what a test asserting per branch
        asserts against: the combined ``sql`` interleaves the two and cannot be
        pinned per store. Note that a branch's own ``ORDER BY`` / ``LIMIT``
        applies to that branch alone, which is exactly why a statement whose
        result depends on them is declared ``union_safe=False`` rather than
        combined here.
    """

    sql: str
    params: dict[str, object]
    branches: tuple[tuple[str, ...], ...]
    branch_sql: tuple[str, ...] = ()


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


#: The fact table's NAME, wherever it appears — the independent counter's whole
#: vocabulary. Deliberately ignorant of ``FROM`` / ``JOIN`` / aliases: its job is
#: to disagree with the structural walk whenever the statement names the table in
#: a form the walk does not model.
#: The trailing ``(?!\s*\.)`` excludes a name used as a COLUMN qualifier
#: (``hydro.river_timeseries_legacy.run_key`` — what the unaliased ``EXISTS``
#: binding emits): that is a reference to a column, not a second read of the
#: table, and counting it would make the post-condition's re-walk of the bound
#: text refuse the module's own output.
_FACT_NAME = re.compile(r"\bhydro\.river_timeseries(?:_legacy)?\b(?!\s*\.)", re.IGNORECASE)
_FACT_NAME_QUOTED = re.compile(r'"hydro"\s*\.\s*"river_timeseries(?:_legacy)?"(?!\s*\.)', re.IGNORECASE)


_DOLLAR_QUOTE_OPEN = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")


def _scan_dollar_quoted(sql: str, start: int, tag: str) -> int:
    """Index just past the dollar-quoted body opening with ``tag`` at ``start``."""
    end = sql.find(tag, start + len(tag))
    return len(sql) if end == -1 else end + len(tag)


_NON_CODE_COMMENT = "comment"
_NON_CODE_LITERAL = "literal"


def non_code_spans(sql: str) -> tuple[tuple[int, int, str], ...]:
    """Half-open ``(start, stop, kind)`` spans of ``sql`` that are NOT code.

    ONE traversal, consumed by everything that has to tell code from data — the
    independent occurrence counter, the table rename (decision 14) and the
    structural check. Three private scanners with three slightly different ideas
    of what a literal is were how ``_rename_table`` came to rewrite the fact
    table's name inside ``'reads hydro.river_timeseries' AS note`` (round-3
    L2-3): one span helper cannot disagree with itself.

    Covered: ``--`` line comments and ``/* … */`` block comments (kind
    ``"comment"``); single-quoted literals with doubled-quote escapes traversed,
    and dollar-quoted bodies ``$$ … $$`` / ``$tag$ … $tag$`` (kind
    ``"literal"``).

    NOT covered, deliberately: double-quoted text. In PostgreSQL that is a
    quoted IDENTIFIER, so ``"hydro"."river_timeseries"`` is a read of the fact
    table and must be counted, while ``'hydro.river_timeseries'`` is data and
    must not.
    """
    spans: list[tuple[int, int, str]] = []
    length = len(sql)
    index = 0
    while index < length:
        character = sql[index]
        if character == '"':
            index = _scan_quoted(sql, index, character)
            continue
        if character == "'":
            end, kind = _scan_quoted(sql, index, character), _NON_CODE_LITERAL
        elif sql.startswith("--", index):
            end, kind = _scan_line_comment(sql, index), _NON_CODE_COMMENT
        elif sql.startswith("/*", index):
            end, kind = _scan_block_comment(sql, index), _NON_CODE_COMMENT
        elif character == "$" and (opener := _DOLLAR_QUOTE_OPEN.match(sql, index)) is not None:
            end, kind = _scan_dollar_quoted(sql, index, opener.group(0)), _NON_CODE_LITERAL
        else:
            index += 1
            continue
        spans.append((index, end, kind))
        index = end
    return tuple(spans)


def _in_non_code(spans: tuple[tuple[int, int, str], ...], position: int) -> bool:
    return any(start <= position < stop for start, stop, _kind in spans)


def _blank_non_code(sql: str, *, keep_literal_quotes: bool = False) -> str:
    """``sql`` with comments and string bodies blanked — OFFSETS AND NEWLINES KEPT.

    ``keep_literal_quotes`` keeps a literal's opening and closing quote and
    blanks only its body, which is what the structural check needs: blanking the
    quotes too folds ``AND tag = 'x'`` down to ``AND tag = `` and synthesises a
    "dangling AND at the end" fault out of a perfectly intact statement, whereas
    ``AND tag = ' '`` cannot. Comments are always blanked whole — a comment is
    not a token, so leaving its delimiters behind would leave a stray ``-`` in
    the code stream.
    """
    blanked = list(sql)
    for start, stop, kind in non_code_spans(sql):
        keep = keep_literal_quotes and kind == _NON_CODE_LITERAL
        inner_start = start + 1 if keep else start
        inner_stop = stop - 1 if keep else stop
        for position in range(inner_start, max(inner_start, inner_stop)):
            if sql[position] != "\n":
                blanked[position] = " "
    return "".join(blanked)


def _blank_comments_and_literals(sql: str) -> str:
    """``sql`` with comments and string bodies blanked, offsets preserved.

    The coordinate system every structural decision in this module is made in:
    the scope walk locates a fact reference, its governing chain and that chain's
    end as offsets into text of the SAME length as the template, so nothing can
    slide out of alignment (review #1996, C3).
    """
    return _blank_non_code(sql)


def fact_table_name_occurrences(sql: str) -> int:
    """How many times the statement NAMES the fact table, counted independently.

    Derived from the name alone, with no model of ``FROM`` / ``JOIN`` / segments,
    precisely so that it can disagree with :func:`fact_table_attribution` and the
    scope walk. When it does, the statement spells a read in a form the binder
    does not understand — a comma join (``FROM fact a, fact b``), ``FROM ONLY``,
    a quoted identifier — and the binder refuses instead of leaving that read
    scanning both stores (round-2 H3).
    """
    text = _blank_comments_and_literals(sql)
    return len(_FACT_NAME.findall(text)) + len(_FACT_NAME_QUOTED.findall(text))


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
#: The one keyword family this module recognises as "a predicate chain stops
#: here". ONE constant, used by both the region-stop regex that finds where a
#: chain ends and the structural faults that catch a chain whose last conjunct
#: was deleted — two enumerations of the same concept drift apart, and the
#: shorter one decides what a line deletion is allowed to break (round-2 H6).
#:
#: The outer-join arm is spelled out rather than listing bare ``LEFT`` /
#: ``RIGHT`` / ``FULL``: those are also the string functions ``LEFT(col, n)`` /
#: ``RIGHT(col, n)``, and a bare listing would both stop a chain mid-expression
#: and report ``AND LEFT(r.tag, 3) = 'abc'`` as a dangling connective.
#:
#: The ``FOR (UPDATE|NO KEY UPDATE|SHARE|KEY SHARE)`` arm is a row-locking
#: clause, not a predicate: a store predicate spliced after it produces ``FOR
#: UPDATE AND h.timeseries_store = '…'``, which is a syntax error at execute
#: time and nothing textual noticed (round-3 L1-3). Listing it here gives the
#: chain end AND the "dangling connective before a keyword" fault the same
#: knowledge in one place.
_KEYWORD_FAMILY = (
    r"(?:(?:LEFT|RIGHT|FULL)(?:\s+OUTER)?\s+JOIN"
    r"|FOR\s+(?:NO\s+KEY\s+)?UPDATE|FOR\s+(?:KEY\s+)?SHARE"
    r"|GROUP|ORDER|LIMIT|HAVING|WINDOW|UNION|EXCEPT|INTERSECT|RETURNING|OFFSET|FETCH"
    r"|JOIN|INNER|CROSS|NATURAL|WHERE|ON|FROM|SELECT|WITH|VALUES|SET)"
)

_REGION_STOP = re.compile(rf"\b{_KEYWORD_FAMILY}\b", re.IGNORECASE)

#: Set operations split one bracket level into independent SELECT segments. An
#: alias, a FROM and a WHERE all belong to exactly one of them.
_SEGMENT_SEPARATOR = re.compile(r"\b(?:UNION|EXCEPT|INTERSECT)\b", re.IGNORECASE)
#: Where a predicate chain OPENS. ``HAVING`` is one (fixture decision 12): its
#: body is an AND-chain of predicates like any other, and leaving it out made the
#: golden blind to a deleted ``HAVING COUNT(…) = %(…)s`` line.
#:
#: ``DISTINCT ON (…)`` is NOT one, and it has to be matched here to say so: the
#: ``ON`` of a ``SELECT DISTINCT ON (col)`` select list is not a join condition,
#: and reading it as a chain opener turned the select list into a pseudo-chain
#: the golden then policed as if it were a predicate (decision 12 again — the
#: golden is a predicate-chain oracle, decision 6). The alternative is matched
#: FIRST so the ``ON`` inside it is consumed, and it captures NOTHING: every
#: consumer keeps only matches whose group 1 is set.
_REGION_START = re.compile(r"\bDISTINCT\s+ON\b|\b(WHERE|ON|HAVING)\b", re.IGNORECASE)


def _region_starts(text: str) -> list[re.Match[str]]:
    """Chain openers at bracket depth 0 — ``DISTINCT ON`` matches dropped."""
    return [match for match in _top_level_spans(text, _REGION_START) if match.group(1) is not None]
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
    starts = _region_starts(text)
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
    starts = _region_starts(text)
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
    conjunct order WITHIN one chain. Chains are ordered positionally, so a
    conjunct migrating from a lateral body to the outer ``WHERE`` changes two
    chains and is red.

    The subject is the conjunct multiset PER PREDICATE CHAIN, and nothing else:
    the SELECT list, ``FROM``/``JOIN`` targets and aliases, ``LIMIT`` /
    ``ORDER BY`` / ``GROUP BY`` and CTE names sit outside every chain, so this
    form is blind to them by construction. Said explicitly because the first
    wording claimed sensitivity "to everything else", which is the kind of
    over-claim that gets an oracle trusted for a job it cannot do (review #1996,
    C11).
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

# A connective immediately before a keyword of the family, a WHERE or an ON
# immediately followed by one, means a line deletion ate the chain's last (or
# only) conjunct: `WHERE a = %s AND` / marker / aid / `LIMIT 1` folds to
# `WHERE a = %s AND LIMIT 1`, and an ON chain whose single conjunct WAS the aid
# folds to `ON JOIN …`. Neither the "dangling AND at the end" nor the "before a
# closing bracket" pattern sees either, because neither the end of the text nor
# a bracket follows (review #1996 C4; round-2 H6 widened the enumeration to the
# whole `_KEYWORD_FAMILY` and added the ON arm).
_STRUCTURAL_FAULTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("WHERE with no predicate", re.compile(r"\bWHERE\s+AND\b", re.IGNORECASE)),
    ("dangling connective before a keyword", re.compile(rf"\b(?:AND|OR)\s+{_KEYWORD_FAMILY}\b", re.IGNORECASE)),
    ("doubled connective", re.compile(r"\b(?:AND|OR)\s+(?:AND|OR)\b", re.IGNORECASE)),
    ("WHERE with no predicate before a keyword", re.compile(rf"\bWHERE\s+{_KEYWORD_FAMILY}\b", re.IGNORECASE)),
    ("ON with no predicate before a keyword", re.compile(rf"\bON\s+{_KEYWORD_FAMILY}\b", re.IGNORECASE)),
    ("FROM followed by AND", re.compile(r"\bFROM\s+AND\b", re.IGNORECASE)),
    ("ON with no predicate", re.compile(r"\bON\s+AND\b", re.IGNORECASE)),
    ("dangling AND before a closing bracket", re.compile(r"\bAND\s*\)", re.IGNORECASE)),
    ("dangling AND at the end", re.compile(r"\bAND\s*$", re.IGNORECASE)),
    ("empty OR bracket", re.compile(r"\bOR\s*\(\s*\)", re.IGNORECASE)),
    ("dangling OR before a closing bracket", re.compile(r"\bOR\s*\)", re.IGNORECASE)),
    ("dangling OR ( at the end", re.compile(r"\bOR\s*\(?\s*$", re.IGNORECASE)),
    ("empty WHERE", re.compile(r"\bWHERE\s*(\)|$)", re.IGNORECASE)),
    ("empty ON", re.compile(r"\bON\s*(\)|$)", re.IGNORECASE)),
    # A `;` ENDS the statement. A connective left in front of it, or a predicate
    # spliced behind it, is not a slow query — the first is a syntax error and
    # the second is text the server never sees as part of the statement. Neither
    # the "before a keyword" nor the "at the end of the text" pattern sees them,
    # because `;` is not a keyword and is not the end of the text (round-3 L1-3).
    ("dangling connective before the statement terminator", re.compile(r"\b(?:AND|OR)\s*;", re.IGNORECASE)),
    ("predicate spliced after the statement terminator", re.compile(r";\s*(?:AND|OR)\b", re.IGNORECASE)),
    # `LIMIT 1 AND …` / `OFFSET 10 AND …` / `FETCH FIRST AND …`: a clause tail
    # that swallowed a conjunct. Only the row-count clauses are listed: `ORDER
    # BY` and `GROUP BY` take comma-separated EXPRESSION lists, in which a
    # following `AND` can be part of a legitimate boolean sort key, so a fault on
    # them would refuse valid SQL (verifier round-3 L3).
    (
        "a conjunct spliced into a row-count clause",
        re.compile(r"\b(?:LIMIT|OFFSET|FETCH)\s+[^\s;]+\s+AND\b", re.IGNORECASE),
    ),
)


def assert_structurally_intact(sql: str, entry: str, *, allow_markers: bool = False) -> None:
    """The stand-in for "it still parses" — see the module docstring, decision 3.

    Not a parser: the repo has none and three placeholder dialects coexist in
    these templates. What is checked is exactly what a line deletion can break.

    ``allow_markers`` is for the legacy variant, which is the template verbatim
    and therefore KEEPS its markers; the narrow variant must have none left, and
    a leftover marker there means a whole aid block escaped the deletion.

    The fault patterns run over CODE, not over data: comments and string bodies
    are blanked first (:func:`non_code_spans`), so a literal reading ``'... AND
    LIMIT ...'`` is neither a fault nor a hiding place for one. The literals'
    QUOTES are kept — blanking them too could fold ``AND tag = 'x'`` down to
    ``AND tag = `` and synthesise a "dangling AND at the end" out of an intact
    statement.
    """
    folded = _WHITESPACE.sub(" ", _blank_non_code(sql, keep_literal_quotes=True)).strip()
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
    """The canonical table name replaced by the store's physical name — IN CODE ONLY.

    ``\\b`` refuses to match before ``_legacy``, so rendering an already-legacy
    text is idempotent rather than producing ``..._legacy_legacy``.

    Substituted only OUTSIDE comments, single-quoted literals and dollar-quoted
    bodies (:func:`non_code_spans`, fixture decision 14). A name inside a literal
    is DATA — ``'reads hydro.river_timeseries' AS note`` is a string a caller may
    compare, log or store — and rewriting it changed the statement's output in
    the legacy branch while the occurrence counter, which already ignored
    literals, saw nothing (round-3 L2-3). A name inside a comment is likewise
    left as written: the transitional markers and the surrounding prose name the
    canonical table on purpose, and the narrow branch deletes them by line rather
    than by rewrite.
    """
    if store != "legacy":
        return template
    spans = non_code_spans(template)
    return _CANONICAL_NAME.sub(
        lambda match: match.group(0) if _in_non_code(spans, match.start()) else RIVER_TABLE_LEGACY,
        template,
    )


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


def _raw_conjuncts(text: str) -> list[str]:
    """``text`` split on its own top-level ``AND``, pieces UNcanonicalised.

    The canonicalising :func:`_conjuncts` is for comparison; this one is for
    rebuilding, so the nested structure of each piece survives the round trip.
    """
    pieces: list[str] = []
    previous = 0
    for match in _top_level_spans(text, _AND_SEPARATOR):
        pieces.append(text[previous : match.start()])
        previous = match.end()
    pieces.append(text[previous:])
    return [piece for piece in pieces if piece.strip()]


def _without_aids(text: str, exempt: set[str]) -> str:
    """``text`` with every aid deleted from inside its bracketed sub-expressions.

    What a conjunct that CONTAINS an aid must look like after the aid is gone —
    ``EXISTS (… AND rt.variable = 'q_down' AND …)`` and the ``(%(scan_run_id)s IS
    NULL OR (aid AND key))`` guard both change text when their aid goes, and
    both must still be accounted for.

    Computed rather than exempted, because "it holds the aid somewhere" is the
    hole this replaced: the aid of an unaliased statement is ``run_id =
    :run_id``, a substring of the authority predicate ``run_key = (SELECT
    run_key FROM hydro.hydro_run WHERE run_id = :run_id)``, so containment let
    that predicate exempt ITSELF and deleting it outright passed every assert
    (review #1996, C8). Here it does not: the aid is not a top-level conjunct of
    the sub-select — the piece is the whole ``SELECT … WHERE run_id = :run_id``
    — so nothing is dropped, the expected form equals the original, and its
    absence is reported.
    """
    rebuilt: list[str] = []
    last = 0
    for content_start, content in _top_level_groups(text):
        kept = [piece for piece in _raw_conjuncts(content) if _canonical(piece) not in exempt]
        rebuilt.append(text[last:content_start])
        rebuilt.append(" AND ".join(_without_aids(piece, exempt).strip() for piece in kept))
        last = content_start + len(content)
    rebuilt.append(text[last:])
    return "".join(rebuilt)


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
    AND rt.run_key = …))``, ``EXISTS (… AND rt.variable = 'q_down' AND …)``)
    necessarily changes text when the aid goes. It is NOT exempted: the form it
    must have without the aid is computed by :func:`_without_aids` and that form
    is required to be present, so the guard's other conjuncts stay protected.

    The one exemption is EXACT, not substring containment. An unaliased
    statement spells its aid ``run_id = :run_id``, and that is a substring of the
    authority predicate ``run_key = (SELECT run_key FROM hydro.hydro_run WHERE
    run_id = :run_id)`` the whole check exists to protect: under containment the
    key predicate exempted itself, and deleting it outright passed every assert
    (review #1996, C8).
    """
    exempt = {_canonical(aid) for aid in removed_aids}
    narrow_conjuncts = sql_conjunct_census(narrow_sql)
    for conjunct, count in sql_conjunct_census(legacy_sql).items():
        if conjunct in exempt or narrow_conjuncts[conjunct] >= count:
            continue
        expected = _canonical(_without_aids(conjunct, exempt))
        if expected != conjunct and narrow_conjuncts[expected] >= count:
            continue
        raise RiverTemplateError(
            f"{entry}: the narrow variant lost the predicate {conjunct!r} that is not a transitional aid"
        )
    # No "the aid text is gone" substring check here on purpose: the unaliased
    # statements spell their aid `run_id = :run_id`, which is ALSO the authority
    # sub-select's own required predicate (`SELECT run_key FROM hydro.hydro_run
    # WHERE run_id = :run_id`). Removal is asserted table-scoped instead, by
    # :func:`_assert_no_fact_text_identity`, which answers a DIFFERENT question
    # from this one — it detects the PRESENCE of a text identity column on the
    # fact table, never the LOSS of a key predicate, so it is a complement to
    # this check and not a substitute for it.


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
    return RenderedSql(sql, removed_placeholders, removed_aids)


# ---------------------------------------------------------------------------
# Cross-store combinator
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# The cross-store union whitelist
#
# A fact-table read is bound by `hydro_run.timeseries_store` ONLY when this walk
# can PROVE the binding is semantics-preserving under `UNION ALL`. Three review
# rounds each found the same class of defect here — a read bound in a context
# where the binding is not equivalent (a guard exemption, a splice into a
# sibling segment, an INSERT accepted as an operand, a nested outer join bound
# silently) — because the rule was an accretion of REFUSED SPELLINGS: each round
# closed the spelling it was shown and left the class open.
#
# So the rule is inverted. `_classify_fact_reads` is a POSITIVE WHITELIST: a
# reference binds iff all five conditions below hold, and every reference the
# walk cannot classify raises `RiverTemplateError` naming the entry, the
# reference and the reason. "Unmodelled" and "refused" are the same thing here;
# there is no path that leaves a read unbound and returns SQL anyway.
#
#   1. STATEMENT KIND    the comment/literal-blanked text carries no
#                        data-modifying keyword at any depth; named parameters;
#                        `kind == statement`. A `WITH … SELECT` read is fine.
#   2. CONTEXT           no ancestor bracket opener contains an outer join or a
#                        negation, and no enclosing segment is the right operand
#                        of `EXCEPT` / `INTERSECT`.
#   3. REFERENCE FORM    `FROM fact [AS] a` bound into its own segment's WHERE,
#                        or `[INNER] JOIN fact [AS] a ON` bound into that join's
#                        own ON. Nothing else, no fallback across chains, and
#                        the independent name counter must agree.
#   4. PREDICATE FORM    the alias form only where the scope correlates a
#                        `hydro_run` alias on `run_key`; otherwise a correlated
#                        EXISTS qualified by the reference's own alias, or by
#                        the physical table name where it has none.
#   5. DECLARED SAFETY   the caller declares `union_safe` — statement-level
#                        UNION equivalence cannot be read off the text, so it is
#                        declared, never inferred (fixture decision 13).
#
# Conditions 1–4 are structural and checked here. Condition 5 is checked by
# `render_union_all`, first, so a declared-unsafe statement is refused with the
# declared reason rather than with whatever structural objection comes first.
# ---------------------------------------------------------------------------

_HYDRO_RUN_REFERENCE = re.compile(
    r"\b(?:FROM|JOIN)\s+hydro\.hydro_run\b"
    r"(?:\s+(?:AS\s+)?(?!(?:WHERE|ON|JOIN|CROSS|LEFT|RIGHT|INNER|FULL|OUTER|NATURAL|GROUP|ORDER|LIMIT|HAVING"
    r"|UNION|EXCEPT|INTERSECT|WINDOW|OFFSET|FETCH|USING|SET|RETURNING|VALUES|SELECT|WITH|AND|OR)\b)"
    r"([A-Za-z_][A-Za-z0-9_]*))?",
    re.IGNORECASE,
)

#: Every statement kind that writes. Scanned at EVERY bracket depth, not only at
#: depth 0: `WITH moved AS (INSERT … RETURNING …) SELECT … FROM fact …` is a
#: data-modifying CTE, it executes once per UNION branch, and a depth-0 scan does
#: not see it. `display_coverage:refresh` — `WITH … INSERT INTO … ON CONFLICT …
#: RETURNING run_id` — was rendered as a UNION operand and passed the whole
#: registry sweep, because the sweep asserted binding placement only (round-3
#: L1-1).
_DATA_MODIFYING = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE|COPY|LOCK)\b",
    re.IGNORECASE,
)

#: An outer join anywhere in an ancestor opener, LATERAL or not. Whichever
#: placement a store predicate takes under one of these, it is not equivalent:
#: in the `ON` it copies the NULL-extended rows into BOTH branches, in a `WHERE`
#: it turns the outer join into an inner one, and a driving row that matches in
#: neither store is still NULL-extended and survives in both branches, so the
#: union emits it twice. Spelled with the `JOIN` required so `LEFT(col, 3)`
#: cannot trip it.
_OUTER_JOIN = re.compile(r"\b(?:LEFT|RIGHT|FULL)(?:\s+OUTER)?\s+JOIN\b", re.IGNORECASE)

#: A `RIGHT` / `FULL` join ANYWHERE in the fact reference's own segment OR in
#: any segment enclosing it.
#:
#: `LEFT JOIN` after the fact reference is safe and the register needs it
#: (`publisher` and `forcing_copyback_backfill` both `LEFT JOIN
#: met.forcing_version`): the fact side is the PRESERVED side, so every fact row
#: still appears in exactly one branch and the nullable side is NULL-extended
#: per row as before. `RIGHT` and `FULL` are the reverse — they preserve the
#: OTHER relation, so a row whose fact side is NULL-extended is dropped by the
#: store predicate in EVERY branch (`FROM fact rt RIGHT JOIN o`: the o-only rows
#: vanish) or kept by EVERY branch and duplicated by the union (`FROM x JOIN fact
#: rt ON … RIGHT JOIN o`: the o-only rows appear twice). Neither is equivalent,
#: neither is caught by the preserved-side check — which only looks at the
#: keyword immediately before the reference — and neither appears in the
#: register, so both refuse (self-review of the round-3 redesign).
#:
#: Checked over the ANCESTRY, not just the reference's own segment, because
#: depth hides exactly the same shape: in `FROM (SELECT … FROM fact rt WHERE …)
#: s RIGHT JOIN o ON o.k = s.k` the bracket's opener is a bare `FROM` and the
#: inner segment holds no join at all, yet the o-only rows are NULL-extended in
#: BOTH branches and the union emits them twice (round-4 finding). Loose like
#: :data:`_NEGATION`: a RIGHT/FULL join elsewhere at an ancestor level refuses
#: too, false refusals accepted, and no registered template has one.
_PRESERVING_OTHER_SIDE = re.compile(r"\b(?:RIGHT|FULL)(?:\s+OUTER)?\s+JOIN\b", re.IGNORECASE)

#: A negated context. `NOT EXISTS (… fact …)` / `NOT IN (SELECT … fact …)` invert
#: the predicate's sense: narrowing the sub-select to one store WIDENS the outer
#: result, so both branches keep rows the single-store statement drops.
#:
#: Loose on purpose — any `NOT` before the opener refuses, false refusals
#: accepted — with ONE exception: `NOT MATERIALIZED` is a CTE materialisation
#: hint and carries no negation at all. Without the exception every `mvt` tile
#: statement (`source_rows AS NOT MATERIALIZED (`) would refuse, which is a false
#: refusal of a real read path rather than caution.
_NEGATION = re.compile(r"\bNOT\b(?!\s+MATERIALIZED\b)", re.IGNORECASE)

#: `UNION` splits a level into operands whose rows all survive, so a fact read in
#: one of them is bindable. `EXCEPT` / `INTERSECT` do not: the RIGHT operand's
#: rows are SUBTRACTED from (or intersected with) the left, so narrowing it to
#: one store adds rows to (or removes rows from) the result in the wrong
#: direction, and the two branches disagree.
_NON_UNION_SET_OPERATOR = re.compile(r"\b(?:EXCEPT|INTERSECT)\b", re.IGNORECASE)

#: A TOP-LEVEL disjunction in the chain the store predicate would be appended to.
#: The one rule here that is not about WHERE the predicate lands but about what
#: it ASSOCIATES with: `AND` binds tighter than `OR`, so appending to `WHERE a =
#: :a OR b = :b` yields `a = :a OR (b = :b AND store)` — the `a` rows are
#: unfiltered, survive in BOTH branches and the union emits them twice.
#: `_conjuncts` splits on top-level `AND` only, so `a OR b` is ONE conjunct and
#: the anchor post-condition sees a well-formed `<anchor> AND <predicate>`; no
#: structural fault names it either (round-4 self-review). Matched against the
#: MASKED chain, so a bracketed disjunction — `AND (rt.run_id = … OR (…))`, the
#: shape D5 gives every registered `OR` — stays invisible and still binds.
_TOP_LEVEL_DISJUNCTION = re.compile(r"\bOR\b", re.IGNORECASE)

_OUTER_JOIN_PREFIX = re.compile(r"\b(?:LEFT|RIGHT|FULL)(?:\s+OUTER)?\s*$", re.IGNORECASE)

#: A row-locking clause. PostgreSQL forbids one on a set operation outright:
#: "Currently, FOR NO KEY UPDATE, FOR UPDATE, FOR SHARE and FOR KEY SHARE cannot
#: be specified either for a UNION result or for any input of a UNION" (SELECT
#: reference, UNION Clause) — the server answers `FOR UPDATE is not allowed with
#: UNION/INTERSECT/EXCEPT`. So a locking read is not a UNION ALL operand at all,
#: whatever the binder could prove about its store predicate.
_LOCK_CLAUSE = re.compile(r"\bFOR\s+(?:NO\s+KEY\s+)?UPDATE\b|\bFOR\s+(?:KEY\s+)?SHARE\b", re.IGNORECASE)


def _mask_nested(text: str) -> str:
    """``text`` with comments, string bodies and nested bracket groups blanked out.

    OFFSETS ARE PRESERVED — every masked character becomes a space, newlines
    kept — and that is the point. The scope walk below must locate a fact
    reference, the chain keyword governing it and that chain's end in ONE
    coordinate system. The first implementation searched ``strip_comments(text)``
    for the fact table and compared the offset it got back against spans of the
    UNSTRIPPED text; each comment ahead of the table collapses to one character
    and slides the two apart, which bound the store predicate into a different
    UNION branch (review #1996, C3). Masking removes the class rather than the
    instance: there is no second coordinate system left to disagree with.

    Masking nested groups is also what makes "this scope's own text" mean what it
    says — a ``hydro_run`` alias declared inside a scalar sub-select is not in
    scope for the enclosing WHERE, and searching the unmasked level text read it
    as if it were (C2).
    """
    masked = list(text)
    length = len(text)
    index = 0

    def blank(start: int, stop: int) -> None:
        for position in range(start, stop):
            if text[position] != "\n":
                masked[position] = " "

    while index < length:
        character = text[index]
        if character in "'\"":
            end = _scan_quoted(text, index, character)
        elif text.startswith("--", index):
            end = _scan_line_comment(text, index)
        elif text.startswith("/*", index):
            end = _scan_block_comment(text, index)
        elif character == "(":
            end = _skip_balanced(text, index)
        else:
            index += 1
            continue
        blank(index, end)
        index = end
    return "".join(masked)


@dataclass(frozen=True)
class BoundScope:
    """One CLASSIFIED fact-table read: where it is, and where its store goes.

    Produced only for a reference that satisfies conditions 1–4 of the whitelist;
    anything else raises instead of becoming one of these. Public because the
    shape oracles pin placement against a hand-written table of these fields
    (fixture decision 13): an oracle that re-ran the binder's own walk to compute
    what it then compared against the binder agrees with any walk, which is how a
    splice into the wrong chain stayed green (round-3 L3-1).
    """

    #: absolute offset of the fact reference itself — the document-order key
    reference_at: int
    #: ``"FROM"`` or ``"JOIN"``, the form the reference is written in
    reference_form: str
    #: ``"where"`` or ``"on"`` — DICTATED by :attr:`reference_form`, never chosen
    chain_kind: str
    #: absolute span of the chain that governs THIS reference. The store
    #: predicate is spliced at :attr:`chain_end`.
    chain_start: int
    chain_end: int
    #: ``"alias"`` — ``<h>.timeseries_store = '<store>'`` on a ``hydro_run``
    #: alias this scope correlates on ``run_key``; ``"exists"`` — a correlated
    #: EXISTS qualified by the fact reference's own alias; ``"physical"`` — the
    #: same EXISTS qualified by the branch's physical table name, for a reference
    #: that has no alias.
    predicate_form: str
    #: the correlated ``hydro_run`` alias, set iff :attr:`predicate_form` is
    #: ``"alias"``
    hydro_run_alias: str | None
    fact_alias: str | None
    #: the PHYSICAL table name as written, used to qualify ``run_key`` when the
    #: fact table has no alias — an unqualified ``run_key`` inside the correlated
    #: sub-select would resolve to the sub-select's OWN ``hydro_run.run_key``,
    #: making the correlation ``x = x`` and the branch predicate a no-op that
    #: selects every run.
    fact_table: str
    #: the chain's LAST conjunct in canonical form — the text the store predicate
    #: is spliced immediately after. The anchor an oracle pins placement on
    #: without re-deriving it from this walk.
    anchor: str


@dataclass(frozen=True)
class _Level:
    """One bracket level of a statement, with the context it inherits."""

    text: str
    offset: int
    #: the level's own text with nested groups, comments and string bodies
    #: blanked; same length as :attr:`text`, so offsets are shared
    masked: str
    #: the opener text of every ANCESTOR bracket, outermost first — each read
    #: back to the region stop that opens its clause. The only cross-level
    #: information the walk carries, and it is carried as TEXT: what a lateral's
    #: enclosing join is written as cannot be read off the lateral's own text.
    openers: tuple[str, ...]
    #: for this level and every ancestor, the set-operation keyword immediately
    #: preceding the segment the descendant sits in (``""`` where there is none)
    separators: tuple[str, ...]
    #: the masked text of the ENCLOSING segment at every ancestor level,
    #: outermost first — the opener alone stops at the clause that opens the
    #: bracket and so cannot see a join written AFTER it
    segments: tuple[str, ...]


def _fact_table_name(reference: str) -> str:
    """``hydro.river_timeseries`` or ``hydro.river_timeseries_legacy``, as written."""
    return RIVER_TABLE_LEGACY if RIVER_TABLE_LEGACY.lower() in reference.lower() else RIVER_TABLE


def _segment_bounds(masked: str, position: int) -> tuple[int, int]:
    """The set-operation segment ``position`` falls in, at this bracket level.

    ``SELECT … FROM fact WHERE …`` and ``SELECT … FROM other WHERE …`` joined by
    ``UNION ALL`` share one bracket level and share nothing else: each has its
    own FROM, its own aliases and its own WHERE. Every search below is bounded to
    one segment, so a reference in the first segment can never be handed the
    second segment's chain — which is what happened when the search ran to the
    end of the level (round-2 H1).
    """
    start, end = 0, len(masked)
    for match in _SEGMENT_SEPARATOR.finditer(masked):
        if match.end() <= position:
            start = match.end()
        else:
            end = match.start()
            break
    return start, end


def _preceding_separator(masked: str, position: int) -> str:
    """The set-operation keyword opening ``position``'s segment, or ``""``."""
    keyword = ""
    for match in _SEGMENT_SEPARATOR.finditer(masked):
        if match.end() <= position:
            keyword = match.group(0)
        else:
            break
    return keyword


def _opener_text(masked: str, bracket_at: int, segment_start: int) -> str:
    """The text that OPENS the bracket at ``bracket_at``, at its parent's level.

    Read back to the START of the region stop that opens the clause the bracket
    sits in — including that keyword, which is the whole point: the opener of
    ``LEFT JOIN (SELECT … FROM fact …)`` has to CONTAIN ``LEFT JOIN`` for the
    context check to see it.

    An ``ON`` is not such a keyword. ``ON`` continues the join clause that
    precedes it, so an opener that starts at an ``ON`` is extended back over it
    until a keyword that really opens a clause is found (falling back to the
    segment start). That is what makes ``LEFT JOIN x ON EXISTS (SELECT 1 FROM
    fact …)`` refuse — its EXISTS bracket sits inside an OUTER JOIN's condition,
    where a store predicate copies NULL-extended rows into both branches — while
    ``[INNER] JOIN x ON EXISTS (…)`` binds (round-3 L2-1).
    """
    stops = [stop for stop in _REGION_STOP.finditer(masked, segment_start, bracket_at)]
    index = len(stops) - 1
    while index >= 0 and stops[index].group(0).upper() == "ON":
        index -= 1
    start = segment_start if index < 0 else stops[index].start()
    return masked[start:bracket_at]


def _levels(
    text: str,
    offset: int,
    openers: tuple[str, ...],
    separators: tuple[str, ...],
    segments: tuple[str, ...],
) -> list[_Level]:
    """Every bracket level of ``text``, outermost first, each with its ancestry."""
    masked = _mask_nested(text)
    levels = [_Level(text, offset, masked, openers, separators, segments)]
    for content_start, content in _top_level_groups(text):
        bracket_at = content_start - 1
        segment_start, segment_end = _segment_bounds(masked, bracket_at)
        levels.extend(
            _levels(
                content,
                offset + content_start,
                openers + (_opener_text(masked, bracket_at, segment_start),),
                separators + (_preceding_separator(masked, bracket_at),),
                segments + (masked[segment_start:segment_end],),
            )
        )
    return levels


def _refuse_uncombinable_statement(template: str, entry: str) -> None:
    """Condition 1: a UNION ALL operand has to be ONE unterminated, unlocked READ.

    Three separate ways a statement fails to be an operand, checked in this order
    because each has its own accurate reason and the DML scan would otherwise
    answer for the first one:

    1. a row-locking clause — PostgreSQL rejects the set operation itself;
    2. a statement terminator — the branches are wrapped in ``(`` ``)``, so a
       ``;`` inside one is a syntax error, and two ``;``-separated statements are
       not a thing a single union can duplicate;
    3. a data-modifying keyword at any depth.

    ``FOR UPDATE`` used to be refused by 3 (``_DATA_MODIFYING`` matches the
    ``UPDATE`` token) with a message calling it data-modifying, and ``FOR SHARE``
    /``FOR KEY SHARE`` carry no such token and were rendered straight through; a
    trailing ``;`` was rendered into ``(\n… ;\n)`` and returned as SQL. Both are
    round-4 findings. The binder's chain-end rule stops at a ``;`` or a locking
    clause independently (:func:`_chain_end`) and is kept as the inner layer:
    :func:`_bind_store` is the seam those probes pin, and it must not walk a
    predicate past the end of the query proper even when its caller has already
    refused the statement.
    """
    blanked = _blank_comments_and_literals(template)
    lock = _LOCK_CLAUSE.search(blanked)
    if lock is not None:
        raise RiverTemplateError(
            f"{entry}: render_union_all accepts unlocked reads only; this one carries the row-locking clause "
            f"{' '.join(lock.group(0).split()).upper()!r} at offset {lock.start()}. PostgreSQL allows a "
            f"locking clause neither on a UNION result nor on any UNION input (SELECT reference, UNION "
            f"Clause), so there is no branch shape to render — lock in a single-store statement, or take the "
            f"rows without a lock"
        )
    terminator = blanked.find(";")
    if terminator != -1:
        raise RiverTemplateError(
            f"{entry}: render_union_all accepts ONE unterminated statement; this one carries a ';' at offset "
            f"{terminator}. Each branch is wrapped in parentheses, so a ';' inside one is a syntax error, and "
            f"a ';'-separated pair of statements is not something a single UNION ALL can duplicate — hand the "
            f"statement over without its terminator"
        )
    match = _DATA_MODIFYING.search(blanked)
    if match is not None:
        raise RiverTemplateError(
            f"{entry}: render_union_all accepts read statements only; this one carries the data-modifying "
            f"keyword {match.group(0).upper()!r} at offset {match.start()}. Duplicating a write into two "
            f"UNION ALL branches executes it twice, and a UNION ALL of two INSERTs is not a statement at "
            f"all — a cross-store WRITE is the write guard's problem, not the read combinator's"
        )


def _refuse_bad_context(level: _Level, reference: re.Match[str], entry: str) -> None:
    """Condition 2: no outer join, no negation, no EXCEPT/INTERSECT right operand.

    Checked over the reference's WHOLE ancestry — every enclosing bracket's
    opener and every enclosing segment's separator — because depth is exactly
    what the earlier "look at the parent level's text before the `(`" rule could
    not see: a LATERAL behind one redundant bracket, or a subquery two levels
    inside an outer join's ON, bound silently (round-3 L2-1).
    """
    name = _fact_table_name(reference.group(0))
    own_segment_start, _own_segment_end = _segment_bounds(level.masked, reference.start())
    own_separator = _preceding_separator(level.masked, reference.start())
    at = level.offset + reference.start()

    # First, because it is the most specific: the fact table IS the outer join's
    # target. Any of the broader checks below would also refuse it, with a
    # message about the segment or an ancestor instead of about this reference.
    if _OUTER_JOIN_PREFIX.search(level.masked[own_segment_start : reference.start()]) is not None:
        raise RiverTemplateError(
            f"{entry}: {name} is read on the preserved side of an outer join at offset {at}; "
            f"neither placement of a store predicate is equivalent there (ON copies the "
            f"NULL-extended rows into both branches, WHERE turns the outer join into an inner one) "
            f"— rewrite the read as a sub-query"
        )

    for depth, opener in enumerate(level.openers):
        if _OUTER_JOIN.search(opener) is not None:
            raise RiverTemplateError(
                f"{entry}: {name} is read at offset {at} inside a bracket that its ancestor (depth {depth}) "
                f"opens with an OUTER JOIN -> ...{_canonical(opener)[-90:]!r}. Neither placement of a store "
                f"predicate is equivalent there: in the join's ON it copies the NULL-extended rows into both "
                f"branches, in a WHERE it turns the outer join into an inner one, and a driving row matching "
                f"in NEITHER store is NULL-extended in both branches so the union emits it twice — rewrite "
                f"the read as a sub-query, or make the join CROSS/INNER"
            )
        if _NEGATION.search(opener) is not None:
            raise RiverTemplateError(
                f"{entry}: {name} is read at offset {at} inside a NEGATED context (depth {depth}) -> "
                f"...{_canonical(opener)[-90:]!r}. Under NOT EXISTS / NOT IN, narrowing the sub-select to one "
                f"store WIDENS the outer result, so both branches keep rows the single-store statement drops"
            )

    for depth, separator in enumerate((*level.separators, own_separator)):
        if separator and _NON_UNION_SET_OPERATOR.match(separator) is not None:
            raise RiverTemplateError(
                f"{entry}: {name} is read at offset {at} in the RIGHT operand of "
                f"{separator.upper()} (depth {depth}); its rows are subtracted from (or intersected with) the "
                f"left operand's, so narrowing it to one store moves the result in the wrong direction"
            )

    own_segment = level.masked[own_segment_start:_own_segment_end]
    for depth, segment in enumerate((*level.segments, own_segment)):
        preserving = _PRESERVING_OTHER_SIDE.search(segment)
        if preserving is None:
            continue
        raise RiverTemplateError(
            f"{entry}: {name} is read at offset {at} inside a segment (depth {depth}) carrying "
            f"{' '.join(preserving.group(0).split()).upper()}, which preserves the OTHER relation; a row whose "
            f"fact side is NULL-extended is then dropped by the store predicate in every branch, or kept by "
            f"every branch and duplicated by the union. A LEFT JOIN after the fact reference is fine (the fact "
            f"side is preserved) — rewrite a RIGHT/FULL join as its LEFT mirror, or the read as a sub-query"
        )

def _governing_chain(level: _Level, reference: re.Match[str], segment: tuple[int, int]) -> re.Match[str] | None:
    """Condition 3's chain: the ONE chain a store predicate for ``reference`` may go in.

    The chain kind is dictated by the reference form and there is no fallback:

    * ``FROM hydro.river_timeseries`` binds in its own segment's ``WHERE``;
    * ``JOIN hydro.river_timeseries … ON`` binds in THAT join's ``ON``, and only
      when the ``ON`` is the next thing the join opens — ``JOIN fact r USING
      (run_key) JOIN other o ON …`` must not be handed the other join's chain.

    Anything else — a FROM reference whose segment has no WHERE, a join written
    with ``USING`` or with no ``ON`` at all — returns ``None`` and is refused by
    the caller. The earlier "WHERE if there is one, else whatever opened first"
    fallback is exactly how a predicate ends up filtering a relation that is not
    the one it was written for (round-2 H2). ``HAVING`` is a chain for the golden
    (decision 12) but never a binding chain: it filters GROUPS, so a store
    predicate there is evaluated after aggregation.
    """
    seg_start, seg_end = segment
    after = [
        start
        for start in _region_starts(level.masked)
        if reference.end() <= start.start() < seg_end and start.start() >= seg_start
    ]
    if reference.group(0).upper().startswith("JOIN"):
        if not after or after[0].group(1).upper() != "ON":
            return None
        if _REGION_STOP.search(level.masked, reference.end(), after[0].start()) is not None:
            return None
        return after[0]
    return next((start for start in after if start.group(1).upper() == "WHERE"), None)


def _chain_end(masked: str, chain: re.Match[str], limit: int) -> int:
    """Where the chain opened by ``chain`` stops — the splice point.

    Three terminators, all of them measured hazards rather than theory:

    * the next region stop at this level (``ORDER BY``, another ``JOIN``, …);
    * a ``;``, which ENDS the statement — a predicate spliced after it is not
      part of the statement at all;
    * a row-locking clause (``FOR UPDATE`` and friends), which is not a
      predicate and cannot be AND-ed with one (round-3 L1-3).

    The last two were invisible to the region-stop family, so the splice landed
    past the terminator and no structural fault named the result.
    """
    end = limit
    for stop in _REGION_STOP.finditer(masked):
        if stop.start() >= chain.end():
            end = min(stop.start(), limit)
            break
    semicolon = masked.find(";", chain.end(), end)
    return semicolon if semicolon != -1 else end


def _scope_chains(level: _Level, segment: tuple[int, int]) -> tuple[str, ...]:
    """Every predicate chain of the reference's own segment, at its own level.

    The scope a ``hydro_run`` correlation must be visible in. Plural because the
    correlation is normally written in the ``hydro_run`` JOIN's ON clause while
    the fact reference's own chain is the WHERE: all eight ``forecast_store``
    segment blocks say ``FROM hydro.river_timeseries rt JOIN hydro.hydro_run h ON
    h.run_key = rt.run_key WHERE …``, so a check that looked only at the
    reference's own chain would find no correlation and fall back to EXISTS for
    every one of them.
    """
    seg_start, seg_end = segment
    chains: list[str] = []
    for start in _region_starts(level.masked):
        if not seg_start <= start.start() < seg_end:
            continue
        chains.append(level.masked[start.end() : _chain_end(level.masked, start, seg_end)])
    return tuple(chains)


def _predicate_form(level: _Level, segment: tuple[int, int], fact_alias: str | None) -> tuple[str, str | None]:
    """Condition 4: which store predicate this scope has EARNED, and its correlation.

    The alias form ``h.timeseries_store = '<store>'`` is only equivalent when
    ``h`` is joined to THIS fact reference on ``run_key``: without that equality
    ``h`` is some other run's row, and the branch predicate then routes by the
    wrong run while looking perfectly correct. ``publisher``'s discovery has a
    ``hydro_run`` alias in scope AND ``ON r.run_key = h.run_key``, so it earns
    it; a statement that reaches ``hydro_run`` through a link table
    (``JOIN hydro.run_link l ON l.child_run_key = rt.run_key JOIN hydro.hydro_run
    h ON h.run_key = l.parent_run_key``) has the alias in scope and NO
    correlation, and gets the correlated EXISTS instead (round-3 L2-2).

    A reference with no alias cannot be correlated at all — there is no
    ``<alias>.run_key`` to write — so it takes the EXISTS form qualified by the
    branch's physical table name.

    The correlation has to BE a top-level conjunct of one of the scope's chains,
    not merely appear somewhere in one. ``ON h.run_key = rt.run_key OR h.run_key
    IS NULL`` and ``WHERE NOT h.run_key = rt.run_key`` both CONTAIN the equality
    and neither ESTABLISHES it, and a substring search earned the alias form for
    both (round-4 self-review). A correlation written inside brackets —
    ``ON (h.run_key = rt.run_key) AND …`` — is blanked out of the masked text
    this reads and so is invisible here; like everything else that does not
    match, it falls back to the correlated EXISTS, which is always correct.
    """
    if fact_alias is None:
        return "physical", None
    seg_start, seg_end = segment
    chains = _scope_chains(level, segment)
    own_segment = level.masked[seg_start:seg_end]
    for match in _HYDRO_RUN_REFERENCE.finditer(own_segment):
        alias = match.group(1)
        if alias is None:
            continue
        # An OUTER-joined `hydro_run` may be NULL-extended, and `NULL =
        # '<store>'` is NULL, so `h.timeseries_store = '…'` would drop the row in
        # EVERY branch — losing rows the single-store statement keeps. The
        # correlated EXISTS on the fact reference's own key is always correct, so
        # such an alias simply does not earn the cheap form (self-review of the
        # round-3 redesign; no registered template outer-joins `hydro_run`).
        if _OUTER_JOIN_PREFIX.search(own_segment[: match.start()]) is not None:
            continue
        correlations = (
            rf"{re.escape(alias)}\.run_key\s*=\s*{re.escape(fact_alias)}\.run_key",
            rf"{re.escape(fact_alias)}\.run_key\s*=\s*{re.escape(alias)}\.run_key",
        )
        conjuncts = [conjunct.strip() for chain in chains for conjunct in _conjuncts(chain)]
        if any(re.fullmatch(pattern, conjunct, re.IGNORECASE) for pattern in correlations for conjunct in conjuncts):
            return "alias", alias
    return "exists", None


def _classify_fact_reads(template: str, entry: str) -> tuple[BoundScope, ...]:
    """EVERY fact-table read of ``template``, classified — or a refusal.

    Conditions 2–4 of the whitelist, per reference, in document order. There is
    no branch that skips a reference: a statement that reads the fact table three
    times (``mvt``'s national tile SQL: two correlated laterals and an identity
    probe) yields three :class:`BoundScope`s or an error, never two scopes and a
    read left scanning both stores while the return value reports the branch
    bound (review #1996, C1).

    The scope count is cross-checked against an INDEPENDENT one
    (:func:`fact_table_name_occurrences`, which counts the NAME and knows nothing
    about ``FROM`` / ``JOIN`` / aliases), so a comma join
    (``FROM fact a, fact b``), a ``FROM ONLY``, a quoted identifier or any other
    spelling that names the table without matching :data:`_FACT_REFERENCE` is
    REFUSED rather than silently left unbound (round-2 H3).
    """
    scopes: list[BoundScope] = []
    for level in _levels(template, 0, (), (), ()):
        for reference in _FACT_REFERENCE.finditer(level.masked):
            name = _fact_table_name(reference.group(0))
            at = level.offset + reference.start()
            _refuse_bad_context(level, reference, entry)

            segment = _segment_bounds(level.masked, reference.start())
            reference_form = "JOIN" if reference.group(0).upper().startswith("JOIN") else "FROM"
            chain = _governing_chain(level, reference, segment)
            if chain is None:
                raise RiverTemplateError(
                    f"{entry}: the {reference_form} reference to {name} at offset {at} has no chain of its "
                    f"own to bind the store on (a FROM reference needs its own segment's WHERE, a JOIN "
                    f"reference its own ON; a comma join, a USING join and a join with no condition have "
                    f"neither) — the walk refuses rather than borrow a sibling's chain"
                )
            end = _chain_end(level.masked, chain, segment[1])
            fact_alias = reference.group(1)
            predicate_form, hydro_run_alias = _predicate_form(level, segment, fact_alias)
            disjunction = _TOP_LEVEL_DISJUNCTION.search(level.masked, chain.end(), end)
            if disjunction is not None:
                raise RiverTemplateError(
                    f"{entry}: the {chain.group(1).upper()} chain governing {name} at offset {at} is a "
                    f"top-level DISJUNCTION (OR at offset {level.offset + disjunction.start()}); AND binds "
                    f"tighter than OR, so appending the store predicate would filter only the LAST operand "
                    f"and leave the others unfiltered in every branch, which the union then duplicates — "
                    f"bracket the disjunction into one conjunct"
                )
            conjuncts = _conjuncts(strip_comments(level.text[chain.end() : end]))
            if not conjuncts:
                raise RiverTemplateError(
                    f"{entry}: the {chain.group(1).upper()} chain governing {name} at offset {at} is empty; "
                    f"there is no conjunct to AND a store predicate onto"
                )
            scopes.append(
                BoundScope(
                    reference_at=at,
                    reference_form=reference_form,
                    chain_kind=chain.group(1).lower(),
                    chain_start=level.offset + chain.start(),
                    chain_end=level.offset + end,
                    predicate_form=predicate_form,
                    hydro_run_alias=hydro_run_alias,
                    fact_alias=fact_alias,
                    fact_table=name,
                    anchor=conjuncts[-1],
                )
            )
    attributed = fact_table_attribution(template).reference_count
    named = fact_table_name_occurrences(template)
    if not scopes and named == 0:
        raise RiverTemplateError(f"{entry}: the template does not read {RIVER_TABLE}")
    # Order matters: a statement that names the table in a form the walk cannot
    # see (`FROM ONLY …`, a quoted identifier) produces NO scopes, and reporting
    # that as "does not read the fact table" would be the opposite of the truth.
    if not len(scopes) == attributed == named:
        raise RiverTemplateError(
            f"{entry}: the scope walk classified {len(scopes)} fact-table reference(s), attribution found "
            f"{attributed} FROM/JOIN reference(s) and the name occurs {named} time(s) — refusing to bind a "
            f"store on a partly-understood statement (a comma join, FROM ONLY, a USING join or a quoted "
            f"identifier names the table in a form the walk does not model)"
        )
    scopes.sort(key=lambda scope: scope.reference_at)
    return tuple(scopes)


def store_binding_plan(template: str, *, entry: str = "<template>") -> tuple[BoundScope, ...]:
    """The classified fact-table reads of ``template``, in document order.

    The public read-only view of the whitelist: what an oracle asks so it can
    compare the walk's decisions against a hand-written table instead of against
    the walk itself. Raises :class:`RiverTemplateError` on any reference the
    whitelist does not admit — conditions 2–4; the caller-declared condition 5
    and the read-statement condition 1 are :func:`render_union_all`'s.
    """
    return _classify_fact_reads(template, entry)


def store_binding_forms(template: str) -> tuple[str, ...]:
    """One predicate form per place the statement reads the fact table, in order.

    ``"alias"`` where that reference's own scope both has a ``hydro_run`` alias
    and correlates it on ``run_key``, ``"exists"`` where the authority table has
    to be reached through a correlated sub-select qualified by the reference's
    alias, ``"physical"`` where the reference has no alias and the sub-select is
    qualified by the branch's physical table name — a different plan and a
    different cost, so which one a template gets is reported rather than decided
    invisibly.

    Plural because a statement can read the fact table more than once and the
    answer is per reference: ``mvt``'s national tile SQL reads it in two laterals
    and once more in an identity probe. The singular :func:`store_binding_form`
    collapses this to one word and says ``"mixed"`` when the references disagree.

    Raises:
        RiverTemplateError: if ANY fact-table read of ``template`` falls outside
            the whitelist (conditions 2-4 -- see :func:`render_union_all`). There
            is no "unclassifiable" form to report: a template this cannot answer
            for is a template the combinator refuses, and reporting a form for
            the references it did understand would be the fail-open the walk
            exists to prevent.
    """
    return tuple(scope.predicate_form for scope in _classify_fact_reads(template, "<template>"))


def store_binding_form(template: str) -> str:
    """``"alias"``, ``"exists"``, ``"physical"``, or ``"mixed"`` when they disagree.

    Same scope, not "anywhere in the statement", deliberately. A correlated body
    can see an OUTER alias — ``forcing_copyback_backfill``'s probe sits inside
    ``EXISTS (…)`` under ``FROM hydro.hydro_run h`` and could legally say
    ``h.timeseries_store`` — but deciding that requires resolving scope chains,
    and being wrong about it produces a statement that binds the wrong run. The
    conservative answer (``exists``) is always correct; a template that wants the
    cheaper form declares it by joining ``hydro_run`` in the same scope AND
    correlating it on ``run_key``. So this reports ``exists`` for the copyback
    probe, which is a cost note, not a bug.

    Raises:
        RiverTemplateError: as :func:`store_binding_forms` -- ``"mixed"`` means
            the references disagree about the FORM, never that one of them could
            not be classified.
    """
    forms = set(store_binding_forms(template))
    return forms.pop() if len(forms) == 1 else "mixed"


def store_predicate(scope: BoundScope, store: str) -> str:
    """The store predicate ``scope`` earns, for ``store``.

    A LITERAL, not a placeholder: the two branches need two DIFFERENT store
    values, the branches share one parameter mapping by contract (fixture
    decision 10, ``params`` returned unchanged), and one name cannot carry two
    values. The value is one of :data:`STORES`, checked by the caller, so it is
    never caller data reaching SQL as text.
    """
    if scope.predicate_form == "alias":
        return f"{scope.hydro_run_alias}.timeseries_store = '{store}'"
    qualifier = scope.fact_alias if scope.predicate_form == "exists" else scope.fact_table
    return (
        "EXISTS (SELECT 1 FROM hydro.hydro_run store_route "
        f"WHERE store_route.run_key = {qualifier}.run_key AND store_route.timeseries_store = '{store}')"
    )


def _bind_store(template: str, store: str, entry: str) -> tuple[tuple[str, ...], str]:
    """``template`` with one store predicate spliced into EVERY classified scope."""
    scopes = _classify_fact_reads(template, entry)
    bound = template
    # Descending, so an earlier splice cannot shift a later cut point.
    for scope in sorted(scopes, key=lambda scope: scope.chain_end, reverse=True):
        cut = scope.chain_end
        bound = f"{bound[:cut]}\n  AND {store_predicate(scope, store)}\n{bound[cut:]}"
    _assert_bound_per_scope(template, bound, store, entry, scopes)
    return tuple(scope.predicate_form for scope in scopes), bound


def _assert_bound_per_scope(
    template: str, bound: str, store: str, entry: str, expected: tuple[BoundScope, ...]
) -> None:
    """Re-derive the scopes FROM THE OUTPUT and check each governs exactly one predicate.

    A total count cannot tell "one predicate per scope" from "all of them in one
    scope and none in the others" — and the second is the shape of the fail-open
    this whole walk exists to prevent, so the post-condition has to be per scope
    (round-2 H5). Re-walking the bound text rather than trusting the cut offsets
    is what makes it a check instead of a restatement of the splice; the ANCHOR
    is what makes it independent of the walk's own idea of where the chain ended
    — the conjunct the predicate was supposed to follow has to be the conjunct it
    does follow, in the output.
    """
    literal = f"timeseries_store = '{store}'"
    emitted = bound.count(literal) - template.count(literal)
    if emitted != len(expected):
        raise RiverTemplateError(
            f"{entry}: bound {emitted} store predicate(s) for {len(expected)} fact-table reference(s)"
        )
    folded = _canonical(strip_comments(bound))
    for scope in expected:
        predicate = store_predicate(scope, store)
        if f"{scope.anchor} AND {_canonical(predicate)}" not in folded:
            raise RiverTemplateError(
                f"{entry}: the store predicate for the fact-table reference at offset {scope.reference_at} "
                f"did not land immediately after its chain's last conjunct {scope.anchor!r}"
            )
    scopes = _classify_fact_reads(bound, entry)
    if len(scopes) != len(expected):
        raise RiverTemplateError(
            f"{entry}: the bound text classifies {len(scopes)} fact-table reference(s), the template "
            f"classified {len(expected)}"
        )
    for scope in scopes:
        expected_kind = "on" if scope.reference_form == "JOIN" else "where"
        if scope.chain_kind != expected_kind:
            raise RiverTemplateError(
                f"{entry}: the {scope.reference_form} reference at offset {scope.reference_at} was bound in "
                f"a {scope.chain_kind.upper()} chain; a {scope.reference_form} reference binds in "
                f"{expected_kind.upper()}"
            )
        held = _scope_territory(bound, scope, scopes).count(literal)
        if held != 1:
            raise RiverTemplateError(
                f"{entry}: the {scope.chain_kind.upper()} chain governing the fact-table reference at offset "
                f"{scope.reference_at} holds {held} store predicate(s), expected exactly one"
            )


def _scope_territory(bound: str, scope: BoundScope, scopes: tuple[BoundScope, ...]) -> str:
    """``scope``'s own chain text, with any chain NESTED inside it cut out.

    A scalar sub-query that reads the fact table sits textually inside the outer
    ``WHERE`` chain and carries its own store predicate. Counting the outer chain
    raw would find two and call the correct output wrong; counting a
    bracket-masked chain would find zero for the correlated ``EXISTS`` form,
    whose literal lives inside its own brackets. Cutting out the nested chain's
    span is the form that says what is meant: this chain's own territory.
    """
    pieces: list[str] = []
    cursor = scope.chain_start
    nested = sorted(
        (
            other
            for other in scopes
            if other is not scope and scope.chain_start <= other.chain_start and other.chain_end <= scope.chain_end
        ),
        key=lambda other: other.chain_start,
    )
    for other in nested:
        if other.chain_start < cursor:
            continue
        pieces.append(bound[cursor : other.chain_start])
        cursor = other.chain_end
    pieces.append(bound[cursor : scope.chain_end])
    return "".join(pieces)


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
    union_safe: bool = True,
    union_unsafe_reason: str | None = None,
) -> RenderedUnion:
    """One ``UNION ALL`` branch per store, each bound to that store's runs.

    A fact-table read is bound by ``hydro_run.timeseries_store`` ONLY where the
    binder can PROVE the binding is semantics-preserving under ``UNION ALL``. The
    rule is a POSITIVE WHITELIST, not a list of refused spellings: a read binds
    iff ALL FIVE of these hold, and every shape the walk cannot classify is
    refused, naming the entry, the reference and the reason.

    1. **Statement kind** — ONE unterminated, unlocked, named-parameter READ.
       The comment/literal-blanked text carries no ``INSERT`` / ``UPDATE`` /
       ``DELETE`` / ``MERGE`` / ``CREATE`` / ``ALTER`` / ``DROP`` / ``TRUNCATE``
       / ``COPY`` / ``LOCK`` at any depth (``WITH … SELECT`` is a read; ``WITH …
       INSERT … RETURNING`` is not), no ``FOR UPDATE`` / ``FOR NO KEY UPDATE`` /
       ``FOR SHARE`` / ``FOR KEY SHARE`` (PostgreSQL allows a locking clause on
       neither a UNION result nor a UNION input), and no ``;`` (the branches are
       parenthesised, so a terminator inside one is a syntax error).
    2. **Context** — the reference's ancestor opener chain (the opener text of
       every enclosing bracket back to the statement root, each read back to the
       region stop that opens its clause) contains no outer join
       (``LEFT``/``RIGHT``/``FULL [OUTER] JOIN``, with or without ``LATERAL``, at
       any depth) and no negation (``NOT``, except ``NOT MATERIALIZED``), no
       enclosing segment is the right operand of ``EXCEPT`` / ``INTERSECT``, and
       no segment enclosing the reference — at any depth, not just its own —
       carries a ``RIGHT`` / ``FULL`` join, which preserves the OTHER relation.
    3. **Reference form** — ``FROM hydro.river_timeseries [AS] a`` bound into
       that segment's own ``WHERE``, or ``[INNER] JOIN hydro.river_timeseries
       [AS] a ON`` bound into THAT join's own ``ON``. No fallback across chains,
       and the independent name counter must agree with the walk's scope count.
    4. **Predicate form** — the governing chain is a conjunction (a top-level
       ``OR`` in it is refused: ``AND`` binds tighter, so the predicate would
       filter only the last operand), and it takes
       ``<h>.timeseries_store = '<store>'`` only where the scope both has a
       ``hydro_run`` alias ``<h>`` and correlates it ``<h>.run_key =
       <a>.run_key`` AS A TOP-LEVEL CONJUNCT of one of the scope's own chains;
       otherwise a correlated ``EXISTS`` on
       ``run_key``, qualified by the reference's own alias, or by the branch's
       physical table name where it has none.
    5. **Declared safety** — ``union_safe`` is True. Whether a statement's
       RESULT decomposes per store cannot be read off its text: it depends on
       what the consumer does with the rows. So it is DECLARED by the caller (the
       registry, in tests) and never inferred here.

    What conditions 1–4 do NOT check, stated because an over-claimed whitelist is
    worse than a narrow one: they are about WHERE a predicate may be bound, not
    about whether duplicating the whole statement is meaning-preserving. These
    shapes all satisfy 1–4 and are gated ONLY by condition 5 —

    * the fact table is not the DRIVING relation: a read in a SELECT-list scalar
      sub-query, or in a CTE that the outer query left-joins, binds correctly and
      then the statement-level union emits every driving row twice
      (``forecast_store:latest_product_fallback`` is exactly this, and it is
      declared ``union_safe=False``);
    * a set-operation level whose segments do not ALL read the fact table:
      ``SELECT … FROM hydro_run … UNION ALL SELECT … FROM fact …`` binds the fact
      segment correctly, and the union then duplicates the ``hydro_run`` segment,
      which no branch filtered. (``EXCEPT`` / ``INTERSECT`` right operands are
      refused by condition 2; plain ``UNION`` operands are bindable by contract,
      fixture Invariant Matrix condition 2, so this one is a declaration
      question. Measured: exactly one registered template has a set operator at
      all — ``mvt:postgis_tile_sql_hydro_national``'s ``selected_values`` CTE
      ``UNION ALL``s two SIBLING CTEs, so neither segment reads the fact table at
      that level, and the entry is declared unsafe for other reasons anyway.)
    * an ``EXISTS`` / ``IN`` probe that is NOT correlated on run identity: it is
      true in both stores, so both branches keep the driving row and the union
      emits it twice;
    * a top level that aggregates, de-duplicates, orders-and-limits, or is read
      one row at a time — the four reasons the register's seven declared-unsafe
      entries give.

    Statement-level ``UNION ALL`` is the only combinator this module ships. For a
    statement whose top level aggregates, de-duplicates, orders-and-limits or is
    consumed one row at a time, the correct union sits INSIDE a CTE — which needs
    a real caller to design against, so it is declared ``union_safe=False`` with
    a reason here and built by the wave-2 PR that has one (fixture decision 13).
    Note also that a branch's own ``ORDER BY`` orders only that branch: the union
    of two ordered branches is not ordered, and a caller that needs a total order
    applies it outside.

    Parameter names are SHARED across the branches: both placeholder dialects
    allow a name to repeat, so the caller's mapping goes through unchanged and
    every branch reads the same window. Per-branch renaming was rejected
    deliberately (orchestrator ruling on #1980 D1) — it would leak a
    ``<name>_<store>`` binding contract into every caller, for a per-branch
    window nothing asks for. A caller that genuinely needs two windows renders
    the branches itself. The STORE itself is a literal, not a placeholder, for
    the same reason inverted: the branches need two different values and one
    shared name cannot carry both.

    Raises:
        RiverTemplateError: on every shape outside the whitelist, always instead
            of returning SQL —

            * ``union_safe=False`` (checked FIRST, so the message carries the
              declared ``union_unsafe_reason``);
            * a positional (``%s``) template — the branches would silently
              require the caller's tuple twice, in an order that depends on which
              aids each branch dropped, and psycopg2 reports the arity mismatch
              only at execute time;
            * an empty ``stores``, a store outside :data:`STORES`, a template
              mixing ``%(name)s`` with ``:name``, a named parameter with no value
              in ``params``;
            * a data-modifying statement, a row-locking clause, or a ``;``
              (condition 1);
            * a fact read under an outer join, a negation, an
              ``EXCEPT``/``INTERSECT`` right operand, or a ``RIGHT``/``FULL``
              join in any segment anywhere in its ancestry (condition 2);
            * a reference form the walk does not model — comma join, ``FROM
              ONLY``, ``USING`` join, quoted identifier, the fact table as the
              preserved side of an outer join — a scope with no chain of its own,
              an empty chain, or a name-occurrence count disagreeing with the
              walk (condition 3);
            * a governing chain that is a top-level disjunction (condition 4);
            * a mis-shaped aid marker or a structural fault in either branch,
              from :func:`render_river_ts_sql` and
              :func:`assert_structurally_intact`;
            * a post-condition failure: a store predicate that did not land
              immediately after its chain's last conjunct, a chain holding other
              than exactly one, or a chain kind the reference form does not
              dictate.
    """
    if not union_safe:
        raise RiverTemplateError(
            f"{entry}: declared union_safe=False, so a statement-level UNION ALL of its two store branches "
            f"is not equivalent to the single-store statement -> "
            f"{union_unsafe_reason or 'no reason was declared, which is itself a contract violation'}"
        )
    if union_unsafe_reason is not None:
        raise RiverTemplateError(
            f"{entry}: union_safe=True was declared together with union_unsafe_reason "
            f"{union_unsafe_reason!r}; the reason exists only to explain a refusal"
        )
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
    _refuse_uncombinable_statement(template, entry)
    branches: list[str] = []
    forms: list[tuple[str, ...]] = []
    for store in stores:
        label = f"{entry} [{store}]"
        rendered = render_river_ts_sql(template, store, entry=label)
        branch_forms, with_store = _bind_store(rendered.sql, store, label)
        references = fact_table_attribution(with_store).reference_count
        if len(branch_forms) != references:
            raise RiverTemplateError(
                f"{label}: bound {len(branch_forms)} store predicate(s) but the branch reads the fact table "
                f"{references} time(s) — an unbound read would scan both stores"
            )
        assert_structurally_intact(with_store, label, allow_markers=store == "legacy")
        forms.append(branch_forms)
        branches.append(with_store)
    sql = "\nUNION ALL\n".join(f"(\n{branch}\n)" for branch in branches)
    return RenderedUnion(sql, dict(params), tuple(forms), tuple(branches))
