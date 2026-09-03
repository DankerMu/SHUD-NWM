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
  breakages a line deletion can cause, enumerated in
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
  deletion artefacts do not reach a database.
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

#: The ``--`` comment as a REGEX, for the one place that has to strip a comment
#: out of already-collected text instead of scanning forward
#: (:func:`_in_comparison_value_position`). Same rule as
#: :func:`_scan_line_comment`, spelled the same way on purpose: PostgreSQL's
#: ``non_newline`` is ``[^\n\r]`` (§4.1.5, fixture decision 17), and with
#: ``[^\n]*`` this sub ate the ``\r`` AND the comparison operator behind it, so
#: an authority sub-select stopped reading as comparison-position and its
#: ``WHERE run_id = …`` was attributed to the fact table (#2018 round-3 G1's
#: line-comment row, second site).
_LINE_COMMENT = re.compile(r"--[^\n\r]*")

#: Where a ``--`` comment ends, as PostgreSQL's ``non_newline`` defines it.
_NEWLINE = re.compile(r"[\n\r]")

_WHITESPACE = re.compile(r"\s+")

#: An identifier character in PostgreSQL: ``ident_cont`` is letters (including
#: non-ASCII ones, hence ``\w`` where a regex is used), digits, ``_`` and ``$``
#: (§4.1.1; the ``$`` is a documented non-standard extension). ONE notion,
#: because two lexical rules depend on it — where a dollar quote may open and
#: where an ``E'…'`` prefix may start — and they must not drift apart.
_IDENTIFIER_TAIL = "_$"


def _opens_escape_string(sql: str, start: int) -> bool:
    """Whether the single quote at ``start`` opens a PostgreSQL ``E'…'`` literal.

    The ``E`` has to be a token of its own: in ``note E'x'`` it is the prefix, in
    an identifier that merely ends in ``e`` (a pasted ``value'x'``, or ``tablE'x'``)
    it is not, and reading the plain literal as an escape string would swallow a
    doubled quote the standard form uses as its escape.

    ``$`` counts as an identifier character here for the same reason it does in
    :data:`_DOLLAR_QUOTE_OPEN`: in ``x$e'C:\\'`` the ``e`` is the tail of the
    identifier ``x$e``, and reading it as a prefix makes the literal
    backslash-aware, its closing quote escaped, and the phantom literal runs over
    the rest of the statement (#2018 round-3, decision 17's identifier row).
    """
    if start == 0 or sql[start - 1] not in "Ee":
        return False
    return start < 2 or not (sql[start - 2].isalnum() or sql[start - 2] in _IDENTIFIER_TAIL)


def _scan_quoted(sql: str, start: int, quote: str) -> int:
    r"""Index just past the quoted run beginning at ``start`` (doubled quote escapes).

    A single-quoted run carrying the ``E'…'`` prefix additionally honours
    BACKSLASH escapes, so ``\'`` and ``\\`` inside it are data and not the end of
    the literal. Without that, ``E'a\'b' AS note FROM hydro.river_timeseries rt``
    ends its "literal" at the middle quote and opens a second one that runs to
    the end of the statement — which blanked the statement's own ``FROM`` clause
    and made :func:`fact_table_attribution` report a read of the fact table as no
    read at all, so the narrow render shipped its text predicates and the legacy
    render skipped the rename (review #2018, B/P2-2).

    Taught HERE rather than in one caller because every traversal in this module
    locates faults, references and chain ends as offsets into the same text: two
    scanners with two ideas of where a literal ends is exactly the disagreement
    :func:`non_code_spans` exists to prevent.
    """
    return _scan_quoted_span(sql, start, quote)[0]


def _scan_quoted_span(sql: str, start: int, quote: str) -> tuple[int, bool]:
    """:func:`_scan_quoted`'s answer plus whether the closing quote was FOUND.

    The flag is what separates ``'q_down'`` at the very end of a template — a
    complete literal whose span happens to stop at ``len(sql)`` — from ``'oops``,
    which the scanner never closed. Only the second one means the blanked text
    every counter reads is not the statement's own code (decision 17's
    unterminated row), and refusing on the offset alone would refuse ordinary
    templates.

    The recorded assumption underneath the backslash arm (decision 17's
    string-constant row): ``standard_conforming_strings = on``, PostgreSQL's
    default since 9.1, which is what makes a backslash DATA in a plain ``'…'``
    and an escape only in an ``E'…'`` one. With it off, every literal would be
    escape-aware; this module cannot see a session setting, so it models the
    default and pins both halves (``'C:\\'`` plain, ``E'a\\'b'`` escaped).
    """
    escaped = quote == "'" and _opens_escape_string(sql, start)
    index = start + 1
    length = len(sql)
    while index < length:
        if escaped and sql[index] == "\\":
            index += 2
            continue
        if sql[index] == quote:
            if index + 1 < length and sql[index + 1] == quote:
                index += 2
                continue
            return index + 1, True
        index += 1
    return length, False


