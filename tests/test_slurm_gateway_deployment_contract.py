"""Static producer/consumer contract for the node-22 Slurm gateway rollout.

Covers issue #1684's deployment half (EVID-05 / CONTRACT-01): the checked-in
generic systemd unit carries an ACTIVE placeholder ``EnvironmentFile=`` and no
inline credential value; the node-22 runbook §3.2.2 executable block:

- creates a SEPARATE owner-only backup ROOT directory and pointer FILE (never
  the pointer-as-directory collision) and snapshots the secret + both drop-ins
  BEFORE any overwrite with exact present/absent markers;
- creates the secret at mode 0600 BEFORE any token bytes are written, then
  generates the credential with the exact active node-22 interpreter via
  ``secrets.token_urlsafe`` written DIRECTLY into the 0600 file;
- resets the inherited EnvironmentFile list, explicitly re-adds the live base
  env (compute.host.env for the gateway, compute.scheduler-dbfree.env for the
  scheduler drop-in) BEFORE the same scratch secret path, sets the live
  loopback 8090 URL;
- verifies the EFFECTIVE EnvironmentFiles of BOTH units resolve BOTH their live
  base env AND the shared scratch secret via path-only ``grep -F`` checks
  (paths only, never values);
- uses a generic ``expect_status`` helper to require EXACT 401/401/404 for the
  no-token/wrong-token/reset boundaries (fail-fast ``|| exit 1``) and
  ``token_probe`` to require exactly 422;
- runs an executable read-only ``_default_gateway_probe`` preflight that asserts
  healthy/submit_capable/accounting_available BEFORE the timer starts;
- step 0 fences the scheduler with REVERSIBLE RUNTIME ``ConditionPathExists``
  drop-ins (a plain ``stop`` was undone twice by a concurrent same-user
  session, and ``mask --runtime`` is provably defeated by persistent user unit
  precedence via a live throwaway probe): a 0700 runtime drop-in dir and 0600
  drop-in file per unit, each carrying ``[Unit]`` +
  ``ConditionPathExists=<release sentinel>`` via an UNQUOTED-EOF heredoc (the
  sentinel path is removed first so the condition is false); daemon-reload, stop
  timer, a still-running service pass is NEVER stopped/killed (fail-closed
  natural-finish exit before backup/overwrite), and both units are mechanically
  verified — ``DropInPaths`` includes the runtime drop-in, a probe
  ``start`` is a skipped no-op (stays inactive, ``ConditionResult=no``) —
  before backup;
- success path releases the fence in EXACT order: remove BOTH runtime drop-ins
  (never create the release sentinel as a bypass), daemon-reload, verify
  ``DropInPaths`` no longer lists the fence, then start the timer and confirm
  active — only after the 401/401/404/422 boundaries and the read-only
  preflight pass;
- rollback is SELF-CONTAINED (redefines its own gates), re-asserts the SAME
  condition fence (fail-closed on a live pass), restores the exact prior
  file/mode or removes an absent file via strict ``case`` state matching, and
  NEVER removes the fence, creates the sentinel, daemon-reloads a release, or
  starts the timer (fail-closed manual recovery).

This is a STATIC source oracle over the tracked files: it parses the fenced
bash blocks (not whole-section substring search), distinguishes executable
lines from comments/prose, and additionally pins the EXECUTABLE OPENER lines
(``cat > ... <<'EOF'``, ``token_probe() {``, ``ACTIVE_PY - <<'PY'``, restore
function declaration) so a commented-out heredoc/function opener cannot pass
while its body is treated as executable.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SECRET_PATH = "/scratch/frd_muziyao/nhms-prod/secrets/slurm-gateway.env"
# Live node-22 base EnvironmentFiles (observed on the real user units; the
# drop-ins must reset the inherited list and explicitly re-add these BEFORE the
# shared secret so the live base config (workspace / object-store / partition /
# runtime) survives the drop-in override).
GATEWAY_BASE_ENV = "/scratch/frd_muziyao/NWM/infra/env/compute.host.env"
SCHEDULER_BASE_ENV = "/scratch/frd_muziyao/NWM/infra/env/compute.scheduler-dbfree.env"
LIVE_URL = "http://127.0.0.1:8090"
ACTIVE_PY = "/scratch/frd_muziyao/NWM/.venv/bin/python"
GATEWAY_UNIT = "infra/systemd/nhms-slurm-gateway.service"
SCHEDULER_DROPIN = "10-slurm-gateway-token.conf"
GATEWAY_DROPIN = "10-node22-live.conf"
RUNBOOK = "docs/runbooks/current-production-ops.md"

TIMER_START_LINE = "systemctl --user start nhms-compute-scheduler.timer"

SCHEDULER_TIMER = "nhms-compute-scheduler.timer"
SCHEDULER_SERVICE = "nhms-compute-scheduler.service"
# The PROVEN fence primitive on this topology: reversible RUNTIME
# ConditionPathExists drop-ins. `mask --runtime` is defeated by persistent user
# unit precedence (live throwaway probe: is-enabled stayed `static`,
# LoadState=loaded, `start` succeeded/active) and is FORBIDDEN.
RUNTIME_DIR = '${XDG_RUNTIME_DIR:-/run/user/$(id -u)}'
FENCE_RELEASE = '"$RUNTIME_DIR/nhms-issue-1684-scheduler-release"'
FENCE_NAME = "90-nhms-issue-1684-maintenance-fence.conf"
TIMER_DROPIN = f"$RUNTIME_DIR/systemd/user/{SCHEDULER_TIMER}.d/{FENCE_NAME}"
SERVICE_DROPIN = f"$RUNTIME_DIR/systemd/user/{SCHEDULER_SERVICE}.d/{FENCE_NAME}"
# The exact Adjacent body the drop-in file must carry: variable form
# (the heredoc is UNQUOTED EOF so `$FENCE_RELEASE` expands at install time to
# the absolute sentinel path under the runtime dir). The oracle pins the
# variable-form line AND the unquoted-EOF opener; an expanded literal alone
# would be a different (also acceptable) shape, but the runbook chooses the
# variable form so the sentinel path is derived from one definition.
FENCE_CONDITION_LINE = "ConditionPathExists=$FENCE_RELEASE"
# Executable lines that MUST be absent from any rollout/rollback fence block.
FORBIDDEN_EXEC = (
    f"systemctl --user mask --runtime --now {SCHEDULER_TIMER}",
    f"systemctl --user mask --runtime --now {SCHEDULER_SERVICE}",
    f"systemctl --user mask --runtime {SCHEDULER_SERVICE}",
    f"systemctl --user mask --runtime {SCHEDULER_TIMER}",
    f"systemctl --user unmask --runtime {SCHEDULER_SERVICE} {SCHEDULER_TIMER}",
    f"systemctl --user unmask --runtime {SCHEDULER_TIMER} {SCHEDULER_SERVICE}",
    "systemctl --user unmask --runtime",
    "systemctl --user mask --runtime",
)
# The drop-in installed to gate a given unit, with the exact opener line.
TIMER_DROPIN_OPENER = (
    f'cat > "$RUNTIME_DIR/systemd/user/{SCHEDULER_TIMER}.d/$FENCE_NAME" <<EOF'
)
SERVICE_DROPIN_OPENER = (
    f'cat > "$RUNTIME_DIR/systemd/user/{SCHEDULER_SERVICE}.d/$FENCE_NAME" <<EOF'
)


def _read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def _fenced_bash_blocks(markdown: str) -> list[str]:
    """Return the contents of every triple-backtick bash-fenced block.

    Fences are triple-backtick ``bash`` ... triple-backtick (allowing a
    trailing language tag such as ``bash``). Non-bash fenced blocks are
    ignored (e.g. ``text`` blocks).
    """
    blocks: list[str] = []
    lines = markdown.splitlines()
    in_block = False
    is_bash = False
    current: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not in_block and stripped.startswith("```"):
            language = stripped[3:].strip().lower()
            in_block = True
            is_bash = language == "bash"
            current = []
            continue
        if in_block and stripped.startswith("```"):
            if is_bash:
                blocks.append("\n".join(current))
            in_block = False
            is_bash = False
            current = []
            continue
        if in_block:
            current.append(line)
    return blocks


def _executable_lines(block: str) -> list[str]:
    """Non-comment, non-blank lines of a fenced bash block."""
    out: list[str] = []
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append(stripped)
    return out


def _section_after_heading(markdown: str, heading: str) -> str:
    """Text from ``heading`` to the next ``###``-level heading (or end)."""
    start = markdown.index(heading)
    for line_no, line in enumerate(markdown.splitlines(), 1):
        if line_no <= markdown[:start].count("\n") + 1:
            continue
        if line.startswith("### "):
            return markdown[start:markdown.index(line, start)]
    return markdown[start:]


