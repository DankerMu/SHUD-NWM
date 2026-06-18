"""洪频产品质量 SQL 可用性分支单测（纯函数，无 DB）。

显式字段存在时以 flood.run_product_quality.quality_state 为真相源，不聚合
return_period_result。旧表或缺表只保留 deterministic fallback，且缺表分支只做
轻量 EXISTS。
"""

from __future__ import annotations

from packages.common.forecast_store import (
    _flood_product_quality_join,
    _flood_product_quality_select,
    _flood_product_ready_sql,
)


def test_join_absent_branch_never_references_missing_table() -> None:
    sql = _flood_product_quality_join("fpq", available="missing_table")
    assert "run_product_quality" not in sql
    assert "LEFT JOIN LATERAL" in sql
    assert "flood.return_period_result" in sql
    assert "AS has_product" in sql
    assert "AS has_peak" in sql
    assert ") fpq ON TRUE" in sql


def test_join_explicit_branch_preserves_materialized_path() -> None:
    sql = _flood_product_quality_join("fpq", available="explicit")
    assert "LEFT JOIN flood.run_product_quality fpq" in sql
    assert "LATERAL" not in sql
    assert "flood.return_period_result" not in sql
    assert "GROUP BY" not in sql
    assert _flood_product_quality_join("fpq") == sql


def test_ready_absent_branch_shows_when_db_or_published_has_product() -> None:
    sql = _flood_product_ready_sql("fpq", available="missing_table")
    assert sql == "(fpq.has_product OR h.status = 'published')"


def test_ready_explicit_branch_uses_quality_state() -> None:
    sql = _flood_product_ready_sql("fpq", available="explicit")
    assert sql == "fpq.quality_state = 'ready'"
    assert "has_product" not in sql
    assert _flood_product_ready_sql("fpq") == sql


def test_ready_legacy_table_branch_keeps_count_fallback() -> None:
    sql = _flood_product_ready_sql("fpq", available="legacy_table")
    assert "quality_state" not in sql
    assert "has_product" not in sql
    assert "max_return_period_rows" in sql


def test_select_absent_branch_maps_existence_to_row_counts() -> None:
    sql = _flood_product_quality_select("fpq", available="missing_table")
    assert "fpq.has_peak AS flood_quality_max_over_window" in sql
    # has_product -> 1/0，喂给 _flood_product_quality_from_row 判 ready/unavailable
    assert "CASE WHEN fpq.has_product THEN 1 ELSE 0 END) AS flood_result_rows" in sql
    assert "CASE WHEN fpq.has_product THEN 1 ELSE 0 END) AS flood_return_period_rows" in sql
    assert "CASE WHEN fpq.has_product THEN 1 ELSE 0 END) AS flood_warning_rows" in sql
    assert "max_result_rows" not in sql


def test_select_explicit_branch_carries_quality_fields_without_result_table() -> None:
    sql = _flood_product_quality_select("fpq", available="explicit")
    assert "fpq.quality_state AS flood_quality_state" in sql
    assert "fpq.unavailable_products AS flood_unavailable_products" in sql
    assert "fpq.expected_result_rows" in sql
    assert "fpq.meaningful_result_rows" in sql
    assert "fpq.no_frequency_curve_rows" in sql
    assert "flood.return_period_result" not in sql
    assert "has_product" not in sql
    assert _flood_product_quality_select("fpq") == sql


def test_select_legacy_table_branch_keeps_count_compatibility_formulas() -> None:
    sql = _flood_product_quality_select("fpq", available="legacy_table")
    assert "quality_state" not in sql
    assert "max_result_rows" in sql
    assert "has_product" not in sql
