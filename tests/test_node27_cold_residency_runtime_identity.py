"""Issue #1929: required numeric container runtime identity for cold residency.

Covers the three refusal layers the fixture separates, and the one evidence
claim that ties them together:

1. env/CLI config — mandatory, pre-connection, both modes;
2. direct Python ``RuntimeConfig`` identity — including the bool and half-pair
   cases a shell cannot express;
3. observed ``Config.User`` / injected-inspector identity — mismatch refuses
   before any writable probe or movement SQL.

The discriminating value pair is image ``postgres=1000:1000`` versus expected +
observed runtime ``1005:1005`` on an owner-matched mode-0700 cold path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from packages.common.compressed_chunk_cold_receipt import (
    SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    publish_receipt,
    validate_receipt,
)
from packages.common.compressed_chunk_cold_runtime import (
    RuntimeConfig,
    TargetIdentity,
    preflight_target_identity,
    require_runtime_exec_identity,
)
from packages.common.compressed_chunk_cold_runtime_catalog import ColdRuntimeError
from packages.common.compressed_chunk_cold_target import CONTAINER_EXEC_ID_MAX
from packages.common.compressed_chunk_cold_tick import runtime_config, target_payload
from scripts import node27_cold_residency as runner
from tests.cold_residency_fakes import (
    ENV_CONTAINER_EXEC_GID,
    ENV_CONTAINER_EXEC_UID,
    IMAGE_DEFAULT_EXEC_GID,
    IMAGE_DEFAULT_EXEC_UID,
    RUNTIME_EXEC_GID,
    RUNTIME_EXEC_UID,
    FakeConnection,
    bound_inventories,
    chunk,
    complete_relations,
    expected_exec_identity,
    required_exec_env,
    target_observation,
)
from tests.test_node27_cold_residency import _NOW, _args, _base_env, _connect_factory, _ready

_ROOT = Path(__file__).resolve().parents[1]
_TERMINAL = json.loads((_ROOT / "schemas/examples/timeseries_cold_residency_receipt.example.json").read_text())
# CPython 3.11+ refuses int() beyond this many digits and raises a bare ValueError
# with no error_class/stage. A config token of this width is the boundary case: it
# passes a canonical-digit regex, so only the bounded parse can refuse it as config.
_INT_PARSE_LIMIT = sys.get_int_max_str_digits()
_OVER_WIDTH = "9" * (_INT_PARSE_LIMIT + 700)


# --- layer 1: env / CLI config -------------------------------------------------


@pytest.mark.parametrize("enforce", [False, True])
@pytest.mark.parametrize(
    ("uid", "gid", "expected_key"),
    [
        (None, "1005", ENV_CONTAINER_EXEC_UID),
        ("1005", None, ENV_CONTAINER_EXEC_GID),
        ("", "1005", ENV_CONTAINER_EXEC_UID),
        ("1005", "", ENV_CONTAINER_EXEC_GID),
        (" 1005", "1005", ENV_CONTAINER_EXEC_UID),
        ("1005 ", "1005", ENV_CONTAINER_EXEC_UID),
        ("  1005  ", "  1005  ", ENV_CONTAINER_EXEC_UID),
        ("+1005", "+1005", ENV_CONTAINER_EXEC_UID),
        ("-1005", "-1005", ENV_CONTAINER_EXEC_UID),
        ("0", "0", ENV_CONTAINER_EXEC_UID),
        ("1005", "0", ENV_CONTAINER_EXEC_GID),
        ("postgres", "postgres", ENV_CONTAINER_EXEC_UID),
        ("nwm", "nwm", ENV_CONTAINER_EXEC_UID),
        ("1005:1005", "1005:1005", ENV_CONTAINER_EXEC_UID),
        ("01005", "01005", ENV_CONTAINER_EXEC_UID),
        ("1005_0", "1005_0", ENV_CONTAINER_EXEC_UID),
        ("1e3", "1e3", ENV_CONTAINER_EXEC_UID),
        ("1005.0", "1005.0", ENV_CONTAINER_EXEC_UID),
        (str(CONTAINER_EXEC_ID_MAX + 1), "1005", ENV_CONTAINER_EXEC_UID),
        ("1005", str(CONTAINER_EXEC_ID_MAX + 1), ENV_CONTAINER_EXEC_GID),
        (str(2**64), str(2**64), ENV_CONTAINER_EXEC_UID),
        # 5000 canonical digits: the regex passes, an unguarded int() would raise
        # a bare ValueError, and the refusal must still be typed config/pre-connect.
        pytest.param(_OVER_WIDTH, "1005", ENV_CONTAINER_EXEC_UID, id="over-width-uid"),
        pytest.param("1005", _OVER_WIDTH, ENV_CONTAINER_EXEC_GID, id="over-width-gid"),
    ],
)
def test_container_exec_env_matrix_refuses_pre_connect(
    tmp_path: Path,
    enforce: bool,
    uid: str | None,
    gid: str | None,
    expected_key: str,
) -> None:
    env = _base_env(tmp_path, override={ENV_CONTAINER_EXEC_UID: uid, ENV_CONTAINER_EXEC_GID: gid})
    with pytest.raises(runner.ColdResidencyConfigError) as raised:
        runner.config_from_args(_args(enforce=enforce), env)
    assert expected_key in str(raised.value)
    assert raised.value.error_class == "config"
    assert raised.value.stage == "config"


@pytest.mark.parametrize("enforce", [False, True])
@pytest.mark.parametrize(
    ("uid", "gid"),
    [("1", "1"), ("1005", "1005"), (str(CONTAINER_EXEC_ID_MAX), str(CONTAINER_EXEC_ID_MAX))],
)
def test_container_exec_env_accepts_the_full_non_root_domain(
    tmp_path: Path,
    enforce: bool,
    uid: str,
    gid: str,
) -> None:
    env = _base_env(tmp_path, override={ENV_CONTAINER_EXEC_UID: uid, ENV_CONTAINER_EXEC_GID: gid})
    config = runner.config_from_args(_args(enforce=enforce), env)
    assert config.expected_container_exec_uid == int(uid)
    assert config.expected_container_exec_gid == int(gid)


def test_container_exec_keys_are_required_before_database_url(tmp_path: Path) -> None:
    """A missing principal refuses even when the DSN is also missing: no path
    reaches a connection or a probe with an unspecified identity."""

    env = _base_env(tmp_path, override={"DATABASE_URL": None, ENV_CONTAINER_EXEC_UID: None})
    with pytest.raises(runner.ColdResidencyConfigError, match=ENV_CONTAINER_EXEC_UID):
        runner.config_from_args(_args(), env)


@pytest.mark.parametrize("enforce", [False, True])
@pytest.mark.parametrize("component", [ENV_CONTAINER_EXEC_UID, ENV_CONTAINER_EXEC_GID])
def test_over_width_exec_id_refuses_as_config_before_database_url(
    tmp_path: Path,
    enforce: bool,
    component: str,
) -> None:
    """A 5000-digit token is a config refusal, never an interpreter error.

    `int('9' * 5000)` raises `ValueError: Exceeds the limit (4300 digits)` on
    CPython 3.11+, which is not a `ColdResidencyConfigError`: it carries no
    error_class/stage, so `main()` would not publish the schema-valid config
    tombstone and the refusal would leave no receipt evidence. The parse must
    reject by width first, and the refusal must not interpolate the unbounded
    token — `error.reason` is capped at 256 characters by the shipping schema.
    """

    other = ENV_CONTAINER_EXEC_GID if component == ENV_CONTAINER_EXEC_UID else ENV_CONTAINER_EXEC_UID
    env = _base_env(
        tmp_path,
        # The DSN is also absent: the identity parse must precede any connection.
        override={"DATABASE_URL": None, component: _OVER_WIDTH, other: str(RUNTIME_EXEC_UID)},
    )
    with pytest.raises(runner.ColdResidencyConfigError) as raised:
        runner.config_from_args(_args(enforce=enforce), env)
    error = raised.value
    assert error.error_class == "config"
    assert error.stage == "config"
    assert component in str(error)
    assert _OVER_WIDTH not in str(error)
    assert "Exceeds the limit" not in str(error)
    assert len(str(error).encode("utf-8")) < 256


def test_no_postgres_or_image_default_survives_the_source() -> None:
    """Static guard for the three fallback shapes the fixture forbids."""

    for relative in (
        "packages/common/compressed_chunk_cold_target.py",
        "packages/common/compressed_chunk_cold_runtime.py",
        "packages/common/compressed_chunk_cold_runtime_target.py",
        "packages/common/compressed_chunk_cold_tick.py",
        "scripts/node27_cold_residency.py",
    ):
        text = (_ROOT / relative).read_text(encoding="utf-8")
        assert '"postgres"' not in text, relative
        assert "CONTAINER_WRITABLE_ARGV" not in text, relative

    script = (_ROOT / "scripts/node27_cold_residency.py").read_text(encoding="utf-8")
    # 1. both keys are parsed by the *required* parser — a function with no
    #    `default` parameter — and never by an `or`-fallback.
    assert "_parse_container_exec_id(env, _ENV_CONTAINER_EXEC_UID)" in script
    assert "_parse_container_exec_id(env, _ENV_CONTAINER_EXEC_GID)" in script
    # The parser signature takes only (env, name): no default parameter exists.
    assert "def _parse_container_exec_id(env: Mapping[str, str], name: str) -> int:" in script
    assert f'"{ENV_CONTAINER_EXEC_UID}" or' not in script
    assert f'"{ENV_CONTAINER_EXEC_GID}" or' not in script
    assert f"str({IMAGE_DEFAULT_EXEC_UID})" not in script
    # 2. RunnerConfig declares both fields WITHOUT a default, so the CLI cannot
    #    construct a config that omits them.
    assert "expected_container_exec_uid: int\n" in script
    assert "expected_container_exec_gid: int\n" in script
    assert "expected_container_exec_uid: int = " not in script
    # 3. the only place a value defaults is RuntimeConfig, where the default is
    #    the provably-invalid marker 0, never an accepted principal. The owner is
    #    compressed_chunk_cold_runtime_target; the movement module must not grow a
    #    second definition.
    owner = (_ROOT / "packages/common/compressed_chunk_cold_runtime_target.py").read_text(encoding="utf-8")
    assert "expected_container_exec_uid: int = 0" in owner
    assert "expected_container_exec_gid: int = 0" in owner
    assert "require_runtime_exec_identity" in owner
    runtime = (_ROOT / "packages/common/compressed_chunk_cold_runtime.py").read_text(encoding="utf-8")
    assert "class RuntimeConfig" not in runtime
    assert "class TargetIdentity" not in runtime
    assert "def preflight_target_identity" not in runtime
    assert "def require_runtime_exec_identity" not in runtime
    assert "expected_container_exec_uid: int = 0" not in runtime


def test_env_example_exposes_both_keys_unassigned() -> None:
    text = (_ROOT / "infra/env/node27-cold-residency.example").read_text(encoding="utf-8")
    for name in (ENV_CONTAINER_EXEC_UID, ENV_CONTAINER_EXEC_GID):
        assert f"#{name}=" in text, name
        assigned = [
            line for line in text.splitlines() if line.startswith(f"{name}=") and line.split("=", 1)[1].strip()
        ]
        assert assigned == [], f"{name} must stay unassigned in the public template"
    assert "1895" in text
    assert "0600" in text


def test_over_width_exec_id_main_refusal_publishes_and_never_connects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero connection attempts on the over-width config path.

    The DSN is present here, so a refusal that only reported a typed error class
    would still be worthless if anything reached the driver. psycopg2 is imported
    lazily inside the connect seam, so blocking it in sys.modules makes "no
    connection" observable rather than assumed.
    """

    calls = {"attempts": 0}

    class Blocked:
        def __getattr__(self, _name: str):
            calls["attempts"] += 1
            raise AssertionError("config refusal must not import psycopg2")

    monkeypatch.setitem(sys.modules, "psycopg2", Blocked())  # type: ignore[arg-type]
    monkeypatch.setitem(sys.modules, "psycopg2.extras", Blocked())  # type: ignore[arg-type]
    env = _base_env(tmp_path, override={ENV_CONTAINER_EXEC_UID: _OVER_WIDTH})
    for key, item in env.items():
        monkeypatch.setenv(key, item)
    assert runner.main([]) == 1
    assert calls["attempts"] == 0
    current = json.loads(Path(env["NODE27_COLD_RESIDENCY_RECEIPT_PATH"]).read_text(encoding="utf-8"))
    assert current["outcome"] == "refused_config"
    assert current["error"]["class"] == "config"
    assert current["error"]["stage"] == "config"
    validate_receipt(current)


