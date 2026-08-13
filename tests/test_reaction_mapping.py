"""Extension proposal 0005: a declared mapping must reproduce the documented default exactly, and
must actually take effect when it differs - the first check alone would also pass if the
annotation were silently ignored."""

import re
from pathlib import Path

import pytest

from esdm2python.adapters.django.adapter import DjangoEventSourcingAdapter
from esdm2python.adapters.django_esdb.adapter import DjangoEventSourcingDbAdapter
from esdm2python.model import create_model, load_directory

MODEL_DIR = Path(__file__).resolve().parent.parent / "examples" / "manufacturing" / "model"
DEFAULT_MAPPING = "{ requestId: id, customerName: customerName, product: product, quantity: quantity }"
SWAPPED_MAPPING = "{ requestId: id, customerName: product, product: customerName, quantity: quantity }"

# Unique to the reaction document: only a policy carries deliveryGuarantee.
POLICY_ANCHOR = "scope:\n  domain: manufacturing\ndeliveryGuarantee:"

ADAPTERS = [DjangoEventSourcingAdapter, DjangoEventSourcingDbAdapter]


def _generate(tmp_path: Path, adapter_cls, mapping: str | None) -> dict[str, str]:
    work = tmp_path / (mapping or "plain").replace(" ", "")[:24]
    work.mkdir(parents=True, exist_ok=True)
    for file in sorted(MODEL_DIR.iterdir()):
        text = file.read_text()
        if file.name == "manufacturing.esdm.yaml":
            # Independent of whether the fixture itself declares a mapping: drop any, add ours.
            text = re.sub(r'\nmetadata:\n  annotations:\n    esdm-extensions\.io/mapping: "[^"]*"', "", text)
            if mapping is not None:
                text = text.replace(
                    POLICY_ANCHOR,
                    f'metadata:\n  annotations:\n    esdm-extensions.io/mapping: "{mapping}"\n' + POLICY_ANCHOR,
                )
        (work / file.name).write_text(text)

    model = create_model(load_directory(work))
    return adapter_cls().generate(model, {"appName": "manufacturing"}).files()


@pytest.mark.parametrize("adapter_cls", ADAPTERS)
def test_a_mapping_that_states_the_default_changes_nothing(tmp_path, adapter_cls):
    plain = _generate(tmp_path, adapter_cls, None)
    annotated = _generate(tmp_path, adapter_cls, DEFAULT_MAPPING)

    assert annotated == plain


@pytest.mark.parametrize("adapter_cls", ADAPTERS)
def test_a_different_mapping_reaches_the_emitted_reaction(tmp_path, adapter_cls):
    swapped = _generate(tmp_path, adapter_cls, SWAPPED_MAPPING)
    plain = _generate(tmp_path, adapter_cls, None)

    reactions = [path for path in plain if "reaction" in path or "polic" in path]
    assert reactions, "no reaction file was emitted"
    assert any(swapped[path] != plain[path] for path in reactions)


def test_the_example_model_still_carries_the_reaction_this_test_rewrites():
    model = create_model(load_directory(MODEL_DIR))

    assert [p.name for p in model.policies] == ["draft-quote-on-request"]
