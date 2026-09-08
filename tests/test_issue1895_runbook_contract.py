"""Static contract for the #1895 controlled live-rollout runbook section.

The section is the only authorized node-27 procedure for creating the
``nhms_cold`` bind/tablespace and moving the first production compressed chunk
group.  These tests are the reviewer gate the OpenSpec fixture names: they
freeze the gate order, the zero-``docker run`` fence, the two identity lanes,
the census/evidence/installer/runner receipt contracts (bound to the real
shipping schemas and parser tokens), the trigger table families, the closure
order and the three-state rollback contract, so a later edit that reintroduces
any known-draft defect reds here instead of reaching an operator.

The scanner deliberately operates on the #1895 section only (its own heading
range), never on the historical ``§4.3.3`` material that must stay as history,
and never executes any of the section's commands.  Every check is scoped by
``_gate``/``_gate_bash`` so a ``### Gx`` block is searched for the required
executable fence patterns — never the whole-section prose.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "tier-node27-timeseries-storage.md"

SECTION_START = "## #1895 controlled live rollout"
NEXT_SECTION = "## Timer cadence order (UTC)"

GATE_ORDER = ("G0", "G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8")

# The single exact #1929 inspect projection the runner executes; the runbook's
# fresh numeric Config.User observation must spell it verbatim.
INSPECT_FORMAT = '{"Mounts":{{json .Mounts}},"User":{{json .Config.User}}}'

# The committed sequential-budget assembly contract (assembly_values order).
ASSEMBLY_LINE = "3900,3901,7842,3600000,3600000,4"

# Exact intent sidecar naming (intent_path_for), no with_name and no guess.
INTENT_EXPR = "${RECEIPT%/*}/.${RECEIPT##*/}.intent"


def _section() -> str:
    """Return the #1895 section text, delimited by its own two headings."""
    text = RUNBOOK.read_text(encoding="utf-8")
    start = text.index(SECTION_START)
    end = text.index(NEXT_SECTION)
    return text[start:end]


def _gate(gate: str) -> str:
    """Return one gate's block, delimited by the next ``### G`` heading."""
    section = _section()
    start = section.index(f"### {gate} ")
    candidates = [
        section.index(f"### {candidate} ") for candidate in GATE_ORDER if section.find(f"### {candidate} ") > start
    ]
    nxt = min(candidates) if candidates else len(section)
    return section[start:nxt]


def _norm(text: str) -> str:
    """Collapse whitespace so prose wrapping line breaks never defeat a check."""
    return re.sub(r"\s+", " ", text)


def _bash_fences(text: str) -> list[tuple[int, str]]:
    """All bash fences in the text, as (opening line number, body).

    Markdown permits indented fences. A closing fence may be no more indented
    than its opener, which avoids treating a nested ````` block in an unrelated
    indented list as the end of a shell fence.
    """

    opening = re.compile(r"^( {0,3})(`{3,}|~{3,})([^\s]*)[^\n]*$", flags=re.MULTILINE)
    lines = text.splitlines()
    fences: list[tuple[int, str]] = []
    active: tuple[int, str, int, int, int] | None = None
    for line_number, line in enumerate(lines, start=1):
        if active is None:
            match = opening.match(line)
            if match is None or match.group(3).lower() not in {"bash", "sh", "shell"}:
                continue
            active = (line_number, match.group(2)[0], len(match.group(2)), len(match.group(1)), line_number)
            continue
        line_start, character, width, indentation, body_start = active
        stripped = line.lstrip(" ")
        leading = len(line) - len(stripped)
        if leading <= indentation and stripped.startswith(character * width) and set(stripped) == {character}:
            body_lines = lines[body_start: line_number - 1]
            fences.append((line_start, "\n".join(body_lines)))
            active = None
    if active is not None:
        raise AssertionError(f"unclosed bash fence at line {active[0]}")
    return fences


def _gate_bash(gate: str) -> list[tuple[int, str]]:
    """All executable bash fences inside one gate's block."""
    return _bash_fences(_gate(gate))


def _logical_lines(body: str) -> list[str]:
    """Join backslash continuations without executing anything."""
    out: list[str] = []
    buf: list[str] = []
    for line in body.splitlines():
        kept = line.rstrip().rstrip("\\").rstrip()
        buf.append(kept)
        if line.rstrip().endswith("\\"):
            continue
        out.append(" ".join(part.strip() for part in buf))
        buf = []
    if buf:
        out.append(" ".join(part.strip() for part in buf))
    return out


def _gate_lines(gate: str) -> list[str]:
    return [line for _, body in _gate_bash(gate) for line in _logical_lines(body)]


def _section_lines() -> list[str]:
    return [line for _, body in _bash_fences(_section()) for line in _logical_lines(body)]


def _section_prose() -> str:
    fences = _bash_fences(_section())
    text = _section()
    for _opening, body in fences:
        text = text.replace(f"```bash\n{body}\n```", "")
    return text


# ---------------------------------------------------------------------------
# Placement and gate order
# ---------------------------------------------------------------------------


def test_section_sits_after_the_1894_contract_and_before_timer_cadence() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    try:
        start = text.index(SECTION_START)
    except ValueError:
        pytest.fail(f"{SECTION_START!r} heading missing from the runbook")
    end = text.index(NEXT_SECTION)
    assert start > text.index("### Disposable installer oracle prerequisite")
    assert end > start
    assert "## #1894 cold-tablespace installation and governance contract" in text


