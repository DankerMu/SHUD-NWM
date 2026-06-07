from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from packages.common.met_store import PsycopgMetStore
from packages.common.object_store import LocalObjectStore, ObjectStoreError, sha256_bytes
from packages.common.source_identity import normalize_source_id

LOGGER = logging.getLogger(__name__)


def _float_env(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return float(raw_value)
    except ValueError:
        LOGGER.warning("Invalid %s=%r; using default %s", name, raw_value, default)
        return default


# IFS 累积净太阳辐射夜间持平,GRIB 位打包让持平段抖动几百 J/m²(≈0.1 W/m²),
# 去累积后产生伪负 delta。SHUD 模型自身(NetcdfForcingProvider.cpp)对净辐射 Rn 即
# "rn<0 → 0" 再 nearbyint 取整到整数 W/m²,故亚 W/m² 的负值经模型读入后与 0 逐位等价,
# 并非数据降级。低于此速率阈值的负值按模型约定 clamp 到 0,不标 quality_flag=warn,
# 以免夜间 shortwave 产品被 forcing 当不可用剔除;超阈值的负值仍标 warn 供诊断。
IFS_SHORTWAVE_NEGATIVE_TOLERANCE_W_M2 = _float_env("IFS_SHORTWAVE_NEGATIVE_TOLERANCE_W_M2", 1.0)

# 累积降水去累积的负 delta 噪声容差(mm/步)。IFS tp(米,GRIB 16-bit 打包)的量化步长
# ≈1000×2⁻¹⁶=0.0153mm,会让晚时次(累积量大)出现伪负 delta;原 0.01mm 阈值比量化步长还
# 小,把量化噪声误判为 warning_negative_precip → 被 forcing(只收 quality_flag==ok)剔除致缺产品。
# SHUD 模型自身对 precip<0 即钳 0 并量化到 4 位,故亚阈值负值与 0 等价。默认 0.1mm 覆盖
# IFS tp 量化步长和 GFS APCP 常见 1/16mm 量化噪声;超阈值的负值仍标 warn 供诊断。
PRECIP_NEGATIVE_NOISE_TOLERANCE_MM = _float_env("PRECIP_NEGATIVE_NOISE_TOLERANCE_MM", 0.1)

VARIABLE_MAPPING: dict[str, str] = {
    "tmp2m": "air_temperature_2m",
    "apcp": "prcp_rate_or_amount",
    "rh2m": "relative_humidity_2m",
    "u10m": "wind_u_10m",
    "v10m": "wind_v_10m",
    "pressfc": "pressure_surface",
    "dswrf": "shortwave_down",
}
ERA5_VARIABLE_MAPPING: dict[str, str] = {
    "2m_temperature": "air_temperature_2m",
    "2m_dewpoint_temperature": "relative_humidity_2m",
    "10m_u_component_of_wind": "wind_u_10m",
    "10m_v_component_of_wind": "wind_v_10m",
    "surface_pressure": "pressure_surface",
    "total_precipitation": "prcp_rate_or_amount",
    "surface_net_solar_radiation": "net_radiation",
    "surface_net_thermal_radiation": "net_radiation",
}
IFS_VARIABLE_MAPPING: dict[str, str] = {
    "2t": "air_temperature_2m",
    "2d": "relative_humidity_2m",
    "tp": "prcp_rate_or_amount",
    "10u": "wind_u_10m",
    "10v": "wind_v_10m",
    "sp": "surface_pressure",
    "ssr": "net_radiation",
    "str": "net_radiation",
}
STANDARD_UNITS: dict[str, str] = {
    "air_temperature_2m": "degC",
    "prcp_rate_or_amount": "mm/day",
    "relative_humidity_2m": "0-1",
    "wind_u_10m": "m/s",
    "wind_v_10m": "m/s",
    "wind_speed": "m/s",
    "pressure_surface": "Pa",
    "shortwave_down": "W/m2",
    "net_radiation": "W/m2",
}
ERA5_STANDARD_UNITS: dict[str, str] = {
    **STANDARD_UNITS,
    "prcp_rate_or_amount": "mm/day",
}
IFS_STANDARD_UNITS: dict[str, str] = {
    **STANDARD_UNITS,
    "surface_pressure": "Pa",
    "prcp_rate_or_amount": "mm/day",
}
GFS_REQUIRED_STANDARD_VARIABLES: tuple[str, ...] = (
    "prcp_rate_or_amount",
    "air_temperature_2m",
    "relative_humidity_2m",
    "wind_u_10m",
    "wind_v_10m",
    "pressure_surface",
    "shortwave_down",
)
GFS_F000_OPTIONAL_INTERVAL_STANDARD_VARIABLES: frozenset[str] = frozenset(
    {"prcp_rate_or_amount", "shortwave_down"}
)
IFS_REQUIRED_STANDARD_VARIABLES: tuple[str, ...] = (
    "prcp_rate_or_amount",
    "air_temperature_2m",
    "relative_humidity_2m",
    "wind_u_10m",
    "wind_v_10m",
    "surface_pressure",
    "shortwave_down",
)
ERA5_REQUIRED_STANDARD_VARIABLES: tuple[str, ...] = (
    "prcp_rate_or_amount",
    "air_temperature_2m",
    "relative_humidity_2m",
    "wind_u_10m",
    "wind_v_10m",
    "pressure_surface",
    "net_radiation",
)
REQUIRED_STANDARD_VARIABLES_BY_SOURCE: dict[str, tuple[str, ...]] = {
    "gfs": GFS_REQUIRED_STANDARD_VARIABLES,
    "ERA5": ERA5_REQUIRED_STANDARD_VARIABLES,
    "IFS": IFS_REQUIRED_STANDARD_VARIABLES,
}
CONVERSION_PARAMS: dict[str, str] = {
    "tmp2m": "K_to_C",
    "apcp": "cumulative_to_mm_day",
    "rh2m": "pct_to_frac",
    "u10m": "pass_through",
    "v10m": "pass_through",
    "pressfc": "pass_through",
    "dswrf": "pass_through",
    "2m_temperature": "K_to_C",
    "2m_dewpoint_temperature": "dewpoint_magnus_rh",
    "10m_u_component_of_wind": "pass_through",
    "10m_v_component_of_wind": "pass_through",
    "surface_pressure": "pass_through",
    "total_precipitation": "cumulative_m_to_mm_day",
    "surface_net_solar_radiation": "cumulative_j_m2_to_w_m2",
    "surface_net_thermal_radiation": "cumulative_j_m2_to_w_m2",
    "2t": "K_to_C",
    "2d": "dewpoint_magnus_rh",
    "tp": "cumulative_m_to_mm_day",  # IFS tp emits mm/day; this alias entry is unreachable for IFS
    "10u": "pass_through",
    "10v": "pass_through",
    "sp": "pass_through",
    "ssr": "cumulative_j_m2_to_w_m2",
    "str": "cumulative_j_m2_to_w_m2",
}
CFGRIB_VARIABLE_ALIASES: dict[str, tuple[str, ...]] = {
    "tmp2m": ("tmp2m", "t2m", "2t"),
    "apcp": ("apcp", "tp", "total_precipitation"),
    "rh2m": ("rh2m", "r2", "2r"),
    "u10m": ("u10m", "u10", "10u"),
    "v10m": ("v10m", "v10", "10v"),
    "pressfc": ("pressfc", "sp", "pres"),
    "dswrf": ("dswrf", "ssrd", "sdswrf"),
    "2m_temperature": ("2m_temperature", "t2m", "2t"),
    "2m_dewpoint_temperature": ("2m_dewpoint_temperature", "d2m", "2d"),
    "10m_u_component_of_wind": ("10m_u_component_of_wind", "u10", "10u"),
    "10m_v_component_of_wind": ("10m_v_component_of_wind", "v10", "10v"),
    "surface_pressure": ("surface_pressure", "sp"),
    "total_precipitation": ("total_precipitation", "tp"),
    "surface_net_solar_radiation": ("surface_net_solar_radiation", "ssr"),
    "surface_net_thermal_radiation": ("surface_net_thermal_radiation", "str"),
    "2t": ("2t", "t2m"),
    "2d": ("2d", "d2m"),
    "10u": ("10u", "u10"),
    "10v": ("10v", "v10"),
    "tp": ("tp",),
    "sp": ("sp",),
    "ssr": ("ssr",),
    "str": ("str",),
}


class CanonicalRepository(Protocol):
    def get_canonical_product(self, *, canonical_product_id: str) -> dict[str, Any] | None: ...

    def upsert_canonical_product(self, record: Mapping[str, Any]) -> dict[str, Any]: ...

    def list_canonical_products(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
    ) -> Sequence[Any]: ...

    def update_forecast_cycle(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        status: str | None = None,
        manifest_uri: str | None = None,
        retry_count: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any] | None: ...


class CanonicalConversionError(RuntimeError):
    """Raised when canonical conversion cannot complete for a cycle."""


@dataclass(frozen=True)
class CanonicalConverterConfig:
    source_id: str = "gfs"
    workspace_root: Path | str = field(default_factory=lambda: os.getenv("WORKSPACE_ROOT", ".nhms-workspace"))
    object_store_root: Path | str = field(default_factory=lambda: os.getenv("OBJECT_STORE_ROOT", ""))
    object_store_prefix: str = field(default_factory=lambda: os.getenv("OBJECT_STORE_PREFIX", ""))
    converter_version: str = "m1.4"
    grid_id: str = "gfs_0p25"
    grid_definition_uri: str = "canonical/gfs/grid/gfs_0p25/grid.json"
    native_time_resolution: str = "3h"
    native_spatial_resolution: str = "0.25deg"
    variable_mapping: Mapping[str, str] = field(default_factory=lambda: dict(VARIABLE_MAPPING))
    cfgrib_variable_aliases: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: dict(CFGRIB_VARIABLE_ALIASES)
    )

    def __post_init__(self) -> None:
        if not str(self.object_store_root):
            object.__setattr__(self, "object_store_root", self.workspace_root)


@dataclass(frozen=True)
class ERA5CanonicalConverterConfig(CanonicalConverterConfig):
    source_id: str = "ERA5"
    converter_version: str = "m2.0"
    grid_id: str = "era5_0p25"
    grid_definition_uri: str = "canonical/ERA5/grid/era5_0p25/grid.json"
    native_time_resolution: str = "1h"
    native_spatial_resolution: str = "0.25deg"
    variable_mapping: Mapping[str, str] = field(default_factory=lambda: dict(ERA5_VARIABLE_MAPPING))
    cfgrib_variable_aliases: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: dict(CFGRIB_VARIABLE_ALIASES)
    )


@dataclass(frozen=True)
class IFSCanonicalConverterConfig(CanonicalConverterConfig):
    source_id: str = "IFS"
    converter_version: str = "m4.1"
    grid_id: str = "ifs_0p25"
    grid_definition_uri: str = "canonical/IFS/grid/ifs_0p25/grid.json"
    native_time_resolution: str = "3h"
    native_spatial_resolution: str = "0.25deg"
    variable_mapping: Mapping[str, str] = field(default_factory=lambda: dict(IFS_VARIABLE_MAPPING))
    cfgrib_variable_aliases: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: dict(CFGRIB_VARIABLE_ALIASES)
    )


