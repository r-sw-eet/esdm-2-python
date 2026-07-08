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


class IllegalTransition(DomainViolation):
    """A command was issued from a state the aggregate does not admit it from (0001)."""

    def __init__(self, command: str, state: str) -> None:
        self.command = command
        self.state = state
        super().__init__(f'"{command}" is not allowed while "{state}"')


class GuardViolation(DomainViolation):
    """A command precondition (FEEL guard) was not met (0002)."""

    def __init__(self, command: str, requirement: str) -> None:
        self.command = command
        self.requirement = requirement
        super().__init__(f'"{command}" requires: {requirement}')
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
# migrate creates both the eventsourcing_django event store and the rm_* read models
CMD ["sh", "-c", "python manage.py migrate --noinput && python manage.py runserver 0.0.0.0:8000"]
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


def _run(app, fn):
    """Execute a command, project it, and map domain outcomes to HTTP."""
    try:
        result = fn()
    except DomainViolation as exc:
        return JsonResponse({"error": str(exc)}, status=409)
    except AggregateNotFound:
        return JsonResponse({"error": "not found"}, status=404)
    projections.project(app)
    return result'''

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
    if max_id:
        start = max(1, max_id - _LIMIT + 1)
        for notification in app.recorder.select_notifications(start, _LIMIT):
            rows.append(_row(app, notification))
        rows.reverse()  # newest first (0004 §4)
    return JsonResponse(rows, safe=False)


def _row(app, notification) -> dict:
    event = app.mapper.to_domain_event(notification)
    aggregate, name = _EVENTS.get(type(event), (%(fallback)s, type(event).__qualname__))
    payload = {"id": str(event.originator_id)}
    payload.update(
        {k: _jsonable(v) for k, v in event.__dict__.items() if k not in _FRAMEWORK_FIELDS}
    )
    return {
        "id": str(notification.id),
        "aggregate": aggregate,
        "aggregate_id": str(notification.originator_id),
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
