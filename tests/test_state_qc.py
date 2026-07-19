from __future__ import annotations

from pathlib import Path

import pytest

from packages.common.state_qc import (
    MAX_STATE_IC_BYTES,
    cfg_ic_header_minute_index,
    cfg_ic_header_minute_time,
    normalize_state_negative_residuals,
    run_state_variable_qc,
)


def _write_ic(
    path: Path,
    *,
    mesh: int,
    river: int,
    lake: int = 0,
    mesh_rows: list[list[float]] | None = None,
    river_rows: list[list[float]] | None = None,
    lake_rows: list[list[float]] | None = None,
    minute_time: float = 27000000.0,
) -> Path:
    header_counts = [str(mesh), str(river)]
    if lake:
        header_counts.append(str(lake))
    header = "\t".join([*header_counts, f"{minute_time:.6f}"])
    lines = [header]

    def _rows(count: int, supplied: list[list[float]] | None, cols: int) -> list[list[float]]:
        if supplied is not None:
            return supplied
        return [[float(i + 1), *([0.1] * cols)] for i in range(count)]

    for row in _rows(mesh, mesh_rows, 5):
        lines.append("\t".join(f"{value:.6f}" for value in row))
    for row in _rows(river, river_rows, 1):
        lines.append("\t".join(f"{value:.6f}" for value in row))
    if lake:
        for row in _rows(lake, lake_rows, 1):
            lines.append("\t".join(f"{value:.6f}" for value in row))

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_happy_path_valid_ic_passes(tmp_path: Path) -> None:
    ic = _write_ic(tmp_path / "good.cfg.ic", mesh=3, river=2)
    result = run_state_variable_qc(ic, expected_mesh_count=3, expected_river_count=2)
    assert result.passed is True
    assert result.reason is None
    assert result.checks["row_counts"]["mesh"] == 3
    assert result.checks["row_counts"]["river"] == 2


def test_happy_path_with_lake_passes(tmp_path: Path) -> None:
    ic = _write_ic(tmp_path / "lake.cfg.ic", mesh=2, river=1, lake=1)
    result = run_state_variable_qc(
        ic, expected_mesh_count=2, expected_river_count=1, expected_lake_count=1
    )
    assert result.passed is True
    assert result.checks["row_counts"]["lake"] == 1


def test_row_count_mismatch_fails(tmp_path: Path) -> None:
    ic = _write_ic(tmp_path / "wrongcount.cfg.ic", mesh=3, river=2)
    result = run_state_variable_qc(ic, expected_mesh_count=5, expected_river_count=2)
    assert result.passed is False
    assert "mesh row count" in (result.reason or "")


def test_negative_state_value_fails(tmp_path: Path) -> None:
    ic = _write_ic(
        tmp_path / "neg.cfg.ic",
        mesh=2,
        river=1,
        mesh_rows=[[1.0, 0.1, 0.1, 0.1, 0.1, 0.1], [2.0, 0.1, -0.5, 0.1, 0.1, 0.1]],
    )
    result = run_state_variable_qc(ic, expected_mesh_count=2, expected_river_count=1)
    assert result.passed is False
    assert "negative" in (result.reason or "")


def test_native_shud_update_header_and_negative_zero_pass(tmp_path: Path) -> None:
    path = tmp_path / "native.cfg.ic.update"
    path.write_text(
        "2\t1\t27000000.000000\n"
        "Index\tCanopy\tSnow\tSurface\tUnsat\tGW\n"
        "1\t0.000000\t0.000000\t-0.000001\t0.000000\t0.000000\n"
        "2\t0.000000\t0.000000\t0.000000\t0.000000\t-0.000001\n"
        "Index\tRiver_Stage\n"
        "1\t0.000000\n",
        encoding="utf-8",
    )

    result = run_state_variable_qc(path, expected_mesh_count=2, expected_river_count=1)

    assert result.passed is True
    assert result.reason is None


