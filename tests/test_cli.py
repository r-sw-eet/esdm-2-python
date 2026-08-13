import json

from esdm2python.cli import main
from esdm2python.project import AdapterRegistry


def test_targets_json_describes_every_registered_target(capsys):
    """The contract esdm-studio reads to populate its target picker."""
    assert main(["targets", "--json"]) == 0

    parsed = json.loads(capsys.readouterr().out)
    adapters = AdapterRegistry.with_defaults().all()

    assert len(parsed) == len(adapters)
    for entry, adapter in zip(parsed, adapters):
        assert sorted(entry) == ["description", "name", "slug"]
        assert entry["name"] == adapter.name()
        assert entry["description"] == adapter.description()
        assert entry["slug"] == adapter.slug()