def test_gates_are_ordered_g0_through_g8() -> None:
    headings = [line for line in _section().splitlines() if re.match(r"^### G\d+", line)]
    found = [re.match(r"^### (G\d+)", line).group(1) for line in headings]
    assert found == list(GATE_ORDER), f"gate headings out of order: {found}"


def test_each_gate_names_its_blocking_contract_and_a_stop_condition() -> None:
    for gate in GATE_ORDER:
        block = _gate(gate)
        assert re.search(r"NO-GO|stop|forbidden|must not|off the table", block, flags=re.IGNORECASE), (
            f"{gate} has no stop condition"
        )


# ---------------------------------------------------------------------------
# Docker fence
# ---------------------------------------------------------------------------


def test_section_has_no_docker_run_in_any_fence() -> None:
    offenders = [
        line
        for _, body in _bash_fences(_section())
        for line in _logical_lines(body)
        if re.search(r"\bdocker run\b", line)
    ]
    assert not offenders, "docker run inside the #1895 executable fence:\n" + "\n".join(offenders)


def test_issue1895_bash_fences_pass_bash_n(tmp_path: Path) -> None:
    for opening, body in _bash_fences(_section()):
        script = tmp_path / f"tier-issue1895-fence-{opening}.sh"
        script.write_text(body, encoding="utf-8")
        completed = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, check=False)
        assert completed.returncode == 0, f"line {opening}: {completed.stderr}"


def test_section_names_the_historical_recipe_as_forbidden_entrypoint() -> None:
    section = _section()
    assert "§4.3.3" in section
    assert re.search(r"(?i)forbidden path|not.{0,25}entrypoint|must not.{0,25}(use|copy|adapt)", section)
    # The historical block itself must remain in the runbook.
    text = RUNBOOK.read_text(encoding="utf-8")
    start = text.index("#### 4.3.3 Recreating the `nhms-db` container (mount-critical)")
    assert "docker run -d" in text[start : text.index("### 4.4", start)]


# ---------------------------------------------------------------------------
# Identity lanes
# ---------------------------------------------------------------------------


def test_identity_observer_covers_both_lanes_with_distinct_formats() -> None:
    section = _section()
    assert "node27_cold_identity_observe.py installer" in section
    assert "node27_cold_identity_observe.py runner" in section
    assert "major:minor:mount-id:source" in section
    assert "st_dev:st_ino" in section
    assert "INSTALLER_DEVICE_IDENTITY" in section
    assert "RUNNER_DEVICE_IDENTITY" in section
    # The two identities are deliberately different fields, never copied.
    assert re.search(r"(?i)never.{0,25}(copied|assigned|swapped)", section)


def test_installer_identity_observed_before_install_runner_after_path_creation() -> None:
    g5 = _gate("G5")
    installer_observe = g5.index("node27_cold_identity_observe.py installer")
    dry_run = g5.index("node27_cold_tablespace_install.py")
    enforce = g5.index("--enforce \\")
    runner_observe = g5.index("node27_cold_identity_observe.py runner")
    assert installer_observe < dry_run
    assert dry_run < enforce
    assert enforce < runner_observe


def test_identity_observation_runs_before_any_cold_bind_mutation() -> None:
    g5 = _gate("G5")
    installer_observe = g5.index("node27_cold_identity_observe.py installer")
    dry_run = g5.index("node27_cold_tablespace_install.py")
    assert installer_observe < dry_run


# ---------------------------------------------------------------------------
# No tautologies / no placeholder binders / no historical hard-code
# ---------------------------------------------------------------------------


def test_no_tautological_assertions_or_placeholder_binders() -> None:
    for line in _section_lines():
        assert " or True" not in line, f"tautological 'or True' present: {line}"
        assert "if False else True" not in line, f"tautological 'if False else True' present: {line}"
        assert "/usr/bin/true" not in line, f"placeholder /usr/bin/true binder present: {line}"
        assert "|| true" not in line, f"swallowed failure '|| true' present: {line}"
        assert "if False" not in line, f"dead 'if False' branch present: {line}"


def test_never_uses_the_historical_numeric_runtime_pair_as_identity() -> None:
    section = _section()
    # The historical pair may appear only in a negative/history context: the
    # identifier must come from the fresh shipping parser, never the old pair.
    for line in _section_prose().splitlines():
        if "1005:1005" in line:
            assert "history" in line.lower() and "not identity" in line.lower(), line
    for _opening, body in _bash_fences(_section()):
        assert "1005:1005" not in body, "historical pair used inside an executable fence"
    assert "Config.User" in section


def test_fresh_numeric_user_uses_the_shipping_parser_and_exact_inspect_format() -> None:
    g4 = _gate("G4")
    assert INSPECT_FORMAT in g4
    assert "parse_container_exec_user" in g4
    # Never a bare {{.Config.User}} inspect inside an executable fence.
    for _opening, body in _gate_bash("G4"):
        assert "'{{.Config.User}}'" not in body
        assert "{{.Config.User}}" not in body.replace(INSPECT_FORMAT, "")


# ---------------------------------------------------------------------------
# Installer argv / root evidence contract
# ---------------------------------------------------------------------------


def test_installer_argv_carries_the_exact_root_evidence_contract() -> None:
    g5 = _gate("G5")
    for token in (
        "--expected-mode 0700",
        "--evidence-max-age-seconds 900",
        "--evidence-owner-uid 0",
        "--evidence-approved-mode 0600",
        "--mdadm-bin /usr/sbin/mdadm",
        "--smartctl-bin /usr/sbin/smartctl",
        "--backup-inventory-bin /usr/local/sbin/nhms-backup-inventory",
        "--install-required-bytes",
        "--rollback-headroom-bytes",
    ):
        assert token in g5, token


