"""Static contract: active node-22 entrypoints preserve the deferred .venv.

#1103 partition: the governed-surface guards (systemd units, the production-ops
runbook tree, failed-basin retry, the ci-test-routing node-27 lane, the QHH
surfaces, the generated instruction roots and the sibling bare-`uv` scan). The
substituted-python classifier and its mutation proofs live in
``tests/test_node22_entrypoint_invariant_python_scan.py``; the shared constants
and ``_read`` live in ``tests/node22_entrypoint_helpers.py``.
"""

from __future__ import annotations

import re
import shlex

from tests.node22_entrypoint_helpers import (
    NODE22_ACTIVE,
    NODE22_VENV_PY,
    NODE27_PY,
    _read,
)
from tests.production_ops_runbook import combined_text as production_ops_text
from tests.production_ops_runbook import surfaces as production_ops_surfaces

# --- 1. systemd units use exact interpreters, no bare uv ExecStart ----------


def test_retention_unit_uses_exact_interpreter_and_absolute_script() -> None:
    unit = _read("infra/systemd/nhms-scheduler-evidence-retention.service")
    execstart = [
        line.strip() for line in unit.splitlines() if line.strip().startswith("ExecStart=")
    ]
    # Exactly one complete ExecStart; extra or partial directives fail.
    expected = (
        f"ExecStart={NODE22_VENV_PY} "
        f"{NODE22_ACTIVE}/scripts/node22_scheduler_evidence_retention.py"
    )
    assert execstart == [expected], (
        f"retention ExecStart must be exactly the exact directive; got: {execstart}"
    )


def test_scheduler_journal_retention_units_pin_the_active_runtime_and_schedule() -> None:
    service = _read("infra/systemd/nhms-scheduler-journal-retention.service")
    timer = _read("infra/systemd/nhms-scheduler-journal-retention.timer")
    fields = (
        "Type=oneshot", f"WorkingDirectory={NODE22_ACTIVE}",
        f"EnvironmentFile={NODE22_ACTIVE}/infra/env/compute.scheduler-dbfree.env",
        f"ExecStart={NODE22_VENV_PY} {NODE22_ACTIVE}/scripts/node22_scheduler_journal_retention.py",
        "TimeoutStartSec=1800", "OnCalendar=*-*-* 04:45:00 UTC",
        "RandomizedDelaySec=15m", "Persistent=true",
        "Unit=nhms-scheduler-journal-retention.service", "WantedBy=default.target",
    )
    joined = f"{service}\n{timer}"
    assert all(field in joined and field not in joined.replace(field, "", 1) for field in fields)
    assert service.count("ExecStart=") == 1 and "uv" not in service.lower()
    assert re.search(r"(?<![./])python3?(?:\s|$)", service) is None
    assert "uv" in service.replace("Type=oneshot", "ExecStart=uv run python").lower()
    assert re.search(r"(?<![./])python3?(?:\s|$)", service.replace(NODE22_VENV_PY, "python3"))


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
    # #1103 moved the commands out of `current-production-ops.md` into the
    # `production-ops/` sub-runbooks; the index page carries none. A reader
    # left on the index alone would find nothing to classify and report
    # `remaining == []` vacuously, so scan every surface and pin that the scan
    # actually reached commands before trusting an empty report.
    commands = [
        (relative, lineno, line)
        for relative in production_ops_surfaces()
        for lineno, line in _command_lines(_read(relative))
    ]
    assert commands, "no `uv run` / `uv sync` command on any production-ops surface"
    # node-27 commands and the disposable rollback-worktree sync stay unchanged.
    remaining = [
        (relative, lineno, line)
        for relative, lineno, line in commands
        if not (
            NODE27_PY in line
            or "nwm@210.77.77.27" in line
            or "node-27" in line
            or "/home/nwm/" in line
            or ("ROLLBACK_CHECKOUT" in line and "uv sync" in line)
        )
    ]
    # Every node-22 active executable command now uses the exact venv.
    assert remaining == [], f"node-22 active bare uv in production-ops: {remaining}"
    text = production_ops_text()
    # node-27 exact-venv commands and the rollback worktree sync stay preserved.
    assert NODE27_PY in text
    assert '(cd "$ROLLBACK_CHECKOUT" && uv sync --all-extras --dev)' in text
    # #1944 §8.11: the scanner above only inspects lines containing `uv run` /
    # `uv sync`, so a node-22 command written with a bare interpreter would pass
    # it by being invisible.  Positive pin, same shape as the failed-basin
    # check: the census command must exist and must carry the exact venv.
    assert f"{NODE22_VENV_PY} -m services.orchestrator.cli census-job-id-scope" in text


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


