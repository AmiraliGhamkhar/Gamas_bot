"""The canonical Gamas note schema, expressed as JSON Schema for providers.

The *application-level* schema is :class:`gamas_bot.structuring.StructuredNotes`
and never changes: providers merely receive different *transport-level*
structured-output hints. This module builds the schema variants each transport
family accepts:

* :func:`note_json_schema` — the descriptive schema (OpenAI ``json_schema``
  without strict; Mistral json_schema; Gemini ``responseSchema`` after
  :func:`gemini_compat`).
* :func:`strict_note_json_schema` — OpenAI/Groq strict mode: every property is
  required and ``additionalProperties: false``. Gamas semantics stay unchanged
  because the prompt already tells the model to emit empty strings/arrays for
  unused optional keys, and the parser tolerates them.
* :func:`gemini_compat` — strips annotations the Gemini ``responseSchema``
  subset does not carry.
"""

from __future__ import annotations

import copy

_NOTE_SECTION_PROPERTIES = {
    "heading": {"type": "string"},
    "paragraphs": {"type": "array", "items": {"type": "string"}},
    "bullets": {"type": "array", "items": {"type": "string"}},
    "definitions": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "term": {"type": "string"},
                "term_en": {"type": "string"},
                "definition": {"type": "string"},
            },
        },
    },
    "examples": {"type": "array", "items": {"type": "string"}},
    "steps": {"type": "array", "items": {"type": "string"}},
    "formulas": {"type": "array", "items": {"type": "string"}},
    "key_points": {"type": "array", "items": {"type": "string"}},
    "table": {
        "type": "object",
        "properties": {
            "headers": {"type": "array", "items": {"type": "string"}},
            "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
        },
    },
    "callouts": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "kind": {"type": "string"},
                "text": {"type": "string"},
            },
        },
    },
}

_NOTE_PROPERTIES = {
    "title": {"type": "string"},
    "summary": {"type": "string"},
    "learning_objectives": {"type": "array", "items": {"type": "string"}},
    "sections": {
        "type": "array",
        "items": {"type": "object", "properties": _NOTE_SECTION_PROPERTIES},
    },
    "key_points": {"type": "array", "items": {"type": "string"}},
    "review_questions": {"type": "array", "items": {"type": "string"}},
    "glossary": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "term": {"type": "string"},
                "definition": {"type": "string"},
            },
        },
    },
}


def note_json_schema() -> dict:
    """Descriptive (non-strict) JSON Schema for the note payload."""
    return {"type": "object", "properties": copy.deepcopy(_NOTE_PROPERTIES)}


def _require_everything(node: dict) -> None:
    """OpenAI/Groq strict-mode transform, applied in place."""
    if not isinstance(node, dict):
        return
    properties = node.get("properties")
    if isinstance(properties, dict):
        node["required"] = sorted(properties)
        node["additionalProperties"] = False
        for child in properties.values():
            if isinstance(child, dict):
                _require_everything(child)
    items = node.get("items")
    if isinstance(items, dict):
        _require_everything(items)


def strict_note_json_schema() -> dict:
    """Strict-mode schema (all properties required, no extras tolerated)."""
    schema = note_json_schema()
    _require_everything(schema)
    return schema


def gemini_compat(schema: dict | None = None) -> dict:
    """Strip keywords outside the Gemini ``responseSchema`` subset."""
    cleaned = copy.deepcopy(schema if schema is not None else note_json_schema())

    def _walk(node):
        if not isinstance(node, dict):
            return
        for key in ("$schema", "$id", "description", "title"):
            node.pop(key, None)
        for child in list((node.get("properties") or {}).values()):
            _walk(child)
        items = node.get("items")
        if isinstance(items, dict):
            _walk(items)

    _walk(cleaned)
    return cleaned
