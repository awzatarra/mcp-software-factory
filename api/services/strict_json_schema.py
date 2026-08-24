from __future__ import annotations

from typing import Any

from openai.lib._pydantic import to_strict_json_schema
from pydantic import BaseModel


class InvalidStrictJsonSchema(ValueError):
    pass


def strict_json_schema_for_model(model: type[BaseModel]) -> dict[str, Any]:
    """Build the same strict schema used by the OpenAI Python SDK."""
    schema = to_strict_json_schema(model)
    validate_strict_json_schema(schema)
    return schema


def validate_strict_json_schema(schema: dict[str, Any]) -> None:
    if not isinstance(schema, dict) or not schema:
        raise InvalidStrictJsonSchema("schema must be a non-empty object")

    definitions = schema.get("$defs", {})
    if definitions is not None and not isinstance(definitions, dict):
        raise InvalidStrictJsonSchema("$defs must be an object")

    def visit(node: Any, path: str) -> None:
        if not isinstance(node, dict) or not node:
            raise InvalidStrictJsonSchema(f"{path} must be a non-empty schema object")

        reference = node.get("$ref")
        if reference is not None:
            prefix = "#/$defs/"
            if not isinstance(reference, str) or not reference.startswith(prefix):
                raise InvalidStrictJsonSchema(f"{path} has an unsupported $ref")
            name = reference[len(prefix):]
            if name not in definitions:
                raise InvalidStrictJsonSchema(f"{path} references missing definition {name!r}")

        combinators = [key for key in ("anyOf", "oneOf") if key in node]
        if len(combinators) > 1:
            raise InvalidStrictJsonSchema(f"{path} has ambiguous schema combinators")
        for combinator in combinators:
            variants = node[combinator]
            if not isinstance(variants, list) or not variants:
                raise InvalidStrictJsonSchema(f"{path}.{combinator} must contain schemas")
            for index, variant in enumerate(variants):
                visit(variant, f"{path}.{combinator}[{index}]")

        node_type = node.get("type")
        types = set(node_type) if isinstance(node_type, list) else {node_type}
        if "object" in types:
            properties = node.get("properties")
            required = node.get("required")
            if not isinstance(properties, dict):
                raise InvalidStrictJsonSchema(f"{path}.properties must be an object")
            if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
                raise InvalidStrictJsonSchema(f"{path}.required must be an array of property names")
            if len(required) != len(set(required)):
                raise InvalidStrictJsonSchema(f"{path}.required contains duplicates")
            missing = sorted(set(properties) - set(required))
            unknown = sorted(set(required) - set(properties))
            if missing:
                raise InvalidStrictJsonSchema(f"{path}.required is missing properties: {missing}")
            if unknown:
                raise InvalidStrictJsonSchema(f"{path}.required contains unknown properties: {unknown}")
            if node.get("additionalProperties") is not False:
                raise InvalidStrictJsonSchema(f"{path}.additionalProperties must be false")
            for name, child in properties.items():
                visit(child, f"{path}.properties.{name}")

        if "array" in types:
            items = node.get("items")
            if not isinstance(items, dict) or not items:
                raise InvalidStrictJsonSchema(f"{path}.items must be a non-empty schema")
            visit(items, f"{path}.items")

        if node_type is None and reference is None and not combinators:
            raise InvalidStrictJsonSchema(f"{path} has no type, $ref, or schema combinator")

    for name, definition in definitions.items():
        visit(definition, f"$.$defs.{name}")
    visit(schema, "$")
