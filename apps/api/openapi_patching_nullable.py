"""OpenAPI 3.1 nullable normalization: the `_finalize_openapi_schema` recursion.

Split out of `apps/api/openapi_patching.py` (#2074). `_remove_nullable_keywords`
is re-exported by the facade because `tests/test_openapi_31_contract.py` drives it
through `openapi_patching`.
"""

from typing import Any


def _remove_nullable_keywords(node: Any) -> Any:
    """Recursively re-express every ``nullable`` schema as valid OpenAPI 3.1.

    Children are normalized before the current node so a nested nullable node
    cannot survive inside a parent that is itself re-expressed. The current
    node's nullable handling:

    - no ``nullable`` key: recurse into all values and return a new dict;
    - ``nullable: False``: drop the legacy keyword and recurse — the value
      domain stays non-nullable (no type union is introduced);
    - ``nullable: True`` with a scalar ``type: T``: ``type: [T, "null"]``;
    - ``nullable: True`` with a valid JSON Schema type array (list[str], e.g.
      ``["string", "number"]``): keep the member order and append ``"null"``
      exactly once if absent, never duplicating it;
    - ``nullable: True`` with ``type + allOf``: the complete composition-or-null
      union (allOf and all siblings recursed, annotations preserved); the
      composition branch's type keeps the same scalar/type-array rules;
    - any other shape (empty type array, non-string member, non str/list type)
      or a non-boolean ``nullable`` value: raise ValueError instead of silently
      weakening the schema.
    """
    if isinstance(node, dict):
        normalized = {key: _remove_nullable_keywords(value) for key, value in node.items()}
        if "nullable" not in normalized:
            return normalized
        nullable = normalized.pop("nullable")
        if not isinstance(nullable, bool):
            raise ValueError(
                f"unable to finalize schema with non-boolean nullable value {nullable!r}: {node!r}"
            )
        if not nullable:
            return normalized
        schema_type = normalized.get("type")
        if "allOf" in normalized:
            composed_type = normalized.pop("type", None)
            all_of = normalized.pop("allOf")
            composed = {"allOf": all_of, "type": _composition_type(composed_type, node=node)}
            composed.update(normalized)
            # The composition branch stays non-null: openapi-typescript turns a
            # null member inside a type+allOf branch into an erroneous
            # intersection, so nullability lives only in the outer anyOf branch.
            return {"anyOf": [composed, {"type": "null"}]}
        normalized["type"] = _with_null(schema_type, node=node)
        return normalized
    if isinstance(node, list):
        return [_remove_nullable_keywords(item) for item in node]
    return node


def _with_null(value: Any, *, node: Any) -> list[str]:
    """Return ``value`` as a type array with ``"null"`` included exactly once.

    A scalar string becomes ``[T, "null"]``; a valid JSON Schema type array
    (non-empty, all string members) keeps its member order and normalizes the
    ``"null"`` members to exactly one (deduplicating any existing ones and
    appending once if absent). Any other value (missing type, empty array,
    non-string member, or a non str/list shape) raises instead of silently
    weakening the schema.
    """
    if isinstance(value, str):
        if value == "null":
            return ["null"]
        return [value, "null"]
    if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
        non_null = [item for item in value if item != "null"]
        return [*non_null, "null"]
    raise ValueError(
        "unable to finalize nullable schema without a scalar type or non-empty "
        f"string type array: {node!r}"
    )


def _composition_type(value: Any, *, node: Any) -> str | list[str]:
    """The non-null type for a type+allOf composition branch.

    The composition branch must never carry a null member (openapi-typescript
    would emit an erroneous intersection), so any ``"null"`` is stripped from a
    type array. A non-null scalar string stays scalar. A missing type, a
    ``"null"`` scalar, an empty type array, an array with no non-null members
    (e.g. ``["null"]``), or a non str/list shape raises instead of silently
    weakening the schema.
    """
    if isinstance(value, str):
        if value == "null":
            raise ValueError(
                "unable to finalize nullable type+allOf composition with no non-null "
                f"type member: {node!r}"
            )
        return value
    if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
        non_null = [item for item in value if item != "null"]
        if not non_null:
            raise ValueError(
                "unable to finalize nullable type+allOf composition with no non-null "
                f"type member: {node!r}"
            )
        return non_null
    raise ValueError(
        "unable to finalize nullable type+allOf composition without a scalar type or "
        f"non-empty type array: {node!r}"
    )