def _runbook() -> str:
    return _read(RUNBOOK)


def _rollout_section() -> str:
    """The §3.2.2 heading text up to the next `###` heading."""
    return _section_after_heading(_runbook(), "#### 3.2.2")


def _rollout_bash_blocks() -> list[str]:
    """All bash-fenced blocks within §3.2.2 (rollout + rollback)."""
    return _fenced_bash_blocks(_rollout_section())


def _rollout_only_blocks() -> list[str]:
    """The rollout bash blocks only (everything before the Rollback heading).

    Rollout-boundary pins must not be satisfied by the rollback block's
    re-declared gates, or a mutant that removes a rollout gate would stay green
    (the rollback copy satisfies the substring/index pin).
    """
    section = _rollout_section()
    if "Rollback" in section:
        section = section[: section.index("Rollback")]
    return _fenced_bash_blocks(section)


def _rollback_blocks() -> list[str]:
    """The bash-fenced blocks under the 'Rollback' subsection of §3.2.2."""
    section = _section_after_heading(_rollout_section(), "Rollback")
    return _fenced_bash_blocks(section)


def _join_executable(blocks: list[str]) -> str:
    """All executable lines of the rollout bash blocks, in order."""
    return "\n".join(line for block in blocks for line in _executable_lines(block))


# ---------------------------------------------------------------------------
# 1. generic systemd unit: active placeholder EnvironmentFile, no secrets
# ---------------------------------------------------------------------------


def test_generic_unit_has_active_placeholder_environment_file() -> None:
    unit = _read(GATEWAY_UNIT)
    assert "EnvironmentFile=/opt/SHUD-NWM/infra/env/slurm-gateway.secret" in unit
    no_comment = "\n".join(line for line in unit.splitlines() if not line.lstrip().startswith("#"))
    assert "EnvironmentFile=/opt/SHUD-NWM/infra/env/slurm-gateway.secret" in no_comment


def test_generic_unit_has_no_inline_credential_value() -> None:
    unit = _read(GATEWAY_UNIT)
    for line_no, line in enumerate(unit.splitlines(), 1):
        stripped = line.strip()
        if "SLURM_GATEWAY_SERVICE_TOKEN=" in stripped:
            if stripped.startswith("#") and "SLURM_GATEWAY_SERVICE_TOKEN=<credential>" in stripped:
                continue  # documented placeholder, not a value
            raise AssertionError(f"{GATEWAY_UNIT}:{line_no}: credential assignment present: {stripped}")
        if "SLURM_GATEWAY_SERVICE_TOKEN" in stripped and not stripped.startswith("#"):
            raise AssertionError(f"{GATEWAY_UNIT}:{line_no}: token name outside comment: {stripped}")


# ---------------------------------------------------------------------------
# 2. tracked env examples: placeholder only
# ---------------------------------------------------------------------------


def test_env_examples_contain_no_credential_value() -> None:
    for relative in (
        "infra/env/compute.example",
        "infra/env/compute.scheduler-dbfree.env.example",
    ):
        text = _read(relative)
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "SLURM_GATEWAY_SERVICE_TOKEN" in stripped:
                raise AssertionError(f"{relative}: credential assignment outside comments: {stripped}")


# ---------------------------------------------------------------------------
# 3. rollout: backup/pointer isolation and strict ordering
# ---------------------------------------------------------------------------


