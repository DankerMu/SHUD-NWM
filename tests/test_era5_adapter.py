from __future__ import annotations

import importlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from packages.common.mock_grib import build_mock_era5_payload, encode_mock_grib2
from packages.common.object_store import LocalObjectStore, sha256_bytes

base = importlib.import_module("workers.data_adapters.base")
cli_module = importlib.import_module("workers.data_adapters.cli")
era5_module = importlib.import_module("workers.data_adapters.era5_adapter")

DownloadManifest = base.DownloadManifest
ERA5Adapter = era5_module.ERA5Adapter
ERA5AdapterConfig = era5_module.ERA5AdapterConfig
MockCDSClient = era5_module.MockCDSClient
parse_era5_cycle_time = era5_module.parse_era5_cycle_time


class FakeMetRepository:
    def __init__(self) -> None:
        self.data_sources: dict[str, dict[str, Any]] = {}
        self.cycles: dict[tuple[str, datetime], dict[str, Any]] = {}
        self.ensure_calls = 0
        self.cycle_upsert_calls = 0
        self.update_calls = 0

    def ensure_data_source(self, **kwargs: Any) -> dict[str, Any]:
        self.ensure_calls += 1
        self.data_sources[kwargs["source_id"]] = dict(kwargs)
        return self.data_sources[kwargs["source_id"]]

    def upsert_forecast_cycle(self, **kwargs: Any) -> dict[str, Any]:
        self.cycle_upsert_calls += 1
        key = (kwargs["source_id"], kwargs["cycle_time"])
        existing = self.cycles.get(key, {})
        existing.update(kwargs)
        self.cycles[key] = existing
        return existing

    def update_forecast_cycle(self, **kwargs: Any) -> dict[str, Any] | None:
        self.update_calls += 1
        key = (kwargs["source_id"], kwargs["cycle_time"])
        cycle = self.cycles.setdefault(key, {"source_id": kwargs["source_id"], "cycle_time": kwargs["cycle_time"]})
        for field in ("status", "manifest_uri", "retry_count", "error_code", "error_message"):
            if kwargs.get(field) is not None:
                cycle[field] = kwargs[field]
        return cycle

    def get_forecast_cycle(self, **kwargs: Any) -> dict[str, Any] | None:
        return self.cycles.get((kwargs["source_id"], kwargs["cycle_time"]))


def build_adapter(
    tmp_path: Path,
    *,
    repository: FakeMetRepository | None = None,
    cds_client: Any | None = None,
    max_retries: int = 2,
    variables: tuple[str, ...] | None = None,
) -> ERA5Adapter:
    config = ERA5AdapterConfig(
        workspace_root=tmp_path,
        max_retries=max_retries,
        retry_backoff_seconds=(0,),
        variables=variables or ERA5AdapterConfig().variables,
    )
    return ERA5Adapter(
        config=config,
        repository=repository or FakeMetRepository(),
        object_store=LocalObjectStore(tmp_path),
        cds_client=cds_client or MockCDSClient(),
        sleeper=lambda _seconds: None,
        now=lambda: datetime(2026, 5, 8, tzinfo=UTC),
    )


def single_entry_manifest(adapter: ERA5Adapter) -> DownloadManifest:
    manifest = adapter.build_manifest("2026-04-20", forecast_hours=[0])
    return DownloadManifest(
        source_id=manifest.source_id, cycle_time=manifest.cycle_time, entries=(manifest.entries[0],)
    )


def test_cycle_discovery_upserts_available_era5_date(tmp_path: Path) -> None:
    repository = FakeMetRepository()
    client = MockCDSClient()
    adapter = build_adapter(tmp_path, repository=repository, cds_client=client)

    discoveries = adapter.discover_cycles("2026-04-20")

    assert len(discoveries) == 1
    assert discoveries[0].cycle_id == "era5_2026042000"
    assert discoveries[0].available is True
    assert discoveries[0].status == "discovered"
    assert repository.data_sources["ERA5"]["adapter_name"] == "era5"
    assert repository.data_sources["ERA5"]["native_format"] == "GRIB"
    assert repository.cycles[("ERA5", parse_era5_cycle_time("2026-04-20"))]["status"] == "discovered"
    assert client.availability_requests[0]["variable"] == list(adapter.config.variables)


def test_recent_date_is_not_yet_available_and_not_upserted(tmp_path: Path) -> None:
    repository = FakeMetRepository()
    adapter = build_adapter(tmp_path, repository=repository)

    discoveries = adapter.discover_cycles("2026-05-05")

    assert discoveries[0].available is False
    assert discoveries[0].status == "not_yet_available"
    assert repository.cycles == {}


