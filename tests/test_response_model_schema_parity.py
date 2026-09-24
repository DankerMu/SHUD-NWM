"""#2348: bind every published success schema to the route's runtime model.

The published ``openapi/nhms.v1.yaml`` keeps its hand-written data schemas
(``apps/api/openapi_restored_schemas.py`` and the ``openapi_patching*``
owners), while the wire is now shaped by ``apps/api/response_models``. The two
are independent sources, so nothing but this test stops them drifting apart.

For every modelled JSON route (the 35 #2348 routes plus the four display
routes modelled earlier) the model's serialization JSON schema is compared with
the published ``data`` schema (or the response root for the bare routes), and
the ops ``identity`` envelope member with ``OpsStrictIdentity``:

* at every object level where BOTH sides declare ``properties``: the same
  property-name set and the same ``required`` set;
* a model-side open mapping (``dict[str, Any]`` / ``Any``) is a leaf;
* arrays compare their ``items``; ``$ref`` resolves on each side;
* alternatives (``oneOf``/``anyOf`` minus ``null``): every published
  alternative matches some model alternative and vice versa;
* nullability: a published-nullable position is nullable in the model;
* enums: where both sides enumerate, the value sets are equal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.routing import APIRoute
from pydantic import BaseModel, TypeAdapter

from apps.api.main import app
from apps.api.response_models.envelope import OkEnvelope
from tests.test_response_model_preservation import returns_response_object

SPEC_PATH = Path(__file__).resolve().parents[1] / "openapi" / "nhms.v1.yaml"
SPEC: dict[str, Any] = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
COMPONENTS: dict[str, Any] = SPEC["components"]["schemas"]
ENVELOPE_KEYS = {"request_id", "status", "data", "auth_policy_decisions"}
_NULL = {"type": "null"}


def _business_routes() -> list[APIRoute]:
    return [
        route
        for route in app.routes
        if isinstance(route, APIRoute) and route.endpoint.__module__.startswith("apps.api.routes.")
    ]


def _modelled_routes() -> list[APIRoute]:
    return [route for route in _business_routes() if route.response_model is not None]


ROUTES = {f"{sorted(route.methods)[0]} {route.path}": route for route in _modelled_routes()}


class _Side:
    """One schema document: resolves its own ``$ref``s."""

    def __init__(self, defs: dict[str, Any], prefix: str) -> None:
        self.defs = defs
        self.prefix = prefix

    def resolve(self, node: Any) -> dict[str, Any]:
        while isinstance(node, dict) and "$ref" in node:
            node = {
                **self.defs[node["$ref"].removeprefix(self.prefix)],
                **{k: v for k, v in node.items() if k != "$ref"},
            }
        if isinstance(node, dict) and "allOf" in node:
            # `allOf: [$ref, {extension}]`: one object, properties and required merged.
            merged: dict[str, Any] = {k: v for k, v in node.items() if k != "allOf"}
            for part in (self.resolve(part) for part in node["allOf"]):
                properties = {**merged.get("properties", {}), **part.get("properties", {})}
                required = [*merged.get("required", ()), *part.get("required", ())]
                merged = {**part, **merged}
                if properties:
                    merged["properties"] = properties
                if required:
                    merged["required"] = sorted(set(required))
            return merged
        return node if isinstance(node, dict) else {}

    def alternatives(self, node: Any) -> tuple[list[dict[str, Any]], bool]:
        node = self.resolve(node)
        types = node.get("type")
        if isinstance(types, list):
            non_null = [t for t in types if t != "null"]
            base = {k: v for k, v in node.items() if k != "type"}
            return [{**base, "type": t} for t in non_null], "null" in types
        options = node.get("oneOf") or node.get("anyOf")
        if options is None:
            return [node], node.get("type") == "null"
        nullable = False
        resolved: list[dict[str, Any]] = []
        for option in options:
            inner, inner_null = self.alternatives(option)
            nullable = nullable or inner_null
            resolved.extend(alt for alt in inner if alt != _NULL and alt.get("type") != "null")
        return resolved, nullable


def _is_open(node: dict[str, Any]) -> bool:
    if not node or set(node) <= {"title", "description", "default", "example", "examples"}:
        return True  # `Any`
    return node.get("type") == "object" and "properties" not in node


def _compatible_scalar(model: dict[str, Any], hand: dict[str, Any]) -> bool:
    model_type, hand_type = model.get("type"), hand.get("type")
    if model_type is None or hand_type is None or model_type == hand_type:
        return True
    return {model_type, hand_type} == {"integer", "number"}


Sides = tuple[_Side, _Side, bool]  # (model, published, defaulted model fields always emitted)


def compare(model: Any, hand: Any, sides: Sides, path: str) -> list[str]:
    model_side, hand_side, _ = sides
    model_alts, model_null = model_side.alternatives(model)
    if any(_is_open(alt) for alt in model_alts):
        return []  # `Any` / open mapping: a leaf that also admits null
    hand_alts, hand_null = hand_side.alternatives(hand)
    errors: list[str] = []
    if hand_null and not model_null:
        errors.append(f"{path}: published nullable, model not")
    if not hand_alts:
        return errors
    if len(model_alts) == 1 and len(hand_alts) == 1:
        return errors + _compare_one(model_alts[0], hand_alts[0], sides, path)
    for label, left, right, forward in (
        ("published", hand_alts, model_alts, False),
        ("model", model_alts, hand_alts, True),
    ):
        for index, alt in enumerate(left):
            pairs = [(alt, other) if forward else (other, alt) for other in right]
            if not any(not _compare_one(m, h, sides, path) for m, h in pairs):
                errors.append(f"{path}: {label} alternative #{index} matches nothing on the other side")
    return errors


def _required_error(model: dict[str, Any], hand: dict[str, Any], defaults_emitted: bool) -> str | None:
    """#2348 routes (``exclude_unset``): published ``required`` == model ``required``.

    The four pre-#2348 display routes emit defaulted fields too, so there a
    published-required key must be always present (model-required or
    defaulted) and a model-required key must be published-required; a
    published-optional key that happens to be always present is still true.
    """
    strict, published = set(model.get("required", ())), set(hand.get("required", ()))
    always = strict | {name for name, prop in model["properties"].items() if "default" in prop}
    if (published == strict) if not defaults_emitted else (strict <= published <= always):
        return None
    return f"required model {sorted(strict)} (always present {sorted(always)}) vs published {sorted(published)}"


def _compare_one(model: dict[str, Any], hand: dict[str, Any], sides: Sides, path: str) -> list[str]:
    errors: list[str] = []
    if "enum" in model and "enum" in hand and set(model["enum"]) != set(hand["enum"]):
        errors.append(f"{path}: enum {sorted(model['enum'])} != published {sorted(hand['enum'])}")
    if model.get("type") == "array" or hand.get("type") == "array":
        if model.get("type") != hand.get("type"):
            return [*errors, f"{path}: model {model.get('type')} vs published {hand.get('type')}"]
        return errors + compare(model.get("items", {}), hand.get("items", {}), sides, f"{path}[]")
    if "properties" in model and "properties" in hand:
        model_props, hand_props = set(model["properties"]), set(hand["properties"])
        if model_props != hand_props:
            model_only, published_only = sorted(model_props - hand_props), sorted(hand_props - model_props)
            errors.append(f"{path}: properties model-only {model_only} published-only {published_only}")
        if (required_error := _required_error(model, hand, sides[2])) is not None:
            errors.append(f"{path}: {required_error}")
        for name in sorted(model_props & hand_props):
            errors.extend(compare(model["properties"][name], hand["properties"][name], sides, f"{path}.{name}"))
        return errors
    if "properties" in model:
        # The published side documents nothing where the model is a typed object.
        return [*errors, f"{path}: model declares properties {sorted(model['properties'])}, published is open"]
    if not _compatible_scalar(model, hand):
        errors.append(f"{path}: model type {model.get('type')} vs published {hand.get('type')}")
    return errors


def _model_schemas(route: APIRoute) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    """(model data schema, model $defs, model identity schema or None)."""
    annotation = route.response_model
    schema = TypeAdapter(annotation).json_schema(mode="serialization", ref_template="#/$defs/{model}")
    defs = schema.pop("$defs", {})
    is_envelope = (
        isinstance(annotation, type) and issubclass(annotation, BaseModel) and "request_id" in annotation.model_fields
    )
    if not is_envelope:
        return schema, defs, None
    properties = schema["properties"]
    return properties["data"], defs, properties.get("identity")


def _published(route: APIRoute) -> tuple[dict[str, Any], dict[str, Any] | None]:
    method = sorted(route.methods)[0].lower()
    responses = SPEC["paths"][route.path][method]["responses"]
    status = "201" if "201" in responses else "200"
    schema = responses[status]["content"]["application/json"]["schema"]
    if "allOf" in schema and schema["allOf"][0] == {"$ref": "#/components/schemas/SuccessEnvelope"}:
        body = schema["allOf"][1]["properties"]
        return body["data"], body.get("identity")
    return schema, None


@pytest.mark.parametrize("route_key", sorted(ROUTES))
def test_published_success_schema_matches_the_route_model(route_key: str) -> None:
    route = ROUTES[route_key]
    model_data, defs, model_identity = _model_schemas(route)
    published_data, published_identity = _published(route)
    sides = (
        _Side(defs, "#/$defs/"),
        _Side(COMPONENTS, "#/components/schemas/"),
        not route.response_model_exclude_unset,
    )
    errors = compare(model_data, published_data, sides, "data")
    if published_identity is not None or model_identity is not None:
        assert published_identity is not None and model_identity is not None, route_key
        errors += compare(model_identity, published_identity, sides, "identity")
    assert not errors, "\n".join(errors)


def test_envelopes_declare_only_the_published_envelope_members() -> None:
    # `OkEnvelope` subclasses: request_id/status/data (+ identity on the ops
    # routes, compared above); auth_policy_decisions passes through as an extra.
    for route in ROUTES.values():
        model = route.response_model
        if isinstance(model, type) and issubclass(model, OkEnvelope):
            assert set(model.model_fields) - ENVELOPE_KEYS <= {"identity"}, model


def test_route_table_is_the_35_plus_4_modelled_routes() -> None:
    assert len(ROUTES) == 39, sorted(ROUTES)


def test_every_unmodelled_business_route_is_annotated_to_return_a_response() -> None:
    # `response_model is None` is also what FastAPI yields for an UNANNOTATED
    # handler, which this suite would otherwise skip without a comparison: only
    # a `-> Response` handler (tiles / PNG / basemap proxy) may stay unmodelled.
    unmodelled = [route for route in _business_routes() if route.response_model is None]
    unannotated = sorted(route.path for route in unmodelled if not returns_response_object(route))
    assert not unannotated, unannotated
    assert len(unmodelled) == 8, sorted(route.path for route in unmodelled)


def test_parity_rule_reds_on_a_renamed_property_a_required_drift_and_a_lost_null() -> None:
    hand = {
        "type": "object",
        "required": ["a"],
        "properties": {"a": {"type": "string"}, "b": {"anyOf": [{"type": "string"}, _NULL]}},
    }
    sides = (_Side({}, "#/$defs/"), _Side({}, "#/components/schemas/"), False)
    same = {
        "type": "object",
        "required": ["a"],
        "properties": {"a": {"type": "string"}, "b": {"anyOf": [{"type": "string"}, _NULL]}},
    }
    assert compare(same, hand, sides, "$") == []
    renamed = {**same, "properties": {"a": {"type": "string"}, "c": {"type": "string"}}}
    required = {**same, "required": ["a", "b"]}
    not_null = {**same, "properties": {"a": {"type": "string"}, "b": {"type": "string"}}}
    narrowed = {**same, "properties": {"a": {"type": "integer"}, "b": same["properties"]["b"]}}
    opened = {"type": "object", "additionalProperties": True}
    assert compare(same, opened, sides, "$")
    for broken in (renamed, required, not_null, narrowed):
        assert compare(broken, hand, sides, "$"), broken