@pytest.mark.parametrize(
    ("component", "value"),
    [
        pytest.param(ENV_CONTAINER_EXEC_UID, "postgres", id="name"),
        pytest.param(ENV_CONTAINER_EXEC_UID, _OVER_WIDTH, id="over-width-uid"),
        pytest.param(ENV_CONTAINER_EXEC_GID, _OVER_WIDTH, id="over-width-gid"),
        # Non-canonical AND over-width: the other message branch on the same path.
        pytest.param(ENV_CONTAINER_EXEC_UID, "x" * 5000, id="over-width-non-canonical"),
        pytest.param(ENV_CONTAINER_EXEC_GID, " " + _OVER_WIDTH, id="over-width-whitespace"),
    ],
)
def test_runtime_identity_refusal_keeps_safe_tombstone_and_redaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    component: str,
    value: str,
) -> None:
    env = _base_env(tmp_path, override={component: value})
    for key, item in env.items():
        monkeypatch.setenv(key, item)
    receipt_path = Path(env["NODE27_COLD_RESIDENCY_RECEIPT_PATH"])
    publish_receipt(receipt_path, _TERMINAL)
    assert runner.main([]) == 1
    current = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert current["outcome"] == "refused_config"
    assert current["config_observed"] is False
    assert current["target"]["observed"] is False
    assert current["target"]["container_exec_uid"] is None
    assert current["target"]["container_exec_gid"] is None
    # The refusal is only evidence if it is publishable: the reason must stay a
    # schema-legal, config-classified string that never echoes the raw token.
    assert current["error"]["class"] == "config"
    assert current["error"]["stage"] == "config"
    # A short name is quoted back so the operator can see the typo; an over-width
    # token is summarized by width, because `error.reason` is capped at 256
    # characters by the shipping schema and must stay publishable either way.
    assert len(current["error"]["reason"]) <= 256
    assert _OVER_WIDTH not in current["error"]["reason"]
    assert _OVER_WIDTH not in json.dumps(current)
    validate_receipt(current)
    assert "secretpw" not in capsys.readouterr().err


