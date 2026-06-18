from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from packages.common.source_identity import normalize_source_id

DIRECT_GRID_MODE = "direct_grid"
IDW_MODE = "idw"
DIRECT_GRID_SECTION_KEYS: tuple[str, ...] = (
    "direct_grid_forcing",
    "direct_grid_contract",
    "forcing_mapping_contract",
)
MAX_DIRECT_GRID_STATION_BINDINGS = 10_000
REQUIRED_MANIFEST_FIELDS: tuple[str, ...] = (
    "binding_uri",
    "binding_checksum",
    "model_input_package_id",
    "sp_att_path",
    "sp_att_checksum",
    "applicable_source_ids",
    "grid_id",
    "grid_signature",
)
REQUIRED_STATION_FIELDS: tuple[str, ...] = (
    "station_id",
    "shud_forcing_index",
    "forcing_filename",
    "longitude",
    "latitude",
    "x",
    "y",
    "z",
    "grid_id",
    "grid_cell_id",
)

_SAFE_STATION_FORCING_FILENAME = re.compile(r"^[A-Za-z0-9._-]+\.csv$")


class DirectGridContractError(ValueError):
    """Raised when a direct-grid forcing asset contract is incomplete or unsafe."""

    def __init__(
        self,
        message: str,
        *,
        field: str | None = None,
        source_id: str | None = None,
        station_id: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.field = field
        self.source_id = source_id
        self.station_id = station_id
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error_code": "DIRECT_GRID_CONTRACT_INVALID", "message": str(self)}
        if self.field is not None:
            payload["field"] = self.field
        if self.source_id is not None:
            payload["source_id"] = self.source_id
        if self.station_id is not None:
            payload["station_id"] = self.station_id
        payload.update(self.details)
        return payload


@dataclass(frozen=True)
class DirectGridStationBinding:
    station_id: str
    shud_forcing_index: int
    forcing_filename: str
    longitude: float
    latitude: float
    x: float
    y: float
    z: float
    grid_id: str
    grid_cell_id: str
    properties: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DirectGridForcingContract:
    forcing_mapping_mode: str
    binding_uri: str
    binding_checksum: str
    model_input_package_id: str
    sp_att_path: str
    sp_att_checksum: str
    applicable_source_ids: tuple[str, ...]
    grid_id: str
    grid_signature: str
    stations: tuple[DirectGridStationBinding, ...]


def load_forcing_mapping_contract_from_manifest(
    manifest: Mapping[str, Any] | None,
    *,
    source_id: str | None = None,
    allow_root_direct_grid: bool = True,
) -> DirectGridForcingContract | None:
    """Parse direct-grid metadata from an in-memory model asset manifest/profile.

    The model asset manifest/resource profile is the only authority here. This helper
    intentionally does not read ``binding_uri`` or consult database mirror tables.
    """

    if manifest is None:
        return None
    if not isinstance(manifest, Mapping):
        raise DirectGridContractError("Model asset manifest must be a JSON object.")

    if "forcing_mapping_mode" in manifest:
        mode = _required_text(manifest, "forcing_mapping_mode", source_id=source_id)
        if mode == IDW_MODE:
            return None
        if mode != DIRECT_GRID_MODE:
            raise DirectGridContractError(
                f"Unsupported forcing_mapping_mode {mode!r}.",
                field="forcing_mapping_mode",
                source_id=source_id,
                details={"supported_modes": [IDW_MODE, DIRECT_GRID_MODE]},
            )
        contract_payload = _direct_grid_section_payload(manifest)
        if contract_payload is None:
            if not allow_root_direct_grid:
                return None
            contract_payload = manifest
        return parse_direct_grid_forcing_contract(contract_payload, source_id=source_id)

    contract_payload = _direct_grid_section_payload(manifest)
    if contract_payload is None:
        return None

    mode = _required_text(contract_payload, "forcing_mapping_mode", source_id=source_id)
    if mode == IDW_MODE:
        return None
    if mode != DIRECT_GRID_MODE:
        raise DirectGridContractError(
            f"Unsupported forcing_mapping_mode {mode!r}.",
            field="forcing_mapping_mode",
            source_id=source_id,
            details={"supported_modes": [IDW_MODE, DIRECT_GRID_MODE]},
        )

    return parse_direct_grid_forcing_contract(contract_payload, source_id=source_id)


def parse_direct_grid_forcing_contract(
    manifest: Mapping[str, Any],
    *,
    source_id: str | None = None,
) -> DirectGridForcingContract:
    mode = _required_text(manifest, "forcing_mapping_mode", source_id=source_id)
    if mode != DIRECT_GRID_MODE:
        raise DirectGridContractError(
            f"Unsupported forcing_mapping_mode {mode!r}.",
            field="forcing_mapping_mode",
            source_id=source_id,
            details={"supported_modes": [DIRECT_GRID_MODE]},
        )

    binding_uri = _required_text(manifest, "binding_uri", source_id=source_id)
    binding_checksum = _required_text(manifest, "binding_checksum", source_id=source_id)
    model_input_package_id = _required_text(manifest, "model_input_package_id", source_id=source_id)
    sp_att_path = _required_text(manifest, "sp_att_path", source_id=source_id)
    sp_att_checksum = _required_text(manifest, "sp_att_checksum", source_id=source_id)
    grid_id = _required_text(manifest, "grid_id", source_id=source_id)
    grid_signature = _required_text(manifest, "grid_signature", source_id=source_id)
    applicable_source_ids = _applicable_source_ids(manifest, source_id=source_id)
    stations = _station_bindings(manifest, grid_id=grid_id, source_id=source_id)

    return DirectGridForcingContract(
        forcing_mapping_mode=mode,
        binding_uri=binding_uri,
        binding_checksum=binding_checksum,
        model_input_package_id=model_input_package_id,
        sp_att_path=sp_att_path,
        sp_att_checksum=sp_att_checksum,
        applicable_source_ids=applicable_source_ids,
        grid_id=grid_id,
        grid_signature=grid_signature,
        stations=stations,
    )


def _direct_grid_section_payload(manifest: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for key in DIRECT_GRID_SECTION_KEYS:
        value = manifest.get(key)
        if value is None:
            continue
        if not isinstance(value, Mapping):
            raise DirectGridContractError(f"{key} must be a JSON object.", field=key)
        return value
    return None


def _required_text(
    payload: Mapping[str, Any],
    field_name: str,
    *,
    source_id: str | None,
    station_id: str | None = None,
) -> str:
    value = payload.get(field_name)
    if value is None:
        raise DirectGridContractError(
            f"Direct-grid contract is missing required field {field_name!r}.",
            field=field_name,
            source_id=source_id,
            station_id=station_id,
        )
    if not isinstance(value, str):
        raise DirectGridContractError(
            f"Direct-grid contract field {field_name!r} must be a JSON string.",
            field=field_name,
            source_id=source_id,
            station_id=station_id,
            details={"actual_type": type(value).__name__},
        )
    text = value.strip()
    if not text:
        raise DirectGridContractError(
            f"Direct-grid contract is missing required field {field_name!r}.",
            field=field_name,
            source_id=source_id,
            station_id=station_id,
        )
    return text


def _applicable_source_ids(payload: Mapping[str, Any], *, source_id: str | None) -> tuple[str, ...]:
    raw_sources = payload.get("applicable_source_ids")
    if not isinstance(raw_sources, Sequence) or isinstance(raw_sources, str | bytes):
        raise DirectGridContractError(
            "Direct-grid contract field 'applicable_source_ids' must be a non-empty list.",
            field="applicable_source_ids",
            source_id=source_id,
        )
    normalized: list[str] = []
    for raw_source in raw_sources:
        if not isinstance(raw_source, str):
            raise DirectGridContractError(
                "Direct-grid contract source identifiers must be JSON strings.",
                field="applicable_source_ids",
                source_id=source_id,
                details={"invalid_source_id": raw_source, "actual_type": type(raw_source).__name__},
            )
        try:
            normalized_source = normalize_source_id(raw_source)
        except ValueError as error:
            raise DirectGridContractError(
                f"Direct-grid contract includes unsupported source {raw_source!r}.",
                field="applicable_source_ids",
                source_id=source_id,
                details={"invalid_source_id": raw_source},
            ) from error
        if normalized_source not in normalized:
            normalized.append(normalized_source)
    if not normalized:
        raise DirectGridContractError(
            "Direct-grid contract field 'applicable_source_ids' must not be empty.",
            field="applicable_source_ids",
            source_id=source_id,
        )
    if source_id is not None:
        current_source = normalize_source_id(source_id)
        if current_source not in normalized:
            raise DirectGridContractError(
                "Direct-grid contract does not apply to the current source.",
                field="applicable_source_ids",
                source_id=current_source,
                details={"applicable_source_ids": tuple(normalized)},
            )
    return tuple(normalized)


def _station_bindings(
    payload: Mapping[str, Any],
    *,
    grid_id: str,
    source_id: str | None,
) -> tuple[DirectGridStationBinding, ...]:
    raw_stations = payload.get("station_bindings", payload.get("stations"))
    if not isinstance(raw_stations, Sequence) or isinstance(raw_stations, str | bytes):
        raise DirectGridContractError(
            "Direct-grid contract requires a non-empty station binding list.",
            field="station_bindings",
            source_id=source_id,
        )
    if not raw_stations:
        raise DirectGridContractError(
            "Direct-grid contract requires at least one station binding.",
            field="station_bindings",
            source_id=source_id,
        )
    if len(raw_stations) > MAX_DIRECT_GRID_STATION_BINDINGS:
        raise DirectGridContractError(
            "Direct-grid contract exceeds the station binding count limit.",
            field="station_bindings",
            source_id=source_id,
            details={
                "observed_count": len(raw_stations),
                "max_count": MAX_DIRECT_GRID_STATION_BINDINGS,
            },
        )

    bindings: list[DirectGridStationBinding] = []
    for offset, raw_station in enumerate(raw_stations):
        if not isinstance(raw_station, Mapping):
            raise DirectGridContractError(
                "Direct-grid station binding must be a JSON object.",
                field=f"station_bindings[{offset}]",
                source_id=source_id,
            )
        station_id = _required_text(raw_station, "station_id", source_id=source_id)
        station_grid_id = _required_text(raw_station, "grid_id", source_id=source_id, station_id=station_id)
        if station_grid_id != grid_id:
            raise DirectGridContractError(
                "Direct-grid station grid_id does not match manifest grid_id.",
                field="grid_id",
                source_id=source_id,
                station_id=station_id,
                details={"expected_grid_id": grid_id, "actual_grid_id": station_grid_id},
            )
        forcing_index = _required_positive_int(
            raw_station,
            "shud_forcing_index",
            source_id=source_id,
            station_id=station_id,
        )
        filename = _required_text(raw_station, "forcing_filename", source_id=source_id, station_id=station_id)
        if not _safe_forcing_filename(filename):
            raise DirectGridContractError(
                "Direct-grid station forcing_filename is unsafe.",
                field="forcing_filename",
                source_id=source_id,
                station_id=station_id,
                details={"forcing_filename": filename},
            )
        bindings.append(
            DirectGridStationBinding(
                station_id=station_id,
                shud_forcing_index=forcing_index,
                forcing_filename=filename,
                longitude=_required_float(raw_station, "longitude", source_id=source_id, station_id=station_id),
                latitude=_required_float(raw_station, "latitude", source_id=source_id, station_id=station_id),
                x=_required_float(raw_station, "x", source_id=source_id, station_id=station_id),
                y=_required_float(raw_station, "y", source_id=source_id, station_id=station_id),
                z=_required_float(raw_station, "z", source_id=source_id, station_id=station_id),
                grid_id=station_grid_id,
                grid_cell_id=_required_text(raw_station, "grid_cell_id", source_id=source_id, station_id=station_id),
                properties={},
            )
        )

    _validate_station_uniqueness_and_indexes(bindings, source_id=source_id)
    return tuple(sorted(bindings, key=lambda binding: binding.shud_forcing_index))


def _required_positive_int(
    payload: Mapping[str, Any],
    field_name: str,
    *,
    source_id: str | None,
    station_id: str,
) -> int:
    value = payload.get(field_name)
    if type(value) is not int:
        raise DirectGridContractError(
            f"Direct-grid station field {field_name!r} must be a JSON integer.",
            field=field_name,
            source_id=source_id,
            station_id=station_id,
            details={"actual_type": type(value).__name__},
        )
    if value <= 0:
        raise DirectGridContractError(
            f"Direct-grid station field {field_name!r} must be positive.",
            field=field_name,
            source_id=source_id,
            station_id=station_id,
        )
    return value


def _required_float(
    payload: Mapping[str, Any],
    field_name: str,
    *,
    source_id: str | None,
    station_id: str,
) -> float:
    if field_name not in payload:
        raise DirectGridContractError(
            f"Direct-grid station is missing required field {field_name!r}.",
            field=field_name,
            source_id=source_id,
            station_id=station_id,
        )
    value = payload[field_name]
    if type(value) not in {int, float}:
        raise DirectGridContractError(
            f"Direct-grid station field {field_name!r} must be a finite JSON number.",
            field=field_name,
            source_id=source_id,
            station_id=station_id,
            details={"actual_type": type(value).__name__},
        )
    parsed_value = float(value)
    if not math.isfinite(parsed_value):
        raise DirectGridContractError(
            f"Direct-grid station field {field_name!r} must be finite.",
            field=field_name,
            source_id=source_id,
            station_id=station_id,
            details={"actual_type": type(value).__name__, "value": repr(value)},
        )
    return parsed_value


def _safe_forcing_filename(filename: str) -> bool:
    if not filename or "/" in filename or "\\" in filename:
        return False
    if filename in {".", ".."}:
        return False
    return bool(_SAFE_STATION_FORCING_FILENAME.fullmatch(filename))


def _validate_station_uniqueness_and_indexes(
    bindings: Sequence[DirectGridStationBinding],
    *,
    source_id: str | None,
) -> None:
    indexes = [binding.shud_forcing_index for binding in bindings]
    expected_indexes = list(range(1, len(bindings) + 1))
    if sorted(indexes) != expected_indexes:
        raise DirectGridContractError(
            "Direct-grid shud_forcing_index values must be unique and contiguous from 1.",
            field="shud_forcing_index",
            source_id=source_id,
            details={"actual_indexes": tuple(sorted(indexes)), "expected_indexes": tuple(expected_indexes)},
        )

    filenames: set[str] = set()
    station_ids: set[str] = set()
    for binding in bindings:
        if binding.forcing_filename in filenames:
            raise DirectGridContractError(
                "Direct-grid forcing_filename values must be unique.",
                field="forcing_filename",
                source_id=source_id,
                station_id=binding.station_id,
                details={"forcing_filename": binding.forcing_filename},
            )
        filenames.add(binding.forcing_filename)
        if binding.station_id in station_ids:
            raise DirectGridContractError(
                "Direct-grid station_id values must be unique.",
                field="station_id",
                source_id=source_id,
                station_id=binding.station_id,
            )
        station_ids.add(binding.station_id)
