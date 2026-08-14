import pytest

from esdm2python.feel import FeelError, compile_to_python, parse, validate


def _py(source: str) -> str:
    expr, _today, _now = compile_to_python(parse(source), lambda name: f"self.{name}")
    return expr


def test_comparison_and_equality():
    assert _py("paidAmount >= total") == "(self.paidAmount >= self.total)"
    assert _py('status = "sent"') == "(self.status == 'sent')"


def test_boolean_and_membership():
    assert _py("a and b or c") == "((self.a and self.b) or self.c)"
    assert _py('status in ["sent", "draft"]') == "(self.status in ['sent', 'draft'])"
    assert _py("not (done)") == "(not (self.done))"


def test_clock_calls_flag_usage():
    expr, uses_today, uses_now = compile_to_python(parse("validUntil >= today()"), lambda n: f"self.{n}")
    assert expr == "(self.validUntil >= today)"
    assert uses_today is True and uses_now is False


def test_validate_reports_unknown_fields():
    assert validate(parse("total >= paid"), {"total", "paid"}) == []
    assert validate(parse("total >= mystery"), {"total"}) == ['unknown field "mystery"']


def test_malformed_raises():
    with pytest.raises(FeelError):
        parse("a >= ")

def test_null_is_a_literal_not_a_field_name():
    ast = parse("cancelledAt = null")

    assert ast == {"t": "bin", "op": "=", "l": {"t": "id", "name": "cancelledAt"}, "r": {"t": "null"}}
    # `null` used to lex as a name, so this reported: unknown field "null".
    assert validate(ast, {"cancelledAt"}) == []


def test_a_negative_literal_folds_so_the_emitted_code_reads_naturally():
    assert parse("amount > -1")["r"] == {"t": "num", "v": -1}


def test_between_and_ranges_desugar_into_two_comparisons():
    expected = parse("qty >= 1 and qty <= 10")

    assert parse("qty between 1 and 10") == expected
    assert parse("qty in [1..10]") == expected


def test_membership_stays_membership_and_binary_minus_waits_for_arithmetic():
    assert parse('status in ["a", "b"]')["t"] == "in"
    with pytest.raises(FeelError):
        parse("a - b")