# --- layer 2: direct Python RuntimeConfig identity -----------------------------


@pytest.mark.parametrize(
    ("uid", "gid", "reason"),
    [
        (0, 0, "dataclass default / root"),
        (RUNTIME_EXEC_UID, 0, "gid missing or root"),
        (0, RUNTIME_EXEC_GID, "uid missing or root"),
        (True, RUNTIME_EXEC_GID, "bool despite int subclass"),
        (RUNTIME_EXEC_UID, False, "bool gid"),
        (CONTAINER_EXEC_ID_MAX + 1, RUNTIME_EXEC_GID, "above bound"),
        (-1, -1, "negative"),
    ],
)
def test_runtime_config_matrix_refuses_before_any_connection(
    tmp_path: Path,
    uid: object,
    gid: object,
    reason: str,
) -> None:
    del reason
    base = _ready(runner.config_from_args(_args(), _base_env(tmp_path)))
    config = base.__class__(
        **{**base.__dict__, "expected_container_exec_uid": uid, "expected_container_exec_gid": gid}
    )
    calls = {"connect": 0}

    def connect() -> FakeConnection:
        calls["connect"] += 1
        raise AssertionError("run_tick must not open an observer connection")

    with pytest.raises(Exception) as raised:
        runner.run_tick(
            config,
            now_utc=_NOW,
            head_sha="a" * 40,
            connect=connect,  # type: ignore[arg-type]
            fetch_watermark=lambda: _NOW,
        )
    assert calls["connect"] == 0
    assert getattr(raised.value, "error_class", "") == "config"


