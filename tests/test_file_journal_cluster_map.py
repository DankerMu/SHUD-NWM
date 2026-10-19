"""Function-level cluster map of the file orchestration journal (entropy-burndown-2026-10, task 5.0).

``tests/fixtures/file_journal_cluster_map.json`` assigns every top-level
definition of ``services/orchestrator/file_orchestration_journal.py`` to exactly
one of eight clusters -- ``facade`` plus the seven extraction clusters in D7
order (``context`` -> ``read_attribution`` -> ``primitives`` -> ``replay`` ->
``candidates`` -> ``paths`` -> ``retry_helpers``) -- and records, for every
name a test monkeypatches on the facade module, how the facade binds it, which
cluster owns it and which top-level units reference it.  Once this map is
merged it is authoritative: extraction PRs (5.2+) move exactly the names it
lists and may only *add* names for helpers created by a move, never reassign.

A top-level definition is a direct child of the module body that is a ``def``,
``async def``, ``class``, ``Assign`` or ``AnnAssign``; imports are bindings, not
definitions; a nested definition belongs to its outer unit; any other top-level
statement (``if``/``try``/``with`` ...) fails loudly.  In a sibling module an
``if TYPE_CHECKING:`` block may hold imports only.

Coverage follows the extraction (option a), so 5.2-5.5 leave this file
unchanged: cluster ``c`` lives in ``services/orchestrator/file_journal_<c>.py``
when that sibling exists and in the facade otherwise; every sibling's top-level
names equal ``clusters[c]`` and the facade's equal ``clusters["facade"]`` plus
every cluster that has no sibling yet.  Callers are collected from every host
file; inside a unit, an import of the facade module makes ``<alias>.<name>``
anywhere in that unit's subtree (closures included) a reference.  A sibling
must never bind a facade-resident patched name by import (top level or function
body): a bound copy escapes the facade monkeypatch.

The patched-name scanner reads every ``tests/**/*.py`` that mentions the facade
module and recognises:

* any call with a positional ``str`` argument starting with
  ``"services.orchestrator.file_orchestration_journal."`` (``mock.patch``,
  ``monkeypatch.setattr`` and custom helpers such as ``_patch(...)``); the first
  dotted segment after the prefix is the name;
* ``setattr`` / ``delattr`` / ``patch.object`` whose first argument is the facade
  module -- reached through ``import ... as``, ``from services.orchestrator
  import file_orchestration_journal [as X]``, the dotted module expression,
  ``importlib.import_module("...")``, a same-file helper that returns the module
  (``journal = _journal()``), plain re-assignment, or a function parameter that
  some same-file call passes the module to (``_record_lock_opens(monkeypatch,
  file_orchestration_journal)``) -- and whose second argument is a ``str``
  literal; a non-literal name there fails loudly.

Patches on an attribute of the module (``journal_module.os``,
``journal_module.FileOrchestrationJournalRepository``) and patches on other
modules are not facade patches.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MAP_PATH = REPO_ROOT / "tests" / "fixtures" / "file_journal_cluster_map.json"
FACADE = "services/orchestrator/file_orchestration_journal.py"
FACADE_MODULE = "services.orchestrator.file_orchestration_journal"
_PATCH_PREFIX = FACADE_MODULE + "."
CLUSTERS = (
    "facade",
    "context",
    "read_attribution",
    "primitives",
    "replay",
    "candidates",
    "paths",
    "retry_helpers",
)
EXTRACTION_ORDER = CLUSTERS[1:]
BINDINGS = ("def", "class", "assign", "import")
# The nine patched names whose placement task 5.0 fixes, plus the two classes
# that never move.
FACADE_PINNED = frozenset(
    {
        "_utcnow",
        "read_bytes_limited_no_follow",
        "list_directory_no_follow_limited",
        "_stat_signature",
        "_iter_regular_json_files",
        "_iter_jsonl_files",
        "_released_candidate_cycle_scope",
        "_cycle_scope_from_job_id",
        "_next_file_manual_retry_job_id_for_run",
        "FileOrchestrationJournalRepository",
        "FileJournalRetryService",
    }
)
_PRUNED_DIRS = frozenset({"__pycache__", ".venv", "node_modules", ".mypy_cache", ".pytest_cache"})


def sibling_path(root: Path, cluster: str) -> Path:
    return root / "services" / "orchestrator" / f"file_journal_{cluster}.py"


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


# ---------------------------------------------------------------------------
# Top-level units
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Unit:
    name: str
    kind: str  # "def" | "class" | "assign"
    node: ast.stmt


def _is_type_checking_block(node: ast.stmt) -> bool:
    if not isinstance(node, ast.If):
        return False
    test = node.test
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _assign_target_names(target: ast.expr, where: str) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for element in target.elts for name in _assign_target_names(element, where)]
    raise AssertionError(f"{where}: unsupported top-level assignment target {ast.dump(target)}")


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def top_level_units(root: Path, path: Path, *, sibling: bool) -> dict[str, Unit]:
    """Return ``name -> Unit`` for the direct def/class/Assign/AnnAssign children."""
    units: dict[str, Unit] = {}
    for index, node in enumerate(_parse(path).body):
        where = f"{_rel(root, path)}:{node.lineno}"
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if (
            index == 0
            and isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            continue
        if sibling and _is_type_checking_block(node):
            if node.orelse or not all(isinstance(child, (ast.Import, ast.ImportFrom)) for child in node.body):
                raise AssertionError(f"{where}: an `if TYPE_CHECKING:` block may hold imports only")
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind, names = "def", [node.name]
        elif isinstance(node, ast.ClassDef):
            kind, names = "class", [node.name]
        elif isinstance(node, ast.Assign):
            kind, names = "assign", [n for target in node.targets for n in _assign_target_names(target, where)]
        elif isinstance(node, ast.AnnAssign):
            kind, names = "assign", _assign_target_names(node.target, where)
        else:
            raise AssertionError(
                f"{where}: unsupported top-level statement {type(node).__name__}; "
                "the cluster map only models def/class/Assign/AnnAssign units"
            )
        for name in names:
            if name in units:
                raise AssertionError(f"{where}: top-level name `{name}` is defined twice")
            units[name] = Unit(name, kind, node)
    return units


def facade_bindings(root: Path) -> dict[str, set[str]]:
    """Return ``name -> {binding kinds}`` for every module-level binding of the facade."""
    bindings: dict[str, set[str]] = {}
    for node in _parse(root / FACADE).body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                bindings.setdefault(alias.asname or alias.name.split(".")[0], set()).add("import")
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bindings.setdefault(alias.asname or alias.name, set()).add("import")
    for unit in top_level_units(root, root / FACADE, sibling=False).values():
        bindings.setdefault(unit.name, set()).add(unit.kind)
    return bindings


# ---------------------------------------------------------------------------
# Coverage and late lookup (option a)
# ---------------------------------------------------------------------------


def _cluster_of(cluster_map: Mapping[str, object]) -> dict[str, str]:
    clusters = cluster_map["clusters"]
    assert isinstance(clusters, Mapping)
    return {name: cluster for cluster, names in clusters.items() for name in names}


def coverage_problems(root: Path, cluster_map: Mapping[str, object]) -> list[str]:
    clusters = cluster_map["clusters"]
    assert isinstance(clusters, Mapping)
    cluster_of = _cluster_of(cluster_map)
    problems: list[str] = []

    def compare(path: Path, actual: set[str], expected: set[str], owner: str) -> None:
        rel = _rel(root, path)
        for name in sorted(actual - expected):
            assigned = cluster_of.get(name)
            detail = "is not assigned to any cluster" if assigned is None else f"is mapped to cluster `{assigned}`"
            problems.append(f"{rel}: top-level definition `{name}` {detail}, but this file hosts {owner}")
        for name in sorted(expected - actual):
            problems.append(
                f"{rel}: map entry `{name}` (cluster `{cluster_of.get(name)}`) has no top-level definition here"
            )

    facade_expected = set(clusters["facade"])
    pending: list[str] = []
    for cluster in EXTRACTION_ORDER:
        path = sibling_path(root, cluster)
        if path.exists():
            actual = set(top_level_units(root, path, sibling=True))
            compare(path, actual, set(clusters[cluster]), f"cluster `{cluster}`")
        else:
            facade_expected |= set(clusters[cluster])
            pending.append(cluster)
    facade_actual = set(top_level_units(root, root / FACADE, sibling=False))
    owners = ", ".join(f"`{c}`" for c in ["facade", *pending])
    compare(root / FACADE, facade_actual, facade_expected, f"clusters {owners}")
    return problems


def late_lookup_problems(root: Path, cluster_map: Mapping[str, object]) -> list[str]:
    patched = cluster_map["patched"]
    assert isinstance(patched, Mapping)
    resident = {name for name, entry in patched.items() if entry["cluster"] == "facade"}
    problems: list[str] = []
    for cluster in EXTRACTION_ORDER:
        path = sibling_path(root, cluster)
        if not path.exists():
            continue
        for node in ast.walk(_parse(path)):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                for name in sorted({bound, alias.name} & resident):
                    problems.append(
                        f"{_rel(root, path)}:{node.lineno}: binds the facade-resident patched name `{name}` "
                        "by import; import the facade module inside the function body and call it as an attribute"
                    )
    return problems


# ---------------------------------------------------------------------------
# Callers
# ---------------------------------------------------------------------------

_ANNOTATION_FIELDS = {
    ast.arg: ("annotation",),
    ast.FunctionDef: ("returns",),
    ast.AsyncFunctionDef: ("returns",),
    ast.AnnAssign: ("annotation",),
}


def _runtime_nodes(node: ast.AST) -> Iterator[ast.AST]:
    """``ast.walk`` that skips annotation subtrees."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        skipped = _ANNOTATION_FIELDS.get(type(current), ())
        for field_name, value in ast.iter_fields(current):
            if field_name in skipped:
                continue
            if isinstance(value, ast.AST):
                stack.append(value)
            elif isinstance(value, list):
                stack.extend(item for item in value if isinstance(item, ast.AST))


