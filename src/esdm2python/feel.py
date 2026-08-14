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
      | (?P<range>\.\.)
      | (?P<op><=|>=|!=|=|<|>|[-+*/])
      | (?P<punc>[()\[\],])
      | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
    """,
    re.VERBOSE,
)

_KEYWORDS = {"and", "or", "not", "in", "between", "if", "then", "else", "true", "false", "null", "today", "now"}
_FUNCTIONS = {"date", "duration", "contains"}
_COMPARISONS = {"=", "!=", "<", "<=", ">", ">="}


def _range(value: dict, low: dict, high: dict) -> dict:
    return {
        "t": "and",
        "l": {"t": "bin", "op": ">=", "l": value, "r": low},
        "r": {"t": "bin", "op": "<=", "l": value, "r": high},
    }


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
        # `if` sits at the lowest precedence, so its branches are whole expressions and it needs
        # no parentheses to hold them.
        if self._peek() == ("kw", "if"):
            self._next()
            condition = self._or()
            if self._peek() != ("kw", "then"):
                raise FeelError('expected "then" in a conditional')
            self._next()
            when_true = self._or()
            if self._peek() != ("kw", "else"):
                raise FeelError('expected "else" in a conditional')
            self._next()
            return {"t": "cond", "c": condition, "a": when_true, "b": self._or()}

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
        node = self._additive()
        kind, value = self._peek()
        if kind == "op" and value in _COMPARISONS:
            self._next()
            return {"t": "bin", "op": value, "l": node, "r": self._additive()}
        if (kind, value) == ("kw", "in"):
            self._next()
            return self._membership(node)
        # `x between a and b` is sugar for two comparisons; desugaring here keeps every
        # compiler in the family unaware that it exists.
        if (kind, value) == ("kw", "between"):
            self._next()
            low = self._additive()
            if self._peek() != ("kw", "and"):
                raise FeelError('expected "and" in a between expression')
            self._next()
            return _range(node, low, self._additive())
        return node

    def _additive(self) -> dict:
        """Left-associative, and binding tighter than any comparison."""
        node = self._multiplicative()
        while self._peek() in (("op", "+"), ("op", "-")):
            op = self._next()[1]
            node = {"t": "bin", "op": op, "l": node, "r": self._multiplicative()}
        return node

    def _multiplicative(self) -> dict:
        """Binds tighter than + and -."""
        node = self._primary()
        while self._peek() in (("op", "*"), ("op", "/")):
            op = self._next()[1]
            node = {"t": "bin", "op": op, "l": node, "r": self._primary()}
        return node

    def _two_word_function(self, kind: str, value: str) -> str | None:
        """The function name if this token (plus maybe the next) starts a supported call."""
        if kind == "name" and value in ("starts", "ends"):
            following = self.tokens[self.i : self.i + 2]
            if len(following) == 2 and following[0][1].lower() == "with" and following[1][1] == "(":
                return f"{value} with"
        if value in _FUNCTIONS and self._peek() == ("punc", "("):
            return value
        return None

    def _arguments(self) -> list[dict]:
        self._expect("(")
        args: list[dict] = []
        if self._peek() != ("punc", ")"):
            args.append(self._or())
            while self._peek() == ("punc", ","):
                self._next()
                args.append(self._or())
        self._expect(")")
        return args

    def _membership(self, node: dict) -> dict:
        """`x in [a, b]` stays a membership test; `x in [1..10]` desugars to a range."""
        self._expect("[")
        first = self._primary()
        if self._peek() == ("range", ".."):
            self._next()
            high = self._primary()
            self._expect("]")
            return _range(node, first, high)
        items = [first]
        while self._peek() == ("punc", ","):
            self._next()
            items.append(self._primary())
        self._expect("]")
        return {"t": "in", "e": node, "list": items}

    def _primary(self) -> dict:
        if self._peek() == ("op", "-"):
            self._next()
            kind, value = self._peek()
            if kind == "num":
                self._next()
                return {"t": "num", "v": -(float(value) if "." in value else int(value))}
            return {"t": "neg", "e": self._primary()}

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
            return {"t": "call", "fn": value, "args": []}
        # FEEL spells some function names with a space, so the name is up to two tokens.
        function = self._two_word_function(kind, value)
        if function is not None:
            if " " in function:
                self._next()
            return {"t": "call", "fn": function, "args": self._arguments()}
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
        elif t in ("not", "neg"):
            walk(n["e"])
        elif t == "cond":
            walk(n["c"]); walk(n["a"]); walk(n["b"])
        elif t == "call":
            for argument in n.get("args", []):
                walk(argument)
        elif t == "in":
            walk(n["e"])
            for item in n["list"]:
                walk(item)

    walk(node)
    return found


_ARITY = {"today": 0, "now": 0, "date": 1, "duration": 1, "starts with": 2, "ends with": 2, "contains": 2}


def _calls(node: dict, errors: list[str]) -> None:
    t = node.get("t")
    if t == "call":
        arity = _ARITY.get(node["fn"])
        if arity is None:
            errors.append(f'unknown function "{node["fn"]}"')
        elif arity != len(node.get("args", [])):
            plural = "" if arity == 1 else "s"
            errors.append(f'{node["fn"]} takes {arity} argument{plural}, got {len(node.get("args", []))}')
    for key in ("l", "r", "e", "c", "a", "b"):
        if isinstance(node.get(key), dict):
            _calls(node[key], errors)
    for item in node.get("list", []) + node.get("args", []):
        _calls(item, errors)


def validate(node: dict, allowed_fields: set[str], field_types: dict[str, str] | None = None) -> list[str]:
    errors = [f'unknown field "{name}"' for name in identifiers(node) if name not in allowed_fields]
    _arithmetic(node, field_types or {}, errors)
    _calls(node, errors)
    return errors


_ARITHMETIC = {"+", "-", "*", "/"}


def _arithmetic(node: dict, types: dict[str, str], errors: list[str]) -> None:
    """The arithmetic gates of 0002's 2026-08-14 amendment: an operand declared string or boolean
    is not arithmetic, and a literal zero divisor never is. An absent type skips the type check."""
    t = node.get("t")
    if t == "bin":
        if node["op"] in _ARITHMETIC:
            _operand(node["l"], types, errors)
            _operand(node["r"], types, errors)
            if node["op"] == "/" and node["r"].get("t") == "num" and node["r"]["v"] == 0:
                errors.append("division by a literal zero")
        _arithmetic(node["l"], types, errors)
        _arithmetic(node["r"], types, errors)
    elif t in {"or", "and"}:
        _arithmetic(node["l"], types, errors)
        _arithmetic(node["r"], types, errors)
    elif t in {"not", "neg"}:
        _arithmetic(node["e"], types, errors)
    elif t == "call":
        for argument in node.get("args", []):
            _arithmetic(argument, types, errors)
    elif t == "cond":
        _arithmetic(node["c"], types, errors)
        _arithmetic(node["a"], types, errors)
        _arithmetic(node["b"], types, errors)
    elif t == "in":
        _arithmetic(node["e"], types, errors)
        for item in node["list"]:
            _arithmetic(item, types, errors)


def _operand(node: dict, types: dict[str, str], errors: list[str]) -> None:
    if node.get("t") == "id":
        declared = types.get(node["name"])
        if declared in {"string", "boolean"}:
            errors.append(f'arithmetic on the {declared} field "{node["name"]}"')
    if node.get("t") in {"str", "bool"}:
        errors.append("arithmetic on a " + ("string" if node["t"] == "str" else "boolean") + " literal")


def duration_days(node: dict) -> int:
    """A duration is always a literal, so its day count is computed here rather than by emitted
    code. Weeks are days; months and years are not, since their length depends on the date."""
    if node.get("t") != "str":
        raise FeelError("duration() takes a string literal")
    match = re.match(r"^P(\d+)([DW])$", node["v"])
    if match is None:
        raise FeelError(f'unsupported duration "{node["v"]}" - use P<n>D or P<n>W')
    return int(match.group(1)) * (7 if match.group(2) == "W" else 1)


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
            # `validUntil + duration("P14D")` is a date shift, not a sum.
            if n["op"] in ("+", "-") and n["r"].get("fn") == "duration":
                sign = "" if n["op"] == "+" else "-"
                days = f"{sign}{duration_days(n['r']['args'][0])}"
                return (
                    f"(__import__('datetime').date.fromisoformat({emit(n['l'])})"
                    f" + __import__('datetime').timedelta(days={days})).isoformat()"
                )
            # FEEL yields null on a zero divisor and null in a predicate is false; nan carries
            # that, since every comparison against nan is False - and it avoids ZeroDivisionError.
            if n["op"] == "/":
                return f"(float('nan') if ({emit(n['r'])}) == 0 else {emit(n['l'])} / {emit(n['r'])})"
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
        if t == "neg":
            return f"-({emit(n['e'])})"
        if t == "cond":
            return f"(({emit(n['a'])}) if ({emit(n['c'])}) else ({emit(n['b'])}))"
        if t == "call":
            if n["fn"] in ("today", "now"):
                uses[n["fn"]] = True
                return n["fn"]
            args = [emit(a) for a in n["args"]]
            if n["fn"] == "date":
                # an ISO-8601 date is already this family's wire form, so date() only normalises it
                return f"__import__('datetime').date.fromisoformat({args[0]}).isoformat()"
            if n["fn"] == "duration":
                return str(duration_days(n["args"][0]))
            if n["fn"] == "starts with":
                return f"str({args[0]}).startswith(str({args[1]}))"
            if n["fn"] == "ends with":
                return f"str({args[0]}).endswith(str({args[1]}))"
            return f"str({args[1]}) in str({args[0]})"
        raise FeelError(f"cannot compile node {n!r}")

    expr = emit(node)
    return expr, uses["today"], uses["now"]
