"""Shared CLI environment/helper fixtures for the recalibration state-clone CLI.

Extracted from :file:`tests/test_state_clone_recalibration_cli.py` when that
suite split at its ``§6.8 --pairs resolution`` marker: both the original
end-to-end/partial-write suite and the validation suite build real object
stores, packages, state indexes and registry payloads through these helpers.
The support module may itself import
:file:`tests/state_clone_recalibration_fixtures.py` (the package fixture
writer + fingerprint oracle); it is a non-test module on purpose so pytest does
not collect it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.object_store import LocalObjectStore, sha256_bytes
from packages.common.state_manager import (
    publish_state_snapshot_index,
    state_snapshot_id,
)
from scripts.node22_clone_direct_grid_cutover_states import (
    build_parser,
    enforce_mode_flags,
)
from tests.state_clone_recalibration_fixtures import (
    _CALIB_TABLE_V1,
    _CALIB_TABLE_V2,
    _CALIB_V1,
    _CALIB_V2,
    _IC_V1,
    _PARA_V1,
    _PARA_V2,
    CUTOVER_VALID_TIME,
    CYCLE_ID,
    M1_MODEL_ID,
    M1_PACKAGE_CHECKSUM,
    M1_PACKAGE_URI,
    M1P_MODEL_ID,
    M1P_PACKAGE_CHECKSUM,
    M1P_PACKAGE_URI,
    ORIGINAL_BASELINE_MODEL_ID,
    SOURCE_ID,
    _m1_source_snapshot,
    _write_package,
)


def _valid_ic_bytes(content: bytes) -> bytes:
    """Structurally-valid SHUD ``.cfg.ic`` body for the state object itself."""

    minute = 27_000_000.0 + (int.from_bytes(content[:4].ljust(4, b"\x00"), "big") % 1000)
    lines = [f"2\t1\t{minute:.6f}", "0.1\t0.2", "0.3\t0.4"]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _registry_payload(
    models: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {"schema_version": "nhms.scheduler.model_registry.v1", "models": list(models)}


def _registry_model(
    model_id: str,
    *,
    package_uri: str,
    package_checksum: str,
    source_id: str = SOURCE_ID,
) -> dict[str, Any]:
    return {
        "model_id": model_id,
        "model_package_uri": package_uri,
        "package_checksum": package_checksum,
        "resource_profile": {
            "direct_grid_source_id": source_id,
            # Still the ORIGINAL baseline, even for M1' -- the field the
            # baseline-keyed variant map would (wrongly) resolve through.
            "baseline_model_id": ORIGINAL_BASELINE_MODEL_ID,
        },
    }


def _build_cli_environment(
    tmp_path: Path,
    *,
    target_ic: bytes = _IC_V1,
    target_source_id: str = SOURCE_ID,
    target_legacy_manifest: bool = False,
    source_legacy_manifest: bool = False,
) -> dict[str, Any]:
    """Object store + both packages + both state indexes + a registry payload."""

    object_root = tmp_path / "object-store"
    object_root.mkdir(parents=True, exist_ok=True)
    store = LocalObjectStore(object_root, "s3://nhms")

    source_root = _write_package(
        store.resolve_path(M1_PACKAGE_URI),
        model_id=M1_MODEL_ID,
        calib=_CALIB_V1,
        calib_table=_CALIB_TABLE_V1,
        para=_PARA_V1,
        ic=_IC_V1,
        legacy_manifest=source_legacy_manifest,
    )
    target_root = _write_package(
        store.resolve_path(M1P_PACKAGE_URI),
        model_id=M1P_MODEL_ID,
        calib=_CALIB_V2,
        calib_table=_CALIB_TABLE_V2,
        para=_PARA_V2,
        ic=target_ic,
        legacy_manifest=target_legacy_manifest,
    )

    state_content = _valid_ic_bytes(b"m1-warm-state")
    state_uri = store.write_bytes_atomic(
        f"states/{SOURCE_ID}/{M1_MODEL_ID}/2026081512/state.cfg.ic", state_content
    )
    checksum = f"sha256:{sha256_bytes(state_content)}"
    source_snapshot = _m1_source_snapshot(state_uri=state_uri, checksum=checksum)

    entry = {
        "state_id": source_snapshot.state_id,
        "model_id": M1_MODEL_ID,
        "run_id": source_snapshot.run_id,
        "source_id": SOURCE_ID,
        "valid_time": _iso(CUTOVER_VALID_TIME),
        "state_uri": state_uri,
        "checksum": checksum,
        "usable_flag": True,
        "created_at": _iso(CUTOVER_VALID_TIME),
        "cycle_id": CYCLE_ID,
        "lead_hours": 12,
        "model_package_version": M1_PACKAGE_URI,
        "model_package_checksum": M1_PACKAGE_CHECKSUM,
    }
    canonical_index = object_root / "scheduler/state-index/index-last.json"
    mirror_index = object_root / "scheduler/state-index-mirror/index-last.json"
    for index_path in (canonical_index, mirror_index):
        publish_state_snapshot_index(
            [entry],
            index_path,
            object_store_root=object_root,
            object_store_prefix="s3://nhms",
            verify_objects=False,
        )

    registry_path = tmp_path / "variant-registry.json"
    registry_path.write_text(
        json.dumps(
            _registry_payload(
                [
                    _registry_model(
                        M1_MODEL_ID,
                        package_uri=M1_PACKAGE_URI,
                        package_checksum=M1_PACKAGE_CHECKSUM,
                    ),
                    _registry_model(
                        M1P_MODEL_ID,
                        package_uri=M1P_PACKAGE_URI,
                        package_checksum=M1P_PACKAGE_CHECKSUM,
                        source_id=target_source_id,
                    ),
                ]
            )
        ),
        encoding="utf-8",
    )
    return {
        "object_root": object_root,
        "source_root": source_root,
        "target_root": target_root,
        "canonical_index": canonical_index,
        "mirror_index": mirror_index,
        "registry_path": registry_path,
        "source_snapshot": source_snapshot,
        "state_uri": state_uri,
        "checksum": checksum,
    }


def _cli_args(env: Mapping[str, Any], *extra: str) -> Any:
    """Build parser args for a recalibration invocation.

    ``--receipt`` is required per-mode for recalibration (both apply and
    dry-run). Every call passes a UNIQUE receipt path derived from the request
    so no two invocations in one test -- and no two tests -- collide on the
    ``O_EXCL`` path. A test that wants the refusal-shape check for a missing
    receipt omits it explicitly.
    """

    parser = build_parser()
    args = parser.parse_args(
        [
            "--object-store-root",
            str(env["object_root"]),
            "--object-store-prefix",
            "s3://nhms",
            "--state-index",
            str(env["canonical_index"]),
            "--mirror-state-index",
            str(env["mirror_index"]),
            "--transfer-mode",
            "recalibration",
            "--variant-registry",
            str(env["registry_path"]),
            "--cutover-time",
            "2026081512",
            "--pairs",
            f"{M1_MODEL_ID}:{M1P_MODEL_ID}",
            "--receipt",
            str(Path(env["object_root"]).parent / f"auto-receipt-{_receipt_counter()}.json"),
            *extra,
        ]
    )
    enforce_mode_flags(parser, args)
    args.dry_run = not args.apply
    return args


def _receipt_counter() -> int:
    _receipt_counter.value = getattr(_receipt_counter, "value", 0) + 1
    return _receipt_counter.value


def _write_registry(path: Path, models: Sequence[Mapping[str, Any]]) -> Path:
    path.write_text(json.dumps(_registry_payload(list(models))), encoding="utf-8")
    return path


def _registry_models_by_id(env: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    payload = json.loads(Path(env["registry_path"]).read_text(encoding="utf-8"))
    return {str(model["model_id"]): dict(model) for model in payload["models"]}


def _add_recalibration_pair(
    env: dict[str, Any],
    *,
    source_model_id: str,
    target_model_id: str,
    target_ic: bytes,
) -> dict[str, Any]:
    """Append a SECOND ``M1->M1'`` pair to an existing CLI environment.

    Writes both packages, publishes a qualified ``(source, gfs, t*)`` +12h row
    into BOTH indexes, and appends both registry rows -- everything the second
    pair needs to reach the gate on its own merits.
    """

    object_root = env["object_root"]
    store = LocalObjectStore(object_root, "s3://nhms")
    source_uri = f"s3://nhms/models/{source_model_id}/package"
    target_uri = f"s3://nhms/models/{target_model_id}/package"
    source_checksum = f"sha256:pkg-{source_model_id}"
    target_checksum = f"sha256:pkg-{target_model_id}"

    source_root = _write_package(
        store.resolve_path(source_uri),
        model_id=source_model_id,
        calib=_CALIB_V1,
        calib_table=_CALIB_TABLE_V1,
        para=_PARA_V1,
        ic=_IC_V1,
    )
    target_root = _write_package(
        store.resolve_path(target_uri),
        model_id=target_model_id,
        calib=_CALIB_V2,
        calib_table=_CALIB_TABLE_V2,
        para=_PARA_V2,
        ic=target_ic,
    )

    state_content = _valid_ic_bytes(source_model_id.encode("utf-8"))
    state_uri = store.write_bytes_atomic(
        f"states/{SOURCE_ID}/{source_model_id}/2026081512/state.cfg.ic", state_content
    )
    checksum = f"sha256:{sha256_bytes(state_content)}"
    entry = {
        "state_id": state_snapshot_id(
            source_model_id,
            CUTOVER_VALID_TIME,
            source_id=SOURCE_ID,
            cycle_id=CYCLE_ID,
            lead_hours=12,
        ),
        "model_id": source_model_id,
        "run_id": f"fcst_{SOURCE_ID}_{CYCLE_ID}_{source_model_id}",
        "source_id": SOURCE_ID,
        "valid_time": _iso(CUTOVER_VALID_TIME),
        "state_uri": state_uri,
        "checksum": checksum,
        "usable_flag": True,
        "created_at": _iso(CUTOVER_VALID_TIME),
        "cycle_id": CYCLE_ID,
        "lead_hours": 12,
        "model_package_version": source_uri,
        "model_package_checksum": source_checksum,
    }
    for index_path in (env["canonical_index"], env["mirror_index"]):
        entries = json.loads(Path(index_path).read_text(encoding="utf-8"))["entries"]
        publish_state_snapshot_index(
            [*entries, entry],
            Path(index_path),
            object_store_root=object_root,
            object_store_prefix="s3://nhms",
            verify_objects=False,
        )

    models = list(_registry_models_by_id(env).values())
    models.extend(
        [
            _registry_model(
                source_model_id, package_uri=source_uri, package_checksum=source_checksum
            ),
            _registry_model(
                target_model_id, package_uri=target_uri, package_checksum=target_checksum
            ),
        ]
    )
    _write_registry(Path(env["registry_path"]), models)
    return {
        "source_root": source_root,
        "target_root": target_root,
        "source_model_id": source_model_id,
        "target_model_id": target_model_id,
    }
