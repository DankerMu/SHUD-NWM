"""Identity and explicit-import guard for ``scheduler_candidate_runtime`` (#3335 item 5).

OpenSpec change ``explicit-candidate-runtime-imports``, task 2.1.  The module
used to start with ``globals().update(_scheduler.__dict__)``: a snapshot of the
scheduler facade that took over its ``__name__`` / ``__file__`` / ``__spec__``
and hid which names it needs (ruff ``F821`` was silenced to allow it).  These
tests pin that the module keeps its own identity, binds each run-time name
explicitly from a module that binds the facade's object, carries no lint
exemption for undefined or unused names, and that the facade's surface did
not change.

The facade is imported before the module on purpose: a first import of
``scheduler_candidate_runtime`` re-enters the facade through its module-level
``importlib.import_module("services.orchestrator.scheduler_gateway")`` and
fails by design (allowlisted in ``tests/test_orchestrator_first_import.py``).
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
import tomllib
import types
from fnmatch import fnmatch
from pathlib import Path

# Order matters: the facade must be imported before the owner module.
from services.orchestrator import scheduler as scheduler_facade
from services.orchestrator import scheduler_candidate_runtime as candidate_runtime

MODULE_NAME = "services.orchestrator.scheduler_candidate_runtime"
FACADE_NAME = "services.orchestrator.scheduler"
REPO_ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATOR_DIR = REPO_ROOT / "services" / "orchestrator"
MODULE_REL_PATH = "services/orchestrator/scheduler_candidate_runtime.py"
MODULE_PATH = REPO_ROOT / MODULE_REL_PATH
FACADE_PATH = ORCHESTRATOR_DIR / "scheduler.py"

# The run-time names the module imports from orchestrator modules.  The
# standard-library names (Any, Callable, Mapping, Sequence, Path, importlib)
# are the same object everywhere and are not pinned.
RUNTIME_ORCHESTRATOR_NAMES = (
    "ForecastOrchestrator",
    "_ensure_utc",
    "_evidence_safe",
    "_scheduler_candidates",
    "_scheduler_discovery",
    "_scheduler_evidence",
    "_scheduler_preflight",
)
ANNOTATION_ONLY_NAMES = frozenset({"ProductionSchedulerConfig", "SchedulerCandidate"})
EXEMPTION_CODES = ("F821", "F401")


def _module_tree() -> ast.Module:
    return ast.parse(MODULE_PATH.read_text(encoding="utf-8"), filename=str(MODULE_PATH))


def _top_level_def_names(tree: ast.Module) -> list[str]:
    return [node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _facade_import_aliases() -> list[ast.alias]:
    tree = ast.parse(FACADE_PATH.read_text(encoding="utf-8"), filename=str(FACADE_PATH))
    aliases: list[ast.alias] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == MODULE_NAME and node.level == 0:
            aliases.extend(node.names)
    return aliases


def _code_exempts(selector: str, code: str) -> bool:
    selector = selector.strip().upper()
    return selector == "ALL" or (bool(selector) and code.startswith(selector))


def test_module_keeps_its_own_identity() -> None:
    """(a) ``__name__``, ``__file__`` and ``__spec__`` are the module's own."""
    assert candidate_runtime.__name__ == MODULE_NAME
    assert candidate_runtime.__file__ is not None
    assert candidate_runtime.__file__.endswith("scheduler_candidate_runtime.py")
    assert candidate_runtime.__spec__ is not None
    assert candidate_runtime.__spec__.name == MODULE_NAME


def test_functions_defined_in_module_report_it_as_their_module() -> None:
    """(b) Every function the file defines reports the module as ``__module__``.

    The ``def`` names come from the source AST.  Forwarder functions the
    module's factories create and bind by assignment are included: their code
    lives in this file, so they must report this module too.
    """
    def_names = _top_level_def_names(_module_tree())
    assert def_names, "no module-level def found in the source"
    wrong = {
        name: getattr(candidate_runtime, name).__module__
        for name in def_names
        if getattr(candidate_runtime, name).__module__ != MODULE_NAME
    }
    module_file = str(MODULE_PATH.resolve())
    for name, value in vars(candidate_runtime).items():
        if not isinstance(value, types.FunctionType):
            continue
        if str(Path(value.__code__.co_filename).resolve()) != module_file:
            continue
        if value.__module__ != MODULE_NAME:
            wrong[name] = value.__module__
    assert wrong == {}


def _globals_update_calls(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines: list[int] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "update"):
            continue
        receiver = node.func.value
        if isinstance(receiver, ast.Call) and isinstance(receiver.func, ast.Name) and receiver.func.id == "globals":
            lines.append(node.lineno)
    return lines


def test_no_orchestrator_source_copies_a_namespace_into_globals() -> None:
    """(c) No source under ``services/orchestrator`` calls ``globals().update(...)``."""
    sources = sorted(ORCHESTRATOR_DIR.rglob("*.py"))
    assert MODULE_PATH in sources
    hits = {str(path.relative_to(REPO_ROOT)): lines for path in sources if (lines := _globals_update_calls(path))}
    assert hits == {}


def test_module_holds_no_facade_binding() -> None:
    """The module neither imports the facade nor keeps a reference to it."""
    facade_imports: list[int] = []
    for node in ast.walk(_module_tree()):
        if isinstance(node, ast.Import):
            if any(alias.name == FACADE_NAME for alias in node.names):
                facade_imports.append(node.lineno)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            if node.module == FACADE_NAME:
                facade_imports.append(node.lineno)
            elif node.module == "services.orchestrator" and any(alias.name == "scheduler" for alias in node.names):
                facade_imports.append(node.lineno)
    assert facade_imports == []
    assert [name for name, value in vars(candidate_runtime).items() if value is scheduler_facade] == []


