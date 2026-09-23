"""Family guard for the ``Path.resolve()`` surface (#2452, ADR 0009 已知限制 2).

``tests/test_path_canonicalization_family_guard.py`` guards the
``os.path.realpath`` surface with one mechanical criterion: every member passes
``strict=True`` somewhere.  That criterion rests on a property of STRICT
``os.path.realpath`` -- it raises an ``OSError`` carrying an errno (``ELOOP``,
``ENOENT``) on every supported CPython -- and that property does not hold for
``Path.resolve()``.  On CPython 3.12 and earlier, ``resolve(strict=True)`` raises
an errno-less ``RuntimeError`` on a symlink loop; only 3.13+ raises
``OSError(ELOOP)``.  So on this surface the ``strict=True`` form is not the safe
form, it is exactly the form whose ``except OSError`` arm is dead code on the 3.11
production pin (``.python-version``) for the one input it exists to classify.
Reusing the realpath criterion here would bless the four sites #2452 found.

This module's criterion is therefore independent:

    the set of STRICT ``.resolve()`` calls (a ``strict`` argument present and
    not the literal ``False``; ``strict=True`` or any non-literal value, taken
    conservatively) that sit lexically inside the body of a ``try`` in the same
    function whose handlers catch ``OSError`` itself (``OSError``, ``IOError`` or
    ``EnvironmentError``, bare, dotted, in a tuple, or through a module-level
    tuple constant), while no ``try`` around the call in that function has a
    handler catching ``RuntimeError``, ``Exception``, ``BaseException`` or a bare
    ``except``, is EMPTY.

Assertion shape follows ADR 0009 "权衡" and the realpath guard: the VIOLATOR SET
is asserted empty, never a member list or a count.

Deliberately excluded, with the reason each exclusion is correct:

* NON-strict calls (``strict`` absent or literally ``False``).  For them the
  ``OSError`` arm is not what is dead -- on 3.13+ the loop is FOLDED and nothing
  is raised -- so a ``RuntimeError`` arm added there would make the interpreters
  disagree (a blocker on 3.11, a folded pass-through on 3.13+), which is issue
  #2453's divergence created on purpose.  Non-strict sites are adjudicated one
  by one against ADR 0009's clauses in the #2452 triage (design.md §Triage of
  ``openspec/changes/resolve-surface-triage-and-loop-convergence``), not here.
* Handlers catching only SUBCLASSES of ``OSError`` (``FileNotFoundError`` ...).
  A loop escapes them on every interpreter alike, so they encode no
  interpreter-dependent dead arm.

Stated limits, not silent gaps:

* Coverage by an ENCLOSING ``try`` in the same function is accepted although an
  outer handler rarely produces the inner ``OSError`` arm's outcome.  The guard
  sees that a loop cannot escape untyped; it cannot see which outcome it gets.
* The guard cannot judge whether a handler that catches the loop converges onto
  the same assembly as the non-raising path.  That is #2453's lesson
  (``_scheduler_root_check`` caught the ``RuntimeError`` and still diverged, by
  returning early with a separately assembled payload); it is design review, as
  the missing-dereference case is for the realpath guard.
* Exception classes the AST cannot name statically -- a handler tuple computed
  at runtime, a class imported under an alias of ``OSError``, a
  ``contextlib.suppress(OSError)`` block -- are not recognised.  None exists in
  the tree today; the self-tests below show which shapes ARE recognised.
* Calls are matched on the attribute name ``resolve``; the receiver is not
  typed.  The #2452 census found no non-``Path`` ``.resolve()`` in the four
  trees, and a false positive could only make this guard red, never green.

Scan surface: the realpath guard's own ``_SCAN_ROOTS`` and file walk, imported
rather than restated, so the two surfaces cannot drift apart.
"""

from __future__ import annotations

import ast
import errno
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.test_path_canonicalization_family_guard import REPO_ROOT, _iter_python_sources