def test_rollout_backup_uses_separate_root_and_pointer_files_atomic_0600() -> None:
    """Defect A: pointer must be a FILE, never the backup directory itself.

    The backup root is installed as an owner-only 0700 directory; each snapshot
    is a collision-free mktemp dir; the pointer is written atomically via a
    ``.tmp`` + ``mv`` and chmod 0600 (never ``mkdir -p "$POINTER"`` then
    ``printf > "$POINTER"``, which collides with the directory).
    """
    ordered = _join_executable(_rollout_bash_blocks())
    assert 'BACKUP_ROOT="$HOME/.config/systemd/user/gateway-rollout-backups"' in ordered
    assert 'BACKUP_POINTER="$HOME/.config/systemd/user/gateway-rollout-backup"' in ordered
    # The pointer variable must be assigned a bare FILE path, and the root a
    # directory with install -d -m 0700 (owner-only).
    assert "install -d -m 0700 \"$BACKUP_ROOT\"" in ordered
    assert "mktemp -d" in ordered, "snapshot must be a collision-free mktemp dir"
    # Atomic pointer write: tmp file, then mv (never `> $BACKUP_POINTER` while
    # it names the directory; never chmod on the directory path).
    assert 'install -m 0600 /dev/null "$BACKUP_POINTER.tmp"' in ordered
    assert 'printf \'%s\\n\' "$BACKUP_DIR" > "$BACKUP_POINTER.tmp"' in ordered
    assert 'mv "$BACKUP_POINTER.tmp" "$BACKUP_POINTER"' in ordered
    assert 'chmod 0600 "$BACKUP_POINTER"' in ordered
    # The old broken pattern must NOT be present (pointer-as-directory).
    assert 'mkdir -p -m 0700 "$BACKUP_DIR"' not in ordered
    assert 'BACKUP_POINTER="$HOME/.config/systemd/user/gateway-rollout-backup"' in ordered


def test_rollout_has_backup_before_gateway_environment_reset_and_secret() -> None:
    """Backup precedes every overwrite: root/pointer/state, then writes."""
    ordered = _join_executable(_rollout_bash_blocks())
    backup_marker = "BACKUP_ROOT="
    secret_marker = "secrets.token_urlsafe"
    gateway_reset = (
        "EnvironmentFile=\n"
        f"EnvironmentFile={GATEWAY_BASE_ENV}\n"
        f"EnvironmentFile={SECRET_PATH}"
    )
    assert ordered.index(backup_marker) < ordered.index(secret_marker), (
        "backup must precede secret generation"
    )
    assert ordered.index(".state") < ordered.index(secret_marker), (
        "per-path absence/presence snapshot must precede secret generation"
    )
    assert ordered.index(backup_marker) < ordered.index(gateway_reset), (
        "backup must precede the gateway EnvironmentFile reset"
    )


def test_rollout_creates_secret_0600_before_token_bytes_then_exact_python() -> None:
    """Defect B: 0600 is established BEFORE any token bytes are written.

    ``install -m 0600 /dev/null "$SECRET_PATH"`` precedes the exact-interpreter
    generation; the ``>`` redirect then truncates the already-0600 file without
    exposing the token at a default-umask mode. No literal placeholder, no
    append.
    """
    ordered = _join_executable(_rollout_bash_blocks())
    install_line = f'install -m 0600 /dev/null {SECRET_PATH}'
    token_line_idx = ordered.index("secrets.token_urlsafe")
    assert install_line in ordered, "secret must be created at mode 0600 first"
    assert ordered.index(install_line) < token_line_idx, (
        "0600 install must precede any token bytes"
    )
    assert 'chmod 0600 /scratch/frd_muziyao/nhms-prod/secrets/slurm-gateway.env' in ordered
    assert f"{ACTIVE_PY} -c" in ordered, "credential must be generated by the exact active interpreter"
    assert "import secrets" in ordered
    assert f"{ACTIVE_PY} -c 'import secrets; print" in ordered
    assert "SLURM_GATEWAY_SERVICE_TOKEN=<generated-scheduler-credential>" not in ordered, (
        "literal placeholder assignment must not be an executable line"
    )
    assert f"> {SECRET_PATH}" in ordered, "credential must be written via overwrite"
    assert f">> {SECRET_PATH}" not in ordered, "credential file must be overwritten, not appended"


GATEWAY_DROPIN_OPENER = 'cat > "$GATEWAY_DROPIN_DIR/10-node22-live.conf" <<\'EOF\''
SCHEDULER_DROPIN_OPENER = (
    'cat > "$HOME/.config/systemd/user/'
    'nhms-compute-scheduler.service.d/10-slurm-gateway-token.conf" <<\'EOF\''
)


def _heredoc_block(opener: str) -> str:
    """Fenced bash block whose executable opener lines contain ``opener``."""
    for block in _rollout_bash_blocks():
        if opener in _executable_lines(block):
            return block
    raise AssertionError(f"executable heredoc opener not found: {opener}")


def test_rollout_gateway_dropin_resets_environment_file_and_sets_8090() -> None:
    ordered = _join_executable(_rollout_bash_blocks())
    assert (
        f"EnvironmentFile=\n"
        f"EnvironmentFile={GATEWAY_BASE_ENV}\n"
        f"EnvironmentFile={SECRET_PATH}"
    ) in ordered
    assert f"Environment=SLURM_GATEWAY_URL={LIVE_URL}" in ordered
    assert "EnvironmentFile=/opt/SHUD-NWM/infra/env/slurm-gateway.secret" not in ordered