def _scan_block_comment(sql: str, start: int) -> int:
    return _scan_block_comment_span(sql, start)[0]


def _scan_block_comment_span(sql: str, start: int) -> tuple[int, bool]:
    """Index just past the block comment at ``start``, DEPTH-COUNTED, plus termination.

    PostgreSQL NESTS block comments (§4.1.5): a ``/*`` inside one begins a nested
    comment that must be closed before the outer one ends, so ``/* a /* b */ c */``
    is ONE comment. Ending at the first ``*/`` instead re-tokenises the tail as
    code, and an apostrophe in that tail (``don't``) opens a phantom literal that
    ran over the statement's own ``FROM`` clause — invisible to the counter-vs-walk
    equality guard, because the guard's two sides read this same blanked text and
    both answered 0 (#2018 round-3 G1, all three lanes; retro-2018.md).

    Unterminated still means "to the end of the text", as before; the flag says so
    rather than the caller inferring it from the offset.
    """
    depth = 0
    index = start
    length = len(sql)
    while index < length:
        if sql.startswith("/*", index):
            depth += 1
            index += 2
            continue
        if sql.startswith("*/", index):
            depth -= 1
            index += 2
            if depth == 0:
                return index, True
            continue
        index += 1
    return length, False


def _scan_line_comment(sql: str, start: int) -> int:
    """Index of the end of the ``--`` comment at ``start``.

    A ``--`` comment runs to the end of the LINE, and PostgreSQL's ``non_newline``
    is ``[^\\n\\r]`` (§4.1.5), so a ``\\r`` ends one just as a ``\\n`` does. Ending
    it only at ``\\n`` let a ``\\r``-terminated comment swallow the rest of a
    statement — its ``FROM`` clause and its text predicates with it (#2018 round-3
    G1, decision 17's line-comment row). The end of the INPUT terminates a line
    comment too, which is why this scanner has no "unterminated" answer.
    """
    match = _NEWLINE.search(sql, start)
    return len(sql) if match is None else match.start()


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

    Deliberately NOT guarded by :func:`_assert_modelled_reference_forms`, and
    therefore NOT the answer to "does this statement predicate on the fact
    table's text identity" — that question is
    :func:`fact_table_text_identity_columns`, which refuses an unmodelled
    reference form instead of answering it. This helper answers about the ONE
    alias its caller names, so it claims nothing about how many reads the
    statement performs and has nothing to be blind about; its callers are the
    shape oracles, which pass a known alias per surface and bare WHERE-chain
    fragments that name no table at all (a guard here would refuse those and
    would need an ``entry`` they do not have). Recorded WITH a pin rather than
    left as a reading of the code: ``tests/test_river_ts_render.py`` asserts
    that this helper answers for an unmodelled reference form while
    :func:`fact_table_text_identity_columns` refuses the same statement.
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
#
# `AS` is consumed by the optional non-capturing group AND listed in the
# keyword lookahead, so it can never be captured as the alias itself. Without
# the lookahead arm the regex backtracked on `FROM hydro.river_timeseries AS
# "r"` — the quoted alias fails `[A-Za-z_]`, the optional `AS\s+` gives up, and
# `AS` is then matched as the alias, which attributes every `"r".<column>`
# predicate to a table called `AS` and silently reports no text identity at all.
_FACT_REFERENCE = re.compile(
    r"\b(?:FROM|JOIN)\s+hydro\.river_timeseries(?:_legacy)?\b"
    r"(?:\s+(?:AS\s+)?(?!(?:AS|WHERE|ON|JOIN|CROSS|LEFT|RIGHT|INNER|FULL|OUTER|NATURAL|GROUP|ORDER|LIMIT|HAVING"
    r"|UNION|EXCEPT|INTERSECT|WINDOW|OFFSET|FETCH|USING|SET|RETURNING|VALUES|SELECT|WITH|AND|OR)\b)"
    r"([A-Za-z_][A-Za-z0-9_]*))?",
    re.IGNORECASE,
)

