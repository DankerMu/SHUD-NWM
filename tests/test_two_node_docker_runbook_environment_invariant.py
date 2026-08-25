"""Static contract: two-node Docker runbook repo-Python commands use the exact
checkout interpreter.

``infra/README.two-node-docker.md`` is the current deployment docs entry for
Docker-compose operations. Every executable command that runs a repository
Python script/module must invoke the exact interpreter of the same checkout
(``"$CHECKOUT_ROOT/.venv/bin/python"``) after ``cd "$CHECKOUT_ROOT"``. A bare
``uv run``/``uv sync`` would let ``uv`` see the new ``.python-version == 3.11``
pin and rebuild the shared node-22 active 3.12.7 ``.venv`` before the
operator-approved cutover (#1831); a bare ``python``/``python3`` or a
``.venv/bin/python`` at any other root would resolve to whatever interpreter
the operator has on PATH.

This scanner parses only the runbook's controlled shape (markdown bash fences,
logical lines joined across trailing backslashes, leading ``NAME=value``
assignments stripped). It deliberately does not shell-parse: a bounded helper
recovers the raw first token after leading assignments, so the exact quoted
executable spelling is required (an unquoted ``$CHECKOUT_ROOT/.venv/bin/python``
would resolve via PATH and is red). Any logical line naming a ``scripts/*.py``
target or ``-m json.tool`` must be scanned; a target outside the terminal
allowlist fails closed as unknown instead of being skipped. Locked counts and
target multisets pin the full terminal inventory, so the scan cannot go green
by skipping a whole fence or silently absorbing a new script target.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = REPO_ROOT / "infra" / "README.two-node-docker.md"

# The exact checkout interpreter token every repo-Python command must use.
# The raw executable spelling must be exactly this quoted form.
EXACT_PYTHON = '"$CHECKOUT_ROOT/.venv/bin/python"'
# Fixed CHECKOUT_ROOT form allowed by the runbook (systemd-install fences).
FIXED_CHECKOUT_ROOT = "CHECKOUT_ROOT=/opt/SHUD-NWM"
# Default/overridable CHECKOUT_ROOT form used by every other python fence.
DEFAULT_CHECKOUT_ROOT = 'CHECKOUT_ROOT="${CHECKOUT_ROOT:-$PWD}"'
# The runbook's trust-root derivation; must stay exactly as-is.
TRUST_ROOT_DEFAULT = 'TRUST_ROOT="${TRUST_ROOT:-$(dirname "$CHECKOUT_ROOT")}"'
TRUST_ROOT_FIXED = 'TRUST_ROOT="${TRUST_ROOT:-/opt}"'
# The fence fail-fast guard every self-contained python fence runs under.
SET_GUARD = "set -euo pipefail"
CD_CHECKOUT = 'cd "$CHECKOUT_ROOT"'

# The four repository scripts the runbook runs (validation helpers + evidence
# aggregators). Every tracked executable command must name exactly one of
# these targets or the ``-m json.tool`` module form; a ``scripts/*.py`` line
# naming anything else fails closed as an unknown tracked repo target.
SCRIPT_TARGETS = (
    "scripts/validate_two_node_docker_runtime.py",
    "scripts/validate_two_node_docker_source_trust.py",
    "scripts/validate_readonly_db_boundary.py",
    "scripts/validate_two_node_e2e_evidence.py",
)
JSON_TOOL = "-m json.tool"
# The single allowlisted repo-Python module (the run.json receipt check).
MODULE_ALLOWLIST = frozenset({"json.tool"})
# Controlled pattern: any repo-script path the runbook runs.
_REPO_SCRIPT = re.compile(r"scripts/[A-Za-z0-9_./-]+\.py")
# Controlled module-invocation shape: ``-m <module>`` where the module name
# starts with a letter/underscore and contains only Python module characters.
# This deliberately excludes ``install -m 0600`` (numeric arg, no space
# letter-start) and ``-m0600`` (attached), so only real module invocations are
# tracked.
_MODULE_INVOCATION = re.compile(r"-m\s+([A-Za-z_][A-Za-z0-9_.]*)\b")

# ---- helpers ----------------------------------------------------------------
# Matches a leading shell assignment token (``NAME=value``) so the executable
# can be identified after stripping them.
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:=(?:\"[^\"]*\"|'[^']*'|[^\s]*))")


def _read() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def _bash_fences(text: str) -> list[tuple[int, str]]:
    """Return ``(opening line number, body)`` of every ```bash`` fence."""
    fences: list[tuple[int, str]] = []
    for match in re.finditer(r"```bash\n(.*?)\n```", text, flags=re.DOTALL):
        opening = text.count("\n", 0, match.start()) + 1
        fences.append((opening, match.group(1)))
    return fences


def _logical_lines(body: str) -> list[tuple[int, str]]:
    r"""Join backslash continuations into logical lines.

    Returns ``(physical index within the fence, joined logical line)`` so the
    scanner can prove the prologue precedes the first Python call. The trailing
    ``\`` is continuation syntax, not part of the command: it is stripped from
    the join so it can never become the executable token.
    """
    out: list[tuple[int, str]] = []
    buf: list[str] = []
    start = 0
    for index, line in enumerate(body.splitlines()):
        if not buf:
            start = index
        buf.append(line.rstrip().rstrip("\\").rstrip())
        if line.rstrip().endswith("\\"):
            continue
        out.append((start, " ".join(part.strip() for part in buf)))
        buf = []
    if buf:
        out.append((start, " ".join(part.strip() for part in buf)))
    return out


def _raw_executable_spelling(logical: str) -> str | None:
    """Return the raw first token (quotes preserved) after leading assignments.

    Bounded helper for the runbook's controlled shape: strips leading
    ``NAME=value`` assignment tokens and returns the next whitespace-delimited
    token verbatim. No general shell parsing. A mis-stripped assignment can
    only yield a token that is not the exact interpreter (fail-safe), never a
    false green.
    """
    remainder = logical.strip()
    if not remainder:
        return None
    while True:
        match = _ASSIGNMENT.match(remainder)
        if match is None:
            break
        remainder = remainder[match.end():].lstrip()
    if not remainder:
        return None
    return remainder.split()[0]


def _command_invocation(logical: str) -> tuple[str | None, str | None]:
    """Return ``(raw executable spelling, tracked target)`` for a logical line.

    Any logical line mentioning a ``scripts/*.py`` path or a controlled
    ``-m <module>`` invocation is a tracked repo-Python command. Lines running
    a tracked script must name exactly one script target; zero and multiple
    (masking) both fail closed. A ``scripts/*.py`` path or ``-m <module>``
    outside the terminal allowlists is an unknown tracked repo target/module
    and fails closed instead of being skipped — regardless of the executable
    (exact, bare python/python3, wrong-root, or an env launcher), so a new repo
    script or module can never silently shrink the inventory.
    """
    if not logical.strip():
        return None, None
    script_hits = [target for target in SCRIPT_TARGETS if target in logical]
    # Token-exact module match: the regex captures the full module name, so
    # `-m json.tool_extra` yields `json.tool_extra` (unknown), never `json.tool`.
    modules_present = list(dict.fromkeys(_MODULE_INVOCATION.findall(logical)))
    if len(script_hits) > 1 or len(modules_present) > 1:
        raise AssertionError(f"ambiguous tracked targets on one command: {logical!r}")
    if modules_present:
        if modules_present[0] in MODULE_ALLOWLIST:
            script_hits.append(JSON_TOOL)
        else:
            raise AssertionError(
                f"unknown tracked repo module target in command: {logical!r}"
            )
    if not script_hits:
        if _REPO_SCRIPT.search(logical):
            raise AssertionError(f"unknown tracked repo script target in command: {logical!r}")
        return None, None
    if len(script_hits) != 1:
        raise AssertionError(f"ambiguous tracked targets on one command: {logical!r}")
    return _raw_executable_spelling(logical), script_hits[0]


def _scan_fence(opening: int, body: str) -> list[str]:
    """Return violation messages for one bash fence.

    For every Python invocation proves the prologue appears *before* the first
    Python call in this fence: ``set -euo pipefail``, exactly one legal
    CHECKOUT_ROOT assignment, and exactly one ``cd "$CHECKOUT_ROOT"``, with
    assignment < cd < invocation. Both default and fixed root forms in one
    fence are a violation (they make the root assignment count two).
    """
    violations: list[str] = []
    logical_lines = _logical_lines(body)

    def is_root_assignment(logical: str) -> bool:
        return logical == DEFAULT_CHECKOUT_ROOT or logical == FIXED_CHECKOUT_ROOT

    root_indices = [i for i, (_, logical) in enumerate(logical_lines) if is_root_assignment(logical)]
    cd_indices = [i for i, (_, logical) in enumerate(logical_lines) if logical == CD_CHECKOUT]
    set_indices = [i for i, (_, logical) in enumerate(logical_lines) if logical == SET_GUARD]

    python_calls = [
        (i, logical)
        for i, logical in logical_lines
        if _command_invocation(logical)[1] is not None
    ]
    if not python_calls:
        return violations

    first_call = python_calls[0][0]
    if len(root_indices) != 1:
        violations.append(
            f"fence {opening}: expected exactly one CHECKOUT_ROOT assignment "
            f"before python calls, got {root_indices}"
        )
    elif root_indices[0] > first_call:
        violations.append(
            f"fence {opening}: CHECKOUT_ROOT assignment must precede the first python call"
        )
    if len(cd_indices) != 1:
        violations.append(
            f"fence {opening}: expected exactly one `cd \"$CHECKOUT_ROOT\"` before python calls, got {cd_indices}"
        )
    elif cd_indices[0] > first_call:
        violations.append(
            f"fence {opening}: `cd \"$CHECKOUT_ROOT\"` must precede the first python call"
        )
    if root_indices and cd_indices and root_indices[0] > cd_indices[0]:
        violations.append(
            f"fence {opening}: CHECKOUT_ROOT assignment must come before `cd \"$CHECKOUT_ROOT\"`"
        )
    if len(set_indices) != 1:
        violations.append(
            f"fence {opening}: python fence must run under exactly one {SET_GUARD}, got {set_indices}"
        )
    elif set_indices[0] > first_call:
        violations.append(
            f"fence {opening}: `{SET_GUARD}` must precede the first python call"
        )

    for i, logical in python_calls:
        exe, target = _command_invocation(logical)
        if exe != EXACT_PYTHON:
            violations.append(
                f"fence {opening} line {i}: repo python must run via {EXACT_PYTHON}, "
                f"got executable {exe!r} (target {target})"
            )
    return violations


def _scan(text: str) -> list[str]:
    violations: list[str] = []
    for opening, body in _bash_fences(text):
        violations.extend(_scan_fence(opening, body))
    return violations


def _invocation_counts(text: str) -> tuple[int, Counter[str]]:
    """Return ``(python fence count, target multiset)`` for the whole text."""
    python_fences = 0
    counts: Counter[str] = Counter()
    for _, body in _bash_fences(text):
        fence_calls = 0
        for _, logical in _logical_lines(body):
            _, target = _command_invocation(logical)
            if target is not None:
                counts[target] += 1
                fence_calls += 1
        if fence_calls:
            python_fences += 1
    return python_fences, counts


# ---- main contract ----------------------------------------------------------
def test_runbook_bash_fences_use_exact_checkout_interpreter() -> None:
    text = _read()
    assert _bash_fences(text), "runbook must contain bash fences"
    violations = _scan(text)
    assert violations == [], "runbook repo-Python command violations:\n" + "\n".join(violations)


def test_runbook_has_no_bare_or_environment_updating_python_in_executable_fences() -> None:
    text = _read()
    for opening, body in _bash_fences(text):
        for _, logical in _logical_lines(body):
            if "uv run" in logical or "uv sync" in logical:
                pytest.fail(f"fence {opening} contains bare/environment-updating uv: {logical!r}")
            if _command_invocation(logical)[1] is None:
                continue
            if any(token in logical for token in ("uv run", "uv sync", "python3", "python -m")):
                pytest.fail(f"fence {opening} masks a repo python with a bare entry: {logical!r}")


def test_runbook_no_bare_system_python_json_tool() -> None:
    text = _read()
    assert "python -m json.tool" not in text, "bare `python -m json.tool` must be converted"
    assert "python3 -m json.tool" not in text
    assert JSON_TOOL in text, "exact-interpreter `-m json.tool` form must be present"


# ---- pinned terminal inventory ---------------------------------------------
def test_runbook_python_invocation_inventory_locked() -> None:
    """Pin the full terminal inventory so the scan cannot skip a fence to go green.

    The scanner's green gate is the fence checks above; this test locks the
    complete set of repo-Python invocations: 17 python fences carrying 25
    fence commands (including the exact-interpreter ``-m json.tool`` receipt
    check, which is itself a self-contained bash fence). The target multiset
    is pinned exactly, so deleting or rewording a whole python fence is a
    visible diff against the locked truth instead of a silently narrower scan.
    """
    text = _read()
    python_fences, counts = _invocation_counts(text)
    assert python_fences == 17, f"expected 17 python fences, got {python_fences}"
    assert sum(counts.values()) == 25, f"expected 25 repo-Python invocations, got {dict(counts)}"
    assert counts == Counter(
        {
            "scripts/validate_two_node_docker_source_trust.py": 15,
            "scripts/validate_two_node_docker_runtime.py": 5,
            "scripts/validate_readonly_db_boundary.py": 3,
            "scripts/validate_two_node_e2e_evidence.py": 1,
            JSON_TOOL: 1,
        }
    ), f"repo-Python target multiset drifted: {dict(counts)}"


def test_runbook_python_fences_have_expected_checkout_root_shapes() -> None:
    text = _read()
    default_count = 0
    fixed_count = 0
    for _, body in _bash_fences(text):
        lines = body.splitlines()
        if not any(_command_invocation(logical)[1] is not None for _, logical in _logical_lines(body)):
            continue  # only python fences carry the exact-interpreter contract
        if DEFAULT_CHECKOUT_ROOT in lines:
            default_count += 1
        if FIXED_CHECKOUT_ROOT in lines:
            fixed_count += 1
    assert default_count == 15, f"expected 15 default CHECKOUT_ROOT python fences, got {default_count}"
    assert fixed_count == 2, f"expected 2 fixed /opt/SHUD-NWM CHECKOUT_ROOT python fences, got {fixed_count}"
    assert default_count + fixed_count == 17


# ---- in-memory mutation regressions ----------------------------------------
def _mutate_and_scan(mutated: str) -> list[str]:
    """Run the same scanner over an in-memory mutated runbook."""
    assert mutated != _read(), "mutation anchor must actually change the runbook"
    return _scan(mutated)


def _first_python_fence_body(text: str) -> str:
    """Body of the first bash fence that runs a repo python command."""
    return next(
        body
        for _, body in _bash_fences(text)
        if any(_command_invocation(logical)[1] is not None for _, logical in _logical_lines(body))
    )


def _move_line_after_call(text: str, line: str) -> str:
    """Move a single prologue line to the end of the first python fence.

    The fence end is necessarily after the first python call, so the moved
    prologue line can no longer precede it.
    """
    body = _first_python_fence_body(text)
    lines = body.splitlines()
    assert line in lines, f"line {line!r} must be present in the first python fence"
    moved = [ln for ln in lines if ln != line] + [line]
    return text.replace(body, "\n".join(moved), 1)


def test_mutation_exact_command_replaced_by_uv_run_goes_red() -> None:
    text = _read()
    mutated = text.replace(EXACT_PYTHON + " scripts/validate_two_node_e2e_evidence.py",
                           "uv run python scripts/validate_two_node_e2e_evidence.py", 1)
    violations = _mutate_and_scan(mutated)
    assert any("must run via" in v and "uv" in v for v in violations), violations


def test_mutation_exact_command_replaced_by_python3_goes_red() -> None:
    text = _read()
    mutated = text.replace(EXACT_PYTHON + " scripts/validate_two_node_e2e_evidence.py",
                           "python3 scripts/validate_two_node_e2e_evidence.py", 1)
    violations = _mutate_and_scan(mutated)
    assert any("must run via" in v and "python3" in v for v in violations), violations


def test_mutation_exact_command_replaced_by_wrong_root_goes_red() -> None:
    text = _read()
    mutated = text.replace(EXACT_PYTHON + " scripts/validate_two_node_e2e_evidence.py",
                           '"/wrong/.venv/bin/python" scripts/validate_two_node_e2e_evidence.py', 1)
    violations = _mutate_and_scan(mutated)
    assert any("must run via" in v and "wrong" in v for v in violations), violations


def test_mutation_exact_command_unquoted_goes_red() -> None:
    """An unquoted exact token resolves to a PATH lookup and must be red."""
    text = _read()
    mutated = text.replace(
        EXACT_PYTHON + " scripts/validate_two_node_e2e_evidence.py",
        "$CHECKOUT_ROOT/.venv/bin/python scripts/validate_two_node_e2e_evidence.py",
        1,
    )
    violations = _mutate_and_scan(mutated)
    assert any("must run via" in v and "got executable '$CHECKOUT_ROOT/.venv/bin/python'" in v
               for v in violations), violations


def test_mutation_later_safe_token_cannot_mask_bad_executable() -> None:
    text = _read()
    mutated = text.replace(
        EXACT_PYTHON + " scripts/validate_two_node_e2e_evidence.py",
        'python3 scripts/validate_two_node_e2e_evidence.py "$CHECKOUT_ROOT/.venv/bin/python"',
        1,
    )
    assert EXACT_PYTHON in text  # anchor exists in the runbook
    violations = _scan(mutated)
    assert any("must run via" in v and "python3" in v for v in violations), violations


def test_mutation_unknown_script_target_fails_closed() -> None:
    """A new ``scripts/*.py`` target outside the allowlist must fail closed."""
    text = _read()
    anchor = EXACT_PYTHON + " scripts/validate_two_node_e2e_evidence.py"
    assert anchor in text, "e2e-evidence anchor missing"
    mutated = text.replace(anchor, EXACT_PYTHON + " scripts/new_validator.py", 1)
    with pytest.raises(AssertionError, match="unknown tracked repo script target"):
        _scan(mutated)


def test_mutation_unknown_script_target_appended_fails_closed() -> None:
    """Appending a new ``scripts/*.py`` command to the runbook fails closed."""
    text = _read()
    body = _first_python_fence_body(text)
    first_line = body.splitlines()[0]
    mutated = text.replace(
        "```bash\n" + first_line,
        "```bash\n" + EXACT_PYTHON + " scripts/new_validator.py\n" + first_line,
        1,
    )
    with pytest.raises(AssertionError, match="unknown tracked repo script target"):
        _scan(mutated)


def test_mutation_exact_interpreter_unknown_module_fails_closed() -> None:
    """Exact interpreter with an unknown ``-m`` module must fail closed."""
    text = _read()
    anchor = EXACT_PYTHON + " -m json.tool"
    assert anchor in text, "json.tool exact-interpreter anchor missing"
    mutated = text.replace(
        anchor,
        EXACT_PYTHON + " -m packages.foo",
        1,
    )
    with pytest.raises(AssertionError, match="unknown tracked repo module target"):
        _scan(mutated)


def test_mutation_bare_python_unknown_module_fails_closed() -> None:
    """Bare python3 with an unknown ``-m`` module must fail closed."""
    text = _read()
    anchor = EXACT_PYTHON + " -m json.tool"
    assert anchor in text, "json.tool exact-interpreter anchor missing"
    mutated = text.replace(
        anchor,
        "python3 -m scripts.foo",
        1,
    )
    with pytest.raises(AssertionError, match="unknown tracked repo module target"):
        _scan(mutated)


def test_mutation_env_launcher_unknown_module_fails_closed() -> None:
    """An env launcher running an unknown ``-m`` module must fail closed."""
    text = _read()
    anchor = EXACT_PYTHON + " -m json.tool"
    assert anchor in text, "json.tool exact-interpreter anchor missing"
    mutated = text.replace(
        anchor,
        "/usr/bin/env python3 -m scripts.foo",
        1,
    )
    with pytest.raises(AssertionError, match="unknown tracked repo module target"):
        _scan(mutated)


def test_mutation_json_tool_extra_not_confused_with_json_tool() -> None:
    """``-m json.tool_extra`` is a different module and must fail closed."""
    text = _read()
    anchor = EXACT_PYTHON + " -m json.tool"
    assert anchor in text, "json.tool exact-interpreter anchor missing"
    mutated = text.replace(
        anchor,
        EXACT_PYTHON + " -m json.tool_extra",
        1,
    )
    with pytest.raises(AssertionError, match="unknown tracked repo module target"):
        _scan(mutated)


def test_install_minus_m_numeric_arg_is_not_a_module_invocation() -> None:
    """`install -m 0600` (numeric arg) is not a repo-Python module invocation."""
    compute_install = (
        'install -m 0600 "$CHECKOUT_ROOT/infra/env/compute.example" '
        '"$CHECKOUT_ROOT/infra/env/compute.env"'
    )
    systemd_install = (
        'sudo install -m 0644 "$CHECKOUT_ROOT/infra/systemd/nhms-compute-compose.service" '
        "/etc/systemd/system/nhms-compute-compose.service"
    )
    for logical in (compute_install, systemd_install):
        _, target = _command_invocation(logical)
        assert target is None, f"install -m with numeric arg must not be tracked: {logical!r}"


def test_mutation_removed_checkout_root_goes_red() -> None:
    text = _read()
    mutated = text.replace(DEFAULT_CHECKOUT_ROOT, "", 1)
    violations = _mutate_and_scan(mutated)
    assert any("exactly one CHECKOUT_ROOT assignment" in v for v in violations), violations


def test_mutation_checkout_root_moved_after_call_goes_red() -> None:
    """Moving the CHECKOUT_ROOT assignment after the first python call is red."""
    mutated = _move_line_after_call(_read(), DEFAULT_CHECKOUT_ROOT)
    violations = _mutate_and_scan(mutated)
    assert any("CHECKOUT_ROOT assignment must precede" in v for v in violations), violations


def test_mutation_cd_moved_after_call_goes_red() -> None:
    """Moving the cd after the first python call is red."""
    mutated = _move_line_after_call(_read(), CD_CHECKOUT)
    violations = _mutate_and_scan(mutated)
    assert any('`cd "$CHECKOUT_ROOT"` must precede' in v for v in violations), violations


def test_mutation_set_moved_after_call_goes_red() -> None:
    """Moving set -euo pipefail after the first python call is red."""
    mutated = _move_line_after_call(_read(), SET_GUARD)
    violations = _mutate_and_scan(mutated)
    assert any("`set -euo pipefail` must precede" in v for v in violations), violations


def test_mutation_removed_cd_goes_red() -> None:
    """Remove the cd line from the first python fence."""
    text = _read()
    body = _first_python_fence_body(text)
    assert CD_CHECKOUT in body, "first python fence must cd"
    mutated = text.replace(body, body.replace(CD_CHECKOUT + "\n", "", 1), 1)
    violations = _mutate_and_scan(mutated)
    assert any("expected exactly one `cd" in v for v in violations), violations


def test_mutation_removed_set_euo_pipefail_goes_red() -> None:
    text = _read()
    mutated = text.replace(SET_GUARD, "", 1)
    violations = _mutate_and_scan(mutated)
    assert any("set -euo pipefail" in v for v in violations), violations


def test_mutation_removed_whole_python_fence_goes_red() -> None:
    text = _read()
    # Remove the whole final-aggregation section (heading + its e2e-evidence
    # fence) so that command is genuinely gone from the runbook. The remaining
    # fences stay individually valid, so the per-fence scan stays green — the
    # gate that must go red is the locked inventory count.
    section = re.search(
        r"最终聚合命令：\n\n```bash\nset -euo pipefail\n"
        r": \"\$\{EVIDENCE_ROOT:.*?scripts/validate_two_node_e2e_evidence\.py.*?```",
        text,
        flags=re.DOTALL,
    )
    assert section is not None, "final-aggregation section anchor not found"
    mutated = text.replace(section.group(0), "", 1)
    violations = _mutate_and_scan(mutated)
    assert violations == [], "remaining fences stay individually valid after the removal"
    python_fences, counts = _invocation_counts(mutated)
    assert python_fences == 16, f"removing one python fence must drop the locked count, got {python_fences}"
    assert sum(counts.values()) == 24, f"removing one python fence must drop the locked count, got {dict(counts)}"


def test_mutation_removed_whole_json_tool_fence_goes_red() -> None:
    """Removing the json.tool fence must drop the locked inventory count."""
    text = _read()
    body = next(
        body for _, body in _bash_fences(text) if JSON_TOOL in body
    )
    fence = f"```bash\n{body}\n```"
    assert fence in text, "json.tool fence must be a self-contained bash fence"
    mutated = text.replace(fence, "", 1)
    python_fences, counts = _invocation_counts(mutated)
    assert python_fences == 16, f"removing the json.tool fence must drop the python fence count, got {python_fences}"
    assert sum(counts.values()) == 24, f"removing the json.tool fence must drop the locked count, got {dict(counts)}"
