from __future__ import annotations

import importlib
from datetime import datetime
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest

from packages.common.object_store import LocalObjectStore, sha256_bytes

base = importlib.import_module("workers.data_adapters.base")
ifs_module = importlib.import_module("workers.data_adapters.ifs_adapter")

DownloadManifest = base.DownloadManifest
ManifestEntry = base.ManifestEntry
parse_cycle_time = base.parse_cycle_time
FileUnavailableError = ifs_module.FileUnavailableError
IFSAdapter = ifs_module.IFSAdapter
IFSAdapterConfig = ifs_module.IFSAdapterConfig


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
    downloader: Any | None = None,
    availability_checker: Any | None = None,
    max_retries: int = 1,
    max_wait_seconds: float = 0,
    poll_interval_seconds: float = 0,
    sleeper: Any | None = None,
) -> IFSAdapter:
    config = IFSAdapterConfig(
        workspace_root=tmp_path,
        poll_interval_seconds=poll_interval_seconds,
        max_wait_seconds=max_wait_seconds,
        max_retries=max_retries,
        retry_backoff_seconds=(0,),
    )
    return IFSAdapter(
        config=config,
        repository=repository or FakeMetRepository(),
        object_store=LocalObjectStore(tmp_path),
        downloader=downloader or (lambda _url: b"GRIB IFS mock bytes 7777"),
        availability_checker=availability_checker or (lambda _url: True),
        sleeper=sleeper or (lambda _seconds: None),
    )


def one_entry_manifest(tmp_path: Path, expected_checksum: str | None = None) -> tuple[IFSAdapter, DownloadManifest]:
    adapter = build_adapter(tmp_path)
    cycle_time = parse_cycle_time("2026050100")
    entry = ManifestEntry(
        remote_url="ecmwf-opendata://ecmwf/ifs/2026050100/ifs.t00z.f000.2t.grib2",
        local_key="raw/IFS/2026050100/ifs.t00z.f000.2t.grib2",
        variable="2t",
        forecast_hour=0,
        expected_checksum=expected_checksum,
        metadata={"cycle_time": cycle_time.isoformat()},
    )
    return adapter, DownloadManifest(source_id="IFS", cycle_time=cycle_time, entries=(entry,))


def http_429(retry_after: str = "120") -> HTTPError:
    headers = Message()
    headers["Retry-After"] = retry_after
    return HTTPError("mock://ifs", 429, "too many requests", headers, None)


def test_discover_cycles_normal_date_range_and_all_day_unavailable(tmp_path: Path) -> None:
    repository = FakeMetRepository()
    adapter = build_adapter(tmp_path, repository=repository, availability_checker=lambda _url: True)

    cycles = adapter.discover_cycles("2026-05-01")
    ranged = adapter.discover_cycles("2026-05-01", end_date="2026-05-03")

    assert [cycle.cycle_hour for cycle in cycles] == [0, 6, 12, 18]
    assert all(cycle.available for cycle in cycles)
    assert len(ranged) == 12
    assert {cycle["status"] for cycle in repository.cycles.values()} == {"discovered"}

    unavailable_repository = FakeMetRepository()
    unavailable_adapter = build_adapter(
        tmp_path / "unavailable",
        repository=unavailable_repository,
        availability_checker=lambda _url: False,
    )
    unavailable = unavailable_adapter.discover_cycles("2026-05-01")

    assert len(unavailable) == 4
    assert not any(cycle.available for cycle in unavailable)
    assert unavailable_repository.cycles == {}


def test_build_manifest_uses_cycle_specific_lead_policy_and_custom_hours(tmp_path: Path) -> None:
    adapter = build_adapter(tmp_path)

    manifest_00 = adapter.build_manifest("2026050100")
    manifest_06 = adapter.build_manifest("2026050106")
    custom = adapter.build_manifest("2026050112", forecast_hours=[0, 3, 6])

    assert len(manifest_00.entries) == 57 * 8
    assert manifest_00.metadata["max_lead_hours"] == 168
    assert manifest_00.entries[0].local_key == "raw/IFS/2026050100/ifs.t00z.f000.2t.grib2"
    assert manifest_00.entries[-1].local_key == "raw/IFS/2026050100/ifs.t00z.f168.str.grib2"
    assert adapter.object_store.exists("raw/IFS/2026050100/manifest.json")

    assert len(manifest_06.entries) == 49 * 8
    assert manifest_06.metadata["max_lead_hours"] == 144

    assert len(custom.entries) == 3 * 8
    assert sorted({entry.forecast_hour for entry in custom.entries}) == [0, 3, 6]