def _dotted(node: ast.expr) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _facade_import_aliases(nodes: Iterable[ast.AST]) -> set[str]:
    aliases: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Import):
            aliases.update(a.asname for a in node.names if a.name == FACADE_MODULE and a.asname)
        elif isinstance(node, ast.ImportFrom):
            from_package = (node.level == 0 and node.module == "services.orchestrator") or (
                node.level == 1 and node.module is None
            )
            if from_package:
                aliases.update(a.asname or a.name for a in node.names if a.name == "file_orchestration_journal")
    return aliases


def _unit_references(node: ast.AST, aliases: set[str]) -> set[str]:
    referenced: set[str] = set()
    for child in _runtime_nodes(node):
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
            referenced.add(child.id)
        elif isinstance(child, ast.Attribute):
            if (isinstance(child.value, ast.Name) and child.value.id in aliases) or _dotted(
                child.value
            ) == FACADE_MODULE:
                referenced.add(child.attr)
    return referenced


@dataclass(frozen=True)
class HostUnit:
    cluster: str
    unit: Unit
    references: frozenset[str]


def host_units(root: Path, cluster_map: Mapping[str, object]) -> list[HostUnit]:
    """Every top-level unit of the facade and of every existing sibling, with its references."""
    cluster_of = _cluster_of(cluster_map)
    hosts = [(root / FACADE, False)]
    hosts += [(sibling_path(root, c), True) for c in EXTRACTION_ORDER if sibling_path(root, c).exists()]
    found: list[HostUnit] = []
    for path, sibling in hosts:
        module_aliases = _facade_import_aliases(
            n for n in _parse(path).body if isinstance(n, (ast.Import, ast.ImportFrom))
        )
        for unit in top_level_units(root, path, sibling=sibling).values():
            aliases = module_aliases | _facade_import_aliases(ast.walk(unit.node))
            found.append(
                HostUnit(
                    cluster_of.get(unit.name, "<unassigned>"), unit, frozenset(_unit_references(unit.node, aliases))
                )
            )
    return found