# --- 4c. node-27 governed-lane status-swallowing classifier ------------------


def _fence_logical(fence_lines: list[str]) -> list[str]:
    """Rebuild backslash continuations in the fence into complete commands."""
    out: list[str] = []
    buf: list[str] = []
    for line in fence_lines:
        buf.append(line.rstrip().rstrip("\\").rstrip())
        if line.rstrip().endswith("\\"):
            continue
        out.append(" ".join(part.strip() for part in buf))
        buf = []
    return out + ([" ".join(part.strip() for part in buf)] if buf else [])


def _fence_tokens(command: str) -> list[str]:
    """shlex(posix=False, punctuation_chars=";&|"): unquoted ; & | operators."""
    lexer = shlex.shlex(command, posix=False, punctuation_chars=";&|")
    lexer.whitespace_split = False
    lexer.commenters = "#"
    lexer.wordchars += "+"  # keep `set +e`/`+o` flags glued like Bash
    return [tok for tok in lexer if tok is not None]


# ``set +euo`` / ``set +o errexit`` disable fail-fast; the lane has no ``||``.
_SET_DISABLE_RE = re.compile(r"\+[euo]+")
_SET_DISABLE_OPTS = frozenset({"errexit", "nounset", "pipefail"})
_SEPARATORS = (";", "&&", "|", "&", ">", "<", ">>", ";;", "||")


def _segments(tokens: list[str]) -> list[list[str]]:
    """Split token runs into command segments at separator operator tokens."""
    segments: list[list[str]] = []
    seg: list[str] = []
    for tok in tokens:
        if tok in _SEPARATORS:
            if seg:
                segments.append(seg)
                seg = []
            continue
        seg.append(tok)
    return segments + ([seg] if seg else [])


def _node27_lane_swallow_violations(fence_lines: list[str]) -> list[tuple[str, str]]:
    """Lane-wide status swallow scan: unquoted ``||`` and ``set`` fail-fast disables."""
    violations: list[tuple[str, str]] = []
    for command in _fence_logical(fence_lines):
        tokens = _fence_tokens(command)
        if not tokens:
            continue
        # The lexer emits a bare ``||`` only for unquoted, unescaped chars.
        if "||" in tokens:
            violations.append((command, "|| swallows failure"))
        for seg in _segments(tokens):
            if len(seg) < 2 or seg[0] != "set":
                continue
            for j, tok in enumerate(seg[1:], 1):
                # `+e`/`+u`/`+euo` disable fail-fast; lone `+o` only names one.
                if _SET_DISABLE_RE.fullmatch(tok) and tok != "+o":
                    violations.append((command, f"fail-fast disabled by set {tok}"))
                elif tok == "+o" and j + 1 < len(seg) and seg[j + 1] in _SET_DISABLE_OPTS:
                    violations.append((command, f"fail-fast disabled by set +o {seg[j + 1]}"))
    return violations


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

    # pytest still pipes into the tee receipt (the `\` joins one logical command).
    commands = _fence_logical(lines)
    pytest_logical = next(cmd for cmd in commands if "uv run --no-sync pytest" in cmd)
    assert "tee artifacts/ci-routing/e2e-grib" in pytest_logical, "pytest must still be piped to tee"

    # The whole lane must stay synchronization-free.
    assert "uv sync" not in "\n".join(lines)

    # Status-swallowing is a lane-wide property: a `|| true` on the guard or
    # pytest/tee, or a fail-fast disable, would let a failed lane exit 0.
    violations = _node27_lane_swallow_violations(lines)
    assert violations == [], f"node-27 governed lane swallows status: {violations}"


