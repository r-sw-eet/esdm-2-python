"""The stack-neutral ESDM model: typed nodes, the YAML loader, and the factory.

`load_directory` reads a dir of multi-doc ESDM YAML into raw dicts; `create_model`
groups them by `kind` and wires the cross-references into a `Model`. Nothing here
knows about Django — that lives in the adapter. Mirrors the model layer of
`esdm-2-symfony/src/Model` and `esdm-2-nimbus/src/model`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import yaml

from .lifecycle import Lifecycle


class _EsdmYamlLoader(yaml.SafeLoader):
    """SafeLoader with YAML 1.2 booleans: `on`/`off`/`yes`/`no` stay strings.

    PyYAML defaults to YAML 1.1, where `on:` (as in a state-machine transition
    `{on: task-added, ...}`) parses as the boolean key `True`. The PHP/TS siblings
    use 1.2 parsers and never hit this, so we match them.
    """


_EsdmYamlLoader.yaml_implicit_resolvers = {
    ch: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:bool"]
    for ch, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_EsdmYamlLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)

# --- defensive coercion (odd YAML degrades instead of throwing) --------------


def _record(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _listy(value: Any) -> list:
    if isinstance(value, list):
        return value
    return [] if value is None else [value]


def _annotation(doc: dict, key: str) -> Any:
    return _record(_record(doc.get("metadata")).get("annotations")).get(key)


# --- schema ------------------------------------------------------------------


@dataclass(frozen=True)
class Field:
    name: str
    json_type: str
    required: bool
    default: Any
    has_default: bool
    is_identity: bool = False

    def with_identity(self, is_identity: bool) -> "Field":
        return Field(
            self.name, self.json_type, self.required,
            self.default, self.has_default, is_identity,
        )


@dataclass(frozen=True)
class Schema:
    fields: tuple[Field, ...] = ()

    def __iter__(self) -> Iterator[Field]:
        return iter(self.fields)

    def field(self, name: str) -> Field | None:
        return next((f for f in self.fields if f.name == name), None)

    def has(self, name: str) -> bool:
        return self.field(name) is not None

    @staticmethod
    def from_raw(raw: Any) -> "Schema":
        raw = _record(raw)
        required = set(_listy(raw.get("required")))
        props = _record(raw.get("properties"))
        fields = []
        for name, spec in props.items():
            spec = _record(spec)
            fields.append(
                Field(
                    name=str(name),
                    json_type=str(spec.get("type", "mixed")),
                    required=name in required,
                    default=spec.get("default"),
                    has_default="default" in spec,
                )
            )
        return Schema(tuple(fields))


def _schema_with_identity(schema: Schema, identity_field: str) -> Schema:
    return Schema(tuple(f.with_identity(f.name == identity_field) for f in schema))


# --- state machine (0001) ----------------------------------------------------


@dataclass(frozen=True)
class State:
    name: str
    final: bool = False


@dataclass(frozen=True)
class Transition:
    event: str
    to: str


@dataclass(frozen=True)
class Admit:
    command: str
    from_states: tuple[str, ...]
    when: str | None = None


@dataclass(frozen=True)
class StateMachine:
    initial: str
    states: tuple[State, ...]
    transitions: tuple[Transition, ...]
    admits: tuple[Admit, ...]

    def transition_target(self, event: str) -> str | None:
        return next((t.to for t in self.transitions if t.event == event), None)

    def admit_for(self, command: str) -> Admit | None:
        return next((a for a in self.admits if a.command == command), None)

    def state(self, name: str) -> State | None:
        return next((s for s in self.states if s.name == name), None)

    def admitting_states(self) -> tuple[str, ...]:
        seen: list[str] = []
        for admit in self.admits:
            for state in admit.from_states:
                if state not in seen:
                    seen.append(state)
        return tuple(seen)


# --- domain nodes ------------------------------------------------------------


@dataclass
class Event:
    name: str
    domain: str
    bounded_context: str
    aggregate: str
    data: Schema
    lifecycle: Lifecycle
    type: str


@dataclass
class Command:
    name: str
    domain: str
    bounded_context: str
    aggregate: str
    data: Schema
    publishes: tuple[str, ...]
    lifecycle: Lifecycle

    def primary_event(self) -> str | None:
        return self.publishes[0] if self.publishes else None


@dataclass
class Aggregate:
    name: str
    domain: str
    bounded_context: str
    identity_field: str
    state: Schema
    events: list[Event] = field(default_factory=list)
    commands: list[Command] = field(default_factory=list)
    state_machine: StateMachine | None = None

    def non_identity_state(self) -> list[Field]:
        return [f for f in self.state if not f.is_identity]

    def event(self, name: str) -> Event | None:
        return next((e for e in self.events if e.name == name), None)

    def create_command(self) -> Command | None:
        create = next((c for c in self.commands if c.lifecycle is Lifecycle.CREATE), None)
        return create or (self.commands[0] if self.commands else None)


@dataclass
class Projection:
    aggregate: str
    event: str
    rule: str | None


@dataclass
class ReadModel:
    name: str
    domain: str
    bounded_context: str
    paradigm: str | None
    columns: Schema
    projections: list[Projection] = field(default_factory=list)

    def primary_key(self) -> str:
        identity = next((c for c in self.columns if c.is_identity), None)
        if identity is not None:
            return identity.name
        return self.columns.fields[0].name if self.columns.fields else "id"

    def projects_event(self, event: str) -> bool:
        return any(p.event == event for p in self.projections)


@dataclass
class Query:
    name: str
    domain: str
    bounded_context: str
    read_model: str
    parameters: Schema

    def is_get(self) -> bool:
        return bool(self.parameters.fields)


@dataclass
class BoundedContext:
    name: str
    domain: str
    aggregates: list[Aggregate] = field(default_factory=list)
    read_models: list[ReadModel] = field(default_factory=list)
    queries: list[Query] = field(default_factory=list)

    def read_model(self, name: str) -> ReadModel | None:
        return next((r for r in self.read_models if r.name == name), None)


@dataclass(frozen=True)
class Policy:
    """A stateless reaction: one handled event dispatches one emitted command."""

    name: str
    domain: str
    handle_context: str
    handle_aggregate: str
    handle_event: str
    emit_context: str
    emit_aggregate: str
    emit_command: str


@dataclass(frozen=True)
class EventExample:
    event: str
    data: dict


@dataclass(frozen=True)
class Scenario:
    name: str
    given: tuple[EventExample, ...]
    command_name: str
    command_data: dict
    then_events: tuple[EventExample, ...]
    rejection_reason: str | None

    def is_rejection(self) -> bool:
        return self.rejection_reason is not None


@dataclass(frozen=True)
class Feature:
    name: str
    domain: str
    bounded_context: str
    aggregate: str
    scenarios: tuple[Scenario, ...]


@dataclass
class Model:
    domain: str
    bounded_contexts: list[BoundedContext] = field(default_factory=list)
    features: list[Feature] = field(default_factory=list)
    policies: list[Policy] = field(default_factory=list)

    def aggregates(self) -> list[Aggregate]:
        return [a for c in self.bounded_contexts for a in c.aggregates]

    def aggregate(self, context: str, name: str) -> Aggregate | None:
        for ctx in self.bounded_contexts:
            if ctx.name == context:
                return next((a for a in ctx.aggregates if a.name == name), None)
        return None

    def features_for(self, aggregate: Aggregate) -> list[Feature]:
        return [
            f for f in self.features
            if f.bounded_context == aggregate.bounded_context and f.aggregate == aggregate.name
        ]


# --- loading -----------------------------------------------------------------

_YAML_SUFFIXES = (".esdm.yaml", ".esdm.yml", ".yaml", ".yml")


def load_directory(path: str | Path) -> list[dict]:
    """Load every ESDM YAML doc under `path` (sorted, multi-doc aware)."""
    root = Path(path)
    documents: list[dict] = []
    files = sorted(p for p in root.rglob("*") if p.is_file() and _is_yaml(p.name))
    for file in files:
        for doc in yaml.load_all(file.read_text(), Loader=_EsdmYamlLoader):
            if isinstance(doc, dict) and doc:
                documents.append(doc)
    return documents


def _is_yaml(name: str) -> bool:
    return name.endswith(_YAML_SUFFIXES)


# --- factory -----------------------------------------------------------------


def create_model(documents: list[dict]) -> Model:
    by_kind: dict[str, list[dict]] = {}
    for doc in documents:
        by_kind.setdefault(str(doc.get("kind", "")), []).append(doc)

    domains = by_kind.get("domain", [])
    if not domains:
        raise ValueError("Model contains no domain document.")
    domain_name = str(domains[0].get("name"))

    contexts: dict[str, BoundedContext] = {}

    def context(name: str | None) -> BoundedContext:
        key = name or "default"
        if key not in contexts:
            contexts[key] = BoundedContext(name=key, domain=domain_name)
        return contexts[key]

    for doc in by_kind.get("bounded-context", []):
        context(str(doc.get("name")))

    aggregates: dict[str, Aggregate] = {}
    for doc in by_kind.get("aggregate", []):
        scope = _record(doc.get("scope"))
        ctx = context(scope.get("boundedContext"))
        identity_field = str(_record(doc.get("identifiedBy")).get("field", "id"))
        state = _schema_with_identity(Schema.from_raw(doc.get("state")), identity_field)
        aggregate = Aggregate(
            name=str(doc.get("name")),
            domain=domain_name,
            bounded_context=ctx.name,
            identity_field=identity_field,
            state=state,
        )
        ctx.aggregates.append(aggregate)
        aggregates[f"{ctx.name}/{aggregate.name}"] = aggregate

    # an event inherits the lifecycle of the command that publishes it
    event_lifecycle: dict[str, Lifecycle] = {}
    for doc in by_kind.get("command", []):
        life = Lifecycle.from_name(
            str(doc.get("name")), _annotation(doc, "esdm-extensions.io/lifecycle")
        )
        for event_name in _listy(doc.get("publishes")):
            event_lifecycle[str(event_name)] = life

    for doc in by_kind.get("event", []):
        scope = _record(doc.get("scope"))
        key = f"{scope.get('boundedContext') or 'default'}/{scope.get('aggregate')}"
        aggregate = aggregates.get(key)
        if aggregate is None:
            continue
        name = str(doc.get("name"))
        annotated = _annotation(doc, "esdm-extensions.io/lifecycle")
        life = Lifecycle(annotated) if annotated else event_lifecycle.get(name, Lifecycle.MUTATE)
        cloud_type = _annotation(doc, "cloudevents.type") or f"{domain_name}.{aggregate.name}.{name}"
        data = _schema_with_identity(Schema.from_raw(doc.get("data")), aggregate.identity_field)
        aggregate.events.append(
            Event(name, domain_name, aggregate.bounded_context, aggregate.name, data, life, str(cloud_type))
        )

    for doc in by_kind.get("command", []):
        scope = _record(doc.get("scope"))
        key = f"{scope.get('boundedContext') or 'default'}/{scope.get('aggregate')}"
        aggregate = aggregates.get(key)
        if aggregate is None:
            continue
        name = str(doc.get("name"))
        life = Lifecycle.from_name(name, _annotation(doc, "esdm-extensions.io/lifecycle"))
        data = _schema_with_identity(Schema.from_raw(doc.get("data")), aggregate.identity_field)
        aggregate.commands.append(
            Command(
                name, domain_name, aggregate.bounded_context, aggregate.name,
                data, tuple(str(e) for e in _listy(doc.get("publishes"))), life,
            )
        )

    for doc in by_kind.get("state-machine", []):
        scope = _record(doc.get("scope"))
        key = f"{scope.get('boundedContext') or 'default'}/{scope.get('aggregate')}"
        aggregate = aggregates.get(key)
        if aggregate is None:
            continue
        states = tuple(
            State(str(_record(s).get("name")), bool(_record(s).get("final", False)))
            for s in _listy(doc.get("states"))
        )
        transitions = tuple(
            Transition(str(_record(t).get("on")), str(_record(t).get("to")))
            for t in _listy(doc.get("transitions"))
        )
        admits = tuple(
            Admit(
                str(_record(a).get("command")),
                tuple(str(s) for s in _listy(_record(a).get("from"))),
                _record(a).get("when"),
            )
            for a in _listy(doc.get("admits"))
        )
        aggregate.state_machine = StateMachine(str(doc.get("initial")), states, transitions, admits)

    for doc in by_kind.get("read-model", []):
        scope = _record(doc.get("scope"))
        ctx = context(scope.get("boundedContext"))
        projections = [
            Projection(
                str(_record(p).get("aggregate")),
                str(_record(p).get("event")),
                _record(p).get("rule"),
            )
            for p in _listy(doc.get("projections"))
        ]
        ctx.read_models.append(
            ReadModel(
                name=str(doc.get("name")),
                domain=domain_name,
                bounded_context=ctx.name,
                paradigm=doc.get("paradigm"),
                columns=Schema.from_raw(doc.get("schema")),
                projections=projections,
            )
        )

    for doc in by_kind.get("query", []):
        scope = _record(doc.get("scope"))
        ctx = context(scope.get("boundedContext"))
        ctx.queries.append(
            Query(
                name=str(doc.get("name")),
                domain=domain_name,
                bounded_context=ctx.name,
                read_model=str(doc.get("readModel")),
                parameters=Schema.from_raw(doc.get("parameters")),
            )
        )

    features: list[Feature] = []
    for doc in by_kind.get("feature", []):
        scope = _record(doc.get("scope"))
        if not scope.get("aggregate"):
            continue
        features.append(_parse_feature(doc, domain_name, scope))

    policies: list[Policy] = []
    for doc in by_kind.get("policy", []):
        handle = _record(next(iter(_listy(doc.get("handles"))), None))
        emit = _record(next(iter(_listy(doc.get("emits"))), None))
        if not handle.get("aggregate") or not emit.get("aggregate"):
            continue  # only aggregate-bound handle/emit are supported for now
        policies.append(
            Policy(
                name=str(doc.get("name")),
                domain=domain_name,
                handle_context=str(handle.get("boundedContext") or "default"),
                handle_aggregate=str(handle.get("aggregate")),
                handle_event=str(handle.get("event") or ""),
                emit_context=str(emit.get("boundedContext") or "default"),
                emit_aggregate=str(emit.get("aggregate")),
                emit_command=str(emit.get("command") or ""),
            )
        )

    return Model(
        domain=domain_name,
        bounded_contexts=list(contexts.values()),
        features=features,
        policies=policies,
    )


def _parse_feature(doc: dict, domain: str, scope: dict) -> Feature:
    scenarios = []
    for raw in _listy(doc.get("scenarios")):
        raw = _record(raw)
        given = tuple(
            EventExample(str(_record(g).get("event")), _record(_record(g).get("data")))
            for g in _listy(raw.get("given"))
        )
        when = _record(raw.get("when"))
        then = _record(raw.get("then"))
        then_events = tuple(
            EventExample(str(_record(e).get("event")), _record(_record(e).get("data")))
            for e in _listy(then.get("events"))
        )
        rejection = _record(then.get("rejection")).get("reason") if "rejection" in then else None
        scenarios.append(
            Scenario(
                name=str(raw.get("name")),
                given=given,
                command_name=str(when.get("command")),
                command_data=_record(when.get("data")),
                then_events=then_events,
                rejection_reason=str(rejection) if rejection is not None else None,
            )
        )
    return Feature(
        name=str(doc.get("name")),
        domain=domain,
        bounded_context=str(scope.get("boundedContext") or "default"),
        aggregate=str(scope.get("aggregate")),
        scenarios=tuple(scenarios),
    )