def test_build_manifest_honors_env_forecast_end_hour_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IFS_FORECAST_END_HOUR", "144")
    adapter = build_adapter(tmp_path)

    manifest = adapter.build_manifest("2026050100")

    assert len(manifest.entries) == 49 * 8
    assert manifest.metadata["max_lead_hours"] == 144
    assert manifest.entries[-1].local_key == "raw/IFS/2026050100/ifs.t00z.f144.str.grib2"


def test_build_manifest_honors_env_forecast_start_hour_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IFS_FORECAST_START_HOUR", "3")
    monkeypatch.setenv("IFS_FORECAST_END_HOUR", "144")
    adapter = build_adapter(tmp_path)

    manifest = adapter.build_manifest("2026050100")

    assert len(manifest.entries) == 48 * 8
    assert manifest.entries[0].local_key == "raw/IFS/2026050100/ifs.t00z.f003.2t.grib2"
    assert manifest.entries[-1].local_key == "raw/IFS/2026050100/ifs.t00z.f144.str.grib2"


@pytest.mark.parametrize(
    ("cycle_time", "forecast_hours"),
    [
        ("2026050106", [147]),
        ("2026050118", [147]),
        ("2026050100", [171]),
        ("2026050112", [171]),
    ],
)
def test_build_manifest_rejects_cycle_specific_invalid_forecast_hours(
    tmp_path: Path,
    cycle_time: str,
    forecast_hours: list[int],
) -> None:
    adapter = build_adapter(tmp_path)

    with pytest.raises(ValueError, match="IFS forecast hour"):
        adapter.build_manifest(cycle_time, forecast_hours=forecast_hours)


def test_download_plan_normal_idempotent_retry_and_mirror_switch(tmp_path: Path) -> None:
    content = b"GRIB IFS payload 7777"
    calls: list[str] = []

    def flaky_downloader(url: str) -> bytes:
        calls.append(url)
        if len(calls) == 1:
            raise RuntimeError("temporary network failure")
        return content

    adapter, manifest = one_entry_manifest(tmp_path, expected_checksum=sha256_bytes(content))
    adapter.downloader = flaky_downloader
    adapter.config = IFSAdapterConfig(
        workspace_root=tmp_path,
        max_retries=2,
        retry_backoff_seconds=(0,),
        max_wait_seconds=0,
        poll_interval_seconds=0,
    )

    result = adapter.download_plan(manifest)

    assert result.status == "raw_complete"
    assert result.retry_count == 1
    assert result.files[0].status == "downloaded"
    assert calls == ["ecmwf-opendata://ecmwf/ifs/2026050100/ifs.t00z.f000.2t.grib2"] * 2

    repository = adapter.repository
    repository.cycles[("IFS", manifest.cycle_time)] = {
        "source_id": "IFS",
        "cycle_time": manifest.cycle_time,
        "status": "raw_complete",
    }
    calls.clear()
    second = adapter.download_plan(manifest)

    assert second.status == "already_done"
    assert second.files[0].status == "already_done"
    assert calls == []

    mirror_calls: list[str] = []

    def mirror_downloader(url: str) -> bytes:
        mirror_calls.append(url)
        if "://ecmwf/" in url:
            raise RuntimeError("primary failed")
        return b"GRIB mirror payload 7777"

    mirror_adapter, mirror_manifest = one_entry_manifest(tmp_path / "mirror")
    mirror_adapter.downloader = mirror_downloader
    mirror_result = mirror_adapter.download_plan(mirror_manifest)

    assert mirror_result.status == "raw_complete"
    assert "://ecmwf/" in mirror_calls[0]
    assert mirror_calls[:2] == ["ecmwf-opendata://ecmwf/ifs/2026050100/ifs.t00z.f000.2t.grib2"] * 2
    assert "://aws/" in mirror_calls[2]