def test_dropins_explicitly_readd_live_base_env_before_shared_secret() -> None:
    """Both executable drop-in heredocs must reset the inherited EnvironmentFile
    list and re-add the live base env BEFORE the shared secret.

    The live gateway base unit loads compute.host.env and the scheduler base
    unit loads compute.scheduler-dbfree.env. An empty ``EnvironmentFile=``
    clears everything inherited, so a drop-in that only appends the secret
    silently DROPS the real base config (workspace / object-store / partition /
    runtime). The exact adjacent sequence — reset, base env, secret — must be
    present inside each heredoc, and the base env must be effective-listed
    afterwards (see the effective-verification test below).
    """
    gateway_heredoc = _executable_lines(_heredoc_block(GATEWAY_DROPIN_OPENER))
    scheduler_heredoc = _executable_lines(_heredoc_block(SCHEDULER_DROPIN_OPENER))

    gateway_sequence = (
        "EnvironmentFile=\n"
        f"EnvironmentFile={GATEWAY_BASE_ENV}\n"
        f"EnvironmentFile={SECRET_PATH}"
    )
    scheduler_sequence = (
        "EnvironmentFile=\n"
        f"EnvironmentFile={SCHEDULER_BASE_ENV}\n"
        f"EnvironmentFile={SECRET_PATH}"
    )

    # Exact adjacent sequence inside each executable heredoc: reset opener,
    # base env, then shared secret. A commented opener cannot satisfy this
    # because _executable_lines drops comment lines and the pin is on the
    # sequence (a `# EnvironmentFile=` line would break adjacency).
    gateway_body = "\n".join(gateway_heredoc)
    scheduler_body = "\n".join(scheduler_heredoc)
    assert gateway_sequence in gateway_body, (
        f"gateway drop-in must reset EnvironmentFile list, re-add {GATEWAY_BASE_ENV}, "
        f"then {SECRET_PATH} (adjacent, executable)"
    )
    assert f"EnvironmentFile=\nEnvironmentFile={SECRET_PATH}" not in gateway_body, (
        "gateway drop-in must re-add the live base env before the secret, not jump straight to it"
    )
    assert scheduler_sequence in scheduler_body, (
        f"scheduler drop-in must reset EnvironmentFile list, re-add {SCHEDULER_BASE_ENV}, "
        f"then {SECRET_PATH} (adjacent, executable)"
    )
    assert f"EnvironmentFile=\nEnvironmentFile={SECRET_PATH}" not in scheduler_body, (
        "scheduler drop-in must re-add the live base env before the secret"
    )
    # No generic template path survives as an executable EnvironmentFile line
    # inside either heredoc.
    assert "EnvironmentFile=/opt/SHUD-NWM/infra/env/slurm-gateway.secret" not in (
        gateway_body + "\n" + scheduler_body
    )


def test_rollout_effective_environmentfiles_checks_both_paths_per_unit() -> None:
    """Effective EnvironmentFiles verification must assert BOTH resolved paths
    for EACH unit (live base env + shared secret), path-only, no values.

    ``systemctl show -p EnvironmentFiles`` prints file PATHS only; the checks
    use ``grep -F`` on the path and must not print/inspect the token value.
    """
    ordered = _join_executable(_rollout_bash_blocks())
    for unit in ("nhms-slurm-gateway.service", "nhms-compute-scheduler.service"):
        base_env = GATEWAY_BASE_ENV if unit.startswith("nhms-slurm-gateway") else SCHEDULER_BASE_ENV
        base_check = (
            f'systemctl --user show {unit} -p EnvironmentFiles | grep -F \'{base_env}\''
        )
        secret_check = (
            f'systemctl --user show {unit} -p EnvironmentFiles | grep -F \'{SECRET_PATH}\''
        )
        assert base_check in ordered, f"effective check missing base env path for {unit}: {base_check}"
        assert secret_check in ordered, f"effective check missing secret path for {unit}: {secret_check}"
    assert "grep -F" in ordered
    # Path-only checks: the effective-listing lines must NEVER open/print the
    # secret file's contents; the fixed secret PATH may appear only as a grep
    # pattern (the token value is sourced exclusively inside token_probe).
    for opener in ("cat ", "cat ${", "sed -n", "awk "):
        if opener in ordered:
            for line in ordered.splitlines():
                if line.startswith(opener) and SECRET_PATH in line:
                    raise AssertionError(f"effective check must not read secret contents: {line}")


# ---------------------------------------------------------------------------
# 4. rollout: auth boundary gates (expect_status / token_probe / preflight)
# ---------------------------------------------------------------------------


def test_rollout_expect_status_requires_exact_401_401_404_fail_fast() -> None:
    """Defect C: no-token/wrong-token/reset boundaries are FAIL-FAST gates.

    Each boundary must be a gated call requiring the EXACT status
    (401/401/404) with ``|| exit 1``, ordered before the timer start. Scoped to
    the rollout-only blocks so the rollback block's re-declared gates cannot
    satisfy the pin.
    """
    ordered = _join_executable(_rollout_only_blocks())
    assert "expect_status() {" in ordered
    assert 'status="$(curl' in ordered
    assert '"$expected"' in ordered
    assert "echo \"auth boundary: $label expected HTTP" in ordered
    assert "return 1" in ordered
    # Exact boundary gates, each ending in a fail-fast `|| exit 1`. The gate
    # call may span a line-continuation (`\`); the gate's LAST logical line
    # (the first line not ending in `\`) must itself end in `|| exit 1`. A
    # later unrelated `|| exit 1` (e.g. the token_probe gate) must not satisfy
    # the pin.
    exact_gates = (
        'expect_status 401 "no token" -X POST http://127.0.0.1:8090/api/v1/slurm/jobs',
        'expect_status 401 "wrong token" -X POST http://127.0.0.1:8090/api/v1/slurm/jobs',
        'expect_status 404 "disabled reset" -X POST http://127.0.0.1:8090/api/v1/slurm/internal/reset',
    )
    for gate in exact_gates:
        gate_index = ordered.index(gate)
        tail_lines = ordered[gate_index:].splitlines()
        # Walk the continuation tail: keep consuming lines that end in `\`,
        # then the FIRST non-continuation line is the gate's final line.
        final_idx = 0
        while final_idx < len(tail_lines) and tail_lines[final_idx].rstrip().endswith("\\"):
            final_idx += 1
        assert tail_lines[final_idx].rstrip().endswith("|| exit 1"), (
            f"boundary gate must fail fast on its own final line: {gate}"
        )
    # The gates precede the timer RESUME (the last `start`; the step-0 fence
    # probe `start` is an earlier, skipped no-op and must not satisfy this).
    timer = ordered.rindex(TIMER_START_LINE)
    assert ordered.index(exact_gates[0]) < timer
    assert ordered.index(exact_gates[2]) < timer