@dataclass(frozen=True)
class RawRecord:
    source_file: str
    native_variable: str
    forecast_hour: int
    values: tuple[float, ...]
    longitudes: tuple[float, ...] = ()
    latitudes: tuple[float, ...] = ()
    shape: tuple[int, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MissingForecastVariable:
    native_variable: str
    standard_variable: str
    forecast_hour: int


@dataclass(frozen=True)
class UnitConversionResult:
    values: tuple[float, ...]
    quality_flag: str = "ok"
    anomalies: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class CanonicalProductResult:
    canonical_product_id: str
    variable: str
    valid_time: datetime
    lead_time_hours: int
    object_uri: str
    checksum: str
    status: str
    quality_flag: str = "ok"
    lineage_json: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConversionResult:
    status: str
    products: tuple[CanonicalProductResult, ...]


@dataclass(frozen=True)
class CanonicalReadinessResult:
    status: str
    ready: bool
    evidence: dict[str, Any]


FORCING_USABLE_CANONICAL_QUALITY_FLAGS = {"ok", "warn"}


def canonical_product_is_forcing_usable(product: Mapping[str, Any]) -> bool:
    quality_flag = str(product.get("quality_flag") or "ok")
    return quality_flag in FORCING_USABLE_CANONICAL_QUALITY_FLAGS and bool(str(product.get("checksum") or "").strip())


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_cycle_time(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return ensure_utc(value)
    candidate = value.strip()
    if len(candidate) == 10 and candidate.isdigit():
        return datetime.strptime(candidate, "%Y%m%d%H").replace(tzinfo=UTC)
    if candidate.endswith("Z"):
        candidate = f"{candidate[:-1]}+00:00"
    return ensure_utc(datetime.fromisoformat(candidate))


def format_cycle_time(value: str | datetime) -> str:
    return parse_cycle_time(value).strftime("%Y%m%d%H")


def map_variable(native_variable: str, mapping: Mapping[str, str] | None = None) -> str | None:
    return dict(mapping or VARIABLE_MAPPING).get(native_variable)


def required_standard_variables_for_source(source_id: str) -> tuple[str, ...]:
    normalized = normalize_source_id(source_id)
    try:
        return REQUIRED_STANDARD_VARIABLES_BY_SOURCE[normalized]
    except KeyError as error:
        raise CanonicalConversionError(f"Unsupported canonical readiness source: {source_id}") from error


def canonical_readiness_source_is_supported(source_id: str) -> bool:
    return normalize_source_id(source_id) in REQUIRED_STANDARD_VARIABLES_BY_SOURCE


def expected_converter_version(source_id: str) -> str:
    """Return the current canonical converter_version for a source.

    Used to detect canonical products written by a stale converter (older or
    missing version) so callers can force a re-conversion before downstream
    stages consume mismatched semantics (e.g. pre-#269 precipitation units).
    """
    sid = normalize_source_id(source_id)
    versions = {
        normalize_source_id("gfs"): CanonicalConverterConfig().converter_version,
        normalize_source_id("ERA5"): ERA5CanonicalConverterConfig().converter_version,
        normalize_source_id("IFS"): IFSCanonicalConverterConfig().converter_version,
    }
    return versions.get(sid, CanonicalConverterConfig().converter_version)


def evaluate_canonical_readiness(
    *,
    source_id: str,
    cycle_time: str | datetime,
    products: Sequence[Any],
    forecast_hours: Sequence[int] | None = None,
    policy_identity: Mapping[str, Any] | None = None,
    source_object_identity: Mapping[str, Any] | None = None,
    canonical_product_id: str | None = None,
    model_id: str | None = None,
    basin_id: str | None = None,
) -> CanonicalReadinessResult:
    normalized_source = normalize_source_id(source_id)
    parsed_cycle_time = parse_cycle_time(cycle_time)
    required_variables = required_standard_variables_for_source(normalized_source)
    cycle_rows: list[dict[str, Any]] = []
    rejected_quality_flags: dict[str, int] = {}
    rejected_quality_samples: list[dict[str, Any]] = []
    checksum_missing_row_count = 0
    checksum_missing_samples: list[dict[str, Any]] = []
    for product in products:
        row = _canonical_readiness_row(product, default_cycle_time=parsed_cycle_time)
        if str(row.get("source_id") or normalized_source) != normalized_source:
            continue
        if parse_cycle_time(row.get("cycle_time", parsed_cycle_time)) != parsed_cycle_time:
            continue
        cycle_rows.append(row)
        quality_flag = str(row.get("quality_flag") or "ok")
        checksum = str(row.get("checksum") or "").strip()
        if quality_flag not in FORCING_USABLE_CANONICAL_QUALITY_FLAGS:
            rejected_quality_flags[quality_flag] = rejected_quality_flags.get(quality_flag, 0) + 1
            if len(rejected_quality_samples) < 10:
                rejected_quality_samples.append(_readiness_rejected_row_sample(row, reason="quality_flag_not_ok"))
        if quality_flag in FORCING_USABLE_CANONICAL_QUALITY_FLAGS and not checksum:
            checksum_missing_row_count += 1
            if len(checksum_missing_samples) < 10:
                checksum_missing_samples.append(_readiness_rejected_row_sample(row, reason="checksum_missing"))
    usable_rows = [row for row in cycle_rows if canonical_product_is_forcing_usable(row)]
    expected_hours = sorted(
        {int(hour) for hour in forecast_hours}
        if forecast_hours is not None
        else {int(row["lead_time_hours"]) for row in usable_rows if row.get("lead_time_hours") is not None}
    )
    variables_by_hour: dict[int, set[str]] = {hour: set() for hour in expected_hours}
    counts_by_variable = {variable: 0 for variable in required_variables}
    lead_counts_by_valid_time: dict[str, int] = {}
    object_identities: set[str] = set()
    policy_identities: set[str] = set()
    identity_rejected_row_count = 0
    missing_policy_identity_row_count = 0
    missing_source_object_identity_row_count = 0
    missing_required_lineage_row_count = 0

    expected_policy_id = _stable_identity(policy_identity)
    expected_object_id = _stable_identity(source_object_identity)

    for row in usable_rows:
        variable = str(row.get("variable") or "")
        if variable not in required_variables:
            continue
        try:
            lead_hour = int(row["lead_time_hours"])
        except (KeyError, TypeError, ValueError):
            continue
        if expected_hours and lead_hour not in variables_by_hour:
            continue
        lineage = _mapping_value(row.get("lineage_json"))
        row_policy = _stable_identity(
            lineage.get("policy_identity") or lineage.get("source_policy") or lineage.get("canonical_policy_identity")
        )
        row_object = _stable_identity(
            lineage.get("source_object_identity") or lineage.get("source_identity") or lineage.get("object_identity")
        )
        missing_required_lineage = False
        if expected_policy_id and not row_policy:
            missing_policy_identity_row_count += 1
            missing_required_lineage = True
        if expected_object_id and not row_object:
            missing_source_object_identity_row_count += 1
            missing_required_lineage = True
        if (expected_policy_id and row_policy != expected_policy_id) or (
            expected_object_id and row_object != expected_object_id
        ):
            identity_rejected_row_count += 1
            if missing_required_lineage:
                missing_required_lineage_row_count += 1
            continue
        if row_policy:
            policy_identities.add(row_policy)
        if row_object:
            object_identities.add(row_object)
        variables_by_hour.setdefault(lead_hour, set()).add(variable)
        counts_by_variable[variable] = counts_by_variable.get(variable, 0) + 1
        valid_time = row.get("valid_time")
        valid_time_text = parse_cycle_time(valid_time).isoformat() if isinstance(valid_time, str | datetime) else ""
        if valid_time_text:
            lead_counts_by_valid_time[valid_time_text] = lead_counts_by_valid_time.get(valid_time_text, 0) + 1

    missing_variables = [variable for variable in required_variables if counts_by_variable.get(variable, 0) == 0]
    missing_leads = []
    for lead_hour in expected_hours:
        required_for_lead = set(required_variables)
        if normalized_source == "gfs" and lead_hour == 0:
            required_for_lead -= set(GFS_F000_OPTIONAL_INTERVAL_STANDARD_VARIABLES)
        present_for_lead = variables_by_hour.get(lead_hour, set())
        if not required_for_lead.issubset(present_for_lead):
            missing_leads.append(
                {
                    "lead_time_hours": lead_hour,
                    "valid_time": (parsed_cycle_time + timedelta(hours=lead_hour)).isoformat(),
                    "missing_variables": sorted(required_for_lead - present_for_lead),
                    "present_variable_count": len(present_for_lead),
                    "required_variable_count": len(required_for_lead),
                }
            )
    identity_mismatch = bool(
        (expected_policy_id and not policy_identities) or (expected_object_id and not object_identities)
        or (identity_rejected_row_count and (missing_variables or missing_leads))
    )
    ready = bool(expected_hours) and not missing_variables and not missing_leads and not identity_mismatch
    status = "canonical_ready" if ready else "canonical_incomplete"
    unusable_required_row_count = sum(rejected_quality_flags.values()) + checksum_missing_row_count
    evidence = {
        "source": normalized_source,
        "source_id": normalized_source,
        "cycle_time": parsed_cycle_time.isoformat(),
        "status": status,
        "ready": ready,
        "canonical_product_id": canonical_product_id
        or f"canon_{normalized_source.lower()}_{format_cycle_time(parsed_cycle_time)}",
        "model_id": model_id,
        "basin_id": basin_id,
        "required_variables": list(required_variables),
        "present_variables": sorted(variable for variable, count in counts_by_variable.items() if count > 0),
        "missing_variables": missing_variables,
        "expected_leads": expected_hours,
        "accepted_horizon": {
            "first_lead_hour": min(expected_hours) if expected_hours else None,
            "last_lead_hour": max(expected_hours) if expected_hours else None,
            "lead_count": len(expected_hours),
        },
        "per_valid_time_lead_counts": lead_counts_by_valid_time,
        "missing_leads": missing_leads,
        "row_count": len(usable_rows),
        "candidate_row_count": len(cycle_rows),
        "rejected_quality_flags": rejected_quality_flags,
        "rejected_quality_samples": rejected_quality_samples,
        "checksum_missing_row_count": checksum_missing_row_count,
        "checksum_missing_samples": checksum_missing_samples,
        "policy_identity": dict(policy_identity or {}),
        "source_object_identity": dict(source_object_identity or {}),
        "policy_identity_matched": not expected_policy_id or bool(policy_identities),
        "source_object_identity_matched": not expected_object_id or bool(object_identities),
        "identity_rejected_row_count": identity_rejected_row_count,
        "missing_policy_identity_row_count": missing_policy_identity_row_count,
        "missing_source_object_identity_row_count": missing_source_object_identity_row_count,
        "missing_required_lineage_row_count": missing_required_lineage_row_count,
        "reused_existing_ready": ready,
    }
    if identity_mismatch:
        if (
            identity_rejected_row_count > 0
            and missing_required_lineage_row_count == identity_rejected_row_count
            and not (policy_identities and object_identities)
        ):
            evidence["reason"] = "canonical_lineage_missing"
        elif not usable_rows and unusable_required_row_count > 0:
            evidence["reason"] = "missing_canonical_variables" if missing_variables else "missing_canonical_leads"
        else:
            evidence["reason"] = "canonical_identity_mismatch"
    elif missing_variables:
        evidence["reason"] = "missing_canonical_variables"
    elif missing_leads:
        evidence["reason"] = "missing_canonical_leads"
    elif not expected_hours:
        evidence["reason"] = "no_expected_leads"
    return CanonicalReadinessResult(status=status, ready=ready, evidence=evidence)


def _canonical_readiness_row(product: Any, *, default_cycle_time: datetime) -> dict[str, Any]:
    if isinstance(product, Mapping):
        return dict(product)
    if is_dataclass(product) and not isinstance(product, type):
        row = {dataclass_field.name: getattr(product, dataclass_field.name) for dataclass_field in fields(product)}
    else:
        row = {}
        for key in (
            "canonical_product_id",
            "source_id",
            "cycle_time",
            "valid_time",
            "lead_time_hours",
            "variable",
            "object_uri",
            "checksum",
            "quality_flag",
            "lineage_json",
        ):
            if hasattr(product, key):
                row[key] = getattr(product, key)
    if row.get("lead_time_hours") is None and row.get("valid_time") is not None:
        cycle_time = parse_cycle_time(row.get("cycle_time", default_cycle_time))
        valid_time = parse_cycle_time(row["valid_time"])
        row["lead_time_hours"] = int((valid_time - cycle_time).total_seconds() // 3600)
    return row


def _readiness_rejected_row_sample(row: Mapping[str, Any], *, reason: str) -> dict[str, Any]:
    sample: dict[str, Any] = {
        "reason": reason,
        "variable": str(row.get("variable") or ""),
        "quality_flag": str(row.get("quality_flag") or "ok"),
    }
    if row.get("lead_time_hours") is not None:
        try:
            sample["lead_time_hours"] = int(row["lead_time_hours"])
        except (TypeError, ValueError):
            sample["lead_time_hours"] = row.get("lead_time_hours")
    if row.get("valid_time") is not None:
        try:
            sample["valid_time"] = parse_cycle_time(row["valid_time"]).isoformat()
        except (TypeError, ValueError):
            sample["valid_time"] = str(row.get("valid_time"))
    return sample


def _canonical_product_result_readiness_row(
    product: CanonicalProductResult,
    *,
    source_id: str,
    cycle_time: datetime,
) -> dict[str, Any]:
    return {
        "canonical_product_id": product.canonical_product_id,
        "source_id": source_id,
        "cycle_time": cycle_time,
        "valid_time": product.valid_time,
        "lead_time_hours": product.lead_time_hours,
        "variable": product.variable,
        "object_uri": product.object_uri,
        "checksum": product.checksum,
        "quality_flag": product.quality_flag,
        "lineage_json": dict(product.lineage_json),
    }


def _canonical_readiness_error_message(evidence: Mapping[str, Any]) -> str:
    reason = str(evidence.get("reason") or "canonical_incomplete")
    details: dict[str, Any] = {"reason": reason}
    for key in (
        "missing_variables",
        "missing_leads",
        "rejected_quality_flags",
        "checksum_missing_row_count",
        "identity_rejected_row_count",
        "missing_required_lineage_row_count",
    ):
        value = evidence.get(key)
        if value not in (None, [], {}, 0):
            details[key] = value
    return json.dumps(details, sort_keys=True, default=str)


def _stable_identity(value: Mapping[str, Any] | None) -> str:
    if not value:
        return ""
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"), default=str)


def _mapping_value(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _apcp_selector_metadata(metadata: Mapping[str, Any]) -> Mapping[str, Any]:
    selector = metadata.get("idx_selector")
    return selector if isinstance(selector, Mapping) else metadata


def _apcp_accumulation_type_from_metadata(metadata: Mapping[str, Any]) -> str | None:
    selector = _apcp_selector_metadata(metadata)
    value = selector.get("accumulation_type")
    if value is None:
        value = selector.get("accumulation_policy")
    if value is None:
        return None
    parsed = str(value)
    if parsed in {"cumulative_since_cycle", "interval_bucket"}:
        return parsed
    return None


def _apcp_step_range_from_metadata(metadata: Mapping[str, Any]) -> str | None:
    selector = _apcp_selector_metadata(metadata)
    value = selector.get("step_range") or selector.get("stepRange")
    if value is None:
        return None
    return str(value)


def _coord_values_by_name(dataset: Any, names: tuple[str, ...]) -> tuple[float, ...]:
    for name in names:
        if name in dataset.coords:
            values = dataset[name].values.ravel().tolist()
            return tuple(float(value) for value in values)
    return ()


def unit_for_standard_variable(standard_variable: str) -> str:
    try:
        return STANDARD_UNITS[standard_variable]
    except KeyError as error:
        raise CanonicalConversionError(f"No standard unit configured for {standard_variable}") from error


def convert_units(
    native_variable: str,
    values: tuple[float, ...] | list[float],
    previous_values: tuple[float, ...] | list[float] | None = None,
) -> tuple[float, ...]:
    return convert_units_with_metadata(native_variable, values, previous_values).values


def convert_units_with_metadata(
    native_variable: str,
    values: tuple[float, ...] | list[float],
    previous_values: tuple[float, ...] | list[float] | None = None,
    *,
    forecast_hour: int | None = None,
    previous_forecast_hour: int | None = None,
    accumulation_type: str | None = None,
    step_range: str | None = None,
) -> UnitConversionResult:
    current = tuple(float(value) for value in values)
    if native_variable in {"tmp2m", "2m_temperature", "2m_dewpoint_temperature", "2t", "2d"}:
        return UnitConversionResult(tuple(value - 273.15 for value in current))
    if native_variable == "apcp":
        if accumulation_type == "interval_bucket":
            _validate_finite_values(current, "APCP precipitation")
            step_hours = _step_hours_from_step_range(step_range)
            negative_values = tuple(value for value in current if value < 0.0)
            anomalies_list: list[dict[str, Any]] = []
            quality_flag = "ok"
            if negative_values:
                _tol = PRECIP_NEGATIVE_NOISE_TOLERANCE_MM
                small_negatives = tuple(value for value in negative_values if -_tol < value < 0.0)
                significant_negatives = tuple(value for value in negative_values if value <= -_tol)
                if small_negatives:
                    anomalies_list.append(
                        {
                            "type": "small_negative_apcp_bucket",
                            "forecast_hour": forecast_hour,
                            "step_range": step_range,
                            "negative_count": len(small_negatives),
                            "min_delta": min(small_negatives),
                        }
                    )
                if significant_negatives:
                    quality_flag = "warn"
                    anomalies_list.append(
                        {
                            "type": "negative_apcp_bucket",
                            "forecast_hour": forecast_hour,
                            "step_range": step_range,
                            "negative_count": len(significant_negatives),
                            "min_delta": min(significant_negatives),
                        }
                    )
            mm_per_day = tuple(max(0.0, value) * 24.0 / step_hours for value in current)
            return UnitConversionResult(mm_per_day, quality_flag, tuple(anomalies_list))

        previous = (
            tuple(float(value) for value in previous_values) if previous_values is not None else (0.0,) * len(current)
        )
        if len(previous) != len(current):
            raise CanonicalConversionError("APCP previous/current value arrays must have the same length.")
        _validate_finite_values((*current, *previous), "APCP precipitation")
        # The GFS adapter resolves FV3-GFS duplicate APCP records to the 0-fhr
        # cumulative-since-cycle record. Canonical precipitation is therefore a
        # straight de-accumulation against the previous lead.
        deltas = tuple(current_value - previous_value for current_value, previous_value in zip(current, previous))
        # 量化噪声级微小负 delta(|δ|<容差)按 SHUD precip<0.0001mm/day→0 的钳零+量化约定
        # 与 0 等价,记 anomaly 但保持 quality_flag=ok(对齐 IFS 降水 small/significant 处理);
        # 显著负值才标 warn。容差用 PRECIP_NEGATIVE_NOISE_TOLERANCE_MM 以覆盖 GRIB 量化步长。
        _tol = PRECIP_NEGATIVE_NOISE_TOLERANCE_MM
        small_negatives = tuple(delta for delta in deltas if -_tol < delta < 0.0)
        significant_negatives = tuple(delta for delta in deltas if delta <= -_tol)
        anomalies_list: list[dict[str, Any]] = []
        quality_flag = "ok"
        if small_negatives:
            anomalies_list.append(
                {
                    "type": "small_negative_apcp_delta",
                    "forecast_hour": forecast_hour,
                    "previous_forecast_hour": previous_forecast_hour,
                    "negative_count": len(small_negatives),
                    "min_delta": min(small_negatives),
                }
            )
        if significant_negatives:
            quality_flag = "warn"
            anomalies_list.append(
                {
                    "type": "negative_apcp_delta",
                    "forecast_hour": forecast_hour,
                    "previous_forecast_hour": previous_forecast_hour,
                    "negative_count": len(significant_negatives),
                    "min_delta": min(significant_negatives),
                }
            )
        anomalies: tuple[dict[str, Any], ...] = tuple(anomalies_list)
        # On the first frame (previous=None) the smallest forecast hour may be >0
        # when GFS_FORECAST_START_HOUR is configured; use the full 0->fh span rather
        # than the shared _step_hours default of 1.0.
        if previous_forecast_hour is None and forecast_hour and forecast_hour > 0:
            step_hours = float(forecast_hour)
        else:
            step_hours = _step_hours(forecast_hour, previous_forecast_hour)
        mm_per_day = tuple(max(0.0, delta) * 24.0 / step_hours for delta in deltas)
        return UnitConversionResult(mm_per_day, quality_flag, anomalies)
    if native_variable == "total_precipitation":
        return convert_era5_precipitation_with_metadata(
            current,
            previous_values,
            forecast_hour=forecast_hour,
            previous_forecast_hour=previous_forecast_hour,
        )
    if native_variable == "rh2m":
        # canonical 单位为分数 0-1;GRIB rh2m 常含过饱和 >100%,按 SHUD 模型(rh 钳 [0,1])
        # 与 IFS RH 路径一致钳到 [0,1],避免 canonical 产品越界声明单位。
        return UnitConversionResult(tuple(clamp(value / 100.0, 0.0, 1.0) for value in current))
    return UnitConversionResult(current)


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def compute_relative_humidity(temperature_c: float, dewpoint_c: float) -> float:
    e_s = 6.112 * math.exp(17.67 * temperature_c / (temperature_c + 243.5))
    e_d = 6.112 * math.exp(17.67 * dewpoint_c / (dewpoint_c + 243.5))
    return clamp(e_d / e_s, 0.0, 1.0)


def compute_relative_humidity_values(
    temperature_c: tuple[float, ...] | list[float],
    dewpoint_c: tuple[float, ...] | list[float],
) -> tuple[float, ...]:
    if len(temperature_c) != len(dewpoint_c):
        raise CanonicalConversionError("Temperature and dewpoint arrays must have the same length.")
    return tuple(compute_relative_humidity(float(t), float(td)) for t, td in zip(temperature_c, dewpoint_c))


def convert_era5_precipitation_with_metadata(
    values_m: tuple[float, ...] | list[float],
    previous_values_m: tuple[float, ...] | list[float] | None = None,
    *,
    forecast_hour: int | None = None,
    previous_forecast_hour: int | None = None,
) -> UnitConversionResult:
    current = tuple(float(value) for value in values_m)
    previous = (
        tuple(float(value) for value in previous_values_m) if previous_values_m is not None else (0.0,) * len(current)
    )
    if len(previous) != len(current):
        raise CanonicalConversionError("ERA5 precipitation previous/current arrays must have the same length.")
    _validate_finite_values((*current, *previous), "ERA5 precipitation")

    step_hours = _step_hours(forecast_hour, previous_forecast_hour)
    deltas = tuple(current_value - previous_value for current_value, previous_value in zip(current, previous))
    negative_deltas = tuple(delta for delta in deltas if delta < 0.0)
    anomalies: tuple[dict[str, Any], ...] = ()
    quality_flag = "ok"
    if negative_deltas:
        quality_flag = "warn"
        anomalies = (
            {
                "type": "negative_era5_precipitation_delta",
                "forecast_hour": forecast_hour,
                "previous_forecast_hour": previous_forecast_hour,
                "negative_count": len(negative_deltas),
                "min_delta_m": min(negative_deltas),
            },
        )

    mm_per_day = tuple(max(0.0, delta) * 1000.0 * 24.0 / step_hours for delta in deltas)
    return UnitConversionResult(mm_per_day, quality_flag, anomalies)


def convert_era5_radiation_values(
    ssr_values: tuple[float, ...] | list[float],
    str_values: tuple[float, ...] | list[float],
    previous_ssr_values: tuple[float, ...] | list[float] | None = None,
    previous_str_values: tuple[float, ...] | list[float] | None = None,
    *,
    forecast_hour: int | None = None,
    previous_forecast_hour: int | None = None,
) -> tuple[float, ...]:
    ssr = tuple(float(value) for value in ssr_values)
    str_ = tuple(float(value) for value in str_values)
    previous_ssr = (
        tuple(float(value) for value in previous_ssr_values) if previous_ssr_values is not None else (0.0,) * len(ssr)
    )
    previous_str = (
        tuple(float(value) for value in previous_str_values) if previous_str_values is not None else (0.0,) * len(str_)
    )
    lengths = {len(ssr), len(str_), len(previous_ssr), len(previous_str)}
    if len(lengths) != 1:
        raise CanonicalConversionError("ERA5 radiation arrays must have the same length.")
    _validate_finite_values((*ssr, *str_, *previous_ssr, *previous_str), "ERA5 radiation")

    step_seconds = _step_hours(forecast_hour, previous_forecast_hour) * 3600.0
    return tuple(
        ((current_ssr - prior_ssr) + (current_str - prior_str)) / step_seconds
        for current_ssr, prior_ssr, current_str, prior_str in zip(ssr, previous_ssr, str_, previous_str)
    )


def compute_ifs_relative_humidity(temperature_c: float, dewpoint_c: float) -> float:
    e_s = math.exp(17.625 * temperature_c / (243.04 + temperature_c))
    e_d = math.exp(17.625 * dewpoint_c / (243.04 + dewpoint_c))
    return clamp(e_d / e_s, 0.0, 1.0)


def compute_ifs_relative_humidity_values(
    temperature_c: tuple[float, ...] | list[float],
    dewpoint_c: tuple[float, ...] | list[float],
) -> tuple[float, ...]:
    if len(temperature_c) != len(dewpoint_c):
        raise CanonicalConversionError("IFS temperature and dewpoint arrays must have the same length.")
    return tuple(compute_ifs_relative_humidity(float(t), float(td)) for t, td in zip(temperature_c, dewpoint_c))


def convert_ifs_precipitation_with_metadata(
    values_m: tuple[float, ...] | list[float],
    previous_values_m: tuple[float, ...] | list[float] | None = None,
    *,
    forecast_hour: int | None = None,
    previous_forecast_hour: int | None = None,
    consecutive_negative_count: int = 0,
) -> tuple[UnitConversionResult, int, float]:
    current = tuple(float(value) for value in values_m)
    previous = (
        tuple(float(value) for value in previous_values_m) if previous_values_m is not None else (0.0,) * len(current)
    )
    if len(previous) != len(current):
        raise CanonicalConversionError("IFS precipitation previous/current arrays must have the same length.")
    _validate_finite_values((*current, *previous), "IFS precipitation")

    step_hours = _ifs_step_hours(forecast_hour, previous_forecast_hour)
    deltas_mm = tuple(
        (current_value - previous_value) * 1000.0 for current_value, previous_value in zip(current, previous)
    )
    # 容差用 PRECIP_NEGATIVE_NOISE_TOLERANCE_MM 覆盖 IFS tp 的 GRIB 量化步长(≈0.0153mm),
    # 否则量化噪声被判 significant→warning_negative_precip→被 forcing 当不可用剔除致缺产品。
    _tol = PRECIP_NEGATIVE_NOISE_TOLERANCE_MM
    small_negatives = tuple(delta for delta in deltas_mm if -_tol < delta < 0.0)
    significant_negatives = tuple(delta for delta in deltas_mm if delta <= -_tol)
    anomalies: list[dict[str, Any]] = []
    next_consecutive_negative_count = 0
    quality_flag = "ok"

    if small_negatives:
        anomalies.append(
            {
                "type": "small_negative_ifs_precipitation_delta",
                "forecast_hour": forecast_hour,
                "previous_forecast_hour": previous_forecast_hour,
                "negative_count": len(small_negatives),
                "min_delta_mm": min(small_negatives),
            }
        )
    if significant_negatives:
        next_consecutive_negative_count = consecutive_negative_count + 1
        quality_flag = "warning_negative_precip"
        if next_consecutive_negative_count >= 3:
            quality_flag = "error_precip_accumulation"
        anomalies.append(
            {
                "type": "negative_ifs_precipitation_delta",
                "forecast_hour": forecast_hour,
                "previous_forecast_hour": previous_forecast_hour,
                "negative_count": len(significant_negatives),
                "min_delta_mm": min(significant_negatives),
                "consecutive_negative_count": next_consecutive_negative_count,
            }
        )

    values = tuple(max(0.0, delta) * 24.0 / step_hours for delta in deltas_mm)
    return UnitConversionResult(values, quality_flag, tuple(anomalies)), next_consecutive_negative_count, step_hours


def convert_ifs_radiation_values(
    ssr_values: tuple[float, ...] | list[float],
    str_values: tuple[float, ...] | list[float],
    previous_ssr_values: tuple[float, ...] | list[float] | None = None,
    previous_str_values: tuple[float, ...] | list[float] | None = None,
    *,
    forecast_hour: int | None = None,
    previous_forecast_hour: int | None = None,
) -> tuple[tuple[float, ...], float]:
    ssr = _finite_float_tuple(ssr_values, "IFS ssr")
    str_ = _finite_float_tuple(str_values, "IFS str")
    previous_ssr = (
        _finite_float_tuple(previous_ssr_values, "previous IFS ssr")
        if previous_ssr_values is not None
        else (0.0,) * len(ssr)
    )
    previous_str = (
        _finite_float_tuple(previous_str_values, "previous IFS str")
        if previous_str_values is not None
        else (0.0,) * len(str_)
    )
    lengths = {len(ssr), len(str_), len(previous_ssr), len(previous_str)}
    if len(lengths) != 1:
        raise CanonicalConversionError("IFS radiation arrays must have the same length.")

    step_hours = _ifs_step_hours(forecast_hour, previous_forecast_hour)
    step_seconds = step_hours * 3600.0
    values: list[float] = []
    for current_ssr, prior_ssr, current_str, prior_str in zip(ssr, previous_ssr, str_, previous_str):
        ssr_delta = current_ssr - prior_ssr
        str_delta = current_str - prior_str
        if not math.isfinite(ssr_delta) or not math.isfinite(str_delta):
            raise CanonicalConversionError("IFS radiation deltas must be finite.")
        values.append((ssr_delta + str_delta) / step_seconds)
    return tuple(values), step_hours


def convert_ifs_shortwave_down_values(
    ssr_values: tuple[float, ...] | list[float],
    previous_ssr_values: tuple[float, ...] | list[float] | None = None,
    *,
    forecast_hour: int | None = None,
    previous_forecast_hour: int | None = None,
) -> tuple[UnitConversionResult, float]:
    ssr = _finite_float_tuple(ssr_values, "IFS ssr")
    previous_ssr = (
        _finite_float_tuple(previous_ssr_values, "previous IFS ssr")
        if previous_ssr_values is not None
        else (0.0,) * len(ssr)
    )
    if len(ssr) != len(previous_ssr):
        raise CanonicalConversionError("IFS shortwave radiation arrays must have the same length.")

    step_hours = _ifs_step_hours(forecast_hour, previous_forecast_hour)
    step_seconds = step_hours * 3600.0
    values: list[float] = []
    negative_deltas: list[float] = []
    small_negative_deltas: list[float] = []
    for current_ssr, prior_ssr in zip(ssr, previous_ssr):
        delta = current_ssr - prior_ssr
        if not math.isfinite(delta):
            raise CanonicalConversionError("IFS shortwave radiation deltas must be finite.")
        rate = delta / step_seconds
        # 只有负速率超过量化噪声容差才算真异常(标 warn);夜间持平段的亚阈值伪负值
        # 记 anomaly 但保持 quality_flag=ok(对齐 IFS 降水 small/significant 处理)。
        if rate < -IFS_SHORTWAVE_NEGATIVE_TOLERANCE_W_M2:
            negative_deltas.append(delta)
        elif delta < 0.0:
            small_negative_deltas.append(delta)
        values.append(max(0.0, rate))
    anomalies_list: list[dict[str, Any]] = []
    quality_flag = "ok"
    if small_negative_deltas:
        anomalies_list.append(
            {
                "type": "small_negative_ifs_shortwave_delta",
                "forecast_hour": forecast_hour,
                "previous_forecast_hour": previous_forecast_hour,
                "negative_count": len(small_negative_deltas),
                "min_delta_j_m2": min(small_negative_deltas),
            }
        )
    if negative_deltas:
        quality_flag = "warn"
        anomalies_list.append(
            {
                "type": "negative_ifs_shortwave_delta",
                "forecast_hour": forecast_hour,
                "previous_forecast_hour": previous_forecast_hour,
                "negative_count": len(negative_deltas),
                "min_delta_j_m2": min(negative_deltas),
            }
        )
    return UnitConversionResult(tuple(values), quality_flag, tuple(anomalies_list)), step_hours


def _finite_float_tuple(values: Iterable[float], label: str) -> tuple[float, ...]:
    parsed = tuple(float(value) for value in values)
    _validate_finite_values(parsed, label)
    return parsed


def _validate_finite_values(values: Iterable[float], label: str) -> None:
    if not all(math.isfinite(value) for value in values):
        raise CanonicalConversionError(f"{label} values must be finite.")


def _step_hours(forecast_hour: int | None, previous_forecast_hour: int | None) -> float:
    if forecast_hour is None or previous_forecast_hour is None:
        return 1.0
    return float(max(1, forecast_hour - previous_forecast_hour))


def _step_hours_from_step_range(step_range: str | None) -> float:
    if not step_range:
        raise CanonicalConversionError("APCP interval bucket conversion requires step_range metadata.")
    start_text, separator, end_text = step_range.partition("-")
    if not separator:
        raise CanonicalConversionError(f"Invalid APCP step_range metadata: {step_range!r}.")
    try:
        start = int(start_text)
        end = int(end_text)
    except ValueError as error:
        raise CanonicalConversionError(f"Invalid APCP step_range metadata: {step_range!r}.") from error
    if end <= start:
        raise CanonicalConversionError(f"Invalid APCP step_range metadata: {step_range!r}.")
    return float(end - start)


def _normalize_longitude(longitude: float) -> float:
    value = float(longitude)
    while value > 180.0:
        value -= 360.0
    while value < -180.0:
        value += 360.0
    return value


def _grid_definition_signature(definition: Mapping[str, Any]) -> tuple[Any, ...]:
    if definition.get("layout") == "rectilinear":
        try:
            shape = tuple(int(value) for value in definition["shape"])
            longitudes = tuple(round(_normalize_longitude(float(value)), 12) for value in definition["longitudes"])
            latitudes = tuple(round(float(value), 12) for value in definition["latitudes"])
        except (KeyError, TypeError, ValueError) as error:
            raise CanonicalConversionError("Grid definition is invalid.") from error
        return ("rectilinear", shape, longitudes, latitudes)

    cells = definition.get("cells") or definition.get("points")
    if isinstance(cells, list):
        signature: list[tuple[str, float, float]] = []
        for index, cell in enumerate(cells):
            if not isinstance(cell, Mapping):
                raise CanonicalConversionError("Grid definition cell is invalid.")
            try:
                longitude = _normalize_longitude(float(cell.get("lon", cell.get("longitude"))))
                latitude = float(cell.get("lat", cell.get("latitude")))
            except (TypeError, ValueError) as error:
                raise CanonicalConversionError("Grid definition cell coordinates are invalid.") from error
            signature.append(
                (
                    str(cell.get("grid_cell_id", cell.get("id", index))),
                    round(longitude, 12),
                    round(latitude, 12),
                )
            )
        return ("cells", tuple(signature))

    raise CanonicalConversionError("Grid definition layout is unsupported.")


def _ifs_step_hours(forecast_hour: int | None, previous_forecast_hour: int | None) -> float:
    if forecast_hour is None:
        return 3.0
    if previous_forecast_hour is None:
        return float(forecast_hour) if forecast_hour > 0 else 3.0
    return float(max(1, forecast_hour - previous_forecast_hour))


def compute_time_axis(cycle_time: str | datetime, forecast_hours: list[int]) -> list[dict[str, Any]]:
    parsed_cycle_time = parse_cycle_time(cycle_time)
    return [
        {
            "valid_time": parsed_cycle_time + timedelta(hours=forecast_hour),
            "lead_time_hours": forecast_hour,
        }
        for forecast_hour in forecast_hours
    ]


class CanonicalConverter:
    def __init__(
        self,
        *,
        config: CanonicalConverterConfig | None = None,
        repository: CanonicalRepository | None = None,
        object_store: LocalObjectStore | None = None,
    ) -> None:
        self.config = config or CanonicalConverterConfig()
        self.repository = repository
        self.object_store = object_store or LocalObjectStore(
            self.config.object_store_root,
            object_store_prefix=self.config.object_store_prefix,
        )

    @classmethod
    def from_env(cls) -> CanonicalConverter:
        config = CanonicalConverterConfig()
        return cls(config=config, repository=PsycopgMetStore.from_env())

    def load_manifest(self, manifest_uri: str) -> dict[str, Any]:
        try:
            return json.loads(self.object_store.read_bytes(manifest_uri).decode("utf-8"))
        except (json.JSONDecodeError, OSError, ObjectStoreError, ValueError) as error:
            raise CanonicalConversionError(f"Failed to load manifest {manifest_uri}: {error}") from error

    def convert_manifest(self, manifest: Any) -> ConversionResult:
        cycle_time = parse_cycle_time(_manifest_value(manifest, "cycle_time"))
        source_id = _manifest_value(manifest, "source_id")
        if source_id != self.config.source_id:
            raise CanonicalConversionError(
                f"Manifest source_id {source_id!r} does not match converter source_id {self.config.source_id!r}."
            )

        try:
            entries = _manifest_entries(manifest)
            covered_pairs = self._covered_required_pairs(entries)
            missing_pairs = self._missing_required_pairs_from_covered(manifest, entries, covered_pairs)
            if missing_pairs:
                self._record_missing_products(source_id, cycle_time, missing_pairs)
                raise CanonicalConversionError(self._missing_pairs_message(missing_pairs))

            entries_by_standard_variable = self._entries_by_standard_variable(entries)
            missing_variables = sorted(set(self.required_standard_variables()) - set(entries_by_standard_variable))
            if missing_variables:
                raise CanonicalConversionError(f"Missing required canonical variables: {', '.join(missing_variables)}")

            manifest_metadata = _manifest_metadata(manifest)
            products: list[CanonicalProductResult] = []
            for standard_variable in sorted(entries_by_standard_variable):
                native_entries = sorted(
                    entries_by_standard_variable[standard_variable],
                    key=lambda entry: int(entry["forecast_hour"]),
                )
                previous_values: tuple[float, ...] | None = None
                previous_source_file: str | None = None
                previous_forecast_hour: int | None = None
                apcp_cumulative_gap = False
                for entry in native_entries:
                    record = self._read_record(entry)
                    apcp_accumulation_type = _apcp_accumulation_type_from_metadata(record.metadata)
                    if (
                        record.native_variable == "apcp"
                        and apcp_accumulation_type == "cumulative_since_cycle"
                        and apcp_cumulative_gap
                    ):
                        raise CanonicalConversionError(
                            (
                                "Cannot convert GFS APCP cumulative record after an interval-bucket gap "
                                f"at f{record.forecast_hour:03d}; exact interval de-accumulation would be ambiguous."
                            )
                        )
                    product = self._convert_record(
                        source_id=source_id,
                        cycle_time=cycle_time,
                        standard_variable=standard_variable,
                        record=record,
                        previous_values=previous_values,
                        previous_source_file=previous_source_file,
                        previous_forecast_hour=previous_forecast_hour,
                        policy_identity=_mapping_value(manifest_metadata.get("source_policy")),
                        source_object_identity=_mapping_value(manifest_metadata.get("source_object_identity")),
                    )
                    products.append(product)
                    if record.native_variable == "apcp" and apcp_accumulation_type == "interval_bucket":
                        previous_values = None
                        previous_source_file = None
                        previous_forecast_hour = None
                        apcp_cumulative_gap = True
                        continue
                    previous_values = record.values
                    previous_source_file = record.source_file
                    previous_forecast_hour = record.forecast_hour
                    if record.native_variable == "apcp":
                        apcp_cumulative_gap = False

            return self._complete_cycle_after_conversion(
                source_id=source_id,
                cycle_time=cycle_time,
                products=products,
                forecast_hours=self._configured_forecast_hours(manifest, entries),
                policy_identity=_mapping_value(manifest_metadata.get("source_policy")),
                source_object_identity=_mapping_value(manifest_metadata.get("source_object_identity")),
            )
        except Exception as error:
            try:
                self._update_cycle_status(
                    cycle_time,
                    status="failed_convert",
                    error_code="CONVERT_FAILED",
                    error_message=str(error),
                )
            except Exception:
                LOGGER.exception(
                    "Failed to record CONVERT_FAILED status for %s; preserving original conversion error",
                    format_cycle_time(cycle_time),
                )
            raise error

    def convert_manifest_uri(self, manifest_uri: str) -> ConversionResult:
        return self.convert_manifest(self.load_manifest(manifest_uri))

    def canonical_readiness(
        self,
        *,
        cycle_time: str | datetime,
        forecast_hours: Sequence[int] | None = None,
        policy_identity: Mapping[str, Any] | None = None,
        source_object_identity: Mapping[str, Any] | None = None,
        canonical_product_id: str | None = None,
        model_id: str | None = None,
        basin_id: str | None = None,
    ) -> CanonicalReadinessResult:
        if self.repository is None:
            return evaluate_canonical_readiness(
                source_id=self.config.source_id,
                cycle_time=cycle_time,
                products=(),
                forecast_hours=forecast_hours,
                policy_identity=policy_identity,
                source_object_identity=source_object_identity,
                canonical_product_id=canonical_product_id,
                model_id=model_id,
                basin_id=basin_id,
            )
        parsed_cycle_time = parse_cycle_time(cycle_time)
        products = self.repository.list_canonical_products(
            source_id=self.config.source_id,
            cycle_time=parsed_cycle_time,
        )
        return evaluate_canonical_readiness(
            source_id=self.config.source_id,
            cycle_time=parsed_cycle_time,
            products=products,
            forecast_hours=forecast_hours,
            policy_identity=policy_identity,
            source_object_identity=source_object_identity,
            canonical_product_id=canonical_product_id,
            model_id=model_id,
            basin_id=basin_id,
        )

    def _complete_cycle_after_conversion(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        products: Sequence[CanonicalProductResult],
        forecast_hours: Sequence[int],
        policy_identity: Mapping[str, Any] | None = None,
        source_object_identity: Mapping[str, Any] | None = None,
    ) -> ConversionResult:
        list_products = getattr(self.repository, "list_canonical_products", None) if self.repository else None
        rows = (
            list_products(source_id=source_id, cycle_time=cycle_time)
            if callable(list_products)
            else [
                _canonical_product_result_readiness_row(product, source_id=source_id, cycle_time=cycle_time)
                for product in products
            ]
        )
        readiness = evaluate_canonical_readiness(
            source_id=source_id,
            cycle_time=cycle_time,
            products=rows,
            forecast_hours=forecast_hours,
            policy_identity=policy_identity,
            source_object_identity=source_object_identity,
            canonical_product_id=f"canon_{normalize_source_id(source_id).lower()}_{format_cycle_time(cycle_time)}",
        )
        error_code = "" if readiness.ready else "CANONICAL_INCOMPLETE"
        error_message = "" if readiness.ready else _canonical_readiness_error_message(readiness.evidence)
        self._update_cycle_status(
            cycle_time,
            status=readiness.status,
            error_code=error_code,
            error_message=error_message,
        )
        return ConversionResult(status=readiness.status, products=tuple(products))

    def _read_records(self, entries: list[dict[str, Any]]) -> list[RawRecord]:
        records: list[RawRecord] = []
        for entry in entries:
            native_variable = entry["variable"]
            standard_variable = map_variable(native_variable, self.config.variable_mapping)
            if standard_variable is None:
                LOGGER.warning("Skipping unmapped variable %s from %s", native_variable, entry["local_key"])
                continue
            records.append(self._read_record(entry))
        return records

    def _read_record(self, entry: Mapping[str, Any]) -> RawRecord:
        return self._read_record_with_xarray(entry)

    def _read_record_with_xarray(self, entry: Mapping[str, Any]) -> RawRecord:
        local_key = str(entry["local_key"])
        try:
            import xarray as xr
        except ImportError as error:
            raise CanonicalConversionError(
                f"Cannot parse raw file {local_key}; install xarray, cfgrib, and netCDF4."
            ) from error

        dataset = None
        file_path = self.object_store.resolve_path(local_key)
        cfgrib_error: Exception | None = None
        try:
            expected_native_variable = str(entry["variable"])
            backend_kwargs = _cfgrib_backend_kwargs(entry, expected_native_variable)
            try:
                dataset = xr.open_dataset(file_path, engine="cfgrib", backend_kwargs=backend_kwargs)
            except Exception as _cfgrib_err:
                cfgrib_error = _cfgrib_err
                LOGGER.warning(
                    "Failed to parse raw file %s with cfgrib; falling back to netcdf4: %s",
                    local_key,
                    _cfgrib_err,
                )
                dataset = xr.open_dataset(file_path, engine="netcdf4")
            data_variable = self._select_data_variable(dataset, expected_native_variable, local_key)
            data_array = dataset[data_variable]
            values = tuple(float(value) for value in data_array.values.ravel().tolist())
            return RawRecord(
                source_file=self.object_store.uri_for_key(local_key),
                native_variable=expected_native_variable,
                forecast_hour=int(entry["forecast_hour"]),
                values=values,
                longitudes=_coord_values_by_name(dataset, ("lon", "longitude")),
                latitudes=_coord_values_by_name(dataset, ("lat", "latitude")),
                shape=tuple(int(size) for size in getattr(data_array.values, "shape", ())),
                metadata=dict(_mapping_value(entry.get("metadata"))),
            )
        except Exception as error:
            detail = f"Failed to parse raw file {local_key}: {error}"
            if cfgrib_error is not None:
                detail += f" (cfgrib also failed: {cfgrib_error})"
            raise CanonicalConversionError(detail) from error
        finally:
            if dataset is not None:
                dataset.close()

    def _select_data_variable(self, dataset: Any, expected_native_variable: str, local_key: str) -> str:
        return self._select_cfgrib_data_variable(dataset, expected_native_variable, local_key)

    def _select_cfgrib_data_variable(self, dataset: Any, expected_native_variable: str, local_key: str) -> str:
        expected_names = set(self.config.cfgrib_variable_aliases.get(expected_native_variable, ()))
        expected_names.add(expected_native_variable)
        matches: list[str] = []
        available: list[str] = []
        for data_variable in dataset.data_vars:
            variable_attrs = dataset[data_variable].attrs
            candidates = {
                str(data_variable),
                str(variable_attrs.get("GRIB_shortName", "")),
                str(variable_attrs.get("shortName", "")),
            }
            available.append("/".join(sorted(candidate for candidate in candidates if candidate)))
            if candidates & expected_names:
                matches.append(str(data_variable))

        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise CanonicalConversionError(
                (
                    f"cfgrib variable mismatch for {local_key}: manifest expected {expected_native_variable} "
                    f"(aliases: {sorted(expected_names)}); dataset variables: {available}."
                )
            )
        raise CanonicalConversionError(
            (
                f"cfgrib variable mapping for {local_key} is ambiguous: manifest expected "
                f"{expected_native_variable}, matched {matches}."
            )
        )

    def _group_records(self, records: list[RawRecord]) -> dict[str, list[RawRecord]]:
        grouped: dict[str, list[RawRecord]] = {}
        for record in records:
            standard_variable = map_variable(record.native_variable, self.config.variable_mapping)
            if standard_variable is None:
                LOGGER.warning("Skipping unmapped variable %s from %s", record.native_variable, record.source_file)
                continue
            grouped.setdefault(standard_variable, []).append(record)
        return grouped

    def _entries_by_standard_variable(self, entries: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for entry in entries:
            native_variable = entry["variable"]
            standard_variable = map_variable(native_variable, self.config.variable_mapping)
            if standard_variable is None:
                LOGGER.warning("Skipping unmapped variable %s from %s", native_variable, entry["local_key"])
                continue
            grouped.setdefault(standard_variable, []).append(entry)
        return grouped

    def _entries_by_hour_and_variable(self, entries: list[dict[str, Any]]) -> dict[int, dict[str, dict[str, Any]]]:
        grouped: dict[int, dict[str, dict[str, Any]]] = {}
        for entry in entries:
            native_variable = str(entry["variable"])
            if map_variable(native_variable, self.config.variable_mapping) is None:
                LOGGER.warning("Skipping unmapped variable %s from %s", native_variable, entry["local_key"])
                continue
            grouped.setdefault(int(entry["forecast_hour"]), {})[native_variable] = entry
        return grouped

    def _covered_required_pairs(self, entries: list[dict[str, Any]]) -> set[tuple[str, int]]:
        return {
            (str(entry["variable"]), int(entry["forecast_hour"]))
            for entry in entries
            if map_variable(str(entry["variable"]), self.config.variable_mapping) is not None
        }

    def _missing_required_pairs(
        self,
        manifest: Any,
        entries: list[dict[str, Any]],
        records: list[RawRecord],
    ) -> tuple[MissingForecastVariable, ...]:
        covered = {(record.native_variable, record.forecast_hour) for record in records}
        return self._missing_required_pairs_from_covered(manifest, entries, covered)

    def _missing_required_pairs_from_covered(
        self,
        manifest: Any,
        entries: list[dict[str, Any]],
        covered: set[tuple[str, int]],
    ) -> tuple[MissingForecastVariable, ...]:
        forecast_hours = self._configured_forecast_hours(manifest, entries)
        missing: list[MissingForecastVariable] = []
        required_standard_variables = set(self.required_standard_variables())
        for forecast_hour in forecast_hours:
            for native_variable, standard_variable in sorted(self.config.variable_mapping.items()):
                if standard_variable not in required_standard_variables:
                    continue
                if (
                    normalize_source_id(self.config.source_id) == "gfs"
                    and forecast_hour == 0
                    and standard_variable in GFS_F000_OPTIONAL_INTERVAL_STANDARD_VARIABLES
                ):
                    continue
                if (native_variable, forecast_hour) not in covered:
                    missing.append(
                        MissingForecastVariable(
                            native_variable=native_variable,
                            standard_variable=standard_variable,
                            forecast_hour=forecast_hour,
                        )
                    )
        return tuple(missing)

    def required_standard_variables(self) -> tuple[str, ...]:
        return required_standard_variables_for_source(self.config.source_id)

    def _configured_forecast_hours(self, manifest: Any, entries: list[dict[str, Any]]) -> list[int]:
        metadata = _manifest_metadata(manifest)
        if isinstance(metadata.get("forecast_hours"), list):
            return sorted({int(forecast_hour) for forecast_hour in metadata["forecast_hours"]})

        first_hour = metadata.get("first_forecast_hour")
        last_hour = metadata.get("last_forecast_hour")
        step_hours = self._native_time_resolution_hours()
        if first_hour is not None and last_hour is not None and step_hours is not None:
            return list(range(int(first_hour), int(last_hour) + 1, step_hours))

        return sorted({int(entry["forecast_hour"]) for entry in entries})

    def _native_time_resolution_hours(self) -> int | None:
        resolution = self.config.native_time_resolution.strip().lower()
        if not resolution.endswith("h"):
            return None
        try:
            step_hours = int(resolution[:-1])
        except ValueError:
            return None
        return step_hours if step_hours > 0 else None

    def _missing_pairs_message(self, missing_pairs: tuple[MissingForecastVariable, ...]) -> str:
        details = ", ".join(
            f"{pair.native_variable}->{pair.standard_variable} f{pair.forecast_hour:03d}" for pair in missing_pairs[:20]
        )
        suffix = ""
        if len(missing_pairs) > 20:
            suffix = f", ... ({len(missing_pairs)} total missing pairs)"
        return f"Missing required canonical variables forecast-hour coverage: {details}{suffix}"

    def _record_missing_products(
        self,
        source_id: str,
        cycle_time: datetime,
        missing_pairs: tuple[MissingForecastVariable, ...],
    ) -> None:
        compact_cycle = format_cycle_time(cycle_time)
        for pair in missing_pairs:
            canonical_product_id = f"{source_id}_{compact_cycle}_{pair.standard_variable}_f{pair.forecast_hour:03d}"
            object_key = (
                f"canonical/{source_id}/{compact_cycle}/{pair.standard_variable}/{canonical_product_id}.missing"
            )
            lineage_json = {
                "source_files": [],
                "source_cycle_id": f"{source_id}_{compact_cycle}",
                "conversion_params": {
                    "operation": "coverage_validation",
                    "missing_native_variable": pair.native_variable,
                    "missing_standard_variable": pair.standard_variable,
                    "missing_forecast_hour": pair.forecast_hour,
                },
                "converter_version": self.config.converter_version,
            }
            self._upsert_product(
                {
                    "canonical_product_id": canonical_product_id,
                    "source_id": source_id,
                    "source_version": compact_cycle,
                    "cycle_time": cycle_time,
                    "valid_time": cycle_time + timedelta(hours=pair.forecast_hour),
                    "lead_time_hours": pair.forecast_hour,
                    "variable": pair.standard_variable,
                    "unit": unit_for_standard_variable(pair.standard_variable),
                    "grid_id": self.config.grid_id,
                    "grid_definition_uri": self.config.grid_definition_uri,
                    "native_time_resolution": self.config.native_time_resolution,
                    "native_spatial_resolution": self.config.native_spatial_resolution,
                    "object_uri": self.object_store.uri_for_key(object_key),
                    "checksum": "",
                    "quality_flag": "fail",
                    "lineage_json": lineage_json,
                }
            )

    def _convert_record(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        standard_variable: str,
        record: RawRecord,
        previous_values: tuple[float, ...] | None,
        previous_source_file: str | None,
        previous_forecast_hour: int | None,
        policy_identity: Mapping[str, Any] | None = None,
        source_object_identity: Mapping[str, Any] | None = None,
    ) -> CanonicalProductResult:
        conversion = convert_units_with_metadata(
            record.native_variable,
            record.values,
            previous_values,
            forecast_hour=record.forecast_hour,
            previous_forecast_hour=previous_forecast_hour,
            accumulation_type=_apcp_accumulation_type_from_metadata(record.metadata),
            step_range=_apcp_step_range_from_metadata(record.metadata),
        )
        valid_time = cycle_time + timedelta(hours=record.forecast_hour)
        compact_cycle = format_cycle_time(cycle_time)
        canonical_product_id = f"{source_id}_{compact_cycle}_{standard_variable}_f{record.forecast_hour:03d}"
        object_key = f"canonical/{source_id}/{compact_cycle}/{standard_variable}/{canonical_product_id}.nc"
        source_files = [record.source_file]
        apcp_accumulation_type = _apcp_accumulation_type_from_metadata(record.metadata)
        if (
            record.native_variable == "apcp"
            and previous_source_file is not None
            and apcp_accumulation_type != "interval_bucket"
        ):
            source_files = [previous_source_file, record.source_file]
        conversion_params: dict[str, Any] = {
            "native_variable": record.native_variable,
            "operation": CONVERSION_PARAMS.get(record.native_variable, "pass_through"),
        }
        if record.native_variable == "apcp":
            conversion_params["accumulation_type"] = apcp_accumulation_type or "cumulative_since_cycle"
            step_range = _apcp_step_range_from_metadata(record.metadata)
            if step_range:
                conversion_params["step_range"] = step_range
            if apcp_accumulation_type == "interval_bucket":
                conversion_params["operation"] = "interval_bucket_mm_to_mm_day"
            if record.metadata:
                conversion_params["raw_metadata"] = dict(record.metadata)
        if conversion.anomalies:
            conversion_params["anomalies"] = list(conversion.anomalies)
            conversion_params["negative_delta_forecast_hours"] = [
                anomaly["forecast_hour"]
                for anomaly in conversion.anomalies
                if anomaly.get("type") == "negative_apcp_delta"
            ]
        lineage_json = {
            "source_files": source_files,
            "source_cycle_id": f"{source_id}_{compact_cycle}",
            "conversion_params": conversion_params,
            "converter_version": self.config.converter_version,
        }
        if policy_identity:
            lineage_json["policy_identity"] = dict(policy_identity)
        if source_object_identity:
            lineage_json["source_object_identity"] = dict(source_object_identity)
        self._ensure_grid_definition(record)
        content = self._serialize_product(
            variable=standard_variable,
            values=conversion.values,
            cycle_time=cycle_time,
            valid_time=valid_time,
            lead_time_hours=record.forecast_hour,
            unit=unit_for_standard_variable(standard_variable),
            lineage_json=lineage_json,
        )
        checksum = sha256_bytes(content)

        # TODO: Batch repository reads for a cycle once CanonicalRepository exposes a
        # keyed bulk fetch; this per-product check intentionally preserves the
        # current write/idempotency/error semantics.
        existing = self._get_existing_product(canonical_product_id)
        if self._existing_product_is_current(existing, object_key, checksum):
            return CanonicalProductResult(
                canonical_product_id=canonical_product_id,
                variable=standard_variable,
                valid_time=valid_time,
                lead_time_hours=record.forecast_hour,
                object_uri=existing["object_uri"],
                checksum=existing["checksum"],
                status="already_done",
                quality_flag=existing.get("quality_flag", "ok"),
                lineage_json=_mapping_value(existing.get("lineage_json")),
            )

        try:
            object_uri = self.object_store.write_bytes_atomic(object_key, content)
        except (OSError, ObjectStoreError, ValueError) as error:
            raise CanonicalConversionError(f"Failed to write canonical product {object_key}: {error}") from error

        record_payload = {
            "canonical_product_id": canonical_product_id,
            "source_id": source_id,
            "source_version": compact_cycle,
            "cycle_time": cycle_time,
            "valid_time": valid_time,
            "lead_time_hours": record.forecast_hour,
            "variable": standard_variable,
            "unit": unit_for_standard_variable(standard_variable),
            "grid_id": self.config.grid_id,
            "grid_definition_uri": self.config.grid_definition_uri,
            "native_time_resolution": self.config.native_time_resolution,
            "native_spatial_resolution": self.config.native_spatial_resolution,
            "object_uri": object_uri,
            "checksum": checksum,
            "quality_flag": conversion.quality_flag,
            "lineage_json": lineage_json,
        }
        self._upsert_product(record_payload)
        return CanonicalProductResult(
            canonical_product_id=canonical_product_id,
            variable=standard_variable,
            valid_time=valid_time,
            lead_time_hours=record.forecast_hour,
            object_uri=object_uri,
            checksum=checksum,
            status="updated" if existing else "created",
            quality_flag=conversion.quality_flag,
            lineage_json=lineage_json,
        )

    def _ensure_grid_definition(self, record: RawRecord) -> None:
        if not record.longitudes or not record.latitudes:
            return
        payload: dict[str, Any]
        if len(record.shape) == 2:
            y_count, x_count = record.shape
            if len(record.longitudes) != x_count or len(record.latitudes) != y_count:
                return
            payload = {
                "schema_version": "nhms.grid_definition.v1",
                "grid_id": self.config.grid_id,
                "layout": "rectilinear",
                "axis_order": ["latitude", "longitude"],
                "shape": [y_count, x_count],
                "longitudes": [_normalize_longitude(longitude) for longitude in record.longitudes],
                "latitudes": list(record.latitudes),
            }
        elif len(record.longitudes) == len(record.values) and len(record.latitudes) == len(record.values):
            payload = {
                "schema_version": "nhms.grid_definition.v1",
                "grid_id": self.config.grid_id,
                "cells": [
                    {"id": index, "lon": _normalize_longitude(longitude), "lat": latitude}
                    for index, (longitude, latitude) in enumerate(zip(record.longitudes, record.latitudes, strict=True))
                ],
            }
        else:
            return
        try:
            if self.object_store.exists(self.config.grid_definition_uri):
                existing = json.loads(self.object_store.read_bytes(self.config.grid_definition_uri).decode("utf-8"))
                if _grid_definition_signature(existing) != _grid_definition_signature(payload):
                    raise CanonicalConversionError(
                        f"Grid definition {self.config.grid_definition_uri} already exists with a different "
                        "longitude/latitude definition or cell order."
                    )
                return
            self.object_store.write_bytes_atomic(
                self.config.grid_definition_uri,
                json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"),
            )
        except (OSError, ObjectStoreError, ValueError) as error:
            message = f"Failed to write grid definition {self.config.grid_definition_uri}: {error}"
            raise CanonicalConversionError(message) from error

    def _serialize_product(
        self,
        *,
        variable: str,
        values: tuple[float, ...],
        cycle_time: datetime,
        valid_time: datetime,
        lead_time_hours: int,
        unit: str,
        lineage_json: Mapping[str, Any],
    ) -> bytes:
        try:
            import netCDF4  # noqa: F401
            import xarray as xr
        except ImportError as error:
            raise CanonicalConversionError(
                "NetCDF4 serialization requires xarray and netCDF4; install both dependencies."
            ) from error

        dataset = xr.Dataset(
            data_vars={variable: ("point", list(values))},
            coords={"point": list(range(len(values)))},
            attrs={
                "cycle_time": cycle_time.isoformat(),
                "valid_time": valid_time.isoformat(),
                "lead_time_hours": lead_time_hours,
                "unit": unit,
                "grid_id": self.config.grid_id,
                "lineage_json": json.dumps(dict(lineage_json), sort_keys=True),
            },
        )
        try:
            with tempfile.NamedTemporaryFile(suffix=".nc") as temp_file:
                dataset.to_netcdf(temp_file.name, engine="netcdf4", format="NETCDF4")
                temp_file.seek(0)
                return temp_file.read()
        except (OSError, ValueError, RuntimeError) as error:
            raise CanonicalConversionError(f"Failed to serialize NetCDF4 product {variable}: {error}") from error
        finally:
            dataset.close()

    def _get_existing_product(self, canonical_product_id: str) -> dict[str, Any] | None:
        if self.repository is None:
            return None
        try:
            return self.repository.get_canonical_product(canonical_product_id=canonical_product_id)
        except Exception:
            LOGGER.exception("Failed to read canonical product %s", canonical_product_id)
            raise

    def _existing_product_is_current(
        self,
        existing: Mapping[str, Any] | None,
        object_key: str,
        checksum: str,
    ) -> bool:
        if existing is None or existing.get("quality_flag") == "fail":
            return False
        # Treat products written by a different (or missing, i.e. legacy)
        # converter_version as stale so semantic changes force a re-conversion.
        # converter_version is recorded inside lineage_json (not at top level),
        # with a top-level fallback for forward compatibility.
        existing_lineage = _mapping_value(existing.get("lineage_json"))
        existing_version = existing_lineage.get("converter_version", existing.get("converter_version"))
        if existing_version != self.config.converter_version:
            return False
        existing_checksum = str(existing.get("checksum", ""))
        if existing_checksum != checksum:
            return False
        try:
            return self.object_store.exists(object_key) and self.object_store.checksum(object_key) == checksum
        except (OSError, ObjectStoreError, ValueError):
            LOGGER.exception("Failed to verify existing canonical object %s", object_key)
            return False

    def _upsert_product(self, record: Mapping[str, Any]) -> None:
        if self.repository is None:
            return
        try:
            self.repository.upsert_canonical_product(record)
        except Exception:
            LOGGER.exception("Failed to upsert canonical product %s", record["canonical_product_id"])
            raise

    def _update_cycle_status(
        self,
        cycle_time: datetime,
        *,
        status: str,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        if self.repository is None:
            return
        try:
            self.repository.update_forecast_cycle(
                source_id=self.config.source_id,
                cycle_time=cycle_time,
                status=status,
                error_code=error_code,
                error_message=error_message,
            )
        except Exception:
            LOGGER.exception("Failed to update forecast cycle conversion status for %s", format_cycle_time(cycle_time))
            raise


class ERA5CanonicalConverter(CanonicalConverter):
    def __init__(
        self,
        *,
        config: ERA5CanonicalConverterConfig | None = None,
        repository: CanonicalRepository | None = None,
        object_store: LocalObjectStore | None = None,
    ) -> None:
        super().__init__(
            config=config or ERA5CanonicalConverterConfig(),
            repository=repository,
            object_store=object_store,
        )

    @classmethod
    def from_env(cls) -> ERA5CanonicalConverter:
        config = ERA5CanonicalConverterConfig()
        return cls(config=config, repository=PsycopgMetStore.from_env())

    def convert_manifest(self, manifest: Any) -> ConversionResult:
        cycle_time = parse_cycle_time(_manifest_value(manifest, "cycle_time"))
        source_id = _manifest_value(manifest, "source_id")
        if source_id != self.config.source_id:
            raise CanonicalConversionError(
                f"Manifest source_id {source_id!r} does not match converter source_id {self.config.source_id!r}."
            )

        try:
            entries = _manifest_entries(manifest)
            covered_pairs = self._covered_required_pairs(entries)
            missing_pairs = self._missing_required_pairs_from_covered(manifest, entries, covered_pairs)
            if missing_pairs:
                self._record_missing_products(source_id, cycle_time, missing_pairs)
                raise CanonicalConversionError(self._missing_pairs_message(missing_pairs))

            entries_by_hour = self._entries_by_hour_and_variable(entries)
            forecast_hours = self._configured_forecast_hours(manifest, entries)
            manifest_metadata = _manifest_metadata(manifest)
            policy_identity = _mapping_value(manifest_metadata.get("source_policy"))
            source_object_identity = _mapping_value(manifest_metadata.get("source_object_identity"))
            products: list[CanonicalProductResult] = []
            previous_precipitation: RawRecord | None = None
            previous_ssr: RawRecord | None = None
            previous_str: RawRecord | None = None

            for forecast_hour in forecast_hours:
                records = {
                    native_variable: self._read_record(entry)
                    for native_variable, entry in entries_by_hour[forecast_hour].items()
                }
                self._ensure_grid_definition_from_records(records.values())
                temperature = records["2m_temperature"]
                dewpoint = records["2m_dewpoint_temperature"]
                wind_u = records["10m_u_component_of_wind"]
                wind_v = records["10m_v_component_of_wind"]
                pressure = records["surface_pressure"]
                precipitation = records["total_precipitation"]
                ssr = records["surface_net_solar_radiation"]
                str_ = records["surface_net_thermal_radiation"]

                temperature_c = convert_units("2m_temperature", temperature.values)
                dewpoint_c = convert_units("2m_dewpoint_temperature", dewpoint.values)
                products.append(
                    self._write_product(
                        source_id=source_id,
                        cycle_time=cycle_time,
                        standard_variable="air_temperature_2m",
                        forecast_hour=forecast_hour,
                        values=temperature_c,
                        unit=self._unit_for_standard_variable("air_temperature_2m"),
                        source_files=[temperature.source_file],
                        conversion_params={"native_variable": temperature.native_variable, "operation": "K_to_C"},
                        policy_identity=policy_identity,
                        source_object_identity=source_object_identity,
                    )
                )
                products.append(
                    self._write_product(
                        source_id=source_id,
                        cycle_time=cycle_time,
                        standard_variable="relative_humidity_2m",
                        forecast_hour=forecast_hour,
                        values=compute_relative_humidity_values(temperature_c, dewpoint_c),
                        unit=self._unit_for_standard_variable("relative_humidity_2m"),
                        source_files=[temperature.source_file, dewpoint.source_file],
                        conversion_params={
                            "native_variables": [temperature.native_variable, dewpoint.native_variable],
                            "operation": "dewpoint_magnus_rh",
                        },
                        policy_identity=policy_identity,
                        source_object_identity=source_object_identity,
                    )
                )
                products.append(
                    self._write_product(
                        source_id=source_id,
                        cycle_time=cycle_time,
                        standard_variable="wind_u_10m",
                        forecast_hour=forecast_hour,
                        values=wind_u.values,
                        unit=self._unit_for_standard_variable("wind_u_10m"),
                        source_files=[wind_u.source_file],
                        conversion_params={"native_variable": wind_u.native_variable, "operation": "pass_through"},
                        policy_identity=policy_identity,
                        source_object_identity=source_object_identity,
                    )
                )
                products.append(
                    self._write_product(
                        source_id=source_id,
                        cycle_time=cycle_time,
                        standard_variable="wind_v_10m",
                        forecast_hour=forecast_hour,
                        values=wind_v.values,
                        unit=self._unit_for_standard_variable("wind_v_10m"),
                        source_files=[wind_v.source_file],
                        conversion_params={"native_variable": wind_v.native_variable, "operation": "pass_through"},
                        policy_identity=policy_identity,
                        source_object_identity=source_object_identity,
                    )
                )
                products.append(
                    self._write_product(
                        source_id=source_id,
                        cycle_time=cycle_time,
                        standard_variable="wind_speed",
                        forecast_hour=forecast_hour,
                        values=self._wind_speed_values(wind_u.values, wind_v.values),
                        unit=self._unit_for_standard_variable("wind_speed"),
                        source_files=[wind_u.source_file, wind_v.source_file],
                        conversion_params={
                            "native_variables": [wind_u.native_variable, wind_v.native_variable],
                            "operation": "vector_magnitude",
                        },
                        policy_identity=policy_identity,
                        source_object_identity=source_object_identity,
                    )
                )
                products.append(
                    self._write_product(
                        source_id=source_id,
                        cycle_time=cycle_time,
                        standard_variable="pressure_surface",
                        forecast_hour=forecast_hour,
                        values=pressure.values,
                        unit=self._unit_for_standard_variable("pressure_surface"),
                        source_files=[pressure.source_file],
                        conversion_params={"native_variable": pressure.native_variable, "operation": "pass_through"},
                        policy_identity=policy_identity,
                        source_object_identity=source_object_identity,
                    )
                )

                precipitation_conversion = convert_era5_precipitation_with_metadata(
                    precipitation.values,
                    previous_precipitation.values if previous_precipitation is not None else None,
                    forecast_hour=forecast_hour,
                    previous_forecast_hour=previous_precipitation.forecast_hour
                    if previous_precipitation is not None
                    else None,
                )
                precipitation_sources = [precipitation.source_file]
                if previous_precipitation is not None:
                    precipitation_sources.insert(0, previous_precipitation.source_file)
                precipitation_params: dict[str, Any] = {
                    "native_variable": precipitation.native_variable,
                    "operation": "cumulative_m_to_mm_day",
                    "accumulation_type": "since_midnight",
                }
                if precipitation_conversion.anomalies:
                    precipitation_params["anomalies"] = list(precipitation_conversion.anomalies)
                products.append(
                    self._write_product(
                        source_id=source_id,
                        cycle_time=cycle_time,
                        standard_variable="prcp_rate_or_amount",
                        forecast_hour=forecast_hour,
                        values=precipitation_conversion.values,
                        unit=self._unit_for_standard_variable("prcp_rate_or_amount"),
                        source_files=precipitation_sources,
                        conversion_params=precipitation_params,
                        quality_flag=precipitation_conversion.quality_flag,
                        policy_identity=policy_identity,
                        source_object_identity=source_object_identity,
                    )
                )

                radiation_values = convert_era5_radiation_values(
                    ssr.values,
                    str_.values,
                    previous_ssr.values if previous_ssr is not None else None,
                    previous_str.values if previous_str is not None else None,
                    forecast_hour=forecast_hour,
                    previous_forecast_hour=previous_ssr.forecast_hour if previous_ssr is not None else None,
                )
                radiation_sources = [ssr.source_file, str_.source_file]
                if previous_ssr is not None and previous_str is not None:
                    radiation_sources = [
                        previous_ssr.source_file,
                        previous_str.source_file,
                        ssr.source_file,
                        str_.source_file,
                    ]
                products.append(
                    self._write_product(
                        source_id=source_id,
                        cycle_time=cycle_time,
                        standard_variable="net_radiation",
                        forecast_hour=forecast_hour,
                        values=radiation_values,
                        unit=self._unit_for_standard_variable("net_radiation"),
                        source_files=radiation_sources,
                        conversion_params={
                            "native_variables": [ssr.native_variable, str_.native_variable],
                            "operation": "cumulative_j_m2_to_w_m2_direct_net",
                            "accumulation_type": "since_midnight",
                        },
                        lineage_updates={"radiation_method": "direct_net"},
                        policy_identity=policy_identity,
                        source_object_identity=source_object_identity,
                    )
                )

                previous_precipitation = precipitation
                previous_ssr = ssr
                previous_str = str_

            return self._complete_cycle_after_conversion(
                source_id=source_id,
                cycle_time=cycle_time,
                products=products,
                forecast_hours=self._configured_forecast_hours(manifest, entries),
                policy_identity=policy_identity,
                source_object_identity=source_object_identity,
            )
        except Exception as error:
            try:
                self._update_cycle_status(
                    cycle_time,
                    status="failed_convert",
                    error_code="CONVERT_FAILED",
                    error_message=str(error),
                )
            except Exception:
                LOGGER.exception(
                    "Failed to record CONVERT_FAILED status for %s; preserving original conversion error",
                    format_cycle_time(cycle_time),
                )
            raise error

    def _records_by_hour_and_variable(self, records: list[RawRecord]) -> dict[int, dict[str, RawRecord]]:
        grouped: dict[int, dict[str, RawRecord]] = {}
        for record in records:
            grouped.setdefault(record.forecast_hour, {})[record.native_variable] = record
        return grouped

    def _ensure_grid_definition_from_records(self, records: Iterable[RawRecord]) -> None:
        for record in records:
            if record.longitudes and record.latitudes:
                self._ensure_grid_definition(record)

    def _write_product(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        standard_variable: str,
        forecast_hour: int,
        values: tuple[float, ...],
        unit: str,
        source_files: list[str],
        conversion_params: Mapping[str, Any],
        quality_flag: str = "ok",
        lineage_updates: Mapping[str, Any] | None = None,
        policy_identity: Mapping[str, Any] | None = None,
        source_object_identity: Mapping[str, Any] | None = None,
    ) -> CanonicalProductResult:
        valid_time = cycle_time + timedelta(hours=forecast_hour)
        compact_cycle = format_cycle_time(cycle_time)
        date_key = cycle_time.strftime("%Y-%m-%d")
        canonical_product_id = f"{source_id}_{compact_cycle}_{standard_variable}_f{forecast_hour:03d}"
        object_key = f"canonical/{source_id}/{date_key}/{standard_variable}/{canonical_product_id}.nc"
        lineage_json: dict[str, Any] = {
            "source_files": source_files,
            "source_cycle_id": f"{source_id}_{compact_cycle}",
            "conversion_params": dict(conversion_params),
            "converter_version": self.config.converter_version,
        }
        if lineage_updates:
            lineage_json.update(lineage_updates)
        if policy_identity:
            lineage_json["policy_identity"] = dict(policy_identity)
        if source_object_identity:
            lineage_json["source_object_identity"] = dict(source_object_identity)
        content = self._serialize_product(
            variable=standard_variable,
            values=values,
            cycle_time=cycle_time,
            valid_time=valid_time,
            lead_time_hours=forecast_hour,
            unit=unit,
            lineage_json=lineage_json,
        )
        checksum = sha256_bytes(content)

        existing = self._get_existing_product(canonical_product_id)
        if self._existing_product_is_current(existing, object_key, checksum):
            return CanonicalProductResult(
                canonical_product_id=canonical_product_id,
                variable=standard_variable,
                valid_time=valid_time,
                lead_time_hours=forecast_hour,
                object_uri=existing["object_uri"],
                checksum=existing["checksum"],
                status="already_done",
                quality_flag=existing.get("quality_flag", "ok"),
                lineage_json=_mapping_value(existing.get("lineage_json")),
            )

        try:
            object_uri = self.object_store.write_bytes_atomic(object_key, content)
        except (OSError, ObjectStoreError, ValueError) as error:
            raise CanonicalConversionError(f"Failed to write canonical product {object_key}: {error}") from error

        self._upsert_product(
            {
                "canonical_product_id": canonical_product_id,
                "source_id": source_id,
                "source_version": compact_cycle,
                "cycle_time": cycle_time,
                "valid_time": valid_time,
                "lead_time_hours": forecast_hour,
                "variable": standard_variable,
                "unit": unit,
                "grid_id": self.config.grid_id,
                "grid_definition_uri": self.config.grid_definition_uri,
                "native_time_resolution": self.config.native_time_resolution,
                "native_spatial_resolution": self.config.native_spatial_resolution,
                "object_uri": object_uri,
                "checksum": checksum,
                "quality_flag": quality_flag,
                "lineage_json": lineage_json,
            }
        )
        return CanonicalProductResult(
            canonical_product_id=canonical_product_id,
            variable=standard_variable,
            valid_time=valid_time,
            lead_time_hours=forecast_hour,
            object_uri=object_uri,
            checksum=checksum,
            status="updated" if existing else "created",
            quality_flag=quality_flag,
            lineage_json=lineage_json,
        )

    def _record_missing_products(
        self,
        source_id: str,
        cycle_time: datetime,
        missing_pairs: tuple[MissingForecastVariable, ...],
    ) -> None:
        compact_cycle = format_cycle_time(cycle_time)
        date_key = cycle_time.strftime("%Y-%m-%d")
        for pair in missing_pairs:
            canonical_product_id = f"{source_id}_{compact_cycle}_{pair.standard_variable}_f{pair.forecast_hour:03d}"
            object_key = f"canonical/{source_id}/{date_key}/{pair.standard_variable}/{canonical_product_id}.missing"
            lineage_json = {
                "source_files": [],
                "source_cycle_id": f"{source_id}_{compact_cycle}",
                "conversion_params": {
                    "operation": "coverage_validation",
                    "missing_native_variable": pair.native_variable,
                    "missing_standard_variable": pair.standard_variable,
                    "missing_forecast_hour": pair.forecast_hour,
                },
                "converter_version": self.config.converter_version,
            }
            self._upsert_product(
                {
                    "canonical_product_id": canonical_product_id,
                    "source_id": source_id,
                    "source_version": compact_cycle,
                    "cycle_time": cycle_time,
                    "valid_time": cycle_time + timedelta(hours=pair.forecast_hour),
                    "lead_time_hours": pair.forecast_hour,
                    "variable": pair.standard_variable,
                    "unit": self._unit_for_standard_variable(pair.standard_variable),
                    "grid_id": self.config.grid_id,
                    "grid_definition_uri": self.config.grid_definition_uri,
                    "native_time_resolution": self.config.native_time_resolution,
                    "native_spatial_resolution": self.config.native_spatial_resolution,
                    "object_uri": self.object_store.uri_for_key(object_key),
                    "checksum": "",
                    "quality_flag": "fail",
                    "lineage_json": lineage_json,
                }
            )

    def _unit_for_standard_variable(self, standard_variable: str) -> str:
        try:
            return ERA5_STANDARD_UNITS[standard_variable]
        except KeyError as error:
            raise CanonicalConversionError(f"No ERA5 standard unit configured for {standard_variable}") from error

    def _wind_speed_values(
        self,
        wind_u_values: tuple[float, ...],
        wind_v_values: tuple[float, ...],
    ) -> tuple[float, ...]:
        if len(wind_u_values) != len(wind_v_values):
            raise CanonicalConversionError("ERA5 wind u/v arrays must have the same length.")
        return tuple(
            math.sqrt((u_value * u_value) + (v_value * v_value))
            for u_value, v_value in zip(wind_u_values, wind_v_values)
        )


class IFSCanonicalConverter(CanonicalConverter):
    def __init__(
        self,
        *,
        config: IFSCanonicalConverterConfig | None = None,
        repository: CanonicalRepository | None = None,
        object_store: LocalObjectStore | None = None,
    ) -> None:
        super().__init__(
            config=config or IFSCanonicalConverterConfig(),
            repository=repository,
            object_store=object_store,
        )

    @classmethod
    def from_env(cls) -> IFSCanonicalConverter:
        config = IFSCanonicalConverterConfig()
        return cls(config=config, repository=PsycopgMetStore.from_env())

    def convert_manifest(self, manifest: Any) -> ConversionResult:
        cycle_time = parse_cycle_time(_manifest_value(manifest, "cycle_time"))
        source_id = _manifest_value(manifest, "source_id")
        if source_id != self.config.source_id:
            raise CanonicalConversionError(
                f"Manifest source_id {source_id!r} does not match converter source_id {self.config.source_id!r}."
            )

        try:
            entries = _manifest_entries(manifest)
            covered_pairs = self._covered_required_pairs(entries)
            missing_pairs = self._missing_required_pairs_from_covered(manifest, entries, covered_pairs)
            if missing_pairs:
                self._record_missing_products(source_id, cycle_time, missing_pairs)
                raise CanonicalConversionError(self._missing_pairs_message(missing_pairs))

            entries_by_hour = self._entries_by_hour_and_variable(entries)
            forecast_hours = self._configured_forecast_hours(manifest, entries)
            manifest_metadata = _manifest_metadata(manifest)
            policy_identity = _mapping_value(manifest_metadata.get("source_policy"))
            source_object_identity = _mapping_value(manifest_metadata.get("source_object_identity"))
            products: list[CanonicalProductResult] = []
            previous_precipitation: RawRecord | None = None
            previous_ssr: RawRecord | None = None
            previous_str: RawRecord | None = None
            consecutive_negative_precipitation = 0

            for forecast_hour in forecast_hours:
                records = {
                    native_variable: self._read_record(entry)
                    for native_variable, entry in entries_by_hour[forecast_hour].items()
                }
                self._ensure_grid_definition_from_records(records.values())
                temperature = records["2t"]
                dewpoint = records["2d"]
                wind_u = records["10u"]
                wind_v = records["10v"]
                pressure = records["sp"]
                precipitation = records["tp"]
                ssr = records["ssr"]
                str_ = records["str"]

                temperature_c = convert_units("2t", temperature.values)
                dewpoint_c = convert_units("2d", dewpoint.values)
                products.append(
                    self._write_product(
                        source_id=source_id,
                        cycle_time=cycle_time,
                        standard_variable="air_temperature_2m",
                        forecast_hour=forecast_hour,
                        values=temperature_c,
                        unit=self._unit_for_standard_variable("air_temperature_2m"),
                        source_files=[temperature.source_file],
                        conversion_params={
                            "native_variable": temperature.native_variable,
                            "operation": "K_to_C",
                            "unit_conversion": "K_to_C",
                        },
                        policy_identity=policy_identity,
                        source_object_identity=source_object_identity,
                    )
                )
                products.append(
                    self._write_product(
                        source_id=source_id,
                        cycle_time=cycle_time,
                        standard_variable="relative_humidity_2m",
                        forecast_hour=forecast_hour,
                        values=compute_ifs_relative_humidity_values(temperature_c, dewpoint_c),
                        unit=self._unit_for_standard_variable("relative_humidity_2m"),
                        source_files=[temperature.source_file, dewpoint.source_file],
                        conversion_params={
                            "native_variables": [temperature.native_variable, dewpoint.native_variable],
                            "operation": "magnus_formula",
                            "derived_from": [temperature.native_variable, dewpoint.native_variable],
                            "method": "magnus_formula",
                        },
                        lineage_updates={
                            "derived_from": [temperature.native_variable, dewpoint.native_variable],
                            "method": "magnus_formula",
                        },
                        policy_identity=policy_identity,
                        source_object_identity=source_object_identity,
                    )
                )
                products.append(
                    self._write_product(
                        source_id=source_id,
                        cycle_time=cycle_time,
                        standard_variable="wind_u_10m",
                        forecast_hour=forecast_hour,
                        values=wind_u.values,
                        unit=self._unit_for_standard_variable("wind_u_10m"),
                        source_files=[wind_u.source_file],
                        conversion_params={"native_variable": wind_u.native_variable, "operation": "pass_through"},
                        policy_identity=policy_identity,
                        source_object_identity=source_object_identity,
                    )
                )
                products.append(
                    self._write_product(
                        source_id=source_id,
                        cycle_time=cycle_time,
                        standard_variable="wind_v_10m",
                        forecast_hour=forecast_hour,
                        values=wind_v.values,
                        unit=self._unit_for_standard_variable("wind_v_10m"),
                        source_files=[wind_v.source_file],
                        conversion_params={"native_variable": wind_v.native_variable, "operation": "pass_through"},
                        policy_identity=policy_identity,
                        source_object_identity=source_object_identity,
                    )
                )
                products.append(
                    self._write_product(
                        source_id=source_id,
                        cycle_time=cycle_time,
                        standard_variable="surface_pressure",
                        forecast_hour=forecast_hour,
                        values=pressure.values,
                        unit=self._unit_for_standard_variable("surface_pressure"),
                        source_files=[pressure.source_file],
                        conversion_params={"native_variable": pressure.native_variable, "operation": "pass_through"},
                        policy_identity=policy_identity,
                        source_object_identity=source_object_identity,
                    )
                )

                precipitation_conversion, consecutive_negative_precipitation, precip_step_hours = (
                    convert_ifs_precipitation_with_metadata(
                        precipitation.values,
                        previous_precipitation.values if previous_precipitation is not None else None,
                        forecast_hour=forecast_hour,
                        previous_forecast_hour=previous_precipitation.forecast_hour
                        if previous_precipitation is not None
                        else None,
                        consecutive_negative_count=consecutive_negative_precipitation,
                    )
                )
                if not precipitation_conversion.anomalies or precipitation_conversion.quality_flag == "ok":
                    consecutive_negative_precipitation = 0
                precipitation_sources = [precipitation.source_file]
                if previous_precipitation is not None:
                    precipitation_sources.insert(0, previous_precipitation.source_file)
                precipitation_params: dict[str, Any] = {
                    "native_variable": precipitation.native_variable,
                    # mm/day, derived from the per-step accumulation rescaled by the
                    # actual step (24 / step_hours); step_hours kept for audit.
                    "operation": "cumulative_m_to_mm_day",
                    "accumulation_type": "since_cycle",
                    "unit_conversion": "m_to_mm_day",
                    "step_hours": precip_step_hours,
                }
                if precipitation_conversion.anomalies:
                    precipitation_params["anomalies"] = list(precipitation_conversion.anomalies)
                products.append(
                    self._write_product(
                        source_id=source_id,
                        cycle_time=cycle_time,
                        standard_variable="prcp_rate_or_amount",
                        forecast_hour=forecast_hour,
                        values=precipitation_conversion.values,
                        unit=self._unit_for_standard_variable("prcp_rate_or_amount"),
                        source_files=precipitation_sources,
                        conversion_params=precipitation_params,
                        quality_flag=precipitation_conversion.quality_flag,
                        policy_identity=policy_identity,
                        source_object_identity=source_object_identity,
                    )
                )

                radiation_values, radiation_step_hours = convert_ifs_radiation_values(
                    ssr.values,
                    str_.values,
                    previous_ssr.values if previous_ssr is not None else None,
                    previous_str.values if previous_str is not None else None,
                    forecast_hour=forecast_hour,
                    previous_forecast_hour=previous_ssr.forecast_hour if previous_ssr is not None else None,
                )
                radiation_sources = [ssr.source_file, str_.source_file]
                if previous_ssr is not None and previous_str is not None:
                    radiation_sources = [
                        previous_ssr.source_file,
                        previous_str.source_file,
                        ssr.source_file,
                        str_.source_file,
                    ]
                shortwave_conversion, shortwave_step_hours = convert_ifs_shortwave_down_values(
                    ssr.values,
                    previous_ssr.values if previous_ssr is not None else None,
                    forecast_hour=forecast_hour,
                    previous_forecast_hour=previous_ssr.forecast_hour if previous_ssr is not None else None,
                )
                shortwave_sources = [ssr.source_file]
                if previous_ssr is not None:
                    shortwave_sources.insert(0, previous_ssr.source_file)
                shortwave_params: dict[str, Any] = {
                    "native_variable": ssr.native_variable,
                    "operation": "cumulative_j_m2_to_w_m2_downward_shortwave",
                    "accumulation_type": "since_cycle",
                    "step_hours": shortwave_step_hours,
                }
                if shortwave_conversion.anomalies:
                    shortwave_params["anomalies"] = list(shortwave_conversion.anomalies)
                products.append(
                    self._write_product(
                        source_id=source_id,
                        cycle_time=cycle_time,
                        standard_variable="shortwave_down",
                        forecast_hour=forecast_hour,
                        values=shortwave_conversion.values,
                        unit=self._unit_for_standard_variable("shortwave_down"),
                        source_files=shortwave_sources,
                        conversion_params=shortwave_params,
                        quality_flag=shortwave_conversion.quality_flag,
                        policy_identity=policy_identity,
                        source_object_identity=source_object_identity,
                    )
                )
                products.append(
                    self._write_product(
                        source_id=source_id,
                        cycle_time=cycle_time,
                        standard_variable="net_radiation",
                        forecast_hour=forecast_hour,
                        values=radiation_values,
                        unit=self._unit_for_standard_variable("net_radiation"),
                        source_files=radiation_sources,
                        conversion_params={
                            "native_variables": [ssr.native_variable, str_.native_variable],
                            "operation": "cumulative_j_m2_to_w_m2_direct_net",
                            "accumulation_type": "since_cycle",
                            "radiation_method": "direct_net",
                            "components": [ssr.native_variable, str_.native_variable],
                            "step_hours": radiation_step_hours,
                        },
                        lineage_updates={
                            "radiation_method": "direct_net",
                            "components": [ssr.native_variable, str_.native_variable],
                        },
                        policy_identity=policy_identity,
                        source_object_identity=source_object_identity,
                    )
                )

                previous_precipitation = precipitation
                previous_ssr = ssr
                previous_str = str_

            return self._complete_cycle_after_conversion(
                source_id=source_id,
                cycle_time=cycle_time,
                products=products,
                forecast_hours=forecast_hours,
                policy_identity=policy_identity,
                source_object_identity=source_object_identity,
            )
        except Exception as error:
            try:
                self._update_cycle_status(
                    cycle_time,
                    status="failed_convert",
                    error_code="CONVERT_FAILED",
                    error_message=str(error),
                )
            except Exception:
                LOGGER.exception(
                    "Failed to record CONVERT_FAILED status for %s; preserving original conversion error",
                    format_cycle_time(cycle_time),
                )
            raise error

    def _records_by_hour_and_variable(self, records: list[RawRecord]) -> dict[int, dict[str, RawRecord]]:
        grouped: dict[int, dict[str, RawRecord]] = {}
        for record in records:
            grouped.setdefault(record.forecast_hour, {})[record.native_variable] = record
        return grouped

    def _missing_required_pairs(
        self,
        manifest: Any,
        entries: list[dict[str, Any]],
        records: list[RawRecord],
    ) -> tuple[MissingForecastVariable, ...]:
        pairs = list(super()._missing_required_pairs(manifest, entries, records))
        return self._add_shortwave_missing_pairs(pairs)

    def _missing_required_pairs_from_covered(
        self,
        manifest: Any,
        entries: list[dict[str, Any]],
        covered: set[tuple[str, int]],
    ) -> tuple[MissingForecastVariable, ...]:
        pairs = list(super()._missing_required_pairs_from_covered(manifest, entries, covered))
        seen_pairs = {(pair.native_variable, pair.standard_variable, pair.forecast_hour) for pair in pairs}
        for forecast_hour in self._configured_forecast_hours(manifest, entries):
            for native_variable in ("ssr", "str"):
                if (native_variable, forecast_hour) in covered:
                    continue
                pair_key = (native_variable, "net_radiation", forecast_hour)
                if pair_key in seen_pairs:
                    continue
                pairs.append(
                    MissingForecastVariable(
                        native_variable=native_variable,
                        standard_variable="net_radiation",
                        forecast_hour=forecast_hour,
                    )
                )
                seen_pairs.add(pair_key)
        return self._add_shortwave_missing_pairs(pairs)

    def _add_shortwave_missing_pairs(
        self,
        pairs: list[MissingForecastVariable],
    ) -> tuple[MissingForecastVariable, ...]:
        shortwave_pairs = [
            MissingForecastVariable(
                native_variable=pair.native_variable,
                standard_variable="shortwave_down",
                forecast_hour=pair.forecast_hour,
            )
            for pair in pairs
            if pair.native_variable == "ssr"
        ]
        return tuple([*pairs, *shortwave_pairs])

    def _ensure_grid_definition_from_records(self, records: Iterable[RawRecord]) -> None:
        for record in records:
            if record.longitudes and record.latitudes:
                self._ensure_grid_definition(record)

    def _write_product(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        standard_variable: str,
        forecast_hour: int,
        values: tuple[float, ...],
        unit: str,
        source_files: list[str],
        conversion_params: Mapping[str, Any],
        quality_flag: str = "ok",
        lineage_updates: Mapping[str, Any] | None = None,
        policy_identity: Mapping[str, Any] | None = None,
        source_object_identity: Mapping[str, Any] | None = None,
    ) -> CanonicalProductResult:
        valid_time = cycle_time + timedelta(hours=forecast_hour)
        compact_cycle = format_cycle_time(cycle_time)
        canonical_product_id = f"{source_id}_{compact_cycle}_{standard_variable}_f{forecast_hour:03d}"
        object_key = f"canonical/{source_id}/{compact_cycle}/{standard_variable}/{canonical_product_id}.nc"
        lineage_json: dict[str, Any] = {
            "source_files": source_files,
            "source_cycle_id": f"{source_id}_{compact_cycle}",
            "conversion_params": dict(conversion_params),
            "converter_version": self.config.converter_version,
        }
        if lineage_updates:
            lineage_json.update(lineage_updates)
        if policy_identity:
            lineage_json["policy_identity"] = dict(policy_identity)
        if source_object_identity:
            lineage_json["source_object_identity"] = dict(source_object_identity)
        content = self._serialize_product(
            variable=standard_variable,
            values=values,
            cycle_time=cycle_time,
            valid_time=valid_time,
            lead_time_hours=forecast_hour,
            unit=unit,
            lineage_json=lineage_json,
        )
        checksum = sha256_bytes(content)

        existing = self._get_existing_product(canonical_product_id)
        if self._existing_product_is_current(existing, object_key, checksum):
            return CanonicalProductResult(
                canonical_product_id=canonical_product_id,
                variable=standard_variable,
                valid_time=valid_time,
                lead_time_hours=forecast_hour,
                object_uri=existing["object_uri"],
                checksum=existing["checksum"],
                status="already_done",
                quality_flag=existing.get("quality_flag", "ok"),
                lineage_json=_mapping_value(existing.get("lineage_json")),
            )

        try:
            object_uri = self.object_store.write_bytes_atomic(object_key, content)
        except (OSError, ObjectStoreError, ValueError) as error:
            raise CanonicalConversionError(f"Failed to write canonical product {object_key}: {error}") from error

        self._upsert_product(
            {
                "canonical_product_id": canonical_product_id,
                "source_id": source_id,
                "source_version": compact_cycle,
                "cycle_time": cycle_time,
                "valid_time": valid_time,
                "lead_time_hours": forecast_hour,
                "variable": standard_variable,
                "unit": unit,
                "grid_id": self.config.grid_id,
                "grid_definition_uri": self.config.grid_definition_uri,
                "native_time_resolution": self.config.native_time_resolution,
                "native_spatial_resolution": self.config.native_spatial_resolution,
                "object_uri": object_uri,
                "checksum": checksum,
                "quality_flag": quality_flag,
                "lineage_json": lineage_json,
            }
        )
        return CanonicalProductResult(
            canonical_product_id=canonical_product_id,
            variable=standard_variable,
            valid_time=valid_time,
            lead_time_hours=forecast_hour,
            object_uri=object_uri,
            checksum=checksum,
            status="updated" if existing else "created",
            quality_flag=quality_flag,
            lineage_json=lineage_json,
        )

    def _record_missing_products(
        self,
        source_id: str,
        cycle_time: datetime,
        missing_pairs: tuple[MissingForecastVariable, ...],
    ) -> None:
        compact_cycle = format_cycle_time(cycle_time)
        for pair in missing_pairs:
            canonical_product_id = f"{source_id}_{compact_cycle}_{pair.standard_variable}_f{pair.forecast_hour:03d}"
            object_key = (
                f"canonical/{source_id}/{compact_cycle}/{pair.standard_variable}/{canonical_product_id}.missing"
            )
            lineage_json = {
                "source_files": [],
                "source_cycle_id": f"{source_id}_{compact_cycle}",
                "conversion_params": {
                    "operation": "coverage_validation",
                    "missing_native_variable": pair.native_variable,
                    "missing_standard_variable": pair.standard_variable,
                    "missing_forecast_hour": pair.forecast_hour,
                },
                "converter_version": self.config.converter_version,
            }
            self._upsert_product(
                {
                    "canonical_product_id": canonical_product_id,
                    "source_id": source_id,
                    "source_version": compact_cycle,
                    "cycle_time": cycle_time,
                    "valid_time": cycle_time + timedelta(hours=pair.forecast_hour),
                    "lead_time_hours": pair.forecast_hour,
                    "variable": pair.standard_variable,
                    "unit": self._unit_for_standard_variable(pair.standard_variable),
                    "grid_id": self.config.grid_id,
                    "grid_definition_uri": self.config.grid_definition_uri,
                    "native_time_resolution": self.config.native_time_resolution,
                    "native_spatial_resolution": self.config.native_spatial_resolution,
                    "object_uri": self.object_store.uri_for_key(object_key),
                    "checksum": "",
                    "quality_flag": "fail",
                    "lineage_json": lineage_json,
                }
            )

    def _unit_for_standard_variable(self, standard_variable: str) -> str:
        try:
            return IFS_STANDARD_UNITS[standard_variable]
        except KeyError as error:
            raise CanonicalConversionError(f"No IFS standard unit configured for {standard_variable}") from error


def _manifest_value(manifest: Any, key: str) -> Any:
    if isinstance(manifest, Mapping):
        return manifest[key]
    return getattr(manifest, key)


def _manifest_metadata(manifest: Any) -> dict[str, Any]:
    if isinstance(manifest, Mapping):
        return dict(manifest.get("metadata") or {})
    return dict(getattr(manifest, "metadata", {}) or {})


def _manifest_entries(manifest: Any) -> list[dict[str, Any]]:
    entries = _manifest_value(manifest, "entries")
    normalized: list[dict[str, Any]] = []
    for entry in entries:
        if isinstance(entry, Mapping):
            normalized.append(dict(entry))
        elif hasattr(entry, "as_dict"):
            normalized.append(entry.as_dict())
        else:
            normalized.append(
                {
                    "local_key": entry.local_key,
                    "variable": entry.variable,
                    "forecast_hour": entry.forecast_hour,
                }
            )
    return normalized


def _cfgrib_backend_kwargs(entry: Mapping[str, Any], expected_native_variable: str) -> dict[str, Any]:
    metadata = _mapping_value(entry.get("metadata"))
    explicit = metadata.get("cfgrib_filter_by_keys")
    if isinstance(explicit, Mapping):
        return {"filter_by_keys": dict(explicit), "indexpath": ""}

    bundle = metadata.get("bundle")
    if isinstance(bundle, Mapping) and bundle.get("layout") == "per_forecast_hour":
        short_name = metadata.get("grib_short_name") or _first_cfgrib_alias(expected_native_variable)
        if short_name:
            return {"filter_by_keys": {"shortName": short_name}, "indexpath": ""}

    return {"indexpath": ""}


def _first_cfgrib_alias(native_variable: str) -> str | None:
    aliases = CFGRIB_VARIABLE_ALIASES.get(native_variable)
    if aliases:
        return aliases[0]
    return native_variable or None