def test_download_plan_rate_limit_honors_retry_after_and_switches_mirror(tmp_path: Path) -> None:
    sleeps: list[float] = []
    calls: list[str] = []

    def downloader(url: str) -> bytes:
        calls.append(url)
        if "://ecmwf/" in url:
            raise http_429("120")
        return b"GRIB rate limit fallback 7777"

    adapter = build_adapter(
        tmp_path,
        downloader=downloader,
        max_wait_seconds=300,
        poll_interval_seconds=60,
        sleeper=sleeps.append,
    )
    manifest = adapter.build_manifest("2026050100", forecast_hours=[0])
    single = DownloadManifest(source_id="IFS", cycle_time=manifest.cycle_time, entries=(manifest.entries[0],))

    result = adapter.download_plan(single)

    assert result.status == "raw_complete"
    assert sleeps == [120.0]
    assert "://aws/" in calls[1]


def test_download_plan_polling_timeout_records_failure(tmp_path: Path) -> None:
    repository = FakeMetRepository()

    def unavailable(_url: str) -> bytes:
        raise FileUnavailableError("not published", attempts=1)

    adapter, manifest = one_entry_manifest(tmp_path)
    adapter.repository = repository
    adapter.downloader = unavailable

    result = adapter.download_plan(manifest)

    cycle = repository.cycles[("IFS", manifest.cycle_time)]
    assert result.status == "failed_download"
    assert cycle["status"] == "failed_download"
    assert cycle["error_code"] == "POLL_TIMEOUT"


def test_download_plan_persistent_rate_limit_records_failure(tmp_path: Path) -> None:
    adapter = build_adapter(tmp_path, downloader=lambda _url: (_ for _ in ()).throw(http_429("10")), max_wait_seconds=0)
    manifest = adapter.build_manifest("2026050100", forecast_hours=[0])
    single = DownloadManifest(source_id="IFS", cycle_time=manifest.cycle_time, entries=(manifest.entries[0],))

    result = adapter.download_plan(single)

    assert result.status == "failed_download"
    assert result.files[0].error_code == "RATE_LIMITED"
    assert "rate limited" in (result.files[0].error_message or "")


def test_verify_manifest_pass_missing_empty_and_checksum(tmp_path: Path) -> None:
    content = b"GRIB checksum payload 7777"
    checksum = sha256_bytes(content)
    adapter, manifest = one_entry_manifest(tmp_path, expected_checksum=checksum)
    entry = manifest.entries[0]
    adapter.object_store.write_bytes_atomic(entry.local_key, content)

    assert adapter.verify_manifest(manifest).passed is True

    missing = DownloadManifest(
        source_id="IFS",
        cycle_time=manifest.cycle_time,
        entries=(
            ManifestEntry(
                remote_url=entry.remote_url,
                local_key="raw/IFS/2026050100/missing.grib2",
                variable="2t",
                forecast_hour=0,
            ),
        ),
    )
    missing_result = adapter.verify_manifest(missing)
    assert missing_result.status == "failed"
    assert missing_result.failures[0].error_code == "MISSING_FILE"

    adapter.object_store.write_bytes_atomic(entry.local_key, b"")
    empty_result = adapter.verify_manifest(manifest)
    assert empty_result.status == "failed"
    assert empty_result.failures[0].error_code == "EMPTY_FILE"


def test_verify_manifest_checksum_mismatch(tmp_path: Path) -> None:
    content = b"GRIB checksum payload 7777"
    adapter, manifest = one_entry_manifest(tmp_path, expected_checksum="bad")
    entry = manifest.entries[0]
    adapter.object_store.write_bytes_atomic(entry.local_key, content)

    result = adapter.verify_manifest(manifest)

    assert result.status == "failed"
    assert result.failures[0].error_code == "CHECKSUM_MISMATCH"


def test_verify_manifest_invalid_grib(tmp_path: Path) -> None:
    adapter, manifest = one_entry_manifest(tmp_path)
    entry = manifest.entries[0]
    adapter.object_store.write_bytes_atomic(entry.local_key, b"<html>error</html>")

    result = adapter.verify_manifest(manifest)

    assert result.status == "failed"
    assert result.failures[0].error_code == "INVALID_GRIB"


def test_initialize_data_source_registers_correctly_and_is_idempotent(tmp_path: Path) -> None:
    repository = FakeMetRepository()
    adapter = build_adapter(tmp_path, repository=repository)

    adapter.initialize_data_source()
    adapter.initialize_data_source()

    assert len(repository.data_sources) == 1
    assert repository.ensure_calls == 2
    source = repository.data_sources["IFS"]
    assert source["source_name"] == "IFS Open Data"
    assert source["native_format"] == "GRIB2"
    assert source["adapter_name"] == "ifs_adapter"
    assert source["config_json"]["lead_time_policy"]["06"] == 144
