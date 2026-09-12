"""Test-only catalog replacements for selected-origin identity proofs.

These helpers construct FakeConnection inputs. They do not implement the
selected-vs-current comparison, shipping-role proof, or discriminator body.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from packages.common.compressed_chunk_cold_residency import CatalogChunk
from tests.cold_residency_fakes import FakeConnection, complete_relations

FIRST_RELOAD_DRIFT_CASES = (
    "origin_oid",
    "range_start",
    "range_end",
    "compressed_sibling",
)

_MUTATION_TOKENS = (
    "SET TABLESPACE",
    "decompress_chunk",
    "compress_chunk",
)


def apply_first_reload_replacement(connection: FakeConnection, selected: CatalogChunk, kind: str) -> CatalogChunk:
    """Replace the selected origin in place with a same schema/name drift."""

    if kind == "origin_oid":
        replacement = CatalogChunk(
            selected.hypertable_schema,
            selected.hypertable_name,
            11,
            selected.origin_schema,
            selected.origin_name,
            21,
            selected.compressed_schema,
            selected.compressed_name,
            selected.range_start,
            selected.range_end,
            selected.is_compressed,
        )
        relations = complete_relations(
            origin_oid=11,
            compressed_oid=21,
            origin_name=selected.origin_name,
            compressed_name=selected.compressed_name or "compress_hyper_2_2_chunk",
        )
    elif kind == "range_start":
        replacement = CatalogChunk(
            selected.hypertable_schema,
            selected.hypertable_name,
            selected.origin_oid,
            selected.origin_schema,
            selected.origin_name,
            selected.compressed_oid,
            selected.compressed_schema,
            selected.compressed_name,
            selected.range_start - timedelta(days=1),
            selected.range_end,
            selected.is_compressed,
        )
        relations = complete_relations()
    elif kind == "range_end":
        replacement = CatalogChunk(
            selected.hypertable_schema,
            selected.hypertable_name,
            selected.origin_oid,
            selected.origin_schema,
            selected.origin_name,
            selected.compressed_oid,
            selected.compressed_schema,
            selected.compressed_name,
            selected.range_start,
            selected.range_end - timedelta(hours=1),
            selected.is_compressed,
        )
        relations = complete_relations()
    elif kind == "compressed_sibling":
        replacement = CatalogChunk(
            selected.hypertable_schema,
            selected.hypertable_name,
            selected.origin_oid,
            selected.origin_schema,
            selected.origin_name,
            21,
            selected.compressed_schema,
            "compress_hyper_replaced_chunk",
            selected.range_start,
            selected.range_end,
            selected.is_compressed,
        )
        relations = complete_relations(
            origin_oid=selected.origin_oid,
            compressed_oid=21,
            origin_name=selected.origin_name,
            compressed_name="compress_hyper_replaced_chunk",
        )
    else:
        raise ValueError(f"unknown first-reload replacement {kind}")

    connection.pending_reload = (replacement, relations)
    return replacement


def first_reload_mutation_sql(connection: FakeConnection) -> list[str]:
    return [
        sql
        for sql, _params in connection.executed
        if any(token in sql for token in _MUTATION_TOKENS)
    ]


def call_name(node: Any) -> str | None:
    func = getattr(node, "func", None)
    if func is None:
        return None
    if getattr(func, "id", None):
        return str(func.id)
    attr = getattr(func, "attr", None)
    return str(attr) if attr else None