def test_rollout_token_probe_asserts_exact_422_and_fails_fast() -> None:
    ordered = _join_executable(_rollout_only_blocks())
    assert "token_probe() {" in ordered
    assert "token_probe || exit 1" in ordered
    assert '"422"' in ordered
    assert "echo \"token probe: expected 422" in ordered


def test_rollout_executable_preflight_bool_assertions_precede_timer_start() -> None:
    ordered = _join_executable(_rollout_only_blocks())
    assert ACTIVE_PY in ordered
    assert "_default_gateway_probe" in ordered
    assert "bool(result.get(\"healthy\"))" in ordered or 'result.get("healthy")' in ordered
    assert "submit_capable" in ordered
    assert "accounting_available" in ordered
    timer = ordered.rindex(TIMER_START_LINE)
    assert ordered.index("_default_gateway_probe") < timer
    assert ordered.index("token_probe || exit 1") < timer
    assert "uv run" not in ordered


# ---------------------------------------------------------------------------
# 4b. rollout/rollback: reversible runtime ConditionPathExists fence (#1684)
# ---------------------------------------------------------------------------


# Exact executable opener lines: unquoted-EOF heredocs so $FENCE_RELEASE
# expands to the absolute sentinel path inside each installed drop-in.
DROPIN_OPENERS = (
    TIMER_DROPIN_OPENER,
    SERVICE_DROPIN_OPENER,
)
LIVE_PASS_FAILCLOSED = (
    "if systemctl --user is-active --quiet nhms-compute-scheduler.service; then"
)
SKIP_VERIFY = (
    'is-active nhms-compute-scheduler.timer)" = inactive',
    'is-active nhms-compute-scheduler.service)" = inactive',
)
CONDITION_RESULT_PROBE = (
    f'test "$(systemctl --user show {SCHEDULER_TIMER} -p ConditionResult --value)" = no',
    f'test "$(systemctl --user show {SCHEDULER_SERVICE} -p ConditionResult --value)" = no',
)
DROPIN_PATHS_PROBE = (
    f'systemctl --user show {SCHEDULER_TIMER} -p DropInPaths | grep -F '
    f'"$RUNTIME_DIR/systemd/user/{SCHEDULER_TIMER}.d/$FENCE_NAME"',
    f'systemctl --user show {SCHEDULER_SERVICE} -p DropInPaths | grep -F '
    f'"$RUNTIME_DIR/systemd/user/{SCHEDULER_SERVICE}.d/$FENCE_NAME"',
)
RELEASE_MISSING = 'rm -f "$FENCE_RELEASE"'
# Fail-fast shell semantics must be the FIRST executable line of both fenced
# blocks (before RUNTIME_DIR or any state change): errexit + pipefail make
# every load-bearing check (grep/test/systemctl/Python preflight) abort the
# block instead of silently continuing to fence release or later restore gates.
SHELL_HARDENING_LINE = "set -euo pipefail"


def _fenced_block_exec(blocks: list[str]) -> str:
    return "\n".join(line for block in blocks for line in _executable_lines(block))


def _fence_heredoc_block(opener: str, blocks: list[str]) -> str:
    """Fenced bash block whose executable opener lines contain ``opener``."""
    for block in blocks:
        if opener in _executable_lines(block):
            return block
    raise AssertionError(f"executable fence heredoc opener not found: {opener}")


def _assert_no_forbidden_exec(ordered: str, where: str) -> None:
    for needle in FORBIDDEN_EXEC:
        assert needle not in ordered, f"{where} must not use forbidden mask/unmask: {needle}"


def _assert_no_service_stop_kill(ordered: str, where: str) -> None:
    for line in ordered.splitlines():
        if SCHEDULER_SERVICE in line and "stop " in line and "mask" not in line:
            raise AssertionError(f"{where} must not stop the scheduler service: {line}")
        if "kill " in line and SCHEDULER_SERVICE in line:
            raise AssertionError(f"{where} must not kill the scheduler service: {line}")


def test_rollout_installs_runtime_condition_fence_with_expanded_absolute_path() -> None:
    """Defect (probe): `mask --runtime` is defeated by persistent user units.

    A live throwaway probe proved `mask --runtime` on a persistent user unit
    leaves ``is-enabled=static`` / ``LoadState=loaded`` and ``start`` succeeds;
    the PROVEN primitive is a runtime ``ConditionPathExists`` drop-in. Step 0
    must define RUNTIME_DIR/FENCE_RELEASE/FENCE_NAME, remove the release
    sentinel (condition false => fence on), install owner-0700 runtime drop-in
    dirs and owner-0600 drop-in files for BOTH units, and the file content must
    contain the ABSOLUTE expanded ``ConditionPathExists=<sentinel>`` path
    (unquoted EOF, no literal ``$FENCE_RELEASE`` inside the installed file).
    """
    ordered = _join_executable(_rollout_only_blocks())
    assert f'RUNTIME_DIR={RUNTIME_DIR}' in ordered, "RUNTIME_DIR must be defined"
    assert FENCE_RELEASE in ordered, "FENCE_RELEASE sentinel must be defined"
    assert f"FENCE_NAME={FENCE_NAME}" in ordered, "FENCE_NAME must be defined"
    assert RELEASE_MISSING in ordered, "release sentinel must be removed (condition false)"
    assert "install -d -m 0700" in ordered
    assert "install -m 0600 /dev/null" in ordered
    for opener in DROPIN_OPENERS:
        assert opener in ordered, f"drop-in opener missing/commented: {opener}"
        body_blocks = _executable_lines(_fence_heredoc_block(opener, _rollout_only_blocks()))
        body = "\n".join(body_blocks)
        assert "[Unit]" in body, f"drop-in must carry [Unit]: {opener}"
        # The drop-in must carry the exact `[Unit]` + ConditionPathExists
        # lines, with the variable form (the UNQUOTED-EOF heredoc expands
        # `$FENCE_RELEASE` at install time to the absolute sentinel path under
        # the runtime dir; a quoted <<'EOF' would leave the literal unexpanded
        # and the condition would test the wrong path).
        assert FENCE_CONDITION_LINE in body, (
            f"drop-in body must contain ConditionPathExists=$FENCE_RELEASE: {opener}"
        )
        assert "<<" in opener and not opener.split("<<", 1)[1].startswith("'"), (
            f"fence heredoc must use UNQUOTED EOF so the sentinel expands: {opener}"
        )
    # No mask/unmask executable anywhere in the rollout fence.
    _assert_no_forbidden_exec(ordered, "rollout")
    _assert_no_service_stop_kill(ordered, "rollout")


