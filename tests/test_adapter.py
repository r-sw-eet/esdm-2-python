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
    # admits are per-command, not the union of all commands' from-states
    assert '"rename-task": (OPEN,),' in domain
    assert "if self.status not in self._ADMITS[command]:" in domain


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
    # creates return 200 {id} — harmonized with the nimbus family (C4)
    assert 'JsonResponse({"id": result})' in views


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


# --- policy wiring + caller-supplied identity (metering mini model) ----------

_CORE = "schema.esdm.io/core/v1"


def _metering_documents() -> list[dict]:
    """Two contexts: `jobs` (primary, minted-identity source aggregate) and `billing`
    (caller-identity `usage` aggregate + read model), wired by one metering policy."""
    def scope(**kw):
        return {"domain": "metering", **kw}

    id_duration = {
        "type": "object",
        "properties": {"id": {"type": "string"}, "durationSeconds": {"type": "integer"}},
        "required": ["id", "durationSeconds"],
    }
    return [
        {"apiVersion": _CORE, "kind": "domain", "name": "metering"},
        {"apiVersion": _CORE, "kind": "bounded-context", "name": "jobs", "scope": scope()},
        {"apiVersion": _CORE, "kind": "bounded-context", "name": "billing", "scope": scope()},
        {
            "apiVersion": _CORE, "kind": "aggregate", "name": "job",
            "scope": scope(boundedContext="jobs"),
            "identifiedBy": {"source": "state", "field": "id"},
            "state": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "durationSeconds": {"type": "integer", "default": 0}},
                "required": ["id"],
            },
        },
        {
            "apiVersion": _CORE, "kind": "event", "name": "job-started",
            "scope": scope(boundedContext="jobs", aggregate="job"),
            "data": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
        },
        {
            "apiVersion": _CORE, "kind": "event", "name": "job-finished",
            "scope": scope(boundedContext="jobs", aggregate="job"),
            "data": id_duration,
        },
        {
            "apiVersion": _CORE, "kind": "command", "name": "start-job",
            "scope": scope(boundedContext="jobs", aggregate="job"),
            "publishes": ["job-started"],
        },
        {
            "apiVersion": _CORE, "kind": "command", "name": "finish-job",
            "scope": scope(boundedContext="jobs", aggregate="job"),
            "data": id_duration,
            "publishes": ["job-finished"],
        },
        {
            "apiVersion": _CORE, "kind": "aggregate", "name": "usage",
            "scope": scope(boundedContext="billing"),
            "identifiedBy": {"source": "state", "field": "id"},
            "state": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "durationSeconds": {"type": "integer", "default": 0}},
                "required": ["id", "durationSeconds"],
            },
        },
        {
            "apiVersion": _CORE, "kind": "event", "name": "usage-recorded",
            "scope": scope(boundedContext="billing", aggregate="usage"),
            "data": id_duration,
        },
        {
            # `record` is no create-verb, so pin create; declaring `id` in the data
            # makes the identity caller-supplied (usage.id = the source job's id)
            "apiVersion": _CORE, "kind": "command", "name": "record-usage",
            "metadata": {"annotations": {"esdm-extensions.io/lifecycle": "create"}},
            "scope": scope(boundedContext="billing", aggregate="usage"),
            "data": id_duration,
            "publishes": ["usage-recorded"],
        },
        {
            "apiVersion": _CORE, "kind": "read-model", "name": "usages",
            "scope": scope(boundedContext="billing"),
            "paradigm": "tabular",
            "schema": id_duration,
            "projections": [{
                "boundedContext": "billing", "aggregate": "usage",
                "event": "usage-recorded",
                "rule": "Insert a row with id and durationSeconds.",
            }],
        },
        {
            "apiVersion": _CORE, "kind": "query", "name": "list-usages",
            "scope": scope(boundedContext="billing"),
            "readModel": "usages",
            "result": {"type": "array", "items": {"type": "object"}},
        },
        {
            "apiVersion": _CORE, "kind": "policy", "name": "meter-finished-job",
            "scope": scope(),
            "handles": [{"boundedContext": "jobs", "aggregate": "job", "event": "job-finished"}],
            "emits": [{"boundedContext": "billing", "aggregate": "usage", "command": "record-usage"}],
        },
        {
            "apiVersion": "schema.esdm.io/given-when-then/v1", "kind": "feature",
            "name": "usage-metering",
            "scope": scope(boundedContext="billing", aggregate="usage"),
            "scenarios": [{
                "name": "record-usage-for-a-job",
                "when": {"command": "record-usage", "data": {"id": "job-1", "durationSeconds": 60}},
                "then": {"events": [{"event": "usage-recorded", "data": {"id": "job-1", "durationSeconds": 60}}]},
            }],
        },
    ]


