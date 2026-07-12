"""Fixed (model-independent) files of the Django + EventSourcingDB target.

These carry no per-model variation, so they are stored verbatim rather than
built from the model. Emitters for the model-driven files live in `adapter.py`.
Stored with single-quoted triple strings because the emitted code itself uses
double-quoted docstrings.
"""

from __future__ import annotations

ERRORS = '''"""Domain-rule violations and lookup misses, mapped to HTTP at the edge:
DomainViolation -> 409, AggregateNotFound -> 404."""

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


class AggregateNotFound(RuntimeError):
    """No events exist for the requested aggregate id."""

    def __init__(self, aggregate_id: str) -> None:
        self.aggregate_id = aggregate_id
        super().__init__(f"aggregate {aggregate_id} not found")
'''

EVENTS = '''"""The store-neutral domain event the write side folds and emits: a CloudEvents
type (`<domain>.<aggregate>.<event>`) plus its payload."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DomainEvent:
    type: str
    data: dict = field(default_factory=dict)
'''

MONGO = '''"""MongoDB connection + database handle for the read side (`rm_*` collections)."""

from __future__ import annotations

import os

from pymongo import MongoClient

_client: MongoClient | None = None


def get_db():
    global _client
    if _client is None:
        _client = MongoClient(os.environ.get("MONGO_URL", "mongodb://mongo:27017"))
    return _client[os.environ.get("MONGO_DB", "app")]
'''

# %(source)s: the CloudEvents source of every event this app appends.
ESDB = '''"""EventSourcingDB access: a small sync facade over the official async SDK.

Events go out in the family's shared envelope - `data = { payload, nimbusMeta }`
with the CloudEvents type `<domain>.<aggregate>.<event>` and the subject
`/<aggregate>/<id>` - so stores written by any codegen of the family stay
interchangeable.
"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

from eventsourcingdb import (
    Client,
    EventCandidate,
    IsSubjectPopulated,
    IsSubjectPristine,
    ReadEventsOptions,
)

from shared.events import DomainEvent

SOURCE = "%(source)s"


def client() -> Client:
    url = os.environ.get("ESDB_URL", "http://esdb:3000")
    api_token = os.environ.get("ESDB_API_TOKEN", "secret")
    return Client(url, api_token)


def payload_of(event) -> dict:
    """Unwrap the family envelope; foreign events count whole as payload."""
    data = event.data
    if isinstance(data, dict) and "payload" in data and "nimbusMeta" in data:
        payload = data["payload"]
        return payload if isinstance(payload, dict) else {}
    return data if isinstance(data, dict) else {}


def append(events: list[DomainEvent], subject: str, pristine: bool) -> None:
    """Append a command's events under its subject, guarded by a precondition."""
    correlation_id = str(uuid4())
    candidates = [
        EventCandidate(
            source=SOURCE,
            subject=subject,
            type=event.type,
            data={"payload": event.data, "nimbusMeta": {"correlationid": correlation_id}},
        )
        for event in events
    ]
    precondition = IsSubjectPristine(subject) if pristine else IsSubjectPopulated(subject)

    async def _append() -> None:
        async with client() as connection:
            await connection.write_events(candidates, [precondition])

    asyncio.run(_append())


def read(subject: str) -> list[DomainEvent]:
    """The subject's events (oldest first), unwrapped for the fold."""

    async def _read() -> list[DomainEvent]:
        events: list[DomainEvent] = []
        async with client() as connection:
            async for event in connection.read_events(subject, ReadEventsOptions(recursive=False)):
                events.append(DomainEvent(event.type, payload_of(event)))
        return events

    return asyncio.run(_read())


def read_all() -> list:
    """Every event in the store (oldest first), raw - the dev window uses this."""

    async def _read() -> list:
        events: list = []
        async with client() as connection:
            async for event in connection.read_events("/", ReadEventsOptions(recursive=True)):
                events.append(event)
        return events

    return asyncio.run(_read())
'''

DEV_VIEWS_TAIL = '''
_LIMIT = 50


def catalog(request):
    return HttpResponse((_HERE / "catalog.json").read_bytes(), content_type="application/json")


def bpmn(request):
    return HttpResponse((_HERE / "source.bpmn").read_bytes(), content_type="application/xml")


def events(request):
    rows: list[dict] = []
    for event in esdb.read_all():
        segments = [s for s in event.subject.split("/") if s]
        aggregate, name = _EVENTS.get(event.type, (segments[0] if segments else "", event.type))
        rows.append({
            "id": event.event_id,
            "aggregate": aggregate,
            "aggregate_id": segments[-1] if segments else "",
            "playhead": None,  # EventSourcingDB has no per-subject playhead (0004)
            "event": name,
            "payload": esdb.payload_of(event),
            "recorded_on": event.time.isoformat(),
        })
        if len(rows) > _LIMIT:
            rows.pop(0)
    rows.reverse()  # newest first (0004 §4)
    return JsonResponse(rows, safe=False)'''

