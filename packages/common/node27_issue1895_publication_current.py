"""C3 direct current publication/display receipt owner and PASS-only binder.

This deliberately does not construct a generic two-node twelve-lane evidence
bundle.  It binds the current local display API, readonly DB identities,
registry-scoped complete-cycle counts, valid-time frontier, and an already
accepted C4 browser receipt for the one #1895 readiness invocation.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from packages.common.evidence_io import BoundedEvidenceError, read_bounded_json_with_identity_no_follow
from packages.common.node27_issue1895_display_runtime import DISPLAY_ENV_PATH
from packages.common.node27_issue1895_identity import bind_hot_identities, exact_identity_params
from packages.common.node27_issue1895_lanes import EXACT_IDENTITY_SQL, SOURCES
from packages.common.node27_issue1895_performance_live import (
    _execute_rows,
    close_performance_connection,
    fetch_identity_only_product,
    open_readonly_performance_connection,
    prove_readonly_session,
    validate_basin_id,
)
from packages.common.node27_issue1895_private_receipt import (
    MAX_INPUT_BYTES,
    assert_bracket,
    assert_timestamp_order,
    private_parent_facts,
    publish_private_receipt,
    read_private_receipt,
    refuse,
    require_matching_shas,
    require_sha,
    require_utc,
    utc_now,
)
from packages.common.node27_issue1895_publication import (
    COMPLETE_RUN_SQL,
    assert_complete_cycle_identities,
    assert_valid_time_frontier,
    registry_expected_identities,
    utc_instant,
)
from packages.common.node27_issue1895_types import Issue1895ReadinessError
from packages.common.source_identity import normalize_source_id
from services.orchestrator.scheduler_file_providers import (
    MAX_FILE_PROVIDER_JSON_NODES,
    MAX_REGISTRY_MANIFEST_BYTES,
    REGISTRY_MANIFEST_SCHEMA_VERSION,
    SchedulerFileProviderError,
    _payload_checksum,
)

ARTIFACT = "nhms-issue1895-c3-current-publication-display"
SCHEMA_VERSION = "1.0"
C4_ARTIFACT = "nhms-frontend-c4-live-evidence"
C4_SCHEMA_VERSION = "1.0"
C4_MAX_BYTES = 262_144
CANONICAL_SCHEDULER_REGISTRY_MANIFEST = Path("/home/ghdc/nwm/object-store/scheduler/registry/manifest-last.json")
REGISTRY_CHECKSUM_RE = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
BASIN_RE = re.compile(r"^[A-Za-z0-9._:-]{1,96}$")
JOB_RE = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
REQUIRED_KEYS = (
    "artifact",
    "schema_version",
    "status",
    "head_sha",
    "reviewed_sha",
    "started_at",
    "ended_at",
    "generated_at",
    "origin",
    "basin_id",
    "registry_sha256",
    "registry_facts",
    "registry_generated_at",
    "baseline_sha256",
    "frontier",
    "sources",
    "c4_receipt",
    "checks",
)
C4_REQUIRED_TOP_LEVEL = {
    "artifact",
    "schema_version",
    "status",
    "generated_at",
    "started_at",
    "ended_at",
    "origins",
    "requested_pins",
    "gfs",
    "ifs",
    "home",
    "ops",
    "source_switch",
    "no_control",
    "failure",
}


def _error(message: str, code: str) -> None:
    refuse(message, code, stage="c3")


def _local_origin(value: object) -> str:
    text = str(value or "")
    if re.fullmatch(r"http://127\.0\.0\.1:(?:[1-9][0-9]{0,4})", text) is None:
        _error("C3 origin is not the local loopback contract", "C3_ORIGIN_INVALID")
    port = int(text.rsplit(":", 1)[1])
    if port > 65535:
        _error("C3 origin port is out of range", "C3_ORIGIN_INVALID")
    return text


def _require_mapping(value: object, *, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _error("C3 JSON value is not an object", code)
    return value


def _read_json_input(
    path: Path,
    *,
    label: str,
    max_bytes: int,
    require_private: bool = False,
    max_nodes: int = 100_000,
    max_array_items: int = 10_000,
) -> tuple[bytes, dict[str, Any], dict[str, int | str]]:
    try:
        raw, identity, payload = read_bounded_json_with_identity_no_follow(
            path,
            max_bytes=max_bytes,
            label=label,
            max_depth=64,
            max_nodes=max_nodes,
            max_array_items=max_array_items,
        )
    except BoundedEvidenceError:
        _error("C3 input is unavailable, unsafe, or oversized", "C3_INPUT_INVALID")
        raise AssertionError("unreachable")
    if not isinstance(payload, Mapping):
        _error("C3 JSON input root is not an object", "C3_INPUT_INVALID")
    try:
        info = os.lstat(path)
    except OSError:
        _error("C3 input cannot be stated", "C3_INPUT_INVALID")
        raise AssertionError("unreachable")
    if int(info.st_dev) != identity.device or int(info.st_ino) != identity.inode or int(info.st_size) != identity.size:
        _error("C3 input pathname drifted after held read", "C3_INPUT_TOCTOU")
    if require_private and (
        int(info.st_uid) != os.geteuid() or (int(info.st_mode) & 0o777) != 0o600 or int(info.st_nlink) != 1
    ):
        _error("C3 input is not an euid-owned mode-0600 nlink-1 receipt", "C3_INPUT_IDENTITY")
    facts: dict[str, int | str] = {
        "st_dev": int(info.st_dev),
        "st_ino": int(info.st_ino),
        "st_uid": int(info.st_uid),
        "st_mode": int(info.st_mode & 0o777),
        "st_nlink": int(info.st_nlink),
        "st_size": int(info.st_size),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    return raw, dict(payload), facts


def _canonical_registry_path(
    path: Path,
    *,
    canonical_path: Path,
    allow_test_override: bool = False,
) -> Path:
    if (not allow_test_override and canonical_path != CANONICAL_SCHEDULER_REGISTRY_MANIFEST) or path != canonical_path:
        _error("C3 registry path is not the canonical shared scheduler manifest", "C3_REGISTRY_PATH")
    try:
        info = os.lstat(path)
    except OSError:
        _error("C3 canonical registry cannot be stated", "C3_REGISTRY_PATH")
        raise AssertionError("unreachable")
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        _error("C3 canonical registry is not a regular non-symlink file", "C3_REGISTRY_PATH")
    return path


def _registry_checksum(payload: Mapping[str, Any]) -> str:
    value = payload.get("checksum")
    text = str(value or "").strip().lower()
    if REGISTRY_CHECKSUM_RE.fullmatch(text) is None:
        _error("C3 registry checksum is invalid", "C3_REGISTRY_INVALID")
    actual = _payload_checksum(payload)
    if text.removeprefix("sha256:") != actual:
        _error("C3 registry checksum does not match manifest payload", "C3_REGISTRY_INVALID")
    return text


def _registry_models(
    path: Path,
    *,
    canonical_path: Path = CANONICAL_SCHEDULER_REGISTRY_MANIFEST,
    allow_test_override: bool = False,
) -> tuple[list[dict[str, Any]], str, dict[str, int | str], str]:
    canonical_path = _canonical_registry_path(
        path,
        canonical_path=canonical_path,
        allow_test_override=allow_test_override,
    )
    raw, payload, facts = _read_json_input(
        canonical_path,
        label="C3 registry",
        max_bytes=MAX_REGISTRY_MANIFEST_BYTES,
        max_nodes=MAX_FILE_PROVIDER_JSON_NODES,
        max_array_items=MAX_FILE_PROVIDER_JSON_NODES,
    )
    models = payload.get("models")
    generated_at = payload.get("generated_at") if isinstance(payload, Mapping) else None
    if (
        payload.get("schema_version") != REGISTRY_MANIFEST_SCHEMA_VERSION
        or not isinstance(models, list)
        or not models
        or not all(isinstance(item, Mapping) for item in models)
        or not isinstance(generated_at, str)
    ):
        _error("C3 registry does not contain a complete canonical manifest", "C3_REGISTRY_INVALID")
    try:
        generated_at = utc_instant(generated_at).isoformat().replace("+00:00", "Z")
    except Issue1895ReadinessError:
        _error("C3 registry generated_at is not calendar-valid", "C3_REGISTRY_INVALID")
    _registry_checksum(payload)
    return [dict(item) for item in models], hashlib.sha256(raw).hexdigest(), facts, generated_at


def _valid_times(payload: Mapping[str, Any], *, label: str) -> list[object]:
    value: object = payload
    if isinstance(payload.get("data"), Mapping):
        value = payload["data"]
    if not isinstance(value, Mapping) or not isinstance(value.get("valid_times"), list):
        _error(f"{label} valid-times payload is invalid", "C3_VALID_TIMES_INVALID")
    return list(value["valid_times"])


def _read_c4_receipt(path: Path, *, basin_id: str) -> tuple[bytes, dict[str, Any], dict[str, int | str]]:
    # C4 owns its private parent and publication.  Pin the descriptor while this
    # owner validates its closed PASS terminal; do not use its pathname later.
    raw, receipt, facts = _read_json_input(
        path,
        label="C3 C4 receipt",
        max_bytes=C4_MAX_BYTES,
        require_private=True,
    )
    if set(receipt) != C4_REQUIRED_TOP_LEVEL:
        _error("C4 receipt keys are not closed", "C3_C4_SCHEMA")
    if (
        receipt.get("artifact") != C4_ARTIFACT
        or receipt.get("schema_version") != C4_SCHEMA_VERSION
        or receipt.get("status") != "PASS"
        or receipt.get("failure") is not None
    ):
        _error("C4 receipt is not a PASS C4 terminal", "C3_C4_STATUS")
    pins = _require_mapping(receipt.get("requested_pins"), code="C3_C4_PIN")
    if pins.get("basin_id") != basin_id:
        _error("C4 receipt basin pin differs", "C3_C4_PIN")
    products: dict[str, Any] = {}
    for source, slot in (("GFS", "gfs"), ("IFS", "ifs")):
        product = _require_mapping(receipt.get(slot), code="C3_C4_PRODUCT")
        if product.get("source_id") != source or product.get("basin_id") != basin_id:
            _error("C4 product source/basin differs", "C3_C4_PRODUCT")
        products[source] = dict(product)
    no_control = _require_mapping(receipt.get("no_control"), code="C3_C4_CONTROL")
    if no_control != {"slurm_request_count": 0, "non_get_control_count": 0}:
        _error("C4 receipt control proof differs", "C3_C4_CONTROL")
    ops = _require_mapping(receipt.get("ops"), code="C3_C4_OPS")
    ops_record: dict[str, Any] = {}
    for source, slot in (("GFS", "gfs"), ("IFS", "ifs")):
        check = _require_mapping(ops.get(slot), code="C3_C4_OPS")
        if check.get("source_id") != source:
            _error("C4 ops source differs", "C3_C4_OPS")
        job_id = str(check.get("job_id") or "")
        if JOB_RE.fullmatch(job_id) is None:
            _error("C4 ops job_id is unsafe", "C3_C4_OPS")
        for key in ("status_status", "stages_status", "jobs_status", "logs_status"):
            status = check.get(key)
            if isinstance(status, bool) or not isinstance(status, int) or not 200 <= status <= 299:
                _error("C4 ops status is not 2xx", "C3_C4_OPS")
        ops_record[source] = {"job_id": job_id, "logs_status": int(check["logs_status"])}
    if ops_record["GFS"]["job_id"] == ops_record["IFS"]["job_id"]:
        _error("C4 GFS/IFS jobs collapsed", "C3_C4_OPS")
    return raw, {"products": products, "ops": ops_record}, facts


def _identity_for_c4(identity: Mapping[str, Any]) -> dict[str, str]:
    fields = (
        "run_id",
        "model_id",
        "basin_id",
        "basin_version_id",
        "river_network_version_id",
        "source_id",
        "cycle_time",
        "scenario",
    )
    return {field: str(identity[field]) for field in fields}


def _validate_c4_matches_bound_identity(c4: Mapping[str, Any], *, bound: Mapping[str, Mapping[str, Any]]) -> None:
    products = _require_mapping(c4.get("products"), code="C3_C4_PRODUCT")
    for source in SOURCES:
        product = _require_mapping(products.get(source), code="C3_C4_PRODUCT")
        expected = _identity_for_c4(bound[source])
        observed = {field: str(product.get(field) or "") for field in expected}
        for field, expected_value in expected.items():
            if field == "cycle_time":
                try:
                    matches = utc_instant(observed[field]) == utc_instant(expected_value)
                except Issue1895ReadinessError:
                    matches = False
            else:
                matches = observed[field] == expected_value
            if not matches:
                _error("C4 product identity differs from API/DB identity", "C3_C4_IDENTITY")
        expected_scenario = "forecast_gfs_deterministic" if source == "GFS" else "forecast_ifs_deterministic"
        if expected["scenario"] != expected_scenario:
            _error("C4 product scenario differs from its source contract", "C3_C4_IDENTITY")


def _complete_rows(connection: Any, *, identity: Mapping[str, Any], source: str) -> list[dict[str, Any]]:
    return _execute_rows(connection, COMPLETE_RUN_SQL, (normalize_source_id(source), str(identity["cycle_time"])))


def observe_current_publication(
    *,
    display_env: Path,
    registry: Path,
    basin_id: str,
    baseline_valid_times: Path,
    c4_receipt: Path,
    receipt_path: Path,
    head_sha: str,
    reviewed_sha: str,
    dsn: str,
    opener: Any | None = None,
    connect: Callable[[str], Any] | None = None,
    canonical_registry_path: Path = CANONICAL_SCHEDULER_REGISTRY_MANIFEST,
    allow_test_registry_override: bool = False,
    expected_display_env: Path = DISPLAY_ENV_PATH,
    allow_test_display_env_override: bool = False,
    now: Callable[[], str] = utc_now,
) -> dict[str, Any]:
    """Produce C3's direct current publication/display receipt after C4 PASS."""

    expected_sha = require_matching_shas(head_sha, reviewed_sha, stage="c3")
    basin = validate_basin_id(basin_id)
    started_at = now()
    # Reuse the C1 parser; it reads only the port and never exposes DATABASE_URL.
    from packages.common.node27_issue1895_display_runtime import local_origin, read_display_port

    if not allow_test_display_env_override and expected_display_env != DISPLAY_ENV_PATH:
        _error("C3 display env override requires an explicit test seam", "C3_DISPLAY_ENV_PATH")
    port, _provenance = read_display_port(display_env, expected_display_env=expected_display_env)
    origin = local_origin(port)
    registry_models, registry_digest, registry_facts, registry_generated_at = _registry_models(
        registry,
        canonical_path=canonical_registry_path,
        allow_test_override=allow_test_registry_override,
    )
    _baseline_raw, baseline_payload, _baseline_facts = _read_json_input(
        baseline_valid_times,
        label="C3 valid-times baseline",
        max_bytes=MAX_INPUT_BYTES,
    )
    baseline_digest = hashlib.sha256(_baseline_raw).hexdigest()
    baseline = _valid_times(baseline_payload, label="baseline")
    _c4_raw, c4, c4_facts = _read_c4_receipt(c4_receipt, basin_id=basin)
    connection = open_readonly_performance_connection(dsn, connect=connect)
    try:
        readonly = prove_readonly_session(connection)
        api_products: dict[str, Any] = {}
        exact_rows: dict[str, list[dict[str, Any]]] = {}
        for source in SOURCES:
            api = fetch_identity_only_product(origin=origin, source=source, basin_id=basin, opener=opener)
            api_products[source] = {"status": "ok", "data": {**api, "status": "ready", "availability": {"ready": True}}}
            exact_rows[source] = _execute_rows(
                connection, EXACT_IDENTITY_SQL, exact_identity_params(api, source=source)
            )
        bound = bind_hot_identities(api_products=api_products, db_rows=exact_rows, basin_id=basin)
        _validate_c4_matches_bound_identity(c4, bound=bound)
        try:
            expected_by_source = registry_expected_identities(registry_models)
        except SchedulerFileProviderError:
            _error("C3 registry identities are invalid", "C3_REGISTRY_INVALID")
        source_records: dict[str, Any] = {}
        for source in SOURCES:
            rows = _complete_rows(connection, identity=bound[source], source=source)
            expected = expected_by_source.get(normalize_source_id(source), frozenset())
            count = assert_complete_cycle_identities(
                source_id=source,
                cycle_time=str(bound[source]["cycle_time"]),
                rows=rows,
                expected_identities=expected,
            )
            source_records[source] = {
                "identity": _identity_for_c4(bound[source]),
                "expected_count": len(expected),
                "observed_count": count,
                "c4_job_id": c4["ops"][source]["job_id"],
                "c4_logs_status": c4["ops"][source]["logs_status"],
            }
        from packages.common.node27_issue1895_http import close_response, open_local_get, read_bounded_json_body

        _request, response = open_local_get(
            url=f"{origin}/api/v1/layers/discharge/valid-times",
            opener=opener,
            timeout_seconds=10,
            stage="c3",
        )
        try:
            valid_status, _body, current_payload = read_bounded_json_body(response, body_limit=65_536, stage="c3")
        finally:
            close_response(response)
        current = _valid_times(_require_mapping(current_payload, code="C3_VALID_TIMES_INVALID"), label="current")
        assert_valid_time_frontier(current=current, baseline=baseline)
    finally:
        close_performance_connection(connection)
    ended_at = now()
    document: dict[str, Any] = {
        "artifact": ARTIFACT,
        "schema_version": SCHEMA_VERSION,
        "status": "PASS",
        "head_sha": expected_sha,
        "reviewed_sha": expected_sha,
        "started_at": started_at,
        "ended_at": ended_at,
        "generated_at": ended_at,
        "origin": origin,
        "basin_id": basin,
        "registry_sha256": registry_digest,
        "registry_facts": registry_facts,
        "registry_generated_at": registry_generated_at,
        "baseline_sha256": baseline_digest,
        "frontier": {
            "status": valid_status,
            "current_count": len(current),
            "baseline_count": len(baseline),
            "non_regressed": True,
        },
        "sources": source_records,
        "c4_receipt": c4_facts,
        "checks": {
            "readonly_session": readonly == {"transaction_read_only": True, "current_user": "nhms_display_ro"},
            "api_db_exact_identity": True,
            "registry_complete_cycle": True,
            "valid_times_nonempty_nonregressed": True,
            "c4_pass_control_zero": True,
            "c4_api_db_identity": True,
            "c4_ops_logs": True,
        },
    }
    validate_c3_receipt(document)
    publish_private_receipt(receipt_path, document, code_prefix="C3_RECEIPT", stage="c3")
    return document


