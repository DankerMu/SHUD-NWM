"""Exclusive two-artifact commit binder tests. No live DB/API."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from packages.common.node27_issue1895_commit import (
    bind_performance_artifacts,
    publish_performance_artifacts,
    publish_performance_receipt,
)
from packages.common.node27_issue1895_lanes import freeze_lanes
from packages.common.node27_issue1895_performance import build_performance_receipt, validate_performance_receipt
from packages.common.node27_issue1895_types import Issue1895ReadinessError
from tests.test_issue1895_readiness_performance import _four_lanes, _public_lane
from tests.test_issue1895_readiness_performance_live import BASIN, ORIGIN, SEGMENT, SHA


def _pass_document() -> dict:
    lanes = _four_lanes()
    frozen = freeze_lanes(lanes)
    identity = {
        "head_sha": SHA,
        "reviewed_sha": SHA,
        "api_origin": ORIGIN,
        "timeout_seconds": 5,
        "body_limit_bytes": 65536,
        "artifact": "nhms-issue1895-performance-oracle",
        "basin_id": BASIN,
        "segment_id": SEGMENT,
        "readonly": {"transaction_read_only": True, "current_user": "nhms_display_ro"},
    }
    public_lanes = {name: _public_lane(lane) for name, lane in lanes.items()}
    return validate_performance_receipt(
        build_performance_receipt(lanes=public_lanes, identity=identity, frozen=frozen, status="PASS")
    )


def test_publication_is_exclusive_and_preserves_a_preexisting_winner(tmp_path: Path) -> None:
    parent = tmp_path / "private"
    parent.mkdir()
    os.chmod(parent, 0o700)
    winner = parent / "performance.json"
    winner.write_bytes(b'{"status":"old"}')
    os.chmod(winner, 0o600)
    before = winner.read_bytes()
    with pytest.raises(Issue1895ReadinessError) as exists:
        publish_performance_receipt(winner, {"status": "PASS"})
    assert exists.value.code == "RECEIPT_EXISTS"
    assert winner.read_bytes() == before
    fresh = parent / "fresh.json"
    payload = {"artifact": "nhms-issue1895-performance-oracle", "status": "PASS"}
    publish_performance_receipt(fresh, payload)
    info = os.lstat(fresh)
    assert stat.S_ISREG(info.st_mode)
    assert info.st_nlink == 1
    assert oct(info.st_mode & 0o777) == "0o600"
    assert json.loads(fresh.read_bytes()) == payload


def test_two_artifact_commit_and_post_link_failure_withdraws_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "private"
    parent.mkdir()
    os.chmod(parent, 0o700)
    receipt = parent / "performance.json"
    payload = _pass_document()
    publish_performance_artifacts(receipt, payload)
    marker = parent / "performance.json.commit"
    assert receipt.is_file()
    assert marker.is_file()
    assert oct(marker.stat().st_mode & 0o777) == "0o600"
    bind_performance_artifacts(
        receipt, marker, expected_sha=SHA, expected_basin=BASIN, expected_segment=SEGMENT
    )
    swapped = json.loads(receipt.read_text(encoding="utf-8"))
    swapped["identity"]["segment_id"] = "qhh_reach_000099"
    receipt.write_bytes(json.dumps(swapped, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    with pytest.raises(Issue1895ReadinessError):
        bind_performance_artifacts(
            receipt, marker, expected_sha=SHA, expected_basin=BASIN, expected_segment=SEGMENT
        )
    second = parent / "second.json"
    real_open = os.open

    def fail_marker(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if dir_fd is not None:
            return real_open(path, flags, mode, dir_fd=dir_fd)
        if "second.json.commit" in str(path) and ".tmp" in str(path):
            raise OSError("marker create failed")
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", fail_marker)
    with pytest.raises(Issue1895ReadinessError):
        publish_performance_artifacts(second, payload)
    assert not (parent / "second.json.commit").exists()
    if second.exists():
        with pytest.raises(Issue1895ReadinessError):
            bind_performance_artifacts(
                second,
                parent / "second.json.commit",
                expected_sha=SHA,
                expected_basin=BASIN,
                expected_segment=SEGMENT,
            )


def test_performance_binder_closes_deep_json_without_traceback(tmp_path: Path) -> None:
    parent = tmp_path / "private"
    parent.mkdir()
    os.chmod(parent, 0o700)
    receipt = parent / "performance.json"
    marker = parent / "performance.json.commit"
    receipt.write_text("{" * 1100 + "}" * 1100, encoding="utf-8")
    marker.write_text("{}", encoding="utf-8")
    os.chmod(receipt, 0o600)
    os.chmod(marker, 0o600)
    with pytest.raises(Issue1895ReadinessError) as invalid:
        bind_performance_artifacts(
            receipt,
            marker,
            expected_sha=SHA,
            expected_basin=BASIN,
            expected_segment=SEGMENT,
        )
    assert invalid.value.code == "COMMIT_JSON"


def test_binder_rejects_inode_swap_parent_drift_and_post_link_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "private"
    parent.mkdir()
    os.chmod(parent, 0o700)
    receipt = parent / "performance.json"
    payload = _pass_document()
    publish_performance_artifacts(receipt, payload)
    marker = parent / "performance.json.commit"
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    start = (now - timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    end = (now + timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    bind_performance_artifacts(
        receipt,
        marker,
        expected_sha=SHA,
        expected_basin=BASIN,
        expected_segment=SEGMENT,
        cmd_start=start,
        cmd_end=end,
    )
    real_open = os.open
    original = receipt.read_bytes()
    decoy = parent / "decoy.json"
    decoy.write_bytes(original)
    os.chmod(decoy, 0o600)
    mutated = json.loads(original)
    mutated["identity"]["segment_id"] = "qhh_reach_000099"
    mutated_bytes = json.dumps(mutated, sort_keys=True, separators=(",", ":")).encode("utf-8")
    mutated_bytes = mutated_bytes + b" " * (len(original) - len(mutated_bytes))
    decoy.write_bytes(mutated_bytes)
    opened: list[str] = []

    def swap_after_lstat(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if dir_fd is not None:
            return real_open(path, flags, mode, dir_fd=dir_fd)
        text = str(path)
        reading = (flags & (os.O_WRONLY | os.O_RDWR)) == 0
        if text.endswith("performance.json") and reading and "performance.json" not in opened:
            opened.append(text)
            os.replace(decoy, receipt)
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", swap_after_lstat)
    with pytest.raises(Issue1895ReadinessError) as swapped:
        bind_performance_artifacts(
            receipt, marker, expected_sha=SHA, expected_basin=BASIN, expected_segment=SEGMENT
        )
    assert swapped.value.code in {"RECEIPT_INODE_SWAP", "COMMIT_PIN", "RECEIPT_IDENTITY_DRIFT"}
    os.chmod(parent, 0o755)
    with pytest.raises(Issue1895ReadinessError) as parent_mode:
        bind_performance_artifacts(
            receipt, marker, expected_sha=SHA, expected_basin=BASIN, expected_segment=SEGMENT
        )
    assert parent_mode.value.code.endswith("PARENT_MODE") or parent_mode.value.code == "COMMIT_PARENT"
    os.chmod(parent, 0o700)
    third = parent / "third.json"
    real_fsync = os.fsync
    calls = {"n": 0}

    def fail_second_fsync(fd: int) -> None:
        calls["n"] += 1
        if calls["n"] >= 4:
            raise OSError("parent fsync failed")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_second_fsync)
    with pytest.raises(Issue1895ReadinessError):
        publish_performance_artifacts(third, payload)
    assert not (parent / "third.json.commit").exists()
    if third.exists():
        with pytest.raises(Issue1895ReadinessError):
            bind_performance_artifacts(
                third,
                parent / "third.json.commit",
                expected_sha=SHA,
                expected_basin=BASIN,
                expected_segment=SEGMENT,
            )


class TrailingResponse:
    def __init__(self, body: bytes, *, fail_read: bool = False) -> None:
        from tests.test_issue1895_readiness_performance_live import FakeResponse

        self._inner = FakeResponse(body, fail_read=fail_read)
        self.reads = self._inner.reads
        self.status = 200
        self.code = 200
        self.headers: dict[str, str] = {}
        self.fail_read = fail_read
        self.body = body
        self.offset = 0

    def read(self, size: int = -1) -> bytes:
        self.reads.append(size)
        if len(self.reads) == 1:
            return self.body
        if self.fail_read:
            raise OSError("stream exploded")
        return b"TRAILING"

    def close(self) -> None:
        return None


def test_identity_only_http_requires_eof_2xx_and_identity_encoding() -> None:
    from urllib.error import HTTPError

    from packages.common.node27_issue1895_http import ACCEPT_ENCODING
    from packages.common.node27_issue1895_identity import IDENTITY_BODY_LIMIT, identity_only_url
    from packages.common.node27_issue1895_performance_live import fetch_identity_only_product
    from tests.test_issue1895_readiness_performance_live import (
        FakeOpener,
        FakeResponse,
        _identity_body,
    )

    valid = FakeOpener(FakeResponse(_identity_body("GFS")))
    product = fetch_identity_only_product(origin=ORIGIN, source="GFS", basin_id=BASIN, opener=valid)
    assert product["run_id"] == "run-gfs-hot"
    request = valid.requests[0]
    assert request.get_method() == "GET"
    assert request.full_url == identity_only_url(origin=ORIGIN, source="GFS", basin_id=BASIN)
    assert (request.get_header("Accept-encoding") or "").lower() == ACCEPT_ENCODING
    assert request.has_header("Authorization") is False
    assert valid.response.reads == [IDENTITY_BODY_LIMIT + 1, 1]
    trailing = FakeOpener(TrailingResponse(_identity_body("GFS")))  # type: ignore[arg-type]
    with pytest.raises(Issue1895ReadinessError) as extra:
        fetch_identity_only_product(origin=ORIGIN, source="GFS", basin_id=BASIN, opener=trailing)
    assert extra.value.code == "API_BODY_LIMIT"
    broken = FakeOpener(TrailingResponse(_identity_body("GFS"), fail_read=True))  # type: ignore[arg-type]
    with pytest.raises(Issue1895ReadinessError) as stream:
        fetch_identity_only_product(origin=ORIGIN, source="GFS", basin_id=BASIN, opener=broken)
    assert stream.value.code in {"API_BODY_INCOMPLETE", "API_BODY_READ_FAILED"}
    denied = FakeOpener(error=HTTPError(ORIGIN, 500, "no", hdrs=None, fp=None))
    with pytest.raises(Issue1895ReadinessError) as status:
        fetch_identity_only_product(origin=ORIGIN, source="GFS", basin_id=BASIN, opener=denied)
    assert status.value.code == "API_STATUS_INVALID"
    moved = FakeOpener(error=HTTPError(ORIGIN, 302, "moved", hdrs=None, fp=None))
    with pytest.raises(Issue1895ReadinessError) as redirect:
        fetch_identity_only_product(origin=ORIGIN, source="GFS", basin_id=BASIN, opener=moved)
    assert redirect.value.code == "API_REDIRECT"
    encoded = FakeResponse(_identity_body("GFS"))
    encoded.headers = {"Content-Encoding": "gzip"}
    gzipped = FakeOpener(encoded)
    with pytest.raises(Issue1895ReadinessError) as encoding:
        fetch_identity_only_product(origin=ORIGIN, source="GFS", basin_id=BASIN, opener=gzipped)
    assert encoding.value.code == "API_ENCODING_INVALID"
