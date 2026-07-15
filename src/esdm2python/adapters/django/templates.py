"""Fixed (model-independent) files of the Django target.

These carry no per-model variation, so they are stored verbatim rather than
built from the model. Emitters for the model-driven files live in `adapter.py`.
Stored with single-quoted triple strings because the emitted code itself uses
double-quoted docstrings.
"""

from __future__ import annotations

WSGI = '''import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

application = get_wsgi_application()
'''

MANAGE = '''#!/usr/bin/env python
import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
'''

ERRORS = '''"""Domain-rule violations. Mapped to HTTP 409 at the edge (see `tasks/views.py`)."""

from __future__ import annotations


class DomainViolation(RuntimeError):
    """Base for domain-rule violations (state machine + guards)."""

    error_code = "DOMAIN_VIOLATION"

    def details(self) -> dict:
        return {"errorCode": self.error_code}


class IllegalTransition(DomainViolation):
    """A command was issued from a state the aggregate does not admit it from (0001)."""

    error_code = "ILLEGAL_TRANSITION"

    def __init__(self, command: str, state: str) -> None:
        self.command = command
        self.state = state or "undefined"
        super().__init__(f'{command} is not allowed while "{self.state}"')

    def details(self) -> dict:
        return {"errorCode": self.error_code, "command": self.command}


class GuardViolation(DomainViolation):
    """A command precondition (FEEL guard) was not met (0002)."""

    error_code = "GUARD_VIOLATION"

    def __init__(self, command: str, requirement: str) -> None:
        self.command = command
        self.requirement = requirement
        super().__init__(f"{command} requires: {requirement}")

    def details(self) -> dict:
        return {"errorCode": self.error_code, "command": self.command}
'''

CORS = '''"""Dev-stack CORS (0004 §5): let a domain console on another origin drive the
whole surface — domain routes and `/_dev/*`. Restrict or drop in production."""

from __future__ import annotations

from django.http import HttpResponse


class CorsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method == "OPTIONS":
            response = HttpResponse(status=204)
        else:
            response = self.get_response(request)
        response["Access-Control-Allow-Origin"] = "*"
        response["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response["Access-Control-Allow-Headers"] = "Content-Type"
        return response
'''

DEV_URLS = '''from django.urls import path

from dev import views

urlpatterns = [
    path("catalog", views.catalog, name="dev_catalog"),
    path("bpmn", views.bpmn, name="dev_bpmn"),
    path("events", views.events, name="dev_events"),
]
'''

CONFTEST = '''import sys
from pathlib import Path

# make the app packages (tasks, shared, ...) importable when running pytest
sys.path.insert(0, str(Path(__file__).resolve().parent))
'''

PYTEST_INI = '''[pytest]
testpaths = tests
'''

REQUIREMENTS = '''Django>=4.2,<5.2
eventsourcing>=9.2,<9.4
eventsourcing-django>=0.4,<0.5
psycopg[binary]>=3.1
'''

ENV_EXAMPLE = '''# Copy to .env for host runs (`python manage.py ...`). In docker compose the api
# service sets these itself; the published DB port is 5433 on the host.
APP_DEBUG=1
SECRET_KEY=dev-insecure-key-do-not-use-in-production
DB_NAME=app
DB_USER=app
DB_PASSWORD=app
DB_HOST=127.0.0.1
DB_PORT=5433
'''

DOCKERFILE = '''FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

EXPOSE 8000
# migrate creates the eventsourcing_django event store and the rm_* read models;
# eventstore_hashchain then arms the tamper-evidence trigger on stored_events
CMD ["sh", "-c", "python manage.py migrate --noinput && python manage.py eventstore_hashchain && python manage.py runserver 0.0.0.0:8000"]
'''

