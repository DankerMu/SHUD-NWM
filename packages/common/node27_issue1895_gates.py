"""G0 ancestry and G8 command-order structural contracts."""

from __future__ import annotations

import ast
import re

from packages.common.node27_issue1895_types import Issue1895ReadinessError

_ANCESTOR_STDOUT = re.compile(
    r"""test\s+"\$\((?:git\s+merge-base\s+--is-ancestor[^)]*)\)"\s*=\s*"0" """
)
_ANCESTOR_STATUS = re.compile(
    r"""git\s+merge-base\s+--is-ancestor\s+\S+\s+\S+\s*(?:\|\||;|\)|$)"""
)


def assert_ancestry_uses_exit_status(script: str) -> None:
    """Refuse command-substitution/stdout comparison of ``merge-base --is-ancestor``.

    ``git merge-base --is-ancestor`` reports via exit status and prints nothing
    on success, so ``test "$(git merge-base --is-ancestor ...)" = "0"`` can
    never observe a real ancestor.
    """

    text = str(script)
    if "merge-base --is-ancestor" not in text:
        raise Issue1895ReadinessError(
            "G0 fence never invokes git merge-base --is-ancestor",
            code="ANCESTRY_MISSING",
            stage="g0",
        )
    if _ANCESTOR_STDOUT.search(text) or 'test "$(git merge-base --is-ancestor' in text:
        raise Issue1895ReadinessError(
            "G0 ancestry gate compares merge-base stdout instead of exit status",
            code="ANCESTRY_STDOUT_COMPARISON",
            stage="g0",
        )
    if not re.search(r"git\s+merge-base\s+--is-ancestor\b", text):
        raise Issue1895ReadinessError(
            "G0 ancestry gate does not invoke merge-base --is-ancestor",
            code="ANCESTRY_MISSING",
            stage="g0",
        )


def python_uses_json_load_on_path(source: str) -> bool:
    """True when the snippet calls ``json.load(open(path))`` rather than a path string."""

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "load":
            if node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name) and arg.func.id == "open":
                    return True
    return False