def derive_callers(
    root: Path, cluster_map: Mapping[str, object], names: Iterable[str]
) -> dict[str, dict[str, list[str]]]:
    wanted = set(names)
    callers: dict[str, dict[str, set[str]]] = {name: {} for name in wanted}
    for host in host_units(root, cluster_map):
        for name in host.references & wanted:
            callers[name].setdefault(host.cluster, set()).add(host.unit.name)
    return {name: {c: sorted(units) for c, units in by.items()} for name, by in callers.items()}


# ---------------------------------------------------------------------------
# Patched-name scanner
# ---------------------------------------------------------------------------


@dataclass(frozen=True, order=True)
class PatchSite:
    file: str
    lineno: int
    name: str


def _python_files(base: Path) -> Iterator[Path]:
    for path in sorted(base.rglob("*.py")):
        if not _PRUNED_DIRS.intersection(path.parts):
            yield path


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _scope_nodes(scope: ast.AST) -> tuple[list[ast.AST], list[ast.AST]]:
    """Split a scope into its own nodes and its directly nested scopes.

    Decorators, default values, bases and keywords of a nested def/class are
    evaluated in the enclosing scope, so they stay with it.
    """
    own: list[ast.AST] = []
    nested: list[ast.AST] = []
    stack: list[ast.AST] = list(scope.body) if isinstance(scope, (ast.Module, *_SCOPES)) else [scope]
    while stack:
        node = stack.pop()
        if isinstance(node, _SCOPES):
            nested.append(node)
            stack.extend(node.decorator_list)
            if isinstance(node, ast.ClassDef):
                stack.extend(node.bases)
                stack.extend(node.keywords)
            else:
                stack.extend(node.args.defaults)
                stack.extend(d for d in node.args.kw_defaults if d is not None)
            continue
        own.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return own, nested


