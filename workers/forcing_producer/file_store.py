from __future__ import annotations

import json
import logging
import os
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from packages.common.forcing_domain_handoff import (
    CONTRACT_ID as FORCING_DOMAIN_HANDOFF_CONTRACT_ID,
)
from packages.common.forcing_domain_handoff import (
    FORCING_DOMAIN_PACKAGE_MANIFEST_CHECKSUM_FIELD,
    FORCING_DOMAIN_PACKAGE_MANIFEST_URI_FIELD,
    FORCING_PACKAGE_MANIFEST_CHECKSUM_FIELD,
    FORCING_PACKAGE_MANIFEST_URI_FIELD,
)
from packages.common.forcing_domain_handoff import (
    PACKAGE_CONTRACT_ID as FORCING_DOMAIN_PACKAGE_CONTRACT_ID,
)
from packages.common.forcing_domain_handoff import (
    SCHEMA_VERSION as FORCING_DOMAIN_HANDOFF_SCHEMA_VERSION,
)
from packages.common.met_store import MetStoreError
from packages.common.object_store import LocalObjectStore, ObjectStoreError, sha256_bytes
from packages.common.source_identity import normalize_source_id
from workers.canonical_converter.converter import unit_for_standard_variable
from workers.forcing_producer.direct_grid_contract import (
    DirectGridContractError,
    DirectGridForcingContract,
    load_forcing_mapping_contract_from_manifest,
)
from workers.forcing_producer.producer import (
    CanonicalProduct,
    ForcingComponent,
    ForcingTimeseriesRow,
    InterpolationWeight,
    MetStation,
    format_cycle_time,
    parse_cycle_time,
)

LOGGER = logging.getLogger(__name__)

_FORECAST_PRODUCT_RE = re.compile(r"^(?P<source>.+)_(?P<cycle>\d{10})_(?P<variable>.+)_f(?P<lead>\d{3})\.nc$")
_NATIVE_RESOLUTION_RE = re.compile(r"^(?P<value>[1-9]\d*)(?P<unit>h|min)$", re.IGNORECASE)
_TRUTHY = {"1", "true", "t", "yes", "y", "on"}