_OSERROR_NAMES = frozenset({"OSError", "IOError", "EnvironmentError"})
_LOOP_COVERING_NAMES = frozenset({"RuntimeError", "Exception", "BaseException"})

# (module, qualified function, line)
Violator = tuple[str, str, int]


def _module_tuple_constants(tree: ast.Module) -> dict[str, ast.expr]:
    """Module-level ``NAME = (A, B, ...)`` bindings, so ``except NAME:`` is readable."""

    constants: dict[str, ast.expr] = {}
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if isinstance(value, ast.Tuple):
            for target in targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = value
    return constants


def _caught_names(handler_type: ast.expr | None, constants: dict[str, ast.expr]) -> frozenset[str]:
    """Exception names a handler catches; ``"<bare>"`` for a bare ``except``."""

    if handler_type is None:
        return frozenset({"<bare>"})
    names: set[str] = set()
    pending: list[ast.expr] = [handler_type]
    expanded: set[str] = set()
    while pending:
        node = pending.pop()
        if isinstance(node, ast.Tuple):
            pending.extend(node.elts)
        elif isinstance(node, ast.Name):
            if node.id in constants and node.id not in expanded:
                expanded.add(node.id)
                pending.append(constants[node.id])
            else:
                names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return frozenset(names)


def _is_strict_resolve(call: ast.Call) -> bool:
    """A ``.resolve()`` call whose ``strict`` is present and not the literal ``False``."""

    if not (isinstance(call.func, ast.Attribute) and call.func.attr == "resolve"):
        return False
    strict: ast.expr | None = call.args[0] if call.args else None
    for keyword in call.keywords:
        if keyword.arg == "strict":
            strict = keyword.value
    if strict is None:
        return False
    return not (isinstance(strict, ast.Constant) and strict.value is False)


def _walk(
    node: ast.AST,
    scope: tuple[str, ...],
    handler_sets: tuple[frozenset[str], ...],
    constants: dict[str, ast.expr],
) -> Iterator[tuple[str, int, tuple[frozenset[str], ...]]]:
    """Yield ``(qualified function, line, handler sets of every enclosing try)`` per strict call.

    ``handler_sets`` holds one entry per ``try`` whose BODY lexically contains the
    current node, innermost last, reset at every function boundary: a handler in
    the outer function does not run for a call made later from a nested ``def``
    or ``lambda``.  Handlers, ``else`` and ``finally`` bodies are walked with the
    OUTER stack, because a call there is not protected by that ``try``'s handlers.
    """

    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield from _walk(child, scope + (child.name,), (), constants)
            continue
        if isinstance(child, ast.Lambda):
            yield from _walk(child, scope + ("<lambda>",), (), constants)
            continue
        if isinstance(child, ast.ClassDef):
            yield from _walk(child, scope + (child.name,), handler_sets, constants)
            continue
        if isinstance(child, (ast.Try, getattr(ast, "TryStar", ast.Try))):
            caught = frozenset().union(*(_caught_names(h.type, constants) for h in child.handlers))
            for statement in child.body:
                yield from _walk_statement(statement, scope, handler_sets + (caught,), constants)
            for handler in child.handlers:
                for statement in handler.body:
                    yield from _walk_statement(statement, scope, handler_sets, constants)
            for statement in (*child.orelse, *child.finalbody):
                yield from _walk_statement(statement, scope, handler_sets, constants)
            continue
        if isinstance(child, ast.Call) and _is_strict_resolve(child):
            yield ".".join(scope) or "<module>", child.lineno, handler_sets
        yield from _walk(child, scope, handler_sets, constants)


def _walk_statement(
    statement: ast.stmt,
    scope: tuple[str, ...],
    handler_sets: tuple[frozenset[str], ...],
    constants: dict[str, ast.expr],
) -> Iterator[tuple[str, int, tuple[frozenset[str], ...]]]:
    # A statement is itself a node the walk must classify (a nested Try, a def), so
    # wrap it instead of iterating only its children.
    yield from _walk(ast.Module(body=[statement], type_ignores=[]), scope, handler_sets, constants)