#: A fact-table reference whose alias is a QUOTED identifier (`AS "r"` or bare
#: `"r"`). :data:`_FACT_REFERENCE` models bare identifiers only, so this form
#: reaches the alias walk as "no alias at all" and the unaliased fallback then
#: misses every `"r".<column>` predicate — `(?<![.\w])run_id` does not fire on
#: `"r".run_id`. The answer would be an empty set, i.e. "this statement has no
#: text identity on the fact table", for a statement that plainly does. So the
#: form is REFUSED naming the entry instead: unmodelled and silently clean are
#: not the same answer.
#:
#: Both gaps are `\s*`, not `\s+`: a double quote needs no whitespace in front of
#: it to start an identifier, so `hydro.river_timeseries"r"` and
#: `hydro.river_timeseries AS"r"` are the same unmodelled form as their spaced
#: spellings and used to walk straight past a `\s+` guard (review #2018, B/P2-1).
#: The accepted consequence is that `FROM "hydro.river_timeseries" r` — a quoted
#: identifier for a table literally named `hydro.river_timeseries` in the default
#: schema, which is not this fact table — is refused too. That form is equally
#: unmodelled by the alias walk (it silently attributed nothing), so refusing it
#: is the fail-closed answer rather than a regression.
_FACT_QUOTED_ALIAS = re.compile(
    r"\bhydro\.river_timeseries(?:_legacy)?\s*(?:AS\s*)?\"",
    re.IGNORECASE,
)

#: PostgreSQL's Unicode-escape prefix, on an identifier (``U&"riv\0065r…"``) or a
#: string (``U&'…'``). The ONE spelling that defeats a name counter rather than
#: merely dodging one of its alternatives: the identifier it denotes need not
#: contain the name at all, so widening the counter cannot reach it and both
#: sides read zero (review #2018 round-2, E/P2-2). Refused by its prefix, which
#: is the only part of it that is always literal.
_UNICODE_ESCAPED = re.compile(r"\bU&", re.IGNORECASE)


