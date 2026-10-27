"""Every setting the config models accept, ``schema/clients.schema.json`` accepts too.

``clients.yml`` is checked twice: against the JSON schema, then by the pydantic models. A field
added to a model and not to the schema is rejected at load — by the schema, before the model ever
sees it — so the setting exists in code and cannot be used. That happened with
``process_list.target_url_column`` (2026-10-08): every test built its config through the models,
``clients.example.yml`` had the line commented out, and it only surfaced when the operator's config
turned it on. Two implementations of one contract drift silently; this walks both.
"""

from __future__ import annotations

import copy
import json
import sys
import typing
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from lib.config import ClientConfig

_SCHEMA = json.loads(Path("schema/clients.schema.json").read_text(encoding="utf-8"))

#: Model fields that are not settings in the file: ``client_id`` is the key the client sits under.
_NOT_IN_THE_FILE = {"client.client_id"}


def _resolve(node: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in node:
        target: Any = _SCHEMA
        for part in node["$ref"].lstrip("#/").split("/"):
            target = target[part]
        node = target
    return node


def _models_in(annotation: Any) -> list[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    return [model for arg in typing.get_args(annotation) for model in _models_in(arg)]


def _object_node(node: dict[str, Any]) -> dict[str, Any]:
    """The schema object a model's fields are listed in: through arrays and keyed maps."""
    node = _resolve(node)
    if node.get("type") == "array":
        node = _resolve(node.get("items", {}))
    extra = node.get("additionalProperties")
    if isinstance(extra, dict) and "properties" not in node:
        node = _resolve(extra)
    return node


def _missing(model: type[BaseModel], node: dict[str, Any], path: str) -> list[str]:
    node = _object_node(node)
    properties = node.get("properties")
    if properties is None:
        return []
    missing = []
    for name, field in model.model_fields.items():
        key = field.alias or name
        where = f"{path}.{key}"
        if key not in properties:
            if node.get("additionalProperties") is False and where not in _NOT_IN_THE_FILE:
                missing.append(where)
            continue
        for nested in _models_in(field.annotation):
            missing += _missing(nested, properties[key], where)
    return missing


def test_the_schema_accepts_every_setting_the_models_define() -> None:
    client = _SCHEMA["properties"]["clients"]["additionalProperties"]

    assert _missing(ClientConfig, client, "client") == []


def test_the_walk_would_have_caught_the_setting_that_slipped_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Against the schema as it was before the fix, the walk names exactly that setting."""
    before = copy.deepcopy(_SCHEMA)
    monkeypatch.setattr(sys.modules[__name__], "_SCHEMA", before)
    client = _resolve(before["properties"]["clients"]["additionalProperties"])
    process_list = _resolve(client["properties"]["process_list"])
    del process_list["properties"]["target_url_column"]

    assert _missing(ClientConfig, client, "client") == ["client.process_list.target_url_column"]
