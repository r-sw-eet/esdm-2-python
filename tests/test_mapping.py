import pytest

from esdm2python.feel import FeelError
from esdm2python.mapping import parse, validate

# The reaction payload mapping of extension proposal 0005.


def test_parses_entries_in_author_order():
    mapping = parse("{ requestId: id, product: product }")

    assert list(mapping) == ["requestId", "product"]
    assert mapping["requestId"]["name"] == "id"


def test_keeps_commas_inside_a_nested_expression():
    mapping = parse('{ tier: status in ["gold", "silver"], id: id }')

    assert list(mapping) == ["tier", "id"]
    assert mapping["tier"]["t"] == "in"


def test_binds_values_against_the_handled_event_fields():
    mapping = parse("{ requestId: id, name: customerName }")

    assert validate(mapping, {"id", "customerName"}) == []
    assert validate(mapping, {"id"}) == ['name: unknown field "customerName"']


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("requestId: id", "context literal"),
        ("{ }", "at least one field"),
        ("{ id }", "key: expression"),
        ("{ id: id, id: product }", "twice"),
    ],
)
def test_rejects_malformed_contexts(source, message):
    with pytest.raises(FeelError, match=message):
        parse(source)