def _assert_modelled_reference_forms(sql: str, entry: str) -> None:
    """Refuse a fact-table reference form the alias walk does not model.

    The guarantee, in one sentence (fixture decision 16): no statement reaches a
    render or a text-identity answer unless the independent occurrence counter
    and the ``FROM`` / ``JOIN`` walk AGREE about how many times it reads the fact
    table, the counter is blind to no spelling of the table's name, and no read
    hides where the text-identity scan cannot look. Four checks, in that order:

    #. an UNTERMINATED literal, comment or dollar-quoted body — the statement
       ends inside a span the scanner never closed, so the blanked text every
       later check reads is not this statement's code. A BELT and nothing more
       (fixture decision 17): verifier #2018 round-3 G measured that it catches
       ONLY the unterminated sub-case, because a phantom literal can CLOSE on a
       later literal that contributes an odd number of quote characters —
       ``E'q\\'x'``, pinned as the re-synchronisation case — and so ends well
       before ``len(sql)`` with the read still blanked. That case is answered by
       :func:`non_code_spans` agreeing with PostgreSQL's lexer, never here;
    #. a Unicode-escaped identifier or literal (``U&"…"``) anywhere in the code —
       the one syntax that can name the table with no occurrence of its NAME, so
       the counter reads 0, the walk reads 0 and the equality below is satisfied
       by mutual blindness. Refused wholesale rather than decoded; the accepted
       over-refusal is a ``U&'…'`` string literal, which no registered template
       has;
    #. a double-quoted ALIAS, whose predicates the walk would attribute to the
       wrong table or to none;
    #. the counts themselves — the permissive name counter against the strict
       walk, and the whole statement against the statement with its
       comparison-position sub-selects removed, because a fact read inside one of
       those is stripped by :func:`outer_predicates` before any column is
       attributed and is therefore invisible to the narrow check (review #2018
       round-2, F4). The sub-select case is REFUSED rather than scanned:
       extending the scan into comparison-position sub-selects false-refuses the
       registered statements whose authority resolution lives there.

    Run over the comment/literal-blanked text so a quoted alias SPELLED inside a
    literal or a comment is data, not a refusal. Double-quoted spans survive that
    blanking on purpose (see :func:`non_code_spans`): in PostgreSQL they are
    identifiers, which is exactly what this is about.

    Every message names the entry and never the table: the statement census in
    ``tests/test_river_ts_text_identity_cleanup.py`` counts the table name in
    this module's string constants, and a refusal message that spelled it would
    add a phantom "read site" to that count.
    """
    unterminated = _unterminated_span(sql)
    if unterminated is not None:
        raise RiverTemplateError(
            f"{entry}: unmodelled fact-table reference form — the statement ends inside an unterminated "
            f"literal or comment opened at offset {unterminated[0]}, so the blanked text every counter and "
            "guard reads is not this statement's code; terminate it"
        )
    blanked = _blank_comments_and_literals(sql)
    if _UNICODE_ESCAPED.search(blanked) is not None:
        raise RiverTemplateError(
            f"{entry}: unmodelled fact-table reference form — a Unicode-escaped identifier or literal "
            "(U&) is not modelled: it can name the fact table with no occurrence of the table's name in "
            "the text, so neither the occurrence counter nor the FROM/JOIN walk can see the read; spell "
            "identifiers literally"
        )
    match = _FACT_QUOTED_ALIAS.search(blanked)
    if match is not None:
        raise RiverTemplateError(
            f"{entry}: unmodelled fact-table reference form {match.group(0).strip()!r} — a double-quoted "
            "alias is not modelled by the alias walk, so this statement's text-identity columns cannot be "
            "attributed to the fact table; alias the table with a bare identifier"
        )
    occurrences = fact_table_name_occurrences(sql)
    modelled = fact_table_attribution(sql).reference_count
    if occurrences != modelled:
        raise RiverTemplateError(
            f"{entry}: unmodelled fact-table reference form — the statement names the fact table "
            f"{occurrences} time(s) but the FROM/JOIN walk models {modelled} reference(s), so at least one "
            "read is spelled in a form whose alias (and therefore whose text-identity predicates) cannot be "
            "attributed; write each read as a plain FROM/JOIN of the fact table with a bare alias"
        )
    outer_occurrences = fact_table_name_occurrences(strip_scalar_subqueries(sql))
    if occurrences != outer_occurrences:
        raise RiverTemplateError(
            f"{entry}: unmodelled fact-table reference form — the statement names the fact table "
            f"{occurrences} time(s) but only {outer_occurrences} outside its comparison-position "
            "sub-select(s), and a read inside one of those is stripped before any column is attributed, "
            "so its text-identity predicates are never seen; resolve identity through the authority "
            "table in that position instead"
        )


@dataclass(frozen=True)
class FactTableAttribution:
    """How a statement names the river fact table."""

    aliases: frozenset[str]
    has_unaliased_reference: bool
    reference_count: int


def fact_table_attribution(sql: str) -> FactTableAttribution:
    """The aliases (and bare references) the statement gives the river fact table.

    Read off the comment/literal-blanked text, like its two sibling counters:
    ``strip_comments`` alone leaves string bodies intact, so a literal spelling
    ``'... FROM hydro.river_timeseries'`` added a phantom reference — inflating
    ``reference_count`` (which :func:`fact_table_name_occurrences` is supposed to
    be able to disagree with) and, when the literal named no alias, flipping
    ``has_unaliased_reference`` to True and arming the unqualified-column
    fallback in :func:`fact_table_text_identity_columns`.
    """
    aliases: set[str] = set()
    unaliased = False
    count = 0
    for match in _FACT_REFERENCE.finditer(_blank_comments_and_literals(sql)):
        count += 1
        alias = match.group(1)
        if alias is None:
            unaliased = True
        else:
            aliases.add(alias)
    return FactTableAttribution(frozenset(aliases), unaliased, count)


