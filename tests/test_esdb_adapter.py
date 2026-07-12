import json
from pathlib import Path

import pytest

from esdm2python.adapters.django import DjangoEventSourcingAdapter
from esdm2python.adapters.django_esdb import DjangoEventSourcingDbAdapter
from esdm2python.model import create_model, load_directory

MODEL_DIR = Path(__file__).resolve().parent.parent / "examples" / "todo" / "model"


@pytest.fixture(scope="module")
def model():
    return create_model(load_directory(MODEL_DIR))


@pytest.fixture(scope="module")
def files(model) -> dict[str, str]:
    return DjangoEventSourcingDbAdapter().generate(model, {}).files()


def test_expected_file_set(files):
    for path in [
        "config/settings.py", "config/urls.py", "config/wsgi.py",
        "tasks/domain.py", "tasks/application.py", "tasks/projections.py",
        "tasks/finders.py", "tasks/views.py", "tasks/urls.py",
        "shared/errors.py", "shared/cors.py", "shared/runtime.py",
        "shared/events.py", "shared/esdb.py", "shared/mongo.py",
        "shared/management/commands/observe.py",
        "dev/views.py", "dev/urls.py", "dev/catalog.json",
        "tests/test_task_lifecycle.py",
        "manage.py", "Dockerfile", "compose.yaml", "requirements.txt", "Makefile",
    ]:
        assert path in files, f"missing {path}"
    # no relational store in this target
    assert "tasks/models.py" not in files
    assert "tasks/migrations/0001_initial.py" not in files


def test_domain_pure_decide_evolve(files):
    domain = files["tasks/domain.py"]
    assert 'TASK_ADDED = "todo.task.task-added"' in domain    # CloudEvents type (family scheme)
    assert 'SUBJECT_ROOT = "/task"' in domain
    assert "class TaskState:" in domain and "@dataclass(frozen=True)" in domain
    assert 'OPEN = "open"' in domain and 'DELETED = "deleted"' in domain
    assert "def apply_event(state: TaskState, event: DomainEvent) -> TaskState:" in domain
    # delete evolves to the final state; admit guards decide
    assert "status=TaskState.DELETED," in domain
    assert '_admit("rename-task", state)' in domain
    assert "if state.status not in (TaskState.OPEN,):" in domain
    # delete carries current state into the event
    assert '"title": state.title,' in domain


def test_application_replay_decide_append(files):
    app = files["tasks/application.py"]
    assert "class TodoApp:" in app
    assert "def add_task(self, title: str) -> str:" in app
    assert "def rename_task(self, task_id: str, title: str) -> None:" in app
    # identity minting mirrors the Postgres target (uuid4)
    assert "domain.TaskState(id=str(uuid4()))" in app
    assert 'esdb.append(events, f"{domain.SUBJECT_ROOT}/{task.id}", pristine=True)' in app
    assert 'esdb.append(events, f"{domain.SUBJECT_ROOT}/{task.id}", pristine=False)' in app
    # per-subject replay + 404 seam
    assert 'for event in esdb.read(f"{domain.SUBJECT_ROOT}/{task_id}"):' in app
    assert "raise AggregateNotFound(task_id)" in app


def test_wire_envelope_matches_family_format(files):
    esdb = files["shared/esdb.py"]
    assert '"payload": event.data' in esdb
    assert '"nimbusMeta": {"correlationid": correlation_id}' in esdb
    assert "IsSubjectPristine" in esdb and "IsSubjectPopulated" in esdb
    # reads unwrap the envelope; foreign events count whole as payload
    assert '"payload" in data and "nimbusMeta" in data' in esdb


def test_projection_folds(files):
    proj = files["tasks/projections.py"]
    assert 'TASKS_COLLECTION = "rm_tasks"' in proj
    assert 'DELETED_TASKS_COLLECTION = "rm_deleted_tasks"' in proj
    assert 'create_index("id", unique=True)' in proj
    assert "revision = int(event.event_id)" in proj           # revision = ESDB event id
    assert "upsert=True," in proj                             # task-added insert
    assert 'rows.delete_one({"id": data.get("id")})' in proj  # task-deleted from live model
    assert "def project_deleted_tasks(event) -> None:" in proj
    assert "def tasks_lower_bound() -> str | None:" in proj   # observer resumes from max revision


def test_finders_strip_internals(files):
    finders = files["tasks/finders.py"]
    assert '_INTERNAL = {"_id": 0, "revision": 0}' in finders
    assert "def list_tasks() -> list[dict]:" in finders
    assert "def get_task(task_id: str) -> dict | None:" in finders
    assert '.sort("id", 1)' in finders


def test_views_same_http_semantics(files):
    views = files["tasks/views.py"]
    assert "status=409" in views
    assert "status=201" in views
    assert "AggregateNotFound" in views
    # eventually consistent: no projection call on the request path
    assert "projections.project" not in views


def test_observe_worker(files):
    observe = files["shared/management/commands/observe.py"]
    assert '("tasks", "/task", projections.project_tasks, projections.tasks_lower_bound),' in observe
    assert "Bound(last, BoundType.EXCLUSIVE)" in observe
    assert 'Bound("0", BoundType.INCLUSIVE)' in observe
    assert "connection.observe_events(subject, options)" in observe
    assert "POLICY_OBSERVERS = [" in observe


def test_dev_contract(files):
    dev = files["dev/views.py"]
    assert '"playhead": None' in dev                          # ESDB has no per-subject playhead
    assert '"todo.task.task-added": ("task", "task-added"),' in dev
    assert "rows.reverse()" in dev                            # newest first (0004 §4)


def test_catalog_identical_to_postgres_target(model, files):
    esdb_catalog = json.loads(files["dev/catalog.json"])
    pg_catalog = json.loads(DjangoEventSourcingAdapter().generate(model, {}).files()["dev/catalog.json"])
    assert esdb_catalog == pg_catalog


def test_compose_stack(files):
    compose = files["compose.yaml"]
    assert "thenativeweb/eventsourcingdb:1.2.0" in compose
    assert "--api-token=secret" in compose and "--data-directory-temporary" in compose
    assert "mongo:7" in compose
    assert '["python", "manage.py", "observe"]' in compose    # worker service
    assert "eventsourcingdb>=1.9,<2" in files["requirements.txt"]
    assert "pymongo" in files["requirements.txt"]


def test_policy_emission():
    documents = load_directory(MODEL_DIR)
    documents.append({
        "apiVersion": "schema.esdm.io/core/v1",
        "kind": "policy",
        "name": "reopen-on-delete",
        "handles": [{"boundedContext": "tasks", "aggregate": "task", "event": "task-deleted"}],
        "emits": [{"boundedContext": "tasks", "aggregate": "task", "command": "rename-task"}],
    })
    model = create_model(documents)
    files = DjangoEventSourcingDbAdapter().generate(model, {}).files()

    policies = files["policies.py"]
    assert "def reopen_on_delete_policy(event) -> None:" in policies
    assert "if event.type != domain.TASK_DELETED:" in policies
    # dispatches the emit command with the handle aggregate's id + carried fields
    assert 'get_app().rename_task(str(data.get("id", "")), str(data.get("title", "")))' in policies

    observe = files["shared/management/commands/observe.py"]
    assert '("reopen-on-delete", "/task", policies.reopen_on_delete_policy),' in observe
    assert "import policies" in observe
