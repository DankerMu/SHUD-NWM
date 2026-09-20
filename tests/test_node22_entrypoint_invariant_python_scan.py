"""node-22 bound system Python is forbidden too (#1103 partition).

Line 340-852 of the pre-#1103 ``tests/test_node22_entrypoint_invariant.py``:
the logical-line / env-launcher / executable-token classifier, the governed
surface scan it drives, and the in-file mutation proofs that keep the scan from
going quietly blind. The governed-surface guards stay in
``tests/test_node22_entrypoint_invariant.py``; the shared constants and
``_read`` live in ``tests/node22_entrypoint_helpers.py``.
"""

from __future__ import annotations

import re
import shlex

from tests.node22_entrypoint_helpers import NODE22_VENV_PY, _read
from tests.production_ops_runbook import combined_text as production_ops_text
from tests.production_ops_runbook import surfaces as production_ops_surfaces

# --- 4b. node-22 bound system Python is forbidden too ------------------------

_DATED_OBSERVATION = re.compile(r"20\d\d-\d\d-\d\d\s+现场验证：?")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:=(?:\"[^\"]*\"|'[^']*'|[^\s]*))")

# Bounded allowlist of exact executables — never a suffix rule, so a
# substituted interpreter at another root (``/wrong/.venv/bin/python``) or a
# stray template path is rejected. ``NODE22_VENV_PY`` is the absolute active
# interpreter; ``.venv/bin/python`` is its in-repo relative form (inside the
# mandatory ``cd /scratch/frd_muziyao/NWM`` ssh session). A different root
# spelling must be added here explicitly, never implied by a suffix.
_NODE22_ALLOWED_PYTHON_EXECUTABLES: frozenset[str] = frozenset(
    {NODE22_VENV_PY, ".venv/bin/python"}
)

# env launcher basenames (``env``/``/usr/bin/env``): env starts whatever
# follows its options and ``NAME=value`` assignments.
_ENV_EXECUTABLE_BASES: frozenset[str] = frozenset({"env"})

# Bound on chained env launchers the resolver unwraps (directly or through
# split strings); deeper chains cannot be fully resolved and fail closed.
_MAX_ENV_NESTING = 4


def _logical_lines(text: str) -> list[tuple[int, str]]:
    r"""Join backslash continuations into logical lines.

    Returns ``(first physical line number, joined logical line)`` so
    diagnostics point at the line an operator would edit. Commands wrap their
    leading ``NAME=value`` assignments across a trailing ``\``; scanning
    physical lines alone would miss wrapped executables and misjudge
    assignment-only lines. The trailing ``\`` is continuation *syntax*, not
    part of the command: it is stripped from the join so it can never become
    the executable token (``PYTHONPATH=/x \`` + ``python3 ...`` would
    otherwise mask the ``python3`` executable).
    """
    out: list[tuple[int, str]] = []
    buf: list[str] = []
    start = 1
    for lineno, line in enumerate(text.splitlines(), 1):
        if not buf:
            start = lineno
        buf.append(line.rstrip().rstrip("\\").rstrip())
        if line.rstrip().endswith("\\"):
            continue
        out.append((start, " ".join(part.strip() for part in buf)))
        buf = []
    if buf:
        out.append((start, " ".join(part.strip() for part in buf)))
    return out


def _strip_assignments(tokens: list[str]) -> list[str]:
    """Drop leading ``NAME=value`` shell assignments from a token list."""
    i = 0
    while i < len(tokens):
        if _ASSIGNMENT.fullmatch(tokens[i]) is None:
            break
        i += 1
    return tokens[i:]