def _digest(value: object, *, code: str) -> str:
    text = str(value or "")
    if re.fullmatch(r"[0-9a-f]{64}", text) is None:
        _error("C3 digest is invalid", code)
    return text


def _validate_source_record(source: str, value: object, *, basin_id: str) -> None:
    record = _require_mapping(value, code="C3_RECEIPT_SOURCE")
    if set(record) != {"identity", "expected_count", "observed_count", "c4_job_id", "c4_logs_status"}:
        _error("C3 source record keys differ", "C3_RECEIPT_SOURCE")
    identity = _require_mapping(record.get("identity"), code="C3_RECEIPT_SOURCE")
    expected_identity = {
        "run_id",
        "model_id",
        "basin_id",
        "basin_version_id",
        "river_network_version_id",
        "source_id",
        "cycle_time",
        "scenario",
    }
    expected_scenario = "forecast_gfs_deterministic" if source == "GFS" else "forecast_ifs_deterministic"
    if (
        set(identity) != expected_identity
        or identity.get("source_id") != source
        or identity.get("basin_id") != basin_id
        or identity.get("scenario") != expected_scenario
    ):
        _error("C3 source identity differs", "C3_RECEIPT_SOURCE")
    for key, value in identity.items():
        if not isinstance(value, str) or not value:
            _error("C3 source identity has empty field", "C3_RECEIPT_SOURCE")
    for key in ("expected_count", "observed_count"):
        value = record.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            _error("C3 source count invalid", "C3_RECEIPT_SOURCE")
    if record["expected_count"] != record["observed_count"]:
        _error("C3 source expected/observed count differs", "C3_RECEIPT_SOURCE")
    if JOB_RE.fullmatch(str(record.get("c4_job_id") or "")) is None:
        _error("C3 C4 job id invalid", "C3_RECEIPT_SOURCE")
    status = record.get("c4_logs_status")
    if isinstance(status, bool) or not isinstance(status, int) or not 200 <= status <= 299:
        _error("C3 C4 logs status invalid", "C3_RECEIPT_SOURCE")