def _violators_in_source(module: str, source: str) -> list[Violator]:
    tree = ast.parse(source, filename=module)
    constants = _module_tuple_constants(tree)
    violators: list[Violator] = []
    for qualname, lineno, handler_sets in _walk(tree, (), (), constants):
        catches_oserror = any(caught & _OSERROR_NAMES for caught in handler_sets)
        covers_loop = any(caught & (_LOOP_COVERING_NAMES | {"<bare>"}) for caught in handler_sets)
        if catches_oserror and not covers_loop:
            violators.append((module, qualname, lineno))
    return violators


def test_no_strict_resolve_sits_behind_an_oserror_only_handler() -> None:
    """The violator set over the four published trees is empty."""

    violators = sorted(
        violator
        for path in _iter_python_sources()
        for violator in _violators_in_source(path.relative_to(REPO_ROOT).as_posix(), path.read_text(encoding="utf-8"))
    )
    detail = "\n".join(f"  - {module}::{qualname} line {lineno}" for module, qualname, lineno in violators)
    assert violators == [], (
        "Strict Path.resolve() calls whose try catches OSError but nothing that catches RuntimeError. On "
        "CPython <=3.12 a symlink loop makes strict resolve() raise an errno-less RuntimeError (3.13+: OSError "
        "ELOOP), so the OSError arm is dead code on the 3.11 pin for exactly the input it classifies. Catch "
        "RuntimeError too and map it onto the same outcome as the OSError arm (#2452; ADR 0009 已知限制 2):\n"
        f"{detail}"
    )


# --- detector self-tests on synthetic sources ---------------------------------------


def _flagged(source: str) -> list[str]:
    return [qualname for _, qualname, _ in _violators_in_source("synthetic.py", source)]


def test_strict_resolve_behind_an_oserror_only_handler_is_flagged() -> None:
    source = (
        "def site(candidate):\n"
        "    try:\n"
        "        return candidate.resolve(strict=True)\n"
        "    except OSError:\n"
        "        return None\n"
    )
    assert _flagged(source) == ["site"]


@pytest.mark.parametrize(
    "handler",
    (
        "(FileNotFoundError, OSError, ValueError)",
        "IOError",
        "EnvironmentError",
        "builtins.OSError",
        "_RESOLVE_ERRORS",
    ),
)
def test_oserror_spelled_as_tuple_alias_attribute_or_constant_is_flagged(handler: str) -> None:
    source = (
        "import builtins\n"
        "_RESOLVE_ERRORS = (ValueError, (OSError,))\n"
        "\n"
        "def site(candidate):\n"
        "    try:\n"
        "        return candidate.resolve(strict=True)\n"
        f"    except {handler}:\n"
        "        return None\n"
    )
    assert _flagged(source) == ["site"]


@pytest.mark.parametrize("strict", ("strict=flag", "True"))
def test_non_literal_or_positional_strict_is_treated_as_strict(strict: str) -> None:
    source = (
        "def site(candidate, flag):\n"
        "    try:\n"
        f"        return candidate.resolve({strict})\n"
        "    except OSError:\n"
        "        return None\n"
    )
    assert _flagged(source) == ["site"]


def test_a_handler_that_also_catches_runtime_error_is_not_flagged() -> None:
    source = (
        "def site(candidate):\n"
        "    try:\n"
        "        return candidate.resolve(strict=True)\n"
        "    except (OSError, RuntimeError):\n"
        "        return None\n"
    )
    assert _flagged(source) == []


@pytest.mark.parametrize("covering", ("RuntimeError", "Exception", "BaseException", ""))
def test_coverage_by_an_enclosing_try_is_accepted(covering: str) -> None:
    source = (
        "def site(candidate):\n"
        "    try:\n"
        "        try:\n"
        "            return candidate.resolve(strict=True)\n"
        "        except OSError:\n"
        "            return None\n"
        f"    except {covering}:\n".replace("except :", "except:")
        + "        return 'loop'\n"
    )
    assert _flagged(source) == []