#: The fact table's bare NAME as a whole token, in any case — the independent
#: counter's whole vocabulary. Deliberately ignorant of ``FROM`` / ``JOIN`` /
#: aliases AND of schemas, quoting and whitespace: its job is to disagree with
#: the structural walk whenever the statement names the table in a form the walk
#: does not model, and it can only do that job for spellings it can SEE. Every
#: qualified spelling — ``hydro.x``, ``"hydro"."x"``, ``hydro . x``,
#: ``hydro./*c*/x``, ``otherhydro.x``, or no schema at all — ends in the same
#: token, so the counter needs no model of the ways a name can be qualified
#: (review #2018 round-2, E/P2-2 and lane-1 F1; fixture decision 16).
#:
#: A name used as a COLUMN QUALIFIER (``hydro.river_timeseries.variable``, the
#: bare ``river_timeseries.variable``, ``hydro."river_timeseries"."run_key"``) is
#: counted like any other mention. It is not a second READ — but the walk does
#: not model it either, so counting it makes the two disagree and the statement
#: is refused: qualify columns through a bare alias. The ``(?!"?\s*\.)``
#: lookahead that used to exclude the form was deleted in #2018 round 3 (G2),
#: because excluding it left a "no opinion" zone the module answered WRONGLY —
#: on an unaliased read neither arm of :func:`fact_table_text_identity_columns`
#: can see a table-name qualifier (the alias set is empty, and the unqualified
#: arm's ``(?<![.\w])`` lookbehind is defeated by the dot), so the narrow render
#: shipped ``variable``; and the bare spelling additionally rendered a legacy
#: statement whose qualifier :func:`_rename_table` cannot follow, i.e. a
#: reference to a ``FROM`` entry that no longer exists. Deletion rather than a
#: detection arm: it adds no pattern and no new over-match record (decision 17,
#: verifier round-3 G, 0/20 registry entries affected).
#:
#: Not a token, and deliberately so: ``river_timeseries_valid_time_idx`` and
#: every other identifier that merely starts with the name, because ``\b`` does
#: not fire in the middle of a word.
_FACT_NAME_TOKEN = re.compile(r"\briver_timeseries(?:_legacy)?\b", re.IGNORECASE)


#: Where a dollar-quoted literal may OPEN (§4.1.2.4, fixture decision 17).
#:
#: The leading ``(?<![\w$])`` is load-bearing: in PostgreSQL a ``$`` after the
#: first character of an identifier belongs to the identifier (a documented
#: non-standard extension), so ``a$b$c`` is ONE identifier and a delimiter cannot
#: begin in the middle of it. Without the guard ``$b$`` opened a quote whose tag
#: never recurred, everything to the end of the text was blanked, and the read it
#: covered was invisible to BOTH counters (#2018 round-3 G1). ``\w`` rather than
#: ``[A-Za-z0-9_]`` because PostgreSQL's ``ident_cont`` includes non-ASCII
#: letters.
#:
#: The tag must start with a letter or ``_``, which is what keeps a positional
#: parameter (``$1``) code.
_DOLLAR_QUOTE_OPEN = re.compile(r"(?<![\w$])\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")


def _scan_dollar_quoted(sql: str, start: int, tag: str) -> int:
    return _scan_dollar_quoted_span(sql, start, tag)[0]