def test_runtime_config_helper_validates_the_pair_directly() -> None:
    assert require_runtime_exec_identity(RuntimeConfig(**expected_exec_identity())) == (
        RUNTIME_EXEC_UID,
        RUNTIME_EXEC_GID,
    )
    with pytest.raises(ColdRuntimeError) as raised:
        require_runtime_exec_identity(RuntimeConfig())
    assert raised.value.error_class == "config"
    assert raised.value.stage == "config"


def test_target_preflight_owner_reexports_are_the_same_objects() -> None:
    """The #1929 preflight contract moved to its own owner; import compatibility
    must be a genuine re-export, not a wrapper or a second definition."""

    import packages.common.compressed_chunk_cold_runtime as movement
    import packages.common.compressed_chunk_cold_runtime_target as owner

    for name in (
        "RuntimeConfig",
        "TargetIdentity",
        "preflight_target_identity",
        "require_runtime_exec_identity",
    ):
        assert getattr(movement, name) is getattr(owner, name), name

    # A RuntimeConfig built through either import path is one type, and the tick
    # propagation helper still feeds the preflight gate.
    from packages.common.compressed_chunk_cold_tick import runtime_config

    built = owner.RuntimeConfig(**expected_exec_identity())
    assert isinstance(built, movement.RuntimeConfig)
    assert require_runtime_exec_identity(built) == (RUNTIME_EXEC_UID, RUNTIME_EXEC_GID)
    assert runtime_config.__module__ == "packages.common.compressed_chunk_cold_tick"


