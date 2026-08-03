"""Shared text helpers for judging rendered Slurm sbatch templates.

Home of the single ``_join_line_continuations`` definition consumed by both
``tests/test_slurm_array_contract.py`` and
``tests/test_production_slurm_validation.py``; a private copy per test module
would re-create exactly the drift issue #1272 is cleaning up.
"""

from __future__ import annotations

import re

# Bash continues a command when a line ends with a backslash: the blanks before
# the backslash, the backslash, the newline, and the next line's leading blanks
# are all one token separator, folded here to a single space so the result is
# the command's canonical single-line spelling (the templates render
# ``... produce \`` with a space before the backslash).
# Both character classes are deliberately ``[ \t]`` and never ``\s``: ``\s``
# also matches newlines, so it would splice two distinct commands into one
# whenever a continuation is followed by an empty line -- bash TERMINATES the
# command there -- and the Jinja-rendered templates are dense with blank lines.
_LINE_CONTINUATION = re.compile(r"[ \t]*\\\n[ \t]*")


def _join_line_continuations(rendered: str) -> str:
    """Fold bash line continuations in ``rendered`` into a single space each.

    Nothing else is folded: ordinary newlines, quoting, and intra-line spacing
    stay untouched, so an assertion against the result still means "this exact
    command exists in the rendering" -- it merely tolerates the one layout
    freedom bash itself grants.
    """

    return _LINE_CONTINUATION.sub(" ", rendered)
