"""Receipt schema 1.0/1.1 compatibility for Issue #1929.

#1893 shipped `1.0` terminal receipts and authoritative intent sidecars that are
still live recovery authority. #1929 adds evidence, so:

* the writer and all shipping examples emit `1.1`;
* the shipping schema and every reader accept the union {`1.0`, `1.1`} — a
  const-`1.1` schema would strand existing authority and is invalid by
  requirement, not just inconvenient;
* `1.0` target objects OMIT the principal fields (back-filling them would
  invent evidence);
* `1.1` target objects always CARRY both fields — non-root integers when
  observed, present nulls when not, never an echo of expected config.

The historical documents below are produced by downgrading a current document
(version + deleting the two fields), which is exactly the pre-#1929 shape.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from packages.common.compressed_chunk_cold_receipt import (
    SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    intent_path_for,
    load_receipt_schema,
    publish_intent,
    publish_receipt,
    read_intent,
    read_public_receipt,
    read_public_receipt_durable,
    remove_intent,
    validate_receipt,
)
from tests.cold_residency_fakes import FakeConnection

_ROOT = Path(__file__).resolve().parents[1]
_EXAMPLES = Path(_ROOT / "schemas/examples")
_BASE_NAME = "timeseries_cold_residency_receipt"
_EXAMPLE_SUFFIXES = ("", ".noop", ".intent", ".partial", ".error")
_EXEC_FIELDS = ("container_exec_uid", "container_exec_gid")
_NOW = datetime(2026, 8, 1, 4, 25, tzinfo=UTC)


def _terminal() -> dict[str, Any]:
    return json.loads((_EXAMPLES / f"{_BASE_NAME}.example.json").read_text(encoding="utf-8"))


def _intent() -> dict[str, Any]:
    return json.loads((_EXAMPLES / f"{_BASE_NAME}.intent.example.json").read_text(encoding="utf-8"))


def _as_historical(document: dict[str, Any]) -> dict[str, Any]:
    """Downgrade a current document to the pre-#1929 (`1.0`) evidence shape."""

    historical = copy.deepcopy(document)
    historical["schema_version"] = "1.0"
    for field in _EXEC_FIELDS:
        assert field in historical["target"], "shipping example must carry the field to downgrade"
        historical["target"].pop(field)
    return historical


# --- writer and examples -------------------------------------------------------


def test_writer_emits_only_the_current_version() -> None:
    assert SCHEMA_VERSION == "1.1"
    assert SUPPORTED_SCHEMA_VERSIONS == ("1.0", "1.1")


@pytest.mark.parametrize("suffix", _EXAMPLE_SUFFIXES)
def test_every_shipping_example_is_1_1_with_observed_numeric_principal(suffix: str) -> None:
    name = _BASE_NAME if suffix == "" else f"{_BASE_NAME}{suffix}"
    document = json.loads((_EXAMPLES / f"{name}.example.json").read_text(encoding="utf-8"))
    assert document["schema_version"] == "1.1"
    assert document["target"]["observed"] is True
    # 1005:1005 is the discriminating runtime value, never the 1000:1000 image
    # default, so an example cannot be satisfied by an image-name regression.
    assert document["target"]["container_exec_uid"] == 1005
    assert document["target"]["container_exec_gid"] == 1005
    validate_receipt(document)


def test_cli_and_receipt_writer_versions_agree() -> None:
    from scripts import node27_cold_residency as runner

    assert runner.SCHEMA_VERSION == SCHEMA_VERSION


def test_schema_version_property_accepts_exactly_the_union() -> None:
    schema = load_receipt_schema()
    version = schema["properties"]["schema_version"]
    assert version == {"enum": ["1.0", "1.1"]}
    assert "const" not in version


# --- historical 1.0 remains readable ------------------------------------------


@pytest.mark.parametrize("kind", ["terminal", "intent"])
def test_historical_1_0_document_validates(kind: str) -> None:
    document = _as_historical(_terminal() if kind == "terminal" else _intent())
    assert document["schema_version"] == "1.0"
    assert not any(field in document["target"] for field in _EXEC_FIELDS)
    validate_receipt(document)


