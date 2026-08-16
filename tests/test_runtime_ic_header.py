from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from workers.shud_runtime.runtime import (
    SHUDRuntimeError,
    _read_cfg_ic_header_minute,
    _shift_cfg_ic_time,
)


def _minute_time(dt: datetime) -> float:
    return dt.timestamp() / 60.0


def test_read_4_token_lake_header_returns_trailing_minute_not_lake_count(tmp_path: Path) -> None:
    # 4-token lake header: <mesh> <river> <lake> <minute-time>. The minute-time must be
    # the LAST token (27000000), NOT the lake count (3) at index 2.
    path = tmp_path / "lake.cfg.ic"
    path.write_text("100\t50\t3\t27000000.000000\n1\t0.1\n", encoding="utf-8")
    assert _read_cfg_ic_header_minute(path) == 27000000.0


def test_read_3_token_header_unchanged(tmp_path: Path) -> None:
    # Regression: 3-token header <mesh> <river> <minute-time> still reads index-2 token.
    path = tmp_path / "nolake.cfg.ic"
    path.write_text("100\t50\t27000000.000000\n1\t0.1\n", encoding="utf-8")
    assert _read_cfg_ic_header_minute(path) == 27000000.0


def test_shift_4_token_lake_header_preserves_counts(tmp_path: Path) -> None:
    # Shifting must overwrite ONLY the trailing minute-time and preserve mesh/river/lake.
    path = tmp_path / "lake.cfg.ic"
    path.write_text("100\t50\t3\t11111111.000000\n1\t0.1\n", encoding="utf-8")
    start = datetime(2024, 1, 2, 3, 4, tzinfo=UTC)
    _shift_cfg_ic_time(path, start)

    header = path.read_text(encoding="utf-8").splitlines()[0].split()
    assert header[0] == "100"
    assert header[1] == "50"
    assert header[2] == "3"  # lake count preserved, not clobbered by run-start
    assert round(float(header[3])) == round(_minute_time(start))
    # And the read-back is the shifted minute-time (last token), not the lake count.
    assert round(_read_cfg_ic_header_minute(path)) == round(_minute_time(start))


def test_shift_3_token_header_unchanged_behavior(tmp_path: Path) -> None:
    # Regression: 3-token header shift overwrites index-2 minute-time, counts intact.
    path = tmp_path / "nolake.cfg.ic"
    path.write_text("100\t50\t11111111.000000\n1\t0.1\n", encoding="utf-8")
    start = datetime(2024, 1, 2, 3, 4, tzinfo=UTC)
    _shift_cfg_ic_time(path, start)

    header = path.read_text(encoding="utf-8").splitlines()[0].split()
    assert header[0] == "100"
    assert header[1] == "50"
    assert round(float(header[2])) == round(_minute_time(start))


def test_shift_header_without_minute_time_pair_fails_closed(tmp_path: Path) -> None:
    # RE-JUDGED (#1197). This used to assert a silent no-op. Silence is exactly how
    # a malformed delivery reached production: nothing downstream can tell "there
    # was nothing to shift" from "this file is not a SHUD IC". The requirement is
    # now fail-closed -- file byte-identical, error visible.
    path = tmp_path / "bad.cfg.ic"
    original = "mesh\t27000000.000000\n1\t0.1\n"
    path.write_text(original, encoding="utf-8")

    with pytest.raises(SHUDRuntimeError) as exc_info:
        _shift_cfg_ic_time(path, datetime(2024, 1, 2, 3, 4, tzinfo=UTC))

    assert exc_info.value.error_code == "IC_TIME_SHIFT_HEADER_INVALID"
    assert path.read_text(encoding="utf-8") == original


def _legacy_shifted_bytes(original: str, start: datetime) -> str:
    """Reproduce the PRE-CHANGE shift, as an independent byte-compat oracle.

    Deliberately a transcription of the old implementation rather than a call into
    the new one: comparing the new output against itself would prove nothing about
    the >=3-token layouts staying byte-identical.
    """
    lines = original.splitlines()
    header = lines[0].split()
    numeric_indices = [
        index
        for index, token in enumerate(header)
        if _is_float_token(token)
    ]
    assert len(numeric_indices) >= 2
    header[numeric_indices[-1]] = f"{start.timestamp() / 60.0:.6f}"
    lines[0] = "\t".join(header)
    return "\n".join(lines) + "\n"


def _is_float_token(token: str) -> bool:
    try:
        float(token)
    except ValueError:
        return False
    return True


@pytest.mark.parametrize(
    "original",
    [
        "100\t50\t11111111.000000\n1\t0.1\n",
        "100\t50\t3\t11111111.000000\n1\t0.1\n",
        "23106\t6\t0.000000\n1\t0.1\n2\t0.2\n",
        # Spacing exactly as the live node-27 baselines write it (task 0(f) probe).
        "484\t 6 \t38920320.000000\n1\t0.1\n",
        # >= 5 numeric tokens: an unknown-but-established layout keeps its existing
        # behaviour at the injector (the gates refuse it upstream instead).
        "1\t2\t3\t4\t11111111.000000\n1\t0.1\n",
    ],
    ids=["native_3", "lake_4", "incident_basin_3", "live_spacing_3", "unknown_5"],
)
def test_headers_with_a_minute_time_slot_shift_byte_for_byte_as_before(
    tmp_path: Path, original: str
) -> None:
    path = tmp_path / "ic.cfg.ic"
    path.write_text(original, encoding="utf-8")
    start = datetime(2024, 1, 2, 3, 4, tzinfo=UTC)

    _shift_cfg_ic_time(path, start)

    assert path.read_bytes() == _legacy_shifted_bytes(original, start).encode("utf-8")


def test_two_token_header_fails_closed_without_touching_the_file(tmp_path: Path) -> None:
    """The #1197 incident header, at the last line of defence.

    Pre-change this rewrote the mesh-state COLUMN COUNT (``6``) with an
    epoch-minute (~29.7M); SHUD sized its state matrix off that and was OOM-killed
    after trying to allocate ~183 GB.
    """
    path = tmp_path / "LH-GL.cfg.ic"
    original = "23106\t6\n1\t0.1\t0.2\t0.3\t0.4\n"
    path.write_bytes(original.encode("utf-8"))
    before = path.read_bytes()

    with pytest.raises(SHUDRuntimeError) as exc_info:
        _shift_cfg_ic_time(path, datetime(2026, 7, 1, tzinfo=UTC))

    assert exc_info.value.error_code == "IC_TIME_SHIFT_HEADER_INVALID"
    assert "2 numeric token(s)" in exc_info.value.message
    assert path.read_bytes() == before
    # The column count is intact -- no epoch-minute anywhere in the header.
    assert path.read_text(encoding="utf-8").splitlines()[0].split() == ["23106", "6"]


def test_missing_or_empty_ic_keeps_the_existing_noop(tmp_path: Path) -> None:
    # Legitimate cold-start / diagnostic-manifest states: no file, or a zero-length
    # one. Both stay no-ops so the fail-closed guard does not break them.
    absent = tmp_path / "absent.cfg.ic"
    _shift_cfg_ic_time(absent, datetime(2024, 1, 2, 3, 4, tzinfo=UTC))
    assert not absent.exists()

    empty = tmp_path / "empty.cfg.ic"
    empty.write_bytes(b"")
    _shift_cfg_ic_time(empty, datetime(2024, 1, 2, 3, 4, tzinfo=UTC))
    assert empty.read_bytes() == b""
