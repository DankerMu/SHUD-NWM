"""Helpers for asserting on SQL *shape* in source-text pins.

Issue #1341 moved the display-boundary ``hydro.river_timeseries`` predicates
onto integer surrogate keys while the caller-supplied identity stays text. The
resulting queries therefore contain text identity predicates in two very
different roles:

* inside a key-resolution scalar sub-select — ``(SELECT run_key FROM
  hydro.hydro_run WHERE run_id = :run_id)`` — which is REQUIRED, and
* directly against ``hydro.river_timeseries`` — ``ts.run_id = :run_id`` —
  which is exactly what must never come back.

A bare substring check cannot tell them apart, so a "the text predicate is
gone" pin that ignores the distinction goes green on code that never switched
(the resolution sub-select alone satisfies it). :func:`strip_scalar_subqueries`
removes the sub-selects so the remaining text is only the outer query's own
predicates, which is what the negative pins assert against.
"""

from __future__ import annotations

import re

_SUBQUERY_START = re.compile(r"\(\s*SELECT\b", re.IGNORECASE)


def strip_scalar_subqueries(sql: str) -> str:
    """Return ``sql`` with every parenthesised sub-``SELECT`` body removed.

    Removal is brace-balanced, so nested parentheses inside the sub-select
    (``unnest(enum_range(NULL::hydro.river_variable))``) are consumed with it.
    Non-``SELECT`` parentheses (``to_char(...)``, ``CASE`` arithmetic) are left
    alone.
    """
    kept: list[str] = []
    index = 0
    length = len(sql)
    while index < length:
        if sql[index] == "(" and _SUBQUERY_START.match(sql, index):
            depth = 0
            cursor = index
            while cursor < length:
                if sql[cursor] == "(":
                    depth += 1
                elif sql[cursor] == ")":
                    depth -= 1
                    if depth == 0:
                        cursor += 1
                        break
                cursor += 1
            index = cursor
            continue
        kept.append(sql[index])
        index += 1
    return "".join(kept)
