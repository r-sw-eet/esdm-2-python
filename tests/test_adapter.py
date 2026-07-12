import json
from pathlib import Path

import pytest

from esdm2python.adapters.django import DjangoEventSourcingAdapter
from esdm2python.model import create_model, load_directory

MODEL_DIR = Path(__file__).resolve().parent.parent / "examples" / "todo" / "model"


@pytest.fixture(scope="module")
def files() -> dict[str, str]:
    model = create_model(load_directory(MODEL_DIR))
    return DjangoEventSourcingAdapter().generate(model, {}).files()


def test_expected_file_set(files):
    for path in [
        "config/settings.py", "config/urls.py", "config/wsgi.py",
        "tasks/domain.py", "tasks/application.py", "tasks/projections.py",
        "tasks/models.py", "tasks/finders.py", "tasks/views.py", "tasks/urls.py",
        "tasks/migrations/0001_initial.py",
        "shared/errors.py", "shared/cors.py", "shared/runtime.py",
        "shared/management/commands/eventstore_hashchain.py",
        "shared/management/commands/eventstore_verify.py",
        "dev/views.py", "dev/urls.py", "dev/catalog.json",
        "tests/test_task_lifecycle.py",
        "manage.py", "Dockerfile", "compose.yaml", "requirements.txt",
    ]:
        assert path in files, f"missing {path}"


def test_hash_chain_commands(files):
    install = files["shared/management/commands/eventstore_hashchain.py"]
    assert "CREATE EXTENSION IF NOT EXISTS pgcrypto;" in install
    assert "ADD COLUMN IF NOT EXISTS predecessor_hash TEXT NOT NULL DEFAULT ''" in install
    assert "pg_advisory_xact_lock(4711)" in install          # appends serialize -> linear chain
    assert "repeat('0', 64)" in install                      # genesis predecessor
    assert "BEFORE INSERT ON stored_events" in install
    assert "encode(NEW.state, 'hex')" in install             # bytea hashed deterministically

    verify = files["shared/management/commands/eventstore_verify.py"]
    assert "lag(hash) OVER (ORDER BY id)" in verify          # link check names the successor
    assert "WHERE hash <> '' AND (bad_hash OR bad_link)" in verify  # pre-chain rows skipped
    assert "raise CommandError" in verify                    # exit 1 with the first broken id

    # installed on boot, after migrate; `shared` is an app so manage.py finds the commands
    assert "manage.py migrate --noinput && python manage.py eventstore_hashchain" in files["Dockerfile"]
    assert '"shared",' in files["config/settings.py"]


def test_domain_decide_evolve(files):
    domain = files["tasks/domain.py"]
    assert 'class Task(Aggregate):' in domain
    assert 'OPEN = "open"' in domain and 'DELETED = "deleted"' in domain
    assert '@event("Added")' in domain and '@event("Deleted")' in domain
    # delete evolves to the final state; admit guards decide
    assert "self.status = self.DELETED" in domain
    assert 'self._admit("rename-task")' in domain
    assert "if self.status not in (self.OPEN,):" in domain


def test_application_methods(files):
    app = files["tasks/application.py"]
    assert "class TodoApp(Application):" in app
    assert "def add_task(self, title: str) -> str:" in app
    assert "def rename_task(self, task_id: str, title: str) -> None:" in app


def test_projection_folds(files):
    proj = files["tasks/projections.py"]
    assert 'PROJECTION = "tasks"' in proj
    assert "RmTask.objects.update_or_create(" in proj           # task-added insert
    assert "RmTask.objects.filter(id=task_id).update(title=event.title)" in proj
    assert "RmTask.objects.filter(id=task_id).delete()" in proj  # task-deleted from live model
    assert "RmDeletedTask.objects.update_or_create(" in proj     # task-deleted into archive


def test_read_model_tables(files):
    models = files["tasks/models.py"]
    assert 'db_table = "rm_tasks"' in models
    assert 'db_table = "rm_deleted_tasks"' in models
    assert "class RmTask(models.Model):" in models


def test_views_map_violation_to_409(files):
    views = files["tasks/views.py"]
    assert "status=409" in views
    assert "status=201" in views  # create


def test_catalog_contract(files):
    catalog = json.loads(files["dev/catalog.json"])
    assert catalog["domain"] == "todo"
    commands = {c["name"]: c for c in catalog["contexts"][0]["commands"]}
    assert commands["add-task"]["lifecycle"] == "create"
    assert commands["add-task"]["guard"] is None
    assert commands["delete-task"]["lifecycle"] == "delete"
    assert commands["rename-task"]["guard"] == {"from": ["open"], "when": None}
    # 0004 rule: finder row keys must equal the advertised read-model columns
    tasks_rm = next(r for r in catalog["contexts"][0]["readModels"] if r["name"] == "tasks")
    assert [c["name"] for c in tasks_rm["columns"]] == ["id", "title", "completed"]
