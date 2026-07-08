from pathlib import Path

import pytest

from esdm2python.lifecycle import Lifecycle
from esdm2python.model import create_model, load_directory

MODEL_DIR = Path(__file__).resolve().parent.parent / "examples" / "todo" / "model"


def build():
    return create_model(load_directory(MODEL_DIR))


def test_domain_and_context():
    model = build()
    assert model.domain == "todo"
    assert [c.name for c in model.bounded_contexts] == ["tasks"]


def test_aggregate_identity_and_state():
    task = build().aggregate("tasks", "task")
    assert task.identity_field == "id"
    assert [f.name for f in task.state] == ["id", "title", "completed"]
    assert task.state.field("id").is_identity is True
    assert task.state.field("completed").is_identity is False
    assert [f.name for f in task.non_identity_state()] == ["title", "completed"]


def test_event_lifecycle_inherited_from_command():
    task = build().aggregate("tasks", "task")
    assert task.event("task-added").lifecycle is Lifecycle.CREATE
    assert task.event("task-deleted").lifecycle is Lifecycle.DELETE
    assert task.event("task-renamed").lifecycle is Lifecycle.MUTATE


def test_state_machine_transitions_survive_yaml_on_key():
    # regression: PyYAML 1.1 would parse the `on:` key as boolean True
    sm = build().aggregate("tasks", "task").state_machine
    assert sm.initial == "open"
    assert sm.state("deleted").final is True
    assert sm.transition_target("task-added") == "open"
    assert sm.transition_target("task-deleted") == "deleted"
    assert sm.admit_for("rename-task").from_states == ("open",)
    assert sm.admit_for("add-task") is None
    assert sm.admitting_states() == ("open",)


def test_read_models_and_queries():
    context = build().bounded_contexts[0]
    tasks = context.read_model("tasks")
    assert [c.name for c in tasks.columns] == ["id", "title", "completed"]
    assert tasks.primary_key() == "id"
    assert context.read_model("deleted-tasks").projects_event("task-deleted") is True
    kinds = {q.name: q.is_get() for q in context.queries}
    assert kinds == {"list-deleted-tasks": False, "list-tasks": False, "get-task": True}


def test_no_domain_document_raises():
    with pytest.raises(ValueError):
        create_model([{"kind": "bounded-context", "name": "x"}])