def _is_patch_attribute_call(func: ast.expr) -> bool:
    if isinstance(func, ast.Name):
        return func.id in {"setattr", "delattr"}
    if isinstance(func, ast.Attribute):
        if func.attr in {"setattr", "delattr"}:
            return True
        if func.attr == "object":
            base = func.value
            return (isinstance(base, ast.Name) and base.id == "patch") or (
                isinstance(base, ast.Attribute) and base.attr == "patch"
            )
    return False


class _PatchScan:
    def __init__(self, tree: ast.Module, rel: str) -> None:
        self.tree = tree
        self.rel = rel
        self.functions: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions.setdefault(node.name, []).append(node)
        self.helpers: set[str] = set()
        self.param_aliases: dict[ast.AST, set[str]] = {}
        self.sites: set[PatchSite] = set()

    def run(self) -> set[PatchSite]:
        while True:
            state = (set(self.helpers), {k: set(v) for k, v in self.param_aliases.items()})
            self.sites = set()
            self._visit(self.tree, frozenset())
            if state == (self.helpers, self.param_aliases):
                return self.sites

    def _is_facade(self, expr: ast.expr | None, aliases: set[str]) -> bool:
        if expr is None:
            return False
        if isinstance(expr, ast.Name):
            return expr.id in aliases
        if isinstance(expr, ast.Attribute):
            return _dotted(expr) == FACADE_MODULE
        if isinstance(expr, ast.Call):
            func = expr.func
            if isinstance(func, ast.Name) and func.id in self.helpers:
                return True
            is_import_module = (isinstance(func, ast.Name) and func.id == "import_module") or (
                isinstance(func, ast.Attribute) and func.attr == "import_module"
            )
            return (
                is_import_module
                and bool(expr.args)
                and isinstance(expr.args[0], ast.Constant)
                and expr.args[0].value == FACADE_MODULE
            )
        return False

    def _visit(self, scope: ast.AST, inherited: frozenset[str]) -> None:
        own, nested = _scope_nodes(scope)
        aliases = set(inherited) | self.param_aliases.get(scope, set()) | _facade_import_aliases(own)
        changed = True
        while changed:
            changed = False
            for node in own:
                targets: list[ast.expr] = []
                if isinstance(node, ast.Assign) and self._is_facade(node.value, aliases):
                    targets = node.targets
                elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and self._is_facade(node.value, aliases):
                    targets = [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and target.id not in aliases:
                        aliases.add(target.id)
                        changed = True
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
            isinstance(node, ast.Return) and self._is_facade(node.value, aliases) for node in own
        ):
            self.helpers.add(scope.name)
        for node in own:
            if isinstance(node, ast.Call):
                self._record_call(node, aliases)
        for child in nested:
            self._visit(child, frozenset(aliases))

    def _record_call(self, call: ast.Call, aliases: set[str]) -> None:
        for arg in call.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.startswith(_PATCH_PREFIX):
                self.sites.add(PatchSite(self.rel, arg.lineno, arg.value[len(_PATCH_PREFIX) :].split(".")[0]))
        if _is_patch_attribute_call(call.func) and call.args and self._is_facade(call.args[0], aliases):
            if len(call.args) < 2 or not (
                isinstance(call.args[1], ast.Constant) and isinstance(call.args[1].value, str)
            ):
                raise AssertionError(
                    f"{self.rel}:{call.lineno}: facade patch whose attribute name is not a str literal"
                )
            self.sites.add(PatchSite(self.rel, call.lineno, call.args[1].value))
        if isinstance(call.func, ast.Name):
            for function in self.functions.get(call.func.id, []):
                params = [*function.args.posonlyargs, *function.args.args]
                by_name = {p.arg for p in [*params, *function.args.kwonlyargs]}
                marked = {
                    params[i].arg
                    for i, arg in enumerate(call.args)
                    if i < len(params) and self._is_facade(arg, aliases)
                }
                marked |= {kw.arg for kw in call.keywords if kw.arg in by_name and self._is_facade(kw.value, aliases)}
                if marked:
                    self.param_aliases.setdefault(function, set()).update(marked)


def scan_patch_sites(root: Path) -> list[PatchSite]:
    sites: set[PatchSite] = set()
    for path in _python_files(root / "tests"):
        text = path.read_text(encoding="utf-8")
        if "file_orchestration_journal" not in text:
            continue
        sites |= _PatchScan(ast.parse(text, filename=str(path)), _rel(root, path)).run()
    return sorted(sites)


# ---------------------------------------------------------------------------
# Map loading
# ---------------------------------------------------------------------------


def load_map() -> dict[str, object]:
    return json.loads(MAP_PATH.read_text(encoding="utf-8"))