HASHCHAIN_COMMAND = '''"""Installs the in-database hash chain on the event store (idempotent).

Every stored event is chained to its predecessor, like EventSourcingDB's
predecessorhash: a BEFORE INSERT trigger hashes the row's immutable columns
together with the previous row's hash, so a later UPDATE, DELETE or reorder
breaks every hash after it. An advisory lock serializes appends, keeping the
chain linear under concurrency. Audit the log with `manage.py eventstore_verify`.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import connection

INSTALL_SQL = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;
ALTER TABLE stored_events ADD COLUMN IF NOT EXISTS predecessor_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE stored_events ADD COLUMN IF NOT EXISTS hash TEXT NOT NULL DEFAULT '';
CREATE OR REPLACE FUNCTION stored_events_hash_chain() RETURNS trigger AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(4711);
    SELECT hash INTO NEW.predecessor_hash FROM stored_events ORDER BY id DESC LIMIT 1;
    NEW.predecessor_hash := COALESCE(NEW.predecessor_hash, repeat('0', 64));
    NEW.hash := encode(digest(jsonb_build_array(
        NEW.predecessor_hash, NEW.id, NEW.application_name, NEW.originator_id::text,
        NEW.originator_version, NEW.topic, encode(NEW.state, 'hex')
    )::text, 'sha256'), 'hex');
    RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE OR REPLACE TRIGGER stored_events_hash_chain
    BEFORE INSERT ON stored_events
    FOR EACH ROW EXECUTE FUNCTION stored_events_hash_chain();
"""


class Command(BaseCommand):
    help = "Install the tamper-evidence hash chain on the stored_events table (idempotent)."

    def handle(self, *args, **options):
        with connection.cursor() as cursor:
            cursor.execute(INSTALL_SQL)
        self.stdout.write("hash chain installed on stored_events")
'''

VERIFY_COMMAND = '''"""Verifies the event-store hash chain in pure SQL.

Recomputes every row's hash and checks every predecessor link (a deleted or
reordered event breaks the link on its successor). Rows stored before the
chain was installed (empty hash) are skipped. Exits 0 when the log is intact,
exits 1 naming the first broken event id.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

VERIFY_SQL = """
SELECT min(id) FROM (
    SELECT id, hash,
        hash <> encode(digest(jsonb_build_array(
            predecessor_hash, id, application_name, originator_id::text,
            originator_version, topic, encode(state, 'hex')
        )::text, 'sha256'), 'hex') AS bad_hash,
        predecessor_hash <> COALESCE(lag(hash) OVER (ORDER BY id), repeat('0', 64)) AS bad_link
    FROM stored_events
) checked
WHERE hash <> '' AND (bad_hash OR bad_link)
"""


class Command(BaseCommand):
    help = "Verify the event-store hash chain; exit 1 at the first broken event."

    def handle(self, *args, **options):
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM stored_events")
            total = cursor.fetchone()[0]
            cursor.execute(VERIFY_SQL)
            broken_at = cursor.fetchone()[0]
        if broken_at is not None:
            raise CommandError(f"event chain broken at id {broken_at}")
        self.stdout.write(f"event chain intact ({total} events)")
'''

COMPOSE = '''# Generated stack: db (PostgreSQL) + api (write/read HTTP with synchronous projection).
# No async projection worker: this target projects on the request path (pull-based over
# the notification log), so reads are consistent with the caller's own writes.
services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: app
      POSTGRES_PASSWORD: app
      POSTGRES_DB: app
    ports:
      - "5433:5432"
    volumes:
      - db-data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U app -d app"]
      interval: 3s
      timeout: 3s
      retries: 20

  api:
    build: .
    environment:
      APP_DEBUG: "1"
      DB_HOST: db
      DB_PORT: "5432"
      DB_NAME: app
      DB_USER: app
      DB_PASSWORD: app
    depends_on:
      db:
        condition: service_healthy
    ports:
      - "8080:8000"

# Domain console: this stack serves the 0004 dev contract (/_dev/*) — point the
# esdm-vue-reader viewer at http://localhost:8080 for commands / read models / events.

volumes:
  db-data:
'''

MAKEFILE = '''.PHONY: up down logs api-logs migrate test

up:
\tdocker compose up -d --build

down:
\tdocker compose down -v

logs:
\tdocker compose logs -f

api-logs:
\tdocker compose logs -f api

migrate:
\tdocker compose exec api python manage.py migrate --noinput

test:
\tpython -m pytest -q
'''

# --- fixed fragments spliced into model-driven files -------------------------

PROJECT_FN = '''def project(app) -> None:
    with transaction.atomic():
        cursor, _ = ProjectionPosition.objects.select_for_update().get_or_create(name=PROJECTION)
        last = cursor.last_id
        while True:
            notifications = app.recorder.select_notifications(last + 1, _BATCH)
            fresh = [n for n in notifications if n.id > last]
            for notification in fresh:
                _fold(app.mapper.to_domain_event(notification))
                last = notification.id
            if len(notifications) < _BATCH:
                break
        if last != cursor.last_id:
            cursor.last_id = last
            cursor.save(update_fields=["last_id"])'''