def test_an_oserror_subclass_only_handler_is_not_flagged() -> None:
    # A loop escapes FileNotFoundError on every interpreter alike: nothing here is
    # dead on one interpreter and live on another.
    source = (
        "def site(candidate):\n"
        "    try:\n"
        "        return candidate.resolve(strict=True)\n"
        "    except FileNotFoundError:\n"
        "        return None\n"
    )
    assert _flagged(source) == []


@pytest.mark.parametrize("call", ("candidate.resolve(strict=False)", "candidate.resolve(False)", "candidate.resolve()"))
def test_a_non_strict_resolve_behind_an_oserror_handler_is_not_flagged(call: str) -> None:
    # On 3.13+ the non-strict form folds a loop without raising; a RuntimeError arm
    # here would make the interpreters disagree, so the call is a triage object.
    source = f"def site(candidate):\n    try:\n        return {call}\n    except OSError:\n        return None\n"
    assert _flagged(source) == []


def test_a_call_in_a_nested_function_or_the_handler_body_is_not_protected_by_that_try() -> None:
    # Neither call runs under the outer try's handlers, so neither is behind an
    # OSError-only handler; both are simply unwrapped strict calls.
    source = (
        "def site(candidate):\n"
        "    try:\n"
        "        def later():\n"
        "            return candidate.resolve(strict=True)\n"
        "        return later\n"
        "    except OSError:\n"
        "        return candidate.resolve(strict=True)\n"
    )
    assert _flagged(source) == []


# --- the cross-interpreter behaviour the criterion rests on (design D5) -------------


def _outcome(call) -> tuple[str, int | None]:
    try:
        call()
    except BaseException as error:  # noqa: BLE001 -- the exception class IS the measurement
        return type(error).__name__, getattr(error, "errno", None)
    return "returns", None


def _pin_shapes(tmp_path: Path) -> dict[str, str]:
    base = Path(os.path.realpath(tmp_path))
    (base / "a").symlink_to(base / "b")
    (base / "b").symlink_to(base / "a")
    return {"loop": str(base / "a"), "missing_dotdot_loop": str(base / "missing" / ".." / "a")}


_FORMS = {
    "resolve_non_strict": lambda value: Path(value).resolve(strict=False),
    "resolve_strict": lambda value: Path(value).resolve(strict=True),
    "realpath_strict": lambda value: os.path.realpath(value, strict=True),
    "realpath_non_strict": lambda value: os.path.realpath(value),
}

_LEGACY = sys.version_info < (3, 13)
_RETURNS = ("returns", None)
_ERRNOLESS_RUNTIME_ERROR = ("RuntimeError", None)

# design D5's table; the <=3.12 column is exercised on 3.11, the >=3.13 column on 3.14.
_TABLE = {
    ("loop", "resolve_non_strict"): _ERRNOLESS_RUNTIME_ERROR if _LEGACY else _RETURNS,
    ("loop", "resolve_strict"): _ERRNOLESS_RUNTIME_ERROR if _LEGACY else ("OSError", errno.ELOOP),
    ("loop", "realpath_strict"): ("OSError", errno.ELOOP),
    ("loop", "realpath_non_strict"): _RETURNS,
    ("missing_dotdot_loop", "resolve_non_strict"): _ERRNOLESS_RUNTIME_ERROR if _LEGACY else _RETURNS,
    ("missing_dotdot_loop", "resolve_strict"): ("FileNotFoundError", errno.ENOENT),
    ("missing_dotdot_loop", "realpath_strict"): ("FileNotFoundError", errno.ENOENT),
    ("missing_dotdot_loop", "realpath_non_strict"): _RETURNS,
}


@pytest.mark.parametrize(("shape", "form"), sorted(_TABLE))
def test_resolve_and_realpath_loop_behaviour_matches_the_pinned_table(tmp_path: Path, shape: str, form: str) -> None:
    value = _pin_shapes(tmp_path)[shape]

    assert _outcome(lambda: _FORMS[form](value)) == _TABLE[(shape, form)]