def test_rollout_loads_probes_and_verifies_fence_before_backup() -> None:
    """Fence must be LOADED and mechanically verified BEFORE backup/overwrite.

    daemon-reload after installing drop-ins; stop the timer; fail closed on a
    live service pass (no stop/kill); verify BOTH units' DropInPaths list their
    runtime drop-in, then probe ``systemctl --user start`` on EACH unit and
    require it stays inactive (skipped start) with ``ConditionResult=no`` —
    this safe skipped start proves the fence. All checks precede
    backup/overwrite; no sleep loops.
    """
    ordered = _join_executable(_rollout_only_blocks())
    assert "systemctl --user daemon-reload" in ordered
    assert f"systemctl --user stop {SCHEDULER_TIMER}" in ordered
    assert LIVE_PASS_FAILCLOSED in ordered
    assert "let it finish naturally" in ordered
    assert "re-run this rollout step once nhms-compute-scheduler.service is inactive" in ordered
    assert "exit 1" in ordered
    for probe in DROPIN_PATHS_PROBE:
        assert probe in ordered, f"DropInPaths probe missing: {probe}"
    for probe in CONDITION_RESULT_PROBE:
        assert probe in ordered, f"ConditionResult probe missing: {probe}"
    for skip in SKIP_VERIFY:
        assert skip in ordered, f"skipped-start must stay inactive: {skip}"
    backup_idx = ordered.index("BACKUP_ROOT=")
    for needle in (
        "systemctl --user show nhms-compute-scheduler.timer -p DropInPaths",
        "systemctl --user show nhms-compute-scheduler.service -p DropInPaths",
        "systemctl --user start nhms-compute-scheduler.timer",
        "systemctl --user start nhms-compute-scheduler.service",
    ):
        assert ordered.index(needle) < backup_idx, (
            "fence load/probe/verify must precede backup/overwrite"
        )
    assert "sleep" not in ordered, "no sleep loops in the fence"
    # The probe start on the SERVICE must never be a real run: service stays
    # inactive (condition skipped); no `--quiet` swallowing, no `|| true`.
    _assert_no_service_stop_kill(ordered, "rollout")


def test_fenced_blocks_begin_with_set_euo_pipefail() -> None:
    """Both fenced blocks fail fast from the very first executable line.

    ``set -euo pipefail`` must be line 0 of BOTH the rollout and rollback fenced
    bash blocks, BEFORE RUNTIME_DIR or any state change: without errexit/pipefail
    a failed ``grep``/``test``/``systemctl``/Python preflight can continue to
    fence release or later restore gates. A commented or missing line cannot
    satisfy the pin (comments are dropped by ``_executable_lines``).
    """
    rollout = _join_executable(_rollout_only_blocks())
    rollback = _join_executable(_rollback_blocks())
    assert rollout.splitlines()[0] == SHELL_HARDENING_LINE, (
        "rollout first executable line must be `set -euo pipefail`"
    )
    assert rollback.splitlines()[0] == SHELL_HARDENING_LINE, (
        "rollback first executable line must be `set -euo pipefail`"
    )
    assert rollout.splitlines()[1] == f'RUNTIME_DIR={RUNTIME_DIR}', (
        "rollout must define RUNTIME_DIR as the first state change after errexit"
    )
    # No block may rely on a later `set -e`; the hardening line is unique per block.
    assert rollout.count(SHELL_HARDENING_LINE) == 1
    assert rollback.count(SHELL_HARDENING_LINE) == 1


def test_rollout_success_removes_dropins_after_gates_before_timer_start() -> None:
    """Success path: remove BOTH fence drop-ins ONLY after gates, BEFORE start.

    The release is the REMOVAL of the runtime condition drop-ins (plus empty
    dirs if possible) + daemon-reload; NEVER create the release sentinel as a
    bypass (existing sentinel => ConditionPathExists true => silent pass, gates
    skipped). Removal/verify must come AFTER the 401/401/404/422 boundaries and
    the read-only preflight and BEFORE ``start``; the timer must end active.
    """
    lines = _join_executable(_rollout_only_blocks()).splitlines()
    rm_timer = 'rm -f "$RUNTIME_DIR/systemd/user/nhms-compute-scheduler.timer.d/$FENCE_NAME"'
    rm_service = 'rm -f "$RUNTIME_DIR/systemd/user/nhms-compute-scheduler.service.d/$FENCE_NAME"'
    rm_timer_idx = lines.index(rm_timer)
    rm_service_idx = lines.index(rm_service)
    assert rm_timer_idx == rm_service_idx - 1, "both fence drop-ins must be removed together"
    # The release zone is everything from the first fence removal onward; the
    # step-0 probe `start` lines sit BEFORE it, so order pins against this zone
    # cannot be satisfied by the fence-verification probes.
    zone = lines[rm_timer_idx:]
    release_reload = zone.index("systemctl --user daemon-reload")
    timer_verify = f'systemctl --user show {SCHEDULER_TIMER} -p DropInPaths | grep -Fv "$FENCE_NAME"'
    service_verify = f'systemctl --user show {SCHEDULER_SERVICE} -p DropInPaths | grep -Fv "$FENCE_NAME"'
    timer_resume = zone.index(TIMER_START_LINE)
    assert release_reload > 2, (
        "daemon-reload must follow both drop-in removals and rmdir cleanup"
    )
    assert timer_verify in zone and service_verify in zone
    assert zone.index(timer_verify) < timer_resume, "fence-gone verify must precede timer start"
    assert zone.index(service_verify) < timer_resume
    assert zone.index("systemctl --user is-active nhms-compute-scheduler.timer") > timer_resume, (
        "resume must be confirmed active after start"
    )
    # The release must NEVER be created as a shortcut in the success path.
    assert 'touch "$FENCE_RELEASE"' not in lines
    # Release AFTER the auth boundaries and the read-only preflight (which sit
    # before the release zone).
    prefix = "\n".join(lines[:rm_timer_idx])
    assert "token_probe || exit 1" in prefix
    assert "_default_gateway_probe" in prefix
    assert 'expect_status 401 "no token"' in prefix


