"""JSON-Schema type -> Python / Django-field mappings for the Django target."""

from __future__ import annotations

from ...model import Field

_PY_TYPE = {"string": "str", "boolean": "bool", "integer": "int", "number": "float"}
_ZERO = {"string": '""', "boolean": "False", "integer": "0", "number": "0.0"}


def py_type(field: Field) -> str:
    return _PY_TYPE.get(field.json_type, "object")


def zero_literal(field: Field) -> str:
    return _ZERO.get(field.json_type, "None")


def default_literal(field: Field) -> str:
    """The value an evolve/insert uses when the event does not carry the field."""
    if field.has_default:
        return repr(field.default)
    return zero_literal(field)


def is_mutable_default(field: Field) -> bool:
    """A dataclass field defaulting to a mutable container needs a default_factory."""
    return field.has_default and isinstance(field.default, (list, dict, set))


def dataclass_default(field: Field) -> str:
    """Default expression for a @dataclass field. A bare mutable literal (list/dict/set)
    is a ValueError at class definition, so route it through default_factory."""
    if not is_mutable_default(field):
        return default_literal(field)
    factory = {list: "list", dict: "dict", set: "set"}[type(field.default)]
    if not field.default:
        return f"field(default_factory={factory})"
    return f"field(default_factory=lambda: {default_literal(field)})"


def coerce_payload(field: Field, expr: str) -> str:
    """Wrap an HTTP-payload lookup in the right Python coercion."""
    caster = _PY_TYPE.get(field.json_type)
    return f"{caster}({expr})" if caster else expr


def model_field(field: Field, is_pk: bool) -> str:
    """A `models.*` field expression for models.py."""
    if field.json_type == "boolean":
        default = repr(field.default) if field.has_default else "False"
        return f"models.BooleanField(default={default})"
    if field.json_type == "integer":
        return "models.BigIntegerField(default=0)" if is_pk else "models.IntegerField()"
    if field.json_type == "number":
        return "models.FloatField()"
    if is_pk:
        return "models.CharField(primary_key=True, max_length=255)"
    return "models.CharField(max_length=255)"


def migration_field(field: Field, is_pk: bool) -> str:
    """A `models.*` field expression as `makemigrations` renders it (kwargs sorted)."""
    if field.json_type == "boolean":
        default = repr(field.default) if field.has_default else "False"
        return f"models.BooleanField(default={default})"
    if field.json_type == "integer":
        if is_pk:
            return "models.BigIntegerField(default=0, primary_key=True, serialize=False)"
        return "models.IntegerField()"
    if field.json_type == "number":
        return "models.FloatField()"
    if is_pk:
        return "models.CharField(max_length=255, primary_key=True, serialize=False)"
    return "models.CharField(max_length=255)"