def test_migration_entrypoint_refuses_invalid_identity_before_connecting() -> None:
    """A caller that builds RuntimeConfig by hand cannot reach SQL."""

    from packages.common.compressed_chunk_cold_runtime import migrate_residency_group

    connection = FakeConnection()
    connection.load_group(chunk(), complete_relations())

    def connect() -> FakeConnection:
        raise AssertionError("must not connect with an invalid expected principal")

    with pytest.raises(ColdRuntimeError) as raised:
        migrate_residency_group(
            connect=connect,  # type: ignore[arg-type]
            chunk=chunk(),
            inventories=bound_inventories(),
            watermark=_NOW,
            lag_seconds=604800,
            cold_free_bytes=10_000,
            hot_free_bytes=10_000,
            cold_reserve_bytes=100,
            wal_reserve_bytes=1,
            config=RuntimeConfig(inspect_target=lambda: target_observation()),
        )
    assert raised.value.error_class == "config"
    assert connection.executed == []


def test_runtime_config_propagates_the_pair_from_cli_config(tmp_path: Path) -> None:
    config = runner.config_from_args(_args(enforce=True), _base_env(tmp_path))
    runtime = runtime_config(config)
    assert runtime.expected_container_exec_uid == RUNTIME_EXEC_UID
    assert runtime.expected_container_exec_gid == RUNTIME_EXEC_GID


# --- layer 3: observed identity -------------------------------------------------


def _execute(connection: FakeConnection):
    return lambda sql, params=None: connection.dispatch(sql, params)[0]


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            {
                key: value
                for key, value in target_observation().items()
                if key not in ("container_exec_uid", "container_exec_gid")
            },
            id="both-fields-omitted",
        ),
        pytest.param(
            {**target_observation(), "container_exec_gid": None},
            id="null-gid",
        ),
        pytest.param(
            {**target_observation(), "container_exec_uid": True},
            id="bool-uid",
        ),
        pytest.param(
            {**target_observation(), "container_exec_uid": 0, "container_exec_gid": 0},
            id="root-pair",
        ),
        pytest.param(
            {**target_observation(), "container_exec_uid": CONTAINER_EXEC_ID_MAX + 1},
            id="above-bound",
        ),
        pytest.param(
            {**target_observation(), "container_exec_uid": "1005", "container_exec_gid": "1005"},
            id="string-pair",
        ),
    ],
)
def test_injected_inspector_identity_is_strictly_validated(payload: dict[str, object]) -> None:
    connection = FakeConnection()
    with pytest.raises(ColdRuntimeError) as raised:
        preflight_target_identity(
            _execute(connection),
            RuntimeConfig(inspect_target=lambda: dict(payload), **expected_exec_identity()),
        )
    assert raised.value.error_class == "target_identity"
    assert connection.executed == []