def test_rollback_reasserts_condition_fence_and_never_removes_or_starts() -> None:
    """Rollback re-asserts the SAME runtime condition fence; NEVER releases.

    First executable command line sets RUNTIME_DIR (re-establishing the fence
    exactly like rollout: same dirs/heredocs/0600, release sentinel removed,
    daemon-reload, timer stopped, fail-closed on a live service pass, DropInPaths
    + skipped-start + ConditionResult=no verification). It NEVER removes a fence
    file, NEVER creates the sentinel, NEVER daemon-reloads a release, and NEVER
    starts the timer. Manual recovery (run full gates, remove BOTH drop-ins,
    daemon-reload, verify absent, start timer) is comment/prose only.
    """
    ordered = _join_executable(_rollback_blocks())
    assert ordered.splitlines()[0] == SHELL_HARDENING_LINE, (
        "rollback's first executable line must be `set -euo pipefail` (fail-fast "
        "before any state change), then re-fence via RUNTIME_DIR"
    )
    assert ordered.splitlines()[1] == f'RUNTIME_DIR={RUNTIME_DIR}', (
        "rollback must re-fence via RUNTIME_DIR as the first state change"
    )
    assert FENCE_RELEASE in ordered
    assert f"FENCE_NAME={FENCE_NAME}" in ordered
    assert RELEASE_MISSING in ordered
    for opener in DROPIN_OPENERS:
        assert opener in ordered, f"rollback drop-in opener missing/commented: {opener}"
        body_blocks = _executable_lines(_fence_heredoc_block(opener, _rollback_blocks()))
        body = "\n".join(body_blocks)
        assert "[Unit]" in body
        assert FENCE_CONDITION_LINE in body, (
            f"rollback drop-in body must contain ConditionPathExists=$FENCE_RELEASE: {opener}"
        )
        assert "<<" in opener and not opener.split("<<", 1)[1].startswith("'"), (
            f"rollback fence heredoc must use UNQUOTED EOF: {opener}"
        )
    assert "systemctl --user daemon-reload" in ordered
    assert f"systemctl --user stop {SCHEDULER_TIMER}" in ordered
    assert LIVE_PASS_FAILCLOSED in ordered
    assert "let it finish naturally" in ordered
    assert "exit 1" in ordered
    for probe in DROPIN_PATHS_PROBE:
        assert probe in ordered, f"rollback DropInPaths probe missing: {probe}"
    for probe in CONDITION_RESULT_PROBE:
        assert probe in ordered, f"rollback ConditionResult probe missing: {probe}"
    for skip in SKIP_VERIFY:
        assert skip in ordered, f"rollback skipped-start must stay inactive: {skip}"
    # Rollback NEVER releases the fence: no removal of fence files, no sentinel
    # creation, no release daemon-reload, no timer start. (The re-fencing
    # daemon-reload is present; only a SECOND daemon-reload after removal would
    # be a release marker — assert at most one daemon-reload.)
    assert "rm -f \"$RUNTIME_DIR" not in ordered, (
        "rollback must not remove any runtime fence drop-in"
    )
    assert "rmdir" not in ordered, "rollback must not remove fence dirs"
    assert 'touch "$FENCE_RELEASE"' not in ordered, (
        "rollback must not create the release sentinel as a bypass"
    )
    # The re-fencing daemon-reload must come BEFORE the restore section; any
    # daemon-reload AFTER the fence removal would be a release marker. Since
    # rollback never removes the fence (asserted above), every daemon-reload is
    # either the fence load or the restore's reload — no release reload allowed
    # after the last fence file manipulation.
    last_fence_write = ordered.rindex("cat > \"$RUNTIME_DIR")
    first_daemon_reload = ordered.index("systemctl --user daemon-reload")
    assert first_daemon_reload < ordered.index("BACKUP_POINTER="), (
        "fence load daemon-reload must precede restore"
    )
    assert last_fence_write < ordered.index("BACKUP_POINTER="), (
        "fence re-install must precede restore"
    )
    # The only `start` lines are the skipped-start PROBES (each immediately
    # followed by an `is-active = inactive` verify, asserted in the pre-existing
    # never-auto-start test); no real resume start exists here.
    _assert_no_forbidden_exec(ordered, "rollback")
    _assert_no_service_stop_kill(ordered, "rollback")
    # Manual recovery instruction exists in the runbook (comment/prose), with
    # the exact order: run gates -> remove both drop-ins -> daemon-reload ->
    # verify absent -> start timer.
    runbook = _runbook()
    assert "run the FULL authenticated gates" in runbook
    assert "$RUNTIME_DIR/systemd/user/nhms-compute-scheduler.timer.d/$FENCE_NAME" in runbook
    assert "systemctl --user daemon-reload" in runbook
    assert "verify DropInPaths no longer list the fence" in runbook
    assert TIMER_START_LINE in runbook  # manual recovery mentions start


# ---------------------------------------------------------------------------
# 5. executable OPENER lines (defect E): heredoc/function openers must be live
# ---------------------------------------------------------------------------


