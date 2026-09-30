"""Driver guard (#2632, design D3): every engine names its PostgreSQL DBAPI.

SQLAlchemy 2.1 resolves a plain ``postgresql://`` URL to psycopg (v3), which the
project does not install. ``packages.common.sqlalchemy_url.sqlalchemy_url`` pins
psycopg2; this AST scan fails, naming ``path:line``, on any ``create_engine`` /
``*.create_engine`` call under the scanned trees whose URL argument is neither a
``sqlalchemy_url(...)`` call nor a literal ``sqlite`` URL.

Honest limits: the scan is syntactic. It does not follow a URL through a local
variable (``url = sqlalchemy_url(x); create_engine(url)`` is flagged — write the
call inline), nor see ``create_engine`` re-bound under another name. Engines
built outside the scanned trees (ad-hoc operator scripts, openspec tools) are
outside the guard.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Scanned trees, walked whole, plus the one shared test helper that builds a
# real PostgreSQL engine. scripts/select_ci_tests.py mirrors this binding in
# SQLALCHEMY_DRIVER_GUARD_ROOTS / SQLALCHEMY_DRIVER_GUARD_FILES.
SCAN_ROOTS: tuple[str, ...] = ("apps", "packages", "services", "workers", "scripts")
SCAN_FILES: tuple[str, ...] = ("tests/integration_helpers.py",)
PRUNED_DIRECTORIES: frozenset[str] = frozenset(
    {"__pycache__", ".git", ".venv", "node_modules", "dist", "build", ".mypy_cache"}
)

HELPER_NAME = "sqlalchemy_url"


def _iter_scanned_sources(repo_root: Path) -> Iterator[Path]:
    for root in SCAN_ROOTS:
        base = repo_root / root
        for path in sorted(base.rglob("*.py")):
            relative = path.relative_to(repo_root)
            if any(part in PRUNED_DIRECTORIES for part in relative.parts[:-1]):
                continue
            yield path
    for name in SCAN_FILES:
        yield repo_root / name


def _is_create_engine(func: ast.expr) -> bool:
    if isinstance(func, ast.Name):
        return func.id == "create_engine"
    return isinstance(func, ast.Attribute) and func.attr == "create_engine"


def _is_helper_call(node: ast.expr) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == HELPER_NAME
    return isinstance(func, ast.Attribute) and func.attr == HELPER_NAME


def _is_sqlite_literal(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.startswith("sqlite")
    if isinstance(node, ast.JoinedStr) and node.values:
        head = node.values[0]
        return isinstance(head, ast.Constant) and isinstance(head.value, str) and head.value.startswith("sqlite")
    return False


def _url_argument(call: ast.Call) -> ast.expr | None:
    if call.args:
        return call.args[0]
    for keyword in call.keywords:
        if keyword.arg == "url":
            return keyword.value
    return None


def unexplicit_engine_sites(source: str, *, filename: str) -> list[str]:
    """``filename:line`` for each ``create_engine`` call without an explicit driver."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if not isinstance(node, ast.Call) or not _is_create_engine(node.func):
            continue
        argument = _url_argument(node)
        if argument is not None and (_is_helper_call(argument) or _is_sqlite_literal(argument)):
            continue
        lines.append(node.lineno)
    return [f"{filename}:{line}" for line in sorted(lines)]


def _scan(repo_root: Path) -> tuple[int, list[str]]:
    files = 0
    offenders: list[str] = []
    for path in _iter_scanned_sources(repo_root):
        files += 1
        relative = path.relative_to(repo_root).as_posix()
        offenders.extend(unexplicit_engine_sites(path.read_text(encoding="utf-8"), filename=relative))
    return files, offenders


def test_every_create_engine_names_its_postgresql_driver() -> None:
    files, offenders = _scan(REPO_ROOT)

    assert files > 100, f"scan walked only {files} files; the guard would be vacuous"
    assert offenders == [], (
        "create_engine must receive sqlalchemy_url(...) (packages/common/sqlalchemy_url.py) or a "
        f"literal sqlite URL, so SQLAlchemy never picks the PostgreSQL DBAPI (#2632): {offenders}"
    )


def test_guard_sees_the_known_engine_factories() -> None:
    """Non-vacuity: the real factories are inside the scan and are recognised."""
    expected = {
        "apps/api/routes/hydro_display.py",
        "apps/api/routes/pipeline.py",
        "services/tile_publisher/forcing_copyback_backfill.py",
        "services/tile_publisher/publisher.py",
        "services/orchestrator/chain_compat_runtime.py",
        "services/orchestrator/scheduler_runtime.py",
        "scripts/node27_coverage_freshness_alert.py",
        "scripts/node27_river_tile_coordinate_evidence.py",
        "tests/integration_helpers.py",
    }
    seen: set[str] = set()
    for path in _iter_scanned_sources(REPO_ROOT):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(isinstance(node, ast.Call) and _is_create_engine(node.func) for node in ast.walk(tree)):
            seen.add(path.relative_to(REPO_ROOT).as_posix())

    assert expected <= seen, f"engine factories missing from the scan: {sorted(expected - seen)}"


def test_guard_prunes_vendored_trees(tmp_path: Path) -> None:
    vendored = tmp_path / "apps" / "frontend" / "node_modules" / "pkg"
    vendored.mkdir(parents=True)
    (vendored / "bad.py").write_text("create_engine(url)\n", encoding="utf-8")
    owned = tmp_path / "services" / "svc.py"
    owned.parent.mkdir(parents=True)
    owned.write_text("from sqlalchemy import create_engine\ncreate_engine(url)\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "integration_helpers.py").write_text("", encoding="utf-8")

    _files, offenders = _scan(tmp_path)

    assert offenders == ["services/svc.py:2"]


def test_guard_rejects_a_bare_create_engine() -> None:
    """Mutation self-check: the predicate turns red on the drift it exists for."""
    source = (
        "import sqlalchemy\n"
        "from sqlalchemy import create_engine\n"
        "from packages.common.sqlalchemy_url import sqlalchemy_url\n"
        "from packages.common import sqlalchemy_url as helpers\n"
        "def ok_helper(database_url):\n"
        "    return create_engine(sqlalchemy_url(database_url), future=True)\n"
        "def ok_attribute(database_url):\n"
        "    return sqlalchemy.create_engine(helpers.sqlalchemy_url(database_url))\n"
        "def ok_keyword(database_url):\n"
        "    return create_engine(url=sqlalchemy_url(database_url))\n"
        "def ok_sqlite(db_path):\n"
        "    return create_engine('sqlite://'), create_engine(f'sqlite:///{db_path}')\n"
        "def bare(database_url):\n"
        "    return create_engine(database_url, future=True)\n"
        "def bare_attribute(config):\n"
        "    return sqlalchemy.create_engine(config.database_url)\n"
        "def bare_literal():\n"
        "    return create_engine('postgresql://u:p@h/d')\n"
        "def bare_fstring(host):\n"
        "    return create_engine(f'postgresql://u:p@{host}/d')\n"
        "def indirect(database_url):\n"
        "    url = sqlalchemy_url(database_url)\n"
        "    return create_engine(url)\n"
        "def no_url():\n"
        "    return create_engine()\n"
    )

    assert unexplicit_engine_sites(source, filename="drift.py") == [
        "drift.py:14",
        "drift.py:16",
        "drift.py:18",
        "drift.py:20",
        "drift.py:23",
        "drift.py:25",
    ]