def test_facade_names_imported_from_module_are_the_modules_objects() -> None:
    """(d) Each name in the facade's import block from the module is the module's object."""
    aliases = _facade_import_aliases()
    assert aliases, "facade has no `from services.orchestrator.scheduler_candidate_runtime import (...)` block"
    missing = [alias.name for alias in aliases if not hasattr(candidate_runtime, alias.name)]
    assert missing == []
    different = [
        alias.name
        for alias in aliases
        if getattr(candidate_runtime, alias.name) is not getattr(scheduler_facade, alias.asname or alias.name)
    ]
    assert different == []


_FILE_NOQA_RE = re.compile(r"#\s*(?:ruff|flake8)\s*:\s*noqa(?:\s*:\s*(?P<codes>[A-Za-z0-9,\s]+))?", re.IGNORECASE)
_LINE_NOQA_RE = re.compile(r"#\s*noqa(?:\s*:\s*(?P<codes>[A-Za-z0-9,\s]+))?", re.IGNORECASE)


def _comment_exemptions(source: str) -> list[tuple[int, str, str]]:
    found: list[tuple[int, str, str]] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.COMMENT:
            continue
        file_match = _FILE_NOQA_RE.search(token.string)
        match = file_match or _LINE_NOQA_RE.search(token.string)
        if match is None:
            continue
        codes = match.group("codes")
        # A bare `noqa` exempts every rule.
        selectors = ["ALL"] if codes is None else re.split(r"[,\s]+", codes.strip())
        for code in EXEMPTION_CODES:
            if any(_code_exempts(selector, code) for selector in selectors):
                found.append((token.start[0], code, token.string))
    return found


def _pyproject_exemptions() -> list[tuple[str, str, str]]:
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    ruff = config.get("tool", {}).get("ruff", {})
    lint = ruff.get("lint", {})
    found: list[tuple[str, str, str]] = []
    for table_name, table in (
        ("tool.ruff.per-file-ignores", ruff.get("per-file-ignores", {})),
        ("tool.ruff.extend-per-file-ignores", ruff.get("extend-per-file-ignores", {})),
        ("tool.ruff.lint.per-file-ignores", lint.get("per-file-ignores", {})),
        ("tool.ruff.lint.extend-per-file-ignores", lint.get("extend-per-file-ignores", {})),
    ):
        for pattern, selectors in table.items():
            if not (pattern == MODULE_REL_PATH or fnmatch(MODULE_REL_PATH, pattern) or pattern == MODULE_PATH.name):
                continue
            for code in EXEMPTION_CODES:
                if any(_code_exempts(selector, code) for selector in selectors):
                    found.append((table_name, pattern, code))
    for table_name, selectors in (
        ("tool.ruff.ignore", ruff.get("ignore", [])),
        ("tool.ruff.extend-ignore", ruff.get("extend-ignore", [])),
        ("tool.ruff.lint.ignore", lint.get("ignore", [])),
        ("tool.ruff.lint.extend-ignore", lint.get("extend-ignore", [])),
    ):
        for code in EXEMPTION_CODES:
            if any(_code_exempts(selector, code) for selector in selectors):
                found.append((table_name, "*", code))
    return found


def test_module_carries_no_undefined_or_unused_name_exemption() -> None:
    """(e) No ``F821`` / ``F401`` exemption: not on a ``noqa`` comment, not in ``pyproject.toml``."""
    assert _comment_exemptions(MODULE_PATH.read_text(encoding="utf-8")) == []
    assert _pyproject_exemptions() == []


def test_runtime_orchestrator_names_are_the_facades_objects() -> None:
    """(f) Each orchestrator name the module uses at run time is the facade's object.

    Ruff cannot tell a same-named helper from the wrong module apart (several
    modules define their own ``_ensure_utc``), so identity is pinned here.
    """
    different = [
        name
        for name in RUNTIME_ORCHESTRATOR_NAMES
        if getattr(candidate_runtime, name) is not getattr(scheduler_facade, name)
    ]
    assert different == []


def _is_type_checking_test(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "TYPE_CHECKING"
    return isinstance(node, ast.Attribute) and node.attr == "TYPE_CHECKING"


def test_annotation_only_classes_are_used_only_in_annotations() -> None:
    """(g) ``ProductionSchedulerConfig`` / ``SchedulerCandidate`` appear only in annotations.

    Ruff treats a ``TYPE_CHECKING`` import as bound, so a run-time use of
    either class would pass lint and fail with ``NameError`` on that path.
    Any import of them must sit under ``if TYPE_CHECKING:``.
    """
    tree = _module_tree()
    annotation_node_ids: set[int] = set()
    for node in ast.walk(tree):
        annotations: list[ast.expr | None] = []
        if isinstance(node, ast.arg):
            annotations.append(node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            annotations.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)
        for annotation in annotations:
            if annotation is not None:
                annotation_node_ids.update(id(sub) for sub in ast.walk(annotation))

    guarded_import_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _is_type_checking_test(node.test):
            guarded_import_ids.update(
                id(sub) for stmt in node.body for sub in ast.walk(stmt) if isinstance(sub, (ast.Import, ast.ImportFrom))
            )

    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in ANNOTATION_ONLY_NAMES and id(node) not in annotation_node_ids:
            violations.append((node.lineno, f"run-time use of {node.id}"))
        elif isinstance(node, (ast.Import, ast.ImportFrom)) and id(node) not in guarded_import_ids:
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                if bound in ANNOTATION_ONLY_NAMES:
                    violations.append((node.lineno, f"import of {bound} outside TYPE_CHECKING"))
    assert violations == []