def test_root_evidence_uses_production_parsers_and_exact_argv() -> None:
    g2 = _gate("G2")
    assert "/usr/sbin/mdadm --detail /dev/md0" in g2
    assert "/usr/sbin/smartctl -H" in g2
    assert "/usr/local/sbin/nhms-backup-inventory --json" in g2
    assert '"schema_version"' in g2
    assert '"1.0"' in g2
    assert "parse_mdadm_evidence" in g2
    assert "parse_smart_evidence" in g2
    assert "parse_backup_inventory" in g2
    assert "EvidencePolicy" in g2
    assert "covered_paths" in g2
    assert "dict.fromkeys" not in g2


def test_g2_output_is_verbatim_producer_text_not_json_roundtrip() -> None:
    g2 = _gate("G2")
    assert '"output": backup_text' in g2
    assert "json.dumps(backup_document" not in g2


def test_root_evidence_never_invokes_the_synthetic_helper_for_production() -> None:
    section = _section()
    assert "node27_cold_tablespace_root_evidence_setup.py" in section
    assert re.search(r"(?i)MUST NOT|disposable-only|forbidden", section)


def test_g5_recaptures_evidence_before_enforce() -> None:
    g5 = _gate("G5")
    dry_run = g5.index("installer-dryrun")
    recapture = g5.index("EVID_STAMP2")
    enforce = g5.index("ENFORCE_RECEIPT")
    assert dry_run < recapture < enforce, "G5 must re-capture with -2 paths before enforce"
    assert "INSTALL_ARGS=(" in g5[g5.index("Then the single enforce invocation") :]


# ---------------------------------------------------------------------------
# Census artifact contract (real schema)
# ---------------------------------------------------------------------------


def test_census_artifact_uses_real_top_level_and_config_nested_fields() -> None:
    g1 = _gate("G1")
    assert '"artifact"' in g1
    assert '"artifact_version"' in g1
    assert '"verdict"' in g1
    assert '"generated_at"' in g1
    assert '"head_sha"' in g1
    assert '"config"' in g1
    assert 'artifact["config"]["application_name"]' in g1
    assert 'artifact["config"]["session_read_only"]' in g1


def test_census_uses_verdict_binder_and_capacity_policy_arithmetic() -> None:
    g1 = _gate("G1")
    assert 'artifact["verdict"]' in g1
    assert '"group_keys"' in g1
    assert '"residency_counts"' in g1
    assert '"capacity_policy"' in g1
    assert '"expansion_values"' in g1
    assert '"retained_values"' in g1
    assert '"census_digest"' in g1
    assert 'policy["E"]' in g1
    assert 'policy["S"]' in g1
    assert 'policy["rollback_headroom_bytes"]' in g1
    assert 'policy["installer_required_cold_free_bytes"]' in g1
    assert 'policy["wal_reserve_bytes"]' in g1
    assert "$(( 2 * E ))" not in g1 and "$((2*E))" not in g1


def test_census_never_accepts_already_cold_or_oldest_six() -> None:
    g1 = _gate("G1")
    assert "all_source" in g1
    # The prohibition on "oldest six of a larger set" is a rollout convention in
    # force for G1: exactly six complete eligible all-source groups, never a
    # truncation.
    assert "taking the oldest six" in _section()
    assert re.search(r"(?i)never.{0,30}oldest", _section())


def test_census_gate_uses_readonly_set_session_contract() -> None:
    g1 = _gate("G1")
    # The driver-native read-only set is the required mechanism; the SQL
    # statement is only ever named as the thing that would be too late.
    assert "set_session(readonly=True, autocommit=False)" in g1
    assert "before any cursor SQL" in _norm(g1)
    for _opening, body in _gate_bash("G1"):
        assert "SET SESSION CHARACTERISTICS" not in body
    # The binder must machine-assert the observed flag, not reassert a constant.
    assert 'artifact["config"]["session_read_only"] is True' in g1


def test_g1_policy_file_is_exclusive_no_clobber_write() -> None:
    g1 = _gate("G1")
    assert "O_CREAT | O_EXCL" in g1 or "O_EXCL" in g1
    assert "os.fchmod(fd, 0o600)" in g1
    assert 'open(os.environ["POLICY_FILE"], "w"' not in g1


def _python_heredoc(body: str, marker: str = "PY") -> str:
    start = body.index(f"<<'{marker}'")
    after = body[start:].split("\n", 1)[1]
    return after[: after.index(f"\n{marker}")]


def _held_fence(gate: str, needle: str) -> str:
    body = next(item[1] for item in _gate_bash(gate) if needle in item[1])
    assert "json.load(open" not in body and "open(bracket)" not in body
    assert "read_held_private_json" in body and "read_held_private_text" in body
    return body