@pytest.mark.parametrize("kind", ["terminal", "intent"])
def test_historical_1_0_recovery_apis_stay_usable(tmp_path: Path, kind: str) -> None:
    """A pre-#1929 sidecar and its public receipt still publish, read, close."""

    receipt_path = tmp_path / "receipt.json"
    sidecar = intent_path_for(receipt_path)
    historical_intent = _as_historical(_intent())
    historical_terminal = _as_historical(_terminal())
    publish_intent(sidecar, historical_intent)
    publish_receipt(receipt_path, historical_intent)
    assert read_intent(sidecar)["schema_version"] == "1.0"
    assert read_public_receipt(receipt_path)["schema_version"] == "1.0"
    assert read_public_receipt_durable(receipt_path)["schema_version"] == "1.0"
    publish_receipt(receipt_path, historical_terminal)
    remove_intent(sidecar)
    closed = read_public_receipt_durable(receipt_path)
    assert closed["outcome"] == "clean"
    assert closed["recovery"]["authority"] == "closed"
    assert not any(field in closed["target"] for field in _EXEC_FIELDS)


def test_new_run_never_emits_1_0(tmp_path: Path) -> None:
    from packages.common.compressed_chunk_cold_receipt import build_receipt, unavailable_target

    receipt = build_receipt(
        mode="dry-run",
        outcome="no_op",
        state="idle",
        head_sha="a" * 40,
        generated_at=_NOW,
        watermark="2026-08-01T04:25:00Z",
        lag_seconds=604800,
        cutoff="2026-07-25T04:25:00Z",
        per_tick_bound=1,
        max_members=64,
        budget={
            "statement_timeout_ms": 3600000,
            "wrapper_wall_seconds": 3901,
            "compression_wrapper_wall_seconds": 3900,
            "systemd_wall_seconds": 7842,
            "cleanup_margin_seconds": 300,
            "systemd_margin_seconds": 40,
        },
        cluster={
            "server_version": "15.2",
            "timescaledb_version": "2.10.2",
            "application_name": "nhms-ts-cold-residency",
            "observed": True,
        },
        target=unavailable_target(),
        inventory={"digest": None, "hypertables": None, "observed": False},
        capacity=None,
        selected=[],
        deferred=[],
        skipped=[],
        config_observed=True,
    )
    published = publish_receipt(tmp_path / "receipt.json", receipt)
    assert published["schema_version"] == "1.1"
    assert read_public_receipt_durable(tmp_path / "receipt.json")["schema_version"] == "1.1"


# --- 1.1 target semantics: observed integers, unobserved nulls ----------------


def test_1_1_tombstone_carries_present_nulls() -> None:
    from packages.common.compressed_chunk_cold_receipt import unavailable_target

    target = unavailable_target()
    assert target["observed"] is False
    assert all(field in target for field in _EXEC_FIELDS)
    assert all(target[field] is None for field in _EXEC_FIELDS)
    tombstone = copy.deepcopy(_terminal())
    tombstone["outcome"] = "refused_config"
    tombstone["state"] = "idle"
    tombstone["error"] = {"class": "config", "stage": "config", "reason": "identity not configured"}
    tombstone["config_observed"] = False
    tombstone["budget"] = None
    tombstone["lag_seconds"] = None
    tombstone["per_tick_bound"] = None
    tombstone["max_members"] = None
    tombstone["target"] = target
    tombstone["inventory"] = {"digest": None, "hypertables": None, "observed": False}
    tombstone["cluster"] = {
        "server_version": None,
        "timescaledb_version": None,
        "application_name": "nhms-ts-cold-residency",
        "observed": False,
    }
    tombstone["selected"] = []
    tombstone["deferred"] = []
    tombstone["skipped"] = []
    tombstone.pop("capacity", None)
    tombstone.pop("watermark", None)
    tombstone.pop("cutoff", None)
    tombstone.pop("recovery", None)
    validate_receipt(tombstone)


# --- mutants -------------------------------------------------------------------


def _observed_mutant() -> dict[str, Any]:
    document = copy.deepcopy(_terminal())
    assert document["target"]["observed"] is True
    return document


@pytest.mark.parametrize(
    ("field", "value", "why"),
    [
        ("__omit_both__", None, "1.1 observed target must carry both fields"),
        ("container_exec_uid", "__omit__", "half pair is not evidence"),
        ("container_exec_gid", "__omit__", "half pair is not evidence"),
        ("container_exec_uid", None, "observed target must not be a null"),
        ("container_exec_gid", None, "observed target must not be a null"),
        ("container_exec_uid", True, "bool is not an identity"),
        ("container_exec_uid", 0, "root is not an identity"),
        ("container_exec_gid", 0, "root is not an identity"),
        ("container_exec_uid", -1, "negative is not an identity"),
        ("container_exec_uid", 4294967295, "(uid_t)-1 sentinel is not an identity"),
        ("container_exec_gid", 4294967296, "above the 32-bit domain"),
        ("container_exec_uid", "1005", "string is not an integer"),
    ],
)
def test_1_1_observed_target_mutants_are_rejected(
    field: str,
    value: Any,
    why: str,
) -> None:
    del why
    document = _observed_mutant()
    if field == "__omit_both__":
        for name in _EXEC_FIELDS:
            document["target"].pop(name)
    elif value == "__omit__":
        document["target"].pop(field)
    else:
        document["target"][field] = value
    with pytest.raises(Exception):
        validate_receipt(document)


