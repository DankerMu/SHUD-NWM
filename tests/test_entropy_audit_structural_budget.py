"""The structural file budget's classification and bounded reads (#1823 partition).

Threshold classification for tracked sources, the exemption vocabulary (data /
fixture / protocol / root lockfiles, and the roots that must NOT be exempted),
the Markdown report sections, the TypeScript import-family and public-surface
scanners, and the byte caps that keep a huge source from being read past the
scan limit -- including the truncated-unknown-line-count and redaction paths.

The shared constants, the memoized ``build_report`` accessor, the finding
selectors and the fixture builders live in ``tests/entropy_audit_helpers.py``.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts.governance import audit_repo_entropy
from tests.entropy_audit_helpers import (
    AUDIT_SCRIPT,
    _commit_all,
    _git_rev_parse,
    _init_git,
    _setup_clean_hard_gate_fixture,
    _structural_budget,
    _structural_growth_signal_types,
    _structural_python_fixture,
    _structural_records_by_path,
    _structural_ts_fixture,
    _structural_yaml_fixture,
    _write,
)


def test_structural_file_budget_classifies_tracked_source_thresholds_and_exemptions(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    _write(
        tmp_path / "services" / "api" / "large.py",
        _structural_python_fixture(
            1001,
            "import os",
            "from services.orchestrator import chain",
        ),
    )
    _write(
        tmp_path / "apps" / "api" / "yellow.py",
        _structural_python_fixture(500),
    )
    _write(
        tmp_path / "schemas" / "generated" / "openapi_types.ts",
        _structural_ts_fixture(1001, "// generated; do not edit"),
    )
    _write(
        tmp_path / "apps" / "frontend" / "pnpm-lock.yaml",
        _structural_yaml_fixture(1001, "lockfileVersion: '9.0'"),
    )
    subprocess.run(
        [
            "git",
            "add",
            "services/api/large.py",
            "apps/api/yellow.py",
            "schemas/generated/openapi_types.ts",
            "apps/frontend/pnpm-lock.yaml",
        ],
        cwd=tmp_path,
        check=True,
    )

    budget = _structural_budget(tmp_path)

    oversized = _structural_records_by_path(budget["oversized_files"])
    yellow = _structural_records_by_path(budget["yellow_zone_files"])
    exemptions = _structural_records_by_path(budget["governed_exemptions"])

    large = oversized["services/api/large.py"]
    assert large["budget_class"] == "mandatory-governance"
    assert large["line_count"] == 1001
    assert large["line_count_is_truncated"] is False
    assert large["line_count_lower_bound"] == 1001
    assert large["size_bytes"] == (tmp_path / "services" / "api" / "large.py").stat().st_size
    assert large["module"] == "services/api"
    assert large["import_family_tokens"] == [
        audit_repo_entropy._structural_import_family_detail_token("os"),
        audit_repo_entropy._structural_import_family_detail_token("services/orchestrator"),
    ]
    assert large["import_family_count"] == 2
    assert "inventory" in str(large["owner_action"])

    yellow_zone = yellow["apps/api/yellow.py"]
    assert yellow_zone["budget_class"] == "yellow-zone"
    assert yellow_zone["line_count"] == 500
    assert "review-only" in str(yellow_zone["review_reason"])

    generated = exemptions["schemas/generated/openapi_types.ts"]
    assert generated["budget_class"] == "governed-exemption"
    assert generated["line_count"] == 1001
    assert generated["exemption_family"] == "generated"
    assert "schemas/generated/openapi_types.ts" not in oversized

    lockfile = exemptions["apps/frontend/pnpm-lock.yaml"]
    assert lockfile["budget_class"] == "governed-exemption"
    assert lockfile["line_count"] == 1001
    assert lockfile["module"] == "apps/frontend"
    assert lockfile["exemption_family"] == "dependency-lockfile"
    assert (
        lockfile["exemption_reason"]
        == "well-known dependency lockfile is a machine-readable dependency artifact"
    )
    assert "apps/frontend/pnpm-lock.yaml" not in oversized

    assert budget["mandatory_governance_count"] == 1
    assert budget["yellow_zone_count"] == 1
    assert budget["governed_exemption_count"] == 2
    assert budget["top_oversized_modules"][0]["module"] == "services/api"


def test_structural_file_budget_does_not_exempt_implementation_roots_with_data_or_schema_labels(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    _write(
        tmp_path / "workers" / "data_adapters" / "gfs_adapter.py",
        _structural_python_fixture(1001),
    )
    _write(
        tmp_path / "services" / "api" / "schema_validator.py",
        _structural_python_fixture(1001),
    )
    subprocess.run(
        ["git", "add", "workers/data_adapters/gfs_adapter.py", "services/api/schema_validator.py"],
        cwd=tmp_path,
        check=True,
    )

    budget = _structural_budget(tmp_path)

    oversized = _structural_records_by_path(budget["oversized_files"])
    exemptions = _structural_records_by_path(budget["governed_exemptions"])
    assert oversized["workers/data_adapters/gfs_adapter.py"]["budget_class"] == "mandatory-governance"
    assert oversized["services/api/schema_validator.py"]["budget_class"] == "mandatory-governance"
    assert "workers/data_adapters/gfs_adapter.py" not in exemptions
    assert "services/api/schema_validator.py" not in exemptions


def test_structural_file_budget_reports_root_lockfiles_as_dependency_exemptions(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    for lockfile_name in audit_repo_entropy.STRUCTURAL_DEPENDENCY_LOCKFILE_NAMES:
        _write(tmp_path / lockfile_name, _structural_yaml_fixture(1001, "lockfileVersion: 'test'"))
    subprocess.run(
        ["git", "add", *sorted(audit_repo_entropy.STRUCTURAL_DEPENDENCY_LOCKFILE_NAMES)],
        cwd=tmp_path,
        check=True,
    )

    budget = _structural_budget(tmp_path)

    exemptions = _structural_records_by_path(budget["governed_exemptions"])
    assert set(audit_repo_entropy.STRUCTURAL_DEPENDENCY_LOCKFILE_NAMES) <= set(exemptions)
    for lockfile_name in audit_repo_entropy.STRUCTURAL_DEPENDENCY_LOCKFILE_NAMES:
        assert exemptions[lockfile_name]["line_count"] == 1001
        assert exemptions[lockfile_name]["exemption_family"] == "dependency-lockfile"


@pytest.mark.parametrize(
    ("relative_path", "expected_family"),
    [
        ("data/catalog/source_payload.json", "data"),
        ("tests/fixtures/api_payload.py", "fixture"),
        ("schemas/contracts/hydro.yaml", "protocol"),
        ("packages/contracts/hydro.proto", "protocol"),
    ],
)
def test_structural_file_budget_exempts_data_fixture_and_protocol_sources(
    tmp_path: Path,
    relative_path: str,
    expected_family: str,
) -> None:
    _init_git(tmp_path)
    source_path = tmp_path / relative_path
    if source_path.suffix == ".py":
        _write(source_path, _structural_python_fixture(1001))
    else:
        _write(source_path, _structural_yaml_fixture(1001))
    subprocess.run(["git", "add", relative_path], cwd=tmp_path, check=True)

    budget = _structural_budget(tmp_path)

    oversized = _structural_records_by_path(budget["oversized_files"])
    exemptions = _structural_records_by_path(budget["governed_exemptions"])
    assert exemptions[relative_path]["budget_class"] == "governed-exemption"
    assert exemptions[relative_path]["exemption_family"] == expected_family
    assert relative_path not in oversized


def test_structural_file_budget_markdown_keeps_existing_report_sections(tmp_path: Path) -> None:
    report = audit_repo_entropy.build_report(tmp_path)

    markdown = audit_repo_entropy.render_markdown(report)

    assert "## Structural File Budget" in markdown
    assert "## Compatibility Facade Guard" in markdown
    assert "## Entropy Heatmap" in markdown
    assert "## High-Spread Patterns" in markdown
    assert "## Prioritized Cleanup Targets" in markdown
    assert markdown.index("## Structural File Budget") < markdown.index("## Entropy Heatmap")
    assert markdown.index("## Compatibility Facade Guard") < markdown.index("## Entropy Heatmap")

def test_structural_ts_import_families_ignore_comments_and_string_literals() -> None:
    families = audit_repo_entropy._structural_ts_import_families(
        """
        // require('comment-only')
        /*
        import blocked from 'block-comment-static';
        import('block-comment-dynamic');
        */
        const quotedRequire = "require('string-only')";
        const quotedDynamic = 'import("quoted-dynamic")';
        const templated = `import('template-only')`;
        import React from 'react';
        const scoped = require('@scope/pkg/submodule');
        const lazy = import('lodash/fp');
        """
    )

    assert families == ("@scope/pkg", "lodash", "react")


def test_structural_ts_import_families_ignore_many_comment_and_string_literals_quickly() -> None:
    ignored_lines = []
    for index in range(6_000):
        ignored_lines.append(f"// import('comment-only-{index}')")
        ignored_lines.append(f"const quoted{index} = \"require('string-only-{index}')\";")

    started_at = time.perf_counter()
    families = audit_repo_entropy._structural_ts_import_families("\n".join(ignored_lines))
    elapsed = time.perf_counter() - started_at

    assert families == ()
    assert elapsed < 2.0


def test_structural_ts_import_families_ignore_many_nonmatching_import_lines_quickly() -> None:
    text = "\n".join(f"import value{index}" for index in range(6_000))

    started_at = time.perf_counter()
    families = audit_repo_entropy._structural_ts_import_families(text)
    elapsed = time.perf_counter() - started_at

    assert families == ()
    assert elapsed < 2.0


@pytest.mark.parametrize(
    ("text", "minimum_token_count"),
    [
        ("\n".join("module.exports = {" for _ in range(4_800)), 0),
        ("\n".join(f"export class C{index} {{" for index in range(2_400)), 2_400),
    ],
    ids=[
        "many-cjs-unmatched-braces",
        "many-exported-classes-unmatched-braces",
    ],
)
def test_structural_ts_public_surface_handles_many_unmatched_braces_quickly(
    text: str,
    minimum_token_count: int,
) -> None:
    started_at = time.perf_counter()
    tokens = audit_repo_entropy._structural_ts_public_surface_tokens(text)
    elapsed = time.perf_counter() - started_at

    assert len(tokens) >= minimum_token_count
    assert elapsed < 2.0


def test_structural_ts_import_families_still_detect_real_import_forms() -> None:
    families = audit_repo_entropy._structural_ts_import_families(
        """
        // import('comment-only')
        const quotedRequire = "require('string-only')";
        import { createApp } from '@scope/app/runtime';
        import {
          createRouter,
        } from 'vue-router';
        const express = require('express');
        const lazy = import('lodash/fp');
        """
    )

    assert families == ("@scope/app", "express", "lodash", "vue-router")

def test_structural_ownership_growth_cli_uses_explicit_base_ref_for_committed_diff(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    source_path = tmp_path / "services" / "api" / "large.py"
    base_text = _structural_python_fixture(1001, "import os")
    _write(source_path, base_text)
    _commit_all(tmp_path, "base oversized source")
    base_ref = _git_rev_parse(tmp_path, "HEAD")
    _write(source_path, base_text + "import requests\n")
    _commit_all(tmp_path, "add committed import")

    result = subprocess.run(
        [
            sys.executable,
            str(AUDIT_SCRIPT),
            "--format",
            "json",
            "--structural-base-ref",
            base_ref,
        ],
        cwd=tmp_path,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    report = json.loads(result.stdout)
    budget = report["metadata"]["structural_file_budget"]
    comparison_base = budget["comparison_base_ref"]

    assert comparison_base["requested"] == base_ref
    assert comparison_base["requested_source"] == "argument"
    assert comparison_base["resolved"] == base_ref
    assert "new-import-family" in _structural_growth_signal_types(
        budget,
        "services/api/large.py",
    )


def test_structural_comparison_base_does_not_use_head_fallback_in_ci(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_git(tmp_path)
    _write(tmp_path / "services" / "api" / "large.py", "VALUE = 1\n")
    _commit_all(tmp_path, "single commit")
    monkeypatch.setenv("CI", "true")

    comparison_base = audit_repo_entropy._structural_comparison_base(tmp_path, None)

    assert comparison_base.resolved is None
    assert comparison_base.ref_kind == "unavailable"
    assert comparison_base.status == "unavailable"
    assert "CI structural comparison requires" in str(comparison_base.fallback_reason)


def test_structural_file_budget_bounds_huge_source_reads(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    source_path = tmp_path / "services" / "api" / "huge.py"
    huge_line = "VALUE = '0123456789abcdef'\n"
    repeat_count = audit_repo_entropy.MAX_SCANNED_TEXT_FILE_BYTES // len(huge_line) + 100
    _write(source_path, huge_line * repeat_count)
    subprocess.run(["git", "add", "services/api/huge.py"], cwd=tmp_path, check=True)

    line_count_result = audit_repo_entropy._structural_physical_line_count(source_path)

    assert line_count_result is not None
    assert line_count_result.line_count_is_truncated is True
    assert line_count_result.line_count >= audit_repo_entropy.STRUCTURAL_FILE_BUDGET_MANDATORY_OVER_LINES + 1
    assert line_count_result.line_count < repeat_count
    assert line_count_result.line_count_lower_bound >= (
        audit_repo_entropy.STRUCTURAL_FILE_BUDGET_MANDATORY_OVER_LINES + 1
    )
    assert line_count_result.size_bytes == source_path.stat().st_size

    budget = audit_repo_entropy._structural_file_budget_summary(tmp_path)

    oversized = _structural_records_by_path(budget["oversized_files"])
    huge_record = oversized["services/api/huge.py"]
    assert huge_record["budget_class"] == "mandatory-governance"
    assert huge_record["line_count"] == line_count_result.line_count
    assert huge_record["line_count"] < repeat_count
    assert huge_record["line_count_is_truncated"] is True
    assert huge_record["line_count_lower_bound"] == line_count_result.line_count_lower_bound
    assert huge_record["size_bytes"] == source_path.stat().st_size
    assert huge_record["import_family_tokens"] == []
    assert huge_record["ownership_surface_signals"] == []


def test_structural_file_budget_records_truncated_unknown_line_count_without_mandatory(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    source_path = tmp_path / "services" / "api" / "huge_single_line.py"
    _write(
        source_path,
        "VALUE = '" + ("x" * audit_repo_entropy.MAX_SCANNED_TEXT_FILE_BYTES) + "'\n",
    )
    subprocess.run(["git", "add", "services/api/huge_single_line.py"], cwd=tmp_path, check=True)

    budget = audit_repo_entropy._structural_file_budget_summary(tmp_path)

    oversized = _structural_records_by_path(budget["oversized_files"])
    yellow = _structural_records_by_path(budget["yellow_zone_files"])
    unknown = _structural_records_by_path(budget["unknown_line_count_files"])
    assert "services/api/huge_single_line.py" not in oversized
    assert "services/api/huge_single_line.py" not in yellow
    unknown_record = unknown["services/api/huge_single_line.py"]
    assert unknown_record["budget_class"] == "unknown-line-count"
    assert unknown_record["line_count"] == 1
    assert unknown_record["line_count_is_truncated"] is True
    assert unknown_record["line_count_lower_bound"] == 1
    assert budget["unknown_line_count_count"] == 1


def test_structural_physical_line_count_does_not_read_beyond_scan_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / "services" / "api" / "huge.py"
    _write(source_path, "VALUE = '" + ("x" * audit_repo_entropy.MAX_SCANNED_TEXT_FILE_BYTES) + "'\n")
    original_open = Path.open
    bytes_read = 0

    class GuardedReader:
        def __init__(self, handle: object) -> None:
            self._handle = handle

        def __enter__(self) -> "GuardedReader":
            self._handle.__enter__()
            return self

        def __exit__(self, *args: object) -> object:
            return self._handle.__exit__(*args)

        def read(self, size: int = -1) -> bytes:
            nonlocal bytes_read
            if size < 0:
                raise AssertionError("structural line count must use bounded reads")
            data = self._handle.read(size)
            bytes_read += len(data)
            if bytes_read > audit_repo_entropy.MAX_SCANNED_TEXT_FILE_BYTES:
                raise AssertionError("structural line count read beyond scan cap")
            return data

    def guarded_open(self: Path, *args: object, **kwargs: object) -> object:
        handle = original_open(self, *args, **kwargs)
        if self == source_path:
            return GuardedReader(handle)
        return handle

    monkeypatch.setattr(Path, "open", guarded_open)

    line_count_result = audit_repo_entropy._structural_physical_line_count(source_path)

    assert line_count_result is not None
    assert line_count_result.line_count == 1
    assert line_count_result.line_count_is_truncated is True
    assert line_count_result.line_count_lower_bound == 1
    assert bytes_read == audit_repo_entropy.MAX_SCANNED_TEXT_FILE_BYTES


def test_structural_ownership_growth_preserves_signals_when_diff_scan_truncates(
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
        + "import requests\n"
        + "HUGE_LITERAL = '"
        + ("x" * (audit_repo_entropy.STRUCTURAL_DIFF_MAX_LINE_BYTES + 1024))
        + "'\n",
    )

    budget = _structural_budget(tmp_path, structural_base_ref=base_ref)
    signal_types = _structural_growth_signal_types(budget, "services/api/large.py")
    signals = [
        signal
        for signal in budget["ownership_growth_signals"]
        if isinstance(signal, dict) and signal["path"] == "services/api/large.py"
    ]

    assert "new-import-family" in signal_types
    assert "diff-analysis-truncated" in signal_types
    assert any("diff-line-byte-cap" in str(signal["detail"]) for signal in signals)


def test_structural_ownership_growth_detects_partial_python_import_diff(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    source_path = tmp_path / "services" / "api" / "large.py"
    base_text = _structural_python_fixture(
        1001,
        "import os",
        "def existing() -> object:",
        "    return os",
    )
    _write(source_path, base_text)
    _commit_all(tmp_path, "base oversized source")
    base_ref = _git_rev_parse(tmp_path, "HEAD")
    changed_text = base_text.replace("import os\n", "import os\nimport requests\n")
    changed_text = changed_text.replace("    return os\n", "    return requests\n")
    _write(source_path, changed_text)

    budget = _structural_budget(tmp_path, structural_base_ref=base_ref)

    assert "new-import-family" in _structural_growth_signal_types(
        budget,
        "services/api/large.py",
    )


@pytest.mark.parametrize(
    ("added_import", "expected_new_import_signal"),
    [
        ("import requests.sessions", False),
        ("import httpx", True),
    ],
)
def test_structural_growth_uses_bounded_huge_base_import_prefix(
    tmp_path: Path,
    added_import: str,
    expected_new_import_signal: bool,
) -> None:
    _init_git(tmp_path)
    source_path = tmp_path / "services" / "api" / "large.py"
    huge_line = "VALUE = '0123456789abcdef'\n"
    repeat_count = audit_repo_entropy.MAX_SCANNED_TEXT_FILE_BYTES // len(huge_line) + 100
    base_text = "import requests\n" + (huge_line * repeat_count)
    _write(source_path, base_text)
    _commit_all(tmp_path, "base huge oversized source with requests import")
    base_ref = _git_rev_parse(tmp_path, "HEAD")
    _write(source_path, "import requests\n" + added_import + "\n" + (huge_line * repeat_count))

    budget = _structural_budget(tmp_path, structural_base_ref=base_ref)
    signals = _structural_growth_signal_types(budget, "services/api/large.py")

    if expected_new_import_signal:
        assert "new-import-family" in signals
    else:
        assert "new-import-family" not in signals


def test_structural_growth_detects_multiline_and_indented_imports_in_huge_source(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    source_path = tmp_path / "services" / "api" / "large.py"
    huge_line = "VALUE = '0123456789abcdef'\n"
    repeat_count = audit_repo_entropy.MAX_SCANNED_TEXT_FILE_BYTES // len(huge_line) + 100
    base_text = "import os\n" + (huge_line * repeat_count)
    _write(source_path, base_text)
    _commit_all(tmp_path, "base huge oversized source")
    base_ref = _git_rev_parse(tmp_path, "HEAD")
    _write(
        source_path,
        "import os\n"
        "from pathlib import (\n"
        "    Path,\n"
        ")\n"
        "if True:\n"
        "    import importlib\n"
        + (huge_line * repeat_count),
    )

    budget = _structural_budget(tmp_path, structural_base_ref=base_ref)
    signal_details = [
        str(signal["detail"])
        for signal in budget["ownership_growth_signals"]
        if isinstance(signal, dict)
        and signal["path"] == "services/api/large.py"
        and signal["signal_type"] == "new-import-family"
    ]

    expected_tokens = {
        audit_repo_entropy._structural_import_family_detail_token("importlib"),
        audit_repo_entropy._structural_import_family_detail_token("pathlib"),
    }

    assert "new-import-family" in _structural_growth_signal_types(
        budget,
        "services/api/large.py",
    )
    assert any(all(token in detail for token in expected_tokens) for detail in signal_details)


def test_structural_ownership_growth_details_do_not_leak_added_source_literals(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    source_path = tmp_path / "services" / "api" / "large.py"
    base_text = _structural_python_fixture(1001, "import os")
    _write(source_path, base_text)
    _commit_all(tmp_path, "base oversized source")
    base_ref = _git_rev_parse(tmp_path, "HEAD")
    parser_secret = "sk_live_parser_validator_secret"
    compat_secret = "sk_live_compat_secret"
    _write(
        source_path,
        base_text
        + "\n".join(
            (
                f"SECRET_SCHEMA_VALUE = '{parser_secret}'  # schema validator",
                f"LEGACY_COMPAT_SECRET = '{compat_secret}'  # compatibility alias",
            )
        )
        + "\n",
    )

    report = audit_repo_entropy.build_report(tmp_path, structural_base_ref=base_ref)
    metadata = report["metadata"]
    assert isinstance(metadata, dict)
    budget = metadata["structural_file_budget"]
    assert isinstance(budget, dict)
    structural_json = json.dumps(budget, sort_keys=True)
    markdown = audit_repo_entropy.render_markdown(report)

    assert "parser-validator-responsibility" in _structural_growth_signal_types(
        budget,
        "services/api/large.py",
    )
    assert "compatibility-symbol" in _structural_growth_signal_types(budget, "services/api/large.py")
    assert parser_secret not in structural_json
    assert compat_secret not in structural_json
    assert parser_secret not in markdown
    assert compat_secret not in markdown
    for signal in budget["ownership_growth_signals"]:
        assert isinstance(signal, dict)
        if signal["path"] == "services/api/large.py":
            assert "matching added line" in str(signal["detail"])


def test_structural_file_budget_report_redacts_source_derived_structural_tokens(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    source_path = tmp_path / "services" / "api" / "large.py"
    import_secret = "sk_live_short_secret"
    public_secret = "sk_live_public_entrypoint"
    base_text = _structural_python_fixture(1001, "import os")
    changed_text = _structural_python_fixture(
        1001,
        "import os",
        f"import {import_secret}",
        "",
        f"def {public_secret}() -> int:",
        "    return 1",
    )
    _write(source_path, base_text)
    _commit_all(tmp_path, "base oversized source")
    base_ref = _git_rev_parse(tmp_path, "HEAD")
    _write(source_path, changed_text)

    report = audit_repo_entropy.build_report(tmp_path, structural_base_ref=base_ref)
    markdown = audit_repo_entropy.render_markdown(report)
    budget = report["metadata"]["structural_file_budget"]
    assert isinstance(budget, dict)
    structural_json = json.dumps(budget, sort_keys=True)

    assert "new-import-family" in _structural_growth_signal_types(
        budget,
        "services/api/large.py",
    )
    assert "public-entrypoint" in _structural_growth_signal_types(
        budget,
        "services/api/large.py",
    )
    assert import_secret not in structural_json
    assert public_secret not in structural_json
    assert import_secret not in markdown
    assert public_secret not in markdown

    oversized = _structural_records_by_path(budget["oversized_files"])
    large_record = oversized["services/api/large.py"]
    assert "import_families" not in large_record
    assert audit_repo_entropy._structural_import_family_detail_token(import_secret) in set(
        large_record["import_family_tokens"]
    )

    top_files = budget["top_oversized_files_by_module"]
    assert isinstance(top_files, dict)
    services_api_records = top_files["services/api"]
    assert isinstance(services_api_records, list)
    assert all("import_families" not in record for record in services_api_records)


def test_structural_file_budget_report_and_hard_gate_commands_do_not_write_baseline(
    tmp_path: Path,
) -> None:
    _setup_clean_hard_gate_fixture(tmp_path)
    _write(
        tmp_path / "services" / "api" / "large.py",
        _structural_python_fixture(1001),
    )
    subprocess.run(["git", "add", "services/api/large.py"], cwd=tmp_path, check=True)
    baseline = tmp_path / ".entropy-baseline" / "latest.json"

    for mode in ("report", "hard-gate"):
        result = subprocess.run(
            [
                sys.executable,
                str(AUDIT_SCRIPT),
                "--format",
                "json",
                "--mode",
                mode,
            ],
            cwd=tmp_path,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
        )
        report = json.loads(result.stdout)

        assert result.returncode == (
            1 if report["metadata"].get("hard_gate_failing_count", 0) else 0
        )
        assert report["metadata"]["baseline_written"] is False
        assert report["metadata"]["structural_file_budget"]["mandatory_governance_count"] == 1
        assert not baseline.exists()