def test_truncated_sectioned_native_update_fails_even_at_row_boundary(tmp_path: Path) -> None:
    path = tmp_path / "native-partial.cfg.ic.update"
    path.write_text(
        "3\t1\t27000000.000000\n"
        "Index\tCanopy\tSnow\tSurface\tUnsat\tGW\n"
        "1\t0.0\t0.0\t0.0\t0.0\t0.0\n"
        "2\t0.0\t0.0\t0.0\t0.0\t0.0\n",
        encoding="utf-8",
    )

    result = run_state_variable_qc(path)

    assert result.passed is False
    assert "truncated sectioned IC body" in (result.reason or "")


def test_negative_beyond_roundoff_tolerance_fails(tmp_path: Path) -> None:
    ic = _write_ic(
        tmp_path / "neg_tolerance.cfg.ic",
        mesh=1,
        river=1,
        mesh_rows=[[1.0, 0.1, -0.02, 0.1, 0.1, 0.1]],
    )

    result = run_state_variable_qc(ic, expected_mesh_count=1, expected_river_count=1)

    assert result.passed is False
    assert "negative" in (result.reason or "")


def test_bounded_unsat_negative_is_projected_to_physical_zero(tmp_path: Path) -> None:
    rows = [[float(index + 1), 0.1, 0.1, 0.1, 0.1, 0.1] for index in range(100)]
    rows[73][4] = -0.014834
    ic = _write_ic(tmp_path / "bounded-unsat.cfg.ic", mesh=100, river=1, mesh_rows=rows)

    raw_result = run_state_variable_qc(ic, expected_mesh_count=100, expected_river_count=1)
    normalization = normalize_state_negative_residuals(ic.read_text(encoding="utf-8"))
    normalized = tmp_path / "normalized.cfg.ic"
    normalized.write_text(normalization.content, encoding="utf-8")

    assert raw_result.passed is False
    assert normalization.accepted is True
    assert normalization.normalized_unsat_row_count == 1
    assert normalization.max_unsat_correction_m == pytest.approx(0.014834)
    assert run_state_variable_qc(normalized, expected_mesh_count=100, expected_river_count=1).passed is True


def test_unsat_negative_beyond_repair_ceiling_remains_qc_failure(tmp_path: Path) -> None:
    rows = [[float(index + 1), 0.1, 0.1, 0.1, 0.1, 0.1] for index in range(100)]
    rows[73][4] = -0.020001
    ic = _write_ic(tmp_path / "excess-unsat.cfg.ic", mesh=100, river=1, mesh_rows=rows)

    normalization = normalize_state_negative_residuals(ic.read_text(encoding="utf-8"))
    normalized = tmp_path / "excess-normalized.cfg.ic"
    normalized.write_text(normalization.content, encoding="utf-8")

    assert normalization.accepted is True
    assert normalization.normalized_unsat_row_count == 0
    result = run_state_variable_qc(normalized, expected_mesh_count=100, expected_river_count=1)
    assert result.passed is False
    assert "negative" in (result.reason or "")


def test_widespread_unsat_negative_projection_is_rejected(tmp_path: Path) -> None:
    rows = [[float(index + 1), 0.1, 0.1, 0.1, 0.1, 0.1] for index in range(100)]
    for index in (3, 20, 73):
        rows[index][4] = -0.001
    ic = _write_ic(tmp_path / "widespread-unsat.cfg.ic", mesh=100, river=1, mesh_rows=rows)

    normalization = normalize_state_negative_residuals(ic.read_text(encoding="utf-8"))

    assert normalization.accepted is False
    assert normalization.normalized_unsat_row_count == 3
    assert "above" in (normalization.reason or "")


def test_out_of_range_value_fails(tmp_path: Path) -> None:
    ic = _write_ic(
        tmp_path / "huge.cfg.ic",
        mesh=1,
        river=1,
        mesh_rows=[[1.0, 0.1, 0.1, 1.0e9, 0.1, 0.1]],
    )
    result = run_state_variable_qc(ic, expected_mesh_count=1, expected_river_count=1)
    assert result.passed is False
    assert "exceeds bound" in (result.reason or "")