def test_manifest_contains_8_variables_times_24_hourly_steps(tmp_path: Path) -> None:
    adapter = build_adapter(tmp_path)

    manifest = adapter.build_manifest("2026-04-20")

    assert len(manifest.entries) == 8 * 24
    assert manifest.metadata["total_file_count"] == 192
    assert manifest.metadata["variable_count"] == 8
    assert manifest.manifest_uri == "raw/ERA5/2026-04-20/manifest.json"
    assert manifest.entries[0].local_key == "raw/ERA5/2026-04-20/2m_temperature_00.grib"
    assert manifest.entries[-1].local_key == "raw/ERA5/2026-04-20/surface_net_thermal_radiation_23.grib"
    assert adapter.object_store.exists("raw/ERA5/2026-04-20/manifest.json")


def test_cds_request_construction_for_download(tmp_path: Path) -> None:
    client = MockCDSClient()
    adapter = build_adapter(tmp_path, cds_client=client)
    manifest = single_entry_manifest(adapter)

    result = adapter.download_plan(manifest)

    request = client.retrieve_requests[0]
    assert result.status == "raw_complete"
    assert request["dataset"] == "reanalysis-era5-single-levels"
    assert request["variable"] == "2m_temperature"
    assert request["year"] == "2026"
    assert request["month"] == "04"
    assert request["day"] == "20"
    assert request["time"] == "00:00"
    assert request["area"] == [55.0, 70.0, 15.0, 140.0]
    assert request["timeout_seconds"] == 7200.0


def test_timeout_retry_behavior_succeeds_after_retry(tmp_path: Path) -> None:
    client = MockCDSClient(failures_before_success=1)
    adapter = build_adapter(tmp_path, cds_client=client, max_retries=2)
    manifest = single_entry_manifest(adapter)

    result = adapter.download_plan(manifest)

    assert result.status == "raw_complete"
    assert result.retry_count == 1
    assert len(client.retrieve_requests) == 2


def test_checksum_verification_reports_mismatch(tmp_path: Path) -> None:
    adapter = build_adapter(tmp_path)
    manifest = single_entry_manifest(adapter)
    entry = manifest.entries[0]
    payload = encode_mock_grib2(build_mock_era5_payload(manifest.cycle_time, entry.variable, entry.forecast_hour))
    adapter.object_store.write_bytes_atomic(entry.local_key, payload)
    bad_entry = replace(entry, expected_checksum="bad")
    bad_manifest = DownloadManifest(source_id=manifest.source_id, cycle_time=manifest.cycle_time, entries=(bad_entry,))

    result = adapter.verify_manifest(bad_manifest)

    assert result.status == "partial_fail"
    assert result.failures[0].error_code == "CHECKSUM_MISMATCH"


def test_idempotency_skips_existing_file_when_checksum_matches(tmp_path: Path) -> None:
    client = MockCDSClient(failure_factory=lambda: RuntimeError("should not retrieve"))
    adapter = build_adapter(tmp_path, cds_client=client)
    manifest = single_entry_manifest(adapter)
    entry = manifest.entries[0]
    payload = encode_mock_grib2(build_mock_era5_payload(manifest.cycle_time, entry.variable, entry.forecast_hour))
    checksum = sha256_bytes(payload)
    adapter.object_store.write_bytes_atomic(entry.local_key, payload)
    checksum_manifest = DownloadManifest(
        source_id=manifest.source_id,
        cycle_time=manifest.cycle_time,
        entries=(replace(entry, expected_checksum=checksum),),
    )
    adapter.repository.cycles[("ERA5", manifest.cycle_time)] = {
        "source_id": "ERA5",
        "cycle_time": manifest.cycle_time,
        "status": "raw_complete",
    }

    result = adapter.download_plan(checksum_manifest)

    assert result.status == "already_done"
    assert result.files[0].status == "already_done"
    assert client.retrieve_requests == []


def test_era5_cli_invocation_downloads_with_area(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    adapter = build_adapter(tmp_path, variables=("2m_temperature",))
    monkeypatch.setattr(cli_module.ERA5Adapter, "from_env", staticmethod(lambda area=None: adapter))

    exit_code = cli_module._argparse_era5_main(["download", "--date", "2026-04-20", "--area", "55,70,15,140"])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["status"] == "raw_complete"
    assert output["files"] == 24
