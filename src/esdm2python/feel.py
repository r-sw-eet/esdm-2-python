"""A tiny FEEL subset (esdm-extensions proposal 0002) for command guards.

Supports comparisons (`= != < <= > >=`), `and`/`or`/`not(...)`, membership
(`x in [a, b]`), parentheses, literals, field identifiers, and niladic
`today()`/`now()`. Ported from the sibling generators' `feel/` package:
lex -> recursive-descent parse -> validate (bind identifiers) -> compile to a
Python boolean expression. Precedence: or < and < comparison < primary.
"""

from __future__ import annotations

import re
from typing import Any, Callable

_TOKEN = re.compile(
    r"""(?P<ws>\s+)
      | (?P<num>\d+(?:\.\d+)?)
      | (?P<str>"[^"]*")
      | (?P<op><=|>=|!=|=|<|>)
      | (?P<punc>[()\[\],])
      | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
    """,
    re.VERBOSE,
)

_KEYWORDS = {"and", "or", "not", "in", "true", "false", "null", "today", "now"}


class FeelError(ValueError):
    pass


def tokenize(source: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(source):
        match = _TOKEN.match(source, pos)
        if match is None:
            raise FeelError(f"unexpected character at {pos}: {source[pos:pos + 10]!r}")
        pos = match.end()
        kind = match.lastgroup
        if kind == "ws":
            continue
        value = match.group()
        if kind == "name" and value in _KEYWORDS:
            tokens.append(("kw", value))
        else:
            tokens.append((kind, value))
    tokens.append(("eof", ""))
    return tokens


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self.tokens = tokens
        self.i = 0

    def _peek(self) -> tuple[str, str]:
        return self.tokens[self.i]

    def _next(self) -> tuple[str, str]:
        token = self.tokens[self.i]
        self.i += 1
        return token

    def _expect(self, value: str) -> None:
        kind, got = self._next()
        if got != value:
            raise FeelError(f"expected {value!r}, got {got!r}")

    def parse(self) -> dict:
        node = self._or()
        if self._peek()[0] != "eof":
            raise FeelError(f"trailing tokens: {self._peek()[1]!r}")
        return node

    def _or(self) -> dict:
        node = self._and()
        while self._peek() == ("kw", "or"):
            self._next()
            node = {"t": "or", "l": node, "r": self._and()}
        return node

    def _and(self) -> dict:
        node = self._comparison()
        while self._peek() == ("kw", "and"):
            self._next()
            node = {"t": "and", "l": node, "r": self._comparison()}
        return node

    def _comparison(self) -> dict:
        node = self._primary()
        kind, value = self._peek()
        if kind == "op":
            self._next()
            return {"t": "bin", "op": value, "l": node, "r": self._primary()}
        if (kind, value) == ("kw", "in"):
            self._next()
            self._expect("[")
            items = [self._primary()]
            while self._peek() == ("punc", ","):
                self._next()
                items.append(self._primary())
            self._expect("]")
            return {"t": "in", "e": node, "list": items}
        return node

    def _primary(self) -> dict:
        kind, value = self._next()
        if (kind, value) == ("punc", "("):
            node = self._or()
            self._expect(")")
            return node
        if (kind, value) == ("kw", "not"):
            self._expect("(")
            node = self._or()
            self._expect(")")
            return {"t": "not", "e": node}
        if (kind, value) in (("kw", "today"), ("kw", "now")):
            self._expect("(")
            self._expect(")")
            return {"t": "call", "fn": value}
        if (kind, value) == ("kw", "null"):
            # without this, `null` lexes as a name and binds as an unknown field
            return {"t": "null"}
        if (kind, value) == ("kw", "true"):
            return {"t": "bool", "v": True}
        if (kind, value) == ("kw", "false"):
            return {"t": "bool", "v": False}
        if kind == "num":
            return {"t": "num", "v": float(value) if "." in value else int(value)}
        if kind == "str":
            return {"t": "str", "v": value[1:-1]}
        if kind == "name":
            return {"t": "id", "name": value}
        raise FeelError(f"unexpected token {value!r}")


def parse(source: str) -> dict:
    return _Parser(tokenize(source)).parse()


def identifiers(node: dict) -> list[str]:
    found: list[str] = []

    def walk(n: dict) -> None:
        t = n["t"]
        if t == "id":
            found.append(n["name"])
        elif t in ("or", "and"):
            walk(n["l"]); walk(n["r"])
        elif t == "bin":
            walk(n["l"]); walk(n["r"])
        elif t == "not":
            walk(n["e"])
        elif t == "in":
            walk(n["e"])
            for item in n["list"]:
                walk(item)

    walk(node)
    return found


def validate(node: dict, allowed_fields: set[str]) -> list[str]:
    return [f'unknown field "{name}"' for name in identifiers(node) if name not in allowed_fields]


def compile_to_python(node: dict, id_to_py: Callable[[str], str]) -> tuple[str, bool, bool]:
    """Compile a FEEL AST to a Python boolean expression over aggregate state."""
    uses = {"today": False, "now": False}

    def emit(n: dict) -> str:
        t = n["t"]
        if t == "or":
            return f"({emit(n['l'])} or {emit(n['r'])})"
        if t == "and":
            return f"({emit(n['l'])} and {emit(n['r'])})"
        if t == "not":
            return f"(not ({emit(n['e'])}))"
        if t == "bin":
            op = "==" if n["op"] == "=" else n["op"]
            return f"({emit(n['l'])} {op} {emit(n['r'])})"
        if t == "in":
            items = ", ".join(emit(item) for item in n["list"])
            return f"({emit(n['e'])} in [{items}])"
        if t == "id":
            return id_to_py(n["name"])
        if t == "str":
            return repr(n["v"])
        if t == "num":
            return repr(n["v"])
        if t == "bool":
            return "True" if n["v"] else "False"
        if t == "null":
            return "None"
        if t == "call":
            uses[n["fn"]] = True
            return n["fn"]
        raise FeelError(f"cannot compile node {n!r}")

    expr = emit(node)
    return expr, uses["today"], uses["now"]