def shape_problems(data: object) -> list[str]:
    problems: list[str] = []
    if not isinstance(data, dict):
        return ["map: top level must be an object"]
    if set(data) != {"module", "clusters", "patched"}:
        problems.append(f"map: top-level keys must be exactly module/clusters/patched, got {sorted(data)}")
    if data.get("module") != FACADE:
        problems.append(f"map: module must be {FACADE!r}, got {data.get('module')!r}")
    clusters = data.get("clusters")
    if not isinstance(clusters, dict):
        problems.append("map: clusters must be an object")
    else:
        for missing in sorted(set(CLUSTERS) - set(clusters)):
            problems.append(f"map: clusters is missing key `{missing}`")
        for extra in sorted(set(clusters) - set(CLUSTERS)):
            problems.append(f"map: clusters has unknown key `{extra}`")
        for cluster, names in clusters.items():
            if not isinstance(names, list) or not all(isinstance(n, str) and n for n in names):
                problems.append(f"map: clusters.{cluster} must be a list of non-empty strings")
            elif names != sorted(set(names)):
                problems.append(f"map: clusters.{cluster} must be sorted and unique")
    patched = data.get("patched")
    if not isinstance(patched, dict):
        problems.append("map: patched must be an object")
        return problems
    for name, entry in patched.items():
        if not isinstance(entry, dict) or set(entry) != {"binding", "cluster", "callers"}:
            problems.append(f"map: patched.{name} must have exactly binding/cluster/callers")
            continue
        if entry["binding"] not in BINDINGS:
            problems.append(f"map: patched.{name}.binding must be one of {BINDINGS}, got {entry['binding']!r}")
        if entry["cluster"] not in CLUSTERS:
            problems.append(f"map: patched.{name}.cluster `{entry['cluster']}` is not a cluster")
        callers = entry["callers"]
        if not isinstance(callers, dict):
            problems.append(f"map: patched.{name}.callers must be an object")
            continue
        for cluster, units in callers.items():
            if cluster not in CLUSTERS:
                problems.append(f"map: patched.{name}.callers has unknown cluster `{cluster}`")
            if not isinstance(units, list) or not units or not all(isinstance(u, str) and u for u in units):
                problems.append(f"map: patched.{name}.callers.{cluster} must be a non-empty list of strings")
            elif units != sorted(set(units)):
                problems.append(f"map: patched.{name}.callers.{cluster} must be sorted and unique")
    return problems


def placement_problems(root: Path, cluster_map: Mapping[str, object]) -> list[str]:
    clusters = cluster_map["clusters"]
    patched = cluster_map["patched"]
    assert isinstance(clusters, Mapping) and isinstance(patched, Mapping)
    cluster_of = _cluster_of(cluster_map)
    kind_of = {host.unit.name: host.unit.kind for host in host_units(root, cluster_map)}
    problems: list[str] = []
    for name in sorted(FACADE_PINNED):
        placed = patched[name]["cluster"] if name in patched else cluster_of.get(name)
        if placed != "facade":
            problems.append(f"`{name}` must stay in the facade, map places it in `{placed}`")
    for name, entry in sorted(patched.items()):
        cluster, callers = entry["cluster"], entry["callers"]
        if "facade" in callers and cluster != "facade":
            problems.append(
                f"patched `{name}` has facade callers {callers['facade']} and must be `facade`, not `{cluster}`"
            )
        if cluster != "facade" and set(callers) != {cluster}:
            problems.append(
                f"patched `{name}` in `{cluster}` must be called only from `{cluster}`, callers are {sorted(callers)}"
            )
        for caller_cluster, units in sorted(callers.items()):
            for unit in units:
                if kind_of.get(unit) == "assign" and caller_cluster != cluster:
                    problems.append(
                        f"patched `{name}` is read at import time by assignment `{unit}` in `{caller_cluster}` "
                        f"and must share its cluster, not `{cluster}`"
                    )
    return problems


# ---------------------------------------------------------------------------
# Real-tree rules
# ---------------------------------------------------------------------------


def test_map_shape_and_serialisation() -> None:
    data = load_map()
    assert shape_problems(data) == []
    assert MAP_PATH.read_text(encoding="utf-8") == json.dumps(data, indent=2, sort_keys=True) + "\n"


def test_every_top_level_definition_is_assigned_and_no_entry_is_stale() -> None:
    assert coverage_problems(REPO_ROOT, load_map()) == []


def test_every_name_sits_in_exactly_one_cluster() -> None:
    clusters = load_map()["clusters"]
    assert isinstance(clusters, dict)
    seen: dict[str, list[str]] = {}
    for cluster, names in clusters.items():
        for name in names:
            seen.setdefault(name, []).append(cluster)
    assert {name: owners for name, owners in seen.items() if len(owners) > 1} == {}