REACT_FN = '''def react(app) -> None:
    with transaction.atomic():
        cursor, _ = ProjectionPosition.objects.select_for_update().get_or_create(name=POSITION)
        last = cursor.last_id
        stalled = False
        while not stalled:
            notifications = app.recorder.select_notifications(last + 1, _BATCH)
            fresh = [n for n in notifications if n.id > last]
            if not fresh:
                break
            for notification in fresh:
                if not _dispatch(app, app.mapper.to_domain_event(notification)):
                    # at-least-once: leave the cursor BEFORE the failed event so the
                    # next request retries it (dedup makes the retry idempotent)
                    stalled = True
                    break
                last = notification.id
        if last != cursor.last_id:
            cursor.last_id = last
            cursor.save(update_fields=["last_id"])'''

REACT_FN_NO_CURSOR = '''def react(app) -> None:
    # no read models -> no cursor table; rescan and rely on deterministic-id dedup
    last = 0
    while True:
        notifications = app.recorder.select_notifications(last + 1, _BATCH)
        fresh = [n for n in notifications if n.id > last]
        if not fresh:
            break
        for notification in fresh:
            if not _dispatch(app, app.mapper.to_domain_event(notification)):
                return  # retried on the next call
            last = notification.id'''

VIEWS_HELPERS = '''try:  # exception name differs across eventsourcing releases
    from eventsourcing.application import AggregateNotFound
except ImportError:  # pragma: no cover
    from eventsourcing.application import AggregateNotFoundError as AggregateNotFound


def _payload(request) -> dict:
    body = request.body.decode() or "{}"
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _run(app, fn, command):
    """Execute a command, project it, and map domain outcomes to HTTP (nimbus envelope)."""
    try:
        result = fn()
    except DomainViolation as exc:
        return JsonResponse({"error": "CONFLICT", "message": str(exc), "details": exc.details()}, status=409)
    except AggregateNotFound:
        # unknown aggregate on a mutate: same wire behavior as the nimbus family
        return JsonResponse(
            {"error": "CONFLICT", "message": f'{command} is not allowed while "undefined"',
             "details": {"errorCode": "ILLEGAL_TRANSITION", "command": command}},
            status=409,
        )
%(react)s%(project)s    return result'''

DEV_VIEWS_TAIL = '''_FRAMEWORK_FIELDS = {"originator_id", "originator_version", "originator_topic", "timestamp"}
_LIMIT = 50


def catalog(request):
    return HttpResponse((_HERE / "catalog.json").read_bytes(), content_type="application/json")


def bpmn(request):
    return HttpResponse((_HERE / "source.bpmn").read_bytes(), content_type="application/xml")


def events(request):
    app = get_app()
    max_id = app.recorder.max_notification_id() or 0
    rows: list[dict] = []
    end = max_id
    # walk backwards in chunks: notification ids may have gaps (rolled-back writes),
    # and the window is "the newest <= _LIMIT rows", not "the newest _LIMIT ids"
    while end >= 1 and len(rows) < _LIMIT:
        start = max(1, end - _LIMIT + 1)
        batch = [n for n in app.recorder.select_notifications(start, end - start + 1) if n.id <= end]
        for notification in reversed(batch):
            rows.append(_row(app, notification))
            if len(rows) >= _LIMIT:
                break
        end = start - 1
    return JsonResponse(rows, safe=False)  # newest first (0004 §4)


def _row(app, notification) -> dict:
    event = app.mapper.to_domain_event(notification)
    aggregate, name, id_attr = _EVENTS.get(type(event), (%(fallback)s, type(event).__qualname__, None))
    payload = {"id": str(event.originator_id)}
    payload.update(
        {k: _jsonable(v) for k, v in event.__dict__.items() if k not in _FRAMEWORK_FIELDS}
    )
    aggregate_id = str(notification.originator_id)
    if id_attr:
        # the wire identity is the model's own (e.g. a usage keyed by its job's id);
        # the derived stream id stays store bookkeeping
        carried = payload.pop(id_attr, "")
        if carried:
            payload["id"] = carried
            aggregate_id = carried
    return {
        "id": str(notification.id),
        "aggregate": aggregate,
        "aggregate_id": aggregate_id,
        "playhead": notification.originator_version,
        "event": name,
        "payload": payload,
        "recorded_on": event.timestamp.isoformat(),
    }


def _jsonable(value):
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)'''
