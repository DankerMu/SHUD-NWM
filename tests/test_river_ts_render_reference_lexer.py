"""Differential: ``non_code_spans`` against an INDEPENDENT PostgreSQL §4.1 lexer (#1980, decision 18).

Four review rounds of #2018 found the same class four times: the module's
hand-rolled lexer disagrees with PostgreSQL at one rule, the disagreement blanks
real code, and because the occurrence counter and the ``FROM`` / ``JOIN`` walk
read that SAME blanked text the counter-vs-walk equality guard is satisfied by
mutual blindness (0 == 0). Every round answered by fixing the rule the reviewer
had found; round 4 found the rule that round 3 fixed still wrong at a second
site, and the strongest evidence of round 4 — a reviewer's own §4.1 reference
lexer, fuzzed 40 000 samples, which localised the divergence to the dollar-quote
tag class — lived in reviewer scratch and never entered the tree
(retro-2018-2.md, "Missing regression evidence").

This module is that evidence, committed and bounded. The reference lexer below is
written from PostgreSQL's §4.1 and ``scan.l`` directly and shares NOTHING with
the module under test — no import of any ``_scan_*`` symbol, no reuse of its
patterns — so an error copied from the module cannot hide in it. What is asserted
is the disjunction decision 18 actually promises:

    for every sample, the module either REFUSES it as outside the declared
    lexical subset, or lexes it span-for-span like PostgreSQL.

Two things keep that from being satisfied vacuously by refusing everything: an
explicit floor on the fraction of samples the module accepts, and a per-fragment
check that every construct in the alphabet appears in at least one ACCEPTED
sample. The registry sibling then asserts the whole thing over the twenty real
templates, all of which must be accepted.

Round-5 reviewers: attack the REFERENCE. If it and the module agree, one of them
is still allowed to be wrong about PostgreSQL — but they no longer fail the same
way by construction, which is what three rounds of common-mode findings cost.
"""

from __future__ import annotations

import random
import re
from typing import NamedTuple

import pytest

from packages.common.river_ts_render import (
    RiverTemplateError,
    _lexical_subset_violation,
    non_code_spans,
    render_river_ts_sql,
)
from tests.river_ts_template_registry import REGISTRY

# ---------------------------------------------------------------------------
# The reference lexer: PostgreSQL §4.1, written from the specification
# ---------------------------------------------------------------------------

#: ``scan.l``'s ``ident_start`` as a BYTE class: ``[A-Za-z\200-\377_]``. Under
#: UTF-8 every non-ASCII codepoint is made of bytes in ``\200-\377``, so the
#: class admits them all — letters, ideographs and symbols alike. Spelled here as
#: the codepoint test that byte class implies, which is the reading round-4 I1
#: turned on (``$备注$`` and ``$€$`` are both real dollar-quote tags).
def _is_ident_start(character: str) -> bool:
    return character.isascii() and (character.isalpha() or character == "_") or not character.isascii()


def _is_ident_cont(character: str) -> bool:
    """``ident_cont``: ``ident_start`` plus digits and ``$`` (the documented extension)."""
    return _is_ident_start(character) or character.isdigit() or character == "$"


class _ReferenceToken(NamedTuple):
    """One identifier or numeric token emitted by the single reference scanner pass."""

    kind: str
    start: int
    stop: int
    text: str


class _ReferenceLexResult(NamedTuple):
    """Literal-comparable product of one ``_reference_lex`` pass.

    Final non-code spans are a projection; identifier and numeric events are the
    token boundaries the scanner actually consumed. A mutant that skips the
    numeric helper on some rows can keep the same spans and still disagree here.
    """

    spans: tuple[tuple[int, int, str], ...]
    identifiers: tuple[_ReferenceToken, ...]
    numerics: tuple[_ReferenceToken, ...]


def _ident(start: int, stop: int, text: str) -> _ReferenceToken:
    return _ReferenceToken("identifier", start, stop, text)


def _num(start: int, stop: int, text: str) -> _ReferenceToken:
    return _ReferenceToken("numeric", start, stop, text)


#: ``dolq_start [A-Za-z\200-\377_]`` / ``dolq_cont [A-Za-z\200-\377_0-9]`` — the
#: tag of a dollar-quoted string follows the unquoted-identifier rules except
#: that it cannot contain a ``$`` (§4.1.2.4). An EMPTY tag (``$$``) is legal.
_DOLLAR_QUOTE_TAG = re.compile(r"\$(?:(?:[A-Za-z_]|[^\x00-\x7f])(?:[A-Za-z0-9_]|[^\x00-\x7f])*)?\$")

_REFERENCE_NEWLINE = re.compile(r"[\n\r]")


def _consume_reference_numeric_token(sql: str, start: int) -> int | None:
    """Half-open stop of the PostgreSQL numeric token opening at ``start``, or ``None``.

    Written from §4.1.2.1, not from production ``_consume_numeric_token``: a
    digit run or a leading-dot decimal, optional trailing-dot, then an optional
    exponent ``e`` / ``E`` with optional sign and a required digit run. An
    incomplete exponent (``1e``, ``1e+``, ``1EE``) is left unconsumed so the
    following character remains its own token. Direct stop-offset pins and the
    scanner-result pins that consume the same literal rows live on
    ``_REFERENCE_LEX_CASES``.
    """
    length = len(sql)
    if start >= length:
        return None

    def digit_run(index: int) -> int:
        while index < length and sql[index].isdigit():
            index += 1
        return index

    if sql[start] == ".":
        stop = digit_run(start + 1)
        if stop == start + 1:
            return None
    elif sql[start].isdigit():
        stop = digit_run(start)
        if stop < length and sql[stop] == ".":
            stop = digit_run(stop + 1)
    else:
        return None

    if stop < length and sql[stop] in "Ee":
        exponent = stop + 1
        if exponent < length and sql[exponent] in "+-":
            exponent += 1
        digits = digit_run(exponent)
        if digits > exponent:
            stop = digits
    return stop