def _scan_dollar_quoted_span(sql: str, start: int, tag: str) -> tuple[int, bool]:
    """Index just past the dollar-quoted body opening with ``tag`` at ``start``."""
    end = sql.find(tag, start + len(tag))
    return (len(sql), False) if end == -1 else (end + len(tag), True)


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
    ``"comment"``); single-quoted literals with doubled-quote escapes traversed
    (and backslash escapes too where the literal carries the ``E'…'`` prefix, see
    :func:`_scan_quoted`), and dollar-quoted bodies ``$$ … $$`` / ``$tag$ … $tag$``
    (kind ``"literal"``). An ``E`` prefix stays OUTSIDE the span: the span's own
    first and last characters have to be the quotes, because
    :func:`_blank_non_code` keeps them when it blanks a literal's body.

    NOT covered, deliberately: double-quoted text. In PostgreSQL that is a
    quoted IDENTIFIER, so ``"hydro"."river_timeseries"`` is a read of the fact
    table and must be counted, while ``'hydro.river_timeseries'`` is data and
    must not.

    Audited rule by rule against PostgreSQL §4.1 (fixture decision 17) after
    #2018 round 3: this scanner is COMMON-MODE — the occurrence counter and the
    ``FROM`` / ``JOIN`` walk read the text it blanks, so a divergence from
    PostgreSQL's lexer blanks real code for both of them and the equality guard
    is satisfied by mutual blindness. Every divergence is therefore either fixed
    here (nested comments, ``$`` inside an identifier, ``\\r`` ending a line
    comment) or REFUSED (``U&``, an unterminated span), and each carries a pin —
    never a "cannot happen" argument.
    """
    return tuple((start, stop, kind) for start, stop, kind, _closed in _scan_non_code(sql))


def _scan_non_code(sql: str) -> tuple[tuple[int, int, str, bool], ...]:
    """:func:`non_code_spans` with a fourth field: did the scanner FIND the close?

    Kept private and separate so the public span tuple stays three-wide (every
    span pin in the suite compares it literally) while the ONE traversal still
    answers both questions. A second traversal that re-derived "was this
    terminated" is precisely the two-scanners-disagree failure this function
    exists to prevent.
    """
    spans: list[tuple[int, int, str, bool]] = []
    length = len(sql)
    index = 0
    while index < length:
        character = sql[index]
        if character == '"':
            index = _scan_quoted(sql, index, character)
            continue
        if character == "'":
            (end, closed), kind = _scan_quoted_span(sql, index, character), _NON_CODE_LITERAL
        elif sql.startswith("--", index):
            # A line comment is ended by the end of the input as legitimately as
            # by a newline, so it is never "unterminated".
            (end, closed), kind = (_scan_line_comment(sql, index), True), _NON_CODE_COMMENT
        elif sql.startswith("/*", index):
            (end, closed), kind = _scan_block_comment_span(sql, index), _NON_CODE_COMMENT
        elif character == "$" and (opener := _DOLLAR_QUOTE_OPEN.match(sql, index)) is not None:
            (end, closed), kind = _scan_dollar_quoted_span(sql, index, opener.group(0)), _NON_CODE_LITERAL
        else:
            index += 1
            continue
        spans.append((index, end, kind, closed))
        index = end
    return tuple(spans)


def _unterminated_span(sql: str) -> tuple[int, int, str] | None:
    """The span the statement ENDS inside because the scanner never found its close.

    Not "the last span stops at ``len(sql)``": a literal whose closing quote is
    the template's last character (``WHERE rt.variable = 'q_down'``) and a
    trailing ``--`` comment both satisfy that and are perfectly terminated, so
    the offset spelling of this check would refuse ordinary templates.
    """
    spans = _scan_non_code(sql)
    if not spans:
        return None
    start, stop, kind, closed = spans[-1]
    return None if closed else (start, stop, kind)


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

    ``\\r`` is preserved beside ``\\n``: PostgreSQL ends a line at either
    (decision 17), so the blanked text keeps the template's line structure
    whichever spelling it uses. Symmetry with the ``\\n`` arm — not a claim about
    a failure that was observed, which is why it carries no pin of its own.
    """
    blanked = list(sql)
    for start, stop, kind in non_code_spans(sql):
        keep = keep_literal_quotes and kind == _NON_CODE_LITERAL
        inner_start = start + 1 if keep else start
        inner_stop = stop - 1 if keep else stop
        for position in range(inner_start, max(inner_start, inner_stop)):
            if sql[position] not in "\n\r":
                blanked[position] = " "
    return "".join(blanked)


def _blank_comments_and_literals(sql: str) -> str:
    """``sql`` with comments and string bodies blanked, offsets preserved.

    The coordinate system every structural decision in this module is made in:
    a fault, a fact reference and a chain end are all located as offsets into
    text of the SAME length as the template, so nothing can slide out of
    alignment (review #1996, C3).
    """
    return _blank_non_code(sql)


