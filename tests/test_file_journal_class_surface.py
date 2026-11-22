"""Class-surface characterization guard of the file orchestration journal (#3275, task 13.1).

``tests/fixtures/file_journal_class_surface.json`` pins four sorted lists taken
from the live code.  J1-J5 move methods of ``FileOrchestrationJournalRepository``
and ``FileJournalRetryService`` into ``file_journal_<cluster>.py`` mixin bases and
top-level units into siblings that the facade re-imports.  A pure move preserves
all four lists, so those slices leave the data file unchanged; a deliberate
change regenerates it and shows up in the diff.

1. ``class_members``: every non-dunder name in ``dir(cls)``, resolved to its raw
   ``vars(base)`` entry through ``cls.__mro__`` (``object`` excluded), with its
   kind and the normalised signature of the underlying function (``__func__``
   for static and class methods, ``fget`` for properties plus which accessors
   exist; ``inspect.signature`` follows ``__wrapped__``) or, for a value, its
   type name and ``repr``.  Neither the owning base nor ``__qualname__`` is
   recorded.  A default renders as ``facade.NAME`` when its ``repr`` is an
   identity repr (`` at 0x``) and it *is* the facade's module-level object
   ``NAME`` (today ``_UNSET``); every other default renders as ``repr``.  No row
   may contain `` at 0x``, so a mixin with its own sentinel fails.
   Annotations stay the strings ``from __future__ import annotations`` produces;
   a signature diff caused by an evaluated annotation is fixed in the mixin,
   never by regenerating.
2. ``read_attribution``: every public plain function resolved the same way is
   either an attribution wrapper (its ``__code__`` is the inner function of
   ``_attributed_public_method``; ``functools.wraps`` copies ``__qualname__``, so
   the code object is the only reliable mark) or a function the installer
   deliberately skips (a generator, or one already carrying a foreign
   ``__wrapped__``).  A public method moved into an undecorated mixin is neither,
   because the installer only wraps ``vars(cls)``.
3. ``external_private_attributes``: the private class attributes that the D4
   modules reach as ``X._name``, where ``_name`` resolves on either class and is
   not defined by the scanned module itself.
4. ``facade_imports``: the facade's import contract, ``name tag source`` with the
   coarse runtime tag ``class`` / ``function`` / ``value`` and the source ``a``,
   ``b`` or ``a+b``.  (a) is every public name the facade defines at top level
   by ``def``/``class``/``Assign``/``AnnAssign``, plus every already-pinned (a)
   name the facade now from-imports from a ``file_journal_*`` sibling, so a pure
   move keeps it while a new public definition fails.  (b) is every name a
   production module outside the facade and its siblings imports from the
   facade or reads as ``<facade alias>.X``.  Names the facade imports only for
   its own use are not pinned.  Every pinned name must resolve on the facade
   with the same tag.

Regenerate with ``NHMS_REGENERATE_CLASS_SURFACE=1 uv run pytest -q
tests/test_file_journal_class_surface.py``; that run always fails, rerun
without the variable.  This module never patches the facade nor reads it as
``facade.X`` / ``getattr(facade, ...)``: the cluster map's patch scanner and the
facade's ``__all__`` hygiene scan read every test that mentions it, so the module
object is only ever passed to helpers as a parameter.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import ModuleType

import pytest

from services.orchestrator import file_orchestration_journal as facade
from services.orchestrator.file_journal_read_attribution import (
    _attributed_public_method,
    _install_public_read_attribution,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = REPO_ROOT / "tests" / "fixtures" / "file_journal_class_surface.json"
REGENERATE_ENV = "NHMS_REGENERATE_CLASS_SURFACE"
FACADE = "services/orchestrator/file_orchestration_journal.py"
FACADE_MODULE = "services.orchestrator.file_orchestration_journal"
SIBLING_MODULE_PREFIX = "services.orchestrator.file_journal_"
SETS = ("class_members", "read_attribution", "external_private_attributes", "facade_imports")
CLASS_NAMES = ("FileOrchestrationJournalRepository", "FileJournalRetryService")
# The four external modules design D4 names as reaching into the journal classes.
D4_MODULES = (
    "scripts/ops/node22_repair_placeholder_hydro_uris.py",
    "services/orchestrator/file_orchestration_migration.py",
    "services/orchestrator/journal_scope_census.py",
    "services/orchestrator/scheduler_state_manual_retry.py",
)
PRODUCTION_DIRS = ("services", "apps", "scripts", "workers", "packages")
_PRUNED_DIRS = frozenset({"__pycache__", ".venv", "node_modules", ".mypy_cache", ".pytest_cache", ".ruff_cache"})
_WRAPPER_CODE = _attributed_public_method("probe", lambda self: None).__code__
_IDENTITY_REPR = " at 0x"
_MISSING = object()


# The facade is always handed to helpers as a parameter, never read as ``facade.X``
# or ``getattr(facade, name)``: ``test_file_orchestration_journal_facade.py`` scans
# every test for reads of the facade's surface.
def _classes_of(module: ModuleType) -> dict[str, type]:
    return {name: getattr(module, name) for name in CLASS_NAMES}


def _journal_classes() -> dict[str, type]:
    return _classes_of(facade)


def _resolve(cls: type, name: str) -> object:
    for base in cls.__mro__:
        if base is not object and name in vars(base):
            return vars(base)[name]
    return _MISSING


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


# ---------------------------------------------------------------------------
# Set 1: class members
# ---------------------------------------------------------------------------


class _Rendered:
    """A default whose ``repr`` is already the normalised text."""

    def __init__(self, text: str) -> None:
        self.text = text

    def __repr__(self) -> str:
        return self.text


def _signature(func: object, module: ModuleType) -> str:
    if func is None:
        return "-"
    named: dict[int, str] = {}
    for name, value in sorted(vars(module).items()):
        named.setdefault(id(value), name)
    signature = inspect.signature(func)
    parameters = []
    for parameter in signature.parameters.values():
        default = parameter.default
        if (
            default is not inspect.Parameter.empty
            and _IDENTITY_REPR in repr(default)
            and id(default) in named
            and getattr(module, named[id(default)]) is default
        ):
            parameter = parameter.replace(default=_Rendered(f"facade.{named[id(default)]}"))
        parameters.append(parameter)
    return str(signature.replace(parameters=parameters))


def class_member_rows(classes: Mapping[str, type], module: ModuleType) -> list[str]:
    rows: list[str] = []
    for class_name, cls in classes.items():
        for name in dir(cls):
            if _is_dunder(name):
                continue
            raw = _resolve(cls, name)
            if raw is _MISSING:
                continue
            if isinstance(raw, staticmethod):
                description = f"staticmethod {_signature(raw.__func__, module)}"
            elif isinstance(raw, classmethod):
                description = f"classmethod {_signature(raw.__func__, module)}"
            elif isinstance(raw, property):
                accessors = "+".join(
                    label
                    for label, accessor in (("get", raw.fget), ("set", raw.fset), ("del", raw.fdel))
                    if accessor is not None
                )
                description = f"property({accessors}) {_signature(raw.fget, module)}"
            elif inspect.isfunction(raw):
                description = f"function {_signature(raw, module)}"
            else:
                description = f"value {type(raw).__name__} {raw!r}"
            row = f"{class_name}.{name} {description}"
            assert _IDENTITY_REPR not in row, (
                f"{row}: an identity repr is not a stable surface; a sentinel default must be the very "
                "object the facade binds at module level (keep it imported on the facade), never a copy "
                "defined by a mixin"
            )
            rows.append(row)
    return sorted(rows)


# ---------------------------------------------------------------------------
# Set 2: read attribution
# ---------------------------------------------------------------------------


def read_attribution_rows(classes: Mapping[str, type]) -> list[str]:
    rows: list[str] = []
    for class_name, cls in classes.items():
        for name in dir(cls):
            if name.startswith("_"):
                continue
            raw = _resolve(cls, name)
            if not inspect.isfunction(raw):
                continue
            if raw.__code__ is _WRAPPER_CODE:
                status = "wrapper"
            elif inspect.isgeneratorfunction(raw):
                status = "skip:generator"
            elif getattr(raw, "__wrapped__", None) is not None:
                status = "skip:foreign-wrapped"
            else:
                status = "UNATTRIBUTED"
            rows.append(f"{class_name}.{name} {status}")
    return sorted(rows)


# ---------------------------------------------------------------------------
# Set 3: private class attributes reached by the D4 modules
# ---------------------------------------------------------------------------


def _own_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Store)
            and isinstance(node.value, ast.Name)
            and node.value.id in {"self", "cls"}
        ):
            names.add(node.attr)
    return names


def external_private_attribute_rows(root: Path, classes: Mapping[str, type]) -> list[str]:
    rows: set[str] = set()
    for rel in D4_MODULES:
        tree = _parse(root / rel)
        own = _own_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            name = node.attr
            if not name.startswith("_") or _is_dunder(name) or name in own:
                continue
            for class_name, cls in classes.items():
                if _resolve(cls, name) is not _MISSING:
                    rows.add(f"{class_name}.{name} {rel}")
    return sorted(rows)


# ---------------------------------------------------------------------------
# Set 4: the facade's import contract
# ---------------------------------------------------------------------------


def _assign_names(target: ast.expr) -> Iterator[str]:
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            yield from _assign_names(element)


def facade_defined_public_names(root: Path) -> set[str]:
    names: set[str] = set()
    for node in _parse(root / FACADE).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(name for target in node.targets for name in _assign_names(target))
        elif isinstance(node, ast.AnnAssign):
            names.update(_assign_names(node.target))
    return {name for name in names if not name.startswith("_")}


def facade_sibling_imported_names(root: Path) -> set[str]:
    names: set[str] = set()
    for node in _parse(root / FACADE).body:
        if not isinstance(node, ast.ImportFrom) or node.module is None:
            continue
        absolute = node.level == 0 and node.module.startswith(SIBLING_MODULE_PREFIX)
        relative = node.level == 1 and node.module.startswith("file_journal_")
        if absolute or relative:
            names.update(alias.asname or alias.name for alias in node.names)
    return names


def _production_files(root: Path) -> Iterator[tuple[str, Path]]:
    for base in PRODUCTION_DIRS:
        for directory, subdirs, files in os.walk(root / base):
            subdirs[:] = sorted(d for d in subdirs if d not in _PRUNED_DIRS)
            for file_name in sorted(files):
                if not file_name.endswith(".py"):
                    continue
                path = Path(directory) / file_name
                rel = path.relative_to(root).as_posix()
                if rel == FACADE or (rel.startswith("services/orchestrator/file_journal_")):
                    continue
                yield rel, path


def production_facade_names(root: Path) -> set[str]:
    names: set[str] = set()
    for rel, path in _production_files(root):
        text = path.read_text(encoding="utf-8")
        if "file_orchestration_journal" not in text:
            continue
        tree = ast.parse(text, filename=str(path))
        in_package = rel.startswith("services/orchestrator/") and rel.count("/") == 2
        aliases: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                direct = (node.level == 0 and node.module == FACADE_MODULE) or (
                    in_package and node.level == 1 and node.module == "file_orchestration_journal"
                )
                package = (node.level == 0 and node.module == "services.orchestrator") or (
                    in_package and node.level == 1 and node.module is None
                )
                for alias in node.names:
                    if direct:
                        assert alias.name != "*", f"{rel}:{node.lineno}: star import from the facade"
                        names.add(alias.name)
                    elif package and alias.name == "file_orchestration_journal":
                        aliases.add(alias.asname or alias.name)
            elif isinstance(node, ast.Import):
                aliases.update(a.asname for a in node.names if a.name == FACADE_MODULE and a.asname)
            elif (
                isinstance(node, ast.Call)
                and any(isinstance(a, ast.Constant) and a.value == FACADE_MODULE for a in node.args)
                and "import_module" in ast.unparse(node.func)
            ):
                raise AssertionError(f"{rel}:{node.lineno}: dynamic import of the facade is invisible to this scan")
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and (
                (isinstance(node.value, ast.Name) and node.value.id in aliases)
                or ast.unparse(node.value) == FACADE_MODULE
            ):
                names.add(node.attr)
    return names


def _tag(value: object) -> str:
    if value is _MISSING:
        return "MISSING"
    if inspect.isclass(value):
        return "class"
    if inspect.isroutine(value):
        return "function"
    return "value"


def facade_import_rows(root: Path, module: ModuleType, pinned_defined: set[str]) -> list[str]:
    defined = facade_defined_public_names(root) | (facade_sibling_imported_names(root) & pinned_defined)
    imported = production_facade_names(root)
    rows = []
    for name in sorted(defined | imported):
        source = "+".join(label for label, group in (("a", defined), ("b", imported)) if name in group)
        rows.append(f"{name} {_tag(getattr(module, name, _MISSING))} {source}")
    return rows


def _pinned_defined(data: Mapping[str, list[str]]) -> set[str]:
    return {row.split(" ")[0] for row in data.get("facade_imports", []) if row.split(" ")[2].startswith("a")}


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def live_surface(root: Path, pinned_defined: set[str]) -> dict[str, list[str]]:
    classes = _journal_classes()
    return {
        "class_members": class_member_rows(classes, facade),
        "read_attribution": read_attribution_rows(classes),
        "external_private_attributes": external_private_attribute_rows(root, classes),
        "facade_imports": facade_import_rows(root, facade, pinned_defined),
    }


def _serialise(data: Mapping[str, list[str]]) -> str:
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def regenerate() -> None:
    previous = json.loads(DATA_PATH.read_text(encoding="utf-8")) if DATA_PATH.exists() else {}
    data = live_surface(REPO_ROOT, _pinned_defined(previous))
    unattributed = [row for row in data["read_attribution"] if row.endswith(" UNATTRIBUTED")]
    assert unattributed == [], "a public method lost read attribution; decorate its mixin, do not pin it"
    missing = [row for row in data["facade_imports"] if " MISSING " in row]
    assert missing == [], "a facade import-contract name no longer resolves on the facade"
    DATA_PATH.write_text(_serialise(data), encoding="utf-8")


@pytest.fixture(scope="module")
def pinned() -> dict[str, list[str]]:
    if os.environ.get(REGENERATE_ENV) == "1":
        regenerate()
        pytest.fail("regenerated; rerun without the env var")
    return json.loads(DATA_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Real-tree rules
# ---------------------------------------------------------------------------


def test_data_file_shape_and_serialisation(pinned: dict[str, list[str]]) -> None:
    assert sorted(pinned) == sorted(SETS)
    for key in SETS:
        rows = pinned[key]
        assert all(isinstance(row, str) and row for row in rows), key
        assert rows == sorted(set(rows)), f"{key} must be sorted and unique"
    assert DATA_PATH.read_text(encoding="utf-8") == _serialise(pinned)


def test_class_members_are_unchanged(pinned: dict[str, list[str]]) -> None:
    assert class_member_rows(_journal_classes(), facade) == pinned["class_members"]


def test_public_methods_keep_read_attribution(pinned: dict[str, list[str]]) -> None:
    live = read_attribution_rows(_journal_classes())
    assert [row for row in live if row.endswith(" UNATTRIBUTED")] == [], (
        "public methods outside the attribution wrapper: decorate the mixin that defines them "
        "with _install_public_read_attribution"
    )
    assert live == pinned["read_attribution"]


def test_external_private_attributes_still_resolve(pinned: dict[str, list[str]]) -> None:
    classes = _journal_classes()
    unresolved = []
    for row in pinned["external_private_attributes"]:
        class_name, name = row.split(" ")[0].split(".")
        if _resolve(classes[class_name], name) is _MISSING:
            unresolved.append(row)
    assert unresolved == [], "a private attribute an external module reaches no longer resolves on its class"
    assert external_private_attribute_rows(REPO_ROOT, classes) == pinned["external_private_attributes"]


def _drifted(rows: list[str], module: ModuleType) -> list[str]:
    return [row for row in rows if _tag(getattr(module, row.split(" ")[0], _MISSING)) != row.split(" ")[1]]


def test_facade_import_contract(pinned: dict[str, list[str]]) -> None:
    drifted = _drifted(pinned["facade_imports"], facade)
    assert drifted == [], "pinned facade names that no longer resolve on the facade with the same tag"
    assert facade_import_rows(REPO_ROOT, facade, _pinned_defined(pinned)) == pinned["facade_imports"]


# ---------------------------------------------------------------------------
# Synthetic classes: the J1-J5 mixin contract
# ---------------------------------------------------------------------------


def _scan(self):
    yield self


def _build(*, decorated_mixin: bool, own_sentinel: bool = False) -> dict[str, dict[str, type]]:
    """The same surface defined flat and split across a mixin base."""

    sentinel = object() if own_sentinel else vars(facade)["_UNSET"]

    def read(self, key: str, *, default: object = sentinel) -> str | None:
        return None

    def helper(value: int) -> int:
        return value

    flat = _install_public_read_attribution(
        type("Journal", (), {"read": read, "scan": _scan, "_helper": staticmethod(helper), "LIMIT": 3})
    )
    mixin = type("_ReadMixin", (), {"read": read})
    if decorated_mixin:
        mixin = _install_public_read_attribution(mixin)
    split = _install_public_read_attribution(
        type("Journal", (mixin,), {"scan": _scan, "_helper": staticmethod(helper), "LIMIT": 3})
    )
    return {"flat": {"Journal": flat}, "split": {"Journal": split}}


def test_synthetic_decorated_mixin_move_preserves_sets_1_and_2() -> None:
    built = _build(decorated_mixin=True)
    assert class_member_rows(built["split"], facade) == class_member_rows(built["flat"], facade)
    assert (
        read_attribution_rows(built["split"])
        == read_attribution_rows(built["flat"])
        == [
            "Journal.read wrapper",
            "Journal.scan skip:generator",
        ]
    )
    assert "Journal.read function (self, key: 'str', *, default: 'object' = facade._UNSET) -> 'str | None'" in (
        class_member_rows(built["flat"], facade)
    )


def test_synthetic_undecorated_mixin_move_fails_set_2() -> None:
    built = _build(decorated_mixin=False)
    assert class_member_rows(built["split"], facade) == class_member_rows(built["flat"], facade)
    assert "Journal.read UNATTRIBUTED" in read_attribution_rows(built["split"])


def test_synthetic_mixin_sentinel_fails_set_1() -> None:
    built = _build(decorated_mixin=True, own_sentinel=True)
    with pytest.raises(AssertionError, match="identity repr"):
        class_member_rows(built["split"], facade)