def _reference_lex(sql: str) -> _ReferenceLexResult:
    r"""One scanner pass: non-code spans plus the identifier and numeric tokens it consumed.

    Comments (``--`` to ``non_newline``, NESTED ``/* */``), string constants
    (``'…'`` with ``''`` escapes; ``E'…'``/``e'…'`` additionally with backslash
    escapes) and dollar-quoted bodies. Quoted identifiers (``"…"``) are code and
    get no span — but they are traversed, because an apostrophe inside one is
    data.

    Deliberately structured as a token loop with an explicit "what was the
    previous token" register rather than as a set of regexes with lookbehinds:
    the ``E'…'`` prefix rule is about the preceding TOKEN (``note E'x'`` is an
    escape string, ``tablE'x'`` is an identifier followed by a plain literal),
    and expressing it as a lookbehind is how the module came to disagree with
    §4.1 at ``x$e'C:\'`` in the first place.

    The register carries the previous token's END OFFSET as well as its text, and
    is inert unless ``previous_end == index``: §4.1.2.2 puts the prefix
    "immediately before the opening single quote", so ``E 'C:\'`` is an
    identifier followed by a PLAIN literal, not an escape string. Without the
    offset the register survives whitespace, the reference over-blanks (verifier
    #2018 round-5 L1 measured it swallowing a whole ``FROM`` clause), and — worse
    for an oracle — it over-blanks in the same direction the module's own
    lookbehind would if someone "tidied" it, so the differential would go green
    on the very mutant it exists to catch. Pinned by the
    ``escape_prefix_needs_adjacency`` row of
    :func:`test_the_reference_lexer_answers_its_own_known_cases`.

    Identifier and numeric events are appended at the branches that consume those
    tokens. They are not reconstructed from the final spans: a scanner that
    bypasses the numeric helper on no-exponent or signed rows can still emit the
    same literal spans.

    Unterminated runs extend to the end of the input, which is also what the
    module does; the difference between "closed" and "ran out" is the module's
    own belt and is not part of this comparison.
    """
    spans: list[tuple[int, int, str]] = []
    identifiers: list[_ReferenceToken] = []
    numerics: list[_ReferenceToken] = []
    index = 0
    length = len(sql)
    previous_token: str | None = None
    previous_end = -1
    while index < length:
        character = sql[index]

        if character == "'":
            escaped = previous_token in ("E", "e") and previous_end == index
            cursor = index + 1
            while cursor < length:
                if escaped and sql[cursor] == "\\":
                    cursor += 2
                    continue
                if sql[cursor] == "'":
                    if cursor + 1 < length and sql[cursor + 1] == "'":
                        cursor += 2
                        continue
                    cursor += 1
                    break
                cursor += 1
            else:
                cursor = length
            spans.append((index, min(cursor, length), "literal"))
            index, previous_token = min(cursor, length), None
            continue

        if character == '"':
            cursor = index + 1
            while cursor < length:
                if sql[cursor] == '"':
                    if cursor + 1 < length and sql[cursor + 1] == '"':
                        cursor += 2
                        continue
                    cursor += 1
                    break
                cursor += 1
            else:
                cursor = length
            index, previous_token = min(cursor, length), None
            continue

        if sql.startswith("--", index):
            match = _REFERENCE_NEWLINE.search(sql, index)
            stop = length if match is None else match.start()
            spans.append((index, stop, "comment"))
            index, previous_token = stop, None
            continue

        if sql.startswith("/*", index):
            depth = 0
            cursor = index
            while cursor < length:
                if sql.startswith("/*", cursor):
                    depth += 1
                    cursor += 2
                    continue
                if sql.startswith("*/", cursor):
                    depth -= 1
                    cursor += 2
                    if depth == 0:
                        break
                    continue
                cursor += 1
            else:
                cursor = length
            spans.append((index, min(cursor, length), "comment"))
            index, previous_token = min(cursor, length), None
            continue

        if character == "$":
            opener = _DOLLAR_QUOTE_TAG.match(sql, index)
            if opener is not None:
                tag = opener.group(0)
                closing = sql.find(tag, opener.end())
                stop = length if closing == -1 else closing + len(tag)
                spans.append((index, stop, "literal"))
                index, previous_token = stop, None
                continue
            index, previous_token = index + 1, None
            continue

        if _is_ident_start(character):
            cursor = index
            while cursor < length and _is_ident_cont(sql[cursor]):
                cursor += 1
            previous_token, previous_end = sql[index:cursor], cursor
            identifiers.append(_ident(index, cursor, previous_token))
            index = cursor
            continue

        numeric_stop = _consume_reference_numeric_token(sql, index)
        if numeric_stop is not None:
            numerics.append(_num(index, numeric_stop, sql[index:numeric_stop]))
            index, previous_token = numeric_stop, None
            continue

        if not character.isspace():
            previous_token = None
        index += 1
    return _ReferenceLexResult(tuple(spans), tuple(identifiers), tuple(numerics))


def reference_non_code_spans(sql: str) -> tuple[tuple[int, int, str], ...]:
    """Half-open ``(start, stop, kind)`` runs of ``sql`` that PostgreSQL does not lex as code."""
    return _reference_lex(sql).spans


# ---------------------------------------------------------------------------
# The alphabet
# ---------------------------------------------------------------------------