def test_1_1_observed_upper_bound_is_inclusive() -> None:
    document = _observed_mutant()
    document["target"]["container_exec_uid"] = 4294967294
    document["target"]["container_exec_gid"] = 4294967294
    validate_receipt(document)


def test_1_1_unobserved_target_cannot_echo_expected_config() -> None:
    from packages.common.compressed_chunk_cold_receipt import unavailable_target

    for echoed in ((1005, 1005), (1000, 1000), (0, 0)):
        target = unavailable_target()
        target["container_exec_uid"], target["container_exec_gid"] = echoed
        document = _observed_mutant()
        document["outcome"] = "refused_config"
        document["error"] = {"class": "config", "stage": "config", "reason": "refused"}
        document["config_observed"] = False
        document["budget"] = None
        document["lag_seconds"] = None
        document["per_tick_bound"] = None
        document["max_members"] = None
        document["target"] = target
        document["selected"] = []
        document["deferred"] = []
        document["skipped"] = []
        document.pop("capacity", None)
        document.pop("recovery", None)
        with pytest.raises(Exception):
            validate_receipt(document)


@pytest.mark.parametrize("field", _EXEC_FIELDS)
def test_historical_1_0_target_rejects_the_new_fields(field: str) -> None:
    document = _as_historical(_terminal())
    document["target"][field] = 1005
    with pytest.raises(Exception):
        validate_receipt(document)


def test_historical_1_0_target_rejects_a_null_new_field_too() -> None:
    """Omission, not "absent-or-null", is the 1.0 contract."""

    document = _as_historical(_terminal())
    document["target"]["container_exec_uid"] = None
    with pytest.raises(Exception):
        validate_receipt(document)


@pytest.mark.parametrize("version", ["1.2", "2.0", "0.9", 1.1, "1"])
def test_unsupported_schema_versions_are_rejected(version: Any) -> None:
    document = _observed_mutant()
    document["schema_version"] = version
    with pytest.raises(Exception):
        validate_receipt(document)