def test_preflight_never_echoes_expected_identity_when_inspector_is_silent() -> None:
    """The pre-fix inspector payload (no identity fields) must fail, not pass."""

    connection = FakeConnection()
    legacy = {
        "container_name": "nhms-db",
        "container_bind": "/data/GHDC/nhms-cold-tablespace",
        "host_path": "/data/GHDC/nhms-cold-tablespace",
        "device_identity": "8:1",
    }
    with pytest.raises(ColdRuntimeError, match="did not observe container_exec_uid"):
        preflight_target_identity(
            _execute(connection),
            RuntimeConfig(inspect_target=lambda: dict(legacy), **expected_exec_identity()),
        )
    # No catalog query ran: the identity gate precedes the first SQL.
    assert connection.executed == []


@pytest.mark.parametrize(
    ("observed_uid", "observed_gid", "refusal"),
    [
        # A legitimate but different principal: equality fails.
        (IMAGE_DEFAULT_EXEC_UID, IMAGE_DEFAULT_EXEC_GID, "identity drifted"),
        (RUNTIME_EXEC_UID, IMAGE_DEFAULT_EXEC_GID, "identity drifted"),
        # Root and above-bound are not identities at all: strict validation
        # fails first, so they can never satisfy equality.
        (0, 0, "out-of-range"),
        (CONTAINER_EXEC_ID_MAX + 1, RUNTIME_EXEC_GID, "out-of-range"),
    ],
)
def test_observed_mismatch_refuses_before_any_catalog_sql(
    observed_uid: int,
    observed_gid: int,
    refusal: str,
) -> None:
    connection = FakeConnection()
    with pytest.raises(ColdRuntimeError, match=refusal) as raised:
        preflight_target_identity(
            _execute(connection),
            RuntimeConfig(
                inspect_target=lambda: target_observation(
                    container_exec_uid=observed_uid,
                    container_exec_gid=observed_gid,
                ),
                **expected_exec_identity(),
            ),
        )
    assert raised.value.stage == "target_identity"
    assert connection.executed == []


def test_matching_identity_preflights_and_returns_the_observed_pair() -> None:
    connection = FakeConnection()
    identity = preflight_target_identity(
        _execute(connection),
        RuntimeConfig(inspect_target=lambda: target_observation(), **expected_exec_identity()),
    )
    assert (identity.container_exec_uid, identity.container_exec_gid) == (
        RUNTIME_EXEC_UID,
        RUNTIME_EXEC_GID,
    )
    assert target_payload(identity)["container_exec_uid"] == RUNTIME_EXEC_UID


def test_target_identity_record_requires_the_pair() -> None:
    with pytest.raises(TypeError):
        TargetIdentity(
            catalog_name="nhms_cold",
            catalog_location="/x",
            container_bind="/y",
            host_path="/y",
            device_identity="8:1",
        )


# --- end-to-end dry-run / enforce ----------------------------------------------


@pytest.mark.parametrize("enforce", [False, True])
def test_preflight_succeeds_for_matching_numeric_identity_and_is_recorded(
    tmp_path: Path,
    enforce: bool,
) -> None:
    config = _ready(
        runner.config_from_args(
            _args(enforce=enforce),
            _base_env(tmp_path, override={"NODE27_COLD_RESIDENCY_PER_TICK_BOUND": "1"}),
        )
    )
    connection = FakeConnection()
    connection.load_group(chunk(), complete_relations(origin_space="nhms_cold"))
    receipt = runner.run_tick(
        config,
        now_utc=_NOW,
        head_sha="a" * 40,
        connect=_connect_factory(connection),
        fetch_watermark=lambda: _NOW,
    )
    validate_receipt(receipt)
    assert receipt["schema_version"] == SCHEMA_VERSION == "1.1"
    assert receipt["target"]["observed"] is True
    assert receipt["target"]["container_exec_uid"] == RUNTIME_EXEC_UID
    assert receipt["target"]["container_exec_gid"] == RUNTIME_EXEC_GID
    # Already-cold group: proof of preflight success with zero movement either way.
    assert not any("SET TABLESPACE" in sql for sql, _params in connection.executed)


