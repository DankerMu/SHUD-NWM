"""Pure dual-device accounting and strict governance receipt tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import jsonschema
import pytest

from packages.common.node27_cold_governance import (
    GovernanceConfig,
    build_cold_governance_receipt,
    reconcile_filesystems,
    write_cold_governance_receipt,
)

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
SHA = "f" * 40


def _sample(
    *,
    path: str,
    used: int,
    pgdata: int,
    cold: int,
    object_store: int,
    observed_at: str = "2026-08-31T12:00:01Z",
    reserved: int = 0,
    total: int | None = None,
    free: int | None = None,
) -> dict:
    used_bytes = used
    reserved_bytes = reserved
    free_bytes = 1_000 - used_bytes - reserved_bytes if free is None else free
    total_bytes = used_bytes + free_bytes + reserved_bytes if total is None else total
    return {
        "path": path,
        "observed_at": observed_at,
        "identity": "8:11" if path == "/home" else "8:12",
        "status": "ok",
        "total_bytes": total_bytes,
        "free_bytes": free_bytes,
        "used_bytes": used_bytes,
        "reserved_bytes": reserved_bytes,
        "pgdata_bytes": pgdata,
        "nhms_cold_relation_bytes": cold,
        "object_store_bytes": object_store,
    }


def _evidence() -> dict:
    return {
        "health": {
            "healthy": True,
            "raid": {"file_identity": {"sha256": "a" * 64}},
            "smart": [
                {"device": "/dev/sdb1", "status": "PASS", "file_identity": {"sha256": "c" * 64}},
                {"device": "/dev/sdc1", "status": "PASS", "file_identity": {"sha256": "d" * 64}},
            ],
        },
        "backup": {"complete": True, "file_identity": {"sha256": "b" * 64}, "missing_targets": []},
        "mount_inventory": {"current": [], "stopped": []},
        "catalog": {
            "tablespace": "nhms_cold",
            "location": "/home/postgres/pgdata/tablespaces/nhms_cold",
            "relations": [],
        },
    }


def test_reserved_ext4_sample_is_healthy_when_evidence_is_healthy() -> None:
    result = reconcile_filesystems(
        _sample(path="/home", used=800, pgdata=300, cold=0, object_store=200, reserved=50),
        _sample(path="/data/GHDC", used=700, pgdata=0, cold=400, object_store=0, reserved=80),
    )
    assert result.approved is True
    assert result.filesystems["home"]["reserved_bytes"] == 50
    assert result.filesystems["cold"]["reserved_bytes"] == 80
    assert result.filesystems["home"]["total_bytes"] == 1_000
    home = result.filesystems["home"]
    assert home["used_bytes"] + home["free_bytes"] + home["reserved_bytes"] == home["total_bytes"]


def test_zero_reserve_sample_remains_healthy() -> None:
    result = reconcile_filesystems(
        _sample(path="/home", used=800, pgdata=300, cold=0, object_store=200, reserved=0),
        _sample(path="/data/GHDC", used=700, pgdata=0, cold=400, object_store=0, reserved=0),
    )
    assert result.approved is True
    assert result.filesystems["home"]["reserved_bytes"] == 0


def test_two_device_residual_is_arithmetic_without_shared_root_scan() -> None:
    result = reconcile_filesystems(
        _sample(path="/home", used=800, pgdata=300, cold=0, object_store=200),
        _sample(path="/data/GHDC", used=700, pgdata=0, cold=400, object_store=0),
    )

    assert result.approved is True
    assert result.filesystems["home"]["residual_bytes"] == 300
    assert result.filesystems["cold"]["residual_bytes"] == 300
    assert "recursive" not in json.dumps(result.filesystems).lower()


def test_negative_or_overlapping_accounting_is_blocking_not_clamped() -> None:
    result = reconcile_filesystems(
        _sample(path="/home", used=100, pgdata=90, cold=20, object_store=0),
        _sample(path="/data/GHDC", used=200, pgdata=0, cold=100, object_store=0),
    )

    assert result.approved is False
    assert result.filesystems["home"]["residual_bytes"] == -10
    assert any("negative" in blocker for blocker in result.blockers)


def test_observation_outside_audit_interval_is_a_refusal() -> None:
    result = reconcile_filesystems(
        _sample(path="/home", used=800, pgdata=300, cold=0, object_store=200, observed_at="2026-08-31T11:59:59Z"),
        _sample(path="/data/GHDC", used=700, pgdata=0, cold=400, object_store=0),
        started_at="2026-08-31T12:00:00Z",
        finished_at="2026-08-31T12:00:05Z",
    )

    assert result.approved is False
    assert any("interval" in blocker for blocker in result.blockers)


def test_healthy_governance_receipt_is_schema_valid_atomic_private_and_secret_free(tmp_path: Path) -> None:
    config = GovernanceConfig(receipt_path=tmp_path / "governance.json", head_sha=SHA)
    receipt, schema = build_cold_governance_receipt(
        config=config,
        started_at="2026-08-31T12:00:00Z",
        finished_at="2026-08-31T12:00:05Z",
        home=_sample(path="/home", used=800, pgdata=300, cold=0, object_store=200),
        cold=_sample(path="/data/GHDC", used=700, pgdata=0, cold=400, object_store=0),
        evidence=_evidence(),
    )

    assert receipt["outcome"] == "healthy"
    jsonschema.validate(receipt, schema)
    write_cold_governance_receipt(config.receipt_path, receipt, schema)
    assert (config.receipt_path.stat().st_mode & 0o777) == 0o600
    assert "password" not in config.receipt_path.read_text(encoding="utf-8")


def test_governance_rejects_secret_bearing_evidence_before_publication(tmp_path: Path) -> None:
    config = GovernanceConfig(receipt_path=tmp_path / "governance.json", head_sha=SHA)
    evidence = _evidence()
    evidence["catalog"]["password"] = "do-not-publish"

    receipt, schema = build_cold_governance_receipt(
        config=config,
        started_at="2026-08-31T12:00:00Z",
        finished_at="2026-08-31T12:00:05Z",
        home=_sample(path="/home", used=800, pgdata=300, cold=0, object_store=200),
        cold=_sample(path="/data/GHDC", used=700, pgdata=0, cold=400, object_store=0),
        evidence=evidence,
    )

    assert "do-not-publish" not in json.dumps(receipt)
    write_cold_governance_receipt(config.receipt_path, receipt, schema)


def test_governance_history_baseline_trend_stale_and_identity_drift_are_bounded(tmp_path: Path) -> None:
    prior_path = tmp_path / "prior.json"
    prior_config = GovernanceConfig(receipt_path=prior_path, head_sha=SHA)
    prior, schema = build_cold_governance_receipt(
        config=prior_config,
        started_at="2026-08-31T11:59:00Z",
        finished_at="2026-08-31T11:59:05Z",
        home=_sample(
            path="/home", used=700, pgdata=300, cold=0, object_store=200, observed_at="2026-08-31T11:59:01Z"
        ),
        cold=_sample(
            path="/data/GHDC", used=600, pgdata=0, cold=400, object_store=0, observed_at="2026-08-31T11:59:02Z"
        ),
        evidence=_evidence(),
    )
    write_cold_governance_receipt(prior_path, prior, schema)
    config = GovernanceConfig(
        receipt_path=tmp_path / "current.json",
        head_sha=SHA,
        prior_receipt_path=prior_path,
        prior_receipt_max_age_seconds=600,
    )

    current, _ = build_cold_governance_receipt(
        config=config,
        started_at="2026-08-31T12:00:00Z",
        finished_at="2026-08-31T12:00:05Z",
        home=_sample(path="/home", used=800, pgdata=300, cold=0, object_store=200),
        cold=_sample(path="/data/GHDC", used=700, pgdata=0, cold=400, object_store=0),
        evidence=_evidence(),
    )

    assert current["outcome"] == "healthy"
    assert current["trend"]["status"] == "trend"
    assert current["trend"]["deltas"] == {"home_residual_bytes": 100, "cold_residual_bytes": 100}
    assert current["trend"]["prior"]["sha256"]

    stale, _ = build_cold_governance_receipt(
        config=GovernanceConfig(
            receipt_path=tmp_path / "stale.json",
            head_sha=SHA,
            prior_receipt_path=prior_path,
            prior_receipt_max_age_seconds=1,
        ),
        started_at="2026-08-31T12:00:00Z",
        finished_at="2026-08-31T12:00:05Z",
        home=_sample(path="/home", used=800, pgdata=300, cold=0, object_store=200),
        cold=_sample(path="/data/GHDC", used=700, pgdata=0, cold=400, object_store=0),
        evidence=_evidence(),
    )
    assert stale["outcome"] == "refusal"
    assert stale["trend"]["status"] == "invalid"
    assert "stale" in " ".join(stale["blockers"])

    prior_path.chmod(0o644)
    drifted, _ = build_cold_governance_receipt(
        config=config,
        started_at="2026-08-31T12:00:00Z",
        finished_at="2026-08-31T12:00:05Z",
        home=_sample(path="/home", used=800, pgdata=300, cold=0, object_store=200),
        cold=_sample(path="/data/GHDC", used=700, pgdata=0, cold=400, object_store=0),
        evidence=_evidence(),
    )
    assert drifted["outcome"] == "refusal"
    assert drifted["trend"]["status"] == "invalid"
    assert "mode" in " ".join(drifted["blockers"])


def test_governance_receipt_examples_are_schema_valid() -> None:
    root = Path(__file__).resolve().parents[1] / "schemas" / "examples"
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "schemas" / "node27_cold_governance_receipt.schema.json").read_text(
            encoding="utf-8"
        )
    )
    for name in ("node27_cold_governance_receipt.example.json", "node27_cold_governance_receipt.drift.example.json"):
        jsonschema.validate(json.loads((root / name).read_text(encoding="utf-8")), schema)


def test_shipping_schema_rejects_healthy_ok_filesystem_with_null_identity_and_bytes() -> None:
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "schemas" / "node27_cold_governance_receipt.schema.json").read_text(
            encoding="utf-8"
        )
    )
    example = (
        Path(__file__).resolve().parents[1]
        / "schemas"
        / "examples"
        / "node27_cold_governance_receipt.example.json"
    )
    mutant = json.loads(example.read_text(encoding="utf-8"))
    mutant["filesystems"]["home"].update(
        {
            "identity": None,
            "total_bytes": None,
            "free_bytes": None,
            "used_bytes": None,
            "reserved_bytes": None,
            "pgdata_bytes": None,
            "nhms_cold_relation_bytes": None,
            "object_store_bytes": None,
            "residual_bytes": None,
        }
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(mutant, schema)


def test_drift_and_missing_evidence_publish_schema_valid_refusal_replacing_stale_success(tmp_path: Path) -> None:
    config = GovernanceConfig(receipt_path=tmp_path / "governance.json", head_sha=SHA)
    config.receipt_path.write_text('{"outcome":"healthy","password":"old-secret"}', encoding="utf-8")
    config.receipt_path.chmod(0o600)
    evidence = _evidence()
    evidence["mount_inventory"] = {"current": [], "stopped": [{"source": "/data/GHDC/nhms-cold-tablespace"}]}
    evidence["backup"] = {
        "complete": False,
        "missing_targets": ["/home/postgres/pgdata/tablespaces/nhms_cold"],
        "file_identity": {"sha256": "b" * 64},
    }

    receipt, schema = build_cold_governance_receipt(
        config=config,
        started_at="2026-08-31T12:00:00Z",
        finished_at="2026-08-31T12:00:05Z",
        home=_sample(path="/home", used=800, pgdata=300, cold=0, object_store=200),
        cold=_sample(path="/data/GHDC", used=700, pgdata=0, cold=400, object_store=0),
        evidence=evidence,
    )
    write_cold_governance_receipt(config.receipt_path, receipt, schema)

    saved = json.loads(config.receipt_path.read_text(encoding="utf-8"))
    assert saved["outcome"] == "refusal"
    assert any("stopped" in blocker or "backup" in blocker for blocker in saved["blockers"])
    assert "old-secret" not in json.dumps(saved)
    jsonschema.validate(saved, schema)


def test_healthy_reserved_receipt_and_examples_carry_reserved_bytes(tmp_path: Path) -> None:
    config = GovernanceConfig(receipt_path=tmp_path / "governance.json", head_sha=SHA)
    receipt, schema = build_cold_governance_receipt(
        config=config,
        started_at="2026-08-31T12:00:00Z",
        finished_at="2026-08-31T12:00:05Z",
        home=_sample(path="/home", used=800, pgdata=300, cold=0, object_store=200, reserved=50),
        cold=_sample(path="/data/GHDC", used=700, pgdata=0, cold=400, object_store=0, reserved=80),
        evidence=_evidence(),
    )
    assert receipt["outcome"] == "healthy"
    assert receipt["filesystems"]["home"]["reserved_bytes"] == 50
    jsonschema.validate(receipt, schema)
    write_cold_governance_receipt(config.receipt_path, receipt, schema)


# ---------------------------------------------------------------------------
# I6 (#1985) — the working-set block travels with the strict receipt too
# ---------------------------------------------------------------------------

_ROOT_DIR = Path(__file__).resolve().parents[1]
_COLD_SCHEMA = json.loads(
    (_ROOT_DIR / "schemas" / "node27_cold_governance_receipt.schema.json").read_text(encoding="utf-8")
)

_WORKING_SET = {
    "uncompressed_bytes": 644245094400,
    "daily_ingest_bytes": 80530636800,
    "next_compressible_at": "2026-09-02T00:00:00Z",
    "home_free_bytes": 966367641600,
    "projected_peak_bytes": 805306368000,
    "projection_status": "ok",
    "compression_lag_seconds": 172800,
    "watermark": "2026-08-31T00:00:00Z",
}


def test_cold_receipt_carries_the_working_set_when_it_is_supplied(tmp_path: Path) -> None:
    config = GovernanceConfig(receipt_path=tmp_path / "governance.json", head_sha=SHA)
    receipt, schema = build_cold_governance_receipt(
        config=config,
        started_at="2026-08-31T12:00:00Z",
        finished_at="2026-08-31T12:00:05Z",
        home=_sample(path="/home", used=800, pgdata=300, cold=0, object_store=200),
        cold=_sample(path="/data/GHDC", used=700, pgdata=0, cold=400, object_store=0),
        evidence=_evidence(),
        working_set=_WORKING_SET,
    )
    assert receipt["working_set"] == _WORKING_SET
    jsonschema.validate(receipt, schema)


def test_cold_receipt_omits_the_working_set_when_it_is_not_supplied(tmp_path: Path) -> None:
    """Optional by construction: the strict receipt predates the projection and
    a caller that cannot measure it must not be forced to invent zeroes."""
    config = GovernanceConfig(receipt_path=tmp_path / "governance.json", head_sha=SHA)
    receipt, schema = build_cold_governance_receipt(
        config=config,
        started_at="2026-08-31T12:00:00Z",
        finished_at="2026-08-31T12:00:05Z",
        home=_sample(path="/home", used=800, pgdata=300, cold=0, object_store=200),
        cold=_sample(path="/data/GHDC", used=700, pgdata=0, cold=400, object_store=0),
        evidence=_evidence(),
    )
    assert "working_set" not in receipt
    jsonschema.validate(receipt, schema)


def test_both_cold_governance_examples_carry_the_working_set() -> None:
    """Both examples carry the block, so the new fields are exercised by CI's
    schema-example job (`.github/workflows/ci.yml`, which strips the `.drift`
    family suffix to find the schema) as well as by this suite.

    Keys are asserted as a SUBSET of the production shape, not as equality with
    the eight-key literal below: the collector emits ten keys (it adds
    `hypertables` and `uncompressed_chunks`), and an example is free to show
    them.
    """
    root = _ROOT_DIR / "schemas" / "examples"
    for name in (
        "node27_cold_governance_receipt.example.json",
        "node27_cold_governance_receipt.drift.example.json",
    ):
        document = json.loads((root / name).read_text(encoding="utf-8"))
        keys = set(document["working_set"])
        assert set(_WORKING_SET) <= keys
        assert keys <= _PRODUCTION_WORKING_SET_KEYS
        jsonschema.validate(document, _COLD_SCHEMA)


# The exact-string pin on CI's family-strip alternation was RETIRED in the
# #1985 round-2 pass. Two reasons: the alternation itself is gone (the strip is
# now `s/\.[a-z0-9-]+$//`, so there is no closed list left to enumerate), and a
# ci.yml-only PR routes to `tests/test_select_ci_tests.py` alone (#1650 exact
# single-target selection) — this file would never have run on the PR that
# broke it. The replacement lives there: it parses the sed out of the workflow
# and asserts that EVERY example under `schemas/examples/` resolves to a real
# schema, which covers the `.drift` case this pin covered plus the twenty-three
# it did not.


_PRODUCTION_WORKING_SET_KEYS = {
    "hypertables",
    "uncompressed_bytes",
    "uncompressed_chunks",
    "daily_ingest_bytes",
    "next_compressible_at",
    "home_free_bytes",
    "projected_peak_bytes",
    "projection_status",
    "compression_lag_seconds",
    "watermark",
}


def _fake_cursor(*, hypertables: list[dict], chunks: list[dict]) -> object:
    class _Cursor:
        def __init__(self) -> None:
            self._rows: list[dict] = []

        def execute(self, sql: str, params: object = None) -> None:
            self._rows = (
                hypertables if "timescaledb_information.hypertables" in sql else chunks
            )

        def fetchall(self) -> list[dict]:
            return list(self._rows)

    return _Cursor()


@pytest.mark.parametrize(
    "status",
    ["ok", "no_uncompressed_chunk", "watermark_unavailable", "working_set_unavailable"],
)
def test_the_production_working_set_validates_for_every_projection_status(
    tmp_path: Path, status: str
) -> None:
    """The shape the COLLECTOR produces — ten keys — driven end to end through
    `finalize_working_set` into the strict receipt and validated. The examples
    are hand-written; this is the only test that proves the real producer's
    output fits the schema, in every state it can be in.
    """
    from packages.common import node27_cold_governance_collection as collection

    watermark = datetime(2026, 9, 1, tzinfo=UTC)
    hypertables = [
        {
            "hypertable_schema": schema,
            "hypertable_name": name,
            "num_chunks": 3,
            "compression_enabled": True,
        }
        for schema, name in (("hydro", "river_timeseries"), ("met", "forcing_station_timeseries"))
    ]
    chunks = [
        {
            "hypertable_schema": "hydro",
            "hypertable_name": "river_timeseries",
            "chunk_schema": "_timescaledb_internal",
            "chunk_name": "_hyper_3_1_chunk",
            "range_start": datetime(2026, 8, 30, tzinfo=UTC),
            "range_end": datetime(2026, 8, 31, tzinfo=UTC),
            "total_bytes": 1024,
        }
    ]
    if status == "no_uncompressed_chunk":
        chunks = []
    if status == "working_set_unavailable":
        hypertables = []
    cursor = _fake_cursor(hypertables=hypertables, chunks=chunks)
    sample = collection.collect_working_set(
        cursor,
        watermark=None if status == "watermark_unavailable" else watermark,
        lag_seconds=172_800,
    )
    working_set = collection.finalize_working_set(sample, home_free_bytes=900 * 1024**3)
    assert working_set["projection_status"] == status
    assert set(working_set) == _PRODUCTION_WORKING_SET_KEYS

    receipt, schema = build_cold_governance_receipt(
        config=GovernanceConfig(receipt_path=tmp_path / "governance.json", head_sha=SHA),
        started_at="2026-08-31T12:00:00Z",
        finished_at="2026-08-31T12:00:05Z",
        home=_sample(path="/home", used=800, pgdata=300, cold=0, object_store=200),
        cold=_sample(path="/data/GHDC", used=700, pgdata=0, cold=400, object_store=0),
        evidence=_evidence(),
        working_set=working_set,
    )
    assert receipt["working_set"] == working_set
    jsonschema.validate(receipt, schema)


@pytest.mark.parametrize(
    "mutation",
    [
        {"projection_status": "made_up"},
        {"uncompressed_bytes": -1},
        {"projected_peak_bytes": "750 GiB"},
    ],
    ids=["status", "negative", "string"],
)
def test_cold_schema_rejects_a_mis_shaped_working_set(mutation: dict) -> None:
    document = json.loads(
        (_ROOT_DIR / "schemas" / "examples" / "node27_cold_governance_receipt.example.json").read_text(
            encoding="utf-8"
        )
    )
    document["working_set"].update(mutation)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, _COLD_SCHEMA)
