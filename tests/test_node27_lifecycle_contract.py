"""Public D3 and historical receipt compatibility at the expand boundary."""

import copy
import json
from pathlib import Path

import jsonschema
import pytest

from scripts.node27_timeseries_compression_supervisor import SupervisorError, validate_current_d3

ROOT = Path(__file__).resolve().parents[1]
FIELDS = (
    "hypertable_schema",
    "hypertable_name",
    "attname",
    "segmentby_column_index",
    "orderby_column_index",
    "orderby_asc",
    "orderby_nullsfirst",
)
TEXT = {
    "hydro.river_timeseries": ("run_id", "river_network_version_id", "river_segment_id"),
    "met.forcing_station_timeseries": ("forcing_version_id", "station_id"),
}
NARROW = {
    "hydro.river_timeseries": ("run_key", "river_segment_key"),
    "met.forcing_station_timeseries": ("forcing_version_key", "station_key"),
}


def catalog(siblings):
    shapes = dict(TEXT)
    for name in siblings:
        shapes[name + "_legacy"] = TEXT[name]
        shapes[name] = NARROW[name]
    rows = []
    for key, segments in sorted(shapes.items()):
        schema, name = key.split(".")
        for index, column in enumerate(segments, 1):
            rows.append(dict(zip(FIELDS, (schema, name, column, index, None, None, None))))
        variable = "variable_e" if key in siblings else "variable"
        for index, column in enumerate((variable, "valid_time"), 1):
            rows.append(dict(zip(FIELDS, (schema, name, column, None, index, True, False))))
    return {"hypertables": dict.fromkeys(shapes, True), "compression_settings": rows, "policy_jobs": []}


@pytest.mark.parametrize(
    "siblings",
    [(), ("hydro.river_timeseries",), ("met.forcing_station_timeseries",), tuple(TEXT)],
)
def test_d3_shape_changes_only_for_its_own_sibling(siblings):
    observed = catalog(siblings)
    validate_current_d3(observed)
    wrong = copy.deepcopy(observed)
    wrong["compression_settings"][0]["attname"] = "incorrect_key"
    with pytest.raises(SupervisorError):
        validate_current_d3(wrong)


@pytest.mark.parametrize("table", tuple(TEXT))
def test_d3_rejects_early_flip(table):
    observed = catalog(())
    for row in observed["compression_settings"]:
        if f"{row['hypertable_schema']}.{row['hypertable_name']}" == table and row["attname"] == "variable":
            row["attname"] = "variable_e"
    with pytest.raises(SupervisorError):
        validate_current_d3(observed)


def test_compression_receipt_keeps_canonical_required_and_accepts_legacy():
    schema = json.loads((ROOT / "schemas/timeseries_compression_receipt.schema.json").read_text())
    receipt = json.loads((ROOT / "schemas/examples/timeseries_compression_receipt.example.json").read_text())
    jsonschema.validate(receipt, schema)
    for key in TEXT:
        receipt["per_table_totals"][key + "_legacy"] = {
            "before_bytes": 10,
            "after_bytes": 5,
            "chunks_compressed": 1,
        }
    jsonschema.validate(receipt, schema)
    invalid = copy.deepcopy(receipt)
    del invalid["per_table_totals"]["hydro.river_timeseries"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(invalid, schema)
    receipt["per_table_totals"]["hydro.unrelated_legacy"] = {
        "before_bytes": 10,
        "after_bytes": 5,
        "chunks_compressed": 1,
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(receipt, schema)


def test_retention_legacy_counts_are_optional_and_nonnegative():
    schema = json.loads((ROOT / "schemas/timeseries_retention_receipt.schema.json").read_text())
    receipt = {
        "schema_version": "1.1",
        "generated_at": "2026-09-01T00:00:00Z",
        "mode": "dry-run",
        "outcome": "dry-run",
        "archive_gate": {
            "mode": "disabled",
            "adr_reference": "docs/adr/0002-node27-timeseries-hot-cold-tiering.md Revision 2026-08-11",
        },
        "candidate_chunks": [],
        "deferred_remainder": [],
    }
    jsonschema.validate(receipt, schema)
    receipt["legacy_chunks"] = {"hydro.river_timeseries_legacy": 2, "met.forcing_station_timeseries_legacy": 0}
    jsonschema.validate(receipt, schema)
    receipt["legacy_chunks"]["hydro.river_timeseries_legacy"] = -1
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(receipt, schema)
