"""Structural ownership growth signals (#1823 partition).

New public surface in an oversized source across Python, JS/CJS and TypeScript:
public functions, class methods (including beyond the context window), route
decorators and APIRouter variables, import families, and the local-helper /
signature-edit cases that must stay silent. Detail tokens are asserted bounded
and source-literal free, and the committed-diff path is compared against an
explicit base ref.

The shared constants, the memoized ``build_report`` accessor, the finding
selectors and the fixture builders live in ``tests/entropy_audit_helpers.py``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.governance import audit_repo_entropy
from tests.entropy_audit_helpers import (
    _commit_all,
    _git_rev_parse,
    _init_git,
    _structural_budget,
    _structural_growth_signal_details,
    _structural_growth_signal_types,
    _structural_public_surface_detail,
    _structural_python_fixture,
    _structural_records_by_path,
    _structural_ts_private_fixture,
    _write,
)


def test_structural_ownership_growth_ignores_oversized_bugfix_only_edit(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    source_path = tmp_path / "services" / "api" / "large.py"
    base_text = _structural_python_fixture(1001, "import os")
    _write(source_path, base_text)
    _commit_all(tmp_path, "initial oversized source")
    base_ref = _git_rev_parse(tmp_path, "HEAD")
    _write(source_path, base_text + "BUGFIX_SENTINEL = True\n")

    budget = _structural_budget(tmp_path, structural_base_ref=base_ref)

    assert "services/api/large.py" in _structural_records_by_path(budget["oversized_files"])
    assert _structural_growth_signal_types(budget, "services/api/large.py") == set()


@pytest.mark.parametrize(
    ("added_lines", "expected_signal"),
    [
        (("import requests",), "new-import-family"),
        (("def public_entrypoint():", "    return 1"), "public-entrypoint"),
        (("LEGACY_ALIAS = object()  # compatibility alias",), "compatibility-symbol"),
        (("SCHEMA = {'mode': 'strict'}",), "parser-validator-responsibility"),
    ],
)
def test_structural_ownership_growth_reports_new_surface_in_oversized_source(
    tmp_path: Path,
    added_lines: tuple[str, ...],
    expected_signal: str,
) -> None:
    _init_git(tmp_path)
    source_path = tmp_path / "services" / "api" / "large.py"
    base_text = _structural_python_fixture(1001, "import os")
    _write(source_path, base_text)
    _commit_all(tmp_path, "initial oversized source")
    base_ref = _git_rev_parse(tmp_path, "HEAD")
    _write(source_path, base_text + "\n".join(added_lines) + "\n")

    budget = _structural_budget(tmp_path, structural_base_ref=base_ref)
    signals = [
        signal
        for signal in budget["ownership_growth_signals"]
        if signal["path"] == "services/api/large.py"
    ]

    assert expected_signal in {signal["signal_type"] for signal in signals}
    assert all("inventory" in str(signal["owner_action"]) for signal in signals)
    assert all("no immediate split" in str(signal["owner_action"]) for signal in signals)


def test_structural_ownership_growth_ignores_nested_python_helper_entrypoint(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    source_path = tmp_path / "services" / "api" / "large.py"
    base_text = _structural_python_fixture(
        1001,
        "import os",
        "def existing_entrypoint():",
        "    return os.name",
    )
    _write(source_path, base_text)
    _commit_all(tmp_path, "base oversized source")
    base_ref = _git_rev_parse(tmp_path, "HEAD")
    changed_text = base_text.replace(
        "    return os.name\n",
        "    def local_helper():\n"
        "        return os.name\n"
        "    return local_helper()\n",
    )
    _write(source_path, changed_text)

    budget = _structural_budget(tmp_path, structural_base_ref=base_ref)

    assert "public-entrypoint" not in _structural_growth_signal_types(
        budget,
        "services/api/large.py",
    )


def test_structural_ownership_growth_reports_python_public_class_method_addition(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    source_path = tmp_path / "services" / "api" / "large.py"
    base_lines = [
        "import os",
        "",
        "class Controller:",
        "    def existing(self) -> str:",
        "        return os.name",
    ]
    changed_lines = [
        "import os",
        "",
        "class Controller:",
        "    def existing(self) -> str:",
        "        return os.name",
        "",
        "    def handle(self) -> str:",
        "        return os.name",
    ]
    _write(source_path, _structural_python_fixture(1001, *base_lines))
    _commit_all(tmp_path, "base oversized source")
    base_ref = _git_rev_parse(tmp_path, "HEAD")
    _write(source_path, _structural_python_fixture(1001, *changed_lines))

    budget = _structural_budget(tmp_path, structural_base_ref=base_ref)
    signal_details = _structural_growth_signal_details(
        budget,
        "services/api/large.py",
        "public-entrypoint",
    )

    assert signal_details == [_structural_public_surface_detail("method:Controller.handle")]


def test_structural_ownership_growth_reports_huge_python_public_class_method_addition(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    source_path = tmp_path / "services" / "api" / "large.py"
    padding = [f"    PAD_{index} = '{'x' * 1024}'" for index in range(1_030)]
    base_lines = [
        "import os",
        "",
        "class Controller:",
        "    def existing(self) -> str:",
        "        return os.name",
        "",
        *padding,
    ]
    changed_lines = [
        "import os",
        "",
        "class Controller:",
        "    def existing(self) -> str:",
        "        return os.name",
        "",
        "    def handle(self) -> str:",
        "        return os.name",
        "",
        *padding,
    ]
    _write(source_path, "\n".join(base_lines) + "\n")
    _commit_all(tmp_path, "base huge oversized source")
    base_ref = _git_rev_parse(tmp_path, "HEAD")
    _write(source_path, "\n".join(changed_lines) + "\n")

    assert source_path.stat().st_size > audit_repo_entropy.MAX_SCANNED_TEXT_FILE_BYTES
    budget = _structural_budget(tmp_path, structural_base_ref=base_ref)

    assert _structural_growth_signal_details(
        budget,
        "services/api/large.py",
        "public-entrypoint",
    ) == [_structural_public_surface_detail("method:Controller.handle")]


def test_structural_ownership_growth_reports_huge_python_class_method_beyond_context_window(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    source_path = tmp_path / "services" / "api" / "large.py"
    class_padding = [
        f"    PAD_{index} = {index}"
        for index in range(audit_repo_entropy.STRUCTURAL_PYTHON_CONTEXT_MAX_LINES + 25)
    ]
    tail_padding = [f"VALUE_{index} = '{'x' * 1024}'" for index in range(1_030)]
    base_lines = [
        "import os",
        "",
        "class Controller:",
        *class_padding,
        "",
        *tail_padding,
    ]
    changed_lines = [
        "import os",
        "",
        "class Controller:",
        *class_padding,
        "",
        "    def handle(self) -> str:",
        "        return os.name",
        "",
        *tail_padding,
    ]
    _write(source_path, "\n".join(base_lines) + "\n")
    _commit_all(tmp_path, "base huge oversized source")
    base_ref = _git_rev_parse(tmp_path, "HEAD")
    _write(source_path, "\n".join(changed_lines) + "\n")

    assert source_path.stat().st_size > audit_repo_entropy.MAX_SCANNED_TEXT_FILE_BYTES
    budget = _structural_budget(tmp_path, structural_base_ref=base_ref)

    assert _structural_growth_signal_details(
        budget,
        "services/api/large.py",
        "public-entrypoint",
    ) == [_structural_public_surface_detail("method:Controller.handle")]


def test_structural_ownership_growth_ignores_huge_python_local_helper(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    source_path = tmp_path / "services" / "api" / "large.py"
    padding = [f"VALUE_{index} = '{'x' * 1024}'" for index in range(1_030)]
    base_lines = [
        "def existing() -> int:",
        "    return 1",
        "",
        *padding,
    ]
    changed_lines = [
        "def existing() -> int:",
        "    def local_helper() -> int:",
        "        return 1",
        "    return local_helper()",
        "",
        *padding,
    ]
    _write(source_path, "\n".join(base_lines) + "\n")
    _commit_all(tmp_path, "base huge oversized source")
    base_ref = _git_rev_parse(tmp_path, "HEAD")
    _write(source_path, "\n".join(changed_lines) + "\n")

    assert source_path.stat().st_size > audit_repo_entropy.MAX_SCANNED_TEXT_FILE_BYTES
    budget = _structural_budget(tmp_path, structural_base_ref=base_ref)

    assert "public-entrypoint" not in _structural_growth_signal_types(
        budget,
        "services/api/large.py",
    )


def test_structural_ownership_growth_ignores_huge_python_local_helper_beyond_context_window(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    source_path = tmp_path / "services" / "api" / "large.py"
    function_padding = [
        f"    value_{index} = {index}"
        for index in range(audit_repo_entropy.STRUCTURAL_PYTHON_CONTEXT_MAX_LINES + 25)
    ]
    tail_padding = [f"VALUE_{index} = '{'x' * 1024}'" for index in range(1_030)]
    base_lines = [
        "def existing() -> int:",
        *function_padding,
        "    return 1",
        "",
        *tail_padding,
    ]
    changed_lines = [
        "def existing() -> int:",
        *function_padding,
        "    def local_helper() -> int:",
        "        return 1",
        "    return local_helper()",
        "",
        *tail_padding,
    ]
    _write(source_path, "\n".join(base_lines) + "\n")
    _commit_all(tmp_path, "base huge oversized source")
    base_ref = _git_rev_parse(tmp_path, "HEAD")
    _write(source_path, "\n".join(changed_lines) + "\n")

    assert source_path.stat().st_size > audit_repo_entropy.MAX_SCANNED_TEXT_FILE_BYTES
    budget = _structural_budget(tmp_path, structural_base_ref=base_ref)

    assert "public-entrypoint" not in _structural_growth_signal_types(
        budget,
        "services/api/large.py",
    )


def test_structural_ownership_growth_ignores_huge_python_local_class_method(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    source_path = tmp_path / "services" / "api" / "large.py"
    padding = [f"VALUE_{index} = '{'x' * 1024}'" for index in range(1_030)]
    base_lines = [
        "def existing() -> int:",
        "    return 1",
        "",
        *padding,
    ]
    changed_lines = [
        "def existing() -> int:",
        "    class Local:",
        "        def helper(self) -> int:",
        "            return 1",
        "    return Local().helper()",
        "",
        *padding,
    ]
    _write(source_path, "\n".join(base_lines) + "\n")
    _commit_all(tmp_path, "base huge oversized source")
    base_ref = _git_rev_parse(tmp_path, "HEAD")
    _write(source_path, "\n".join(changed_lines) + "\n")

    assert source_path.stat().st_size > audit_repo_entropy.MAX_SCANNED_TEXT_FILE_BYTES
    budget = _structural_budget(tmp_path, structural_base_ref=base_ref)

    assert "public-entrypoint" not in _structural_growth_signal_types(
        budget,
        "services/api/large.py",
    )


@pytest.mark.parametrize(
    ("base_lines", "changed_lines"),
    [
        (
            [
                "def existing(value: int) -> int:",
                "    return value",
            ],
            [
                "def existing(value: int, *, strict: bool = False) -> int:",
                "    return value if strict else value",
            ],
        ),
        (
            [
                "class Existing:",
                "    def handle(self) -> int:",
                "        return 1",
            ],
            [
                "class Existing(object):",
                "    def handle(self) -> int:",
                "        return 1",
            ],
        ),
    ],
)
def test_structural_ownership_growth_ignores_existing_python_public_signature_edit(
    tmp_path: Path,
    base_lines: list[str],
    changed_lines: list[str],
) -> None:
    _init_git(tmp_path)
    source_path = tmp_path / "services" / "api" / "large.py"
    _write(source_path, _structural_python_fixture(1001, *base_lines))
    _commit_all(tmp_path, "base oversized source")
    base_ref = _git_rev_parse(tmp_path, "HEAD")
    _write(source_path, _structural_python_fixture(1001, *changed_lines))

    budget = _structural_budget(tmp_path, structural_base_ref=base_ref)

    assert "public-entrypoint" not in _structural_growth_signal_types(
        budget,
        "services/api/large.py",
    )


@pytest.mark.parametrize(
    ("relative_path", "added_lines", "expected_detail"),
    [
        (
            "apps/frontend/src/large.ts",
            ("const handler = () => null;", "export { handler };"),
            _structural_public_surface_detail("export:handler"),
        ),
        (
            "apps/frontend/src/large.js",
            ("const handler = () => null;", "module.exports = { handler };"),
            _structural_public_surface_detail("export:handler"),
        ),
        (
            "apps/frontend/src/large.js",
            ("const handler = () => null;", "exports.foo = handler;"),
            _structural_public_surface_detail("export:foo"),
        ),
        (
            "apps/frontend/src/large.ts",
            ("export default defineConfig({});",),
            _structural_public_surface_detail("export:default"),
        ),
    ],
)
def test_structural_ownership_growth_reports_js_ts_public_exports(
    tmp_path: Path,
    relative_path: str,
    added_lines: tuple[str, ...],
    expected_detail: str,
) -> None:
    _init_git(tmp_path)
    source_path = tmp_path / relative_path
    base_text = _structural_ts_private_fixture(1001)
    _write(source_path, base_text)
    _commit_all(tmp_path, "base oversized source")
    base_ref = _git_rev_parse(tmp_path, "HEAD")
    _write(source_path, base_text + "\n".join(added_lines) + "\n")

    budget = _structural_budget(tmp_path, structural_base_ref=base_ref)

    assert _structural_growth_signal_details(budget, relative_path, "public-entrypoint") == [
        expected_detail
    ]


def test_structural_ownership_growth_reports_cjs_export_after_nested_object(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    relative_path = "apps/frontend/src/large.js"
    source_path = tmp_path / relative_path
    base_text = _structural_ts_private_fixture(
        1001,
        "const handler = () => null;",
        "module.exports = { config: { enabled: true }, \"default\": handler };",
    )
    changed_text = _structural_ts_private_fixture(
        1001,
        "const handler = () => null;",
        "module.exports = { config: { enabled: true }, \"default\": handler, handler };",
    )
    _write(source_path, base_text)
    _commit_all(tmp_path, "base oversized source")
    base_ref = _git_rev_parse(tmp_path, "HEAD")
    _write(source_path, changed_text)

    budget = _structural_budget(tmp_path, structural_base_ref=base_ref)

    assert _structural_growth_signal_details(budget, relative_path, "public-entrypoint") == [
        _structural_public_surface_detail("export:handler")
    ]


def test_structural_ownership_growth_reports_cjs_export_after_regex_literal_brace(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    relative_path = "apps/frontend/src/large.js"
    source_path = tmp_path / relative_path
    base_text = _structural_ts_private_fixture(
        1001,
        "const handler = () => null;",
        "module.exports = { pattern: /}/ };",
    )
    changed_text = _structural_ts_private_fixture(
        1001,
        "const handler = () => null;",
        "module.exports = { pattern: /}/, handler };",
    )
    _write(source_path, base_text)
    _commit_all(tmp_path, "base oversized source")
    base_ref = _git_rev_parse(tmp_path, "HEAD")
    _write(source_path, changed_text)

    budget = _structural_budget(tmp_path, structural_base_ref=base_ref)

    assert _structural_growth_signal_details(budget, relative_path, "public-entrypoint") == [
        _structural_public_surface_detail("export:handler")
    ]


def test_structural_ownership_growth_public_export_detail_is_bounded(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    relative_path = "apps/frontend/src/large.ts"
    source_path = tmp_path / relative_path
    base_text = _structural_ts_private_fixture(1001)
    added_exports = "\n".join(f"export const handler{index:02d} = {index};" for index in range(30))
    _write(source_path, base_text)
    _commit_all(tmp_path, "base oversized source")
    base_ref = _git_rev_parse(tmp_path, "HEAD")
    _write(source_path, base_text + added_exports + "\n")

    budget = _structural_budget(tmp_path, structural_base_ref=base_ref)
    details = _structural_growth_signal_details(budget, relative_path, "public-entrypoint")

    assert len(details) == 1
    assert details[0].startswith("new public surface tokens (30 total): ")
    assert "(+20 more)" in details[0]
    assert audit_repo_entropy._structural_bounded_detail_token("export:handler00") in details[0]
    assert "export:handler00" not in details[0]
    assert "export:handler29" not in details[0]
    assert len(details[0]) < 500


def test_structural_ownership_growth_reports_ts_exported_class_method_addition(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    relative_path = "apps/frontend/src/large.ts"
    source_path = tmp_path / relative_path
    base_text = _structural_ts_private_fixture(
        1001,
        "export class Controller {",
        "  existing(value: string): string {",
        "    return value;",
        "  }",
        "}",
    )
    changed_text = _structural_ts_private_fixture(
        1001,
        "export class Controller {",
        "  existing(value: string): string {",
        "    return value;",
        "  }",
        "",
        "  handle(value: string): string {",
        "    return value.trim();",
        "  }",
        "}",
    )
    _write(source_path, base_text)
    _commit_all(tmp_path, "base oversized source")
    base_ref = _git_rev_parse(tmp_path, "HEAD")
    _write(source_path, changed_text)

    budget = _structural_budget(tmp_path, structural_base_ref=base_ref)

    assert _structural_growth_signal_details(budget, relative_path, "public-entrypoint") == [
        _structural_public_surface_detail("method:Controller.handle")
    ]


@pytest.mark.parametrize("class_prefix", ["export abstract class", "export declare class"])
def test_structural_ownership_growth_reports_ts_exported_modified_class_method_addition(
    tmp_path: Path,
    class_prefix: str,
) -> None:
    _init_git(tmp_path)
    relative_path = "apps/frontend/src/large.ts"
    source_path = tmp_path / relative_path
    base_text = _structural_ts_private_fixture(
        1001,
        f"{class_prefix} Controller {{",
        "  existing(value: string): string {",
        "    return value;",
        "  }",
        "}",
    )
    changed_text = _structural_ts_private_fixture(
        1001,
        f"{class_prefix} Controller {{",
        "  existing(value: string): string {",
        "    return value;",
        "  }",
        "",
        "  handle(value: string): string {",
        "    return value.trim();",
        "  }",
        "}",
    )
    _write(source_path, base_text)
    _commit_all(tmp_path, "base oversized source")
    base_ref = _git_rev_parse(tmp_path, "HEAD")
    _write(source_path, changed_text)

    budget = _structural_budget(tmp_path, structural_base_ref=base_ref)

    assert _structural_growth_signal_details(budget, relative_path, "public-entrypoint") == [
        _structural_public_surface_detail("method:Controller.handle")
    ]


def test_structural_ownership_growth_ignores_existing_ts_exported_class_method_signature_edit(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    relative_path = "apps/frontend/src/large.ts"
    source_path = tmp_path / relative_path
    base_text = _structural_ts_private_fixture(
        1001,
        "export class Controller {",
        "  existing(value: string): string {",
        "    return value;",
        "  }",
        "}",
    )
    changed_text = _structural_ts_private_fixture(
        1001,
        "export class Controller {",
        "  existing(value: string, strict = false): string {",
        "    return strict ? value.trim() : value;",
        "  }",
        "}",
    )
    _write(source_path, base_text)
    _commit_all(tmp_path, "base oversized source")
    base_ref = _git_rev_parse(tmp_path, "HEAD")
    _write(source_path, changed_text)

    budget = _structural_budget(tmp_path, structural_base_ref=base_ref)

    assert "public-entrypoint" not in _structural_growth_signal_types(
        budget,
        relative_path,
    )


def test_structural_ownership_growth_ignores_existing_ts_public_signature_edit(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    source_path = tmp_path / "apps" / "frontend" / "src" / "large.ts"
    base_text = _structural_ts_private_fixture(
        1001,
        "export function handler(value: string): string {",
        "  return value;",
        "}",
    )
    changed_text = _structural_ts_private_fixture(
        1001,
        "export function handler(value: string, strict = false): string {",
        "  return strict ? value.trim() : value;",
        "}",
    )
    _write(source_path, base_text)
    _commit_all(tmp_path, "base oversized source")
    base_ref = _git_rev_parse(tmp_path, "HEAD")
    _write(source_path, changed_text)

    budget = _structural_budget(tmp_path, structural_base_ref=base_ref)

    assert "public-entrypoint" not in _structural_growth_signal_types(
        budget,
        "apps/frontend/src/large.ts",
    )


def test_structural_ownership_growth_reports_new_route_decorator_alias(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    relative_path = "apps/api/routes/large.py"
    source_path = tmp_path / relative_path
    base_text = _structural_python_fixture(
        1001,
        "from fastapi import APIRouter",
        "",
        "router = APIRouter()",
        "",
        "@router.get('/existing')",
        "def existing() -> dict[str, object]:",
        "    return {}",
    )
    changed_text = _structural_python_fixture(
        1001,
        "from fastapi import APIRouter",
        "",
        "router = APIRouter()",
        "",
        "@router.get('/new')",
        "@router.get('/existing')",
        "def existing() -> dict[str, object]:",
        "    return {}",
    )
    _write(source_path, base_text)
    _commit_all(tmp_path, "base oversized route source")
    base_ref = _git_rev_parse(tmp_path, "HEAD")
    _write(source_path, changed_text)

    budget = _structural_budget(tmp_path, structural_base_ref=base_ref)

    expected_route_token = audit_repo_entropy._structural_route_path_token("/new")
    assert _structural_growth_signal_details(budget, relative_path, "public-entrypoint") == [
        _structural_public_surface_detail(f"route:get:{expected_route_token}")
    ]


def test_structural_ownership_growth_reports_new_apirouter_variable_route_alias(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    relative_path = "apps/api/routes/large.py"
    source_path = tmp_path / relative_path
    base_text = _structural_python_fixture(
        1001,
        "from fastapi import APIRouter",
        "",
        "runtime_router = APIRouter()",
        "",
        "@runtime_router.get('/existing')",
        "def existing() -> dict[str, object]:",
        "    return {}",
    )
    changed_text = _structural_python_fixture(
        1001,
        "from fastapi import APIRouter",
        "",
        "runtime_router = APIRouter()",
        "",
        "@runtime_router.get('/new')",
        "@runtime_router.get('/existing')",
        "def existing() -> dict[str, object]:",
        "    return {}",
    )
    _write(source_path, base_text)
    _commit_all(tmp_path, "base oversized route source")
    base_ref = _git_rev_parse(tmp_path, "HEAD")
    _write(source_path, changed_text)

    budget = _structural_budget(tmp_path, structural_base_ref=base_ref)

    expected_route_token = audit_repo_entropy._structural_route_path_token("/new")
    assert _structural_growth_signal_details(budget, relative_path, "public-entrypoint") == [
        _structural_public_surface_detail(f"route:get:{expected_route_token}")
    ]


def test_structural_ownership_growth_route_detail_hashes_long_path_literal(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    relative_path = "apps/api/routes/large.py"
    source_path = tmp_path / relative_path
    long_path = "/secret_" + ("x" * 200)
    base_text = _structural_python_fixture(
        1001,
        "from fastapi import APIRouter",
        "",
        "router = APIRouter()",
        "",
        "def existing() -> dict[str, object]:",
        "    return {}",
    )
    changed_text = _structural_python_fixture(
        1001,
        "from fastapi import APIRouter",
        "",
        "router = APIRouter()",
        "",
        f"@router.get('{long_path}')",
        "def existing() -> dict[str, object]:",
        "    return {}",
    )
    _write(source_path, base_text)
    _commit_all(tmp_path, "base oversized route source")
    base_ref = _git_rev_parse(tmp_path, "HEAD")
    _write(source_path, changed_text)

    budget = _structural_budget(tmp_path, structural_base_ref=base_ref)
    details = _structural_growth_signal_details(budget, relative_path, "public-entrypoint")

    expected_route_token = audit_repo_entropy._structural_route_path_token(long_path)
    assert details == [_structural_public_surface_detail(f"route:get:{expected_route_token}")]
    assert "secret_" not in details[0]
    assert len(details[0]) < 120


def test_structural_ownership_growth_import_detail_hashes_import_family(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    relative_path = "services/api/large.py"
    source_path = tmp_path / relative_path
    import_module = "sk_live_short_secret"
    base_text = _structural_python_fixture(1001)
    changed_text = _structural_python_fixture(1001, f"import {import_module}")
    _write(source_path, base_text)
    _commit_all(tmp_path, "base oversized source")
    base_ref = _git_rev_parse(tmp_path, "HEAD")
    _write(source_path, changed_text)

    budget = _structural_budget(tmp_path, structural_base_ref=base_ref)
    details = _structural_growth_signal_details(budget, relative_path, "new-import-family")

    expected_import_token = audit_repo_entropy._structural_import_family_detail_token(import_module)
    assert details == [f"new import family tokens: {expected_import_token}"]
    assert "sk_live_short_secret" not in details[0]
    assert len(details[0]) < 120


def test_structural_ownership_growth_reports_committed_pr_diff_against_base_ref(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    source_path = tmp_path / "services" / "api" / "large.py"
    base_text = _structural_python_fixture(1001, "import os")
    _write(source_path, base_text)
    _commit_all(tmp_path, "base oversized source")
    base_ref = _git_rev_parse(tmp_path, "HEAD")
    _write(
        source_path,
        base_text
        + "\n".join(
            (
                "import requests",
                "def public_entrypoint():",
                "    return requests.__name__",
            )
        )
        + "\n",
    )
    _commit_all(tmp_path, "add ownership surface")
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=tmp_path,
        check=True,
        stdout=subprocess.PIPE,
    )
    assert status.stdout == b""

    budget = _structural_budget(tmp_path, structural_base_ref=base_ref)
    signals = _structural_growth_signal_types(budget, "services/api/large.py")

    assert {"new-import-family", "public-entrypoint"} <= signals
    comparison_base = budget["comparison_base_ref"]
    assert isinstance(comparison_base, dict)
    assert comparison_base["requested"] == base_ref
    assert comparison_base["resolved"] == base_ref
    assert comparison_base["ref_kind"] == "explicit"
