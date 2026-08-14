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


def test_membership_stays_membership():
    assert parse('status in ["a", "b"]')["t"] == "in"


def test_arithmetic_precedence_and_safe_division():
    assert parse("x = 1 + 2 * 3") == parse("x = 1 + (2 * 3)")
    assert parse("a - b > 1")["t"] == "bin"
    compiled, _, _ = compile_to_python(parse("total / count > 1"), lambda n: "self." + n)
    # a zero divisor must not raise: nan makes the comparison False, which is FEEL's null
    assert "nan" in compiled


def test_the_arithmetic_gate_rejects_what_the_amendment_says_it_should():
    types = {"amount": "number", "quantity": "integer", "status": "string"}
    allowed = {"amount", "quantity", "status"}

    assert validate(parse("amount * quantity >= 5000"), allowed, types) == []
    assert validate(parse("status * 2 > 1"), allowed, types) == ['arithmetic on the string field "status"']
    assert validate(parse("amount / 0 > 1"), allowed, types) == ["division by a literal zero"]


def test_conditionals_parse_bind_and_compile():
    e = "if quantity > 1 then amount * quantity >= 5000 else amount >= 99999"
    types = {"amount": "number", "quantity": "integer"}

    assert validate(parse(e), {"amount", "quantity"}, types) == []
    compiled, _, _ = compile_to_python(parse(e), lambda n: "self." + n)
    assert " if " in compiled and " else " in compiled

    with pytest.raises(FeelError):
        parse("if a then b")


def test_unary_minus_compiles():
    # the emitter called a name that does not exist in scope, so this raised at generation time
    compiled, _, _ = compile_to_python(parse("-amount > 1"), lambda n: "self." + n)

    assert compiled == "(-(self.amount) > 1)"


def test_calls_carry_arguments_and_their_arity_is_checked():
    assert validate(parse('contains(product, "c")'), {"product"}) == []
    assert validate(parse("starts with(product)"), {"product"}) == ["starts with takes 2 arguments, got 1"]


def test_date_arithmetic_is_a_shift_not_a_sum():
    compiled, _, _ = compile_to_python(parse('validUntil + duration("P2W") >= today()'), lambda n: "self." + n)

    # a duration is a literal, so its day count is computed at generation time: two weeks is 14
    assert "timedelta(days=14)" in compiled


def test_an_unsupported_duration_is_rejected():
    with pytest.raises(FeelError):
        compile_to_python(parse('validUntil + duration("P1M") >= today()'), lambda n: "self." + n)


def test_collections_and_paths_compile_and_scope():
    r = lambda n: "self." + n  # noqa: E731

    assert compile_to_python(parse("count(tags) >= 1"), r)[0] == "(len(self.tags or []) >= 1)"
    assert compile_to_python(parse("sum(amounts) > 100"), r)[0] == "(sum(self.amounts or []) > 100)"
    assert "all(" in compile_to_python(parse('every t in tags satisfies t != ""'), r)[0]
    assert compile_to_python(parse('customer.name = "ada"'), r)[0] == "((self.customer or {}).get('name') == 'ada')"

    # the quantifier variable is in scope for its predicate only
    assert validate(parse('every t in tags satisfies t != ""'), {"tags"}) == []
    assert validate(parse("some t in tags satisfies u = 1"), {"tags"}) == ['unknown field "u"']


def test_the_model_layer_keeps_nested_and_element_types():
    from esdm2python.model import Schema

    schema = Schema.from_raw({
        "properties": {
            "tags": {"type": "array", "items": {"type": "string"}},
            "customer": {"type": "object", "properties": {"name": {"type": "string"}}},
        }
    })

    assert schema.field("tags").element.json_type == "string"
    assert schema.field("customer").property_named("name").json_type == "string"