def fact_table_name_occurrences(sql: str) -> int:
    """How many times the statement NAMES the fact table, counted independently.

    Derived from the bare name alone, with no model of ``FROM`` / ``JOIN`` /
    segments, precisely so that it can disagree with
    :func:`fact_table_attribution`. When it does, the statement spells a read in a
    form the ``FROM`` / ``JOIN`` walk does not model — a comma join (``FROM fact
    a, fact b``), ``FROM ONLY``, a quoted identifier, a schema written with a
    space or a comment around its dot, a different schema, or no schema at all —
    and a caller that trusted the walk alone would be reasoning about fewer reads
    than the statement performs (round-2 H3). Since review #2018 (C/P2-1)
    :func:`_assert_modelled_reference_forms` REFUSES on the disagreement, so every
    render and every text-identity answer is taken over a statement both counters
    agree about.

    **Counter permissive, walk strict, disagreement refuses.** The counter needs
    no model of schemas, quoting, case or whitespace because it counts the
    table's bare NAME as a whole token, so no SPELLING of the name escapes it
    except PostgreSQL's Unicode-escape syntax, which is refused wholesale in
    :func:`_assert_modelled_reference_forms`. That is a closure over spellings
    and over nothing else. It holds GIVEN a scanner that agrees with
    PostgreSQL's lexer (§4.1, fixture decision 17): the counter and the walk
    both read the text :func:`non_code_spans` blanks, so a divergence there is
    COMMON-MODE — it blanks real code for both sides, both answer 0, and the
    equality guard is satisfied by mutual blindness (three rounds of #2018 hit
    this one class). The scanner's divergences are therefore ENUMERATED in
    decision 17 and each one is pinned; none of them is reasoned about here.
    Widening is counter-side only: teaching the WALK a spelling makes it
    "modelled" while the rename and the alias attribution still need the
    canonical literal, which is a new fail-open (fixture decision 16).

    The permissiveness is paid for in refusals, all fail-closed and all of forms
    the registry does not use: ``otherhydro.river_timeseries x`` (a different
    schema), ``FROM river_timeseries rt`` (the search_path spelling, whose
    identity depends on a session setting this module cannot see),
    ``hydro."River_Timeseries"`` (a quoted upper-case identifier, which in
    PostgreSQL is a DIFFERENT table), a column qualified by the table name
    (``hydro.river_timeseries.variable`` — alias the table instead), a CTE named
    ``river_timeseries`` and a column alias ``AS river_timeseries`` /
    ``AS "river_timeseries"`` (both counted twice against a walk that models one
    read or none). All are counted, disagree with the walk, and are refused as
    unmodelled rather than rendered.
    """
    return len(_FACT_NAME_TOKEN.findall(_blank_comments_and_literals(sql)))


def fact_table_text_identity_columns(sql: str, *, entry: str = "<template>") -> set[str]:
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

    Raises :class:`RiverTemplateError`, naming ``entry``, on a reference form the
    alias walk does not model rather than returning the empty set that form would
    otherwise produce.
    """
    _assert_modelled_reference_forms(sql, entry)
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
#: clause, not a predicate: a chain read as continuing into it produces ``FOR
#: UPDATE AND …``, which is a syntax error at execute time that nothing textual
#: noticed (round-3 L1-3). Listing it here gives the chain end AND the "dangling
#: connective before a keyword" fault the same knowledge in one place.
_KEYWORD_FAMILY = (
    r"(?:(?:LEFT|RIGHT|FULL)(?:\s+OUTER)?\s+JOIN"
    r"|FOR\s+(?:NO\s+KEY\s+)?UPDATE|FOR\s+(?:KEY\s+)?SHARE"
    r"|GROUP|ORDER|LIMIT|HAVING|WINDOW|UNION|EXCEPT|INTERSECT|RETURNING|OFFSET|FETCH"
    r"|JOIN|INNER|CROSS|NATURAL|WHERE|ON|FROM|SELECT|WITH|VALUES|SET)"
)

_REGION_STOP = re.compile(rf"\b{_KEYWORD_FAMILY}\b", re.IGNORECASE)

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

# `re.IGNORECASE` because an unquoted SQL identifier is case-insensitive: with
# this the module has ONE case policy for the fact table's name, shared by the
# counter, the FROM/JOIN walk, the quoted-alias guard and this rename. Without
# it, `FROM HYDRO.RIVER_TIMESERIES` passed both counters and the equality guard
# and then rendered "legacy" naming the CANONICAL table — the narrow one, which
# holds none of the legacy rows (review #2018 round-2, F2).
_CANONICAL_NAME = re.compile(rf"{re.escape(RIVER_TABLE)}\b", re.IGNORECASE)
_POSITIONAL_PLACEHOLDER = re.compile(r"%s")


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
    found = fact_table_text_identity_columns(sql, entry=entry)
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
    _assert_modelled_reference_forms(template, entry)
    if store == "legacy":
        sql = _rename_table(template, "legacy")
        assert_structurally_intact(sql, entry, allow_markers=True)
        return RenderedSql(sql)
    sql, removed_placeholders, removed_aids = _strip_aids(template, entry)
    assert_structurally_intact(sql, entry)
    _assert_no_fact_text_identity(sql, entry)
    _assert_key_predicates_retained(template, sql, removed_aids, entry)
    return RenderedSql(sql, removed_placeholders, removed_aids)