def _resolve_env_launcher(tokens: list[str]) -> tuple[list[str] | None, bool]:
    """Return ``(argv env would start, resolved)``.

    env execs its first non-option, non-assignment argument; every later token
    is that command's argv. Bounded options: ``-i``/``--ignore-environment``,
    ``-u``/``--unset`` (``NAME``, ``=NAME``, ``-uNAME``), ``-C``/``--chdir``
    (``DIR``, ``=DIR``, ``-CDIR``), ``-S``/``--split-string`` (``STRING``,
    ``=STRING``; the string is a simple command shape parsed with
    ``shlex.split`` — its full argv replaces the option and any trailing
    tokens stay as argv), ``--``, and env's own ``NAME=value`` assignments.

    The full argv is returned so a nested ``env`` (directly or through a split
    string) can keep resolving; ``_command_executable`` unwraps such chains to
    a bounded depth. ``resolved=False`` on an unknown option or an unparsable
    split string — the caller must not guess what env would start, so it fails
    closed. ``env python3`` yields argv ``["python3", ...]`` (violation);
    ``env .../.venv/bin/python`` yields the exact interpreter (allowed);
    ``env echo python3`` yields ``["echo", "python3"]`` (not reported).
    """
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if _ASSIGNMENT.fullmatch(tok):
            i += 1
            continue
        if tok in ("-i", "--ignore-environment"):
            i += 1
            continue
        if tok in ("-u", "--unset"):
            if i + 1 >= len(tokens):
                return None, False  # missing option value, cannot resolve
            i += 2  # option consumes the next token (the variable name)
            continue
        if tok.startswith("--unset=") or (tok.startswith("-u") and len(tok) > 2):
            i += 1
            continue
        if tok in ("-C", "--chdir"):
            if i + 1 >= len(tokens):
                return None, False
            i += 2  # chdir consumes the next token (the directory)
            continue
        if tok.startswith("--chdir=") or (tok.startswith("-C") and len(tok) > 2):
            i += 1
            continue
        if tok in ("-S", "--split-string"):
            if i + 1 >= len(tokens):
                return None, False
            try:
                parts = _strip_assignments(shlex.split(tokens[i + 1]))
            except ValueError:
                return None, False  # malformed split string, cannot resolve
            i += 2
            if not parts:
                continue  # empty split-string, keep scanning
            return parts + tokens[i:], True
        if tok.startswith("--split-string=") or (tok.startswith("-S") and len(tok) > 2):
            value = tok.split("=", 1)[1] if "=" in tok else tok[2:]
            try:
                parts = _strip_assignments(shlex.split(value))
            except ValueError:
                return None, False  # malformed split string, cannot resolve
            i += 1
            if not parts:
                continue
            return parts + tokens[i:], True
        if tok == "--":
            i += 1
            continue
        if tok.startswith("-"):
            # Unknown env option: do not guess what env would start — the
            # scanner fails closed on unresolved env lines mentioning python.
            return None, False
        # First non-option, non-assignment token = the command env starts;
        # the rest of the line is that command's argv.
        return tokens[i:], True
    return None, True  # env with only options/assignments: no command