@pytest.fixture(scope="module")
def metering_files() -> dict[str, str]:
    model = create_model(_metering_documents())
    return DjangoEventSourcingAdapter().generate(model, {}).files()


def test_reactions_cursor_dispatch_and_dedup(metering_files):
    reactions = metering_files["shared/reactions.py"]
    # the reactor's cursor lives in the first read-model context's position table
    assert "from billing.models import ProjectionPosition" in reactions
    assert 'POSITION = "__reactions__"' in reactions
    assert "ProjectionPosition.objects.select_for_update().get_or_create(name=POSITION)" in reactions
    # isinstance dispatch on the handle event class -> per-policy fn
    assert "if isinstance(event, Job.Finished):" in reactions
    assert "_meter_finished_job(app, event)" in reactions
    # a policy-minted create inherits the source aggregate's id (usage.id = job.id)
    assert "app.record_usage(str(event.originator_id), event.duration_seconds)" in reactions
    # savepoint per dispatch; a redelivered event dedups instead of crashing
    assert "with transaction.atomic():" in reactions
    assert "except IntegrityError:" in reactions


def test_views_react_before_project(metering_files, files):
    # billing has the read model: policies fire first so projections fold their
    # emitted events in the same request
    billing = metering_files["billing/views.py"]
    assert "from shared import reactions" in billing
    assert billing.index("reactions.react(app)") < billing.index("projections.project(app)")
    # jobs has aggregates but no read models: react still hooks in, and there is
    # no projections module to import or call
    jobs = metering_files["jobs/views.py"]
    assert "reactions.react(app)" in jobs
    assert "projections" not in jobs
    # a policy-free model gets neither the module nor the hook
    assert "shared/reactions.py" not in files
    assert "reactions" not in files["tasks/views.py"]


def test_caller_identity_domain(metering_files):
    billing = metering_files["billing/domain.py"]
    assert "from uuid import NAMESPACE_URL, UUID, uuid4, uuid5" in billing
    assert '_USAGE_NS = uuid5(NAMESPACE_URL, "billing/usage")' in billing
    # the model identity travels as a ctor param and is stored on the aggregate
    assert 'def __init__(self, duration_seconds: int, esdm_id: str = "") -> None:' in billing
    assert "self.esdm_id = esdm_id" in billing
    # deterministic stream id from the carried identity; minted when absent
    assert 'def create_id(cls, esdm_id: str = "", **_) -> UUID:' in billing
    assert "return uuid5(_USAGE_NS, esdm_id) if esdm_id else uuid4()" in billing
    # a minted-identity aggregate carries none of the machinery
    jobs = metering_files["jobs/domain.py"]
    assert "esdm_id" not in jobs and "create_id" not in jobs and "uuid5" not in jobs


def test_caller_identity_service(metering_files):
    app = metering_files["jobs/application.py"]  # primary context hosts the service
    # the id param sits in its declared cmd.data position (the command view passes ALL fields)
    assert "def record_usage(self, id: str, duration_seconds: int) -> str:" in app
    assert "usage = Usage(duration_seconds, esdm_id=id)" in app
    assert "return id or str(usage.id)" in app


def test_projection_keys_row_by_carried_identity(metering_files):
    proj = metering_files["billing/projections.py"]
    # getattr keeps replays of pre-carried-identity history working
    assert 'usage_id = getattr(event, "esdm_id", "") or usage_id' in proj
    assert "RmUsage.objects.update_or_create(" in proj
    assert "id=usage_id" in proj


def test_dev_events_map_carried_identity(metering_files):
    dev = metering_files["dev/views.py"]
    assert 'Usage.Recorded: ("usage", "usage-recorded", "esdm_id"),' in dev
    assert 'Job.Started: ("job", "job-started", None),' in dev
    assert 'Job.Finished: ("job", "job-finished", None),' in dev


def test_feature_test_imports_primary_context_app(metering_files):
    test = metering_files["tests/test_usage_metering.py"]
    # the application service lives in `jobs` (primary), the feature in `billing`
    assert "from jobs.application import MeteringApp" in test
    # a caller-identity create takes the id literal in its declared position
    assert 'usage_id = app.record_usage("job-1", 60)' in test


def test_reactor_is_at_least_once(metering_files):
    reactions = metering_files["shared/reactions.py"]
    # a failed dispatch reports False and the cursor stays BEFORE its event
    assert "def _dispatch(app, event) -> bool:" in reactions
    assert "def _meter_finished_job(app, event) -> bool:" in reactions
    assert "return False" in reactions
    assert "stalled = True" in reactions