def test_non_finite_value_fails(tmp_path: Path) -> None:
    path = tmp_path / "nan.cfg.ic"
    path.write_text(
        "2\t1\t27000000.000000\n"
        "1\t0.1\t0.1\tnan\t0.1\t0.1\n"
        "2\t0.1\t0.1\t0.1\t0.1\t0.1\n"
        "1\t0.5\n",
        encoding="utf-8",
    )
    result = run_state_variable_qc(path, expected_mesh_count=2, expected_river_count=1)
    assert result.passed is False
    assert "not finite" in (result.reason or "")


def test_empty_file_is_parse_failure(tmp_path: Path) -> None:
    path = tmp_path / "empty.cfg.ic"
    path.write_text("", encoding="utf-8")
    result = run_state_variable_qc(path)
    assert result.passed is False
    assert "parse failed" in (result.reason or "").lower()


def test_truncated_body_is_parse_failure(tmp_path: Path) -> None:
    path = tmp_path / "trunc.cfg.ic"
    path.write_text("5\t3\t27000000.000000\n1\t0.1\t0.1\t0.1\t0.1\t0.1\n", encoding="utf-8")
    result = run_state_variable_qc(path)
    assert result.passed is False
    assert "parse failed" in (result.reason or "").lower()


def test_non_numeric_row_is_parse_failure(tmp_path: Path) -> None:
    path = tmp_path / "garbage.cfg.ic"
    path.write_text(
        "1\t1\t27000000.000000\nhello world here we go\n1\t0.5\n",
        encoding="utf-8",
    )
    result = run_state_variable_qc(path)
    assert result.passed is False
    assert "parse failed" in (result.reason or "").lower()


def test_missing_file_is_parse_failure(tmp_path: Path) -> None:
    result = run_state_variable_qc(tmp_path / "does_not_exist.cfg.ic")
    assert result.passed is False
    assert result.reason is not None


def test_water_balance_within_threshold_passes(tmp_path: Path) -> None:
    ic = _write_ic(tmp_path / "wb_ok.cfg.ic", mesh=2, river=1)
    result = run_state_variable_qc(
        ic,
        expected_mesh_count=2,
        expected_river_count=1,
        water_balance={
            "threshold": 0.05,
            "deltas": {"soil_moisture": 0.01, "groundwater": 0.0, "channel_storage": 0.02},
        },
    )
    assert result.passed is True
    assert result.checks["water_balance"]["passed"] is True


def test_water_balance_over_threshold_fails(tmp_path: Path) -> None:
    ic = _write_ic(tmp_path / "wb_bad.cfg.ic", mesh=2, river=1)
    result = run_state_variable_qc(
        ic,
        expected_mesh_count=2,
        expected_river_count=1,
        water_balance={"threshold": 0.05, "deltas": {"groundwater": 0.5}},
    )
    assert result.passed is False
    assert "water-balance" in (result.reason or "")


def test_water_balance_absent_is_skipped(tmp_path: Path) -> None:
    ic = _write_ic(tmp_path / "wb_skip.cfg.ic", mesh=1, river=1)
    result = run_state_variable_qc(ic, expected_mesh_count=1, expected_river_count=1)
    assert result.passed is True
    assert result.checks["water_balance"] == "skipped"


# ---------------------------------------------------------------------------
# Bounded read (OOM protection): oversized / binary input -> QC fail, never crash
# ---------------------------------------------------------------------------