def _validate_registry_facts(value: object) -> None:
    if not isinstance(value, Mapping) or set(value) != {
        "st_dev",
        "st_ino",
        "st_uid",
        "st_mode",
        "st_nlink",
        "st_size",
        "sha256",
    }:
        _error("C3 registry facts are not closed", "C3_RECEIPT_REGISTRY")
    for key in ("st_dev", "st_ino", "st_uid", "st_mode", "st_nlink", "st_size"):
        item = value.get(key)
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            _error("C3 registry facts are invalid", "C3_RECEIPT_REGISTRY")
    _digest(value.get("sha256"), code="C3_RECEIPT_REGISTRY")


def _validate_c4_facts(value: object) -> None:
    if not isinstance(value, Mapping) or set(value) != {
        "st_dev",
        "st_ino",
        "st_uid",
        "st_mode",
        "st_nlink",
        "st_size",
        "sha256",
    }:
        _error("C3 C4 facts are not closed", "C3_RECEIPT_C4")
    if value.get("st_uid") != os.geteuid() or value.get("st_mode") != 0o600 or value.get("st_nlink") != 1:
        _error("C3 C4 facts do not describe private C4 receipt", "C3_RECEIPT_C4")
    _digest(value.get("sha256"), code="C3_RECEIPT_C4")


