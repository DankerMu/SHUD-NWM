"""Static contract: active node-22 entrypoints preserve the deferred .venv.

Before the operator-approved maintenance cutover, nothing bound to
``/scratch/frd_muziyao/NWM`` may create, update, replace, or synchronize the
shared active ``.venv``. Allowed pre-window forms: a wrapper exec'ing the exact
active ``.venv/bin/python``; that interpreter directly (``-m`` for console
scripts); ``uv run --no-sync`` for read-only observation (not pin proof); a
disposable detached worktree with its own synced environment (bounded
pin-acceptance evidence only, never the e2e/grib oracle). Forbidden in the
active checkout: bare/environment-updating ``uv run``, ``uv sync``,
``--active``, and system Python (``python``/``python3``) as a substitute.

The e2e/grib oracle is **node-27** (``docs/runbooks/ci-test-routing.md``): after
the interactive ``ssh``, the lane runs under ``set -euo pipefail`` so checkout
failures and a non-3.11 environment abort before pytest; it asserts Python 3.11
with an executable ``uv run --no-sync python -c "import sys; assert
sys.version_info[:2] == (3, 11), sys.version"`` guard, then runs
``uv run --no-sync pytest``.

Explicit exclusions (not a broad ignore): unrelated node-27 surfaces; the
isolated rollback-worktree ``(cd "$ROLLBACK_CHECKOUT" && uv sync ...)``; dated
historical observation sections (heading-bounded "现场验证"/incident records)
and whole-document-marked historical runbooks — observed records, not current
executable guidance.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
NODE22_ACTIVE = "/scratch/frd_muziyao/NWM"
NODE22_VENV_PY = f"{NODE22_ACTIVE}/.venv/bin/python"
NODE27_PY = "/home/nwm/NWM"


def _read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


# --- 1. systemd units use exact interpreters, no bare uv ExecStart ----------


def test_retention_unit_uses_exact_interpreter_and_absolute_script() -> None:
    unit = _read("infra/systemd/nhms-scheduler-evidence-retention.service")
    execstart = [
        line.strip() for line in unit.splitlines() if line.strip().startswith("ExecStart=")
    ]
    # Exactly one ExecStart: the full exact directive (deferred-venv
    # interpreter + absolute script path, Checklist §2, robust to working-dir
    # drift). Any wrong-first/correct-later pair or extra ExecStart is a
    # violation — only the complete directive is acceptable.
    expected = (
        f"ExecStart={NODE22_VENV_PY} "
        f"{NODE22_ACTIVE}/scripts/node22_scheduler_evidence_retention.py"
    )
    assert execstart == [expected], (
        f"retention ExecStart must be exactly the exact directive; got: {execstart}"
    )


def test_slurm_gateway_unit_uses_exact_interpreter() -> None:
    unit = _read("infra/systemd/nhms-slurm-gateway.service")
    execstart = [line.strip() for line in unit.splitlines() if line.strip().startswith("ExecStart=")]
    assert execstart, "gateway unit must declare ExecStart"
    # node-22 compute key entry: exact deferred-venv interpreter, never a
    # template path or bare uv.
    assert (
        execstart[0] == "ExecStart=/opt/SHUD-NWM/.venv/bin/python -m services.slurm_gateway"
    ), f"gateway ExecStart must be the exact deferred-venv interpreter: {execstart[0]}"
    for line in execstart:
        assert "uv run" not in line
        assert "uv sync" not in line


# --- 2. current-production-ops node-22 active commands use exact venv -------


def _command_lines(text: str) -> list[tuple[int, str]]:
    """Return lines that look like an executable command, excluding prose."""
    out = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if "uv run" not in line and "uv sync" not in line:
            continue
        # Prose merely naming the prohibition ("禁止 `uv run`") is not a command.
        if "禁止" in line or "不要" in line or "不是" in line or "不得" in line:
            continue
        if line.strip().startswith((">", "#", "-", "*")):
            continue
        out.append((lineno, line))
    return out


def test_current_production_ops_node22_active_uses_exact_venv() -> None:
    text = _read("docs/runbooks/current-production-ops.md")
    bare_uv_node22 = []
    for lineno, line in _command_lines(text):
        # node-27 commands and the disposable rollback-worktree sync stay unchanged.
        if NODE27_PY in line or "nwm@210.77.77.27" in line or "node-27" in line:
            continue
        if "/home/nwm/" in line:  # node-27 geo etc.
            continue
        if "ROLLBACK_CHECKOUT" in line and "uv sync" in line:
            continue
        bare_uv_node22.append((lineno, line.strip()))
    assert bare_uv_node22 == [], f"node-22 active bare uv in current-production-ops: {bare_uv_node22}"


def test_current_production_ops_keeps_node27_and_rollback_isolated() -> None:
    text = _read("docs/runbooks/current-production-ops.md")
    assert NODE27_PY in text  # node-27 exact-venv commands preserved
    assert '(cd "$ROLLBACK_CHECKOUT" && uv sync --all-extras --dev)' in text
    remaining = [
        (lineno, line)
        for lineno, line in _command_lines(text)
        if not (
            NODE27_PY in line
            or "nwm@210.77.77.27" in line
            or "node-27" in line
            or "/home/nwm/" in line
            or ("ROLLBACK_CHECKOUT" in line and "uv sync" in line)
        )
    ]
    # Every node-22 active executable command now uses the exact venv.
    assert remaining == [], f"node-22 active uv commands remain: {remaining}"


# --- 3. failed-basin demotion and placeholder repair use exact venv ---------


def test_failed_basin_demotion_uses_exact_venv() -> None:
    text = _read("docs/runbooks/failed-basin-retry.md")
    # Generic/local validation (fake-slurm, pytest) is not node-22 bound; the
    # demotion and recovery commands use the exact interpreter.
    for lineno, line in _command_lines(text):
        if "fake-slurm" in line or "pytest -q" in line:
            continue  # generic validation, not node-22 active
        if "/scratch/frd_muziyao/NWM/.venv/bin/python" in line:
            continue
        assert False, f"bare uv command in failed-basin-retry: {lineno}: {line.strip()}"
    assert f"{NODE22_VENV_PY} -m services.orchestrator.cli" in text


def test_placeholder_repair_usage_uses_exact_venv() -> None:
    text = _read("scripts/ops/node22_repair_placeholder_hydro_uris.py")
    assert "uv run python scripts/ops/node22_repair_placeholder_hydro_uris.py" not in text
    assert NODE22_VENV_PY in text


# --- 4. ci-test-routing node-27 oracle + conftest pointer --------------------


def _node27_bash_fence(text: str) -> list[str]:
    """Return the fenced bash block running the e2e/grib lane on node-27."""
    blocks = re.findall(r"```bash\n(.*?)```", text, re.DOTALL)
    for block in blocks:
        if "nwm@210.77.77.27" in block and "uv run --no-sync pytest" in block:
            return block.splitlines()
    raise AssertionError("node-27 bash fence (ssh + uv run --no-sync pytest) not found")


def test_ci_test_routing_uses_node27_oracle() -> None:
    text = _read("docs/runbooks/ci-test-routing.md")
    # node-27 oracle runs `uv run --no-sync` (active venv is already 3.11);
    # the deferred node-22 checkout must not be the execution site. The 3.11
    # guard must be executable, not a comment-only `python -V`.
    assert "node-27" in text
    assert "uv run --no-sync" in text
    assert "uv run --no-sync python -c" in text
    assert "sys.version_info[:2] == (3, 11)" in text
    assert "sys.version" in text
    assert "uv run --no-sync pytest" in text
    assert "uv sync" not in text
    assert "cd /scratch/frd_muziyao/NWM" not in text
    assert "/home/nwm/NWM/.venv/bin/python" not in text
    assert "tee artifacts/ci-routing/e2e-grib" in text


def test_ci_routing_fence_failfast_ordering() -> None:
    """Fail-fast, ordered lane: ssh < set -euo pipefail < cd < guard < pytest."""
    lines = _node27_bash_fence(_read("docs/runbooks/ci-test-routing.md"))

    def idx(pred: object, what: str) -> int:
        for i, line in enumerate(lines):
            if pred(line):  # type: ignore[operator]
                return i
        raise AssertionError(f"node-27 bash fence is missing: {what}")

    i_ssh = idx(lambda ln: "ssh -p 32099 nwm@210.77.77.27" in ln, "interactive ssh")
    i_set = idx(lambda ln: ln.strip() == "set -euo pipefail", "set -euo pipefail")
    i_cd = idx(lambda ln: ln.strip().startswith("cd /home/nwm/NWM"), "cd /home/nwm/NWM")
    i_pull = idx(lambda ln: "git pull --ff-only" in ln, "git pull --ff-only")
    i_guard = idx(
        lambda ln: "uv run --no-sync python -c" in ln and "(3, 11)" in ln, "3.11 guard"
    )
    i_pytest = idx(lambda ln: "uv run --no-sync pytest" in ln, "uv run --no-sync pytest")

    # set -euo pipefail runs in the remote shell: after ssh, before cd/pull, guard, pytest.
    assert i_ssh < i_set, "set -euo pipefail must come after the ssh line (remote shell)"
    assert i_set < i_cd < i_pull, "set -euo pipefail must precede cd / git pull"
    assert i_set < i_guard < i_pytest, "set -euo pipefail then 3.11 guard must precede pytest"

    # The guard is the abort gate; it must not be silenced with || true.
    guard = lines[i_guard].strip()
    assert "|| true" not in guard, f"3.11 guard must not be silenced with || true: {guard}"

    # pytest still pipes into the tee receipt (logical line joins the `\`).
    logical = lines[i_pytest]
    for line in lines[i_pytest + 1:]:
        logical += line
        if not line.rstrip().endswith("\\"):
            break
    assert "tee artifacts/ci-routing/e2e-grib" in logical, "pytest must still be piped to tee"

    # The whole lane must stay synchronization-free.
    assert "uv sync" not in "\n".join(lines)


def test_conftest_skip_guidance_points_to_runbook() -> None:
    text = _read("tests/conftest.py")
    # Skip guidance points at the runbook authority only, no bare-command
    # duplicate; the generic NHMS_RUN_INTEGRATION opt-in hint stays.
    assert "uv run pytest -m" not in text
    assert "ci-test-routing.md" in text
    assert "node-27" in text
    assert "node-22" not in text


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
    text = _read("docs/runbooks/current-production-ops.md")
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
    r"""A bare `python`/`python3` on a node-22 active surface is forbidden.

    The exact interpreter must be named explicitly; a bare `python -m
    services.slurm_gateway` or `python scripts/...` would resolve to whatever
    system/active python is on PATH.

    Classification is by the *executable token*, not whole-line substrings:
    leading ``NAME=value`` assignments are stripped first, then the first token
    decides. Only ``_NODE22_ALLOWED_PYTHON_EXECUTABLES`` (absolute or in-repo
    relative active interpreter) is allowed; a ``.venv/bin/python`` at any
    other root and bare ``python``/``python3`` are forbidden. This neither
    skips a whole ``PYTHONPATH=...`` line nor lets a ``.venv/bin/python``
    elsewhere on the line mask a ``python3`` executable, nor accepts a root by
    suffix.

    Historical handling is a line-by-line state machine evaluated BEFORE the
    python check: a line whose stripped text exactly matches a dated
    observation heading (``20\d\d-\d\d-\d\d 现场验证``) enters the historical
    section; the next Markdown heading leaves it. Only lines inside that
    bounded range are exempt — no broad bullets/prose ignore.
    """
    surfaces = [
        "docs/runbooks/current-production-ops.md",
        "docs/runbooks/failed-basin-retry.md",
        "infra/systemd/nhms-scheduler-evidence-retention.service",
        "infra/systemd/nhms-slurm-gateway.service",
        "scripts/ops/node22_repair_placeholder_hydro_uris.py",
    ]
    bad = []
    for relative in surfaces:
        text = _read(relative)
        in_historical = False
        for lineno, line in _logical_lines(text):
            stripped = line.strip()
            # Section structure first: enter on a dated-observation heading,
            # leave on the next Markdown heading; then the python check.
            if _DATED_OBSERVATION.fullmatch(stripped):
                in_historical = True
            if in_historical and line.startswith("#"):
                in_historical = False
            if "python" not in line:
                continue
            # node-27 / rollback / non-command surfaces are out of scope.
            if "node-27" in line or "/home/nwm/" in line or "nwm@210.77.77.27" in line:
                continue
            if "ROLLBACK_CHECKOUT" in line:
                continue
            if "uv run --no-sync python" in line:
                continue  # observation-only, allowed
            if "python3 -m json.tool" in line or "python -m json.tool" in line:
                continue  # node-27 receipt inspection idiom
            if in_historical:
                continue  # dated observation record, not current guidance
            if line.strip().startswith("#!"):
                continue  # shebang line, not a command invocation
            # Command classification: executable token after assignments. An
            # unresolvable env launcher (unknown option) on a python-mentioning
            # line must be reported, never skipped.
            exe, resolved = _command_executable(line)
            if not resolved:
                bad.append(f"{relative}:{lineno}: {line}")
                continue
            if exe is None:
                continue  # assignment/prose-only, no executable
            # Only a real python interpreter executable can be a violation;
            # backtick prose tokens and field identifiers are never reported.
            if not _is_python_executable_token(exe):
                continue
            if _allowed_python_executable(exe):
                continue  # exact bounded interpreter, allowed
            # Any other python executable — bare python/python3, a wrong-root
            # ``/wrong/.venv/bin/python``, a stray template path — is a
            # substituted interpreter on a node-22 active surface.
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
    surface = "docs/runbooks/current-production-ops.md"
    # The doc already carries the audit command in its exact form — the
    # verifier's false green was this same shape with the executable swapped
    # to a nested env.
    good = (
        "PYTHONPATH=/scratch/frd_muziyao/NWM "
        "/scratch/frd_muziyao/NWM/.venv/bin/python scripts/audit_first_cycle_initial_state.py"
    )
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


# --- 5. QHH whole-document historical marker --------------------------------


def test_qhh_bringup_has_historical_marker() -> None:
    text = _read("docs/runbooks/qhh-22-business-bringup.md")
    assert text.startswith("---\n"), "qhh bring-up must carry YAML front matter"
    front = text.split("---", 2)[1]
    assert "status: historical baseline" in front
    assert "current_authority" in front
    assert "current-production-ops.md" in front
    assert "scripts/diagnostic/qhh/README.md" in front
    assert "status_since" in front
    assert "archive_scope: whole-document" in front
    assert "retained_for" in front


def test_qhh_readme_production_replacement_uses_exact_interpreter() -> None:
    """QHH Production Replacement must use the exact interpreter, never bare uv."""
    text = _read("scripts/diagnostic/qhh/README.md")
    # Bounded section: the Production Replacement heading to the next heading.
    match = re.search(
        r"^## Production Replacement\n(.*?)(?=^## )",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert match, "QHH README Production Replacement section missing"
    fence = re.search(r"```bash\n(.*?)```", match.group(1), re.DOTALL)
    assert fence, "Production Replacement code fence missing"
    lines = [ln.strip() for ln in fence.group(1).splitlines() if ln.strip()]
    prefix = f"{NODE22_VENV_PY} -m services.orchestrator.cli plan-production "
    assert len(lines) == 3, f"expected 3 plan-production lines, got: {lines}"
    for line in lines:
        assert line.startswith(prefix), f"Production Replacement line must use exact interpreter: {line}"
        assert "uv run" not in line, f"bare uv run in Production Replacement: {line}"
    # All three modes present with their arguments preserved.
    joined = "\n".join(lines)
    assert "--dry-run --source gfs --source IFS --workspace-root" in joined
    assert "--submit --source gfs --source IFS --workspace-root" in joined
    assert "--continuous --submit --max-passes" in joined


# --- 6. instructions source + generated roots byte-exact ---------------------


def _composition(header_lines: list[str], tail: str | None = None) -> str:
    header = "".join(header_lines)
    shared = _read("instructions/agents/shared.md")
    if tail is None:
        return header + shared
    return header + shared + tail + _read("instructions/agents/codex.md")


def test_generated_roots_byte_exact() -> None:
    hdr_c = [
        "<!--\n",
        "Generated from instructions/agents/shared.md and instructions/agents/claude.md\n",
        "by the project-instruction-bootstrap skill. Edit those sources, then re-run the skill.\n",
        "Do not hand-edit this file.\n",
        "-->\n\n",
    ]
    hdr_a = [
        "<!--\n",
        "Generated from instructions/agents/shared.md and instructions/agents/codex.md\n",
        "by the project-instruction-bootstrap skill. Edit those sources, then re-run the skill.\n",
        "Do not hand-edit this file.\n",
        "-->\n\n",
    ]
    assert _read("CLAUDE.md") == _composition(hdr_c)
    assert _read("AGENTS.md") == _composition(hdr_a, tail="\n")


def test_instruction_roots_contain_command_contract() -> None:
    for relative in ("instructions/agents/shared.md", "CLAUDE.md", "AGENTS.md"):
        text = _read(relative)
        node22 = [ln for ln in text.splitlines() if "node-22" in ln and "3.12.7" in ln]
        assert node22, f"{relative}: node-22 contract line missing"
        line = node22[0]
        for term in (
            "uv sync",
            "uv run --no-sync",
            "--active",
            "系统 Python",
            "维护窗口",
            "#1831",
        ):
            assert term in line, f"{relative}: missing '{term}'"
        assert "openspec/changes/" not in line, f"{relative}: active-change link"


# --- 7. sibling-surface scan: any newly introduced bare uv on governed files -


def test_sibling_scan_no_new_bare_uv_on_governed_node22_surfaces() -> None:
    governed = [
        "infra/systemd/nhms-scheduler-evidence-retention.service",
        "infra/systemd/nhms-slurm-gateway.service",
        "docs/runbooks/current-production-ops.md",
        "docs/runbooks/failed-basin-retry.md",
        "docs/runbooks/ci-test-routing.md",
        "scripts/ops/node22_repair_placeholder_hydro_uris.py",
        "tests/conftest.py",
        "instructions/agents/shared.md",
        "CLAUDE.md",
        "AGENTS.md",
    ]
    bad = []
    for relative in governed:
        text = _read(relative)
        for lineno, line in enumerate(text.splitlines(), 1):
            if "uv run" not in line and "uv sync" not in line:
                continue
            if "uv run --no-sync" in line:
                continue  # observation-only, explicitly allowed
            if "uv run --python" in line:
                continue  # explicit cross-version, not node-22 active
            if NODE27_PY in line or "node-27" in line or "nwm@210.77.77.27" in line or "/home/nwm/" in line:
                continue
            if "ROLLBACK_CHECKOUT" in line:
                continue  # isolated disposable worktree, intentionally synced
            # Prose naming the prohibition is not an executable command.
            if "禁止" in line or "不要" in line or "不是" in line or "不得" in line or "不会" in line:
                continue
            if line.strip().startswith((">", "#", "-", "*")):
                continue
            # Root-instruction "关键命令" summary lines are generic guidance,
            # not node-22 active operations.
            if (
                relative in ("instructions/agents/shared.md", "CLAUDE.md", "AGENTS.md")
                and line.startswith("- 关键命令")
            ):
                continue
            # conftest NHMS_RUN_INTEGRATION opt-in and failed-basin generic
            # validation examples are local guidance, not node-22 active.
            if relative == "tests/conftest.py" and "NHMS_RUN_INTEGRATION" in line:
                continue
            if relative == "docs/runbooks/failed-basin-retry.md" and (
                "fake-slurm" in line or "pytest -q" in line
            ):
                continue
            bad.append(f"{relative}:{lineno}: {line.strip()}")
    assert bad == [], "new bare uv on governed node-22 surfaces:\n" + "\n".join(bad)


# --- 8. explicit non-findings stay classified (not asserted as violations) ---


def test_historical_and_generic_surfaces_not_overreached() -> None:
    # forcing-copyback-backfill is explicitly historical; qhh-continuous has a
    # superseded marker; source-latency/slurm-backlog are generic-host. These
    # are intentionally not converted in this PR, so they may still contain
    # bare uv — the scan above only governs the explicit surfaces.
    historical = _read("docs/runbooks/forcing-copyback-backfill.md")
    assert "historical" in historical or "归档" in historical