def test_patched_names_equal_the_scanner_result() -> None:
    sites = scan_patch_sites(REPO_ROOT)
    scanned = {site.name for site in sites}
    patched = set(load_map()["patched"])
    missing = {name: [f"{s.file}:{s.lineno}" for s in sites if s.name == name] for name in sorted(scanned - patched)}
    assert missing == {}, "patched on the facade by tests but absent from map.patched"
    assert sorted(patched - scanned) == [], "map.patched lists names no test patches"


def test_patched_bindings_match_the_facade() -> None:
    data = load_map()
    patched = data["patched"]
    assert isinstance(patched, dict)
    cluster_of = _cluster_of(data)
    bindings = facade_bindings(REPO_ROOT)
    problems: list[str] = []
    for name, entry in sorted(patched.items()):
        actual = bindings.get(name, set())
        if actual != {entry["binding"]}:
            problems.append(f"`{name}`: map binding `{entry['binding']}`, facade binds it as {sorted(actual)}")
        if entry["binding"] == "import":
            if name in cluster_of or entry["cluster"] != "facade":
                problems.append(f"`{name}` is import-bound: it must be absent from clusters and its cluster `facade`")
        elif cluster_of.get(name) != entry["cluster"]:
            problems.append(f"`{name}` must be listed in clusters.{entry['cluster']}, found in {cluster_of.get(name)}")
    assert problems == []


def test_patched_callers_match_the_ast() -> None:
    data = load_map()
    patched = data["patched"]
    assert isinstance(patched, dict)
    recorded = {name: entry["callers"] for name, entry in patched.items()}
    assert derive_callers(REPO_ROOT, data, patched) == recorded


def test_placement_rules() -> None:
    assert placement_problems(REPO_ROOT, load_map()) == []


def test_logger_users_stay_in_the_facade() -> None:
    # tests filter on record.name == the facade module; a moved LOGGER user would log under another name.
    users = sorted(
        (host.unit.name, host.cluster)
        for host in host_units(REPO_ROOT, load_map())
        if "LOGGER" in host.references or host.unit.name == "LOGGER"
    )
    assert users and all(cluster == "facade" for _, cluster in users), users


def test_siblings_never_bind_patched_names_by_import() -> None:
    assert late_lookup_problems(REPO_ROOT, load_map()) == []


# ---------------------------------------------------------------------------
# Synthetic trees (tmp_path): the rules that are vacuous until 5.2 lands
# ---------------------------------------------------------------------------

_FACADE_SRC = '''"""Synthetic facade."""
from __future__ import annotations

from packages.common.safe_fs import stat_no_follow

LIMIT = 3


def _stat_signature(path):
    return stat_no_follow(path)


class Repository:
    def read(self, path):
        return _stat_signature(path)
'''


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _synthetic_map(**clusters: list[str]) -> dict[str, object]:
    return {
        "module": FACADE,
        "clusters": {c: sorted(clusters.get(c, [])) for c in CLUSTERS},
        "patched": {"_stat_signature": {"binding": "def", "cluster": "facade", "callers": {}}},
    }


def test_synthetic_moved_cluster_passes(tmp_path: Path) -> None:
    _write(tmp_path, FACADE, _FACADE_SRC)
    _write(
        tmp_path,
        "services/orchestrator/file_journal_context.py",
        '"""Moved."""\nfrom __future__ import annotations\n\nfrom typing import TYPE_CHECKING\n\n'
        "if TYPE_CHECKING:\n    from pathlib import Path\n\n\n"
        "def helper(path):\n"
        "    from services.orchestrator import file_orchestration_journal\n\n"
        "    return file_orchestration_journal._stat_signature(path)\n",
    )
    cluster_map = _synthetic_map(facade=["LIMIT", "Repository", "_stat_signature"], context=["helper"])
    assert coverage_problems(tmp_path, cluster_map) == []
    assert late_lookup_problems(tmp_path, cluster_map) == []
    assert derive_callers(tmp_path, cluster_map, ["_stat_signature", "stat_no_follow"]) == {
        "_stat_signature": {"context": ["helper"], "facade": ["Repository"]},
        "stat_no_follow": {"facade": ["_stat_signature"]},
    }


def test_synthetic_pending_cluster_is_hosted_by_the_facade(tmp_path: Path) -> None:
    _write(tmp_path, FACADE, _FACADE_SRC + "\n\ndef helper():\n    return LIMIT\n")
    assert (
        coverage_problems(
            tmp_path, _synthetic_map(facade=["LIMIT", "Repository", "_stat_signature"], context=["helper"])
        )
        == []
    )
    stale = coverage_problems(
        tmp_path, _synthetic_map(facade=["LIMIT", "Repository", "_stat_signature", "gone"], context=["helper"])
    )
    assert len(stale) == 1 and "`gone`" in stale[0] and "no top-level definition" in stale[0]
    unassigned = coverage_problems(tmp_path, _synthetic_map(facade=["LIMIT", "Repository", "_stat_signature"]))
    assert len(unassigned) == 1 and "`helper` is not assigned" in unassigned[0]


