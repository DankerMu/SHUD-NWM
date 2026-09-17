"""Family guard for the ENOENT non-strict fallback doctrine (ADR 0009).

``docs/adr/0009-path-canonicalization-dereference-doctrine.md`` rules that a
path canonicalisation site MAY tolerate the non-strict ``os.path.realpath``
fallback only when the adjudicated path is dereferenced against the kernel
before any verdict derived from the normalised product is committed.  A
non-strict ``os.path.realpath`` does not raise on the ``<missing>/../<loop>``
input class -- it folds the loop lexically into a partially resolved product --
so the strict form is the only call in the supported interpreter range that
makes the loop visible to a handler as an ``OSError`` carrying an errno.

This module pins the one MECHANICAL invariant of that ruling:

    every member of the authority set passes ``strict=True`` on at least one
    of its ``os.path.realpath`` calls, unless it is named in the exemption
    list below with the ADR clause it relies on.

The authority set is ``(module, qualified function)`` pairs that call
``realpath``, collected by AST over ``services/``, ``workers/``, ``packages/``
and ``apps/``.  That key was chosen over a code-shape matcher on purpose (ADR
0009 "普查"): a shape matcher encodes the shape its author guessed, and the one
written for this family missed the site whose re-check sits in a second ``try``
after the handler, then -- once loosened -- misread a strict call on the other
arm of the same function as a re-check, and missed
``retry.py::_local_runtime_root_safety`` entirely.  Keyed by call site the set
is shape-independent: rewriting a site, adding or removing an errno split, or
moving the fallback into a helper does not move the set; only ADDING or
DELETING a site does.

The assertion is on the VIOLATOR SET, not on a member list or a member count
(ADR 0009 "权衡", PR #2440's lesson).  A pin that spells out "the authority set
is exactly these N functions" fails on every unrelated addition and cannot
notice its own name being deleted; the cardinality therefore appears in the
failure message for diagnosis and never as an assertion.

The exemption list is checked in BOTH directions: an entry naming a function
that no longer exists -- renamed, moved, deleted -- fails this test instead of
passing silently, so the list cannot rot into a set of dead names that quietly
exempt nothing.

Scan surface (ADR 0009 "普查"): ``services/``, ``workers/``, ``packages/``,
``apps/``, every ``*.py`` outside build and dependency directories.
``scripts/``, ``db/`` and ``infra/`` hold zero real call sites and ``tests/``
is not published.

The attribute-name key has a PREMISE -- ``realpath`` must never be reachable
under a bare name, because such a call parses as an ``ast.Name`` and drops out
of the authority set silently.  ADR 0009 states that premise in prose.  A prose
premise standing next to a mechanical guard is a hand-transcribed copy of a
property nobody re-checks, which is the exact drift this ADR exists to stop, so
:func:`test_no_call_shape_defeats_the_attribute_name_key` enforces it in the
same violator-set-is-empty shape.  The two tests are separate on purpose: with
a bare-name call in the tree, the authority test still passes (that is the
hole) while the premise test fails (that is the catch), and one transcript
shows both.

What this guard does NOT catch -- deliberately, per ADR 0009 "守卫" -- is an
author who writes the strict form but forgets the downstream dereference.  That
is a design-review question, and the ADR's four questions are that review's
checklist.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The four published trees. ADR 0009 records why scripts/, db/, infra/ and
# tests/ are out of scope.
_SCAN_ROOTS: tuple[str, ...] = ("services", "workers", "packages", "apps")

_EXCLUDED_DIR_PARTS: frozenset[str] = frozenset(
    {"__pycache__", ".venv", "node_modules", ".mypy_cache", ".pytest_cache"}
)

# Qualified name used for a realpath call that sits outside any function, so a
# module-level call cannot slip through the enumeration unnamed.
_MODULE_SCOPE = "<module>"

# (module path relative to the repository root, dotted qualified function name)
Member = tuple[str, str]

# (line number, whether this call passes strict=True)
RealpathCall = tuple[int, bool]

# Sites admitted WITHOUT a strict call. Each entry names the ADR 0009 clause it
# rests on; anything beyond these three has to go through review.
_EXEMPT_MEMBERS: frozenset[Member] = frozenset(
    {
        # ADR 0009 clause 2 (provable coincidence): the containment verdict and
        # the receipt write act on the same path on every input the write can
        # succeed on. Clause 2 quantifies over inputs only, so this entry also
        # carries the ADR's 已知限制 3 premise: a single operator CLI with no
        # concurrent writer. See the docstring of _require_output_outside_root.
        ("services/orchestrator/journal_scope_census.py", "_require_output_outside_root"),
        # ADR 0009 clause 1 (downstream dereference): the product of that
        # realpath only feeds the `_is_stub_basename(real)` verdict, and the
        # executable it judges is afterwards actually executed, so a phantom
        # product faults at the kernel rather than being admitted here.
        ("packages/common/shud_preflight.py", "check_shud_executable"),
        # ADR 0009 clause 1 (downstream dereference): it only compares products
        # it produced itself, and both operands are gated on blockers that
        # _db_free_path_check raises from real kernel probes (parent.lstat(),
        # exists(), is_symlink()/is_dir()); db_free.py:164-167 records that this
        # function has no rejection channel of its own.
        ("services/orchestrator/scheduler_config/db_free.py", "_db_free_path_identity"),
    }
)


def _iter_python_sources() -> list[Path]:
    """Every ``*.py`` file under the four scanned trees, in a stable order."""

    files: list[Path] = []
    for root_name in _SCAN_ROOTS:
        root = REPO_ROOT / root_name
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if any(part in _EXCLUDED_DIR_PARTS for part in path.parts):
                continue
            files.append(path)
    return files


def _collect_realpath_calls(node: ast.AST, scope: tuple[str, ...], found: dict[str, list[RealpathCall]]) -> None:
    """Record every ``*.realpath(...)`` call under ``node``, keyed by its scope.

    Functions and classes are qualified by dotted path, so two same-named
    helpers in one module stay distinct members.  Matching is on the CALL's
    attribute name rather than on the module expression in front of it, so the
    scan survives an ``import os.path as p`` or an ``import posixpath`` that a
    hard-coded ``os.path`` match would miss.  The price is a premise -- a call
    through a BARE ``realpath`` name is invisible here -- and that premise is
    enforced, not assumed, by
    :func:`test_no_call_shape_defeats_the_attribute_name_key`.
    """

    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _collect_realpath_calls(child, scope + (child.name,), found)
            continue
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and child.func.attr == "realpath":
            qualname = ".".join(scope) if scope else _MODULE_SCOPE
            strict = any(
                keyword.arg == "strict" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True
                for keyword in child.keywords
            )
            found.setdefault(qualname, []).append((child.lineno, strict))
        _collect_realpath_calls(child, scope, found)


def _parsed_sources() -> list[tuple[str, ast.Module]]:
    """Every scanned module as ``(path relative to the repo root, parsed tree)``.

    A ``SyntaxError`` is left to surface: a module the family guard cannot parse
    is a module whose canonicalisation sites it cannot see, and swallowing that
    would turn the guard green on a file it never read.
    """

    return [
        (path.relative_to(REPO_ROOT).as_posix(), ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        for path in _iter_python_sources()
    ]


def _authority_set() -> dict[Member, list[RealpathCall]]:
    """The ``(module, qualified function)`` pairs that call ``realpath``."""

    authority: dict[Member, list[RealpathCall]] = {}
    for module, tree in _parsed_sources():
        found: dict[str, list[RealpathCall]] = {}
        _collect_realpath_calls(tree, (), found)
        for qualname, calls in found.items():
            authority[(module, qualname)] = sorted(calls)
    return authority


def _key_defeating_shapes(module: str, tree: ast.Module) -> list[tuple[str, int, str]]:
    """Every construct in ``module`` that makes ``realpath`` callable unseen.

    The authority scan keys on the CALL's attribute name, so it sees a call if
    and only if that call's ``func`` is an ``ast.Attribute`` named ``realpath``.
    Three constructs break that, and each is reported here:

    1. ``from <anything> import realpath`` (under any ``asname``).  The call
       then parses as an ``ast.Name`` and is invisible.  The source module is
       NOT restricted to ``os.path``/``posixpath``/``ntpath``: a local helper
       re-exporting ``os.path.realpath`` defeats the key in exactly the same
       way, and a module allowlist would be one more hand-maintained list.
    2. A reference to ``<expr>.realpath`` that is not the ``func`` of a call --
       ``f = os.path.realpath``, a dict value, a callback argument, a default.
       The function object escapes and the eventual call site is somewhere the
       walk cannot attribute it to a function.
    3. ``getattr(<expr>, "realpath")`` with a literal name -- the same escape
       without ever producing an ``ast.Attribute`` node.

    Three OTHER shapes are deliberately NOT reported, because measurement says
    they do not defeat the key -- asserting them would be banning imports, and
    one of them is live in the tree today:

    * ``import posixpath`` / ``import ntpath``.  ``posixpath.realpath(x)`` is
      still an ``ast.Attribute`` with attr ``realpath``, so the authority scan
      keys on it normally.  These widen the surface without hiding anything,
      and ``workers/forcing_producer/file_store.py:6`` imports ``posixpath``
      today (for ``posixpath.basename``): asserting this shape empty would be
      red on an innocent import.
    * ``import os.path as p`` / ``from os import path as p``.  ``p.realpath(x)``
      is likewise an ``ast.Attribute`` named ``realpath``.  Keying on the
      attribute name rather than on the literal text ``os.path`` is precisely
      what makes the scan survive these.
    * ``import os as _os``.  ``_os.path.realpath(x)`` -- same.

    Not statically enforceable, and therefore a stated limit rather than a
    silent gap: a lookup whose attribute name is computed at runtime
    (``getattr(mod, name)``), or a call built through ``exec``/``eval``.
    """

    call_func_nodes = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    shapes: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "realpath":
                    bound = alias.asname or alias.name
                    source = "." * node.level + (node.module or "")
                    shapes.append(
                        (
                            module,
                            node.lineno,
                            f"`from {source} import realpath"
                            f"{f' as {alias.asname}' if alias.asname else ''}` binds a bare name "
                            f"`{bound}`; a call through it parses as ast.Name and leaves the authority set",
                        )
                    )
        elif isinstance(node, ast.Attribute) and node.attr == "realpath" and id(node) not in call_func_nodes:
            shapes.append(
                (
                    module,
                    node.lineno,
                    f"`{ast.unparse(node)}` is referenced without being called; the function object escapes "
                    "and its eventual call site is invisible to the authority scan",
                )
            )
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "realpath"
        ):
            shapes.append(
                (
                    module,
                    node.lineno,
                    f"`{ast.unparse(node)}` looks realpath up dynamically; no ast.Attribute node is produced",
                )
            )
    return shapes


def _render(members: list[Member]) -> str:
    return "\n".join(f"  - {module}::{qualname}" for module, qualname in members)


def test_every_realpath_site_resolves_strictly_or_is_a_named_exemption() -> None:
    """The violator set is empty, and no exemption names a vanished function.

    Two failure modes, both real:

    * a new canonicalisation site that only ever calls the non-strict form,
      with no entry admitting it -- it shows up as a violator;
    * an exemption that has rotted -- its function was renamed, moved or
      deleted, so the entry exempts nothing and the name misleads the next
      reader -- it shows up as unresolved.
    """

    authority = _authority_set()

    unresolved = sorted(_EXEMPT_MEMBERS - set(authority))
    assert unresolved == [], (
        "ADR 0009 exemption entries that no longer name a member of the authority set "
        f"({len(authority)} members today). Each was renamed, moved or deleted; an entry that "
        "resolves to nothing exempts nothing and misleads the next reader. Re-point or drop it:\n"
        f"{_render(unresolved)}"
    )

    violators = sorted(
        member
        for member, calls in authority.items()
        if not any(strict for _, strict in calls) and member not in _EXEMPT_MEMBERS
    )
    detail = "\n".join(
        f"  - {module}::{qualname} -> realpath calls at "
        f"{', '.join(f'line {lineno} (strict={strict})' for lineno, strict in authority[(module, qualname)])}"
        for module, qualname in violators
    )
    assert violators == [], (
        f"Path canonicalisation sites with no strict os.path.realpath call ({len(authority)} members in the "
        "authority set today). A non-strict realpath folds a symlink loop behind a missing component into its "
        "product instead of raising, so the loop never reaches a handler. Either pass strict=True on the "
        "adjudicating call, or add the site to _EXEMPT_MEMBERS naming the ADR 0009 clause that justifies it "
        "(docs/adr/0009-path-canonicalization-dereference-doctrine.md):\n"
        f"{detail}"
    )


def test_no_call_shape_defeats_the_attribute_name_key() -> None:
    """The authority set's own premise, enforced instead of asserted in prose.

    The scan above finds a canonicalisation site only when the call's ``func``
    is an ``ast.Attribute`` named ``realpath``.  Reach ``realpath`` under a bare
    name and the site is not a violator -- it is not in the authority set at
    all, and the guard stays green on a file that canonicalises non-strictly.
    That is a silent hole, the worst failure mode for a guard, so the day
    someone writes such a call shape this assertion fires by name.

    This is NOT a ban on imports: :func:`_key_defeating_shapes` documents which
    shapes were measured to leave the key intact (``import posixpath`` among
    them, live in the tree today) and reports only the ones that break it.
    """

    defeating = sorted(
        shape for module, tree in _parsed_sources() for shape in _key_defeating_shapes(module, tree)
    )
    detail = "\n".join(f"  - {module}:{lineno} -- {reason}" for module, lineno, reason in defeating)
    assert defeating == [], (
        "Call shapes that defeat the authority set's attribute-name key. The key is only closed while "
        "`realpath` is unreachable under a bare name; each construct below makes a canonicalisation call "
        "invisible to test_every_realpath_site_resolves_strictly_or_is_a_named_exemption, which would then "
        "stay GREEN on an unadjudicated non-strict site. Either call through an attribute "
        "(`os.path.realpath(...)`), or widen the authority scan to see this shape -- do not delete this "
        "assertion (ADR 0009, docs/adr/0009-path-canonicalization-dereference-doctrine.md):\n"
        f"{detail}"
    )
