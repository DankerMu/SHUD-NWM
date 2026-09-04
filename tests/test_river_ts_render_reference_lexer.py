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


#: ``dolq_start [A-Za-z\200-\377_]`` / ``dolq_cont [A-Za-z\200-\377_0-9]`` — the
#: tag of a dollar-quoted string follows the unquoted-identifier rules except
#: that it cannot contain a ``$`` (§4.1.2.4). An EMPTY tag (``$$``) is legal.
_DOLLAR_QUOTE_TAG = re.compile(r"\$(?:(?:[A-Za-z_]|[^\x00-\x7f])(?:[A-Za-z0-9_]|[^\x00-\x7f])*)?\$")

_REFERENCE_NEWLINE = re.compile(r"[\n\r]")


def reference_non_code_spans(sql: str) -> tuple[tuple[int, int, str], ...]:
    r"""Half-open ``(start, stop, kind)`` runs of ``sql`` that PostgreSQL does not lex as code.

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

    Unterminated runs extend to the end of the input, which is also what the
    module does; the difference between "closed" and "ran out" is the module's
    own belt and is not part of this comparison.
    """
    spans: list[tuple[int, int, str]] = []
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
            index = cursor
            continue

        if character.isdigit() or (
            character == "." and index + 1 < length and sql[index + 1].isdigit()
        ):
            cursor = index
            if sql[cursor] == ".":
                cursor += 1
                while cursor < length and sql[cursor].isdigit():
                    cursor += 1
            else:
                while cursor < length and sql[cursor].isdigit():
                    cursor += 1
                if cursor < length and sql[cursor] == ".":
                    cursor += 1
                    while cursor < length and sql[cursor].isdigit():
                        cursor += 1
            if (
                cursor < length
                and sql[cursor] in "Ee"
                and cursor + 1 < length
                and (sql[cursor + 1].isdigit() or sql[cursor + 1] in "+-")
            ):
                exponent = cursor + 1
                if sql[exponent] in "+-":
                    exponent += 1
                if exponent < length and sql[exponent].isdigit():
                    exponent += 1
                    while exponent < length and sql[exponent].isdigit():
                        exponent += 1
                    cursor = exponent
            index, previous_token = cursor, None
            continue

        if not character.isspace():
            previous_token = None
        index += 1
    return tuple(spans)


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
#: token-adjacent escape counts on the same seed: 232 uppercase ``E'…'``, 142
#: lowercase ``e'…'``.
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

    Independent of the production recognizer: walks THIS file's reference lexer
    and looks for an escape-string span whose preceding character is ``prefix``.
    Used only as an anti-vacuity count so an uppercase-only alphabet or an
    uppercase-only reference cannot stay green.
    """
    for start, _stop, kind in reference_non_code_spans(sql):
        if kind != "literal" or start == 0:
            continue
        if sql[start] == "'" and sql[start - 1] == prefix:
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