#: Fragments composed into samples. Every one is a §4.1 construct some review
#: round touched: the round-1 ``E'…'`` escape string, the round-3 nested comment
#: and ``\r`` line comment, the round-4 non-ASCII and symbol dollar tags, the
#: quoted identifiers whose apostrophe desynchronised the scan under J2's mutant,
#: and the ordinary statement pieces that make a sample look like a read.
ALPHABET: tuple[str, ...] = (
    "SELECT ",
    "value ",
    "rt.value ",
    "FROM hydro.river_timeseries rt ",
    "WHERE rt.variable = %(v)s ",
    "AND rt.run_key = %(k)s ",
    "-- note 'x\n",
    "-- note 'x\r",
    "-- tail",
    "/* c */ ",
    "/* a /* b */ don't */ ",
    "/* a /* b */ c */ ",
    "/*/ x */ ",
    "/**/ ",
    "'q_down' ",
    "E'q\\'x' ",
    "e'q\\'x' ",
    "E'a''b' ",
    "e'a''b' ",
    "'it''s' ",
    "'a' ",
    "'a'\n'b' ",
    '"a""b" ',
    '"quoted" ',
    "\"a'b\" ",
    '"a--b" ',
    '"a/*b" ',
    "B'1010' ",
    "X'FF' ",
    "1e5 ",
    "don't ",
    "tablE'x' ",
    # A PLAIN literal ending in a backslash, and the same shape behind a keyword
    # that merely ends in `E`. These are what discriminate the `E'…'` SCOPING
    # rule: with `_opens_escape_string` returning True unconditionally every
    # literal becomes backslash-aware, the closing quote reads as escaped, and
    # the span runs on — the round-1 B/P2-2 failure. Without them the mutant is
    # invisible to the differential (measured: it survived).
    r"'C:\' ",
    r"LIKE'C:\' ",
    # The `E'…'` ADJACENCY fragments (#2018 round-5 L1). Every other fragment
    # ends in whitespace, so before these the alphabet could not even GENERATE a
    # bare `E` next to a quote: 380 of the 2000 samples contained `[Ee]\s+'` and
    # in every one the token before the quote was a multi-char identifier
    # (`value 'it''s'`), never the `E` token the rule is about. The first two
    # produce `E ␠'…'`, which §4.1.2.2 makes a PLAIN literal because the prefix
    # must sit immediately before the opening quote; the last two produce a
    # genuine `E'…'` behind a token that is not an identifier character. Without
    # all four the reference's own adjacency rule is unreachable and a module
    # that walked its lookbehind back over whitespace stayed green (mutant m6).
    "E ",
    "e ",
    '"x"E',
    ")E",
    '"x"e',
    ")e",
    # In-subset on their own, out of subset only in COMPOSITION: glued to a
    # following `'…'` fragment they spell `1E'` / `1e'`, which decision 18's
    # second arm refuses (round-5 L2). Addable only because that arm exists —
    # with the corrected reference and no arm the pair is a real 18-mismatch
    # divergence over 20 seeds, which is what L2 measured.
    "1E",
    "1e",
    "AS n ",
    ", ",
    "|| ",
    "(SELECT 1) ",
    "::text ",
    # Outside the declared subset — present so the differential proves the
    # REFUSAL arm is reached, not merely assumed (decision 18).
    "$q$body$q$ ",
    "$$body$$ ",
    "$tag$a$tag$ ",
    "$备注$don't$备注$ ",
    "$€$don't$€$ ",
    "$q€$a$q€$ ",
    "€$q$b ",
    "a$b$c ",
    "é$b$c ",
    r"x$e'C:\' ",
    "$1 ",
)

#: The fragments that carry no subset violation of their own, i.e. the ones that
#: CAN appear in an accepted sample. Derived, never hand-listed: a hand-listed
#: copy would silently stop matching the alphabet above.
IN_SUBSET_ALPHABET: tuple[str, ...] = tuple(
    fragment for fragment in ALPHABET if _lexical_subset_violation(fragment) is None
)

#: Fixed so the differential is a REGRESSION test, not a lottery: a seeded run
#: that goes red goes red for everyone, and a reviewer can reproduce the exact
#: sample from the seed alone.
SEED = 20180905
SAMPLE_COUNT = 2000

#: Measured at this commit: 892/2000 samples (44.6%) are inside the declared
#: subset and are therefore actually compared span-for-span. Of the other 1108,
#: 1101 include at least one of the eleven intrinsically out-of-subset fragments
#: and 7 are refused only in COMPOSITION — individually in-subset numeric-prefix
#: fragments (``1E`` / ``1e`` and extended token composition) glued to a following
#: quote-bearing fragment, which is decision 18's second arm (round-5 L2) — so
#: the refusal arm is exercised by both halves of the subset rule. Accepted
#: token-adjacent escape counts on the same seed: 135 uppercase ``E'…'``, 128
#: lowercase ``e'…'`` (true one-character ``E`` / ``e`` tokens; identifier-tail
#: ``LIKE'…'`` / ``tablE'…'`` are not counted).
#: Pinned well below the measurement so ordinary drift in the alphabet does
#: not redden it, while the failure this floor exists to catch — a subset rule
#: that widens until it refuses everything and the differential asserts nothing —
#: cannot pass.
MINIMUM_ACCEPTED_FRACTION = 0.25


def _samples() -> list[str]:
    generator = random.Random(SEED)
    return [
        "".join(generator.choice(ALPHABET) for _ in range(generator.randint(2, 7))) for _ in range(SAMPLE_COUNT)
    ]


def _has_token_adjacent_escape(sql: str, prefix: str) -> bool:
    """Whether ``sql`` contains a token-adjacent ``{prefix}'`` that is not inside a span.

    Independent of the production recognizer: a quote qualifies only when the
    preceding identifier event from the same ``_reference_lex`` pass is the
    one-character token ``prefix`` and its stop equals the quote start.
    ``LIKE'…'`` and ``tablE'…'`` are identifier tails, not ``E`` / ``e``
    tokens. Used only as an anti-vacuity count so an uppercase-only alphabet
    or an uppercase-only reference cannot stay green.
    """
    lexed = _reference_lex(sql)
    for start, _stop, kind in lexed.spans:
        if kind != "literal" or start == 0 or sql[start] != "'":
            continue
        if any(token.stop == start and token.text == prefix for token in lexed.identifiers):
            return True
    return False


