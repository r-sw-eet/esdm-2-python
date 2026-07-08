"""Command/event lifecycle — create / mutate / delete.

Derived from the first token of the kebab name against verb lists (an explicit
`esdm-extensions.io/lifecycle` annotation overrides). Ported verbatim from the
sibling generators so the whole family classifies commands identically.
"""

from __future__ import annotations

import re
from enum import Enum

_CREATE_VERBS = {
    "add", "create", "register", "open", "start", "new",
    "init", "submit", "draft", "place", "raise", "issue", "request",
}
_DELETE_VERBS = {"delete", "remove", "archive", "close", "cancel", "discard", "withdraw"}


class Lifecycle(str, Enum):
    CREATE = "create"
    MUTATE = "mutate"
    DELETE = "delete"

    @classmethod
    def from_name(cls, name: str, annotation: str | None = None) -> "Lifecycle":
        if annotation is not None:
            return cls(annotation)
        verb = re.split(r"[-_]", name)[0].lower() if name else ""
        if verb in _CREATE_VERBS:
            return cls.CREATE
        if verb in _DELETE_VERBS:
            return cls.DELETE
        return cls.MUTATE