def validate_c3_receipt(document: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(document, Mapping) or set(document) != set(REQUIRED_KEYS):
        _error("C3 receipt keys are not closed", "C3_RECEIPT_KEYS")
    if (
        document.get("artifact") != ARTIFACT
        or document.get("schema_version") != SCHEMA_VERSION
        or document.get("status") != "PASS"
    ):
        _error("C3 receipt artifact/schema/status differs", "C3_RECEIPT_SCHEMA")
    require_matching_shas(document.get("head_sha"), document.get("reviewed_sha"), stage="c3")
    assert_timestamp_order(document, stage="c3")
    origin = _local_origin(document.get("origin"))
    del origin
    basin = document.get("basin_id")
    if not isinstance(basin, str) or BASIN_RE.fullmatch(basin) is None:
        _error("C3 basin pin invalid", "C3_RECEIPT_BASIN")
    registry_digest = _digest(document.get("registry_sha256"), code="C3_RECEIPT_DIGEST")
    _validate_registry_facts(document.get("registry_facts"))
    if document["registry_facts"]["sha256"] != registry_digest:
        _error("C3 registry facts do not bind registry digest", "C3_RECEIPT_REGISTRY")
    try:
        require_utc(document.get("registry_generated_at"), label="registry_generated_at", stage="c3")
    except Issue1895ReadinessError:
        _error("C3 registry generated_at is invalid", "C3_RECEIPT_REGISTRY")
    _digest(document.get("baseline_sha256"), code="C3_RECEIPT_DIGEST")
    frontier = _require_mapping(document.get("frontier"), code="C3_RECEIPT_FRONTIER")
    if (
        set(frontier) != {"status", "current_count", "baseline_count", "non_regressed"}
        or frontier.get("non_regressed") is not True
    ):
        _error("C3 valid-times frontier differs", "C3_RECEIPT_FRONTIER")
    for key in ("status", "current_count", "baseline_count"):
        value = frontier.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or (key != "status" and value < 1):
            _error("C3 valid-times frontier invalid", "C3_RECEIPT_FRONTIER")
    if not 200 <= frontier["status"] <= 299:
        _error("C3 valid-times status is not 2xx", "C3_RECEIPT_FRONTIER")
    sources = document.get("sources")
    if not isinstance(sources, Mapping) or set(sources) != set(SOURCES):
        _error("C3 source set differs", "C3_RECEIPT_SOURCE")
    for source in SOURCES:
        _validate_source_record(source, sources[source], basin_id=basin)
    if (
        sources["GFS"]["identity"]["run_id"] == sources["IFS"]["identity"]["run_id"]
        and sources["GFS"]["identity"]["model_id"] == sources["IFS"]["identity"]["model_id"]
        and sources["GFS"]["identity"]["cycle_time"] == sources["IFS"]["identity"]["cycle_time"]
    ):
        _error("C3 GFS/IFS source identities collapsed", "C3_RECEIPT_SOURCE")
    _validate_c4_facts(document.get("c4_receipt"))
    checks = document.get("checks")
    if checks != {
        "readonly_session": True,
        "api_db_exact_identity": True,
        "registry_complete_cycle": True,
        "valid_times_nonempty_nonregressed": True,
        "c4_pass_control_zero": True,
        "c4_api_db_identity": True,
        "c4_ops_logs": True,
    }:
        _error("C3 receipt checks differ", "C3_RECEIPT_CHECKS")
    return dict(document)


def bind_c3_receipt(
    receipt_path: Path,
    *,
    reviewed_sha: str,
    basin_id: str,
    c4_receipt: Path,
    registry: Path,
    canonical_registry_path: Path = CANONICAL_SCHEDULER_REGISTRY_MANIFEST,
    allow_test_registry_override: bool = False,
    cmd_start: str,
    cmd_end: str,
) -> dict[str, Any]:
    expected_sha = require_sha(reviewed_sha, label="reviewed_sha", stage="c3")
    basin = validate_basin_id(basin_id)
    raw, document, info = read_private_receipt(receipt_path, code_prefix="C3_RECEIPT", stage="c3")
    del raw
    private_parent_facts(receipt_path, code_prefix="C3_RECEIPT")
    validated = validate_c3_receipt(document)
    if validated["head_sha"] != expected_sha or validated["reviewed_sha"] != expected_sha:
        _error("C3 receipt SHA does not bind reviewed SHA", "C3_BIND_SHA")
    if validated["basin_id"] != basin:
        _error("C3 receipt basin does not bind expected pin", "C3_BIND_BASIN")
    raw_c4, _c4, facts = _read_json_input(
        c4_receipt,
        label="C3 C4 receipt",
        max_bytes=C4_MAX_BYTES,
        require_private=True,
    )
    if hashlib.sha256(raw_c4).hexdigest() != validated["c4_receipt"]["sha256"] or facts != validated["c4_receipt"]:
        _error("C3 C4 receipt changed after acceptance", "C3_BIND_C4_TAMPER")
    _models, registry_digest, registry_facts, registry_generated_at = _registry_models(
        registry,
        canonical_path=canonical_registry_path,
        allow_test_override=allow_test_registry_override,
    )
    if (
        registry_digest != validated["registry_sha256"]
        or registry_facts != validated["registry_facts"]
        or registry_generated_at != validated["registry_generated_at"]
    ):
        _error("C3 canonical registry changed after acceptance", "C3_BIND_REGISTRY_TAMPER")
    assert_bracket(info=info, document=validated, cmd_start=cmd_start, cmd_end=cmd_end, stage="c3")
    return validated


__all__ = (
    "ARTIFACT",
    "CANONICAL_SCHEDULER_REGISTRY_MANIFEST",
    "COMPLETE_RUN_SQL",
    "SCHEMA_VERSION",
    "bind_c3_receipt",
    "observe_current_publication",
    "validate_c3_receipt",
)
