"""Publication of the #2348 runtime response models (``apps/api/response_models``).

Two passes, generalising the envelope reshaping that
``openapi_patching._patch_layer_metadata_openapi`` does for the four display
routes:

1. ``_patch_response_model_envelopes`` runs first, on the raw FastAPI document.
   Every success response whose schema ``$ref``s an :class:`OkEnvelope`
   subclass is republished as ``allOf[SuccessEnvelope, {data}]``, where
   ``data`` is the envelope's own generated ``data`` property. Bare routes
   (forecast-series, best-available, state snapshots) keep the generated schema
   at the response root. The hand patches that run afterwards then overwrite,
   by component name, every data schema that has a hand-written contract.
2. ``_prune_unreferenced_response_model_components`` runs last, before
   ``_finalize_openapi_schema``: a component generated from a response model
   that no published path reaches (the envelopes, and generated duplicates a
   hand schema replaced) is dropped so it cannot leak into the static document
   or ``types.ts``. Only response-model names are candidates; every other
   component is left alone. A surviving generated component (its ``title`` is
   still its own name, i.e. no hand schema replaced it) also loses the
   ``description`` pydantic copies from the class docstring: those docstrings
   are developer source pointers, not public contract text.

An owner module: it never imports the ``openapi_patching`` facade.
"""

from __future__ import annotations

import copy
import inspect
from enum import Enum
from typing import Any

from pydantic import BaseModel

from apps.api.openapi_patching_envelopes import _success_envelope_schema, _success_response_schema
from apps.api.response_models import (
    best_available_selection,
    data_sources,
    envelope,
    forecast,
    models,
    pipeline,
    state_snapshots,
)

_RESPONSE_MODEL_MODULES = (
    envelope,
    forecast,
    models,
    data_sources,
    best_available_selection,
    state_snapshots,
    pipeline,
)
_SUCCESS_STATUSES = ("200", "201")
_COMPONENT_PREFIX = "#/components/schemas/"


def _response_model_classes() -> list[type]:
    classes: list[type] = []
    for module in _RESPONSE_MODEL_MODULES:
        for _, value in inspect.getmembers(module, inspect.isclass):
            if value.__module__ == module.__name__ and issubclass(value, BaseModel | Enum):
                classes.append(value)
    return classes


def response_model_component_names() -> frozenset[str]:
    """Every OpenAPI component name a response model can generate."""
    return frozenset(cls.__name__ for cls in _response_model_classes())


def response_model_envelope_names() -> frozenset[str]:
    return frozenset(
        cls.__name__
        for cls in _response_model_classes()
        if issubclass(cls, envelope.OkEnvelope) and "data" in cls.model_fields
    )


def _patch_response_model_envelopes(schema: dict) -> None:
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    envelope_names = response_model_envelope_names()
    reshaped = False
    for operations in schema.get("paths", {}).values():
        for operation in operations.values():
            if not isinstance(operation, dict):
                continue
            for status in _SUCCESS_STATUSES:
                content = operation.get("responses", {}).get(status, {}).get("content", {}).get("application/json")
                if not content:
                    continue
                name = _component_name(content.get("schema", {}).get("$ref"))
                if name not in envelope_names:
                    continue
                data_schema = copy.deepcopy(components[name]["properties"]["data"])
                data_schema.pop("title", None)
                content["schema"] = _success_response_schema(data_schema)
                reshaped = True
    if reshaped:
        components["SuccessEnvelope"] = _success_envelope_schema()


def _prune_unreferenced_response_model_components(schema: dict) -> None:
    components = schema.get("components", {}).get("schemas", {})
    candidates = response_model_component_names()
    reachable = _reachable_component_names(schema)
    for name in [name for name in components if name in candidates and name not in reachable]:
        del components[name]
    for name in candidates & set(components):
        # A component still carrying its generated ``title`` was not replaced by
        # a hand schema; its ``description`` is the model's developer docstring
        # (source pointers), not public contract text.
        if components[name].get("title") == name:
            components[name].pop("description", None)


def _reachable_component_names(schema: dict) -> set[str]:
    components = schema.get("components", {})
    schemas = components.get("schemas", {})
    roots = [schema.get("paths", {})] + [value for key, value in components.items() if key != "schemas"]
    reachable: set[str] = set()
    pending = [name for root in roots for name in _refs(root)]
    while pending:
        name = pending.pop()
        if name in reachable or name not in schemas:
            continue
        reachable.add(name)
        pending.extend(_refs(schemas[name]))
    return reachable


def _refs(node: Any) -> list[str]:
    found: list[str] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            name = _component_name(current.get("$ref"))
            if name is not None:
                found.append(name)
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return found


def _component_name(ref: Any) -> str | None:
    if isinstance(ref, str) and ref.startswith(_COMPONENT_PREFIX):
        return ref.removeprefix(_COMPONENT_PREFIX)
    return None