@dataclass
class FileForcingRepository:
    """Object-store/file backed repository for DB-free forcing production."""

    object_store: LocalObjectStore
    registry_manifest: Path | str | None = None
    _registry_cache: Mapping[str, Any] | None = field(default=None, init=False, repr=False)
    _model_manifest_cache: dict[str, Mapping[str, Any]] = field(default_factory=dict, init=False, repr=False)
    _stations_by_basin_version: dict[str, tuple[MetStation, ...]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _weights_by_scope: dict[tuple[str, str, str], tuple[InterpolationWeight, ...]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _forcing_versions: dict[str, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)
    _forcing_components: dict[str, tuple[ForcingComponent, ...]] = field(default_factory=dict, init=False, repr=False)
    _forcing_timeseries_summary: dict[str, Mapping[str, Any]] = field(default_factory=dict, init=False, repr=False)
    _forcing_timeseries_rows: dict[str, tuple[ForcingTimeseriesRow, ...]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    @classmethod
    def from_env(
        cls,
        *,
        object_store: LocalObjectStore | None = None,
        object_store_root: Path | str | None = None,
        object_store_prefix: str | None = None,
    ) -> FileForcingRepository:
        store = object_store or LocalObjectStore(
            object_store_root or os.getenv("OBJECT_STORE_ROOT") or os.getenv("WORKSPACE_ROOT") or ".nhms-workspace",
            object_store_prefix=(
                object_store_prefix if object_store_prefix is not None else os.getenv("OBJECT_STORE_PREFIX", "")
            ),
        )
        registry_manifest = (
            os.getenv("NHMS_SCHEDULER_REGISTRY_MANIFEST")
            or os.getenv("NHMS_FORCING_MODEL_REGISTRY_MANIFEST")
            or os.getenv("NHMS_FILE_MODEL_REGISTRY_MANIFEST")
            or "scheduler/registry/manifest-last.json"
        )
        return cls(object_store=store, registry_manifest=registry_manifest)

    def resolve_model_identity(self, *, model_id: str) -> dict[str, Any]:
        model = self._model_entry(model_id)
        return {
            "basin_id": str(model["basin_id"]),
            "basin_version_id": str(model["basin_version_id"]),
            "river_network_version_id": str(model.get("river_network_version_id") or ""),
        }

    def resolve_model_basin_version(self, *, model_id: str) -> str:
        return str(self._model_entry(model_id)["basin_version_id"])

    def load_met_stations(self, *, basin_version_id: str) -> tuple[MetStation, ...]:
        cached = self._stations_by_basin_version.get(basin_version_id)
        if cached is not None:
            return cached
        model = self._model_entry_for_basin_version(basin_version_id)
        manifest = self._model_manifest(model)
        stations = self._stations_from_model_manifest(
            model=model,
            manifest=manifest,
            basin_version_id=basin_version_id,
        )
        self._stations_by_basin_version[basin_version_id] = stations
        return stations

    def list_canonical_products(self, *, source_id: str, cycle_time: datetime) -> tuple[CanonicalProduct, ...]:
        normalized_source = normalize_source_id(source_id)
        compact_cycle = format_cycle_time(cycle_time)
        catalog_products = self._canonical_products_from_catalog(
            source_id=normalized_source,
            cycle_time=parse_cycle_time(compact_cycle),
        )
        if catalog_products is not None:
            return catalog_products
        cycle_dir = Path(self.object_store.root) / "canonical" / normalized_source / compact_cycle
        if not cycle_dir.exists():
            return ()
        products: list[CanonicalProduct] = []
        for product_path in sorted(cycle_dir.glob("*/*.nc")):
            product = self._canonical_product_from_path(
                product_path,
                source_id=normalized_source,
                cycle_time=parse_cycle_time(compact_cycle),
            )
            if product is not None:
                products.append(product)
        return tuple(sorted(products, key=lambda item: (item.variable, item.valid_time, item.canonical_product_id)))

    def list_fallback_canonical_products(
        self,
        *,
        source_id: str,
        start_time: datetime,
        end_time: datetime,
        variables: Sequence[str],
    ) -> tuple[CanonicalProduct, ...]:
        normalized_source = normalize_source_id(source_id)
        source_dir = Path(self.object_store.root) / "canonical" / normalized_source
        if not source_dir.exists() or not variables:
            return ()
        selected: dict[tuple[datetime, str], CanonicalProduct] = {}
        for cycle_dir in sorted(path for path in source_dir.iterdir() if path.is_dir()):
            for product_path in sorted(cycle_dir.glob("*/*.nc")):
                product = self._canonical_product_from_path(
                    product_path,
                    source_id=normalized_source,
                    cycle_time=parse_cycle_time(cycle_dir.name),
                )
                if product is None or product.variable not in variables:
                    continue
                if not start_time <= product.valid_time <= end_time:
                    continue
                if product.quality_flag == "fail" or not product.checksum:
                    continue
                key = (product.valid_time, product.variable)
                existing = selected.get(key)
                if existing is None or _lead_sort_key(product) < _lead_sort_key(existing):
                    selected[key] = product
        return tuple(sorted(selected.values(), key=lambda item: (item.variable, item.valid_time)))

    def load_interp_weights(
        self,
        *,
        source_id: str,
        grid_id: str,
        model_id: str,
    ) -> tuple[InterpolationWeight, ...]:
        return self._weights_by_scope.get((source_id, grid_id, model_id), ())

    def upsert_interp_weights(self, weights: Sequence[InterpolationWeight]) -> None:
        if not weights:
            return
        scopes = {(weight.source_id, weight.grid_id, weight.model_id) for weight in weights}
        if len(scopes) != 1:
            raise MetStoreError("Interpolation weights must be replaced one source/grid/model scope at a time.")
        self._weights_by_scope[next(iter(scopes))] = tuple(weights)

    def ensure_direct_grid_met_stations(
        self,
        *,
        basin_version_id: str,
        contract: DirectGridForcingContract,
    ) -> None:
        return None

    def load_forcing_mapping_contract(
        self,
        *,
        model_id: str,
        basin_version_id: str,
        source_id: str | None = None,
    ) -> DirectGridForcingContract | None:
        model = self._model_entry(model_id)
        if str(model.get("basin_version_id") or "") != basin_version_id:
            raise MetStoreError(
                f"Model instance {model_id!r} for basin_version_id {basin_version_id!r} was not found."
            )
        resource_profile = model.get("resource_profile") or {}
        if not isinstance(resource_profile, Mapping):
            raise DirectGridContractError(
                "Model resource_profile must be a JSON object.",
                details={
                    "model_id": model_id,
                    "basin_version_id": basin_version_id,
                    "actual_type": type(resource_profile).__name__,
                },
            )
        return load_forcing_mapping_contract_from_manifest(
            resource_profile,
            source_id=source_id,
            allow_root_direct_grid=False,
        )

    def load_direct_grid_validation_assets(
        self,
        *,
        model_id: str,
        basin_version_id: str,
        contract: DirectGridForcingContract,
    ) -> Mapping[str, Any]:
        model = self._model_entry(model_id)
        if str(model.get("basin_version_id") or "") != basin_version_id:
            raise MetStoreError(
                f"Model instance {model_id!r} for basin_version_id {basin_version_id!r} was not found."
            )
        authoritative_contract = self.load_forcing_mapping_contract(
            model_id=model_id,
            basin_version_id=basin_version_id,
        )
        if authoritative_contract != contract:
            raise DirectGridContractError(
                "Direct-grid validation contract does not match file model registry resource_profile.",
                field="direct_grid_forcing",
                details={"model_id": model_id, "basin_version_id": basin_version_id},
            )

        package_uri = str(model.get("model_package_uri") or "").rstrip("/")
        if not package_uri:
            raise MetStoreError(f"Model {model_id!r} does not declare model_package_uri.")
        sp_att_relative = _safe_direct_grid_package_member(contract.sp_att_path)
        sp_att_uri = f"{package_uri}/{sp_att_relative}"
        try:
            binding_content = self.object_store.read_bytes(contract.binding_uri)
            sp_att_content = self.object_store.read_bytes(sp_att_uri)
        except (OSError, ObjectStoreError, ValueError) as error:
            raise MetStoreError(
                f"Failed to read authoritative direct-grid assets for model {model_id!r}: {error}"
            ) from error
        try:
            decoded_sp_att = sp_att_content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise MetStoreError(
                f"Direct-grid .sp.att for model {model_id!r} is not UTF-8 text."
            ) from error
        return {
            "binding_checksum": sha256_bytes(binding_content),
            "model_input_package_id": authoritative_contract.model_input_package_id,
            "sp_att_checksum": sha256_bytes(sp_att_content),
            "sp_att_content": decoded_sp_att,
        }

    def get_forcing_version(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str,
    ) -> dict[str, Any] | None:
        forcing_version_id = f"forc_{normalize_source_id(source_id).lower()}_{format_cycle_time(cycle_time)}_{model_id}"
        return self._forcing_versions.get(forcing_version_id)

    def upsert_forcing_version(self, record: Mapping[str, Any]) -> dict[str, Any]:
        forcing_version_id = str(record["forcing_version_id"])
        stored = dict(record)
        self._forcing_versions[forcing_version_id] = stored
        return dict(stored)

    def finalize_forcing_version(self, forcing_version_id: str, checksum: str) -> dict[str, Any]:
        record = dict(self._forcing_versions.get(forcing_version_id) or {"forcing_version_id": forcing_version_id})
        record["checksum"] = checksum
        self._forcing_versions[forcing_version_id] = record
        self._write_forcing_version_sidecar(record)
        self._write_forcing_domain_handoff(record)
        return dict(record)

    def clear_forcing_version_checksum(self, forcing_version_id: str) -> dict[str, Any]:
        record = dict(self._forcing_versions.get(forcing_version_id) or {"forcing_version_id": forcing_version_id})
        record["checksum"] = None
        self._forcing_versions[forcing_version_id] = record
        return dict(record)

    def verify_forcing_version_children(
        self,
        *,
        forcing_version_id: str,
        expected_components: Sequence[ForcingComponent],
        expected_station_ids: Sequence[str],
        expected_valid_times: Sequence[datetime],
        expected_variables: Sequence[str],
    ) -> Mapping[str, Any]:
        components = self._forcing_components.get(forcing_version_id, ())
        summary = self._forcing_timeseries_summary.get(forcing_version_id) or {}
        expected_timeseries_count = len(expected_station_ids) * len(expected_valid_times) * len(expected_variables)
        complete = (
            len(components) == len(expected_components)
            and int(summary.get("row_count") or 0) == expected_timeseries_count
            and int(summary.get("station_count") or 0) == len(expected_station_ids)
            and int(summary.get("timestep_count") or 0) == len(expected_valid_times)
            and int(summary.get("variable_count") or 0) == len(expected_variables)
        )
        return {
            "forcing_version_id": forcing_version_id,
            "expected_component_count": len(expected_components),
            "component_count": len(components),
            "expected_timeseries_row_count": expected_timeseries_count,
            "timeseries_row_count": int(summary.get("row_count") or 0),
            "station_count": int(summary.get("station_count") or 0),
            "timestep_count": int(summary.get("timestep_count") or 0),
            "variable_count": int(summary.get("variable_count") or 0),
            "complete": complete,
        }

    def replace_forcing_components(self, forcing_version_id: str, components: Sequence[ForcingComponent]) -> None:
        self._forcing_components[forcing_version_id] = tuple(components)

    def replace_forcing_timeseries(
        self,
        forcing_version_id: str,
        rows: Sequence[ForcingTimeseriesRow],
    ) -> None:
        stored_rows = tuple(rows)
        self._forcing_timeseries_summary[forcing_version_id] = {
            "row_count": len(stored_rows),
            "station_count": len({row.station_id for row in stored_rows}),
            "timestep_count": len({row.valid_time for row in stored_rows}),
            "variable_count": len({row.variable for row in stored_rows}),
        }
        self._forcing_timeseries_rows[forcing_version_id] = stored_rows

    def update_forecast_cycle(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        status: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any] | None:
        return {
            "source_id": normalize_source_id(source_id),
            "cycle_time": cycle_time,
            "status": status,
            "error_code": error_code,
            "error_message": error_message,
            "repository_backend": "file",
        }

    def find_registered_snapshot_bbox_by_identity(
        self,
        *,
        source_id: str,
        grid_id: str,
        grid_signature: str,
    ) -> tuple[float, float, float, float, uuid.UUID, datetime | None] | None:
        """Resolve the checksum-bound snapshot projection embedded in file registry rows.

        Node-22 is deliberately DB-free.  A direct-grid registry row therefore
        carries the immutable DB snapshot projection under
        ``resource_profile.canonical_grid_snapshot``.  Multiple source-scoped
        model variants may reference the same projection; byte-equivalent
        duplicates are accepted while any conflicting projection fails closed.
        """
        normalized_source = normalize_source_id(source_id)
        matches: list[tuple[float, float, float, float, uuid.UUID, datetime | None]] = []
        for model in self._registry_models():
            profile = model.get("resource_profile")
            if not isinstance(profile, Mapping):
                continue
            direct_grid = profile.get("direct_grid_forcing")
            if not isinstance(direct_grid, Mapping):
                continue
            if str(direct_grid.get("grid_id") or "") != grid_id:
                continue
            if str(direct_grid.get("grid_signature") or "") != grid_signature:
                continue
            applicable = direct_grid.get("applicable_source_ids")
            if not isinstance(applicable, Sequence) or isinstance(applicable, (str, bytes)):
                continue
            try:
                normalized_applicable = {normalize_source_id(str(value)) for value in applicable}
            except ValueError:
                continue
            if normalized_source not in normalized_applicable:
                continue
            snapshot = profile.get("canonical_grid_snapshot")
            if not isinstance(snapshot, Mapping):
                raise MetStoreError(
                    f"Direct-grid model {model.get('model_id')!r} is missing "
                    "resource_profile.canonical_grid_snapshot for DB-free preflight."
                )
            matches.append(
                _direct_grid_snapshot_bbox(
                    snapshot,
                    source_id=normalized_source,
                    grid_id=grid_id,
                    grid_signature=grid_signature,
                )
            )
        if not matches:
            return None
        first = matches[0]
        if any(match != first for match in matches[1:]):
            raise MetStoreError(
                "Conflicting canonical_grid_snapshot projections exist in the file model registry "
                f"for source_id={normalized_source!r}, grid_id={grid_id!r}, "
                f"grid_signature={grid_signature!r}."
            )
        return first

    def _registry(self) -> Mapping[str, Any]:
        if self._registry_cache is None:
            self._registry_cache = self._read_json_reference(str(self.registry_manifest or ""))
        return self._registry_cache

    def _model_entry(self, model_id: str) -> Mapping[str, Any]:
        for model in self._registry_models():
            if str(model.get("model_id") or "") == model_id:
                return model
        raise MetStoreError(f"Model instance {model_id!r} was not found in file model registry.")

    def _model_entry_for_basin_version(self, basin_version_id: str) -> Mapping[str, Any]:
        for model in self._registry_models():
            if str(model.get("basin_version_id") or "") == basin_version_id:
                return model
        raise MetStoreError(f"Basin version {basin_version_id!r} was not found in file model registry.")

    def _registry_models(self) -> tuple[Mapping[str, Any], ...]:
        registry = self._registry()
        models = registry.get("models")
        if not isinstance(models, Sequence) or isinstance(models, (str, bytes)):
            raise MetStoreError("File model registry must contain a models array.")
        return tuple(model for model in models if isinstance(model, Mapping))

    def _model_manifest(self, model: Mapping[str, Any]) -> Mapping[str, Any]:
        manifest_uri = str(model.get("manifest_uri") or "")
        if not manifest_uri:
            resource_profile = model.get("resource_profile") or {}
            if isinstance(resource_profile, Mapping):
                manifest_uri = str(resource_profile.get("model_package_manifest_uri") or "")
        if not manifest_uri:
            raise MetStoreError(f"Model {model.get('model_id')!r} does not declare a manifest_uri.")
        cached = self._model_manifest_cache.get(manifest_uri)
        if cached is not None:
            return cached
        manifest = self._read_json_reference(manifest_uri)
        self._model_manifest_cache[manifest_uri] = manifest
        return manifest

    def _stations_from_model_manifest(
        self,
        *,
        model: Mapping[str, Any],
        manifest: Mapping[str, Any],
        basin_version_id: str,
    ) -> tuple[MetStation, ...]:
        package_uri = str(model.get("model_package_uri") or "").rstrip("/")
        resource_profile = model.get("resource_profile") or {}
        shud_input_name = ""
        if isinstance(resource_profile, Mapping):
            shud_input_name = str(resource_profile.get("shud_input_name") or resource_profile.get("project_name") or "")
        basin_slug = str(manifest.get("basin_slug") or shud_input_name or "model")
        forc_uri = f"{package_uri}/{shud_input_name or basin_slug}.tsd.forc" if package_uri else ""
        content: str | None = None
        if forc_uri:
            try:
                content = self.object_store.read_bytes(forc_uri).decode("utf-8")
            except (ObjectStoreError, UnicodeDecodeError, ValueError):
                content = None
        if content is None:
            forcing = manifest.get("forcing")
            if isinstance(forcing, Mapping):
                forcing_dir = str(forcing.get("forcing_dir") or "")
                if forcing_dir:
                    candidate = Path(forcing_dir).parent / f"{shud_input_name or basin_slug}.tsd.forc"
                    if candidate.exists():
                        content = candidate.read_text(encoding="utf-8")
        if content is None:
            raise MetStoreError(
                f"Model {model.get('model_id')!r} does not expose a readable SHUD forcing index file."
            )
        stations = _parse_shud_tsd_forc_stations(
            content,
            basin_version_id=basin_version_id,
            station_prefix=basin_slug,
        )
        if not stations:
            raise MetStoreError(f"Model {model.get('model_id')!r} has no forcing stations in SHUD forcing index.")
        return stations

    def _canonical_product_from_path(
        self,
        product_path: Path,
        *,
        source_id: str,
        cycle_time: datetime,
    ) -> CanonicalProduct | None:
        variable = product_path.parent.name
        match = _FORECAST_PRODUCT_RE.fullmatch(product_path.name)
        if match is None:
            return None
        canonical_product_id = product_path.stem
        attrs = self._read_netcdf_attrs(product_path)
        valid_time = parse_cycle_time(str(attrs.get("valid_time") or cycle_time.isoformat()))
        lead_time = _int_or_none(attrs.get("lead_time_hours"))
        if lead_time is None:
            lead_time = int(match.group("lead"))
        unit = str(attrs.get("unit") or unit_for_standard_variable(variable))
        grid_id = str(attrs.get("grid_id") or _grid_id_for_source(source_id))
        lineage_json = _json_object(attrs.get("lineage_json"))
        try:
            object_uri = self.object_store.uri_for_key(str(product_path.relative_to(self.object_store.root)))
            checksum = self.object_store.checksum(object_uri)
        except (OSError, ObjectStoreError, ValueError) as error:
            raise MetStoreError(f"Failed to inspect canonical product {product_path}: {error}") from error
        return CanonicalProduct(
            canonical_product_id=canonical_product_id,
            source_id=source_id,
            cycle_time=cycle_time,
            valid_time=valid_time,
            lead_time_hours=lead_time,
            variable=variable,
            unit=unit,
            grid_id=grid_id,
            grid_definition_uri=_grid_definition_uri_for_source(source_id),
            native_time_resolution=_native_time_resolution_for_source(source_id),
            native_spatial_resolution="0.25deg",
            object_uri=object_uri,
            checksum=checksum,
            quality_flag=str(attrs.get("quality_flag") or "ok"),
            lineage_json=lineage_json,
        )

    def _canonical_products_from_catalog(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
    ) -> tuple[CanonicalProduct, ...] | None:
        catalog_key = f"canonical/{source_id}/{format_cycle_time(cycle_time)}/_catalog/catalog.json"
        if not self.object_store.exists(catalog_key):
            return None
        try:
            payload = json.loads(self.object_store.read_bytes(catalog_key).decode("utf-8"))
        except (OSError, ObjectStoreError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise MetStoreError(f"Failed to read canonical product catalog {catalog_key}: {error}") from error
        if not isinstance(payload, Mapping):
            raise MetStoreError(f"Canonical product catalog {catalog_key} must contain a JSON object.")
        rows = payload.get("products")
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            raise MetStoreError(f"Canonical product catalog {catalog_key} must contain a products array.")
        products = [
            self._canonical_product_from_catalog_row(row, source_id=source_id, cycle_time=cycle_time)
            for row in rows
            if isinstance(row, Mapping)
        ]
        return tuple(sorted(products, key=lambda item: (item.variable, item.valid_time, item.canonical_product_id)))

    def _canonical_product_from_catalog_row(
        self,
        row: Mapping[str, Any],
        *,
        source_id: str,
        cycle_time: datetime,
    ) -> CanonicalProduct:
        variable = str(row["variable"])
        return CanonicalProduct(
            canonical_product_id=str(row["canonical_product_id"]),
            source_id=str(row.get("source_id") or source_id),
            cycle_time=parse_cycle_time(row.get("cycle_time", cycle_time)),
            valid_time=parse_cycle_time(row["valid_time"]),
            lead_time_hours=_int_or_none(row.get("lead_time_hours")),
            variable=variable,
            unit=str(row.get("unit") or unit_for_standard_variable(variable)),
            grid_id=str(row.get("grid_id") or _grid_id_for_source(source_id)),
            grid_definition_uri=str(row.get("grid_definition_uri") or _grid_definition_uri_for_source(source_id)),
            native_time_resolution=str(
                row.get("native_time_resolution") or _native_time_resolution_for_source(source_id)
            ),
            native_spatial_resolution=str(row.get("native_spatial_resolution") or "0.25deg"),
            object_uri=str(row["object_uri"]),
            checksum=str(row.get("checksum") or ""),
            quality_flag=str(row.get("quality_flag") or "ok"),
            lineage_json=_json_object(row.get("lineage_json")),
        )

    def _read_netcdf_attrs(self, product_path: Path) -> Mapping[str, Any]:
        try:
            import xarray as xr
        except ImportError:
            LOGGER.warning("xarray is not available; canonical metadata will be inferred from filenames.")
            return {}
        dataset = None
        try:
            dataset = xr.open_dataset(product_path, decode_times=False)
            return dict(dataset.attrs)
        except Exception as error:
            LOGGER.warning("Failed to read canonical NetCDF attrs from %s: %s", product_path, error)
            return {}
        finally:
            if dataset is not None:
                dataset.close()

    def _read_json_reference(self, reference: str) -> Mapping[str, Any]:
        if not reference:
            raise MetStoreError("File repository JSON reference is empty.")
        try:
            if reference.startswith("s3://"):
                content = self.object_store.read_bytes(reference)
            elif Path(reference).is_absolute():
                content = Path(reference).read_bytes()
            else:
                try:
                    content = self.object_store.read_bytes(reference)
                except (ObjectStoreError, ValueError):
                    content = (Path(self.object_store.root) / reference).read_bytes()
            payload = json.loads(content.decode("utf-8"))
        except (OSError, ObjectStoreError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise MetStoreError(f"Failed to read file repository JSON reference {reference}: {error}") from error
        if not isinstance(payload, Mapping):
            raise MetStoreError(f"File repository JSON reference {reference} must contain a JSON object.")
        return payload

    def _write_forcing_version_sidecar(self, record: Mapping[str, Any]) -> None:
        package_uri = str(record.get("forcing_package_uri") or "")
        if not package_uri:
            return
        try:
            package_key = self.object_store.normalize_key(package_uri).strip("/")
            payload = _json_bytes(_json_safe(record))
            self.object_store.write_bytes_atomic(f"{package_key}/forcing_version_record.json", payload)
        except Exception:
            LOGGER.exception("Failed to write DB-free forcing version sidecar for %s", record.get("forcing_version_id"))

    def _write_forcing_domain_handoff(self, record: Mapping[str, Any]) -> None:
        forcing_version_id = str(record.get("forcing_version_id") or "")
        if not forcing_version_id:
            raise MetStoreError("Cannot write forcing-domain handoff without forcing_version_id.")
        rows = self._forcing_timeseries_rows.get(forcing_version_id, ())
        if not rows:
            raise MetStoreError(f"Cannot write forcing-domain handoff for {forcing_version_id}: no timeseries rows.")

        package_uri = str(record.get("forcing_package_uri") or "")
        if not package_uri:
            raise MetStoreError(f"Cannot write forcing-domain handoff for {forcing_version_id}: missing package URI.")
        package_key = self.object_store.normalize_key(package_uri).strip("/")
        package_manifest_uri = _forcing_package_manifest_uri(record, self.object_store.uri_for_key(package_key))
        package_manifest = self._read_json_reference(package_manifest_uri)

        source_id = str(record.get("source_id") or package_manifest.get("source_id") or rows[0].source_id)
        source_key = source_id.lower()
        cycle_time = _time_value(record.get("cycle_time") or package_manifest.get("cycle_time") or rows[0].valid_time)
        compact_cycle = format_cycle_time(cycle_time)
        model_id = str(record.get("model_id") or package_manifest.get("model_id") or "")
        basin_version_id = str(
            record.get("basin_version_id")
            or package_manifest.get("basin_version_id")
            or rows[0].basin_version_id
        )
        basin_id = str(
            record.get("basin_id")
            or package_manifest.get("basin_id")
            or _lineage_value(record, "basin_id")
            or basin_version_id
        )
        run_id = _handoff_run_id(record, source_key=source_key, compact_cycle=compact_cycle, model_id=model_id)
        start_time = min(_ensure_utc(row.valid_time) for row in rows)
        end_time = max(_ensure_utc(row.valid_time) for row in rows)
        forcing_package_manifest_checksum = str(record.get("checksum") or "")
        if not forcing_package_manifest_checksum:
            raise MetStoreError(
                f"Cannot write forcing-domain handoff for {forcing_version_id}: missing package checksum."
            )

        station_rows = self._handoff_station_rows(
            record=record,
            package_manifest=package_manifest,
            rows=rows,
            basin_id=basin_id,
            basin_version_id=basin_version_id,
            model_id=model_id,
        )
        native_resolution_by_time = _time_lattice_resolution_by_variable_time(rows)
        timeseries_rows = [
            _handoff_timeseries_row(
                row,
                native_resolution=native_resolution_by_time.get((row.variable, _ensure_utc(row.valid_time))),
            )
            for row in sorted(rows, key=_timeseries_sort_key)
        ]
        weight_rows = self._handoff_weight_rows(
            record=record,
            package_manifest=package_manifest,
            source_id=source_id,
            model_id=model_id,
        )
        if not weight_rows:
            raise MetStoreError(f"Cannot write forcing-domain handoff for {forcing_version_id}: no interpolation rows.")

        payload_specs = {
            "station_inventory": ("station_inventory.json", "met.met_station", station_rows),
            "station_timeseries": ("station_timeseries.json", "met.forcing_station_timeseries", timeseries_rows),
            "interpolation_weights": ("interp_weights.json", "met.interp_weight", weight_rows),
        }
        payload_refs: dict[str, dict[str, Any]] = {}
        for role, (filename, table, payload_rows) in payload_specs.items():
            payload_key = f"{package_key}/payloads/{filename}"
            payload_content = _json_bytes(payload_rows)
            payload_uri = self.object_store.write_bytes_atomic(payload_key, payload_content)
            payload_refs[role] = {
                "uri": payload_uri,
                "checksum_sha256": sha256_bytes(payload_content),
                "table": table,
                "row_count": len(payload_rows),
                "content_type": "application/json",
            }

        variables = sorted({row.variable for row in rows})
        units = {variable: _first_unit(rows, variable) for variable in variables}
        payload_refs["station_timeseries"]["variables"] = variables
        payload_refs["station_timeseries"]["units"] = units
        payload_refs["station_timeseries"]["time_lattice"] = _time_lattice(native_resolution_by_time)

        table_row_counts = {
            "met.forcing_version": 1,
            "met.met_station": len(station_rows),
            "met.forcing_station_timeseries": len(timeseries_rows),
            "met.interp_weight": len(weight_rows),
        }
        forcing_package_uri = self.object_store.uri_for_key(package_key)
        forcing_domain_package_uri = self.object_store.uri_for_key(f"{package_key}/forcing_domain_package.json")
        package_envelope = {
            "schema_version": FORCING_DOMAIN_HANDOFF_SCHEMA_VERSION,
            "contract_id": FORCING_DOMAIN_PACKAGE_CONTRACT_ID,
            "run_id": run_id,
            "source_id": source_id,
            "source": source_key,
            "cycle_time": _format_time(cycle_time),
            "start_time": _format_time(start_time),
            "end_time": _format_time(end_time),
            "model_id": model_id,
            "basin_id": basin_id,
            "basin_version_id": basin_version_id,
            "forcing_version_id": forcing_version_id,
            "station_count": len(station_rows),
            "payloads": payload_refs,
            "table_row_counts": table_row_counts,
        }
        package_content = _json_bytes(package_envelope)
        self.object_store.write_bytes_atomic(f"{package_key}/forcing_domain_package.json", package_content)
        package_checksum = sha256_bytes(package_content)

        handoff = {
            **package_envelope,
            "contract_id": FORCING_DOMAIN_HANDOFF_CONTRACT_ID,
            "model_package_uri": self._model_package_uri(model_id, record, package_manifest),
            "forcing_uri": forcing_package_uri,
            "forcing_package_uri": forcing_package_uri,
            FORCING_PACKAGE_MANIFEST_URI_FIELD: package_manifest_uri,
            FORCING_PACKAGE_MANIFEST_CHECKSUM_FIELD: forcing_package_manifest_checksum,
            FORCING_DOMAIN_PACKAGE_MANIFEST_URI_FIELD: forcing_domain_package_uri,
            FORCING_DOMAIN_PACKAGE_MANIFEST_CHECKSUM_FIELD: package_checksum,
            "scenario_id": str(record.get("scenario_id") or f"forecast_{source_key}_deterministic"),
            "run_manifest_uri": self.object_store.uri_for_key(f"runs/{run_id}/input/manifest.json"),
            "output_uri": self.object_store.uri_for_key(f"runs/{run_id}/output/"),
        }
        self.object_store.write_bytes_atomic(
            f"runs/{run_id}/input/forcing_domain_handoff.json",
            _json_bytes(handoff),
        )

    def _handoff_station_rows(
        self,
        *,
        record: Mapping[str, Any],
        package_manifest: Mapping[str, Any],
        rows: Sequence[ForcingTimeseriesRow],
        basin_id: str,
        basin_version_id: str,
        model_id: str,
    ) -> list[dict[str, Any]]:
        station_ids = {row.station_id for row in rows}
        stations_by_id = {
            station.station_id: station
            for station in self._stations_by_basin_version.get(basin_version_id, ())
        }
        manifest_stations = _manifest_station_order(package_manifest)
        ordered_ids = [station_id for station_id in manifest_stations if station_id in station_ids]
        ordered_ids.extend(sorted(station_ids - set(ordered_ids)))

        station_rows: list[dict[str, Any]] = []
        for station_id in ordered_ids:
            manifest_station = manifest_stations.get(station_id, {})
            station = stations_by_id.get(station_id)
            longitude = _float_value(manifest_station.get("longitude"))
            latitude = _float_value(manifest_station.get("latitude"))
            elevation_m = _float_value(manifest_station.get("elevation_m"))
            if station is not None:
                longitude = station.longitude if longitude is None else longitude
                latitude = station.latitude if latitude is None else latitude
                elevation_m = station.elevation_m if elevation_m is None else elevation_m
            if longitude is None or latitude is None or elevation_m is None:
                raise MetStoreError(f"Cannot write forcing-domain handoff: station {station_id} lacks coordinates.")
            forcing_index = manifest_station.get("shud_forcing_index")
            properties = dict(station.properties_json if station is not None else {})
            if forcing_index is not None:
                properties.setdefault("shud_forcing_index", forcing_index)
            forcing_filename = manifest_station.get("forcing_filename")
            if forcing_filename:
                properties.setdefault("forcing_filename", forcing_filename)
            properties.setdefault("source", "qhh.tsd.forc")
            properties.setdefault("basin_id", basin_id)
            properties.setdefault("basin_version_id", basin_version_id)
            properties.setdefault("model_id", model_id)
            station_name = (
                station.station_name
                if station is not None and station.station_name
                else _station_name_from_id(basin_id, station_id)
            )
            station_rows.append(
                {
                    "station_id": station_id,
                    "basin_version_id": basin_version_id,
                    "station_name": station_name,
                    "longitude": longitude,
                    "latitude": latitude,
                    "elevation_m": elevation_m,
                    "station_role": station.station_role if station is not None else "forcing_grid",
                    # §D2 flag ownership: mirror activation belongs to Change 8's cutover flip,
                    # not the runtime producer/file plane. Emit `False` so the ingest lands fresh
                    # rows inactive and the ON CONFLICT DO UPDATE preserves an existing flip.
                    "active_flag": False,
                    "properties_json": _json_safe(properties),
                }
            )
        return station_rows

    def _handoff_weight_rows(
        self,
        *,
        record: Mapping[str, Any],
        package_manifest: Mapping[str, Any],
        source_id: str,
        model_id: str,
    ) -> list[dict[str, Any]]:
        grid_id = str(
            record.get("grid_id")
            or _lineage_value(record, "grid_id")
            or package_manifest.get("grid_id")
            or (package_manifest.get("lineage") or {}).get("grid_id")
            or ""
        )
        scopes = []
        if grid_id:
            scopes.append((source_id, grid_id, model_id))
        scopes.extend(
            scope
            for scope in sorted(self._weights_by_scope)
            if scope[0].lower() == source_id.lower() and scope[2] == model_id and scope not in scopes
        )
        weights: list[InterpolationWeight] = []
        for scope in scopes:
            weights.extend(self._weights_by_scope.get(scope, ()))
        return [
            {
                "source_id": weight.source_id,
                "grid_id": weight.grid_id,
                "model_id": weight.model_id,
                "station_id": weight.station_id,
                "variable": weight.variable,
                "grid_cell_id": weight.grid_cell_id,
                "weight": weight.weight,
                "method": weight.method,
                "grid_signature": weight.grid_signature,
            }
            for weight in sorted(weights, key=_weight_sort_key)
        ]

    def _model_package_uri(
        self,
        model_id: str,
        record: Mapping[str, Any],
        package_manifest: Mapping[str, Any],
    ) -> str:
        for value in (
            record.get("model_package_uri"),
            _lineage_value(record, "model_package_uri"),
            package_manifest.get("model_package_uri"),
            (package_manifest.get("lineage") or {}).get("model_package_uri")
            if isinstance(package_manifest.get("lineage"), Mapping)
            else None,
        ):
            if value:
                return str(value)
        try:
            model = self._model_entry(model_id)
        except Exception:
            return self.object_store.uri_for_key(f"models/{model_id}/package/")
        return str(
            model.get("model_package_uri")
            or model.get("manifest_uri")
            or (model.get("resource_profile") or {}).get("model_package_manifest_uri")
            or self.object_store.uri_for_key(f"models/{model_id}/package/")
        )


def _safe_direct_grid_package_member(value: str) -> str:
    normalized = value.replace("\\", "/").strip()
    path = Path(normalized)
    if (
        not normalized
        or path.is_absolute()
        or normalized.startswith("/")
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise DirectGridContractError(
            "Direct-grid sp_att_path must be a safe model-package-relative path.",
            field="sp_att_path",
            details={"actual": value},
        )
    return normalized


def _direct_grid_snapshot_bbox(
    payload: Mapping[str, Any],
    *,
    source_id: str,
    grid_id: str,
    grid_signature: str,
) -> tuple[float, float, float, float, uuid.UUID, datetime | None]:
    try:
        snapshot_source = normalize_source_id(str(payload["source_id"]))
        snapshot_grid_id = str(payload["grid_id"])
        snapshot_signature = str(payload["grid_signature"])
        snapshot_id = uuid.UUID(str(payload["grid_snapshot_id"]))
        bbox_south = float(payload["bbox_south"])
        bbox_north = float(payload["bbox_north"])
        bbox_west = float(payload["bbox_west"])
        bbox_east = float(payload["bbox_east"])
    except (KeyError, TypeError, ValueError) as error:
        raise MetStoreError(f"Invalid canonical_grid_snapshot projection: {error}") from error
    if (snapshot_source, snapshot_grid_id, snapshot_signature) != (
        source_id,
        grid_id,
        grid_signature,
    ):
        raise MetStoreError(
            "canonical_grid_snapshot projection identity does not match its direct-grid contract."
        )
    superseded_at_raw = payload.get("superseded_at")
    superseded_at: datetime | None = None
    if superseded_at_raw not in (None, ""):
        try:
            superseded_at = datetime.fromisoformat(str(superseded_at_raw).replace("Z", "+00:00"))
        except ValueError as error:
            raise MetStoreError(
                "canonical_grid_snapshot.superseded_at must be an ISO-8601 timestamp."
            ) from error
        if superseded_at.tzinfo is None:
            raise MetStoreError("canonical_grid_snapshot.superseded_at must include a timezone.")
    return (
        bbox_south,
        bbox_north,
        bbox_west,
        bbox_east,
        snapshot_id,
        superseded_at,
    )


def db_free_repository_enabled() -> bool:
    repository_backend = os.getenv("NHMS_FORCING_REPOSITORY_BACKEND", "").strip().lower()
    if repository_backend in {"file", "none", "object-store", "object_store", "db-free", "db_free"}:
        return True
    return _env_flag("NHMS_FORCING_DB_FREE") or _env_flag("NHMS_SCHEDULER_DB_FREE_REQUIRED")


def _env_flag(name: str, *, default: bool = False) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value.strip() == "":
        return default
    return raw_value.strip().lower() in _TRUTHY


def _parse_shud_tsd_forc_stations(
    content: str,
    *,
    basin_version_id: str,
    station_prefix: str,
) -> tuple[MetStation, ...]:
    stations: list[MetStation] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("ID") or stripped.startswith("/"):
            continue
        parts = stripped.split()
        if len(parts) < 7 or not parts[0].isdigit():
            continue
        forcing_index = int(parts[0])
        try:
            longitude = float(parts[1])
            latitude = float(parts[2])
            elevation = float(parts[5])
        except ValueError:
            continue
        filename = parts[6]
        if elevation < 0:
            elevation = 0.0
        station_id = f"{station_prefix}_forc_{forcing_index:03d}"
        stations.append(
            MetStation(
                station_id=station_id,
                basin_version_id=basin_version_id,
                longitude=longitude,
                latitude=latitude,
                elevation_m=elevation,
                station_role="forcing_grid",
                station_name=f"{station_prefix.upper()} forcing station {forcing_index:03d}",
                properties_json={
                    "shud_forcing_index": forcing_index,
                    "forcing_filename": filename,
                    "manifest_authority": True,
                },
            )
        )
    return tuple(stations)


def _grid_id_for_source(source_id: str) -> str:
    return {
        "gfs": "gfs_0p25",
        "ERA5": "era5_0p25",
        "IFS": "ifs_0p25",
    }[normalize_source_id(source_id)]


def _grid_definition_uri_for_source(source_id: str) -> str:
    normalized = normalize_source_id(source_id)
    return {
        "gfs": "canonical/gfs/grid/gfs_0p25/grid.json",
        "ERA5": "canonical/ERA5/grid/era5_0p25/grid.json",
        "IFS": "canonical/IFS/grid/ifs_0p25/grid.json",
    }[normalized]


def _native_time_resolution_for_source(source_id: str) -> str:
    return "1h" if normalize_source_id(source_id) == "ERA5" else "3h"


def _lead_sort_key(product: CanonicalProduct) -> tuple[int, datetime, str]:
    lead_time = product.lead_time_hours if product.lead_time_hours is not None else 999_999
    return (int(lead_time), product.cycle_time, product.canonical_product_id)


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _json_object(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, Mapping) else {}
    return {}


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        return {str(key): _json_safe(nested) for key, nested in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_json_safe(item) for item in value]
    return value


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, default=_json_default, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return _format_time(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable.")


def _time_value(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _ensure_utc(value)
    return parse_cycle_time(str(value))


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _format_time(value: datetime) -> str:
    return _ensure_utc(value).isoformat().replace("+00:00", "Z")


def _forcing_package_manifest_uri(record: Mapping[str, Any], package_uri: str) -> str:
    lineage = record.get("lineage_json")
    if isinstance(lineage, Mapping):
        value = lineage.get("forcing_package_manifest_uri")
        if value:
            return str(value)
    return f"{package_uri.rstrip('/')}/forcing_package.json"


def _lineage_value(record: Mapping[str, Any], key: str) -> Any:
    lineage = record.get("lineage_json")
    if isinstance(lineage, Mapping):
        return lineage.get(key)
    return None


def _handoff_run_id(
    record: Mapping[str, Any],
    *,
    source_key: str,
    compact_cycle: str,
    model_id: str,
) -> str:
    lineage_run_id = _lineage_value(record, "run_id")
    if lineage_run_id:
        return str(lineage_run_id)
    value = record.get("run_id")
    if value:
        return str(value)
    return f"fcst_{source_key}_{compact_cycle}_{model_id}"


def _manifest_station_order(package_manifest: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw_stations = package_manifest.get("station_order")
    stations: dict[str, Mapping[str, Any]] = {}
    if not isinstance(raw_stations, Sequence) or isinstance(raw_stations, str | bytes | bytearray):
        return stations
    for index, item in enumerate(raw_stations, start=1):
        if isinstance(item, Mapping):
            station_id = str(item.get("station_id") or "").strip()
            if station_id:
                stations[station_id] = item
        elif isinstance(item, str) and item.strip():
            stations[item.strip()] = {"station_id": item.strip(), "shud_forcing_index": index}
    return stations


def _float_value(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        converted = float(value)
    except (TypeError, ValueError):
        return None
    return converted


def _station_name_from_id(basin_id: str, station_id: str) -> str:
    match = re.search(r"(\d+)$", station_id)
    prefix = _station_name_prefix(basin_id)
    if match:
        return f"{prefix} forcing station {int(match.group(1)):03d}"
    return f"{prefix} forcing station {station_id}"


def _station_name_prefix(basin_id: str) -> str:
    value = basin_id.strip()
    if value.lower().startswith("basins_"):
        value = value[7:]
    return value.upper()


def _handoff_timeseries_row(row: ForcingTimeseriesRow, *, native_resolution: str | None = None) -> dict[str, Any]:
    return {
        "forcing_version_id": row.forcing_version_id,
        "basin_version_id": row.basin_version_id,
        "station_id": row.station_id,
        "valid_time": _format_time(row.valid_time),
        "source_id": row.source_id,
        "variable": row.variable,
        "value": row.value,
        "unit": row.unit,
        "native_resolution": native_resolution or row.native_resolution,
        "quality_flag": row.quality_flag,
    }


def _timeseries_sort_key(row: ForcingTimeseriesRow) -> tuple[str, datetime, str]:
    return (row.station_id, _ensure_utc(row.valid_time), row.variable)


def _weight_sort_key(weight: InterpolationWeight) -> tuple[str, str, str, str, str, str]:
    return (
        weight.source_id,
        weight.grid_id,
        weight.model_id,
        weight.station_id,
        weight.variable,
        weight.grid_cell_id,
    )


def _first_unit(rows: Sequence[ForcingTimeseriesRow], variable: str) -> str:
    for row in rows:
        if row.variable == variable:
            return row.unit
    return ""


def _time_lattice(
    native_resolution_by_time: Mapping[tuple[str, datetime], str],
) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    variables = sorted({variable for variable, _valid_time in native_resolution_by_time})
    for variable in variables:
        ordered_points = sorted(
            (valid_time, native_resolution)
            for (row_variable, valid_time), native_resolution in native_resolution_by_time.items()
            if row_variable == variable
        )
        if not ordered_points:
            continue

        segment_start, segment_resolution = ordered_points[0]
        segment_end = segment_start
        segment_delta = _native_resolution_delta(segment_resolution)
        for valid_time, native_resolution in ordered_points[1:]:
            expected_next = segment_end + segment_delta if segment_delta is not None else None
            if native_resolution == segment_resolution and expected_next == valid_time:
                segment_end = valid_time
                continue
            segments.append(_time_lattice_segment(variable, segment_start, segment_end, segment_resolution))
            segment_start = valid_time
            segment_end = valid_time
            segment_resolution = native_resolution
            segment_delta = _native_resolution_delta(segment_resolution)
        segments.append(_time_lattice_segment(variable, segment_start, segment_end, segment_resolution))
    return segments


def _time_lattice_resolution_by_variable_time(
    rows: Sequence[ForcingTimeseriesRow],
) -> dict[tuple[str, datetime], str]:
    native_resolution_by_time: dict[tuple[str, datetime], str] = {}
    variables = sorted({row.variable for row in rows})
    for variable in variables:
        rows_for_variable = [row for row in rows if row.variable == variable]
        labels_by_time: dict[datetime, set[str]] = {}
        for row in rows_for_variable:
            label = str(row.native_resolution or "").strip()
            if label:
                labels_by_time.setdefault(_ensure_utc(row.valid_time), set()).add(label)
        ordered_times = sorted({_ensure_utc(row.valid_time) for row in rows_for_variable})
        if not ordered_times:
            continue
        for index, valid_time in enumerate(ordered_times):
            if index > 0:
                inferred = _duration_label(valid_time - ordered_times[index - 1])
            elif len(ordered_times) > 1:
                inferred = _duration_label(ordered_times[1] - valid_time)
            else:
                inferred = None
            existing = sorted(labels_by_time.get(valid_time, ()))
            native_resolution = inferred or (existing[0] if existing else None)
            if native_resolution:
                native_resolution_by_time[(variable, valid_time)] = native_resolution
    return native_resolution_by_time


def _time_lattice_segment(variable: str, start: datetime, end: datetime, native_resolution: str) -> dict[str, Any]:
    return {
        "variable": variable,
        "valid_time_start": _format_time(start),
        "valid_time_end": _format_time(end),
        "native_resolution": native_resolution,
    }


def _duration_label(delta: timedelta) -> str | None:
    total_seconds = int(delta.total_seconds())
    if total_seconds <= 0:
        return None
    if total_seconds % 3600 == 0:
        return f"{total_seconds // 3600}h"
    if total_seconds % 60 == 0:
        return f"{total_seconds // 60}min"
    return None


def _native_resolution_delta(native_resolution: str) -> timedelta | None:
    match = _NATIVE_RESOLUTION_RE.match(native_resolution)
    if match is None:
        return None
    value = int(match.group("value"))
    if match.group("unit").lower() == "h":
        return timedelta(hours=value)
    return timedelta(minutes=value)