def test_synthetic_sibling_binding_a_patched_name_by_import_fails(tmp_path: Path) -> None:
    _write(tmp_path, FACADE, _FACADE_SRC)
    _write(
        tmp_path,
        "services/orchestrator/file_journal_paths.py",
        "from services.orchestrator.file_orchestration_journal import _stat_signature\n\n\n"
        "def walk(path):\n    import services.orchestrator.file_orchestration_journal as _stat_signature\n\n"
        "    return _stat_signature\n",
    )
    problems = late_lookup_problems(
        tmp_path, _synthetic_map(facade=["LIMIT", "Repository", "_stat_signature"], paths=["walk"])
    )
    assert [p.split(": ", 1)[0] for p in problems] == [
        "services/orchestrator/file_journal_paths.py:1",
        "services/orchestrator/file_journal_paths.py:5",
    ]
    assert all("`_stat_signature`" in p for p in problems)


def test_synthetic_sibling_with_an_extra_definition_fails(tmp_path: Path) -> None:
    _write(tmp_path, FACADE, _FACADE_SRC)
    _write(tmp_path, "services/orchestrator/file_journal_replay.py", "def moved():\n    pass\n\n\nEXTRA = 1\n")
    problems = coverage_problems(
        tmp_path, _synthetic_map(facade=["LIMIT", "Repository", "_stat_signature"], replay=["moved"])
    )
    assert len(problems) == 1
    assert problems[0].startswith("services/orchestrator/file_journal_replay.py: top-level definition `EXTRA`")


def test_synthetic_sibling_missing_a_mapped_definition_fails(tmp_path: Path) -> None:
    _write(tmp_path, FACADE, _FACADE_SRC)
    _write(tmp_path, "services/orchestrator/file_journal_replay.py", "def moved():\n    pass\n")
    problems = coverage_problems(
        tmp_path, _synthetic_map(facade=["LIMIT", "Repository", "_stat_signature"], replay=["moved", "left_behind"])
    )
    assert problems == [
        "services/orchestrator/file_journal_replay.py: map entry `left_behind` (cluster `replay`) "
        "has no top-level definition here"
    ]


def test_synthetic_closure_alias_counts_for_the_outer_unit(tmp_path: Path) -> None:
    _write(tmp_path, FACADE, _FACADE_SRC)
    _write(
        tmp_path,
        "services/orchestrator/file_journal_paths.py",
        "def outer(path):\n"
        "    from services.orchestrator import file_orchestration_journal as fj\n\n"
        "    def walk():\n        return fj._stat_signature(path)\n\n"
        "    return walk\n\n\n"
        "def unrelated(fj):\n    return fj._stat_signature\n",
    )
    cluster_map = _synthetic_map(facade=["LIMIT", "Repository", "_stat_signature"], paths=["outer", "unrelated"])
    assert derive_callers(tmp_path, cluster_map, ["_stat_signature"]) == {
        "_stat_signature": {"facade": ["Repository"], "paths": ["outer"]}
    }


def test_synthetic_annotations_are_not_callers(tmp_path: Path) -> None:
    _write(
        tmp_path,
        FACADE,
        "from __future__ import annotations\n\n\nclass Repository:\n    pass\n\n\n"
        "def typed(repo: Repository) -> Repository:\n    value: Repository = repo\n    return value\n\n\n"
        "def built():\n    return Repository()\n",
    )
    cluster_map = _synthetic_map(facade=["Repository", "built", "typed"])
    assert derive_callers(tmp_path, cluster_map, ["Repository"]) == {"Repository": {"facade": ["built"]}}


def test_synthetic_unsupported_top_level_statement_fails_loudly(tmp_path: Path) -> None:
    _write(tmp_path, FACADE, "import os\n\nif os.name:\n    X = 1\n")
    try:
        coverage_problems(tmp_path, _synthetic_map())
    except AssertionError as error:
        assert "services/orchestrator/file_orchestration_journal.py:3: unsupported top-level statement If" in str(error)
    else:
        raise AssertionError("a top-level `if` must fail loudly")