def test_the_scanner_agrees_with_a_reference_lexer_or_refuses() -> None:
    """Every sample is either refused as outside the subset, or lexed exactly like §4.1.

    The disjunction is the whole contract of decision 18: the module is allowed
    to be ignorant, it is not allowed to be WRONG. A mismatch here is the
    common-mode failure of rounds 1–4 — the module blanking text PostgreSQL calls
    code (hiding a read from both counters at once) or reading as code what
    PostgreSQL blanks.

    Both anti-vacuity guards are load-bearing and were chosen because the obvious
    version of this test is trivially satisfiable: a module that refused every
    statement would pass the disjunction with no comparison ever made.

    What the fragment-coverage guard does NOT prove: it is a SUBSTRING test, so
    the adjacency fragments ``"E "`` and ``"e "`` are satisfied by any accepted
    sample containing ``WHERE `` or ``value `` and their presence in the alphabet
    is not evidence that a bare ``E`` ever landed next to a quote. The evidence
    for that is the mutant: with ``_opens_escape_string`` walking its lookbehind
    back over whitespace this test goes red (round-5 L1, and the kill is recorded
    in the round-5 implementer report), which it could not do before these
    fragments existed.
    """
    samples = _samples()
    accepted: list[str] = []
    mismatches: list[tuple[str, tuple[object, ...], tuple[object, ...]]] = []

    for sample in samples:
        if _lexical_subset_violation(sample) is not None:
            continue
        accepted.append(sample)
        module_spans = non_code_spans(sample)
        reference_spans = reference_non_code_spans(sample)
        if module_spans != reference_spans:
            mismatches.append((sample, module_spans, reference_spans))

    assert mismatches == [], (
        f"{len(mismatches)}/{len(accepted)} accepted samples lex differently from PostgreSQL §4.1; "
        f"first: {mismatches[0][0]!r} module={mismatches[0][1]} reference={mismatches[0][2]}"
    )

    fraction = len(accepted) / len(samples)
    assert fraction >= MINIMUM_ACCEPTED_FRACTION, (
        f"only {fraction:.3f} of samples were accepted (floor {MINIMUM_ACCEPTED_FRACTION}) — the disjunction "
        "is being satisfied by refusing everything, so it asserts nothing"
    )

    uncovered = [fragment for fragment in IN_SUBSET_ALPHABET if not any(fragment in sample for sample in accepted)]
    assert uncovered == [], (
        f"these in-subset constructs never reached the span comparison: {uncovered!r} — the differential "
        "does not cover the rules it claims to"
    )

    # And the refusal arm is genuinely exercised, not a branch nothing takes.
    assert len(accepted) < len(samples)

    # L1's case split is not a substring of ``"e "`` / ``"E "``. Those fragments
    # only generate whitespace-separated prefixes; the adjacent forms that
    # actually exercise ``previous_token in ("E", "e") and previous_end == index``
    # come from ``E'…'`` / ``e'…'`` and from ``"x"E`` / ``)E`` / ``"x"e`` / ``)e``
    # glued to a following quote. An uppercase-only reference mutant stays green
    # unless both counts are non-zero (PR #2057 cand-LOWER-01).
    adjacent_upper = sum(1 for sample in accepted if _has_token_adjacent_escape(sample, "E"))
    adjacent_lower = sum(1 for sample in accepted if _has_token_adjacent_escape(sample, "e"))
    assert adjacent_upper > 0, "seeded alphabet generated no accepted adjacent E'…' samples"
    assert adjacent_lower > 0, "seeded alphabet generated no accepted adjacent e'…' samples"


def test_the_scanner_agrees_with_the_reference_lexer_over_the_registry() -> None:
    """The deterministic sibling: the twenty real templates, and both renders of each.

    The fuzz above samples a construct space; this asserts the thing decision 18
    is actually paid for — that the production read templates are all INSIDE the
    declared subset (so the subset costs the readers nothing) and that the module
    lexes every one of them exactly as PostgreSQL does.
    """
    compared = 0
    for entry in REGISTRY:
        source = entry.source()
        texts = [(entry.key, source)]
        for store in ("legacy", "narrow"):
            try:
                texts.append((f"{entry.key}:{store}", render_river_ts_sql(source, store, entry=entry.key).sql))
            except RiverTemplateError as error:  # pragma: no cover - a refusing entry is a red elsewhere
                raise AssertionError(f"{entry.key} does not render for {store}: {error}") from error
        for label, sql in texts:
            assert _lexical_subset_violation(sql) is None, f"{label} is outside the declared lexical subset"
            assert non_code_spans(sql) == reference_non_code_spans(sql), f"{label} lexes differently from §4.1"
            compared += 1

    assert compared == 3 * len(REGISTRY) == 60


@pytest.mark.parametrize(
    ("label", "sql", "expected"),
    [
        ("nested_block_comment", "/* a /* b */ c */ SELECT 1", ((0, 17, "comment"),)),
        ("carriage_return_line_comment", "SELECT 1 -- note\rFROM t", ((9, 16, "comment"),)),
        ("escape_string", r"SELECT E'a\'b' FROM t", ((8, 14, "literal"),)),
        ("escape_string_lowercase", r"SELECT e'a\'b' FROM t", ((8, 14, "literal"),)),
        ("escape_prefix_needs_adjacency", r"SELECT note E 'C:\' AS n", ((14, 19, "literal"),)),
        ("escape_prefix_needs_adjacency_lowercase", r"SELECT note e 'C:\' AS n", ((14, 19, "literal"),)),
        ("plain_backslash_literal", r"SELECT 'C:\' FROM t", ((7, 12, "literal"),)),
        ("quoted_identifier_is_code", "SELECT \"it's\" FROM t", ()),
        ("dollar_body", "SELECT $q$a'b$q$ FROM t", ((7, 16, "literal"),)),
    ],
    ids=[
        "nested_block_comment",
        "carriage_return_line_comment",
        "escape_string",
        "escape_string_lowercase",
        "escape_prefix_needs_adjacency",
        "escape_prefix_needs_adjacency_lowercase",
        "plain_backslash_literal",
        "quoted_identifier_is_code",
        "dollar_body",
    ],
)
def test_the_reference_lexer_answers_its_own_known_cases(
    label: str, sql: str, expected: tuple[tuple[int, int, str], ...]
) -> None:
    """The reference is pinned INDEPENDENTLY of the module, or it is not an oracle.

    Without this, a reference that silently degraded to "no spans at all" would
    still make the differential green on every sample the module also blanks
    nothing in. The ``dollar_body`` id is the one case where the reference and
    the module deliberately DISAGREE: §4.1 has dollar quoting, this module
    refuses it (decision 18), and the differential never compares such a sample —
    so the reference's dollar rule needs its own pin here.

    ``escape_prefix_needs_adjacency`` is round-5 L1's row and the one that pins a
    reference rule the MODULE is already right about: the reference as first
    committed read ``E ␠'C:\\'`` as an escape string and blanked ``(14, 81)`` of
    that statement — over its ``FROM`` clause and every predicate — while the
    module answered ``(14, 19)``. A reference that over-blanks is an oracle that
    agrees with the fail-open mutant, which is precisely the common-mode failure
    this file exists to end.
    """
    assert reference_non_code_spans(sql) == expected