def test_rollout_executable_openers_are_live_not_commented() -> None:
    """The opener lines themselves must be executable.

    The body-only parser would treat heredoc bodies as executable even when the
    opening ``cat > ... <<'EOF'`` / ``python - <<'PY'`` / function declaration
    is commented out; pin the exact opener lines.
    """
    blocks = _rollout_bash_blocks()
    scheduler_dropin_path = (
        f"$HOME/.config/systemd/user/nhms-compute-scheduler.service.d/{SCHEDULER_DROPIN}"
    )
    gateway_dropin_path = f"$GATEWAY_DROPIN_DIR/{GATEWAY_DROPIN}"
    needle_openers = (
        f'cat > "{scheduler_dropin_path}" <<\'EOF\'',
        f'cat > "{gateway_dropin_path}" <<\'EOF\'',
        "token_probe() {",
        "expect_status() {",
        f"{ACTIVE_PY} - <<'PY'",
    )
    # `_join_executable` drops comment lines, so a commented `# cat > ...` opener
    # cannot satisfy the pin (that is the exact false-positive class this test
    # guards against).
    executable = _join_executable(blocks)
    for opener in needle_openers:
        assert opener in executable, f"executable opener missing/commented: {opener}"


def test_rollback_executable_openers_are_live_not_commented() -> None:
    executable = _join_executable(_rollback_blocks())
    for opener in ("restore_snapshot() {", "token_probe() {", "expect_status() {",
                   f"{ACTIVE_PY} - <<'PY'"):
        assert opener in executable, f"rollback executable opener missing/commented: {opener}"


# ---------------------------------------------------------------------------
# 6. rollback: strict state parsing, self-contained gates, NO auto timer start
# ---------------------------------------------------------------------------


def test_rollback_restore_logic_is_exact_state_case_and_fail_closed() -> None:
    """Defect D: exact `case` on state marker; corrupt state FAILS closed.

    ``present`` requires the .previous file; ``absent`` removes; anything else
    (corrupted/unknown) returns nonzero instead of being treated as absent.
    """
    ordered = _join_executable(_rollback_blocks())
    assert "restore_snapshot() {" in ordered
    assert "case \"$marker\" in" in ordered or 'case "$marker" in' in ordered
    assert 'present)' in ordered
    assert 'absent)' in ordered
    # present requires the .previous snapshot; unknown/corrupt fails.
    assert ".previous" in ordered
    assert "snapshot state is corrupt/unknown" in ordered
    assert "return 1" in ordered
    assert "cp -a --preserve=mode,ownership,timestamps" in ordered
    assert 'rm -f "$live_path"' in ordered
    scheduler_dropin_path = (
        f"$HOME/.config/systemd/user/nhms-compute-scheduler.service.d/{SCHEDULER_DROPIN}"
    )
    gateway_dropin_path = (
        f"$HOME/.config/systemd/user/nhms-slurm-gateway.service.d/{GATEWAY_DROPIN}"
    )
    assert f"restore_snapshot {SECRET_PATH} slurm-gateway.env" in ordered
    assert f'restore_snapshot "{scheduler_dropin_path}" {SCHEDULER_DROPIN}' in ordered
    assert f'restore_snapshot "{gateway_dropin_path}" {GATEWAY_DROPIN}' in ordered


def test_rollback_never_auto_starts_timer_and_is_self_contained() -> None:
    """Defect D (fail-closed): the rollback block must NOT auto-start the timer.

    It is self-contained (redefines expect_status/token_probe/preflight so it
    does not depend on rollout-shell functions), runs the same auth gates, and
    leaves the scheduler FENCED (timer stopped) for manual operator recovery.
    The only ``start`` commands are the step-0 fence PROBES, which must be
    condition-skipped no-ops immediately followed by an ``is-active`` verify of
    ``inactive`` — never a real start; a real resume start is comment-only.
    """
    ordered = _join_executable(_rollback_blocks())
    # Every timer/service start in the rollback block is a fence probe: it must
    # be immediately followed (next line) by `test ... is-active ... = inactive`.
    lines = ordered.splitlines()
    for idx, line in enumerate(lines):
        if line in (
            TIMER_START_LINE,
            f"systemctl --user start {SCHEDULER_SERVICE}",
        ):
            assert idx + 1 < len(lines) and "is-active" in lines[idx + 1], (
                f"rollback start must be a skipped-start probe, not a real start: {line}"
            )
    # Self-contained gates are redefined here (not relying on rollout scope).
    for needle in ("expect_status() {", "token_probe() {", "_default_gateway_probe",
                   "expect_status 401 \"rollback no token\"",
                   "expect_status 401 \"rollback wrong token\"",
                   "expect_status 404 \"rollback disabled reset\""):
        assert needle in ordered, f"rollback must be self-contained with gate: {needle}"
    assert "token_probe || exit 1" in ordered
    # Fail-closed instruction: code rollback/fix-forward required.
    assert "code rollback" in _runbook() or "code rollback or fix-forward" in _runbook()
    # The block's own comment may mention the manual start as an instruction,
    # but it must not be an executable line.
    assert TIMER_START_LINE in _runbook()


# ---------------------------------------------------------------------------
# 7. no tracked credential value anywhere in the producer chain
# ---------------------------------------------------------------------------

def test_no_tracked_producer_file_contains_a_literal_token_value() -> None:
    """No assignment of a token value anywhere in the tracked producer chain.

    The variable NAME may appear in the generation line
    (``print("SLURM_GATEWAY_SERVICE_TOKEN=" + ...)``) and the preflight reader,
    but never a concrete literal value.
    """
    surfaces = (
        GATEWAY_UNIT,
        "infra/env/compute.example",
        "infra/env/compute.scheduler-dbfree.env.example",
        "infra/env/README.md",
        RUNBOOK,
    )
    for relative in surfaces:
        text = _read(relative)
        for line_no, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                if "SLURM_GATEWAY_SERVICE_TOKEN=" in stripped and not any(
                    marker in stripped
                    for marker in ("<credential>", "<generated", "<loaded from")
                ):
                    raise AssertionError(
                        f"{relative}:{line_no}: comment assigns a concrete token value: {stripped}"
                    )
                continue
            if "SLURM_GATEWAY_SERVICE_TOKEN=" in stripped:
                if 'print("SLURM_GATEWAY_SERVICE_TOKEN="' not in stripped and not (
                    stripped.startswith('if line.startswith("SLURM_GATEWAY_SERVICE_TOKEN=")')
                ):
                    raise AssertionError(
                        f"{relative}:{line_no}: literal token assignment outside the "
                        f"generation/reader patterns: {stripped}"
                    )
