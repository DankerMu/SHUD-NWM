"""#5 洪频产品质量 SQL 可用性分支单测（纯函数，无 DB）。

node-27 只读副本缺 flood.run_product_quality 物化表（迁移未应用）。available=False
分支必须：(1) 绝不引用缺失表，否则查询 500；(2) 以 return_period_result 行存在性
廉价合成质量信号；(3) "有产物即就绪"——DB 有行 或 run 已 published。available=True
分支必须原样保留物化路径，确保 node-22 真实 DB pytest 回归不变。
"""

from __future__ import annotations

from packages.common.forecast_store import (
    _flood_product_quality_join,
    _flood_product_quality_select,
    _flood_product_ready_sql,
)


def test_join_absent_branch_never_references_missing_table() -> None:
    sql = _flood_product_quality_join("fpq", available=False)
    assert "run_product_quality" not in sql
    assert "LEFT JOIN LATERAL" in sql
    assert "flood.return_period_result" in sql
    assert "AS has_product" in sql
    assert "AS has_peak" in sql
    assert ") fpq ON TRUE" in sql


def test_join_available_branch_preserves_materialized_path() -> None:
    sql = _flood_product_quality_join("fpq", available=True)
    assert "LEFT JOIN flood.run_product_quality fpq" in sql
    assert "LATERAL" not in sql
    # 默认值即 materialized，保证既有调用点行为不变
    assert _flood_product_quality_join("fpq") == sql


def test_ready_absent_branch_shows_when_db_or_published_has_product() -> None:
    sql = _flood_product_ready_sql("fpq", available=False)
    assert sql == "(fpq.has_product OR h.status = 'published')"


def test_ready_available_branch_unchanged() -> None:
    sql = _flood_product_ready_sql("fpq", available=True)
    assert "has_product" not in sql
    assert "max_return_period_rows" in sql
    assert _flood_product_ready_sql("fpq") == sql


def test_select_absent_branch_maps_existence_to_row_counts() -> None:
    sql = _flood_product_quality_select("fpq", available=False)
    assert "fpq.has_peak AS flood_quality_max_over_window" in sql
    # has_product -> 1/0，喂给 _flood_product_quality_from_row 判 ready/unavailable
    assert "CASE WHEN fpq.has_product THEN 1 ELSE 0 END) AS flood_result_rows" in sql
    assert "CASE WHEN fpq.has_product THEN 1 ELSE 0 END) AS flood_return_period_rows" in sql
    assert "CASE WHEN fpq.has_product THEN 1 ELSE 0 END) AS flood_warning_rows" in sql
    assert "max_result_rows" not in sql


def test_select_available_branch_unchanged() -> None:
    sql = _flood_product_quality_select("fpq", available=True)
    assert "max_result_rows" in sql
    assert "has_product" not in sql
    assert _flood_product_quality_select("fpq") == sql
