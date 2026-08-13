"""A FEEL context literal - ``{ requestId: id, name: customerName }`` - as used by extension
proposal 0005 to say what a reaction's emitted command carries.

Values are ordinary FEEL expressions bound against the handled event's payload, so the whole
expression language comes from :mod:`feel` and this module only splits the context into entries.
"""

from __future__ import annotations

import re

from .feel import FeelError, parse as parse_feel, validate as validate_feel

_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse(source: str) -> dict[str, dict]:
    """Parse a context literal into ``key -> value expression``, in author order."""
    body = source.strip()
    if not body.startswith("{") or not body.endswith("}"):
        raise FeelError("A mapping must be a FEEL context literal: { key: expression, ... }")

    entries: dict[str, dict] = {}
    for entry in _split_top_level(body[1:-1]):
        if not entry.strip():
            continue
        colon = _colon_at(entry)
        if colon < 0:
            raise FeelError(f'Mapping entry is not "key: expression": "{entry.strip()}"')
        key = entry[:colon].strip()
        if not _KEY.match(key):
            raise FeelError(f'Mapping key is not a field name: "{key}"')
        if key in entries:
            raise FeelError(f'Mapping assigns "{key}" twice')
        entries[key] = parse_feel(entry[colon + 1 :])

    if not entries:
        raise FeelError("A mapping must assign at least one field")

    return entries


def validate(mapping: dict[str, dict], allowed_fields: set[str]) -> list[str]:
    """Binding errors for every value expression, prefixed with the key they came from."""
    errors: list[str] = []
    for key, value in mapping.items():
        errors.extend(f"{key}: {error}" for error in validate_feel(value, allowed_fields))
    return errors


def _split_top_level(body: str) -> list[str]:
    """Split on top-level commas only, so a nested list or call keeps its own separators."""
    parts: list[str] = []
    depth = 0
    in_string = False
    start = 0

    for i, c in enumerate(body):
        if c == '"':
            in_string = not in_string
        elif not in_string and c in "([{":
            depth += 1
        elif not in_string and c in ")]}":
            depth -= 1
        elif not in_string and depth == 0 and c == ",":
            parts.append(body[start:i])
            start = i + 1
    parts.append(body[start:])

    return parts


def _colon_at(entry: str) -> int:
    """The key separator, skipping any colon inside a nested expression or a string."""
    depth = 0
    in_string = False

    for i, c in enumerate(entry):
        if c == '"':
            in_string = not in_string
        elif not in_string and c in "([{":
            depth += 1
        elif not in_string and c in ")]}":
            depth -= 1
        elif not in_string and depth == 0 and c == ":":
            return i

    return -1