def _command_executable(line: str) -> tuple[str | None, bool]:
    """Return ``(effective command executable, resolved)``.

    ``resolved=False`` only when the command runs through ``env`` and hits an
    unknown option, a malformed split string, or a nested ``env`` chain deeper
    than ``_MAX_ENV_NESTING`` — fail closed rather than guess. ``resolved=True``
    otherwise (blank, shebang, and assignment-only lines have no executable).

    ``env`` launchers unwrap iteratively: the argv ``env`` would start is
    resolved, and if its executable is itself ``env`` the unwrapping continues
    until a non-env executable, no command, or the depth bound.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#!"):
        return None, True  # blank or shebang line, not a command invocation
    try:
        tokens = shlex.split(stripped)
    except ValueError:
        # Malformed quoting: fall back to whitespace tokenization.
        tokens = stripped.split()
    if not tokens:
        return None, True
    tokens = _strip_assignments(tokens)
    if not tokens:
        return None, True
    for _ in range(_MAX_ENV_NESTING + 1):
        if tokens[0] not in _ENV_EXECUTABLE_BASES and tokens[0].rsplit("/", 1)[-1] not in _ENV_EXECUTABLE_BASES:
            return tokens[0], True
        launched = _resolve_env_launcher(tokens[1:])
        if launched is None:
            return None, False  # unknown option / malformed split string
        argv, resolved = launched
        if not resolved or not argv:
            return None, resolved  # env with no command: no executable
        tokens = argv
    # Nesting deeper than the explicit bound: cannot fully resolve, fail closed.
    return None, False


def _executable_token(line: str) -> str | None:
    """Return the effective command executable token (None if unresolved).

    Unresolved ``env`` lines (unknown option) yield ``None``; callers that
    must not skip them (the python scan) use ``_command_executable`` directly.
    """
    token, resolved = _command_executable(line)
    return token if resolved else None


def _allowed_python_executable(exe: str) -> bool:
    """True only for the exact deferred-venv interpreter (or an explicit bound).

    A ``.venv/bin/python`` suffix is NOT enough: a substituted interpreter at
    another root (``/wrong/.venv/bin/python``) would pass a suffix check. Only
    ``NODE22_VENV_PY`` (and its in-repo relative form) is legitimate; any other
    exact path must be added to ``_NODE22_ALLOWED_PYTHON_EXECUTABLES``.
    """
    return exe in _NODE22_ALLOWED_PYTHON_EXECUTABLES


def _is_python_executable_token(exe: str) -> bool:
    """True only for a real python interpreter executable token.

    After removing any directory, the basename must be exactly ``python``,
    ``python3``, or a versioned ``python3.<digits>``. Inline backtick prose
    tokens (`` `.venv/bin/python` ``) and field identifiers
    (``target_python_source_root`` / ``target_python_runtime``) contain
    "python" but their basename is not a python interpreter, so they are NOT
    executables and must never be reported.
    """
    base = exe.rsplit("/", 1)[-1]
    if base in ("python", "python3"):
        return True
    return re.fullmatch(r"python3\.\d+", base) is not None


def test_node22_current_key_entry_and_history_separate() -> None:
    """Historical truth (dated observation) and current guidance stay distinct."""
    # #1103: both pins moved into sub-runbooks (§2 key-entry row, §3.2.2
    # history line), so the scan is over the whole production-ops tree.
    text = production_ops_text()
    # Historical original preserved verbatim; current key-entry names the exact
    # interpreter with no leftover bare python.
    assert "- `python -m services.slurm_gateway` 在 node-22 运行。" in text
    key_entry = [
        line
        for line in text.splitlines()
        if "node-22 compute" in line and "services.slurm_gateway" in line
    ]
    assert key_entry, "node-22 compute key-entry row missing"
    assert "/scratch/frd_muziyao/NWM/.venv/bin/python -m services.slurm_gateway" in key_entry[0], (
        f"key-entry must use exact active interpreter, got: {key_entry[0].strip()}"
    )
    assert "python -m services.slurm_gateway" not in key_entry[0].replace(
        "/scratch/frd_muziyao/NWM/.venv/bin/python -m services.slurm_gateway", "", 1
    )


def test_node22_active_surfaces_have_no_bare_system_python() -> None:
    """Every governed node-22 surface is scanned for substituted python.

    Delegates to ``_scan_for_substituted_python`` (the same classifier the
    mutation tests drive): no bare `python`/`python3` anywhere on a governed
    surface — only the exact deferred-venv interpreter is acceptable, with
    dated historical observation sections exempted as records, never broad
    prose/bullet ignores.
    """
    surfaces = [
        # #1103: index page plus every `production-ops/` sub-runbook.
        *production_ops_surfaces(),
        "docs/runbooks/failed-basin-retry.md",
        "infra/systemd/nhms-scheduler-evidence-retention.service",
        "infra/systemd/nhms-scheduler-journal-retention.service",
        "infra/systemd/nhms-scheduler-journal-retention.timer",
        "infra/systemd/nhms-slurm-gateway.service",
        "scripts/ops/node22_repair_placeholder_hydro_uris.py",
    ]
    bad = []
    for relative in surfaces:
        for lineno, line in _scan_for_substituted_python(_read(relative)):
            bad.append(f"{relative}:{lineno}: {line}")
    assert bad == [], "node-22 bound substituted python:\n" + "\n".join(bad)


def test_python_executable_allowlist_rejects_wrong_root() -> None:
    """Phase-2 false-green regression: the allowlist must reject any wrong root."""
    assert _allowed_python_executable(NODE22_VENV_PY)  # absolute active venv
    assert _allowed_python_executable(".venv/bin/python")  # in-repo relative form
    for exe in (
        "/wrong/.venv/bin/python",
        "/opt/SHUD-NWM/.venv/bin/python",  # stray template root
        "/scratch/frd_muziyao/NWM/.venv/bin/python3",
        "python",
        "python3",
        "/usr/bin/python3",
        "/usr/bin/python3.11",
    ):
        assert not _allowed_python_executable(exe), f"must reject substituted interpreter: {exe}"


def _scan_for_substituted_python(text: str) -> list[tuple[int, str]]:
    """Mirror the real scan's classification over one governed surface.

    Applies the same logical-line / historical-section / command classification
    as the main scanner and returns ``(lineno, line)`` of any substituted
    python executable. ``text`` must be non-empty (an empty surface would
    vacuously pass).
    """
    assert text, "mutation surface must be non-empty"
    bad: list[tuple[int, str]] = []
    in_historical = False
    for lineno, line in _logical_lines(text):
        stripped = line.strip()
        if _DATED_OBSERVATION.fullmatch(stripped):
            in_historical = True
        if in_historical and line.startswith("#"):
            in_historical = False
        if "python" not in line:
            continue
        if "node-27" in line or "/home/nwm/" in line or "nwm@210.77.77.27" in line:
            continue
        if "ROLLBACK_CHECKOUT" in line:
            continue
        if "uv run --no-sync python" in line:
            continue
        if "python3 -m json.tool" in line or "python -m json.tool" in line:
            continue
        if in_historical:
            continue
        if line.strip().startswith("#!"):
            continue
        exe, resolved = _command_executable(line)
        if not resolved:
            bad.append((lineno, line))
            continue
        if exe is None:
            continue
        if not _is_python_executable_token(exe):
            continue
        if _allowed_python_executable(exe):
            continue
        bad.append((lineno, line))
    return bad


def test_mutated_surface_nested_env_reports_substituted_python() -> None:
    """End-to-end proof: the nested env shape on a governed surface goes red.

    The authoritative surface is byte-clean, so isolated in-file mutation is
    the only way to prove the shape reaches the red report without touching
    real governed docs; the same ``_command_executable`` seam decides both.
    """
    # The doc already carries the audit command in its exact form — the
    # verifier's false green was this same shape with the executable swapped
    # to a nested env.
    good = (
        "PYTHONPATH=/scratch/frd_muziyao/NWM "
        "/scratch/frd_muziyao/NWM/.venv/bin/python scripts/audit_first_cycle_initial_state.py"
    )
    # #1103: the anchor moved into a sub-runbook, so its owner is resolved by
    # content. A path literal left on the index would stop matching and the
    # `mutated != text` guard below would be the only thing standing between
    # this proof and a vacuous pass.
    carriers = [
        relative for relative in production_ops_surfaces() if good in _read(relative)
    ]
    assert len(carriers) == 1, f"the audit command must have exactly one owner: {carriers}"
    surface = carriers[0]
    text = _read(surface)
    clean = _scan_for_substituted_python(text)
    assert clean == [], f"authoritative surface must be clean: {clean}"

    mutated = text.replace(
        good,
        "PYTHONPATH=/scratch/frd_muziyao/NWM "
        "/usr/bin/env /usr/bin/env python3 scripts/audit_first_cycle_initial_state.py",
        1,
    )
    assert mutated != text, "mutation anchor not found in authoritative surface"
    bad = _scan_for_substituted_python(mutated)
    assert any(
        "/usr/bin/env /usr/bin/env python3" in line for _, line in bad
    ), f"nested env shape must be reported as substituted python: {bad}"

    mutated2 = text.replace(
        good,
        "PYTHONPATH=/scratch/frd_muziyao/NWM "
        "env -S 'env python3 scripts/audit_first_cycle_initial_state.py'",
        1,
    )
    bad2 = _scan_for_substituted_python(mutated2)
    assert any("env -S 'env python3" in line for _, line in bad2), (
        f"nested split-string shape must be reported: {bad2}"
    )


def test_python_executable_token_recognition() -> None:
    """Only real python interpreter basenames are executables for this scan."""
    for exe in ("python", "python3", "python3.11", "python3.14", "/usr/bin/python3.11", "/wrong/.venv/bin/python"):
        assert _is_python_executable_token(exe), f"must recognize python executable: {exe}"
    for exe in (
        "`.venv/bin/python`",  # inline backtick prose token
        "target_python_source_root",  # field identifier, not an executable
        "target_python_runtime",  # field identifier, not an executable
        "python3-script",  # hyphenated name, not an interpreter
        "my_python_tool",
    ):
        assert not _is_python_executable_token(exe), f"must NOT recognize as python executable: {exe}"


def test_logical_lines_strip_continuation_backslash_from_token() -> None:
    r"""Trailing ``\`` is continuation syntax and must not become the executable."""
    joined = _logical_lines(
        "PYTHONPATH=/scratch/frd_muziyao/NWM \\\n"
        "python3 scripts/publish_scheduler_file_registry.py \\\n"
        "  --basins-root /ghdc/data/nwm/Basins\n"
    )
    assert len(joined) == 1, joined
    lineno, line = joined[0]
    assert lineno == 1
    assert "\\" not in line, f"continuation backslash must be stripped from the join: {line!r}"
    assert _executable_token(line) == "python3", _executable_token(line)
    # The wrapped relative-form interpreter stays a single logical executable.
    joined2 = _logical_lines(
        "NHMS_SCHEDULER_REQUIRE_DIRECT_GRID=false \\\n"
        ".venv/bin/python scripts/publish_scheduler_file_registry.py \\\n"
        "  --basins-root \"$NHMS_BASINS_ROOT\"\n"
    )
    assert _executable_token(joined2[0][1]) == ".venv/bin/python", _executable_token(joined2[0][1])


def test_executable_token_resolves_env_launcher() -> None:
    """`env` starts its first non-option, non-assignment argument."""
    assert _executable_token("/usr/bin/env python3 script.py") == "python3"
    assert _executable_token("env PYTHONPATH=/x python3 script.py") == "python3"
    assert _executable_token("env -i python3 script.py") == "python3"
    assert _executable_token("env --unset=FOO -- python3 script.py") == "python3"
    assert _executable_token("env -u FOO /scratch/frd_muziyao/NWM/.venv/bin/python script.py") == NODE22_VENV_PY
    assert _executable_token(
        "/usr/bin/env /scratch/frd_muziyao/NWM/.venv/bin/python -m services.slurm_gateway"
    ) == NODE22_VENV_PY
    assert _executable_token("env echo python3") == "echo"
    assert _executable_token("PYTHONPATH=/x env python3 script.py") == "python3"
    # GNU/coreutils chdir and split-string options resolve to the started command.
    assert _executable_token("env --chdir=/tmp python3 script.py") == "python3"
    assert _executable_token("env --chdir /tmp python3 script.py") == "python3"
    assert _executable_token("env -C /tmp python3 script.py") == "python3"
    assert _executable_token("env -C/tmp python3 script.py") == "python3"
    assert _executable_token("env -S 'python3 script.py'") == "python3"
    assert _executable_token("env --split-string='python3 script.py'") == "python3"
    assert _executable_token("env --split-string 'python3 script.py'") == "python3"
    # Direct (non-env) executables keep resolving to the first token.
    assert _executable_token("PYTHONPATH=/x python3 script.py") == "python3"
    assert _executable_token("PYTHONPATH=/x .venv/bin/python script.py") == ".venv/bin/python"


def test_nested_env_launchers_resolve_to_real_executable() -> None:
    """Nested `env` launchers unwrap to the executable actually started.

    The verifier-confirmed false greens (``... /usr/bin/env /usr/bin/env
    python3 scripts/audit_first_cycle_initial_state.py`` and ``env -S 'env
    python3 scripts/audit_first_cycle_initial_state.py'``) both actually run
    PATH ``python3``, so they must resolve to ``python3`` and be reported.
    """
    # Directly nested env: the inner env executable must be unwrapped.
    assert _executable_token(
        "PYTHONPATH=/scratch/frd_muziyao/NWM /usr/bin/env /usr/bin/env python3 "
        "scripts/audit_first_cycle_initial_state.py"
    ) == "python3"
    assert _executable_token("env env python3 script.py") == "python3"
    assert _executable_token("/usr/bin/env env python3 script.py") == "python3"
    # Nested split-string: its own env must keep resolving, not be dropped at
    # the outer split's first token.
    assert _executable_token(
        "env -S 'env python3 scripts/audit_first_cycle_initial_state.py'"
    ) == "python3"
    assert _executable_token("env --split-string='env python3 script.py'") == "python3"
    assert _executable_token("env -S 'env -S \"python3 script.py\"'") == "python3"
    assert _executable_token(
        "env -S 'env python3 script.py' -- trailing args"
    ) == "python3"
    # Nested env that starts the exact active interpreter stays allowed.
    assert _executable_token(
        f"env /usr/bin/env {NODE22_VENV_PY} -m services.slurm_gateway"
    ) == NODE22_VENV_PY
    assert _executable_token(
        f"env -S 'env {NODE22_VENV_PY} -m services.slurm_gateway'"
    ) == NODE22_VENV_PY
    # Green controls: `env echo python3` and nested `env env echo python3`
    # start `echo`; `python3` is an argument, never reported.
    assert _executable_token("env env echo python3") == "echo"
    assert _executable_token("env -S 'env echo python3'") == "echo"
    assert _executable_token("/usr/bin/env /usr/bin/env echo python3") == "echo"


def test_nested_env_beyond_depth_fails_closed() -> None:
    """A chain deeper than the bound is unresolved (resolved=False), not skipped."""
    nested = " ".join("/usr/bin/env" for _ in range(_MAX_ENV_NESTING + 1))
    exe, resolved = _command_executable(f"{nested} python3 script.py")
    assert exe is None and resolved is False, (exe, resolved)
    assert _executable_token(f"{nested} python3 script.py") is None
    within = " ".join("/usr/bin/env" for _ in range(_MAX_ENV_NESTING))
    assert _executable_token(f"{within} python3 script.py") == "python3"
    # Nested split-string chains obey the same bound.
    deeper = " ".join("env -S 'env" for _ in range(_MAX_ENV_NESTING + 1))
    exe, resolved = _command_executable(f"{deeper} python3 script.py'")
    assert exe is None and resolved is False, (exe, resolved)


def test_env_unknown_option_fails_closed() -> None:
    """An unknown env option is unresolved; the scanner fails closed, also nested."""
    exe, resolved = _command_executable("env --foo python3 script.py")
    assert exe is None and resolved is False, (exe, resolved)
    exe, resolved = _command_executable("env -Z python3 script.py")
    assert exe is None and resolved is False, (exe, resolved)
    # A nested env launcher still fails closed on the inner unknown option.
    exe, resolved = _command_executable("env /usr/bin/env --foo python3 script.py")
    assert exe is None and resolved is False, (exe, resolved)
    exe, resolved = _command_executable("env -S 'env --foo python3 script.py'")
    assert exe is None and resolved is False, (exe, resolved)
    # A malformed split string cannot be resolved either.
    exe, resolved = _command_executable("env -S 'unbalanced quote python3")
    assert exe is None and resolved is False, (exe, resolved)
    # Recognized forms stay resolved.
    exe, resolved = _command_executable("env echo python3")
    assert exe == "echo" and resolved is True, (exe, resolved)
    exe, resolved = _command_executable("env -C /tmp /scratch/frd_muziyao/NWM/.venv/bin/python -m x")
    assert exe == NODE22_VENV_PY and resolved is True, (exe, resolved)
    exe, resolved = _command_executable("env -S 'python3 script.py'")
    assert exe == "python3" and resolved is True, (exe, resolved)


def test_executable_token_shebang_and_prose_not_commands() -> None:
    """Shebang/comment lines and assignment-only lines are not invocations."""
    assert _executable_token("#!/usr/bin/env python3") is None
    assert _executable_token("#!/bin/sh") is None
    assert _executable_token("PYTHONPATH=/x") is None  # assignment-only
    assert _executable_token("") is None
    # `export PATH=...` is a shell builtin invocation, not an assignment-only
    # line: it resolves to `export`, which is not a python executable, so the
    # scan never reports it.
    assert _executable_token("export PATH=$HOME/.local/bin:$PATH") == "export"
