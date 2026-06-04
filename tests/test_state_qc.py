from __future__ import annotations

from pathlib import Path

from packages.common.state_qc import run_state_variable_qc


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
