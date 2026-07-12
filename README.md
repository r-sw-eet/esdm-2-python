# esdm-2-python

The **Python codegen** of the BPAG family: it turns a business-process / domain model —
authored as **BPMN** or as an [ESDM](https://www.esdm.io/) model (Event-Sourced Domain
Modeling — YAML documents describing an event-sourced domain) — into a **real, runnable
application**. It emits **Django** apps that implement the model with **CQRS**, an
**event-driven read side** and **event sourcing**, in one of two flavors:

| Target                          | Event store                                                                                                               | Read models                | Projection style                                   |
|---------------------------------|---------------------------------------------------------------------------------------------------------------------------|----------------------------|----------------------------------------------------|
| `django-eventsourcing-postgres` | PostgreSQL via [`eventsourcing`](https://github.com/pyeventsourcing/eventsourcing) + `eventsourcing-django`, hash-chained | Django-ORM `rm_*` tables   | pull-based, on the request path (read-your-writes) |
| `django-eventsourcingdb`        | [EventSourcingDB](https://www.eventsourcingdb.io/) via the official `eventsourcingdb` SDK                                 | MongoDB `rm_*` collections | `observe` worker process (eventually consistent)   |

Both serve the **same HTTP surface** (`POST /{context}/{command}`, `GET /{context}/{query}`,
409 on rejected domain rules, the 0004 `/_dev/*` dev contract), so a client can't tell which
backend is behind it. The EventSourcingDB target writes the family's shared wire envelope
(`data = { payload, nimbusMeta }`, type `<domain>.<aggregate>.<event>`, subject
`/<aggregate>/<id>`), so its store is interchangeable with the sibling codegens'. The
PostgreSQL target hash-chains its event log in-database (`manage.py eventstore_hashchain` /
`eventstore_verify`) for tamper evidence.

It is the sibling of `esdm-2-symfony` (PHP → Symfony +
`patchlevel/event-sourcing`) and `esdm-2-nimbus` (TypeScript → Nimbus): **same model in, an
equivalent event-sourced app out**, in a different stack.

> Draw the business process. BPAG makes it run.

## Status

**Working codegen.** The generator — model parser + the two Django adapters + CLI — lives under
[`src/esdm2python/`](src/esdm2python/). Point it at an ESDM model and it emits a complete, runnable
Django app:

```sh
python -m esdm2python.cli generate examples/todo   # default target -> examples/todo/generated/python/
python -m esdm2python.cli generate examples/todo --target django-eventsourcingdb
                                                   # -> examples/todo/generated/python-esdb/
python -m esdm2python.cli targets                  # list adapter targets
```

The pipeline mirrors the sibling generators (`esdm-2-symfony`, `esdm-2-nimbus`): **load the ESDM
YAML → build a stack-neutral typed model → FEEL gate → the chosen adapter emits an in-memory file
tree → write to disk**. Everything upstream of the adapters is framework-agnostic; only the
adapters know their target stack. Generated output is disposable (gitignored) — regenerate it,
never edit by hand.

Verified by generator unit tests (`pytest`) plus a smoke gate (`scripts/conformance.sh`, every
example × every target); the emitted `todo` app's own write-side GWT tests pass, and both full
stacks (Django + PostgreSQL; Django + EventSourcingDB + MongoDB) boot and serve the domain
surface + 0004 contract under `docker compose`.

## The two targets

**`django-eventsourcing-postgres`** (default) is the single-database flavor: the event store is
a Django-ORM table (`stored_events`) via `eventsourcing-django`, projections run synchronously
on the request path (read-your-writes), and the whole app needs only PostgreSQL. Its event log
is **tamper-evident**: `manage.py eventstore_hashchain` (run on boot) installs a `BEFORE INSERT`
trigger that hash-chains every event to its predecessor in-database (appends serialized by an
advisory lock), and `manage.py eventstore_verify` re-computes the chain in pure SQL — exit 0
when intact, exit 1 naming the first broken event id.

**`django-eventsourcingdb`** is the dedicated-event-store flavor: commands replay their subject
from **EventSourcingDB** (official `eventsourcingdb` SDK; preconditions `isSubjectPristine` /
`isSubjectPopulated` guard concurrency), fold pure state and append new events; a long-running
`manage.py observe` worker (its own compose service) tails the store and folds events into
**MongoDB** `rm_*` collections — resuming from each read model's max stored `revision` — and
runs the model's policies. EventSourcingDB brings its own hash chain (`predecessorhash`).

## Why `eventsourcing`

Each BPAG target picks the mature, idiomatic event-sourcing **runtime** of its language and emits
code against it — the codegen orchestrates, it does not reimplement event sourcing. `eventsourcing`
is the Python analog of Symfony's `patchlevel/event-sourcing`: DDD-first, persistence-agnostic,
native PostgreSQL, with aggregates, an application/command layer, projections and snapshots. The
ESDM concepts map onto it almost 1:1:

| ESDM concept         | esdm-2-symfony (patchlevel) | esdm-2-python (`eventsourcing`)           |
|----------------------|-----------------------------|-------------------------------------------|
| aggregate            | `#[Aggregate]` class        | `Aggregate` subclass                      |
| event                | `#[Apply]` event methods    | `@event`-decorated mutators               |
| command              | message handler → aggregate | `Application` command method              |
| read-model / proj.   | async subscriber/projector  | projector over the notification log       |
| query                | read from projection table  | read-model finder                         |
| state machine (0001) | guard in the command method | admit check in the command method         |
| store                | Doctrine DBAL / PostgreSQL  | `eventsourcing-django` (Django ORM store) |

`django-cqrs` (master→replica replication) and `protean` (a whole DDD framework) were the
alternatives; both were rejected — the first is not event sourcing, the second would fight the
codegen for control of the emitted structure. `eventsourcing` is a library the codegen emits
*against*, which is exactly the patchlevel relationship.

## Related projects

Standalone codegen — depends on no sibling repo. Around it:

| Repo                 | Role                                                                                                                                             |
|----------------------|--------------------------------------------------------------------------------------------------------------------------------------------------|
| `../esdm-extensions` | the **spec repo** this codegen implements — proposals 0001 (state machines), 0002 (FEEL rules), 0003 (BPMN→ESDM), 0004 (domain-console contract) |
| `../esdm-vue-reader` | the stack-agnostic **domain console** — point it at a running generated app; it consumes the 0004 contract the app emits                         |
| `../esdm-2-symfony`  | the sibling PHP → Symfony codegen (reference for the target shape)                                                                               |

## Generate and run the `todo` app

```sh
pip install -e .                                   # PyYAML is the only runtime dep
python -m esdm2python.cli generate examples/todo   # -> examples/todo/generated/python/

cd examples/todo/generated/python
docker compose up -d --build      # api migrates + arms the hash chain on boot, serves on :8080
curl -s -XPOST localhost:8080/tasks/add-task -d '{"title":"Buy milk"}'
curl -s localhost:8080/tasks/list-tasks
curl -s localhost:8080/_dev/catalog          # 0004 domain-console contract
docker compose exec api python manage.py eventstore_verify   # audit the event chain
```

Same app on the EventSourcingDB + MongoDB stack:

```sh
python -m esdm2python.cli generate examples/todo --target django-eventsourcingdb
cd examples/todo/generated/python-esdb
docker compose up -d --build      # esdb + mongo + api (:8080) + observe worker
```

The emitted app's own write-side lifecycle tests (in-memory, no database):

```sh
cd examples/todo/generated/python
pip install -r requirements.txt
python -m pytest -q
```

## Develop the generator

```sh
pip install -e ".[dev]"
python -m pytest -q          # generator unit tests (model, FEEL, adapter)
scripts/conformance.sh       # regenerate every examples/* app, fail on empty/error
```

## License

[MIT](LICENSE) © 2026 Ralf Süss
