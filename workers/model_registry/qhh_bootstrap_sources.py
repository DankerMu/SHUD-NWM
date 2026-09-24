"""QHH bootstrap source readers and identity checks: ``tsd.forc`` / ``sp.riv``
parsing, bounded inventory/manifest JSON preflight and source identity /
physical binding validation (#2490 split of ``qhh_production_bootstrap``).

``workers.model_registry.qhh_production_bootstrap`` stays the stable import
path and re-exports every name defined here.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from .basins_geometry import TrustedBasinsRoot
from .basins_registry_import import (
    BasinsRegistryImportError,
    ImportSources,
    _find_inventory_model,
    _input_dir,
    _inventory_root,
    _prepare_sources,
    _recorded_relative_inventory_root,
    _source_root,
)
from .qhh_bootstrap_contracts import (
    DEFAULT_QHH_MODEL_ID,
    DEFAULT_QHH_PROJECT_NAME,
    MAX_QHH_CHECKSUM_BYTES,
    MAX_QHH_JSON_BYTES,
    MAX_QHH_OUTPUT_SEGMENTS,
    MAX_QHH_SP_RIV_BYTES,
    MAX_QHH_TSD_FORC_BYTES,
    MAX_QHH_TSD_FORC_STATIONS,
    QhhBootstrapPaths,
    QhhForcingStation,
    QhhPreflightSources,
    QhhProductionBootstrapError,
    _from_registry_error,
)
from .qhh_bootstrap_fs import (
    _coerce_trusted_root,
    _read_contained_file_limited,
    _read_standalone_file_limited,
    _safe_directory_binding,
    _safe_trusted_root_binding,
)


def read_qhh_tsd_forc(
    path: str | Path,
    containment_root: Path | TrustedBasinsRoot,
    *,
    model_id: str = DEFAULT_QHH_MODEL_ID,
    project_name: str = DEFAULT_QHH_PROJECT_NAME,
) -> tuple[tuple[QhhForcingStation, ...], str]:
    root = _coerce_trusted_root(containment_root, role="qhh_tsd_forc")
    source = Path(path).expanduser()
    content = _read_contained_file_limited(
        source,
        root,
        max_bytes=MAX_QHH_TSD_FORC_BYTES,
        error_code="QHH_BOOTSTRAP_TSD_FORC_OVERSIZED",
        model_id=model_id,
        role="qhh_tsd_forc",
    )
    checksum = hashlib.sha256(content).hexdigest()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_TSD_FORC_MALFORMED",
            "QHH qhh.tsd.forc is not valid UTF-8 text.",
            model_id=model_id,
            path=str(source),
            details={"no_mutation_expected": True},
        ) from error

    raw_lines = text.splitlines()
    lines = [line.strip() for line in raw_lines if line.strip()]
    if len(lines) < 4:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_TSD_FORC_MALFORMED",
            "QHH qhh.tsd.forc must include a count header, metadata lines, and station rows.",
            model_id=model_id,
            path=str(source),
            details={"line_count": len(lines), "no_mutation_expected": True},
        )
    try:
        expected_count = int(lines[0].split()[0])
    except (IndexError, ValueError) as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_TSD_FORC_MALFORMED",
            "QHH qhh.tsd.forc first token must be the forcing station count.",
            model_id=model_id,
            path=str(source),
            details={"header": lines[0][:120], "no_mutation_expected": True},
        ) from error
    if expected_count < 1 or expected_count > MAX_QHH_TSD_FORC_STATIONS:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_STATION_COUNT_INVALID",
            "QHH forcing station count is outside bootstrap bounds.",
            model_id=model_id,
            path=str(source),
            details={
                "expected_count": expected_count,
                "max_station_count": MAX_QHH_TSD_FORC_STATIONS,
                "no_mutation_expected": True,
            },
        )

    stations: list[QhhForcingStation] = []
    malformed_rows: list[dict[str, Any]] = []
    for line_number, raw in enumerate(lines[3:], start=4):
        parts = raw.split()
        if len(parts) < 7:
            malformed_rows.append({"line_number": line_number, "reason": "too_few_columns"})
            continue
        forcing_index = _parse_tsd_forc_station_index(parts[0])
        forcing_filename = _parse_tsd_forc_filename(parts[6])
        if forcing_index is None:
            malformed_rows.append({"line_number": line_number, "reason": "invalid_forcing_index"})
            continue
        if forcing_filename is None:
            malformed_rows.append({"line_number": line_number, "reason": "invalid_forcing_filename"})
            continue
        try:
            longitude = float(parts[1])
            latitude = float(parts[2])
            x = float(parts[3])
            y = float(parts[4])
            z = float(parts[5])
        except ValueError:
            malformed_rows.append({"line_number": line_number, "reason": "non_numeric_column"})
            continue
        if not (
            math.isfinite(longitude)
            and math.isfinite(latitude)
            and math.isfinite(x)
            and math.isfinite(y)
            and math.isfinite(z)
        ):
            malformed_rows.append({"line_number": line_number, "reason": "non_finite_xyz"})
            continue
        if not (-180.0 <= longitude <= 180.0 and -90.0 <= latitude <= 90.0):
            malformed_rows.append({"line_number": line_number, "reason": "invalid_lon_lat"})
            continue
        station_id = f"{project_name}_forc_{forcing_index:03d}"
        stations.append(
            QhhForcingStation(
                station_id=station_id,
                station_name=f"{project_name.upper()} forcing station {forcing_index:03d}",
                forcing_index=forcing_index,
                longitude=longitude,
                latitude=latitude,
                x=x,
                y=y,
                z=z,
                elevation_m=0.0 if z <= -9990 else z,
                forcing_filename=forcing_filename,
                original_id=parts[0],
            )
        )
    index_counts = Counter(station.forcing_index for station in stations)
    duplicate_indexes = sorted(index for index, count in index_counts.items() if count > 1)
    if malformed_rows:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_TSD_FORC_MALFORMED",
            "QHH qhh.tsd.forc contains malformed forcing station rows.",
            model_id=model_id,
            path=str(source),
            details={"malformed_rows": malformed_rows[:20], "no_mutation_expected": True},
        )
    if duplicate_indexes:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_TSD_FORC_MALFORMED",
            "QHH qhh.tsd.forc contains duplicate forcing station indexes.",
            model_id=model_id,
            path=str(source),
            details={"duplicate_forcing_indexes": duplicate_indexes[:20], "no_mutation_expected": True},
        )
    if len(stations) != expected_count:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_STATION_COUNT_MISMATCH",
            "QHH forcing station row count does not match qhh.tsd.forc header.",
            model_id=model_id,
            path=str(source),
            details={
                "expected_count": expected_count,
                "parsed_count": len(stations),
                "no_mutation_expected": True,
            },
        )
    stations.sort(key=lambda item: item.forcing_index)
    return tuple(stations), checksum


def _parse_tsd_forc_station_index(value: str) -> int | None:
    if not value or not value.isascii() or not value.isdecimal():
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    if parsed < 1 or parsed > MAX_QHH_TSD_FORC_STATIONS:
        return None
    return parsed


def _parse_tsd_forc_filename(value: str) -> str | None:
    if not value or "\x00" in value or value in {".", ".."} or "\\" in value:
        return None
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or len(candidate.parts) != 1:
        return None
    part = candidate.parts[0]
    if part in {"", ".", ".."}:
        return None
    return part


def read_qhh_output_segment_count(
    path: str | Path,
    containment_root: Path | TrustedBasinsRoot,
    *,
    model_id: str = DEFAULT_QHH_MODEL_ID,
) -> tuple[int, str]:
    root = _coerce_trusted_root(containment_root, role="qhh_sp_riv")
    source = Path(path).expanduser()
    content = _read_contained_file_limited(
        source,
        root,
        max_bytes=MAX_QHH_SP_RIV_BYTES,
        error_code="QHH_BOOTSTRAP_SP_RIV_OVERSIZED",
        model_id=model_id,
        role="qhh_sp_riv",
    )
    checksum = hashlib.sha256(content).hexdigest()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_SP_RIV_MALFORMED",
            "QHH SHUD river output identity file is not valid UTF-8 text.",
            model_id=model_id,
            path=str(source),
            details={"no_mutation_expected": True},
        ) from error
    lines = [(line_number, line.strip()) for line_number, line in enumerate(text.splitlines(), start=1) if line.strip()]
    try:
        header_line_number, header = lines[0]
        count = int(header.split()[0])
    except (StopIteration, UnicodeDecodeError, IndexError, ValueError) as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_SP_RIV_MALFORMED",
            "QHH SHUD river output identity file has an invalid count header.",
            model_id=model_id,
            path=str(source),
            details={"no_mutation_expected": True},
        ) from error
    if count < 1 or count > MAX_QHH_OUTPUT_SEGMENTS:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_OUTPUT_SEGMENT_COUNT_INVALID",
            "QHH SHUD output river count is outside bootstrap bounds.",
            model_id=model_id,
            path=str(source),
            details={
                "output_segment_count": count,
                "max_output_segment_count": MAX_QHH_OUTPUT_SEGMENTS,
                "no_mutation_expected": True,
            },
        )
    body = lines[1:]
    # Standard SHUD .sp.riv files are multi-block: the count header is followed by a
    # column-name line (e.g. "Index Down Type Slope Length BC"), then `count` data
    # rows, then unrelated trailing blocks (channel types, coordinates). Skip the
    # optional column-name line and read exactly the first block's `count` rows. A
    # legacy single-block file (data rows immediately after the header) still works:
    # such a first row parses as a valid segment row, not a column-name line.
    if body:
        _, first_raw = body[0]
        first_parts = first_raw.split()
        if (
            len(first_parts) >= 6
            and _parse_sp_riv_segment_token(first_parts[0]) is None
            and not _sp_riv_numeric_columns_valid(first_parts[1:6])
        ):
            body = body[1:]
    data_rows = body[:count]
    malformed_rows: list[dict[str, Any]] = []
    segment_tokens: list[int] = []
    for line_number, raw in data_rows:
        parts = raw.split()
        if len(parts) < 6:
            malformed_rows.append({"line_number": line_number, "reason": "too_few_columns"})
            continue
        segment_token = _parse_sp_riv_segment_token(parts[0])
        if segment_token is None:
            malformed_rows.append({"line_number": line_number, "reason": "invalid_segment_token"})
            continue
        if not _sp_riv_numeric_columns_valid(parts[1:6]):
            malformed_rows.append({"line_number": line_number, "reason": "non_numeric_column"})
            continue
        segment_tokens.append(segment_token)
    if malformed_rows:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_SP_RIV_MALFORMED",
            "QHH qhh.sp.riv contains malformed output river rows.",
            model_id=model_id,
            path=str(source),
            details={"malformed_rows": malformed_rows[:20], "no_mutation_expected": True},
        )
    if len(data_rows) != count:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_OUTPUT_SEGMENT_COUNT_MISMATCH",
            "QHH SHUD output river body row count does not match qhh.sp.riv header.",
            model_id=model_id,
            path=str(source),
            details={
                "expected_count": count,
                "parsed_count": len(data_rows),
                "header_line_number": header_line_number,
                "no_mutation_expected": True,
            },
        )
    expected_tokens = set(range(1, count + 1))
    observed_tokens = set(segment_tokens)
    duplicate_tokens = sorted(token for token, token_count in Counter(segment_tokens).items() if token_count > 1)
    missing_tokens = sorted(expected_tokens - observed_tokens)
    extra_tokens = sorted(observed_tokens - expected_tokens)
    if duplicate_tokens or missing_tokens or extra_tokens:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_SP_RIV_MALFORMED",
            "QHH qhh.sp.riv segment identity tokens must exactly match the declared output river count.",
            model_id=model_id,
            path=str(source),
            details={
                "duplicate_segment_tokens": duplicate_tokens[:20],
                "missing_segment_tokens": missing_tokens[:20],
                "extra_segment_tokens": extra_tokens[:20],
                "no_mutation_expected": True,
            },
        )
    return count, checksum


def _parse_sp_riv_segment_token(value: str) -> int | None:
    if not value or not value.isascii() or not value.isdecimal():
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    if parsed < 1 or parsed > MAX_QHH_OUTPUT_SEGMENTS:
        return None
    return parsed


def _sp_riv_numeric_columns_valid(values: Sequence[str]) -> bool:
    for value in values:
        try:
            parsed = float(value)
        except ValueError:
            return False
        if not math.isfinite(parsed):
            return False
    return True


def _prepare_preflight_sources_from_bounded_json(
    inventory_path: Path,
    manifest_path: Path,
    *,
    model_id: str,
) -> QhhPreflightSources:
    inventory, inventory_bytes = _read_json_object_bounded(
        inventory_path,
        max_bytes=MAX_QHH_JSON_BYTES,
        error_code="QHH_BOOTSTRAP_INVENTORY_INVALID",
        not_found_code="QHH_BOOTSTRAP_INVENTORY_NOT_FOUND",
        model_id=model_id,
    )
    manifest, _manifest_bytes = _read_json_object_bounded(
        manifest_path,
        max_bytes=MAX_QHH_JSON_BYTES,
        error_code="QHH_BOOTSTRAP_PACKAGE_MANIFEST_INVALID",
        not_found_code="QHH_BOOTSTRAP_PACKAGE_MANIFEST_NOT_FOUND",
        model_id=model_id,
    )
    try:
        manifest_model_id = manifest.get("model_id")
        if not isinstance(manifest_model_id, str) or not manifest_model_id:
            raise BasinsRegistryImportError(
                "BASINS_REGISTRY_PACKAGE_MANIFEST_INVALID",
                "Required field is missing: model_id",
                model_id=model_id,
            )
        if manifest_model_id != model_id:
            raise QhhProductionBootstrapError(
                "QHH_BOOTSTRAP_MODEL_ID_MISMATCH",
                "QHH package manifest model_id does not match requested bootstrap model_id.",
                model_id=model_id,
                details={"actual_model_id": manifest_model_id, "no_mutation_expected": True},
            )
        model = _find_inventory_model(inventory, model_id)
        inventory_root = _inventory_root(inventory, model_id)
        inventory_relative_root = _recorded_relative_inventory_root(inventory)
        source_root = _source_root(inventory_root, inventory_relative_root, model, model_id)
        input_dir = _input_dir(inventory_root, inventory_relative_root, source_root, model, model_id)
    except QhhProductionBootstrapError:
        raise
    except BasinsRegistryImportError as error:
        raise _from_registry_error(error, model_id=model_id) from error
    return QhhPreflightSources(
        inventory=inventory,
        manifest=manifest,
        model=model,
        input_dir=input_dir,
        source_root=source_root,
        inventory_raw_checksum=hashlib.sha256(inventory_bytes).hexdigest(),
    )


def _prepare_sources_from_preflight(preflight_sources: QhhPreflightSources, *, model_id: str) -> ImportSources:
    try:
        return _prepare_sources(
            preflight_sources.inventory,
            preflight_sources.manifest,
            inventory_raw_checksum=preflight_sources.inventory_raw_checksum,
        )
    except BasinsRegistryImportError as error:
        raise _from_registry_error(error, model_id=model_id) from error


def _prepare_sources_from_bounded_json(inventory_path: Path, manifest_path: Path, *, model_id: str) -> ImportSources:
    preflight_sources = _prepare_preflight_sources_from_bounded_json(
        inventory_path,
        manifest_path,
        model_id=model_id,
    )
    return _prepare_sources_from_preflight(preflight_sources, model_id=model_id)


def _require_qhh_source_identity(
    sources: ImportSources,
    *,
    qhh_basin_slug: str,
    qhh_project_name: str,
    model_id: str,
) -> None:
    if sources.ids["model_id"] != model_id:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_MODEL_ID_MISMATCH",
            "QHH package manifest model_id does not match requested bootstrap model_id.",
            model_id=model_id,
            details={"actual_model_id": sources.ids["model_id"], "no_mutation_expected": True},
        )
    if sources.model.get("basin_slug") != qhh_basin_slug or sources.model.get("shud_input_name") != qhh_project_name:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_SOURCE_IDENTITY_MISMATCH",
            "QHH package source identity does not match requested basin/project.",
            model_id=model_id,
            details={
                "expected_basin_slug": qhh_basin_slug,
                "actual_basin_slug": sources.model.get("basin_slug"),
                "expected_project_name": qhh_project_name,
                "actual_project_name": sources.model.get("shud_input_name"),
                "no_mutation_expected": True,
            },
        )


def _require_qhh_preflight_source_identity(
    sources: QhhPreflightSources,
    *,
    qhh_basin_slug: str,
    qhh_project_name: str,
    model_id: str,
) -> None:
    if sources.manifest.get("model_id") != model_id:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_MODEL_ID_MISMATCH",
            "QHH package manifest model_id does not match requested bootstrap model_id.",
            model_id=model_id,
            details={"actual_model_id": sources.manifest.get("model_id"), "no_mutation_expected": True},
        )
    identity_fields = {
        "inventory_basin_slug": sources.model.get("basin_slug"),
        "inventory_project_name": sources.model.get("shud_input_name"),
        "manifest_basin_slug": sources.manifest.get("basin_slug"),
        "manifest_project_name": sources.manifest.get("shud_input_name"),
    }
    if (
        identity_fields["inventory_basin_slug"] != qhh_basin_slug
        or identity_fields["inventory_project_name"] != qhh_project_name
        or identity_fields["manifest_basin_slug"] != qhh_basin_slug
        or identity_fields["manifest_project_name"] != qhh_project_name
    ):
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_SOURCE_IDENTITY_MISMATCH",
            "QHH package source identity does not match requested basin/project.",
            model_id=model_id,
            details={
                "expected_basin_slug": qhh_basin_slug,
                "expected_project_name": qhh_project_name,
                **identity_fields,
                "no_mutation_expected": True,
            },
        )


def _require_qhh_physical_source_binding(
    sources: ImportSources,
    paths: QhhBootstrapPaths,
    *,
    model_id: str,
) -> None:
    source_actual = _safe_directory_binding(sources.source_root, model_id=model_id, role="inventory_source_root")
    source_expected = _safe_directory_binding(paths.qhh_source_root, model_id=model_id, role="qhh_source_root")
    input_actual = _safe_trusted_root_binding(sources.input_dir, model_id=model_id, role="inventory_input_dir")
    input_expected = _safe_trusted_root_binding(paths.qhh_input_dir, model_id=model_id, role="qhh_input_dir")
    mismatches: list[str] = []
    if source_actual != source_expected:
        mismatches.append("source_root")
    if input_actual != input_expected:
        mismatches.append("input_dir")
    if mismatches:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_SOURCE_ROOT_MISMATCH",
            "QHH inventory/package physical source paths do not match the configured Basins root.",
            model_id=model_id,
            path=str(paths.basins_root),
            details={
                "fields": mismatches,
                "expected_source_root": str(source_expected["path"]),
                "actual_source_root": str(source_actual["path"]),
                "expected_input_dir": str(input_expected["path"]),
                "actual_input_dir": str(input_actual["path"]),
                "no_mutation_expected": True,
            },
        )


def _require_qhh_preflight_physical_source_binding(
    sources: QhhPreflightSources,
    paths: QhhBootstrapPaths,
    *,
    model_id: str,
) -> None:
    source_actual = _safe_directory_binding(sources.source_root, model_id=model_id, role="inventory_source_root")
    source_expected = _safe_directory_binding(paths.qhh_source_root, model_id=model_id, role="qhh_source_root")
    input_actual = _safe_trusted_root_binding(sources.input_dir, model_id=model_id, role="inventory_input_dir")
    input_expected = _safe_trusted_root_binding(paths.qhh_input_dir, model_id=model_id, role="qhh_input_dir")
    mismatches: list[str] = []
    if source_actual != source_expected:
        mismatches.append("source_root")
    if input_actual != input_expected:
        mismatches.append("input_dir")
    if mismatches:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_SOURCE_ROOT_MISMATCH",
            "QHH inventory/package physical source paths do not match the configured Basins root.",
            model_id=model_id,
            path=str(paths.basins_root),
            details={
                "fields": mismatches,
                "expected_source_root": str(source_expected["path"]),
                "actual_source_root": str(source_actual["path"]),
                "expected_input_dir": str(input_expected["path"]),
                "actual_input_dir": str(input_actual["path"]),
                "no_mutation_expected": True,
            },
        )


def _validate_manifest_checksum(path: Path, manifest: dict[str, Any], *, model_id: str) -> None:
    del manifest
    checksum_path = path.with_suffix(path.suffix + ".sha256")
    if not checksum_path.exists():
        return
    content = _read_standalone_file_limited(
        checksum_path,
        max_bytes=MAX_QHH_CHECKSUM_BYTES,
        error_code="QHH_BOOTSTRAP_CHECKSUM_OVERSIZED",
        model_id=model_id,
        role="manifest_checksum",
    )
    try:
        expected = content.decode("utf-8", errors="replace").strip().split()[0]
    except IndexError as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_CHECKSUM_MALFORMED",
            "QHH package manifest checksum sidecar is empty or malformed.",
            model_id=model_id,
            path=str(checksum_path),
            details={"no_mutation_expected": True},
        ) from error
    manifest_bytes = _read_standalone_file_limited(
        path,
        max_bytes=MAX_QHH_JSON_BYTES,
        error_code="QHH_BOOTSTRAP_PACKAGE_MANIFEST_OVERSIZED",
        model_id=model_id,
        role="package_manifest",
    )
    actual = hashlib.sha256(manifest_bytes).hexdigest()
    if expected != actual:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_MANIFEST_DIGEST_MISMATCH",
            "QHH package manifest checksum sidecar does not match manifest content.",
            model_id=model_id,
            path=str(checksum_path),
            details={"expected_sha256": expected, "actual_sha256": actual, "no_mutation_expected": True},
        )


def _read_json_object_bounded(
    path: Path,
    *,
    max_bytes: int,
    error_code: str,
    not_found_code: str,
    model_id: str,
) -> tuple[dict[str, Any], bytes]:
    try:
        content = _read_standalone_file_limited(
            path,
            max_bytes=max_bytes,
            error_code=f"{error_code}_OVERSIZED",
            model_id=model_id,
            role="json",
        )
    except FileNotFoundError as error:
        raise QhhProductionBootstrapError(
            not_found_code,
            "QHH bootstrap JSON input does not exist.",
            model_id=model_id,
            path=str(path),
            details={"no_mutation_expected": True},
        ) from error
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise QhhProductionBootstrapError(
            error_code,
            "QHH bootstrap JSON input is malformed.",
            model_id=model_id,
            path=str(path),
            details={"no_mutation_expected": True},
        ) from error
    if not isinstance(payload, dict):
        raise QhhProductionBootstrapError(
            error_code,
            "QHH bootstrap JSON input must contain an object.",
            model_id=model_id,
            path=str(path),
            details={"no_mutation_expected": True},
        )
    return payload, content