class _ReferenceLexCase(NamedTuple):
    """One independently authored scanner-result oracle row.

    ``helper_stop`` is the helper's half-open token end at ``helper_start``.
    ``spans`` / ``identifiers`` / ``numerics`` are the exact ``_reference_lex``
    partition of the same ``sql``. Every expected value is a literal in this
    table: none is computed from helper output, and neither consumer may consult
    production ``_lexical_subset_violation``.
    """

    label: str
    sql: str
    helper_start: int
    helper_stop: int | None
    spans: tuple[tuple[int, int, str], ...]
    identifiers: tuple[_ReferenceToken, ...]
    numerics: tuple[_ReferenceToken, ...]


# Finite lexical-transition oracle. 50 scanner-result rows: 4 mantissa
# classes × 7 exponent states (28), plus incomplete/malformed/EOF/ident
# controls. Expected values are authored literals, not helper output.
# Unsigned uppercase and signed plus/minus rows use two exponent digits
# so the post-first-digit loop is discriminated by stop and token text.
_REFERENCE_LEX_CASES: tuple[_ReferenceLexCase, ...] = (
    _ReferenceLexCase(
        "integer_none",
        r"SELECT 12E'a\'x' AS n",
        7,
        9,
        ((10, 16, "literal"),),
        (_ident(0, 6, "SELECT"), _ident(9, 10, "E"), _ident(17, 19, "AS"), _ident(20, 21, "n")),
        (_num(7, 9, "12"),),
    ),
    _ReferenceLexCase(
        "integer_exp_e",
        r"SELECT 12e2E'a\'x' AS n",
        7,
        11,
        ((12, 18, "literal"),),
        (_ident(0, 6, "SELECT"), _ident(11, 12, "E"), _ident(19, 21, "AS"), _ident(22, 23, "n")),
        (_num(7, 11, "12e2"),),
    ),
    _ReferenceLexCase(
        "integer_exp_E",
        r"SELECT 12E34e'a\'x' AS n",
        7,
        12,
        ((13, 19, "literal"),),
        (_ident(0, 6, "SELECT"), _ident(12, 13, "e"), _ident(20, 22, "AS"), _ident(23, 24, "n")),
        (_num(7, 12, "12E34"),),
    ),
    _ReferenceLexCase(
        "integer_exp_e_plus",
        r"SELECT 12e+2E'a\'x' AS n",
        7,
        12,
        ((13, 19, "literal"),),
        (_ident(0, 6, "SELECT"), _ident(12, 13, "E"), _ident(20, 22, "AS"), _ident(23, 24, "n")),
        (_num(7, 12, "12e+2"),),
    ),
    _ReferenceLexCase(
        "integer_exp_E_plus",
        r"SELECT 12E+23e'a\'x' AS n",
        7,
        13,
        ((14, 20, "literal"),),
        (_ident(0, 6, "SELECT"), _ident(13, 14, "e"), _ident(21, 23, "AS"), _ident(24, 25, "n")),
        (_num(7, 13, "12E+23"),),
    ),
    _ReferenceLexCase(
        "integer_exp_e_minus",
        r"SELECT 12e-23E'a\'x' AS n",
        7,
        13,
        ((14, 20, "literal"),),
        (_ident(0, 6, "SELECT"), _ident(13, 14, "E"), _ident(21, 23, "AS"), _ident(24, 25, "n")),
        (_num(7, 13, "12e-23"),),
    ),
    _ReferenceLexCase(
        "integer_exp_E_minus",
        r"SELECT 12E-2e'a\'x' AS n",
        7,
        12,
        ((13, 19, "literal"),),
        (_ident(0, 6, "SELECT"), _ident(12, 13, "e"), _ident(20, 22, "AS"), _ident(23, 24, "n")),
        (_num(7, 12, "12E-2"),),
    ),
    _ReferenceLexCase(
        "trailing_dot_none",
        r"SELECT 1.E'a\'x' AS n",
        7,
        9,
        ((10, 16, "literal"),),
        (_ident(0, 6, "SELECT"), _ident(9, 10, "E"), _ident(17, 19, "AS"), _ident(20, 21, "n")),
        (_num(7, 9, "1."),),
    ),
    _ReferenceLexCase(
        "trailing_dot_exp_e",
        r"SELECT 1.e2E'a\'x' AS n",
        7,
        11,
        ((12, 18, "literal"),),
        (_ident(0, 6, "SELECT"), _ident(11, 12, "E"), _ident(19, 21, "AS"), _ident(22, 23, "n")),
        (_num(7, 11, "1.e2"),),
    ),
    _ReferenceLexCase(
        "trailing_dot_exp_E",
        r"SELECT 1.E34e'a\'x' AS n",
        7,
        12,
        ((13, 19, "literal"),),
        (_ident(0, 6, "SELECT"), _ident(12, 13, "e"), _ident(20, 22, "AS"), _ident(23, 24, "n")),
        (_num(7, 12, "1.E34"),),
    ),
    _ReferenceLexCase(
        "trailing_dot_exp_e_plus",
        r"SELECT 1.e+2E'a\'x' AS n",
        7,
        12,
        ((13, 19, "literal"),),
        (_ident(0, 6, "SELECT"), _ident(12, 13, "E"), _ident(20, 22, "AS"), _ident(23, 24, "n")),
        (_num(7, 12, "1.e+2"),),
    ),
    _ReferenceLexCase(
        "trailing_dot_exp_E_plus",
        r"SELECT 1.E+23e'a\'x' AS n",
        7,
        13,
        ((14, 20, "literal"),),
        (_ident(0, 6, "SELECT"), _ident(13, 14, "e"), _ident(21, 23, "AS"), _ident(24, 25, "n")),
        (_num(7, 13, "1.E+23"),),
    ),
    _ReferenceLexCase(
        "trailing_dot_exp_e_minus",
        r"SELECT 1.e-23E'a\'x' AS n",
        7,
        13,
        ((14, 20, "literal"),),
        (_ident(0, 6, "SELECT"), _ident(13, 14, "E"), _ident(21, 23, "AS"), _ident(24, 25, "n")),
        (_num(7, 13, "1.e-23"),),
    ),
    _ReferenceLexCase(
        "trailing_dot_exp_E_minus",
        r"SELECT 1.E-2e'a\'x' AS n",
        7,
        12,
        ((13, 19, "literal"),),
        (_ident(0, 6, "SELECT"), _ident(12, 13, "e"), _ident(20, 22, "AS"), _ident(23, 24, "n")),
        (_num(7, 12, "1.E-2"),),
    ),
    _ReferenceLexCase(
        "fractional_none",
        r"SELECT 1.25E'a\'x' AS n",
        7,
        11,
        ((12, 18, "literal"),),
        (_ident(0, 6, "SELECT"), _ident(11, 12, "E"), _ident(19, 21, "AS"), _ident(22, 23, "n")),
        (_num(7, 11, "1.25"),),
    ),
    _ReferenceLexCase(
        "fractional_exp_e",
        r"SELECT 1.2e34E'a\'x' AS n",
        7,
        13,
        ((14, 20, "literal"),),
        (_ident(0, 6, "SELECT"), _ident(13, 14, "E"), _ident(21, 23, "AS"), _ident(24, 25, "n")),
        (_num(7, 13, "1.2e34"),),
    ),
    _ReferenceLexCase(
        "fractional_exp_E",
        r"SELECT 1.2E3e'a\'x' AS n",
        7,
        12,
        ((13, 19, "literal"),),
        (_ident(0, 6, "SELECT"), _ident(12, 13, "e"), _ident(20, 22, "AS"), _ident(23, 24, "n")),
        (_num(7, 12, "1.2E3"),),
    ),
    _ReferenceLexCase(
        "fractional_exp_e_plus",
        r"SELECT 1.2e+2E'a\'x' AS n",
        7,
        13,
        ((14, 20, "literal"),),
        (_ident(0, 6, "SELECT"), _ident(13, 14, "E"), _ident(21, 23, "AS"), _ident(24, 25, "n")),
        (_num(7, 13, "1.2e+2"),),
    ),
    _ReferenceLexCase(
        "fractional_exp_E_plus",
        r"SELECT 1.2E+23e'a\'x' AS n",
        7,
        14,
        ((15, 21, "literal"),),
        (_ident(0, 6, "SELECT"), _ident(14, 15, "e"), _ident(22, 24, "AS"), _ident(25, 26, "n")),
        (_num(7, 14, "1.2E+23"),),
    ),
    _ReferenceLexCase(
        "fractional_exp_e_minus",
        r"SELECT 1.2e-23E'a\'x' AS n",
        7,
        14,
        ((15, 21, "literal"),),
        (_ident(0, 6, "SELECT"), _ident(14, 15, "E"), _ident(22, 24, "AS"), _ident(25, 26, "n")),
        (_num(7, 14, "1.2e-23"),),
    ),
    _ReferenceLexCase(
        "fractional_exp_E_minus",
        r"SELECT 1.2E-2e'a\'x' AS n",
        7,
        13,
        ((14, 20, "literal"),),
        (_ident(0, 6, "SELECT"), _ident(13, 14, "e"), _ident(21, 23, "AS"), _ident(24, 25, "n")),
        (_num(7, 13, "1.2E-2"),),
    ),
    _ReferenceLexCase(
        "leading_dot_none",
        r"SELECT .25E'a\'x' AS n",
        7,
        10,
        ((11, 17, "literal"),),
        (_ident(0, 6, "SELECT"), _ident(10, 11, "E"), _ident(18, 20, "AS"), _ident(21, 22, "n")),
        (_num(7, 10, ".25"),),
    ),
    _ReferenceLexCase(
        "leading_dot_exp_e",
        r"SELECT .5e2E'a\'x' AS n",
        7,
        11,
        ((12, 18, "literal"),),
        (_ident(0, 6, "SELECT"), _ident(11, 12, "E"), _ident(19, 21, "AS"), _ident(22, 23, "n")),
        (_num(7, 11, ".5e2"),),
    ),
    _ReferenceLexCase(
        "leading_dot_exp_E",
        r"SELECT .5E34e'a\'x' AS n",
        7,
        12,
        ((13, 19, "literal"),),
        (_ident(0, 6, "SELECT"), _ident(12, 13, "e"), _ident(20, 22, "AS"), _ident(23, 24, "n")),
        (_num(7, 12, ".5E34"),),
    ),
    _ReferenceLexCase(
        "leading_dot_exp_e_plus",
        r"SELECT .5e+2E'a\'x' AS n",
        7,
        12,
        ((13, 19, "literal"),),
        (_ident(0, 6, "SELECT"), _ident(12, 13, "E"), _ident(20, 22, "AS"), _ident(23, 24, "n")),
        (_num(7, 12, ".5e+2"),),
    ),
    _ReferenceLexCase(
        "leading_dot_exp_E_plus",
        r"SELECT .5E+23e'a\'x' AS n",
        7,
        13,
        ((14, 20, "literal"),),
        (_ident(0, 6, "SELECT"), _ident(13, 14, "e"), _ident(21, 23, "AS"), _ident(24, 25, "n")),
        (_num(7, 13, ".5E+23"),),
    ),
    _ReferenceLexCase(
        "leading_dot_exp_e_minus",
        r"SELECT .5e-23E'a\'x' AS n",
        7,
        13,
        ((14, 20, "literal"),),
        (_ident(0, 6, "SELECT"), _ident(13, 14, "E"), _ident(21, 23, "AS"), _ident(24, 25, "n")),
        (_num(7, 13, ".5e-23"),),
    ),
    _ReferenceLexCase(
        "leading_dot_exp_E_minus",
        r"SELECT .5E-2e'a\'x' AS n",
        7,
        12,
        ((13, 19, "literal"),),
        (_ident(0, 6, "SELECT"), _ident(12, 13, "e"), _ident(20, 22, "AS"), _ident(23, 24, "n")),
        (_num(7, 12, ".5E-2"),),
    ),
    _ReferenceLexCase(
        "incomplete_exp_e",
        r"SELECT 1e'a\'x' AS n",
        7,
        8,
        ((9, 15, "literal"),),
        (_ident(0, 6, "SELECT"), _ident(8, 9, "e"), _ident(16, 18, "AS"), _ident(19, 20, "n")),
        (_num(7, 8, "1"),),
    ),
    _ReferenceLexCase(
        "incomplete_exp_E",
        r"SELECT 1E'a\'x' AS n",
        7,
        8,
        ((9, 15, "literal"),),
        (_ident(0, 6, "SELECT"), _ident(8, 9, "E"), _ident(16, 18, "AS"), _ident(19, 20, "n")),
        (_num(7, 8, "1"),),
    ),
    _ReferenceLexCase(
        "incomplete_exp_e_plus",
        r"SELECT 1e+'a\'x' AS n",
        7,
        8,
        ((10, 14, "literal"), (15, 21, "literal")),
        (_ident(0, 6, "SELECT"), _ident(8, 9, "e"), _ident(14, 15, "x")),
        (_num(7, 8, "1"),),
    ),
    _ReferenceLexCase(
        "incomplete_exp_E_minus",
        r"SELECT 1E-'a\'x' AS n",
        7,
        8,
        ((10, 14, "literal"), (15, 21, "literal")),
        (_ident(0, 6, "SELECT"), _ident(8, 9, "E"), _ident(14, 15, "x")),
        (_num(7, 8, "1"),),
    ),
    _ReferenceLexCase(
        "incomplete_trailing_dot_exp_e",
        r"SELECT 1.e'a\'x' AS n",
        7,
        9,
        ((10, 16, "literal"),),
        (_ident(0, 6, "SELECT"), _ident(9, 10, "e"), _ident(17, 19, "AS"), _ident(20, 21, "n")),
        (_num(7, 9, "1."),),
    ),
    _ReferenceLexCase(
        "incomplete_trailing_dot_exp_e_plus",
        r"SELECT 1.e+'a\'x' AS n",
        7,
        9,
        ((11, 15, "literal"), (16, 22, "literal")),
        (_ident(0, 6, "SELECT"), _ident(9, 10, "e"), _ident(15, 16, "x")),
        (_num(7, 9, "1."),),
    ),
    _ReferenceLexCase(
        "incomplete_trailing_dot_exp_E_minus",
        r"SELECT 1.E-'a\'x' AS n",
        7,
        9,
        ((11, 15, "literal"), (16, 22, "literal")),
        (_ident(0, 6, "SELECT"), _ident(9, 10, "E"), _ident(15, 16, "x")),
        (_num(7, 9, "1."),),
    ),
    _ReferenceLexCase(
        "malformed_double_E",
        r"SELECT 1EE'a\'x' AS n",
        7,
        8,
        ((10, 14, "literal"), (15, 21, "literal")),
        (_ident(0, 6, "SELECT"), _ident(8, 10, "EE"), _ident(14, 15, "x")),
        (_num(7, 8, "1"),),
    ),
    _ReferenceLexCase(
        "valid_exponent_then_plain_quote",
        r"SELECT 1.5e3'a\'x' AS n",
        7,
        12,
        ((12, 16, "literal"), (17, 23, "literal")),
        (_ident(0, 6, "SELECT"), _ident(16, 17, "x")),
        (_num(7, 12, "1.5e3"),),
    ),
    _ReferenceLexCase(
        "nonnumeric_start",
        r"SELECT E'a\'x' AS n",
        7,
        None,
        ((8, 14, "literal"),),
        (_ident(0, 6, "SELECT"), _ident(7, 8, "E"), _ident(15, 17, "AS"), _ident(18, 19, "n")),
        (),
    ),
    _ReferenceLexCase(
        "dot_without_digits",
        r"SELECT .E'a\'x' AS n",
        7,
        None,
        ((9, 15, "literal"),),
        (_ident(0, 6, "SELECT"), _ident(8, 9, "E"), _ident(16, 18, "AS"), _ident(19, 20, "n")),
        (),
    ),
    _ReferenceLexCase(
        "empty_input",
        "",
        0,
        None,
        (),
        (),
        (),
    ),
    _ReferenceLexCase(
        "eof_one_digit",
        "1",
        0,
        1,
        (),
        (),
        (_num(0, 1, "1"),),
    ),
    _ReferenceLexCase(
        "eof_integer",
        "12",
        0,
        2,
        (),
        (),
        (_num(0, 2, "12"),),
    ),
    _ReferenceLexCase(
        "eof_decimal",
        "1.25",
        0,
        4,
        (),
        (),
        (_num(0, 4, "1.25"),),
    ),
    _ReferenceLexCase(
        "eof_trailing_dot",
        "1.",
        0,
        2,
        (),
        (),
        (_num(0, 2, "1."),),
    ),
    _ReferenceLexCase(
        "eof_leading_dot",
        ".25",
        0,
        3,
        (),
        (),
        (_num(0, 3, ".25"),),
    ),
    _ReferenceLexCase(
        "eof_exponent",
        "1e2",
        0,
        3,
        (),
        (),
        (_num(0, 3, "1e2"),),
    ),
    _ReferenceLexCase(
        "eof_signed_exponent",
        "12E+23",
        0,
        6,
        (),
        (),
        (_num(0, 6, "12E+23"),),
    ),
    _ReferenceLexCase(
        "start_zero_unsigned_exp",
        r"12e2E'a\'x'",
        0,
        4,
        ((5, 11, "literal"),),
        (_ident(4, 5, "E"),),
        (_num(0, 4, "12e2"),),
    ),
    _ReferenceLexCase(
        "ident_digit_upper",
        r"SELECT col1E'C:\' AS n, value FROM t",
        7,
        None,
        ((12, 17, "literal"),),
        (
            _ident(0, 6, "SELECT"),
            _ident(7, 12, "col1E"),
            _ident(18, 20, "AS"),
            _ident(21, 22, "n"),
            _ident(24, 29, "value"),
            _ident(30, 34, "FROM"),
            _ident(35, 36, "t"),
        ),
        (),
    ),
    _ReferenceLexCase(
        "ident_digit_lower",
        r"SELECT x1e'C:\' AS n, value FROM t",
        7,
        None,
        ((10, 15, "literal"),),
        (
            _ident(0, 6, "SELECT"),
            _ident(7, 10, "x1e"),
            _ident(16, 18, "AS"),
            _ident(19, 20, "n"),
            _ident(22, 27, "value"),
            _ident(28, 32, "FROM"),
            _ident(33, 34, "t"),
        ),
        (),
    ),
)

