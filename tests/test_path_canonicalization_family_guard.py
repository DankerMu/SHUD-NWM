"""Family guard for the ENOENT non-strict fallback doctrine (ADR 0009).

``docs/adr/0009-path-canonicalization-dereference-doctrine.md`` rules that a
path canonicalisation site MAY tolerate the non-strict ``os.path.realpath``
fallback only when the adjudicated path is dereferenced against the kernel
before any verdict derived from the normalised product is committed.  A
non-strict ``os.path.realpath`` does not raise on the ``<missing>/../<loop>``
input class -- it folds the loop lexically into a partially resolved product --
so the strict form is the only call in the supported interpreter range that
makes the loop visible to a handler as an ``OSError`` carrying an errno.

This module pins the two MECHANICAL invariants of that ruling:

    every member of the authority set passes ``strict=True`` on at least one
    of its ``os.path.realpath`` calls, unless it is named in the exemption
    list below with the ADR clause it relies on;

    and every member of the authority set records its disposition IN ITS OWN
    BODY as a comment naming either the admission clause it rests on (``ADR
    0009 clause 1``/``2``/``3``) or the fact that it re-resolves strictly
    instead of admitting (``ADR 0009 loop-filtered``).

The second invariant is required of EVERY member, not only of the members that
admit the ``ENOENT`` arm, because deciding which members admit is a judgement
about handler shape -- and this family's whole lesson is that shape inference
encodes the author's guess (see the authority-set paragraph below).  The guard
decides that a disposition is NAMED, never that the named one is CORRECT; the
latter is the design review the ADR's four questions drive.  Three further
limits are stated rather than papered over: a bare ``ADR 0009`` mention
deliberately does NOT satisfy the check, because it names no clause and
therefore says nothing; the marker is searched only within the member's own
``lineno``/``end_lineno`` span, so a comment at module scope cannot vouch for
every member of its file; and "as a comment" is taken literally, matched against
``tokenize.COMMENT`` tokens rather than against the raw text, so a docstring
sentence quoting the token or a string literal whose value spells it marks
nothing.  A marker sitting inside a NESTED function does count for the enclosing
one, since the enclosing span contains it -- no member in the tree is nested
today, and closing that would need a member-shaped exclusion.

One shape is refused outright instead of being marked: a qualified name bound by
more than one ``def`` in its scope.  The member key IS that name, so the two
bodies collapse into one member -- the strictness check's ``any()`` then lets a
strict call in either body cover a non-strict call in the other, and a marker in
either body vouches for both.  ``ruff --select F811`` does not report the
``if sys.platform == "win32": ... else: ...`` spelling of it.  The guard has no
way to split the member, so it reports the name and asks for two distinct names
or one shared helper.  There is no such member in the tree today.

Read that quantifier literally, because its scope is the guard's main limit.
The member key is ``(module, qualified function)`` and the test is ``any()``
over that member's calls, so a member that normalises EXCLUSIVELY non-strictly
is a violator, while a non-strict call added INSIDE a member that already
resolves strictly somewhere is not detected.  Worked example, verified by
deletion: ``services/orchestrator/scheduler_config/db_free.py``'s
``_db_free_loop_filtered_realpath`` resolves strictly at :206 and re-checks the
loop-filtered fallback strictly at :215 -- the re-check ADR 0009's census lists
as this function's whole reason for existing.  Delete ``strict=True`` from the
:215 re-check and this guard stays GREEN, because :206 still satisfies
``any()``.  That is a STATED limit, not a silent gap: the alternative pin
("this member's strict call count must not decrease") is a member-list-shaped
assertion, which ADR 0009 "权衡" / "守卫" rejects for the reasons recorded
under the violator-set paragraph below.  Losing a second strict call inside an
already-strict member is a design-review question, like the missing-dereference
case at the end of this docstring.

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

The exemption list is checked in BOTH directions, and both are mechanical:

* an entry naming a function that no longer exists -- renamed, moved, deleted
  -- fails this test instead of passing silently, so the list cannot rot into a
  set of dead names that quietly exempt nothing;
* an entry naming a member that DOES now resolve strictly fails too.  An
  exemption that is no longer needed is not harmless: while it stands, that
  member may lose its strict arm again without going red, so a site that had
  been repaired can silently regress behind the entry that once excused it.
  The entry has to be deleted the day the repair lands.

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
import io
import re
import tokenize
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

# The disposition every authority member owes its own body, per the requirement
# "Path canonicalization SHALL resolve strictly unless named ...".  The pattern
# is matched against the text of a `tokenize.COMMENT` token, never against the
# raw source: the spec says "as a comment", and a plain-text search cannot tell
# a comment from a docstring sentence or a string literal whose VALUE spells the
# token, which would let a member be marked by describing the scheme instead of
# applying it.  The pattern no longer spells a leading `#` -- the token type
# already carries that -- and it never did the work of rejecting a bare mention:
# that comes from the alternation.  The clause number is `[123]` rather than
# `\d+`, so an invented "clause 4" is not accepted, and a BARE `ADR 0009` matches
# neither branch, naming no disposition and therefore satisfying nothing.
_DISPOSITION_MARKER = re.compile(
    r"\bADR\s+0009\s+(?:clause\s+[123]\b|loop[-\s]?filtered\b)",
    re.IGNORECASE,
)

# Every (first line, last line) span that defines the member, or None when the
# realpath call has no enclosing function to carry a marker.  A list rather than
# one span because a qualified name CAN be bound more than once in a scope; the
# caller reports that instead of picking a definition.
MemberBody = list[tuple[int, int]] | None

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
        # it produced itself, and the config-side operand is gated on blockers
        # that _db_free_path_check raises from real kernel probes (parent.lstat(),
        # exists(), is_symlink()/is_dir()). The other operand is a module constant
        # passed through no gate and never probed. The function's own marker in
        # db_free.py carries both halves, plus the note that it has no rejection
        # channel of its own.
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


def _collect_member_bodies(
    node: ast.AST,
    scope: tuple[str, ...],
    enclosing: ast.FunctionDef | ast.AsyncFunctionDef | None,
    found: dict[str, MemberBody],
) -> None:
    """Record, per authority member, the line span of its own function body.

    Walks the same way as :func:`_collect_realpath_calls` and keys members
    identically, but carries the INNERMOST enclosing function alongside the
    dotted scope, because the scope's last element may be a class (whose body is
    not where a per-site marker belongs) or may be empty at module scope.  A
    ``realpath`` call with no enclosing function records ``None``: there is no
    body to carry a disposition, which the caller reports as a violation rather
    than silently skipping.

    Spans ACCUMULATE.  One qualified name can be bound by more than one ``def``
    in a scope -- the ``if sys.platform == "win32": ... else: ...`` split -- and
    recording only the last one hid every earlier definition's body from the
    marker search.  Collecting them all lets the caller report the ambiguity
    instead of silently adjudicating one definition on another's evidence.
    """

    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _collect_member_bodies(child, scope + (child.name,), child, found)
            continue
        if isinstance(child, ast.ClassDef):
            _collect_member_bodies(child, scope + (child.name,), None, found)
            continue
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and child.func.attr == "realpath":
            qualname = ".".join(scope) if scope else _MODULE_SCOPE
            if enclosing is None:
                found[qualname] = None
            else:
                span = (enclosing.lineno, enclosing.end_lineno or enclosing.lineno)
                spans = found.get(qualname)
                if not isinstance(spans, list):
                    # Absent, or the `None` sentinel a class-body call recorded
                    # under this same name. Either way there is nothing to
                    # extend; both states end in a reported violation.
                    found[qualname] = [span]
                elif span not in spans:
                    spans.append(span)
        _collect_member_bodies(child, scope, enclosing, found)


def _marker_comment_lines(source: str) -> frozenset[int]:
    """Line numbers of ``source`` carrying a disposition marker IN A COMMENT.

    Tokenising is the whole point: a regex over the raw text cannot distinguish
    a comment from a docstring sentence quoting the token, or from a string
    literal whose value spells it, and both read as a marker to a plain-text
    search while marking nothing.  ``tokenize.COMMENT`` is the only token type
    the spec's "as a comment" admits.

    A ``TokenError`` is left to surface for the same reason ``_read_sources``
    lets a ``SyntaxError`` through: a module this cannot tokenise is a module
    whose markers it cannot see, and swallowing that would pass it silently.
    """

    return frozenset(
        token.start[0]
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT and _DISPOSITION_MARKER.search(token.string)
    )


def _members_missing_disposition(module: str, source: str) -> list[tuple[Member, str]]:
    """Authority members of ``source`` whose own body carries no ADR 0009 marker.

    Takes the module TEXT rather than a path so the negative cases below -- a
    bare ``ADR 0009`` mention, a marker parked at module scope, a marker quoted
    in a docstring or a string literal, and a qualified name bound twice -- are
    exercised against synthetic sources instead of against the live tree, which
    would otherwise have to be temporarily broken to prove the check bites.

    A member whose qualified name is bound MORE THAN ONCE is reported without
    looking at its markers at all.  ``(module, qualified function)`` is the
    member key, so two definitions of one name are one member and the guard
    cannot say which body owes the marker; adjudicating either on the other's
    evidence is exactly the silent merge this reports.
    """

    marker_lines = _marker_comment_lines(source)
    bodies: dict[str, MemberBody] = {}
    _collect_member_bodies(ast.parse(source, filename=module), (), None, bodies)

    missing: list[tuple[Member, str]] = []
    for qualname, body in sorted(bodies.items()):
        if body is None:
            missing.append((
                (module, qualname),
                "the realpath call has no enclosing function, so there is no body that could carry a marker",
            ))
            continue
        if len(body) > 1:
            where = ", ".join(f"line {start}" for start, _ in sorted(body))
            missing.append((
                (module, qualname),
                f"this qualified name is bound {len(body)} times ({where}), so the (module, qualified function) "
                "key cannot tell the definitions apart: their realpath calls merge into one member and a marker "
                "in either body would vouch for both. Give them distinct names, or hoist the realpath call into "
                "a single helper",
            ))
            continue
        start, end = body[0]
        if not any(start <= line <= end for line in marker_lines):
            missing.append(((module, qualname), f"no disposition comment anywhere in lines {start}-{end}"))
    return missing


def _read_sources() -> list[tuple[str, str, ast.Module]]:
    """Every scanned module as ``(relative path, source text, parsed tree)``.

    A ``SyntaxError`` is left to surface: a module the family guard cannot parse
    is a module whose canonicalisation sites it cannot see, and swallowing that
    would turn the guard green on a file it never read.
    """

    sources: list[tuple[str, str, ast.Module]] = []
    for path in _iter_python_sources():
        text = path.read_text(encoding="utf-8")
        sources.append((path.relative_to(REPO_ROOT).as_posix(), text, ast.parse(text, filename=str(path))))
    return sources


def _parsed_sources() -> list[tuple[str, ast.Module]]:
    """Every scanned module as ``(path relative to the repo root, parsed tree)``."""

    return [(module, tree) for module, _, tree in _read_sources()]


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
    """The violator set is empty, and the exemption list holds no dead weight.

    Three failure modes, all real:

    * a canonicalisation site that normalises EXCLUSIVELY non-strictly -- every
      one of that ``(module, qualified function)`` member's ``realpath`` calls
      omits ``strict=True`` -- with no entry admitting it: it shows up as a
      violator.  The grouping is the limit, and it is stated rather than
      papered over: the check is ``any()`` over the member's calls, so a
      non-strict call added inside a member that already resolves strictly
      SOMEWHERE is NOT detected.  Concretely, deleting ``strict=True`` from the
      loop-filtered re-check at
      ``services/orchestrator/scheduler_config/db_free.py:215`` leaves this test
      green, because the same function's strict call at :206 satisfies
      ``any()``.  The module docstring records why the member-list-shaped pin
      that would catch it is rejected (ADR 0009 "守卫", 断言二);
    * an exemption that has rotted -- its function was renamed, moved or
      deleted, so the entry exempts nothing and the name misleads the next
      reader -- it shows up as unresolved;
    * an exemption that is no longer NEEDED -- the member it names now passes
      ``strict=True`` somewhere, so the entry is dead weight that would let that
      member drop its strict arm again without going red -- it shows up as
      unnecessary.
    """

    authority = _authority_set()

    unresolved = sorted(_EXEMPT_MEMBERS - set(authority))
    assert unresolved == [], (
        "ADR 0009 exemption entries that no longer name a member of the authority set "
        f"({len(authority)} members today). Each was renamed, moved or deleted; an entry that "
        "resolves to nothing exempts nothing and misleads the next reader. Re-point or drop it:\n"
        f"{_render(unresolved)}"
    )

    unnecessary = sorted(
        member for member in _EXEMPT_MEMBERS if any(strict for _, strict in authority.get(member, []))
    )
    unnecessary_detail = "\n".join(
        f"  - {module}::{qualname} -> strict realpath call(s) at "
        f"{', '.join(f'line {lineno}' for lineno, strict in authority[(module, qualname)] if strict)}"
        for module, qualname in unnecessary
    )
    assert unnecessary == [], (
        "ADR 0009 exemption entries whose member now resolves strictly. The entry is no longer "
        "needed, and while it stands the member may silently lose that strict call again without "
        "reddening this test -- a repaired site quietly regressing behind the entry that once "
        "excused it. Delete the entry (and the ADR clause note it carries, which no longer "
        "describes the code):\n"
        f"{unnecessary_detail}"
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


def _render_missing(missing: list[tuple[Member, str]]) -> str:
    return "\n".join(f"  - {module}::{qualname} -- {reason}" for (module, qualname), reason in missing)


def test_every_authority_member_records_its_adr_0009_disposition() -> None:
    """The set of authority members carrying no disposition marker is empty.

    Same violator-set shape as the strictness assertion above, and for the same
    reason: a member LIST would fail on every unrelated addition and could not
    notice its own name being deleted, so the cardinality appears only in the
    message.  The obligation is on EVERY member -- the requirement puts it that
    way because "which members admit the ENOENT arm" is a judgement about
    handler shape, and the commit that introduced this rule got that judgement
    wrong on 13 of 15 sites while writing the rule.
    """

    missing = [entry for module, source, _ in _read_sources() for entry in _members_missing_disposition(module, source)]
    authority_size = len(_authority_set())
    assert missing == [], (
        f"Path canonicalisation sites recording no ADR 0009 disposition ({authority_size} members in the authority "
        "set today). Every member owes a comment IN ITS OWN BODY naming either the admission clause it rests on "
        "(`ADR 0009 clause 1`, `2` or `3`) or the fact that it re-resolves strictly instead of admitting "
        "(`ADR 0009 loop-filtered`), plus the sentence saying why that disposition holds here -- a token with no "
        "reason feeds the guard and is the behaviour the ADR exists to stop. A bare `ADR 0009` mention names no "
        "disposition and does not count, and the marker must sit inside the member's own body, not at module "
        "scope (docs/adr/0009-path-canonicalization-dereference-doctrine.md):\n"
        f"{_render_missing(missing)}"
    )


def test_a_marker_inside_the_body_satisfies_the_disposition_check() -> None:
    """Positive control, so the two negative cases below cannot pass vacuously."""

    source = (
        "import os\n"
        "\n"
        "\n"
        "def site(path):\n"
        "    # ADR 0009 clause 1: the caller opens what it accepted.\n"
        "    return os.path.realpath(path)\n"
    )
    assert _members_missing_disposition("synthetic.py", source) == []


def test_a_bare_adr_0009_mention_is_not_a_disposition() -> None:
    """``ADR 0009`` on its own names nothing, so it must not satisfy the check.

    Accepting it would make the marker a ritual: the point of the token is to
    say WHICH of the three clauses the site rests on, or that it rests on none
    of them because it loop-filters.  A pointer to the document says neither.
    """

    source = (
        "import os\n"
        "\n"
        "\n"
        "def site(path):\n"
        "    # See ADR 0009 for the canonicalisation doctrine.\n"
        "    return os.path.realpath(path)\n"
    )
    missing = _members_missing_disposition("synthetic.py", source)
    assert [member for member, _ in missing] == [("synthetic.py", "site")]


def test_a_marker_quoted_in_a_docstring_is_not_a_disposition() -> None:
    """Prose ABOUT the marker is not the marker.

    A docstring that quotes the token -- documentation, a cross-reference, a
    sentence explaining the convention -- reads to a plain-text search exactly
    like a comment carrying it, and the requirement says "as a comment" for a
    reason: a marker has to be the site's own statement of its disposition, not
    a mention of the rule that governs it.  Accepting the mention would let any
    member be marked by describing the scheme instead of applying it.
    """

    source = (
        "import os\n"
        "\n"
        "\n"
        "def site(path):\n"
        '    """Sites record `# ADR 0009 clause 1` in their body."""\n'
        "    return os.path.realpath(path)\n"
    )
    missing = _members_missing_disposition("synthetic.py", source)
    assert [member for member, _ in missing] == [("synthetic.py", "site")]


def test_a_marker_inside_a_string_literal_is_not_a_disposition() -> None:
    """A string whose VALUE is the marker text does not mark the site either.

    Same hole as the docstring case and reported separately because it needs no
    prose at all: a constant, a log template or an error message whose text
    happens to contain the token is data the member carries, not a disposition
    it records.  Only a real comment token counts.
    """

    source = (
        "import os\n"
        "\n"
        "\n"
        "def site(path):\n"
        '    note = "# ADR 0009 clause 1"\n'
        "    return os.path.realpath(note and path)\n"
    )
    missing = _members_missing_disposition("synthetic.py", source)
    assert [member for member, _ in missing] == [("synthetic.py", "site")]


def test_two_definitions_of_one_qualified_name_are_reported_rather_than_merged() -> None:
    """A qualified name bound twice breaks the member key, so it is a violation.

    ``(module, qualified function)`` identifies a member only while the name is
    bound once.  Two ``def``s of one name in a scope -- the ordinary
    ``if sys.platform == "win32": ... else: ...`` split -- collapse into a
    single key, and the collapse hides the FIRST definition from both
    assertions: the strictness check is ``any()`` over the merged call list, so
    a strict call in either body covers a non-strict one in the other, and the
    body span used to be overwritten, so only the last definition's comments
    were searched.  ``ruff --select F811`` does not report this shape.

    The guard therefore refuses to guess which definition a marker belongs to
    and reports the name.  Splitting the member is not available -- the key IS
    the name -- so the rule is fail-closed: give the two definitions distinct
    names, or hoist the ``realpath`` call into one helper they both call.
    """

    source = (
        "import os\n"
        "import sys\n"
        "\n"
        "\n"
        "if sys.platform == 'win32':\n"
        "    def _canon(path):\n"
        "        return os.path.realpath(path, strict=True)\n"
        "else:\n"
        "    def _canon(path):\n"
        "        # ADR 0009 clause 1: the caller opens what it accepted.\n"
        "        return os.path.realpath(path)\n"
    )
    missing = _members_missing_disposition("synthetic.py", source)
    assert [member for member, _ in missing] == [("synthetic.py", "_canon")]
    assert "bound 2 times" in missing[0][1]


def test_a_module_scope_marker_does_not_vouch_for_a_member() -> None:
    """The search window is the member's own span, not the whole file.

    A file-level comment would otherwise exempt every member in its module at
    once, which is the file-granularity the ADR rejects: a marker has to move
    and die with the function it annotates.
    """

    source = (
        "import os\n"
        "\n"
        "# ADR 0009 clause 1: parked at module scope, which vouches for nothing.\n"
        "\n"
        "\n"
        "def site(path):\n"
        "    return os.path.realpath(path)\n"
    )
    missing = _members_missing_disposition("synthetic.py", source)
    assert [member for member, _ in missing] == [("synthetic.py", "site")]


def test_a_realpath_call_outside_any_function_has_nowhere_to_carry_a_marker() -> None:
    """A module-scope call site is reported rather than skipped.

    ``_collect_member_bodies`` records ``None`` for it, and the rule chosen is
    to treat that as a violation: a site with no body cannot satisfy an
    in-body marker requirement, and silently passing it would be a hole of
    exactly the shape this guard exists to close.
    """

    source = "import os\n\nBASE = os.path.realpath('/tmp')\n"
    missing = _members_missing_disposition("synthetic.py", source)
    assert [member for member, _ in missing] == [("synthetic.py", _MODULE_SCOPE)]