OBSERVE_TAIL = '''

async def _run() -> None:
    await _wait_for_store()
    watchers = [
        _observe(name, subject, handler, lower_bound)
        for name, subject, handler, lower_bound in OBSERVERS
    ] + [
        _observe(name, subject, handler, None)
        for name, subject, handler in POLICY_OBSERVERS
    ]
    await asyncio.gather(*watchers)


async def _wait_for_store() -> None:
    for attempt in range(1, 61):
        try:
            async with esdb.client() as connection:
                await connection.ping()
            return
        except Exception:
            print(f"waiting for EventSourcingDB ({attempt})...", flush=True)
            await asyncio.sleep(2)
    raise RuntimeError("EventSourcingDB did not become ready in time")


async def _observe(name: str, subject: str, handler, lower_bound) -> None:
    while True:
        options = ObserveEventsOptions(recursive=True)
        if lower_bound is not None:
            last = lower_bound()
            options.lower_bound = (
                Bound(last, BoundType.EXCLUSIVE)
                if last is not None
                else Bound("0", BoundType.INCLUSIVE)
            )
        try:
            async with esdb.client() as connection:
                async for event in connection.observe_events(subject, options):
                    handler(event)
        except Exception as error:
            print(f"observer {name} failed ({error}); reconnecting...", flush=True)
            await asyncio.sleep(3)'''

VIEWS_HELPERS = '''def _payload(request) -> dict:
    body = request.body.decode() or "{}"
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _run(app, fn):
    """Execute a command and map domain outcomes to HTTP."""
    try:
        return fn()
    except DomainViolation as exc:
        return JsonResponse({"error": str(exc)}, status=409)
    except AggregateNotFound:
        return JsonResponse({"error": "not found"}, status=404)'''

REQUIREMENTS = '''Django>=4.2,<5.2
eventsourcingdb>=1.9,<2
pymongo>=4.6,<5
'''

ENV_EXAMPLE = '''# Copy to .env for host runs (`python manage.py ...`). In docker compose the
# services set these themselves; the published host ports are esdb 3000 and
# mongo 27018.
APP_DEBUG=1
SECRET_KEY=dev-insecure-key-do-not-use-in-production
ESDB_URL=http://127.0.0.1:3000
ESDB_API_TOKEN=secret
MONGO_URL=mongodb://127.0.0.1:27018
MONGO_DB=app
'''

DOCKERFILE = '''FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

EXPOSE 8000
# no migrate: events live in EventSourcingDB, read models in MongoDB
CMD ["sh", "-c", "python manage.py runserver 0.0.0.0:8000"]
'''

COMPOSE = '''# Generated stack: esdb (EventSourcingDB event store + UI), mongo (read models),
# api (Django HTTP), worker (`manage.py observe`: projections + policies).
# Reads are eventually consistent - the worker tails the store and folds events
# into the rm_* collections the queries serve.
services:
  esdb:
    image: thenativeweb/eventsourcingdb:1.2.0
    command:
      - run
      - --api-token=secret
      - --data-directory-temporary
      - --http-enabled
      - --https-enabled=false
      - --with-ui
    ports:
      - "3000:3000"

  mongo:
    image: mongo:7
    ports:
      - "27018:27017"
    volumes:
      - mongo-data:/data/db

  api:
    build: .
    environment:
      APP_DEBUG: "1"
      ESDB_URL: "http://esdb:3000"
      ESDB_API_TOKEN: secret
      MONGO_URL: "mongodb://mongo:27017"
      MONGO_DB: app
    depends_on:
      - esdb
      - mongo
    ports:
      - "8080:8000"

  worker:
    build: .
    command: ["python", "manage.py", "observe"]
    environment:
      ESDB_URL: "http://esdb:3000"
      ESDB_API_TOKEN: secret
      MONGO_URL: "mongodb://mongo:27017"
      MONGO_DB: app
    depends_on:
      - esdb
      - mongo

# Domain console: this stack serves the 0004 dev contract (/_dev/*) — point the
# esdm-vue-reader viewer at http://localhost:8080 for commands / read models / events.

volumes:
  mongo-data:
'''

MAKEFILE = '''.PHONY: up down logs api-logs worker-logs test

up:
\tdocker compose up -d --build

down:
\tdocker compose down -v

logs:
\tdocker compose logs -f

api-logs:
\tdocker compose logs -f api

worker-logs:
\tdocker compose logs -f worker

test:
\tpython -m pytest -q
'''