def test_oversized_ic_fails_without_crash(tmp_path: Path) -> None:
    # A file larger than MAX_STATE_IC_BYTES must fail QC (parse failure), not be read
    # unboundedly into memory. Write a valid header then pad past the limit.
    path = tmp_path / "huge.cfg.ic"
    header = "2\t1\t27000000.000000\n"
    padding = "1\t0.1\n" * 8  # a few valid data rows
    body = header + padding
    # Pad with a long comment-free numeric line repeated to exceed the byte limit.
    filler = ("2 0.1\n") * ((MAX_STATE_IC_BYTES // 6) + 16)
    path.write_text(body + filler, encoding="utf-8")
    assert path.stat().st_size > MAX_STATE_IC_BYTES

    result = run_state_variable_qc(path)
    assert result.passed is False
    assert result.reason is not None
    assert "limit" in (result.reason or "").lower() or "exceeds" in (result.reason or "").lower()


def test_binary_non_utf8_ic_fails_without_crash(tmp_path: Path) -> None:
    # Non-UTF-8 / binary garbage must be a QC failure, not a UnicodeDecodeError crash.
    path = tmp_path / "binary.cfg.ic"
    path.write_bytes(b"\xff\xfe\x00\x01\x02 not valid utf-8 \x80\x81")

    result = run_state_variable_qc(path)
    assert result.passed is False
    assert result.reason is not None


# ---------------------------------------------------------------------------
# Missing state columns + header/body lake inconsistency -> QC fail
# ---------------------------------------------------------------------------


def test_missing_state_columns_row_fails(tmp_path: Path) -> None:
    # A river row with only the element id and no state column is structurally short:
    # missing state columns -> QC fail (not silently range-checked on absent columns).
    path = tmp_path / "shortrow.cfg.ic"
    ic = _write_ic(
        path,
        mesh=1,
        river=1,
        mesh_rows=[[1.0, 0.1, 0.1, 0.1, 0.1, 0.1]],
        river_rows=[[1.0]],  # id only, missing river_stage state column
    )
    result = run_state_variable_qc(ic, expected_mesh_count=1, expected_river_count=1)
    assert result.passed is False
    assert "missing state columns" in (result.reason or "")


def test_header_declares_lake_but_body_missing_lake_fails(tmp_path: Path) -> None:
    # Header reports lake_count=1 but the body has no lake row. Silently truncating to
    # an empty lake block masks a corrupt/truncated restart file -> must be QC fail.
    path = tmp_path / "lake_missing.cfg.ic"
    # mesh=1, river=1 rows present, but no lake row despite header lake=1.
    header = "1\t1\t1\t27000000.000000\n"
    body = "1\t0.1\t0.1\t0.1\t0.1\t0.1\n1\t0.1\n"  # mesh row + river row only
    path.write_text(header + body, encoding="utf-8")

    result = run_state_variable_qc(path, expected_mesh_count=1, expected_river_count=1, expected_lake_count=1)
    assert result.passed is False
    assert "lake" in (result.reason or "").lower()


def test_cfg_ic_header_minute_index_3_token_no_lake() -> None:
    # <mesh> <river> <minute-time>: minute-time is the trailing (index 2) token.
    header = ["100", "50", "27000000.000000"]
    assert cfg_ic_header_minute_index(header) == 2
    assert cfg_ic_header_minute_time(header) == 27000000.0


def test_cfg_ic_header_minute_index_4_token_with_lake() -> None:
    # <mesh> <river> <lake> <minute-time>: minute-time is the LAST token (index 3),
    # NOT the lake count at index 2.
    header = ["100", "50", "3", "27000000.000000"]
    assert cfg_ic_header_minute_index(header) == 3
    assert cfg_ic_header_minute_time(header) == 27000000.0


def test_cfg_ic_header_minute_no_numeric_returns_none() -> None:
    assert cfg_ic_header_minute_index(["mesh", "river"]) is None
    assert cfg_ic_header_minute_time(["mesh", "river"]) is None
    # Single numeric token is insufficient (need a count + minute-time pair).
    assert cfg_ic_header_minute_index(["27000000.0"]) is None
    assert cfg_ic_header_minute_time([]) is None