def test_mutants_are_also_rejected_by_the_external_validator(tmp_path: Path) -> None:
    """The jsonschema-library verdicts above must match `check-jsonschema`."""

    import shutil
    import subprocess

    validator = shutil.which("check-jsonschema")
    if validator is None:
        pytest.skip("check-jsonschema is required; run `uv sync --all-extras --dev`")

    def check(document: dict[str, Any], *, expect_valid: bool) -> None:
        candidate = tmp_path / "candidate.json"
        candidate.write_text(json.dumps(document), encoding="utf-8")
        result = subprocess.run(
            [validator, "--schemafile", str(_ROOT / "schemas" / f"{_BASE_NAME}.schema.json"), str(candidate)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert (result.returncode == 0) is expect_valid, result.stdout + result.stderr

    check(_as_historical(_terminal()), expect_valid=True)
    check(_terminal(), expect_valid=True)
    missing = _observed_mutant()
    missing["target"].pop("container_exec_gid")
    check(missing, expect_valid=False)
    stale = _as_historical(_terminal())
    stale["target"]["container_exec_uid"] = 1005
    check(stale, expect_valid=False)
    echoed = _observed_mutant()
    echoed["target"]["container_exec_uid"] = 0
    check(echoed, expect_valid=False)


# --- production recovery across the evidence upgrade ---------------------------


def test_startup_recovers_a_historical_1_0_sidecar_without_inventing_identity(
    tmp_path: Path,
) -> None:
    """A pre-#1929 sidecar still drives recovery handling, and every new
    terminal is 1.1 carrying THIS run's observed principal — never back-filled."""

    from scripts import node27_cold_residency as runner
    from tests.cold_residency_fakes import chunk, complete_relations
    from tests.test_node27_cold_residency import _NOW, _args, _base_env, _ready

    config = _ready(runner.config_from_args(_args(enforce=True), _base_env(tmp_path)))
    sidecar = intent_path_for(config.receipt_path)
    publish_intent(sidecar, _as_historical(_intent()))
    connection = FakeConnection()
    # Members split across tablespaces: recovery cannot claim a clean state.
    connection.load_group(chunk(), complete_relations(other_space="nhms_cold"))

    receipt = runner.run_tick(
        config,
        now_utc=_NOW,
        head_sha="a" * 40,
        connect=lambda: connection,
        fetch_watermark=lambda: _NOW,
    )

    assert receipt["outcome"] == "failed"
    assert receipt["recovery"]["authority"] == "sidecar"
    assert receipt["recovery"]["blocked_new_selection"] is True
    assert sidecar.exists()
    assert not any("SET TABLESPACE" in sql for sql, _params in connection.executed)
    # The blocker terminal is a NEW document: 1.1 with this run's observed
    # identity, while the authority it recovered from stays historical.
    assert receipt["schema_version"] == "1.1"
    assert receipt["target"]["observed"] is True
    assert receipt["target"]["container_exec_uid"] == 1005
    assert receipt["target"]["container_exec_gid"] == 1005
    assert json.loads(sidecar.read_text(encoding="utf-8"))["schema_version"] == "1.0"


def test_pending_cleanup_from_1_0_authority_closes_without_reemitting_1_0(
    tmp_path: Path,
) -> None:
    """Historical pending-cleanup authority is still reconciled and closed."""

    from scripts import node27_cold_residency as runner
    from tests.cold_residency_fakes import chunk, complete_relations
    from tests.test_node27_cold_residency import _NOW, _args, _base_env, _ready

    config = _ready(runner.config_from_args(_args(enforce=True), _base_env(tmp_path)))
    pending = _as_historical(_intent())
    pending["outcome"] = "clean"
    pending["state"] = "complete_target"
    pending["recovery"] = {
        "classification": "complete_target",
        "sidecar_present": False,
        "replayed": False,
        "blocked_new_selection": True,
        "authority": "pending_cleanup",
        "cleanup_pending": True,
    }
    publish_receipt(config.receipt_path, pending)
    connection = FakeConnection()
    connection.load_group(chunk(), complete_relations(origin_space="nhms_cold"))

    receipt = runner.run_tick(
        config,
        now_utc=_NOW,
        head_sha="a" * 40,
        connect=lambda: connection,
        fetch_watermark=lambda: _NOW,
    )

    assert receipt["schema_version"] == "1.1"
    assert receipt["recovery"]["authority"] in {"closed", "pending_cleanup"}
    assert receipt["target"]["container_exec_uid"] == 1005
    assert not any("SET TABLESPACE" in sql for sql, _params in connection.executed)
    assert read_public_receipt_durable(config.receipt_path)["schema_version"] == "1.1"


def test_startup_accepts_a_historical_1_0_public_authority_for_dry_run(tmp_path: Path) -> None:
    """A 1.0 `pending_cleanup` public receipt must still block a dry run rather
    than be dismissed as corrupt, and must not be rewritten."""

    from packages.common.compressed_chunk_cold_receipt import public_authority_blocks_selection
    from packages.common.compressed_chunk_cold_runtime_catalog import ColdRuntimeError
    from scripts import node27_cold_residency as runner
    from tests.cold_residency_fakes import chunk, complete_relations
    from tests.test_node27_cold_residency import _NOW, _args, _base_env, _ready

    config = _ready(runner.config_from_args(_args(), _base_env(tmp_path)))
    pending = _as_historical(_intent())
    pending["outcome"] = "clean"
    pending["state"] = "complete_source"
    pending["recovery"] = {
        "classification": "complete_source",
        "sidecar_present": False,
        "replayed": False,
        "blocked_new_selection": True,
        "authority": "pending_cleanup",
        "cleanup_pending": True,
    }
    publish_receipt(config.receipt_path, pending)
    before = config.receipt_path.read_bytes()
    assert public_authority_blocks_selection(read_public_receipt_durable(config.receipt_path)) is True

    connection = FakeConnection()
    connection.load_group(chunk(), complete_relations())
    with pytest.raises(ColdRuntimeError) as raised:
        runner.run_tick(
            config,
            now_utc=_NOW,
            head_sha="a" * 40,
            connect=lambda: connection,
            fetch_watermark=lambda: _NOW,
        )
    assert raised.value.error_class == "recovery_required"
    assert config.receipt_path.read_bytes() == before
    assert not any("SET TABLESPACE" in sql for sql, _params in connection.executed)


def test_reader_guard_refuses_a_schema_that_drifts_from_the_union(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A schema narrowed to const `1.1` must fail loudly, not strand 1.0."""

    import packages.common.compressed_chunk_cold_receipt as receipt

    monkeypatch.setattr(receipt, "SUPPORTED_SCHEMA_VERSIONS", ("1.1",))
    with pytest.raises(receipt.ColdReceiptError, match="union does not match"):
        receipt.load_receipt_schema()