def test_synthetic_placement_rules(tmp_path: Path) -> None:
    _write(
        tmp_path,
        FACADE,
        "LIMIT = 3\nSENTINEL = LIMIT + 1\n\n\ndef _stat_signature():\n    return LIMIT\n\n\n"
        "def leaf():\n    return _stat_signature()\n\n\n"
        "class FileOrchestrationJournalRepository:\n    pass\n\n\nclass FileJournalRetryService:\n    pass\n",
    )
    base = {
        "facade": ["FileJournalRetryService", "FileOrchestrationJournalRepository", "LIMIT", "SENTINEL"],
        "paths": ["_stat_signature", "leaf"],
    }
    cluster_map = _synthetic_map(**base)
    cluster_map["patched"] = {
        "_stat_signature": {"binding": "def", "cluster": "paths", "callers": {"paths": ["leaf"]}},
        "LIMIT": {
            "binding": "assign",
            "cluster": "paths",
            "callers": {"facade": ["SENTINEL"], "paths": ["_stat_signature"]},
        },
    }
    problems = placement_problems(tmp_path, cluster_map)
    assert "`_stat_signature` must stay in the facade, map places it in `paths`" in problems
    assert any(p.startswith("patched `LIMIT` has facade callers ['SENTINEL']") for p in problems)
    assert any(p.startswith("patched `LIMIT` in `paths` must be called only from `paths`") for p in problems)
    assert any(p.startswith("patched `LIMIT` is read at import time by assignment `SENTINEL`") for p in problems)


def test_scanner_recognises_every_patch_form(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "tests/test_forms.py",
        "import importlib\n"
        "from unittest import mock\n"
        "import services.orchestrator.file_orchestration_journal as journal_module\n"
        "from services.orchestrator import scheduler as scheduler_module\n"
        "\n"
        "LOGGER_NAME = 'services.orchestrator.file_orchestration_journal'\n"
        "PATH = 'services/orchestrator/file_orchestration_journal.py'\n"
        "\n"
        "\n"
        "def _patch(target, value):\n"
        "    return mock.patch(target, value)\n"
        "\n"
        "\n"
        "def _journal():\n"
        "    import services.orchestrator.file_orchestration_journal as module\n"
        "\n"
        "    return module\n"
        "\n"
        "\n"
        "def _record(monkeypatch, module):\n"
        "    monkeypatch.setattr(module, 'by_parameter', None)\n"
        "\n"
        "\n"
        "@mock.patch('services.orchestrator.file_orchestration_journal.by_decorator')\n"
        "def test_a(monkeypatch):\n"
        "    _patch('services.orchestrator.file_orchestration_journal.by_helper.attr', 1)\n"
        "    monkeypatch.setattr(journal_module, 'by_alias', 1)\n"
        "    monkeypatch.setattr(journal_module.os, 'stat', 1)\n"
        "    monkeypatch.setattr(journal_module.FileOrchestrationJournalRepository, '_read', 1)\n"
        "    monkeypatch.setattr(scheduler_module, '_utcnow', 1)\n"
        "    mock.patch.object(services.orchestrator.file_orchestration_journal, 'by_dotted')\n"
        "\n"
        "\n"
        "def test_b(monkeypatch):\n"
        "    from services.orchestrator import file_orchestration_journal\n"
        "    from services.orchestrator import file_orchestration_journal as fj\n"
        "\n"
        "    journal = _journal()\n"
        "    again = journal\n"
        "    loaded = importlib.import_module('services.orchestrator.file_orchestration_journal')\n"
        "    monkeypatch.setattr(journal, 'by_helper_return', 1)\n"
        "    monkeypatch.delattr(again, 'by_reassignment')\n"
        "    setattr(loaded, 'by_import_module', 1)\n"
        "    mock.patch.object(\n"
        "        fj,\n"
        "        'by_from_import_as',\n"
        "    )\n"
        "    _record(monkeypatch, file_orchestration_journal)\n"
        "\n"
        "\n"
        "def test_c(monkeypatch):\n"
        "    journal = object()\n"
        "    monkeypatch.setattr(journal, 'not_the_facade', 1)\n",
    )
    assert [(site.lineno, site.name) for site in scan_patch_sites(tmp_path)] == [
        (21, "by_parameter"),
        (24, "by_decorator"),
        (26, "by_helper"),
        (27, "by_alias"),
        (31, "by_dotted"),
        (41, "by_helper_return"),
        (42, "by_reassignment"),
        (43, "by_import_module"),
        (44, "by_from_import_as"),
    ]


def test_scanner_fails_loudly_on_a_non_literal_patch_name(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "tests/test_dynamic.py",
        "from services.orchestrator import file_orchestration_journal as fj\n\n\n"
        "def test(monkeypatch, name):\n    monkeypatch.setattr(fj, name, 1)\n",
    )
    try:
        scan_patch_sites(tmp_path)
    except AssertionError as error:
        assert str(error).startswith("tests/test_dynamic.py:5: facade patch whose attribute name is not a str literal")
    else:
        raise AssertionError("a non-literal facade patch name must fail loudly")