def test_ci_routing_lane_rejects_status_swallowing_mutations() -> None:
    """Mutations must be red; authoritative, quoted, and benign controls green."""
    lines = _node27_bash_fence(_read("docs/runbooks/ci-test-routing.md"))
    fence = "\n".join(lines)
    assert _node27_lane_swallow_violations(lines) == [], "authoritative fence must be green"

    guard = 'uv run --no-sync python -c "import sys; assert sys.version_info[:2] == (3, 11), sys.version"'
    tee = "| tee artifacts/ci-routing/e2e-grib-$(date +%F).log"
    set_failfast = "set -euo pipefail"
    # (anchor, append, expected reason substring; the splice text must be
    # flagged). Every ``||`` shape and every fail-fast disable goes red.
    mutations = [
        (tee, "||true", "|| swallows failure"), (tee, "|| true", "|| swallows failure"),
        (tee, "||:", "|| swallows failure"), (guard, "||true", "|| swallows failure"),
        (guard, " || true", "|| swallows failure"), (tee, " || true", "|| swallows failure"),
        (tee, " || echo receipt", "|| swallows failure"), (tee, " || exit 0", "|| swallows failure"),
        (set_failfast, "\nset +e", "fail-fast disabled by set +e"),
        (set_failfast, "\nset +o pipefail", "set +o pipefail"),
        (set_failfast, "\nset +euo pipefail", "set +euo"),
        (set_failfast, "\nset +eu", "set +eu"),
        (set_failfast, " +e", "set +e"), (set_failfast, " +o pipefail", "set +o pipefail"),
        (set_failfast, "\nset -euo pipefail +e", "set +e"),
        (set_failfast, "\nset -euo pipefail +u", "set +u"),
        (set_failfast, "\nset -euo pipefail +o pipefail", "set +o pipefail"),
        (set_failfast, "\nset -euo pipefail +o errexit", "set +o errexit"),
        (set_failfast, "\nset +e\n" + tee + " || true", "|| swallows failure"),
    ]
    for anchor, append, expected_reason in mutations:
        assert anchor in fence, f"anchor not found: {anchor!r}"
        flagged = _node27_lane_swallow_violations(
            fence.replace(anchor, anchor + append, 1).splitlines()
        )
        splice = append.strip().splitlines()[0]
        assert flagged and any(expected_reason in r for _, r in flagged) and any(
            splice in cmd for cmd, _ in flagged
        ), f"{append!r}: expected red ({expected_reason!r}, {splice!r}), got {flagged}"

    # Green controls: quoted/escaped literals are not operators, comments are
    # stripped, lone ``set +o`` disables nothing, ``set -euo pipefail`` is the
    # lane's own fail-fast set.
    for cmd in ("echo 'a||b'", 'echo "a||b"', r"echo a\|\|b", "true # || true",
                "set +o", "set -euo pipefail", "set -eu"):
        assert _node27_lane_swallow_violations([cmd]) == [], f"green control flagged: {cmd}"


def test_glued_operator_tokens_isolated() -> None:
    """``||`` glued to a word is an operator token, never absorbed."""
    for cmd in ("cmd >/tmp/log||true", "cmd >/tmp/log|| true", "cmd >/tmp/log||:",
                'python -c "print(1)"||true'):
        tokens = _fence_tokens(cmd)
        assert "||" in tokens and not any(t != "||" and "||" in t for t in tokens), (
            f"{cmd}: glued || not isolated: {tokens!r}"
        )
    # Separator-glued set disables land in a scanned segment and go red.
    for cmd in ("cmd; set +e", "cmd && set +e", "cmd | set +e", "cmd & set +e",
                "cmd;; set +e", "cmd; set +o pipefail", "cmd; set -euo pipefail +e",
                "cmd && set -euo pipefail +o pipefail"):
        assert _node27_lane_swallow_violations([cmd]), f"{cmd}: separator set disable must be red"


def test_conftest_skip_guidance_points_to_runbook() -> None:
    text = _read("tests/conftest.py")
    # Skip guidance points at the runbook authority only, no bare-command
    # duplicate; the generic NHMS_RUN_INTEGRATION opt-in hint stays.
    assert "uv run pytest -m" not in text
    assert "ci-test-routing.md" in text
    assert "node-27" in text
    assert "node-22" not in text


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
        "infra/systemd/nhms-scheduler-journal-retention.service",
        "infra/systemd/nhms-scheduler-journal-retention.timer",
        "infra/systemd/nhms-slurm-gateway.service",
        # #1103: the index page plus every `production-ops/` sub-runbook, so a
        # bare `uv` added to a sub-runbook still reddens this scan.
        *production_ops_surfaces(),
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
    # Historical/generic surfaces may still contain bare uv; this scan does not.
    historical = _read("docs/runbooks/forcing-copyback-backfill.md")
    assert "historical" in historical or "归档" in historical
