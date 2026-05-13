from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from packages.common.source_identity import normalize_source_id
from services.orchestrator.chain import scenario_for_source
from workers.canonical_converter import cli as canonical_cli
from workers.forcing_producer import cli as forcing_cli
from workers.forcing_producer.producer import ForcingProducer, ForcingProducerConfig


def test_normalize_source_id_gfs_variants() -> None:
    assert normalize_source_id("GFS") == "gfs"
    assert normalize_source_id("gfs") == "gfs"
    assert normalize_source_id("Gfs") == "gfs"


def test_normalize_source_id_era5() -> None:
    assert normalize_source_id("ERA5") == "ERA5"
    assert normalize_source_id("era5") == "ERA5"
    assert normalize_source_id("Era5") == "ERA5"


def test_normalize_source_id_ifs() -> None:
    assert normalize_source_id("IFS") == "IFS"
    assert normalize_source_id("ifs") == "IFS"
    assert normalize_source_id("Ifs") == "IFS"


def test_normalize_source_id_unknown_raises() -> None:
    with pytest.raises(ValueError, match="Unknown source_id"):
        normalize_source_id("UNKNOWN")


def test_gfs_canonical_and_forcing_use_same_source_id(monkeypatch: pytest.MonkeyPatch) -> None:
    object_store = _RecordingObjectStore()
    converter = SimpleNamespace(
        config=SimpleNamespace(source_id="gfs"),
        object_store=object_store,
        convert_manifest_uri=lambda _uri: SimpleNamespace(status="canonical_ready", products=()),
    )
    monkeypatch.setattr(canonical_cli.CanonicalConverter, "from_env", staticmethod(lambda: converter))

    canonical_cli._convert("GFS", "2026050700")
    forcing_source_id, _, _, _ = forcing_cli._resolve_produce_args("GFS", "2026050700", "demo_model", None, None, None)

    assert object_store.keys == ["raw/gfs/2026050700/manifest.json"]
    assert forcing_source_id == "gfs"


def test_era5_fallback_uses_normalized_gfs_id(tmp_path: Path) -> None:
    repository = _FallbackRecordingRepository()
    producer = ForcingProducer(
        config=ForcingProducerConfig(workspace_root=tmp_path),
        repository=repository,
    )

    result = producer._apply_era5_latency_fallback(
        source_id="ERA5",
        cycle_time=datetime(2026, 5, 7, tzinfo=UTC),
        products_by_variable={"net_radiation": {}},
        required_variables=("net_radiation",),
    )

    assert result is None
    assert repository.source_ids == ["gfs"]


@pytest.mark.parametrize(
    ("source_id", "expected"),
    [
        ("GFS", "gfs"),
        ("gFs", "gfs"),
        ("ERA5", "ERA5"),
        ("era5", "ERA5"),
        ("IFS", "IFS"),
        ("iFs", "IFS"),
    ],
)
def test_user_input_case_insensitive_maps_to_canonical_storage_id(source_id: str, expected: str) -> None:
    assert normalize_source_id(source_id) == expected


def test_scenario_id_preserves_business_semantics() -> None:
    assert scenario_for_source("gfs") == "forecast_gfs_deterministic"


class _RecordingObjectStore:
    def __init__(self) -> None:
        self.keys: list[str] = []

    def uri_for_key(self, key: str) -> str:
        self.keys.append(key)
        return f"memory://{key}"


class _FallbackRecordingRepository:
    def __init__(self) -> None:
        self.source_ids: list[str] = []

    def list_fallback_canonical_products(
        self,
        *,
        source_id: str,
        start_time: Any,
        end_time: Any,
        variables: tuple[str, ...],
    ) -> tuple[Any, ...]:
        self.source_ids.append(source_id)
        return ()
