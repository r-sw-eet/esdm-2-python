"""The Django + EventSourcingDB + MongoDB adapter.

Same Django HTTP surface as the Postgres target (routes, 409/404 semantics,
0004 dev contract), different machinery behind it: the write side replays a
subject from EventSourcingDB and folds pure state, the read side lives in
MongoDB `rm_*` collections kept fresh by a long-running `observe` worker.
Events use the family's shared wire envelope, so a store written by any
sibling codegen replays here unchanged.
"""

from __future__ import annotations

import json

from ...model import Aggregate, BoundedContext, Command, Event, Feature, Model, Policy, ReadModel
from ...names import camel, snake, studly, upper_const
from ...project import GeneratedProject
from ..django import templates as base_t
from ..django.adapter import DjangoEventSourcingAdapter, _file, _py_literal
from ..django.types import coerce_payload, default_literal, py_type, zero_literal
from . import templates as t


class DjangoEventSourcingDbAdapter(DjangoEventSourcingAdapter):
    guard_receiver = "state"

    def name(self) -> str:
        return "django-eventsourcingdb"

    def description(self) -> str:
        return "Django + EventSourcingDB event store + MongoDB read models (CQRS, event-sourced, observe worker)."

    def slug(self) -> str:
        return "python-esdb"

    # -- name derivations ------------------------------------------------------

    def _event_const(self, event: Event) -> str:
        return upper_const(event.name)

    def _state_class(self, agg: Aggregate) -> str:
        return studly(agg.name) + "State"

    def _collection_const(self, rm: ReadModel) -> str:
        return upper_const(rm.name) + "_COLLECTION"

    def _subject_root(self, agg: Aggregate) -> str:
        return f"/{agg.name}"

    def _source(self, model: Model, options: dict) -> str:
        return str(options.get("source") or f"https://esdm-extensions.io/{model.domain}")

    def _payload_key(self, name: str) -> str:
        # the family envelope carries camelCase payload keys (wire compatibility)
        return camel(name)

    def _domain_refs(self, model: Model) -> dict[str, str]:
        contexts = [c.name for c in model.bounded_contexts if c.aggregates]
        if len(contexts) == 1:
            return {contexts[0]: "domain"}
        return {name: f"{snake(name)}_domain" for name in contexts}

    def _projection_refs(self, model: Model) -> dict[str, str]:
        contexts = [c.name for c in model.bounded_contexts if c.read_models]
        if len(contexts) == 1:
            return {contexts[0]: "projections"}
        return {name: f"{snake(name)}_projections" for name in contexts}

    @staticmethod
    def _module_import(context: str, module: str, ref: str) -> str:
        plain = f"from {context} import {module}"
        return plain if ref == module else f"{plain} as {ref}"

    # -- orchestration -----------------------------------------------------------

    def generate(self, model: Model, options: dict | None = None) -> GeneratedProject:
        options = options or {}
        project = GeneratedProject()
        primary = next((c for c in model.bounded_contexts if c.aggregates), None)

        for context in model.bounded_contexts:
            project.add(f"{context.name}/__init__.py", "")
            project.add(f"{context.name}/apps.py", self._apps(context))
            for aggregate in context.aggregates:
                project.add(f"{context.name}/domain.py", self._domain(aggregate))
            if context.read_models:
                project.add(f"{context.name}/projections.py", self._projections(context))
                project.add(f"{context.name}/finders.py", self._finders(context))
            if context.aggregates or context.queries:
                project.add(f"{context.name}/urls.py", self._urls(context))
                project.add(f"{context.name}/views.py", self._views(context))

        if primary is not None:
            project.add(f"{primary.name}/application.py", self._application(model))
        if self._resolvable_policies(model):
            project.add("policies.py", self._policies_module(model))

        project.add("config/__init__.py", "")
        project.add("config/settings.py", self._settings(model))
        project.add("config/urls.py", self._config_urls(model))
        project.add("config/wsgi.py", base_t.WSGI)

        project.add("shared/__init__.py", "")
        project.add("shared/errors.py", t.ERRORS)
        project.add("shared/cors.py", base_t.CORS)
        project.add("shared/events.py", t.EVENTS)
        project.add("shared/esdb.py", t.ESDB % {"source": self._source(model, options)})
        project.add("shared/mongo.py", t.MONGO)
        project.add("shared/runtime.py", self._runtime(model, primary))
        project.add("shared/management/__init__.py", "")
        project.add("shared/management/commands/__init__.py", "")
        project.add("shared/management/commands/observe.py", self._observe_command(model))

        project.add("dev/__init__.py", "")
        project.add("dev/views.py", self._dev_views(model))
        project.add("dev/urls.py", base_t.DEV_URLS)
        project.add("dev/catalog.json", self._catalog(model))
        project.add("dev/source.bpmn", str(options.get("bpmnSource") or ""))

        project.add("tests/__init__.py", "")
        for feature in model.features:
            project.add(f"tests/test_{snake(feature.name)}.py", self._test(model, feature))

        project.add("manage.py", base_t.MANAGE)
        project.add("conftest.py", base_t.CONFTEST)
        project.add("pytest.ini", base_t.PYTEST_INI)
        project.add("requirements.txt", t.REQUIREMENTS)
        project.add(".env.example", t.ENV_EXAMPLE)
        project.add("Dockerfile", t.DOCKERFILE)
        project.add("compose.yaml", t.COMPOSE)
        project.add("Makefile", t.MAKEFILE)
        project.add("README.md", self._readme(model))
        return project

    # -- write side: pure decide/evolve -------------------------------------------

    def _domain(self, agg: Aggregate) -> str:
        cls = self._state_class(agg)
        sm = agg.state_machine
        lines = [
            f'"""Write model for the `{agg.name}` aggregate — pure decide/evolve.',
            "",
            "The command functions *decide* (admit against the 0001 state machine, then",
            "return new events); `apply_event` *evolves* (folds an event into the immutable",
            "state, and re-runs on every replay). Never put admit checks into the fold — it",
            're-runs on every load. Framework-free, so the write side tests in-memory."""',
            "",
            "from __future__ import annotations",
            "",
            "from dataclasses import dataclass, replace",
            "",
        ]
        err = []
        if sm is not None:
            err.append("IllegalTransition")
        if any(self._guard_expr(agg, c) for c in agg.commands):
            err.append("GuardViolation")
        if err:
            lines.append(f"from shared.errors import {', '.join(err)}")
        lines.append("from shared.events import DomainEvent")
        lines.append("")
        for event in agg.events:
            lines.append(f'{self._event_const(event)} = "{event.type}"')
        lines += ["", f'SUBJECT_ROOT = "{self._subject_root(agg)}"']

        lines += ["", "", "@dataclass(frozen=True)", f"class {cls}:"]
        if sm is not None:
            for state in sm.states:
                lines.append(f'    {upper_const(state.name)} = "{state.name}"')
            lines.append("")
        lines.append(f"    {snake(agg.identity_field)}: str")
        for field in agg.non_identity_state():
            lines.append(f"    {snake(field.name)}: {py_type(field)} = {default_literal(field)}")
        if sm is not None:
            lines.append("    status: str | None = None")

        lines += ["", "", f"def apply_event(state: {cls}, event: DomainEvent) -> {cls}:"]
        lines.append("    data = event.data")
        for event in agg.events:
            lines += self._fold_branch(agg, event)
        lines.append("    return state")

        create = agg.create_command()
        for cmd in agg.commands:
            event = agg.event(cmd.primary_event() or "")
            if event is None:
                continue
            lines += ["", ""] + self._decider(agg, cmd, event, cmd is create)
        if sm is not None:
            lines += ["", ""] + self._admit_fn(agg)
        return _file(lines)

    def _fold_branch(self, agg: Aggregate, event: Event) -> list[str]:
        cls = self._state_class(agg)
        sm = agg.state_machine
        assigns: list[str] = []
        if event.lifecycle.value == "create":
            for field in agg.non_identity_state():
                if event.data.has(field.name):
                    key = self._payload_key(field.name)
                    assigns.append(f'{snake(field.name)}=data.get("{key}", {default_literal(field)}),')
                else:
                    assigns.append(f"{snake(field.name)}={default_literal(field)},")
        elif event.lifecycle.value != "delete":
            for field in event.data:
                if field.is_identity or not agg.state.has(field.name):
                    continue
                key = self._payload_key(field.name)
                assigns.append(f'{snake(field.name)}=data.get("{key}", {default_literal(field)}),')
        if sm is not None:
            target = sm.transition_target(event.name)
            if target is None and event.lifecycle.value == "create":
                target = sm.initial
            if target is not None:
                assigns.append(f"status={cls}.{upper_const(target)},")

        lines = [f"    if event.type == {self._event_const(event)}:"]
        if event.lifecycle.value == "delete":
            lines.append("        # soft delete: the projection removes the row, the event stream stays intact")
        if not assigns:
            lines.append("        return state")
            return lines
        lines.append("        return replace(")
        lines.append("            state,")
        for assign in assigns:
            lines.append(f"            {assign}")
        lines.append("        )")
        return lines

    def _decider(self, agg: Aggregate, cmd: Command, event: Event, is_create: bool) -> list[str]:
        cls = self._state_class(agg)
        params = "".join(f", {snake(f.name)}: {py_type(f)}" for f in cmd.data if not f.is_identity)
        lines = [f"def {snake(cmd.name)}(state: {cls}{params}) -> list[DomainEvent]:"]
        sm = agg.state_machine
        if not is_create and sm is not None and sm.admit_for(cmd.name) is not None:
            lines.append(f'    _admit("{cmd.name}", state)')
        guard = self._guard_expr(agg, cmd)
        if not is_create and guard is not None:
            expr, requirement, prelude = guard
            lines += [f"    {line}" for line in prelude]
            lines.append(f"    if not ({expr}):")
            lines.append(f'        raise GuardViolation("{cmd.name}", {json.dumps(requirement)})')

        carried = {f.name for f in cmd.data if not f.is_identity}
        entries, from_state = [], []
        for field in event.data:
            key = self._payload_key(field.name)
            if field.is_identity:
                entries.append(f'"{key}": state.{snake(agg.identity_field)},')
            elif field.name in carried:
                entries.append(f'"{key}": {snake(field.name)},')
            elif not is_create and agg.state.has(field.name):
                entries.append(f'"{key}": state.{snake(field.name)},')
                from_state.append(field.name)
            else:
                entries.append(f'"{key}": {default_literal(field)},')
        if from_state:
            lines.append(f"    # carry current {', '.join(from_state)} into the event")
        lines.append("    return [")
        lines.append(f"        DomainEvent({self._event_const(event)}, {{")
        for entry in entries:
            lines.append(f"            {entry}")
        lines.append("        }),")
        lines.append("    ]")
        return lines

    def _admit_fn(self, agg: Aggregate) -> list[str]:
        # Per-command from-states: the union of all admitting states would wrongly
        # admit command A from a state only command B allows (C4: orders scenario).
        cls = self._state_class(agg)
        entries = []
        for admit in agg.state_machine.admits:
            rendered = ", ".join(f"{cls}.{upper_const(s)}" for s in admit.from_states)
            trailing = "," if len(admit.from_states) == 1 else ""
            entries.append(f'    "{admit.command}": ({rendered}{trailing}),')
        return [
            "_ADMITS = {",
            *entries,
            "}",
            "",
            "",
            f"def _admit(command: str, state: {cls}) -> None:",
            "    if state.status not in _ADMITS[command]:",
            "        raise IllegalTransition(command, state.status)",
        ]

    # -- write side: application service -------------------------------------------

    def _application(self, model: Model) -> str:
        app_class = self._app_class(model)
        aggregates = model.aggregates()
        refs = self._domain_refs(model)
        has_mutation = any(c is not a.create_command() for a in aggregates for c in a.commands)
        lines = [
            f'"""Application service for the `{model.domain}` domain.',
            "",
            "One method per command: rebuild the aggregate's state by replaying its subject",
            "from EventSourcingDB, run the pure decider, and append the new events with a",
            "concurrency precondition. The read side is projected separately (see",
            '`projections.py` and the `observe` worker)."""',
            "",
            "from __future__ import annotations",
            "",
            "from uuid import uuid4",
            "",
            "from shared import esdb",
        ]
        if has_mutation:
            lines.append("from shared.errors import AggregateNotFound")
        for context in model.bounded_contexts:
            if context.aggregates:
                lines.append(self._module_import(context.name, "domain", refs[context.name]))
        lines += ["", "", f"class {app_class}:"]

        blocks: list[list[str]] = []
        loaders: list[list[str]] = []
        for aggregate in aggregates:
            ref = refs[aggregate.bounded_context]
            create = aggregate.create_command()
            needs_loader = False
            for cmd in aggregate.commands:
                if aggregate.event(cmd.primary_event() or "") is None:
                    continue
                if cmd is create:
                    blocks.append(self._create_service(aggregate, cmd, ref))
                else:
                    blocks.append(self._mutate_service(aggregate, cmd, ref))
                    needs_loader = True
            if needs_loader:
                loaders.append(self._loader(aggregate, ref))

        body: list[str] = []
        for index, block in enumerate(blocks + loaders):
            if index > 0:
                body.append("")
            body += block
        return _file(lines + body)

    def _create_service(self, agg: Aggregate, cmd: Command, ref: str) -> list[str]:
        cls = self._state_class(agg)
        var = snake(agg.name)
        id_attr = snake(agg.identity_field)
        params = "".join(f", {snake(f.name)}: {py_type(f)}" for f in cmd.data if not f.is_identity)
        args = "".join(f", {snake(f.name)}" for f in cmd.data if not f.is_identity)
        return [
            f"    def {snake(cmd.name)}(self{params}) -> str:",
            f"        {var} = {ref}.{cls}({id_attr}=str(uuid4()))",
            f"        events = {ref}.{snake(cmd.name)}({var}{args})",
            f'        esdb.append(events, f"{{{ref}.SUBJECT_ROOT}}/{{{var}.{id_attr}}}", pristine=True)',
            f"        return {var}.{id_attr}",
        ]

    def _mutate_service(self, agg: Aggregate, cmd: Command, ref: str) -> list[str]:
        var = snake(agg.name)
        id_attr = snake(agg.identity_field)
        id_param = self._id_param(agg.name)
        params = f", {id_param}: str" + "".join(
            f", {snake(f.name)}: {py_type(f)}" for f in cmd.data if not f.is_identity
        )
        args = "".join(f", {snake(f.name)}" for f in cmd.data if not f.is_identity)
        return [
            f"    def {snake(cmd.name)}(self{params}) -> None:",
            f"        {var} = self._load_{var}({id_param})",
            f"        events = {ref}.{snake(cmd.name)}({var}{args})",
            f'        esdb.append(events, f"{{{ref}.SUBJECT_ROOT}}/{{{var}.{id_attr}}}", pristine=False)',
        ]

    def _loader(self, agg: Aggregate, ref: str) -> list[str]:
        cls = self._state_class(agg)
        var = snake(agg.name)
        id_attr = snake(agg.identity_field)
        id_param = self._id_param(agg.name)
        return [
            f"    def _load_{var}(self, {id_param}: str) -> {ref}.{cls}:",
            f"        {var} = {ref}.{cls}({id_attr}={id_param})",
            "        found = False",
            f'        for event in esdb.read(f"{{{ref}.SUBJECT_ROOT}}/{{{id_param}}}"):',
            f"            {var} = {ref}.apply_event({var}, event)",
            "            found = True",
            "        if not found:",
            f"            raise AggregateNotFound({id_param})",
            f"        return {var}",
        ]

    # -- read side: projections + finders -------------------------------------------

    def _projections(self, context: BoundedContext) -> str:
        lines = [
            f'"""Read-side projections for the `{context.name}` context: fold events into MongoDB.',
            "",
            "One collection per read model (`rm_*`); each row's `revision` is the id of the",
            "last event folded into it and feeds the observer's resume lower bound. The",
            "`observe` worker (its own process) tails EventSourcingDB and calls these",
            'handlers. Rebuild a read model by dropping its collection and restarting."""',
            "",
            "from __future__ import annotations",
            "",
            "from shared.esdb import payload_of",
            "from shared.mongo import get_db",
            f"from {context.name} import domain",
            "",
        ]
        for rm in context.read_models:
            lines.append(f'{self._collection_const(rm)} = "{self._rm_table(rm)}"')
        lines += ["", "", "def ensure_indexes() -> None:"]
        for rm in context.read_models:
            lines.append(
                f'    get_db()[{self._collection_const(rm)}].create_index("{rm.primary_key()}", unique=True)'
            )
        for rm in context.read_models:
            lines += ["", ""] + self._lower_bound_fn(rm)
            lines += ["", ""] + self._projection_fn(context, rm)
        return _file(lines)

    def _lower_bound_fn(self, rm: ReadModel) -> list[str]:
        return [
            f"def {snake(rm.name)}_lower_bound() -> str | None:",
            f'    newest = get_db()[{self._collection_const(rm)}].find_one(sort=[("revision", -1)])',
            '    return str(newest["revision"]) if newest else None',
        ]

    def _projection_fn(self, context: BoundedContext, rm: ReadModel) -> list[str]:
        lines = [
            f"def project_{snake(rm.name)}(event) -> None:",
            "    data = payload_of(event)",
            "    revision = int(event.event_id)",
            f"    rows = get_db()[{self._collection_const(rm)}]",
            "",
        ]
        first = True
        for aggregate in context.aggregates:
            for event in aggregate.events:
                projection = next((p for p in rm.projections if p.event == event.name), None)
                if projection is None:
                    continue
                keyword = "if" if first else "elif"
                first = False
                lines.append(f"    {keyword} event.type == domain.{self._event_const(event)}:")
                lines += self._projection_op(rm, aggregate, event, projection)
        return lines

    def _projection_op(self, rm: ReadModel, agg: Aggregate, event: Event, projection) -> list[str]:
        pk = rm.primary_key()
        id_expr = f'data.get("{self._payload_key(agg.identity_field)}")'
        op = (projection.rule or "").strip().split(" ")[0].lower() if projection.rule else event.lifecycle.value
        op = {
            "insert": "insert", "update": "update", "delete": "delete",
            "create": "insert", "mutate": "update",
        }.get(op, "update")
        carried = {f.name for f in event.data}
        if op == "delete":
            return [f'        rows.delete_one({{"{pk}": {id_expr}}})']
        sets: list[str] = []
        for column in rm.columns:
            if column.name == pk:
                continue
            if column.name in carried:
                key = self._payload_key(column.name)
                sets.append(f'"{column.name}": data.get("{key}", {default_literal(column)}),')
            elif op == "insert":
                sets.append(f'"{column.name}": {default_literal(column)},')
        sets.append('"revision": revision,')
        lines = ["        rows.update_one("]
        lines.append(f'            {{"{pk}": {id_expr}}},')
        lines.append('            {"$set": {')
        for entry in sets:
            lines.append(f"                {entry}")
        lines.append("            }},")
        if op == "insert":
            lines.append("            upsert=True,")
        lines.append("        )")
        return lines

    def _finders(self, context: BoundedContext) -> str:
        used = sorted({
            self._collection_const(context.read_model(q.read_model))
            for q in context.queries
            if context.read_model(q.read_model)
        })
        lines = [
            '"""Query helpers over the read-model collections. Row keys are the app\'s own',
            'naming (what `/_dev/catalog` advertises as the read-model columns)."""',
            "",
            "from __future__ import annotations",
            "",
            "from shared.mongo import get_db",
            f"from {context.name}.projections import {', '.join(used)}",
            "",
            "# Mongo bookkeeping (_id) and the projection cursor (revision) stay internal.",
            '_INTERNAL = {"_id": 0, "revision": 0}',
        ]
        for query in context.queries:
            rm = context.read_model(query.read_model)
            if rm is None:
                continue
            lines += ["", ""] + self._finder_fn(query, rm)
        return _file(lines)

    def _finder_fn(self, query, rm: ReadModel) -> list[str]:
        coll = self._collection_const(rm)
        pk = rm.primary_key()
        fn = snake(query.name)
        if query.is_get():
            param = self._id_param(rm.name)
            return [
                f"def {fn}({param}: str) -> dict | None:",
                f'    return get_db()[{coll}].find_one({{"{pk}": {param}}}, _INTERNAL)',
            ]
        return [
            f"def {fn}() -> list[dict]:",
            f'    return list(get_db()[{coll}].find({{}}, _INTERNAL).sort("{pk}", 1))',
        ]

    # -- HTTP edge ---------------------------------------------------------------

    def _views(self, context: BoundedContext) -> str:
        lines = [
            '"""HTTP edge for the `' + context.name + "` context — the uniform domain surface (0004 §1).",
            "",
            "    POST /" + context.name + "/<command>   JSON body per the command's fields",
            "    GET  /" + context.name + "/<query>     list -> array, get -> one row (params via query string)",
            "",
            'A rejected domain rule (0001 transition / 0002 guard) is 409 with `{ "error": ... }`.',
            "Commands append to EventSourcingDB; the `observe` worker projects them into the",
            'read models, so reads are eventually consistent with writes."""',
            "",
            "from __future__ import annotations",
            "",
            "import json",
            "",
            "from django.http import HttpResponse, JsonResponse",
            "from django.views.decorators.csrf import csrf_exempt",
            "from django.views.decorators.http import require_http_methods",
            "",
            "from shared.errors import AggregateNotFound, DomainViolation",
            "from shared.runtime import get_app",
            f"from {context.name} import finders",
            "",
            t.VIEWS_HELPERS,
            "",
            "",
            "# --- commands -------------------------------------------------------------",
        ]
        for aggregate in context.aggregates:
            create = aggregate.create_command()
            for cmd in aggregate.commands:
                lines += ["", ""] + self._command_view(cmd, cmd is create)
        lines += ["", "", "# --- queries --------------------------------------------------------------"]
        for query in context.queries:
            lines += ["", ""] + self._query_view(query)
        return _file(lines)

    def _query_view(self, query) -> list[str]:
        fn = snake(query.name)
        lines = ['@require_http_methods(["GET"])', f"def {fn}(request):"]
        if query.is_get():
            param = query.parameters.fields[0]
            entity = studly(query.read_model)
            code = f"{snake(query.read_model).upper()}_NOT_FOUND"
            lines += [
                f'    row = finders.{fn}(request.GET.get("{param.name}", ""))',
                "    if row is None:",
                '        return JsonResponse({"error": "NOT_FOUND", "message": '
                + f'"{entity} not found"'
                + ', "details": {"errorCode": '
                + f'"{code}"'
                + ', "reason": '
                + f'"Could not find {entity} matching the given filter"'
                + "}}, status=404)",
                "    return JsonResponse(row)",
            ]
        else:
            lines.append(f"    return JsonResponse(finders.{fn}(), safe=False)")
        return lines

    # -- policies -----------------------------------------------------------------

    def _resolvable_policies(self, model: Model) -> list[Policy]:
        resolved = []
        for policy in model.policies:
            handle = model.aggregate(policy.handle_context, policy.handle_aggregate)
            emit = model.aggregate(policy.emit_context, policy.emit_aggregate)
            if handle is None or emit is None or handle.event(policy.handle_event) is None:
                continue
            if not any(c.name == policy.emit_command for c in emit.commands):
                continue
            resolved.append(policy)
        return resolved

    def _policies_module(self, model: Model) -> str:
        refs = self._domain_refs(model)
        lines = [
            '"""Policies: stateless reactions — whenever their event occurs, dispatch the',
            "follow-up command through the application service. Fed by the `observe` worker;",
            'a failed dispatch is logged, never retried (replay the log to re-run it)."""',
            "",
            "from __future__ import annotations",
            "",
            "from shared.esdb import payload_of",
            "from shared.runtime import get_app",
        ]
        handle_contexts = sorted({p.handle_context for p in self._resolvable_policies(model)})
        for context in handle_contexts:
            lines.append(self._module_import(context, "domain", refs[context]))
        for policy in self._resolvable_policies(model):
            lines += ["", ""] + self._policy_fn(model, policy, refs)
        return _file(lines)

    def _policy_fn(self, model: Model, policy: Policy, refs: dict[str, str]) -> list[str]:
        handle = model.aggregate(policy.handle_context, policy.handle_aggregate)
        emit = model.aggregate(policy.emit_context, policy.emit_aggregate)
        command = next(c for c in emit.commands if c.name == policy.emit_command)
        event = handle.event(policy.handle_event)
        ref = refs[policy.handle_context]
        is_create = command is emit.create_command()

        args = []
        if not is_create:
            args.append(f'str(data.get("{self._payload_key(handle.identity_field)}", ""))')
        for field in command.data:
            if field.is_identity:
                continue
            if snake(field.name) == snake(f"{policy.handle_aggregate}-id"):
                args.append(f'str(data.get("{self._payload_key(handle.identity_field)}", ""))')
            elif event.data.has(field.name):
                key = self._payload_key(field.name)
                args.append(coerce_payload(field, f'data.get("{key}", {zero_literal(field)})'))
            else:
                args.append(default_literal(field))

        return [
            f"def {snake(policy.name)}_policy(event) -> None:",
            f"    if event.type != {ref}.{self._event_const(event)}:",
            "        return",
            "    data = payload_of(event)",
            "    try:",
            f"        get_app().{snake(command.name)}({', '.join(args)})",
            "    except Exception as error:",
            f'        print(f"policy {policy.name} failed to dispatch {command.name} ({{error}})", flush=True)',
        ]

    # -- worker ---------------------------------------------------------------------

    def _observe_command(self, model: Model) -> str:
        refs = self._projection_refs(model)
        policies = self._resolvable_policies(model)
        lines = [
            '"""Long-running read-side worker: tails EventSourcingDB, folds events into the',
            "MongoDB read models, and lets policies dispatch their follow-up commands. Runs",
            'as its own process/service: `python manage.py observe`."""',
            "",
            "from __future__ import annotations",
            "",
            "import asyncio",
            "",
            "from django.core.management.base import BaseCommand",
            "from eventsourcingdb import Bound, BoundType, ObserveEventsOptions",
            "",
            "from shared import esdb",
        ]
        if policies:
            lines.append("import policies")
        for context in model.bounded_contexts:
            if context.read_models:
                lines.append(self._module_import(context.name, "projections", refs[context.name]))
        lines += [
            "",
            "# (name, subject, handler, resume lower bound) — one observer per read model.",
            "OBSERVERS = [",
        ]
        for context in model.bounded_contexts:
            ref = refs.get(context.name)
            for rm in context.read_models:
                subject = self._projection_subject(context, rm)
                lines.append(
                    f'    ("{rm.name}", "{subject}", {ref}.project_{snake(rm.name)}, '
                    f"{ref}.{snake(rm.name)}_lower_bound),"
                )
        lines += [
            "]",
            "",
            "# One per policy — no cursor: a policy re-sees the log from the start on boot.",
            "POLICY_OBSERVERS = [",
        ]
        for policy in policies:
            handle = model.aggregate(policy.handle_context, policy.handle_aggregate)
            lines.append(
                f'    ("{policy.name}", "{self._subject_root(handle)}", policies.{snake(policy.name)}_policy),'
            )
        lines += [
            "]",
            "",
            "",
            "class Command(BaseCommand):",
            '    help = "Tail the event store; keep read models projected and policies running."',
            "",
            "    def handle(self, *args, **options):",
        ]
        for context in model.bounded_contexts:
            if context.read_models:
                lines.append(f"        {refs[context.name]}.ensure_indexes()")
        lines.append("        asyncio.run(_run())")
        lines.append(t.OBSERVE_TAIL)
        return _file(lines)

    def _projection_subject(self, context: BoundedContext, rm: ReadModel) -> str:
        aggregates = {
            aggregate.name
            for aggregate in context.aggregates
            for event in aggregate.events
            if rm.projects_event(event.name)
        }
        names = sorted(aggregates)
        return f"/{names[0]}" if len(names) == 1 else "/"

    # -- project-level files -----------------------------------------------------

    def _settings(self, model: Model) -> str:
        apps = ['"shared"'] + [f'"{c.name}"' for c in model.bounded_contexts]
        installed = "\n".join(f"    {a}," for a in apps)
        return _file([
            f'"""Django settings for the generated `{model.domain}` app.',
            "",
            "Deliberately lean: no relational database — events live in EventSourcingDB",
            "and the read models in MongoDB. Django provides the HTTP edge, the",
            'management commands and the dev window (0004)."""',
            "",
            "from __future__ import annotations",
            "",
            "import os",
            "",
            'SECRET_KEY = os.environ.get("SECRET_KEY", "dev-insecure-key-do-not-use-in-production")',
            'DEBUG = os.environ.get("APP_DEBUG", "1") == "1"',
            'ALLOWED_HOSTS = ["*"]',
            "",
            "INSTALLED_APPS = [",
            installed,
            "]",
            "",
            "# CORS is the only middleware the domain console needs (0004 §5).",
            "MIDDLEWARE = [",
            '    "shared.cors.CorsMiddleware",',
            "]",
            "",
            'ROOT_URLCONF = "config.urls"',
            'WSGI_APPLICATION = "config.wsgi.application"',
            "",
            "DATABASES: dict = {}",
            "",
            "USE_TZ = True",
        ])

    def _runtime(self, model: Model, primary: BoundedContext | None) -> str:
        app_class = self._app_class(model)
        ctx = primary.name if primary is not None else "app"
        return _file([
            '"""Process-wide application singleton. Stateless (every call goes to the event',
            "store), but kept behind get_app() so views, policies and tests share the same",
            'seam as the sibling targets."""',
            "",
            "from __future__ import annotations",
            "",
            "_app = None",
            "",
            "",
            "def get_app():",
            "    global _app",
            "    if _app is None:",
            f"        from {ctx}.application import {app_class}",
            "",
            f"        _app = {app_class}()",
            "    return _app",
        ])

    def _dev_views(self, model: Model) -> str:
        lines = [
            '"""Dev-only window onto the app for an external domain console (0004): the model',
            "catalog, the authoring BPMN, and the raw event stream. Not part of the domain",
            'API — gate it out of production."""',
            "",
            "from __future__ import annotations",
            "",
            "from pathlib import Path",
            "",
            "from django.http import HttpResponse, JsonResponse",
            "",
            "from shared import esdb",
            "",
            "_HERE = Path(__file__).resolve().parent",
            "",
            "# CloudEvents event type -> (aggregate name, ESDM event name) for the 0004 rows",
            "_EVENTS = {",
        ]
        for aggregate in model.aggregates():
            for event in aggregate.events:
                lines.append(f'    "{event.type}": ("{aggregate.name}", "{event.name}"),')
        lines.append("}")
        lines.append(t.DEV_VIEWS_TAIL)
        return _file(lines)

    # -- GWT tests over the pure deciders -----------------------------------------

    def _test(self, model: Model, feature: Feature) -> str:
        aggregate = model.aggregate(feature.bounded_context, feature.aggregate)
        has_rejection = any(s.is_rejection() for s in feature.scenarios)
        lines = [
            '"""Write-side lifecycle tests — the GWT scenarios of the `' + feature.name + "` feature",
            "run against the pure deciders (fold the given events, decide, compare events),",
            'so neither a database nor the event store is needed."""',
            "",
            "from __future__ import annotations",
            "",
        ]
        if has_rejection:
            lines += ["import pytest", "", "from shared.errors import IllegalTransition"]
        lines.append("from shared.events import DomainEvent")
        lines.append(f"from {feature.bounded_context} import domain")
        for scenario in feature.scenarios:
            lines += ["", ""] + self._scenario(aggregate, scenario)
        return _file(lines)

    def _scenario(self, aggregate: Aggregate, scenario) -> list[str]:
        cls = self._state_class(aggregate)
        id_attr = snake(aggregate.identity_field)
        scenario_id = self._scenario_identity(aggregate, scenario)
        lines = [
            f"def test_{snake(scenario.name)}():",
            f"    state = domain.{cls}({id_attr}={_py_literal(scenario_id)})",
        ]
        for example in scenario.given:
            event = aggregate.event(example.event)
            if event is None:
                continue
            lines += [
                "    state = domain.apply_event(",
                "        state,",
                f"        DomainEvent(domain.{self._event_const(event)}, "
                f"{self._data_literal(event.data, example.data)}),",
                "    )",
            ]
        cmd = next((c for c in aggregate.commands if c.name == scenario.command_name), None)
        call = self._decider_call(aggregate, cmd, scenario.command_data)
        if scenario.is_rejection():
            lines.append("    with pytest.raises(IllegalTransition):")
            lines.append(f"        {call}")
            return lines
        lines.append(f"    events = {call}")
        lines.append("    assert events == [")
        for example in scenario.then_events:
            event = aggregate.event(example.event)
            if event is None:
                continue
            lines.append(
                f"        DomainEvent(domain.{self._event_const(event)}, "
                f"{self._data_literal(event.data, example.data)}),"
            )
        lines.append("    ]")
        return lines

    def _scenario_identity(self, aggregate: Aggregate, scenario) -> str:
        id_field = aggregate.identity_field
        if scenario.command_data.get(id_field) is not None:
            return str(scenario.command_data[id_field])
        for example in [*scenario.then_events, *scenario.given]:
            if example.data.get(id_field) is not None:
                return str(example.data[id_field])
        return ""

    def _decider_call(self, aggregate: Aggregate, cmd: Command | None, data: dict) -> str:
        if cmd is None:
            return "[]"
        args = ["state"]
        for field in cmd.data:
            if field.is_identity:
                continue
            args.append(_py_literal(data.get(field.name, self._zero_value(field))))
        return f"domain.{snake(cmd.name)}({', '.join(args)})"

    def _data_literal(self, fields, data: dict) -> str:
        parts = []
        for field in fields:
            value = data.get(field.name, self._zero_value(field))
            parts.append(f'"{self._payload_key(field.name)}": {_py_literal(value)}')
        return "{" + ", ".join(parts) + "}"

    # -- README --------------------------------------------------------------------

    def _readme(self, model: Model) -> str:
        primary = next((c for c in model.bounded_contexts if c.aggregates), None)
        ctx = primary.name if primary is not None else "app"
        return _file([
            f"# {model.domain} (generated)",
            "",
            "Generated by **esdm-2-python** (`django-eventsourcingdb` target) from the",
            f"`{model.domain}` ESDM model. Do not edit by hand — change the model and regenerate.",
            "",
            "## Architecture",
            "",
            f"- **Write side** (`{ctx}/domain.py` + `application.py`): HTTP",
            "  `POST /<context>/<command>` calls the application service, which replays the",
            "  subject's events from **EventSourcingDB** (subject `/<aggregate>/<id>`), folds",
            "  the pure aggregate state, runs the pure decider (0001 admit + 0002 guards),",
            "  and appends the new events with a concurrency precondition",
            "  (`isSubjectPristine` on create, `isSubjectPopulated` on mutate).",
            f"- **Read side** (`{ctx}/projections.py` + the `observe` worker): a separate",
            "  worker process tails the event store and folds events into **MongoDB**",
            "  collections (`rm_*`); each row's `revision` is the last folded event id and",
            "  the observer resumes from the max stored revision. Reads are eventually",
            "  consistent with writes.",
            f"- **Query side** (`{ctx}/views.py` + `finders.py`): HTTP `GET /<context>/<query>`",
            "  reads the collections (`_id`/`revision` stay internal).",
            "- **Policies** react to events and dispatch commands; they run in the worker too.",
            "",
            "Events are stored in the family's shared wire envelope",
            "(`data = { payload, nimbusMeta: { correlationid } }`, CloudEvents type",
            "`<domain>.<aggregate>.<event>`), so a store written by any sibling codegen",
            "replays here unchanged.",
            "",
            "## Run",
            "",
            "```sh",
            "docker compose up -d --build      # esdb + mongo + api (:8080) + observe worker",
            "# EventSourcingDB UI: http://localhost:3000",
            "curl -s -XPOST localhost:8080/" + ctx + "/add-task -d '{\"title\":\"Buy milk\"}'",
            "curl -s localhost:8080/" + ctx + "/list-tasks",
            "```",
            "",
            "Run the write-side lifecycle tests (in-memory, no services):",
            "",
            "```sh",
            "pip install -r requirements.txt",
            "python -m pytest -q",
            "```",
            "",
            "## Domain console",
            "",
            "The app serves the **domain-console contract** (esdm-extensions 0004) in dev:",
            "`GET /_dev/catalog`, `GET /_dev/bpmn` and `GET /_dev/events`, plus permissive CORS.",
            "Point the stack-agnostic **esdm-vue-reader** viewer at `http://localhost:8080`.",
        ])
