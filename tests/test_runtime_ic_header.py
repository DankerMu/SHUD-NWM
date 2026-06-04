from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from workers.shud_runtime.runtime import _read_cfg_ic_header_minute, _shift_cfg_ic_time


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


def test_shift_header_without_minute_time_pair_is_noop(tmp_path: Path) -> None:
    # Header lacking a count + minute-time pair -> safe no-op, file untouched.
    path = tmp_path / "bad.cfg.ic"
    original = "mesh\t27000000.000000\n1\t0.1\n"
    path.write_text(original, encoding="utf-8")
    _shift_cfg_ic_time(path, datetime(2024, 1, 2, 3, 4, tzinfo=UTC))
    assert path.read_text(encoding="utf-8") == original
