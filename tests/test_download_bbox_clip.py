from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from packages.common.object_store import LocalObjectStore

region_module = importlib.import_module("workers.data_adapters.region")
gfs_module = importlib.import_module("workers.data_adapters.gfs_adapter")
ifs_module = importlib.import_module("workers.data_adapters.ifs_adapter")

GeoBBox = region_module.GeoBBox
china_buffered_bbox_from_env = region_module.china_buffered_bbox_from_env
GFSAdapter = gfs_module.GFSAdapter
GFSAdapterConfig = gfs_module.GFSAdapterConfig
IFSAdapter = ifs_module.IFSAdapter
IFSAdapterConfig = ifs_module.IFSAdapterConfig
CdoMissingError = ifs_module.CdoMissingError
CdoClipError = ifs_module.CdoClipError

BBOX_ENV_VARS = (
    "NHMS_DOWNLOAD_BBOX_SOUTH",
    "NHMS_DOWNLOAD_BBOX_NORTH",
    "NHMS_DOWNLOAD_BBOX_WEST",
    "NHMS_DOWNLOAD_BBOX_EAST",
)


# --------------------------------------------------------------------------- region

def test_default_bbox_matches_china_buffered(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in BBOX_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    bbox = china_buffered_bbox_from_env()
    assert (bbox.south, bbox.north, bbox.west, bbox.east) == (8.0, 64.0, 63.0, 145.0)


def test_env_overrides_bbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NHMS_DOWNLOAD_BBOX_SOUTH", "10")
    monkeypatch.setenv("NHMS_DOWNLOAD_BBOX_NORTH", "55")
    monkeypatch.setenv("NHMS_DOWNLOAD_BBOX_WEST", "70")
    monkeypatch.setenv("NHMS_DOWNLOAD_BBOX_EAST", "140")
    bbox = china_buffered_bbox_from_env()
    assert bbox.as_dict() == {"south": 10.0, "north": 55.0, "west": 70.0, "east": 140.0}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"south": 64, "north": 8, "west": 63, "east": 145},  # south >= north
        {"south": 8, "north": 64, "west": 145, "east": 63},  # west >= east
        {"south": -100, "north": 64, "west": 63, "east": 145},  # lat out of range
        {"south": 8, "north": 64, "west": -200, "east": 145},  # lon out of range
    ],
)
def test_invalid_bbox_raises(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        GeoBBox(**kwargs)


def test_bbox_identity_changes_with_region() -> None:
    a = GeoBBox(south=8, north=64, west=63, east=145)
    b = GeoBBox(south=10, north=55, west=70, east=140)
    assert a.identity() != b.identity()


# --------------------------------------------------------------------------- GFS

def _gfs_adapter(tmp_path: Path, bbox: GeoBBox | None = None) -> GFSAdapter:
    config = GFSAdapterConfig(
        workspace_root=tmp_path,
        bbox=bbox or GeoBBox(south=8, north=64, west=63, east=145),
    )
    return GFSAdapter(config=config, object_store=LocalObjectStore(tmp_path))


def test_gfs_remote_url_includes_subregion(tmp_path: Path) -> None:
    adapter = _gfs_adapter(tmp_path)
    url = adapter.remote_url("2026050100", forecast_hour=0, variable="tmp2m")
    query = parse_qs(urlparse(url).query, keep_blank_values=True)
    assert query["subregion"] == ["on"]
    assert query["leftlon"] == ["63"]
    assert query["rightlon"] == ["145"]
    assert query["toplat"] == ["64"]
    assert query["bottomlat"] == ["8"]


def test_gfs_identity_includes_bbox(tmp_path: Path) -> None:
    adapter = _gfs_adapter(tmp_path)
    policy = adapter.source_policy_identity()
    obj = adapter.source_object_identity("2026050100")
    assert policy["bbox"] == {"south": 8.0, "north": 64.0, "west": 63.0, "east": 145.0}
    assert obj["bbox"] == policy["bbox"]


def test_gfs_identity_digest_changes_with_bbox(tmp_path: Path) -> None:
    a = _gfs_adapter(tmp_path, GeoBBox(south=8, north=64, west=63, east=145))
    b = _gfs_adapter(tmp_path, GeoBBox(south=10, north=55, west=70, east=140))
    digest_a = gfs_module._stable_digest(a.source_object_identity("2026050100"))
    digest_b = gfs_module._stable_digest(b.source_object_identity("2026050100"))
    assert digest_a != digest_b


# --------------------------------------------------------------------------- IFS

class _FakeClient:
    def retrieve(self, **kwargs: Any) -> None:
        Path(kwargs["target"]).write_bytes(b"GRIB global payload " + b"x" * 64)


def _ifs_adapter(tmp_path: Path, bbox: GeoBBox | None = None) -> IFSAdapter:
    config = IFSAdapterConfig(
        workspace_root=tmp_path,
        bbox=bbox or GeoBBox(south=8, north=64, west=63, east=145),
    )
    adapter = IFSAdapter(config=config, object_store=LocalObjectStore(tmp_path))
    adapter.downloader = adapter._download_url
    adapter._client_for_source = lambda _source: _FakeClient()  # type: ignore[method-assign]
    return adapter


def test_ifs_clip_invokes_cdo_with_sellonlatbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        captured["argv"] = argv
        # cdo writes the clipped output to argv[-1]
        Path(argv[-1]).write_bytes(b"GRIB clipped payload")
        return type("R", (), {"returncode": 0, "stderr": b""})()

    monkeypatch.setattr(ifs_module.shutil, "which", lambda _name: "/usr/bin/cdo")
    monkeypatch.setattr(ifs_module.subprocess, "run", fake_run)

    adapter = _ifs_adapter(tmp_path)
    url = adapter.remote_url("2026050100", forecast_hour=0, variable="2t")
    payload = adapter._download_url(url)

    assert payload.content == b"GRIB clipped payload"
    argv = captured["argv"]
    assert argv[0] == "/usr/bin/cdo"
    assert "sellonlatbox,63,145,8,64" in argv
    assert argv[1:3] == ["-f", "grb2"]


def test_ifs_clip_fails_loud_when_cdo_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ifs_module.shutil, "which", lambda _name: None)
    adapter = _ifs_adapter(tmp_path)
    url = adapter.remote_url("2026050100", forecast_hour=0, variable="2t")
    with pytest.raises(CdoMissingError) as exc:
        adapter._download_url(url)
    assert exc.value.error_code == "CDO_MISSING"