# Helper-only start guards on the same EOF integer as eof_integer.
_REFERENCE_HELPER_EDGE_CASES: tuple[_ReferenceLexCase, ...] = (
    _ReferenceLexCase(
        "start_eq_len",
        "12",
        2,
        None,
        (),
        (),
        (_num(0, 2, "12"),),
    ),
    _ReferenceLexCase(
        "start_gt_len",
        "12",
        3,
        None,
        (),
        (),
        (_num(0, 2, "12"),),
    ),
)

_REFERENCE_HELPER_CASES = _REFERENCE_LEX_CASES + _REFERENCE_HELPER_EDGE_CASES


@pytest.mark.parametrize(
    "case",
    _REFERENCE_HELPER_CASES,
    ids=[case.label for case in _REFERENCE_HELPER_CASES],
)
def test_the_reference_numeric_token_consumer_stops_at_the_token_boundary(
    case: _ReferenceLexCase,
) -> None:
    """Helper grammar is pinned by the table's literal stops, not by production L2.

    Numbers are code, so a disconnected scanner can still look green unless this
    pin and the sibling scanner-result pin both fire. Deleting exponent, sign,
    leading-dot, trailing-dot, the multi-digit exponent loop, EOF consumption, or
    restoring a digit-only walker has to fail here.
    """
    assert _consume_reference_numeric_token(case.sql, case.helper_start) == case.helper_stop