def test_identity_param_dodges_model_field_collision():
    docs = _metering_documents()
    for doc in docs:
        if doc.get("name") in ("usage-recorded", "record-usage"):
            doc["data"]["properties"]["esdmId"] = {"type": "string", "default": ""}
    files = DjangoEventSourcingAdapter().generate(create_model(docs), {}).files()
    billing = files["billing/domain.py"]
    # the reserved param steps aside for a model field that snakes to esdm_id
    assert 'esdm_id_: str = ""' in billing
    assert "uuid5(_USAGE_NS, esdm_id_)" in billing
    compile(billing, "billing/domain.py", "exec")  # duplicate args would SyntaxError


def test_carried_identity_mutates_load_via_create_id_and_carry_the_id():
    docs = _metering_documents()
    docs += [
        {
            "apiVersion": _CORE, "kind": "event", "name": "usage-adjusted",
            "scope": {"domain": "metering", "boundedContext": "billing", "aggregate": "usage"},
            "data": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "durationSeconds": {"type": "integer"}},
                "required": ["id", "durationSeconds"],
            },
        },
        {
            "apiVersion": _CORE, "kind": "command", "name": "adjust-usage",
            "scope": {"domain": "metering", "boundedContext": "billing", "aggregate": "usage"},
            "data": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "durationSeconds": {"type": "integer"}},
                "required": ["id", "durationSeconds"],
            },
            "publishes": ["usage-adjusted"],
        },
    ]
    files = DjangoEventSourcingAdapter().generate(create_model(docs), {}).files()
    # composed/model ids resolve through create_id, never a raw UUID() parse
    app = files["jobs/application.py"]
    assert "usage: Usage = self.repository.get(Usage.create_id(esdm_id=usage_id))" in app
    assert "UUID(usage_id)" not in app
    # the mutate event carries the model identity so folds key rows by it
    billing = files["billing/domain.py"]
    assert 'def _adjusted(self, duration_seconds: int, esdm_id: str = "") -> None:' in billing
    assert "self._adjusted(duration_seconds, self.esdm_id)" in billing


def test_fold_maps_identity_columns_and_foreign_pk():
    # invoices shape: pk is NOT the aggregate identity, and a non-pk column IS it
    docs = _metering_documents()
    docs += [
        {
            "apiVersion": _CORE, "kind": "event", "name": "entry-logged",
            "scope": {"domain": "metering", "boundedContext": "billing", "aggregate": "usage"},
            "data": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "entryNumber": {"type": "string"}},
                "required": ["id", "entryNumber"],
            },
        },
        {
            "apiVersion": _CORE, "kind": "command", "name": "log-entry",
            "scope": {"domain": "metering", "boundedContext": "billing", "aggregate": "usage"},
            "data": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "entryNumber": {"type": "string"}},
                "required": ["id", "entryNumber"],
            },
            "publishes": ["entry-logged"],
        },
        {
            "apiVersion": _CORE, "kind": "read-model", "name": "ledger",
            "scope": {"domain": "metering", "boundedContext": "billing"},
            "paradigm": "tabular",
            "schema": {
                "type": "object",
                "properties": {"entryNumber": {"type": "string"}, "id": {"type": "string"}},
                "required": ["entryNumber", "id"],
            },
            "projections": [{
                "boundedContext": "billing", "aggregate": "usage",
                "event": "entry-logged",
                "rule": "Insert a ledger row keyed by entryNumber.",
            }],
        },
    ]
    proj = DjangoEventSourcingAdapter().generate(create_model(docs), {}).files()["billing/projections.py"]
    # the row key is the event's own pk field (snake-cased ORM kwarg), and the
    # identity column maps to the fold's id var - never a phantom event.id
    assert "entry_number=event.entry_number" in proj
    assert '"id": usage_id' in proj
    assert "event.id" not in proj


def test_reactions_without_read_models_require_carried_identity():
    docs = [
        d for d in _metering_documents()
        if d.get("kind") not in ("read-model", "query") and d.get("name") != "usage-metering"
    ]
    for doc in docs:
        # the builder shares one schema dict across docs; pop tolerates the rerun
        if doc.get("name") in ("usage-recorded", "record-usage"):
            doc["data"]["properties"].pop("id", None)
            doc["data"]["required"] = ["durationSeconds"]
    with pytest.raises(ValueError, match="meter-finished-job"):
        DjangoEventSourcingAdapter().generate(create_model(docs), {})