def test_ifs_clip_fails_loud_on_cdo_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        return type("R", (), {"returncode": 1, "stderr": b"cdo sellonlatbox: boom"})()

    monkeypatch.setattr(ifs_module.shutil, "which", lambda _name: "/usr/bin/cdo")
    monkeypatch.setattr(ifs_module.subprocess, "run", fake_run)

    adapter = _ifs_adapter(tmp_path)
    url = adapter.remote_url("2026050100", forecast_hour=0, variable="2t")
    # The fake client wrote a global payload to the temp file; on cdo failure the
    # adapter must raise rather than fall back to returning that global payload.
    with pytest.raises(CdoClipError) as exc:
        adapter._download_url(url)
    assert exc.value.error_code == "CDO_CLIP_FAILED"


def test_ifs_clip_timeout_fails_loud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        raise ifs_module.subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(ifs_module.shutil, "which", lambda _name: "/usr/bin/cdo")
    monkeypatch.setattr(ifs_module.subprocess, "run", fake_run)

    adapter = _ifs_adapter(tmp_path)
    url = adapter.remote_url("2026050100", forecast_hour=0, variable="2t")
    with pytest.raises(CdoClipError) as exc:
        adapter._download_url(url)
    assert exc.value.error_code == "CDO_CLIP_FAILED"


def test_ifs_url_exists_does_not_require_cdo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # No cdo available; availability probing must still succeed because it does
    # not clip. A spurious cdo dependency would otherwise be flagged here.
    monkeypatch.setattr(ifs_module.shutil, "which", lambda _name: None)

    def fail_clip(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("availability probe must not invoke cdo")

    monkeypatch.setattr(ifs_module.subprocess, "run", fail_clip)

    adapter = _ifs_adapter(tmp_path)
    url = adapter.remote_url("2026050100", forecast_hour=0, variable="2t")
    assert adapter._url_exists(url) is True


def test_ifs_url_exists_false_when_file_unavailable(tmp_path: Path) -> None:
    class _MissingClient:
        def retrieve(self, **kwargs: Any) -> None:
            from urllib.error import HTTPError

            raise HTTPError(url="x", code=404, msg="not found", hdrs=None, fp=None)

    adapter = _ifs_adapter(tmp_path)
    adapter._client_for_source = lambda _source: _MissingClient()  # type: ignore[method-assign]
    url = adapter.remote_url("2026050100", forecast_hour=0, variable="2t")
    assert adapter._url_exists(url) is False


def test_ifs_real_download_fails_loud_when_cdo_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The real download path (clip=True, via _download_with_retries) must still
    # surface a missing cdo as CDO_MISSING after the discovery decoupling.
    monkeypatch.setattr(ifs_module.shutil, "which", lambda _name: None)
    adapter = _ifs_adapter(tmp_path)
    url = adapter.remote_url("2026050100", forecast_hour=0, variable="2t")
    with pytest.raises(CdoMissingError) as exc:
        adapter._download_with_retries(url)
    assert exc.value.error_code == "CDO_MISSING"


def test_ifs_clip_uses_env_overridden_bbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        captured["argv"] = argv
        Path(argv[-1]).write_bytes(b"GRIB clipped payload")
        return type("R", (), {"returncode": 0, "stderr": b""})()

    monkeypatch.setattr(ifs_module.shutil, "which", lambda _name: "/usr/bin/cdo")
    monkeypatch.setattr(ifs_module.subprocess, "run", fake_run)

    adapter = _ifs_adapter(tmp_path, GeoBBox(south=10, north=55, west=70, east=140))
    adapter._download_url(adapter.remote_url("2026050100", forecast_hour=0, variable="2t"))
    assert "sellonlatbox,70,140,10,55" in captured["argv"]


def test_ifs_identity_includes_bbox(tmp_path: Path) -> None:
    adapter = _ifs_adapter(tmp_path)
    policy = adapter.source_policy_identity("2026050100")
    obj = adapter.source_object_identity("2026050100")
    assert policy["bbox"] == {"south": 8.0, "north": 64.0, "west": 63.0, "east": 145.0}
    assert obj["bbox"] == policy["bbox"]


def test_ifs_identity_digest_changes_with_bbox(tmp_path: Path) -> None:
    a = _ifs_adapter(tmp_path, GeoBBox(south=8, north=64, west=63, east=145))
    b = _ifs_adapter(tmp_path, GeoBBox(south=10, north=55, west=70, east=140))
    digest_a = ifs_module._stable_digest(a.source_object_identity("2026050100"))
    digest_b = ifs_module._stable_digest(b.source_object_identity("2026050100"))
    assert digest_a != digest_b