@pytest.mark.parametrize(
    "case",
    _REFERENCE_LEX_CASES,
    ids=[case.label for case in _REFERENCE_LEX_CASES],
)
def test_the_reference_scanner_emits_the_literal_lex_result(
    case: _ReferenceLexCase,
) -> None:
    """The real scanner must emit authored spans AND token events in one pass.

    A mutant that uses the numeric helper only for unsigned-exponent rows can
    keep the same final spans on no-exponent and signed rows. Token events are
    emitted at the consuming branches, so that bypass fails here even when the
    span projection stays equal. Expected values are table literals, never
    derived from helper output.
    """
    assert reference_non_code_spans(case.sql) == case.spans
    assert _reference_lex(case.sql) == _ReferenceLexResult(case.spans, case.identifiers, case.numerics)


@pytest.mark.parametrize(
    ("label", "sql", "prefix", "expected"),
    [
        ("bare_upper", r"E'q\'x'", "E", True),
        ("bare_lower", r"e'q\'x'", "e", True),
        ("after_paren_upper", ")E'x'", "E", True),
        ("after_quoted_ident_upper", "\"x\"E'x'", "E", True),
        ("after_paren_lower", ")e'x'", "e", True),
        ("after_quoted_ident_lower", "\"x\"e'x'", "e", True),
        ("keyword_tail_upper", r"LIKE'C:\'", "E", False),
        ("identifier_tail_upper", "tablE'x'", "E", False),
        ("keyword_tail_lower", "like'x'", "e", False),
        ("identifier_tail_lower", "value'x'", "e", False),
        ("ident_digit_upper", r"SELECT col1E'C:\' AS n, value FROM t", "E", False),
        ("ident_digit_lower", r"SELECT x1e'C:\' AS n, value FROM t", "e", False),
        ("whitespace_separated_upper", r"SELECT note E 'C:\' AS n", "E", False),
        ("whitespace_separated_lower", r"SELECT note e 'C:\' AS n", "e", False),
        ("case_mismatch_upper_query", "E'x'", "e", False),
        ("case_mismatch_lower_query", "e'x'", "E", False),
    ],
    ids=[
        "bare_upper",
        "bare_lower",
        "after_paren_upper",
        "after_quoted_ident_upper",
        "after_paren_lower",
        "after_quoted_ident_lower",
        "keyword_tail_upper",
        "identifier_tail_upper",
        "keyword_tail_lower",
        "identifier_tail_lower",
        "ident_digit_upper",
        "ident_digit_lower",
        "whitespace_separated_upper",
        "whitespace_separated_lower",
        "case_mismatch_upper_query",
        "case_mismatch_lower_query",
    ],
)
def test_token_adjacent_escape_is_a_one_character_identifier_token(
    label: str, sql: str, prefix: str, expected: bool
) -> None:
    """L1's anti-vacuity predicate counts tokens, not the character before a quote."""
    assert _has_token_adjacent_escape(sql, prefix) is expected