@pytest.mark.parametrize("enforce", [False, True])
@pytest.mark.parametrize(
    ("observed_uid", "observed_gid"),
    [
        (IMAGE_DEFAULT_EXEC_UID, IMAGE_DEFAULT_EXEC_GID),
        (RUNTIME_EXEC_UID, IMAGE_DEFAULT_EXEC_GID),
        (0, 0),
        (CONTAINER_EXEC_ID_MAX + 1, RUNTIME_EXEC_UID),
    ],
)
def test_mismatch_is_a_truthful_failure_with_zero_movement(
    tmp_path: Path,
    enforce: bool,
    observed_uid: int,
    observed_gid: int,
) -> None:
    base = _ready(runner.config_from_args(_args(enforce=enforce), _base_env(tmp_path)))
    config = base.__class__(
        **{
            **base.__dict__,
            "inspect_target": lambda: target_observation(
                container_exec_uid=observed_uid,
                container_exec_gid=observed_gid,
            ),
        }
    )
    connection = FakeConnection()
    connection.load_group(chunk(), complete_relations())
    with pytest.raises(Exception) as raised:
        runner.run_tick(
            config,
            now_utc=_NOW,
            head_sha="a" * 40,
            connect=_connect_factory(connection),
            fetch_watermark=lambda: _NOW,
        )
    assert getattr(raised.value, "error_class", "") == "target_identity"
    assert not any("SET TABLESPACE" in sql for sql, _params in connection.executed)


def test_enforce_movement_still_proceeds_with_matching_numeric_identity(tmp_path: Path) -> None:
    config = _ready(
        runner.config_from_args(
            _args(enforce=True),
            _base_env(tmp_path, override={"NODE27_COLD_RESIDENCY_PER_TICK_BOUND": "1"}),
        )
    )
    connection = FakeConnection()
    connection.load_group(chunk(), complete_relations())
    receipt = runner.run_tick(
        config,
        now_utc=_NOW,
        head_sha="a" * 40,
        connect=_connect_factory(connection),
        fetch_watermark=lambda: _NOW,
    )
    assert receipt["outcome"] == "clean"
    assert receipt["target"]["container_exec_uid"] == RUNTIME_EXEC_UID
    assert any("SET TABLESPACE" in sql for sql, _params in connection.executed)


def test_required_env_keys_are_documented_in_the_runbook() -> None:
    runbook = (_ROOT / "docs/runbooks/tier-node27-timeseries-storage.md").read_text(encoding="utf-8")
    assert ENV_CONTAINER_EXEC_UID in runbook
    assert ENV_CONTAINER_EXEC_GID in runbook
    assert "Config.User" in runbook
    assert "1929" in runbook
    # The width rule is operator-visible behaviour, not an implementation detail:
    # a too-long token refuses as config before conversion and is never echoed.
    assert "refused by width" in runbook
    assert "4300-digit" in runbook
    assert "256 characters" in runbook


def test_runbook_documents_the_exact_production_inspect_format() -> None:
    """Operators copy this command verbatim, so a paraphrased template is a
    wrong procedure, not just stale prose. It must equal the constant the runner
    actually executes, and the sprig-only form must not appear at all."""

    from packages.common.compressed_chunk_cold_target import INSPECT_FORMAT

    runbook = (_ROOT / "docs/runbooks/tier-node27-timeseries-storage.md").read_text(encoding="utf-8")
    assert f"--format '{INSPECT_FORMAT}'" in runbook
    assert "{{json (dict" not in runbook


def test_writer_and_reader_version_sets_are_distinct_by_design() -> None:
    assert SCHEMA_VERSION == "1.1"
    assert SUPPORTED_SCHEMA_VERSIONS == ("1.0", "1.1")
    assert required_exec_env()[ENV_CONTAINER_EXEC_UID] == str(RUNTIME_EXEC_UID)
