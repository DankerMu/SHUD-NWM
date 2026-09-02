"""Production target inspector tests for #1893, numeric runtime identity for #1929.

The oracle for the discriminating case is POSIX ownership semantics, not a live
Docker daemon: on a mode-0700 directory owned by ``1005:1005``, a probe executed
as the exact image default account (``postgres`` -> ``1000:1000``) must fail and a
probe executed as ``1005:1005`` must succeed. The fake below encodes exactly that
rule, so a regression to a name-based probe cannot pass by construction.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from packages.common.compressed_chunk_cold_runtime_catalog import ColdRuntimeError
from packages.common.compressed_chunk_cold_target import (
    CONTAINER_COLD_PATH,
    CONTAINER_EXEC_ID_DIGITS_MAX,
    CONTAINER_EXEC_ID_MAX,
    CONTAINER_EXEC_ID_MIN,
    HOST_COLD_PATH,
    INSPECT_FORMAT,
    INSPECT_OUTPUT_MAX_BYTES,
    INSPECT_TIMEOUT_SECONDS,
    LIVE_CONTAINER_NAME,
    TRUSTED_DOCKER_BIN,
    ObservedTarget,
    container_exec_id_from_decimal,
    container_writable_argv,
    inspect_container_identity_observation,
    inspect_container_writable,
    inspect_production_target,
    parse_container_exec_user,
    production_inspect_target,
    run_bounded_command,
    safe_token_echo,
    validate_container_exec_id,
)

# The account the exact image ships with, and the principal node-27's live
# container runs as (docs/runbooks/tier-node27-timeseries-storage.md,
# `docker run --user 1005:1005`, cold path `nwm:nwm` mode 0700).
IMAGE_USER = "postgres"
IMAGE_UID, IMAGE_GID = 1000, 1000
RUNTIME_UID, RUNTIME_GID = 1005, 1005
COLD_PATH_MODE = 0o700
COLD_PATH_OWNER = (RUNTIME_UID, RUNTIME_GID)
EXPECTED = {"expected_container_exec_uid": RUNTIME_UID, "expected_container_exec_gid": RUNTIME_GID}
# CPython 3.11+ refuses to convert more than this many digits, raising a bare
# ValueError that carries no error_class/stage. These tokens are how an
# unbounded parse escapes a typed refusal, so the inspectors must reject them by
# width before ever calling int().
_INT_PARSE_LIMIT = sys.get_int_max_str_digits()
_JUST_PAST_LIMIT = "4" + "0" * _INT_PARSE_LIMIT
_OVER_WIDTH_DIGITS = "9" * 5000
_WRITE_PROBE = "import os,sys; raise SystemExit(0 if os.access(sys.argv[1], os.W_OK) else 1)"


# Docker's `--format` evaluates a Go text/template with a FIXED function set
# (`json`, `printf`, `split`, ...). `dict` is a sprig/Helm helper and is NOT
# available, so `{{json (dict ...)}}` dies client-side at exit 64 before any
# daemon lookup. This renderer mirrors that constraint: an unknown function or an
# unexpected field path is a template error, exactly like the real CLI. Without
# it the fake could hand back a prebuilt JSON object and never notice the
# production format string was unparsable — which is how the original #1929
# format defect survived the unit lane.
_TEMPLATE_ACTIONS = {"json .Mounts": "Mounts", "json .Config.User": "User"}
_ACTION_RE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)


class TemplateParseError(Exception):
    """Mirrors `docker inspect --format` client-side template failure."""


def render_inspect_format(template: str, *, mounts: list[dict[str, str]], user: Any) -> str:
    values = {"Mounts": json.dumps(mounts), "User": json.dumps(user)}

    def replace(match: re.Match[str]) -> str:
        action = " ".join(match.group(1).split())
        if action not in _TEMPLATE_ACTIONS:
            if re.search(r"\bdict\b", action):
                raise TemplateParseError('template: :1: function "dict" not defined')
            raise TemplateParseError(f"unsupported inspect --format action: {action!r}")
        return values[_TEMPLATE_ACTIONS[action]]

    return _ACTION_RE.sub(replace, template)


def _projection(user: Any, *sources: str, template: str = INSPECT_FORMAT) -> str:
    mounts = [{"Destination": CONTAINER_COLD_PATH, "Source": source} for source in sources]
    return render_inspect_format(template, mounts=mounts, user=user)


def _docker_cli_probe() -> str | None:
    """A real Docker CLI to prove the template parses, or None to skip."""

    if os.path.exists(TRUSTED_DOCKER_BIN):
        return TRUSTED_DOCKER_BIN
    return shutil.which("docker")


def _host(_path: str) -> dict[str, int | str]:
    return {"device_identity": "8:11", "mode": 0o555, "uid": 999, "gid": 999}


def _cold_owner(_path: str) -> dict[str, int | str]:
    """The node-27 shape: cold path owned 1005:1005, mode 0700."""

    return {
        "device_identity": "8:11",
        "mode": COLD_PATH_MODE,
        "uid": COLD_PATH_OWNER[0],
        "gid": COLD_PATH_OWNER[1],
    }


def _owner_matched_exec_rc(argv: list[str]) -> int:
    """``test -w`` against that path: only the owner uid can write at mode 0700.

    A user *name* resolves through the image ``/etc/passwd`` (``postgres`` ->
    1000:1000), i.e. a non-owner, and fails — the node-27 observation that
    motivated #1929. Group and other bits are deliberately empty at 0700, so the
    principal and not the path is what this probe measures.
    """

    spec = argv[argv.index("--user") + 1]
    if spec == IMAGE_USER:
        return 0 if (IMAGE_UID, IMAGE_GID) == COLD_PATH_OWNER else 1
    parts = spec.split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return 1
    return 0 if (int(parts[0]), int(parts[1])) == COLD_PATH_OWNER else 1


class DockerActions:
    """Records every docker invocation so ordering claims are testable."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    @property
    def commands(self) -> list[str]:
        return [call[1] for call in self.calls]

    def writable_specs(self) -> list[str]:
        return [call[call.index("--user") + 1] for call in self.calls if "--user" in call]


