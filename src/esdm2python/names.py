"""Naming helpers — the family's casing pipeline.

ESDM identifiers are kebab-case; casing is applied only at emit time. `studly`
(class names), `snake` (modules/tables/fields), `camel` (rarely needed here),
and `singular` (read-model class names: `tasks` -> `RmTask`).
"""

from __future__ import annotations

import re

_SEPARATORS = re.compile(r"[-_\s]+")
_CAMEL_HUMP = re.compile(r"([a-z0-9])([A-Z])")


def _words(value: str) -> list[str]:
    spaced = _CAMEL_HUMP.sub(r"\1 \2", _SEPARATORS.sub(" ", value))
    return [word for word in spaced.split() if word]


def studly(value: str) -> str:
    return "".join(word[:1].upper() + word[1:].lower() for word in _words(value))


def camel(value: str) -> str:
    pascal = studly(value)
    return pascal[:1].lower() + pascal[1:]


def snake(value: str) -> str:
    return "_".join(word.lower() for word in _words(value))


def singular(value: str) -> str:
    """Naive English singularization, enough for read-model names."""
    if value.endswith("ies") and len(value) > 3:
        return value[:-3] + "y"
    if value.endswith("s") and not value.endswith("ss"):
        return value[:-1]
    return value


def upper_const(value: str) -> str:
    return snake(value).upper()