def test_g1_freeze_validates_digest_as_lowercase_hex_and_bytes_as_decimal(tmp_path: Path) -> None:
    freeze = next(body for _opening, body in _gate_bash("G1") if "CENSUS_DIGEST" in body and "POLICY_FILE" in body)
    python = _python_heredoc(freeze)
    digest = "0a1b2c3d4e5f6789abcdef0123456789abcdef0123456789abcdef0123456789"
    os.chmod(tmp_path, 0o700)
    census = tmp_path / "census.json"
    policy_bytes = {
        "E": "4096",
        "S": "8192",
        "cold_reserve_bytes": "4096",
        "wal_reserve_bytes": "4096",
        "install_required_bytes": "16384",
        "rollback_headroom_bytes": "8192",
    }
    census.write_text(
        json.dumps({"verdict": "GO", "census_digest": digest, "capacity_policy": policy_bytes}),
        encoding="utf-8",
    )
    os.chmod(census, 0o600)
    policy = tmp_path / "capacity-policy.env"
    completed = subprocess.run(
        ["uv", "run", "--no-sync", "python", "-c", python],
        cwd=tmp_path,
        env={**os.environ, "CENSUS_ARTIFACT": str(census), "POLICY_FILE": str(policy), "PYTHONPATH": str(REPO_ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    text = policy.read_text(encoding="utf-8")
    assert f"CENSUS_DIGEST={digest}" in text
    assert "E=4096" in text and "S=8192" in text
    assert re.search(r"\[0-9a-f\]\{64\}", python)
    values_block = python.split("values = {", 1)[1].split("}", 1)[0]
    assert "CENSUS_DIGEST" not in values_block


def test_current_run_g1_g3_g4_g5_g6_g8_fences_use_held_helpers() -> None:
    g1 = _gate("G1")
    assert "read_held_private_json" in g1 and "read_held_private_text" in g1
    assert "valid-times-baseline.json" in g1 and "BASELINE_BRACKET" in g1
    assert 'chmod 600 "$BASELINE_FILE"' in g1
    assert "G7" not in g1 or "baseline" in g1.lower()
    _held_fence("G1", "valid-times-baseline.json")
    python = _python_heredoc(_held_fence("G3", "parse_probe_report"))
    assert "os.lstat" not in python and "os.stat(" not in python
    assert "Path(path).stat" not in python
    held = python.split("read_held_private_json", 1)[1]
    assert "assert_report_within_command_bracket" in held
    assert 'report_mtime=facts["st_mtime_ns"]' in held
    projection = next(body for _opening, body in _gate_bash("G4") if "runtime-projection.json" in body)
    assert "json.load(open" not in projection
    g4_py = _python_heredoc(projection)
    assert "read_held_private_json" in g4_py and "parse_container_exec_user" in g4_py
    assert "parse_container_exec_user(" in g4_py.split("read_held_private_json", 1)[1]
    enforce = next(body for _opening, body in _gate_bash("G5") if '"outcome"' in body and "installed" in body)
    assert "json.load(open" not in enforce and "open(bracket)" not in enforce
    assert "read_held_private_json" in enforce and "read_held_private_text" in enforce
    g5, g6, g8 = (" ".join(_gate_lines(name)) for name in ("G5", "G6", "G8"))
    assert "scripts/node27_issue1895_census_bind.py" in g5
    assert "scripts/node27_issue1895_sequential_receipt.py" in g6
    assert "read_held_private_json" in _gate("G6")
    assert "scripts/node27_issue1895_group_reconcile.py" in g8
    assert "scripts/node27_issue1895_post_target_observe.py" in g8


# ---------------------------------------------------------------------------
# Isolated oracle: real probe schema tokens
# ---------------------------------------------------------------------------


def test_oracle_uses_shipping_probe_parser_and_owned_unique_identity() -> None:
    g3 = _gate("G3")
    assert "--mode isolated-cluster" in g3
    assert "parse_probe_report" in g3
    assert 'report["status"] == "passed"' in g3
    assert '"sequence"' in g3
    assert '"cleanup"' in g3
    assert "container_absent" in g3
    assert "work_root_absent" in g3
    assert "PROBE_NAME" in g3
    assert "^nhms-1892-probe-[0-9a-f]{8,32}$" in g3
    assert "--host-port" in g3 and "55492" in g3
    assert 'docker inspect "$PROBE_NAME"' in g3
    assert 'test ! -e "$PROBE_ROOT"' in g3


def test_g3_cleanup_is_exact_no_prefix_grep() -> None:
    g3 = _gate("G3")
    assert "docker ps -a" not in g3
    assert 'grep -F "nhms-1892-probe-"' not in g3
    assert "grep -F nhms-1892-probe" not in g3


def test_g3_readonly_checks_are_machine_asserted() -> None:
    g3 = _gate("G3")
    assert 'test "$rc" -eq 0 ||' in g3
    assert "CUTOFF_COUNT" in g3
    assert 'test "$CUTOFF_COUNT" = "$REQUIRE_COUNT"' in g3
    assert 'test -z "$TARGET_ABSENT"' in g3
    assert "tablespace_name" in g3
    assert "SELECT tablespace FROM _timescaledb_catalog.tablespace" not in g3
    assert "node27_external_contract_snapshot.py --check" in g3


# ---------------------------------------------------------------------------
# Installer receipts
# ---------------------------------------------------------------------------


def test_installer_dry_run_receipt_asserts_mode_outcome_bracket_and_freshness() -> None:
    g5 = _gate("G5")
    assert '"mode"' in g5 and '"dry-run"' in g5
    assert '"outcome"' in g5 and '"dry_run"' in g5
    assert "head_sha" in g5
    assert "generated_at" in g5
    assert "bracket" in g5
    assert "900" in g5


def test_installer_dry_run_never_uses_a_tautological_path_device_identity() -> None:
    g5 = _gate("G5")
    assert 'document["path"]["device_identity"] is None' in g5
    assert '"host_path"][:0]' not in g5
    assert 'document["path"]["exists"] is False' in g5


def test_installer_receipt_uses_real_authority_schema_fields() -> None:
    g5 = _gate("G5")
    assert '"authority"' in g5
    assert "recovery_authority" in g5
    # The runbook may name the non-existent field only as a negation; no fence
    # may read it, and no prose may assert a groups_moved=0 rollback.
    for _opening, body in _gate_bash("G5"):
        assert "groups_moved" not in body, "installer fence reads non-existent groups_moved"
    assert 'document["authority"]["groups_moved"]' not in g5


def test_installer_argv_never_carries_a_database_url() -> None:
    for gate in GATE_ORDER:
        for _opening, body in _gate_bash(gate):
            assert "--database-url" not in body, f"{gate} fence carries --database-url"


def test_installer_rollback_legal_only_before_terminal_via_reconcile() -> None:
    section = _section()
    assert "reconcile()" in section
    assert "recovery path" in section.lower() or "recovery-path" in section
    assert "no rollback CLI" in section or "there is none" in section
    # Terminal installed closes the authority: no later operator rollback.
    assert "Any trigger after" in section
    assert "preserve" in section.lower()
    # The non-existent installer field must never be quoted as a rollback rule
    # anywhere in the section, prose or fence.
    norm = _norm(_section_prose())
    mentions = [match.group(0) for match in re.finditer(r".{0,30}groups_moved.{0,30}", norm)]
    assert mentions, "the omitted field must still be named once as absent"
    for mention in mentions:
        assert "no " in mention.lower() or "not" in mention.lower(), (
            f"groups_moved must be named as absent, found: {mention}"
        )
    for _opening, body in _bash_fences(_section()):
        assert "groups_moved" not in body, "groups_moved read inside a fence"


# ---------------------------------------------------------------------------
# Runner receipts, environment, one-group-at-a-time
# ---------------------------------------------------------------------------


def test_runner_receipt_requires_fresh_migrated_and_one_per_receipt() -> None:
    g6 = _gate("G6")
    assert "unique_migrated_observation" in g6
    assert "assert_sequential_tick_receipt" in g6
    assert "scripts/node27_issue1895_sequential_receipt.py" in g6
    assert 'len(receipt["selected"]) == 1 and not receipt["deferred"]' not in g6
    assert "already_cold" in _gate("G8")
    assert 'in {"migrated", "already_cold"}' not in g6


def test_intent_sidecar_path_matches_the_shipping_owner_rule() -> None:
    g6 = _gate("G6")
    assert INTENT_EXPR in g6
    assert "intent_path_for" in g6
    # with_name may appear only as the named wrong alternative in prose;
    # no executable fence may use it.
    for _opening, body in _gate_bash("G6"):
        assert "with_name" not in body, "G6 fence computes intent path with with_name"


def test_runner_invocation_sources_the_private_env() -> None:
    # G6 sources the private cold env before every runner invocation.
    sources = 0
    for _opening, body in _gate_bash("G6"):
        if ". /home/nwm/NWM/infra/env/node27-cold-residency.env" in body:
            sources += 1
    assert sources == 2, f"G6 must source the cold env before preview and again before the loop, found {sources}"


def test_one_invocation_per_group_with_per_tick_bound_one() -> None:
    g6 = _gate("G6")
    assert "PER_TICK_BOUND=1" in g6
    assert "per_tick_bound" in g6
    assert 'assert receipt["per_tick_bound"] == 1' in g6
    assert "$GROUP_INDEX" in g6
    assert "GROUP_INDEX=$(( GROUP_INDEX + 1 ))" in g6


def test_g6_loop_reads_without_word_splitting() -> None:
    g6 = _gate("G6")
    assert "while IFS= read -r GROUP" in g6
    assert "done < <(" in g6
    assert "for GROUP in $(/home/nwm/NWM/.venv/bin/python" not in g6


def test_g6_preview_requires_exactly_one_matching_key() -> None:
    g6 = _gate("G6")
    assert "selected_keys <= set(census" not in g6
    assert "assert_sequential_tick_receipt" in g6
    assert "call_index=1" in g6
    assert 'census["group_keys"][0]' in g6
    assert 'receipt["outcome"] == "clean"' in g6
    assert 'assert len(receipt["selected"]) == 1' not in g6


def test_receipt_key_binds_to_the_current_census_key_not_a_group_count() -> None:
    g6 = _gate("G6")
    assert "group_keys" in g6
    assert "durable_key" in g6
    assert "assert key == expected_key" in g6
    assert "COUNT(DISTINCT" not in g6
    assert "count(DISTINCT" not in g6


def test_no_live_move_back_entrypoint() -> None:
    section = _section()
    assert re.search(r"(?i)no move-back|no --rollback|no inverse mode", section)
    assert re.search(r"(?i)owning[ -]implementation", section)


def test_runner_fence_has_no_database_url_in_argv() -> None:
    for gate in ("G6", "G7", "G8"):
        for _opening, body in _gate_bash(gate):
            assert "--database-url" not in body, f"{gate} passes a DSN by argv"


# ---------------------------------------------------------------------------
# G7: plan gates, display/publication families
# ---------------------------------------------------------------------------


def test_plan_gates_do_not_ban_decompresschunk_wholesale() -> None:
    g7 = _gate("G7")
    assert "Seq Scan" in g7
    assert "DecompressChunk" in g7
    assert re.search(r"(?i)all.chunk decompression|all.chunk", g7)
    assert "not chunks" not in g7


def test_issue1895_receipt_fences_use_nanosecond_instants_and_c2_closes_after_publication() -> None:
    section = _section()
    assert "date -u +%FT%T%:z" not in section
    assert section.count("date -u +%FT%T.%N%:z") >= 6
    g7 = _gate("G7")
    c2_start = g7.index("C2_CMD_START")
    c2_accept = g7.index("scripts/node27_issue1895_readonly_accept.py")
    c2_end = g7.index("C2_CMD_END")
    c2_bind = g7.index("scripts/node27_issue1895_readonly_accept_bind.py")
    assert c2_start < c2_accept < c2_end < c2_bind


def test_g7_c1_c2_c3_owners_replace_grep_and_empty_aggregator() -> None:
    g7 = _gate("G7")
    for token in (
        "scripts/node27_issue1895_display_runtime.py",
        "scripts/node27_issue1895_display_runtime_bind.py",
        "scripts/node27_issue1895_readonly_accept.py",
        "scripts/node27_issue1895_readonly_accept_bind.py",
        "scripts/node27_issue1895_publication_current.py",
        "scripts/node27_issue1895_publication_current_bind.py",
    ):
        assert token in g7
    assert "scripts/validate_two_node_e2e_evidence.py" not in g7
    assert "--full-scope" not in g7
    c1_c3 = g7[g7.index("C1:") : g7.index("The #1342 SQL/API oracle")]
    assert "curl -fsS" not in c1_c3
    assert "docker exec nhms-db psql" not in c1_c3
    assert 'mktemp -d "$REPO_ROOT/artifacts/.nhms-issue1895-readonly-XXXXXX"' in g7
    assert '--evidence-root "$RUN_ROOT/receipts/readonly-boundary"' not in g7
    assert "unset NHMS_DISPLAY_READONLY_DATABASE_URL NHMS_READONLY_DB_VALIDATION_DATABASE_URL" in g7
    assert "--merge-declared-source GFS --merge-declared-source IFS" in g7
    assert 'mktemp -d "$RUN_ROOT/receipts/c4-display-XXXXXX"' in g7
    assert 'mktemp -d "$RUN_ROOT/receipts/river-click-XXXXXX"' in g7
    assert "$REPO_ROOT/.nhms-issue1895-c4-display-" not in g7
    assert "$REPO_ROOT/.nhms-issue1895-riverclick-" not in g7


def test_g7_orders_direct_c3_after_c4_river_click_and_performance_binders() -> None:
    g7 = _gate("G7")
    c3_owner = g7.index("scripts/node27_issue1895_publication_current.py")
    assert c3_owner > g7.index("c4-receipt-binder.mjs")
    assert c3_owner > g7.index("river-click-receipt-binder.mjs")
    assert c3_owner > g7.index("scripts/node27_issue1895_performance_bind.py")


def test_g7_covers_performance_plans_display_and_internal_observability() -> None:
    g7 = _gate("G7")
    for token in ("valid-times", "publication", "GFS", "IFS", "#1342", "river-click", "P95"):
        assert token in g7, f"G7 missing token {token}"
    assert "test:e2e:live-c4-display" in g7
    assert "c4-receipt-binder.mjs" in g7
    assert "schemas/frontend_c4_live_evidence.schema.json" in g7
    assert "test:e2e:live-display" not in g7
    assert "start-display-api.sh" in g7
    assert "validate_readonly_db_boundary.py" in g7
    assert "scripts/node27_issue1895_display_runtime.py" in g7
    assert "C1 evidence" in g7


def test_g7_valid_times_references_the_g1_baseline_path() -> None:
    g7 = _gate("G7")
    assert "valid-times-baseline.json" in g7
    assert '"$RUN_ROOT/census/valid-times-baseline.json"' in g7 or "census/valid-times-baseline.json" in g7
    assert "receipts/valid-times-baseline.json" not in g7


def test_g7_publication_counts_bind_canonical_sources_and_current_cycle() -> None:
    g7 = _gate("G7")
    assert "scripts/node27_issue1895_publication_current.py" in g7
    assert "--registry" not in g7
    publication_owner = (
        REPO_ROOT / "packages" / "common" / "node27_issue1895_publication_current.py"
    ).read_text(encoding="utf-8")
    assert "CANONICAL_SCHEDULER_REGISTRY_MANIFEST" in publication_owner
    assert 'Path("/home/ghdc/nwm/object-store/scheduler/registry/manifest-last.json")' in publication_owner
    assert "CANONICAL_SCHEDULER_REGISTRY_MANIFEST_ALIASES" not in publication_owner
    assert "registry_generated_at" in publication_owner
    assert "_registry_checksum" in publication_owner
    assert "registry mtime is not a freshness oracle" in g7
    assert '--c4-receipt "$C4_RECEIPT"' in g7
    assert "scripts/node27_issue1895_publication_prove.py" not in g7
    assert "grep -ci 'gfs'" not in g7
    assert "CURRENT_CYCLE" not in g7
    assert "gfs_expected_count=sum" not in g7


def test_g7_plan_binder_does_real_all_chunk_comparison() -> None:
    g7 = _gate("G7")
    assert "Seq Scan" in g7
    assert "DecompressChunk" in g7
    assert "scripts/node27_issue1895_performance_oracle.py" in g7
    assert "--display-env /home/nwm/NWM/infra/env/display.env" in g7
    assert "scripts/node27_issue1895_performance_bind.py" in g7
    for _opening, body in _gate_bash("G7"):
        assert "Shared Read Buffers:" not in body
        assert "json.load(open(path))" not in body
    assert "assert not chunks" not in g7


# ---------------------------------------------------------------------------
# G8: restore semantics and natural tick evidence
# ---------------------------------------------------------------------------


def test_g8_restores_by_recorded_unit_file_state_without_enable_now() -> None:
    g8 = _gate("G8")
    assert "UnitFileState" in g8
    assert 'case "$STATE" in' in g8
    assert "masked|unknown" in g8
    assert "never `enable --now`" in g8 or "never `start`" in g8
    lines = _gate_lines("G8")
    for line in lines:
        # `enable --now` is banned outright (force-fires the tick); the string is
        # only legal inside a prose line that names it as forbidden.
        if "enable --now" in line:
            assert "#" in line and "never" in line, f"executable enable --now: {line}"
        # `start` may re-arm a *.timer (G4 stop leaves it deactivated; enable
        # alone cannot re-arm it in this session) but must never start a
        # *.service — that would fabricate a tick. `restart` is banned outright.
        service_start = re.search(r"\bsystemctl --user start ([^ ]*\.service)\b", line)
        assert service_start is None, f"G8 starts a service by hand: {line}"
        assert "systemctl --user restart" not in line, f"G8 restarts a unit by hand: {line}"
    # The restore loop must distinguish all four recorded states.
    for token in ("enabled)", "static)", "disabled)", "masked|unknown)"):
        assert token in g8, token
    # A static unit has no [Install] section (shipping unit files confirm), so
    # "static" must be a no-op, never `systemctl --user enable` (which fails on
    # a static unit) and never a hand start.
    static_branch = g8[g8.index("static)") : g8.index("masked|unknown)")]
    assert "systemctl --user enable" not in static_branch, "static branch enables a static unit"
    assert "systemctl --user start" not in static_branch, "static branch starts a unit"
    assert ":" in static_branch, "static branch must be a no-op"
    # The enabled branch must re-arm a stopped timer in this live session:
    # enable alone persists the symlink but does not make a stopped timer active.
    enabled_branch = g8[g8.index("enabled)") : g8.index("static)")]
    assert "systemctl --user enable" in enabled_branch
    assert re.search(r"/usr/bin/systemctl --user start \"\$UNIT\"", enabled_branch), (
        "enabled branch must start the timer to re-arm it"
    )
    # Fail-closed: an enabled non-timer must be refused, not started.
    assert 'echo "NO-GO: enabled' in enabled_branch


def test_g8_observes_natural_tick_through_systemd_fields_not_sleep_or_inotify() -> None:
    g8 = _gate("G8")
    assert "LastTriggerUSec" in g8
    assert "ExecMainStartTimestamp" in g8
    assert "ExecMainExitTimestamp" in g8
    # The watch loop may sleep to wait; it must not use sleep/inotify as proof.
    # inotifywait is named in prose only as the forbidden alternative, never in
    # an executable fence.
    assert "inotifywait" in g8, "the forbidden alternative must be named once"
    for _opening, body in _gate_bash("G8"):
        assert "inotifywait" not in body, "G8 fence uses inotifywait as proof"
    assert "The poll sleep only waits" in g8 or "the poll sleep only waits" in g8
    lines = _gate_lines("G8")
    assert "/usr/bin/sleep 5" in " ".join(lines), "watch loop must bound-wait"
    # The proof fields must be machine-compared in the fence, not merely named
    # in prose.
    assert "TIMER_AFTER" in " ".join(lines) and "SERVICE_NOW" in " ".join(lines)
    assert 'test "$TIMER_AFTER" != "$TIMER_BEFORE"' in " ".join(lines)
    assert 'test "$SERVICE_NOW" != "$SERVICE_BEFORE"' in " ".join(lines)


def test_g8_noop_requires_catalog_proof_and_moved_key_presence() -> None:
    g8 = _gate("G8")
    assert "REMAINING_ALL_SOURCE" not in g8
    assert "scripts/node27_issue1895_post_target_observe.py" in g8
    assert "scripts/node27_issue1895_group_reconcile.py" in g8
    assert "assert_natural_receipt_identity" in g8
    assert "newly_terminal_keys" in g8
    assert "complete_source_keys" in g8
    assert 'test "$MOVED_GROUPS" -ge "$REQUIRE_COUNT"' not in g8
    assert "no_op" in g8


def test_rollout_conventions_require_one_maintenance_shell_for_cross_fence_state() -> None:
    section = _section()
    assert "One maintenance shell owns G0…G8." in section
    assert "in that same shell" in section
    assert "Never paste an individual downstream" in section
    for variable in ("$RUN_ROOT", "$RUN_STAMP", "$REVIEWED_SHA", "$UNITS_ENABLE_STATE", "TIMER_BEFORE", "W8_PATH"):
        assert variable in section


def test_g8_binds_a_post_tick_external_horizon_before_receipt_or_group_validation() -> None:
    g8 = _gate("G8")
    restore = g8.index('/usr/bin/systemctl --user start "$UNIT"')
    service_success = g8.index('test "$RESULT" = "success"')
    service_exit = g8.index('SERVICE_EXIT=')
    w8_owner = g8.index('scripts/node27_issue1895_watermark.py')
    receipt_identity = g8.index("assert_natural_receipt_identity")
    receipt_horizon = g8.index("assert_independent_receipt_horizon")
    group_reconcile = g8.index("scripts/node27_issue1895_group_reconcile.py")
    pre_observation = g8.index('output "$PRE_NATURAL"')

    assert pre_observation < restore < service_success < service_exit < w8_owner
    assert w8_owner < receipt_identity < group_reconcile
    assert w8_owner < receipt_horizon < group_reconcile
    assert 'test ! -e "$W8_PATH"' in g8
    assert "post-tick external independent horizon" in g8
    assert "not G1" in g8
    assert "never receipt self-report" in g8
    assert g8.index('output "$PRE_NATURAL"') < g8.index('scripts/node27_issue1895_watermark.py')
    assert "expected_watermark=receipt" not in g8
    assert "expected_cutoff=receipt" not in g8
    assert '--expected-cutoff "$(/home/nwm/NWM/.venv/bin/python -c' in g8
    assert '--expected-watermark "$(/home/nwm/NWM/.venv/bin/python -c' in g8


def test_g8_systemd_facts_output_is_exclusive_private_and_checked() -> None:
    g8 = _gate("G8")
    timer_show = g8.index('TIMER_SHOW="$RUN_ROOT/receipts/timer-show-$RUN_STAMP.txt"')
    service_show = g8.index('SERVICE_SHOW="$RUN_ROOT/receipts/service-show-$RUN_STAMP.txt"')
    output = g8.index('SYSTEMD_FACTS="$RUN_ROOT/receipts/systemd-facts-$RUN_STAMP.json"')
    absent = g8.index('test ! -e "$RUN_ROOT/receipts/systemd-facts-$RUN_STAMP.json"')
    owner = g8.index("scripts/node27_issue1895_systemd_facts.py")
    regular = g8.index('test -f "$SYSTEMD_FACTS" && test ! -L "$SYSTEMD_FACTS"')
    mode = g8.index("stat -c '%a' \"$SYSTEMD_FACTS\")\" = \"600\"")
    nlink = g8.index("stat -c '%h' \"$SYSTEMD_FACTS\")\" = \"1\"")

    assert timer_show < service_show < output < absent < owner < regular < mode < nlink
    assert 'test ! -e "$TIMER_SHOW"' in g8
    assert 'test ! -e "$SERVICE_SHOW"' in g8


# ---------------------------------------------------------------------------
# Closure order
# ---------------------------------------------------------------------------


def test_closure_order_is_merge_then_close_then_archive() -> None:
    section = _section()
    block = section[section.index("Closure order, in this exact sequence:") :]
    lower = block.lower()
    merge = lower.index("merge")
    close_1895 = lower.index("close #1895")
    close_1891 = lower.index("#1891")
    archive = lower.index("archive")
    assert merge < close_1895 < close_1891 < archive
    assert "strict" in lower


# ---------------------------------------------------------------------------
# Rollback table matches the shipping three-state fixture
# ---------------------------------------------------------------------------


def test_rollback_table_matches_three_state_contract() -> None:
    section = _section()
    table = section[section.index("#### Rollback points") :]
    assert "Before any mutation" in table
    assert "reconcile()" in table
    assert "there is no rollback CLI" in table
    assert "zero groups moved" in table
    assert "stop and preserve" in table
    assert "no move-back" in table
    # Rollback rows that restore timer enablement must re-arm the recorded
    # `.timer`s (start), not just `enable` — otherwise a stopped timer never
    # fires again until the user manager restarts.
    before = next(line for line in table.splitlines() if line.startswith("| Before any mutation"))
    assert "no-op" in before.lower()
    assert "does not exist yet" in before
    assert "must not be referenced" in before
    drain = next(line for line in table.splitlines() if line.startswith("| After the writer drain"))
    assert "`start` on each recorded `.timer`" in drain
    assert "never `enable --now`" in drain
    assert "never start a `.service`" in drain
    # Six and only six rows, matching the three-state fixture: pre-mutation,
    # post-drain, post-assembly, live-authority install, terminal installed,
    # post-movement.
    row_pattern = (
        r"^\| (After any group moved|Terminal `installed`|Install in progress"
        r"|After env/unit assembly|After the writer drain|Before any mutation)"
    )
    rows = re.findall(row_pattern, table, flags=re.MULTILINE)
    assert len(rows) == 6, rows
    for _opening, body in _bash_fences(section):
        assert "groups_moved" not in body


# ---------------------------------------------------------------------------
# Existing contract must not regress
# ---------------------------------------------------------------------------


def test_runbook_keeps_timeout_budget_and_env_contract() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "TimeoutStartSec=7842" in text
    assert '"systemd_wall_seconds": 7842' in text
    assert "06:36 UTC" in text
    assert "node27-cold-residency.env" in text
    assert "node27_timeseries_budget_preflight.py" in text
    assert "OnCalendar=*-*-* 05:15:00 UTC" not in text


def test_budget_assembly_matches_the_committed_sequential_contract() -> None:
    g4 = _gate("G4")
    assert ASSEMBLY_LINE in g4
    assert "TimeoutStartSec" in g4


def test_repo_python_runs_via_no_sync_uv_or_pinned_interpreter_only() -> None:
    for line in _section_lines():
        if "python scripts/" in line or "python packages/" in line:
            assert "uv run --no-sync" in line or ".venv/bin/python" in line, f"bare repo-python invocation: {line}"
        if "uv run" in line:
            assert "--no-sync" in line, f"uv run without --no-sync: {line}"


def test_no_ssh_paste_is_mistaken_for_local_execution() -> None:
    g0 = _gate("G0")
    assert "bash -s <<'REMOTE'" in g0
    assert "heredoc body is executed" in g0
    assert "never in a local shell" in g0 or "Nothing in the heredoc body runs locally" in g0