def _fake_docker(
    actions: DockerActions,
    *,
    config_user: Any = f"{RUNTIME_UID}:{RUNTIME_GID}",
    sources: tuple[str, ...] = (HOST_COLD_PATH,),
    writable: bool | None = None,
    returncode: int = 0,
    stdout: str | None = None,
    stderr: str = "",
):
    def runner(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        actions.calls.append(list(argv))
        if "inspect" in argv:
            if returncode != 0:
                return SimpleNamespace(returncode=returncode, stdout=stdout or "", stderr=stderr)
            if stdout is not None:
                return SimpleNamespace(returncode=0, stdout=stdout, stderr=stderr)
            # Render the ACTUAL format string the production code asked for, and
            # fail exactly where the CLI does: an unparsable template is a client
            # side exit 64 before any daemon lookup.
            template = argv[argv.index("--format") + 1]
            try:
                rendered = _projection(config_user, *sources, template=template)
            except TemplateParseError as error:
                return SimpleNamespace(
                    returncode=64,
                    stdout="",
                    stderr=f"template parsing error: {error}",
                )
            return SimpleNamespace(returncode=0, stdout=rendered, stderr="")
        if "exec" in argv:
            if writable is not None:
                return SimpleNamespace(returncode=0 if writable else 1, stdout="", stderr="denied")
            return SimpleNamespace(returncode=_owner_matched_exec_rc(argv), stdout="", stderr="")
        raise AssertionError(argv)

    return runner


def test_single_inspect_projection_observes_bind_and_numeric_user() -> None:
    actions = DockerActions()
    observed = inspect_production_target(
        runner=_fake_docker(actions),
        host_inspect=_host,
        docker_bin=TRUSTED_DOCKER_BIN,
        **EXPECTED,
    )
    assert actions.commands == ["inspect", "exec"]
    assert actions.calls[0] == [
        "/usr/bin/docker",
        "inspect",
        "--format",
        INSPECT_FORMAT,
        LIVE_CONTAINER_NAME,
    ]
    # A small projection, not the full inspect document: two field paths and no
    # whole-container dump (a bare `{{json .}}` would carry Env/Cmd/Labels).
    assert INSPECT_FORMAT.count(".Mounts") == 1
    assert INSPECT_FORMAT.count(".Config") == 1
    assert "{{json .}}" not in INSPECT_FORMAT
    assert "postgres" not in INSPECT_FORMAT
    assert observed.container_exec_uid == RUNTIME_UID
    assert observed.container_exec_gid == RUNTIME_GID
    assert observed.writable is True


def test_inspect_format_is_a_supported_go_template_literal_projection() -> None:
    """`docker inspect --format` is a Go text/template, not Helm.

    `dict` is a sprig helper: Docker 29.1.3 rejects
    `{{json (dict "Mounts" .Mounts "User" .Config.User)}}` with exit 64
    `function "dict" not defined` BEFORE any daemon lookup, so the probe would
    fail closed on a perfectly healthy container. The supported shape is a JSON
    literal whose values come from `{{json X}}`, which emits valid JSON.
    """

    assert INSPECT_FORMAT == '{"Mounts":{{json .Mounts}},"User":{{json .Config.User}}}'
    assert "dict" not in INSPECT_FORMAT
    assert "(" not in INSPECT_FORMAT and ")" not in INSPECT_FORMAT
    # Exactly the two consumed fields, each once, and no whole-container dump.
    assert INSPECT_FORMAT.count("{{") == 2 and INSPECT_FORMAT.count("}}") == 2
    assert _ACTION_RE.findall(INSPECT_FORMAT) == ["json .Mounts", "json .Config.User"]
    assert "{{json .}}" not in INSPECT_FORMAT
    assert "postgres" not in INSPECT_FORMAT
    # Renders to one parseable JSON object carrying only those two keys.
    rendered = _projection(f"{RUNTIME_UID}:{RUNTIME_GID}", HOST_COLD_PATH)
    assert json.loads(rendered) == {
        "Mounts": [{"Destination": CONTAINER_COLD_PATH, "Source": HOST_COLD_PATH}],
        "User": f"{RUNTIME_UID}:{RUNTIME_GID}",
    }
    assert len(INSPECT_FORMAT.encode("utf-8")) < INSPECT_OUTPUT_MAX_BYTES


def test_sprig_dict_projection_is_a_client_side_template_failure() -> None:
    """The fake reproduces the CLI's ordering, so the old format cannot pass.

    Docker parses `--format` before any daemon lookup: exit 64 with
    `function "dict" not defined`. The fake renders the format string it was
    actually handed, so reintroducing a sprig helper produces the same refusal
    instead of the prebuilt JSON that previously hid the defect.
    """

    broken = '{{json (dict "Mounts" .Mounts "User" .Config.User)}}'
    actions = DockerActions()
    result = run_bounded_command(
        [TRUSTED_DOCKER_BIN, "inspect", "--format", broken, LIVE_CONTAINER_NAME],
        runner=_fake_docker(actions),
    )
    assert result.returncode == 64
    assert 'function "dict" not defined' in result.stderr

    # A CLI that refuses the format surfaces as a fail-closed target error, never
    # as a false "writable" verdict or an invented identity.
    refusing = DockerActions()
    with pytest.raises(ColdRuntimeError, match="could not inspect") as raised:
        inspect_container_identity_observation(
            runner=_fake_docker(refusing, returncode=64, stderr='template parsing error: function "dict" not defined'),
        )
    assert raised.value.error_class == "target_identity"
    assert refusing.commands == ["inspect"]


@pytest.mark.skipif(_docker_cli_probe() is None, reason="no docker CLI available")
def test_real_docker_cli_parses_the_production_inspect_format() -> None:
    """Proves the template is accepted by a real Docker CLI, without a daemon.

    Go parses `--format` client-side before any object or API lookup, so a bogus
    `-H` socket plus a nonexistent synthetic name reaches the daemon-connect
    branch (exit 1) instead of the template branch (exit 64). Nothing is created,
    inspected for real, or mutated, and the pinned production constant
    `/usr/bin/docker` is not repointed.
    """

    cli = _docker_cli_probe()
    assert cli is not None
    probe_name = "nhms-1929-template-probe-does-not-exist"
    result = subprocess.run(
        [cli, "-H", "unix:///nonexistent/nhms-1929-probe.sock", "inspect", "--format", INSPECT_FORMAT, probe_name],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    combined = (result.stdout or "") + (result.stderr or "")
    assert "template parsing error" not in combined, combined
    assert 'function "dict" not defined' not in combined, combined
    assert result.returncode != 64, combined
    assert result.returncode != 0, "the probe object must not resolve"


@pytest.mark.skipif(_docker_cli_probe() is None, reason="no docker CLI available")
def test_real_docker_cli_probe_distinguishes_the_defective_format() -> None:
    """Anti-vacuity for the probe above: the pre-fix literal DOES reach exit 64.

    Without this row the daemon-free probe could pass for reasons unrelated to
    the template (e.g. a CLI that never parses `--format`), and a regression to
    `dict` would stay green.
    """

    cli = _docker_cli_probe()
    assert cli is not None
    result = subprocess.run(
        [
            cli,
            "-H",
            "unix:///nonexistent/nhms-1929-probe.sock",
            "inspect",
            "--format",
            '{{json (dict "Mounts" .Mounts "User" .Config.User)}}',
            "nhms-1929-template-probe-does-not-exist",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 64
    assert 'function "dict" not defined' in (result.stdout + result.stderr)


def test_writable_probe_executes_the_observed_numeric_pair_and_never_a_name() -> None:
    actions = DockerActions()
    inspect_production_target(runner=_fake_docker(actions), host_inspect=_cold_owner, **EXPECTED)
    assert actions.writable_specs() == [f"{RUNTIME_UID}:{RUNTIME_GID}"]
    # No argv element anywhere is a user name or the image-default principal.
    assert all(arg != IMAGE_USER for call in actions.calls for arg in call)
    assert all(spec != f"{IMAGE_UID}:{IMAGE_GID}" for spec in actions.writable_specs())


def test_ceilings_are_the_existing_5s_and_64KiB_on_both_commands() -> None:
    assert INSPECT_TIMEOUT_SECONDS == 5
    assert INSPECT_OUTPUT_MAX_BYTES == 64 * 1024
    actions = DockerActions()
    timeouts: dict[str, Any] = {}

    def runner(argv: list[str], **kwargs: object) -> SimpleNamespace:
        timeouts[argv[1]] = kwargs["timeout"]
        return _fake_docker(actions)(argv, **kwargs)

    inspect_production_target(runner=runner, host_inspect=_cold_owner, **EXPECTED)
    assert timeouts == {"inspect": 5, "exec": 5}
    # The byte ceiling is enforced on both commands by the shared collector.
    huge = "x" * (INSPECT_OUTPUT_MAX_BYTES + 1)

    def flood(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout=huge, stderr="")

    with pytest.raises(ColdRuntimeError, match="byte ceiling"):
        inspect_container_writable(uid=RUNTIME_UID, gid=RUNTIME_GID, runner=flood)


def test_runtime_principal_writes_where_named_postgres_cannot() -> None:
    """The #1929 defect, same container and path, two principals, one mode-0700 dir."""

    actions = DockerActions()
    observed = inspect_production_target(
        runner=_fake_docker(actions),
        host_inspect=_cold_owner,
        **EXPECTED,
    )
    assert observed.writable is True
    assert actions.writable_specs() == [f"{RUNTIME_UID}:{RUNTIME_GID}"]

    # The pre-fix probe asked the image account, and `test -w` denied it.
    named = run_bounded_command(
        ["/usr/bin/docker", "exec", "--user", IMAGE_USER, LIVE_CONTAINER_NAME, "test", "-w", CONTAINER_COLD_PATH],
        runner=_fake_docker(DockerActions()),
    )
    assert named.returncode == 1

    # Even a *configured* image default cannot conjure access: identity matches,
    # the probe still refuses. No fallback, no false green.
    fallback = DockerActions()
    with pytest.raises(ColdRuntimeError, match="not writable"):
        inspect_container_writable(
            uid=IMAGE_UID,
            gid=IMAGE_GID,
            runner=_fake_docker(fallback),
        )
    assert fallback.writable_specs() == [f"{IMAGE_UID}:{IMAGE_GID}"]


@pytest.mark.parametrize(
    ("principal", "writable"),
    [
        (f"{RUNTIME_UID}:{RUNTIME_GID}", True),
        (f"{IMAGE_UID}:{IMAGE_GID}", False),
        (f"{IMAGE_UID}:{IMAGE_UID}", False),
        (f"{RUNTIME_UID}:{IMAGE_GID}", False),
        (f"{IMAGE_GID}:{RUNTIME_UID}", False),
    ],
)
def test_writable_outcome_follows_owner_match_not_the_principal_name(
    principal: str,
    writable: bool,
) -> None:
    uid_text, gid_text = principal.split(":")
    if writable:
        assert (
            inspect_container_writable(uid=int(uid_text), gid=int(gid_text), runner=_fake_docker(DockerActions()))
            is True
        )
        return
    with pytest.raises(ColdRuntimeError, match="not writable"):
        inspect_container_writable(uid=int(uid_text), gid=int(gid_text), runner=_fake_docker(DockerActions()))


def test_mode0700_owner_bits_are_a_real_filesystem_claim(tmp_path: Path) -> None:
    """Backs the fake's rule with real POSIX semantics for the writable probe.

    Only the owner bits are exercised here (a local unprivileged process cannot
    assume another uid); the owner/non-owner distinction the fake encodes is the
    same rule one class removed.
    """

    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root bypasses mode bits, so owner-write is not observable")
    cold = tmp_path / "cold"
    cold.mkdir()
    cold.chmod(COLD_PATH_MODE)
    assert run_bounded_command([sys.executable, "-c", _WRITE_PROBE, str(cold)]).returncode == 0
    cold.chmod(0o500)
    assert run_bounded_command([sys.executable, "-c", _WRITE_PROBE, str(cold)]).returncode == 1


@pytest.mark.parametrize(
    ("config_user", "reason"),
    [
        (None, "missing"),
        ("", "empty"),
        ("postgres", "named"),
        ("nwm", "named"),
        ("1005", "uid-only"),
        ("1005:", "half pair"),
        (":1005", "half pair"),
        ("0:0", "root"),
        ("0:1005", "root uid"),
        ("1005:0", "root gid"),
        ("1000:1000", "image default mismatch"),
        ("1005:1006", "gid mismatch"),
        (" 1005:1005", "leading whitespace"),
        ("1005:1005 ", "trailing whitespace"),
        ("01005:1005", "non-canonical uid"),
        ("1005:01005", "non-canonical gid"),
        ("+1005:1005", "plus syntax"),
        ("-1:-1", "negative"),
        ("1005_1005:1005", "underscore syntax"),
        (f"{CONTAINER_EXEC_ID_MAX + 1}:{CONTAINER_EXEC_ID_MAX + 1}", "above bound"),
        (f"{2**64}:{2**64}", "far above bound"),
        # Over-width tokens: every decimal string CPython will not convert.
        pytest.param(f"{_OVER_WIDTH_DIGITS}:{RUNTIME_GID}", "uid beyond the int limit", id="over-width-uid"),
        pytest.param(f"{RUNTIME_UID}:{_OVER_WIDTH_DIGITS}", "gid beyond the int limit", id="over-width-gid"),
        pytest.param(f"{_JUST_PAST_LIMIT}:{_JUST_PAST_LIMIT}", "just past limit", id="just-past-limit"),
        pytest.param(
            f"{10**CONTAINER_EXEC_ID_DIGITS_MAX}:{RUNTIME_GID}",
            "narrowest over-width uid",
            id="narrowest-over-width",
        ),
        ("1005:1005:1005", "malformed"),
        ("1005:1005:0", "malformed"),
        (1005, "non-string"),
    ],
)
def test_config_user_matrix_refuses_before_writable(config_user: Any, reason: str) -> None:
    del reason
    actions = DockerActions()
    with pytest.raises(ColdRuntimeError) as raised:
        inspect_production_target(
            runner=_fake_docker(actions, config_user=config_user),
            host_inspect=_cold_owner,
            **EXPECTED,
        )
    assert raised.value.error_class == "target_identity"
    # Nothing beyond the single inspect ran: no writable probe, no fallback.
    assert actions.commands == ["inspect"]


def test_boundaries_are_inclusive_and_root_is_excluded() -> None:
    assert parse_container_exec_user("1:1").uid == 1
    assert parse_container_exec_user(f"{CONTAINER_EXEC_ID_MAX}:{CONTAINER_EXEC_ID_MAX}").gid == (
        CONTAINER_EXEC_ID_MAX
    )
    # The last accepted token and the first rejected one differ by a single unit,
    # so the bound is inclusive and the refusal is arithmetic, not a parse failure.
    with pytest.raises(ColdRuntimeError, match="must be within"):
        parse_container_exec_user(f"{CONTAINER_EXEC_ID_MAX + 1}:1")
    with pytest.raises(ColdRuntimeError, match="must be within"):
        parse_container_exec_user("0:0")


def test_over_width_observed_component_refuses_by_width_not_by_conversion() -> None:
    """A 5000-digit Config.User component must not reach ``int()``.

    Before the width guard this raised the interpreter's bare
    ``ValueError: Exceeds the limit (4300 digits)`` — no error_class, no stage,
    and not a ``ColdRuntimeError`` at all — so the caller's refusal handling was
    skipped and the writable probe could be reached on an unvalidated principal.
    """

    for pair in (f"{_OVER_WIDTH_DIGITS}:{RUNTIME_GID}", f"{RUNTIME_UID}:{_OVER_WIDTH_DIGITS}"):
        with pytest.raises(ColdRuntimeError) as raised:
            parse_container_exec_user(pair)
        assert raised.value.error_class == "target_identity"
        assert raised.value.stage == "target_identity"
        assert "Exceeds the limit" not in str(raised.value)
        assert _OVER_WIDTH_DIGITS not in str(raised.value)
        assert len(str(raised.value).encode("utf-8")) < 256


def test_over_width_bare_numeric_json_refuses_before_writable() -> None:
    """The refusal must survive the JSON decoder, not only the pair parser.

    ``json.loads`` converts numeric literals with ``int()``, so an unquoted
    5000-digit value inside the projection raises the same bare ValueError during
    decoding — before ``parse_container_exec_user`` is ever called. The output is
    well under the 64-KiB ceiling, so the ceiling is not the guard here.
    """

    actions = DockerActions()
    raw = '{"Mounts":[{"Destination":"%s","Source":"%s"}],"User":%s}' % (
        CONTAINER_COLD_PATH,
        HOST_COLD_PATH,
        _OVER_WIDTH_DIGITS,
    )
    assert len(raw.encode("utf-8")) < INSPECT_OUTPUT_MAX_BYTES
    with pytest.raises(ColdRuntimeError) as raised:
        inspect_production_target(
            runner=_fake_docker(actions, stdout=raw),
            host_inspect=_cold_owner,
            **EXPECTED,
        )
    assert raised.value.error_class == "target_identity"
    assert "Exceeds the limit" not in str(raised.value)
    assert actions.commands == ["inspect"]


@pytest.mark.parametrize("label", ["uid", "gid"])
def test_bounded_decimal_parse_never_converts_an_unbounded_token(label: str) -> None:
    """The shared parse rule: accept the domain, refuse above it, never raise."""

    del label
    assert container_exec_id_from_decimal(str(CONTAINER_EXEC_ID_MIN)) == CONTAINER_EXEC_ID_MIN
    # 4294967294 is the last uid_t that is neither root nor the (uid_t)-1 sentinel.
    assert container_exec_id_from_decimal(str(CONTAINER_EXEC_ID_MAX)) == CONTAINER_EXEC_ID_MAX
    assert container_exec_id_from_decimal(str(CONTAINER_EXEC_ID_MAX + 1)) is None
    assert container_exec_id_from_decimal(str(4294967295)) is None
    assert container_exec_id_from_decimal(_OVER_WIDTH_DIGITS) is None
    assert container_exec_id_from_decimal(_JUST_PAST_LIMIT) is None
    # The narrowest token the width rule rejects: one digit longer than the bound.
    assert container_exec_id_from_decimal(str(10**CONTAINER_EXEC_ID_DIGITS_MAX)) is None
    # Zero is representable and in-domain for uid_t; the caller's floor rejects it
    # as an exec principal, which keeps "root" and "absent" distinguishable.
    assert container_exec_id_from_decimal("0") == 0


def test_over_width_int_is_refused_without_string_conversion() -> None:
    """The Python-config seam must not str() a huge int into its own refusal."""

    for value in (4294967295, 2**64, 10**5000):
        with pytest.raises(ColdRuntimeError) as raised:
            validate_container_exec_id(value, name="expected_container_exec_uid")
        assert raised.value.error_class == "config"
        assert raised.value.stage == "config"
        assert "Exceeds the limit" not in str(raised.value)
        assert "must be within" in str(raised.value)


@pytest.mark.parametrize("token", [_OVER_WIDTH_DIGITS, _JUST_PAST_LIMIT])
def test_refusal_messages_are_bounded_enough_to_publish(token: str) -> None:
    """Receipt ``error.reason`` is capped at 256 characters by the schema.

    An unbounded token interpolated into a refusal would produce a tombstone that
    fails its own validation and is never published — a silent loss of the refusal
    evidence, which is the other half of the same bypass.
    """

    assert len(token) > _INT_PARSE_LIMIT
    echo = safe_token_echo(token)
    assert len(echo) < 64
    assert token not in echo
    message = f"NODE27_COLD_RESIDENCY_CONTAINER_EXEC_UID must be within, got {echo}"
    assert len(message.encode("utf-8")) < 256


def test_expected_mismatch_refuses_before_writable() -> None:
    actions = DockerActions()
    with pytest.raises(ColdRuntimeError, match="runtime identity drifted"):
        inspect_production_target(
            runner=_fake_docker(actions, config_user=f"{RUNTIME_UID}:{RUNTIME_GID}"),
            host_inspect=_cold_owner,
            expected_container_exec_uid=IMAGE_UID,
            expected_container_exec_gid=IMAGE_GID,
        )
    assert actions.commands == ["inspect"]


def test_untrusted_absolute_docker_bin_is_refused(tmp_path: Path) -> None:
    fake = tmp_path / "fake-docker"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    actions = DockerActions()
    with pytest.raises(ColdRuntimeError, match="trusted"):
        inspect_production_target(
            docker_bin=str(fake),
            host_inspect=_host,
            runner=_fake_docker(actions),
            **EXPECTED,
        )
    assert actions.calls == []


def test_writable_argv_builder_is_exact_and_numeric() -> None:
    assert container_writable_argv(uid=RUNTIME_UID, gid=RUNTIME_GID) == (
        "/usr/bin/docker",
        "exec",
        "--user",
        f"{RUNTIME_UID}:{RUNTIME_GID}",
        "nhms-db",
        "test",
        "-w",
        CONTAINER_COLD_PATH,
    )
    for kwargs in ({"uid": 0, "gid": RUNTIME_GID}, {"uid": RUNTIME_UID, "gid": -1}):
        with pytest.raises(ColdRuntimeError) as raised:
            container_writable_argv(**kwargs)
        assert raised.value.error_class == "config"


def test_missing_container_is_target_identity_error() -> None:
    def runner(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=1, stdout="", stderr="No such container")

    with pytest.raises(ColdRuntimeError, match="could not inspect"):
        inspect_production_target(runner=runner, host_inspect=_host, **EXPECTED)


def test_malformed_mount_json_is_refused() -> None:
    def runner(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout="{not-json", stderr="")

    with pytest.raises(ColdRuntimeError, match="malformed"):
        inspect_production_target(runner=runner, host_inspect=_host, **EXPECTED)


def test_mount_only_output_is_refused_as_malformed() -> None:
    """A legacy `.Mounts`-only projection cannot satisfy the new observation."""

    def runner(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps([{"Destination": CONTAINER_COLD_PATH, "Source": HOST_COLD_PATH}]),
            stderr="",
        )

    with pytest.raises(ColdRuntimeError, match="malformed"):
        inspect_production_target(runner=runner, host_inspect=_host, **EXPECTED)


def test_timeout_is_refused() -> None:
    def runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="docker", timeout=5)

    with pytest.raises(ColdRuntimeError, match="timed out"):
        inspect_production_target(runner=runner, host_inspect=_host, **EXPECTED)


def test_output_cap_is_enforced() -> None:
    huge = "x" * (INSPECT_OUTPUT_MAX_BYTES + 1)

    def runner(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout=huge, stderr="")

    with pytest.raises(ColdRuntimeError, match="byte ceiling"):
        inspect_production_target(runner=runner, host_inspect=_host, **EXPECTED)


def test_stderr_cap_is_enforced() -> None:
    huge = "e" * (INSPECT_OUTPUT_MAX_BYTES + 1)

    def runner(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=1, stdout="", stderr=huge)

    with pytest.raises(ColdRuntimeError, match="byte ceiling"):
        inspect_production_target(runner=runner, host_inspect=_host, **EXPECTED)


def test_mount_mismatch_is_refused() -> None:
    actions = DockerActions()
    with pytest.raises(ColdRuntimeError, match="bind source drifted"):
        inspect_production_target(
            runner=_fake_docker(actions, sources=("/tmp/wrong-cold",)),
            host_inspect=_host,
            **EXPECTED,
        )
    assert actions.commands == ["inspect"]


def test_two_cold_binds_are_refused() -> None:
    actions = DockerActions()
    with pytest.raises(ColdRuntimeError, match="exactly one cold tablespace bind"):
        inspect_container_identity_observation(
            runner=_fake_docker(actions, sources=(HOST_COLD_PATH, "/tmp/second-cold")),
        )


def test_path_swap_is_refused() -> None:
    def boom(_path: str) -> dict[str, int | str]:
        raise ColdRuntimeError(
            "target host path identity drifted during inspection",
            error_class="target_identity",
            stage="target_identity",
        )

    with pytest.raises(ColdRuntimeError, match="identity drifted"):
        inspect_production_target(runner=_fake_docker(DockerActions()), host_inspect=boom, **EXPECTED)


def test_production_inspect_target_does_not_echo_expected_values() -> None:
    # Observed and expected happen to agree at 1000:1000 here; `writable=True`
    # isolates payload carriage from the permission rule proven elsewhere.
    payload = production_inspect_target(
        runner=_fake_docker(
            DockerActions(),
            config_user=f"{IMAGE_UID}:{IMAGE_GID}",
            writable=True,
        ),
        host_inspect=_host,
        expected_host_path=HOST_COLD_PATH,
        expected_container_exec_uid=IMAGE_UID,
        expected_container_exec_gid=IMAGE_GID,
    )
    assert payload["device_identity"] == "8:11"
    assert payload["writable"] is True
    assert (payload["container_exec_uid"], payload["container_exec_gid"]) == (IMAGE_UID, IMAGE_GID)

    # The pair is the inspector's own read: the same expected values against an
    # observed 1005:1005 must refuse rather than echo.
    diverging = DockerActions()
    with pytest.raises(ColdRuntimeError, match="runtime identity drifted"):
        production_inspect_target(
            runner=_fake_docker(diverging, config_user=f"{RUNTIME_UID}:{RUNTIME_GID}"),
            host_inspect=_host,
            expected_container_exec_uid=IMAGE_UID,
            expected_container_exec_gid=IMAGE_GID,
        )
    assert diverging.commands == ["inspect"]


def test_observed_target_record_carries_the_pair() -> None:
    fields = set(ObservedTarget.__dataclass_fields__)
    assert {"container_exec_uid", "container_exec_gid"} <= fields


def test_bounded_collector_caps_real_stdout_child() -> None:
    started = time.monotonic()
    with pytest.raises(ColdRuntimeError, match="byte ceiling"):
        run_bounded_command(
            [sys.executable, "-c", "import sys; sys.stdout.write('x' * 200000); sys.stdout.flush()"],
            timeout=5,
        )
    assert time.monotonic() - started < 4


def test_bounded_collector_caps_real_stderr_child() -> None:
    started = time.monotonic()
    with pytest.raises(ColdRuntimeError, match="byte ceiling"):
        run_bounded_command(
            [sys.executable, "-c", "import sys; sys.stderr.write('e' * 200000); sys.stderr.flush()"],
            timeout=5,
        )
    assert time.monotonic() - started < 4


def test_bounded_collector_kills_hanging_child() -> None:
    started = time.monotonic()
    with pytest.raises(ColdRuntimeError, match="timed out"):
        run_bounded_command([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)
    assert time.monotonic() - started < 4


def test_no_name_based_probe_survives_the_module() -> None:
    import packages.common.compressed_chunk_cold_target as target

    source = Path(target.__file__).read_text(encoding="utf-8")
    assert '"postgres"' not in source
    assert "CONTAINER_WRITABLE_ARGV" not in source
    assert "inspect_nhms_db_cold_bind" not in source


def test_host_identity_drift_after_writable_check_is_refused() -> None:
    seen = {"count": 0}

    def drifting(_path: str) -> dict[str, int | str]:
        seen["count"] += 1
        if seen["count"] == 1:
            return {"device_identity": "8:11", "mode": 0o555, "uid": 999, "gid": 999}
        return {"device_identity": "8:12", "mode": 0o555, "uid": 999, "gid": 999}

    actions = DockerActions()
    with pytest.raises(ColdRuntimeError, match="identity drifted after writable check"):
        inspect_production_target(
            runner=_fake_docker(actions, writable=True),
            host_inspect=drifting,
            **EXPECTED,
        )
    assert seen["count"] == 2
    # Accepted TOCTOU fence: one Config.User read, never a second one afterwards.
    assert actions.commands == ["inspect", "exec"]
