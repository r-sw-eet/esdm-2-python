"""The Django + `eventsourcing` adapter.

Turns a `Model` into a runnable Django app (CQRS, event sourcing, pull-based
projections) whose shape is fixed by the golden reference at
`examples/todo/generated/python/`. Files are built as line lists and registered
into a `GeneratedProject`; only this package knows the target stack.
"""

from __future__ import annotations

import json

from ...feel import compile_to_python
from ...mapping import parse as parse_mapping
from ...model import Aggregate, BoundedContext, Command, Event, Feature, Model, Policy, ReadModel
from ...names import camel, singular, snake, studly, upper_const
from ...project import GeneratedProject
from . import templates as t
from .types import coerce_payload, default_literal, migration_field, model_field, py_type, zero_literal


def _file(lines: list[str]) -> str:
    return "\n".join(lines) + "\n"


def _py_literal(value: object) -> str:
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, str):
        return json.dumps(value)
    if value is None:
        return "None"
    return repr(value)


class DjangoEventSourcingAdapter:
    def name(self) -> str:
        return "django-eventsourcing-postgres"

    def description(self) -> str:
        return "Django + eventsourcing + PostgreSQL (CQRS, event sourcing, pull-based projections)."

    def slug(self) -> str:
        return "python"

    # -- name derivations ----------------------------------------------------

    def _event_domain(self, event: Event, agg: Aggregate) -> str:
        prefix = f"{agg.name}-"
        core = event.name[len(prefix):] if event.name.startswith(prefix) else event.name
        return studly(core)

    def _event_ref(self, event: Event, agg: Aggregate) -> str:
        return f"{studly(agg.name)}.{self._event_domain(event, agg)}"

    def _evolve_method(self, event: Event, agg: Aggregate) -> str:
        return "_" + snake(self._event_domain(event, agg))

    def _agg_method(self, cmd: Command, agg: Aggregate) -> str:
        tokens = snake(cmd.name).split("_")
        agg_token = snake(agg.name)
        filtered = [tok for tok in tokens if tok != agg_token] or tokens
        return "_".join(filtered)

    def _id_param(self, entity: str) -> str:
        return f"{snake(singular(entity))}_id"

    def _load_id_param(self, agg: Aggregate, cmd: Command) -> str:
        # The aggregate-id param can collide with a same-named command data field
        # (e.g. `accountId` on the `account` aggregate -> both snake to `account_id`);
        # rename the id param so the field keeps its own value and the signature stays valid.
        id_param = self._id_param(agg.name)
        taken = {snake(f.name) for f in cmd.data if not f.is_identity}
        while id_param in taken:
            id_param = "aggregate_id" if id_param != "aggregate_id" else id_param + "_"
        return id_param

    def _rm_class(self, rm: ReadModel) -> str:
        return "Rm" + singular(studly(rm.name))

    def _rm_table(self, rm: ReadModel) -> str:
        return "rm_" + snake(rm.name)

    def _app_class(self, model: Model) -> str:
        return studly(model.domain) + "App"

    def _path(self, context: str, name: str) -> str:
        return f"/{context}/{name}"

    # -- orchestration -------------------------------------------------------

    def generate(self, model: Model, options: dict | None = None) -> GeneratedProject:
        options = options or {}
        project = GeneratedProject()
        primary = next((c for c in model.bounded_contexts if c.aggregates), None)
        has_policies = bool(self._resolvable_policies(model))

        for context in model.bounded_contexts:
            project.add(f"{context.name}/__init__.py", "")
            project.add(f"{context.name}/apps.py", self._apps(context))
            if context.aggregates:
                project.add(f"{context.name}/domain.py", self._domain(context.aggregates))
            if context.read_models:
                project.add(f"{context.name}/models.py", self._models(context))
                project.add(f"{context.name}/migrations/__init__.py", "")
                project.add(f"{context.name}/migrations/0001_initial.py", self._migration(context))
                project.add(f"{context.name}/projections.py", self._projections(context))
                project.add(f"{context.name}/finders.py", self._finders(context))
            if context.aggregates or context.queries:
                project.add(f"{context.name}/urls.py", self._urls(context))
                project.add(f"{context.name}/views.py", self._views(context, react=has_policies))

        if primary is not None:
            project.add(f"{primary.name}/application.py", self._application(model))

        project.add("config/__init__.py", "")
        project.add("config/settings.py", self._settings(model))
        project.add("config/urls.py", self._config_urls(model))
        project.add("config/wsgi.py", t.WSGI)

        project.add("shared/__init__.py", "")
        project.add("shared/errors.py", t.ERRORS)
        project.add("shared/cors.py", t.CORS)
        project.add("shared/runtime.py", self._runtime(model, primary))
        if has_policies:
            project.add("shared/reactions.py", self._reactions(model))
        project.add("shared/management/__init__.py", "")
        project.add("shared/management/commands/__init__.py", "")
        project.add("shared/management/commands/eventstore_hashchain.py", t.HASHCHAIN_COMMAND)
        project.add("shared/management/commands/eventstore_verify.py", t.VERIFY_COMMAND)

        project.add("dev/__init__.py", "")
        project.add("dev/views.py", self._dev_views(model))
        project.add("dev/urls.py", t.DEV_URLS)
        project.add("dev/catalog.json", self._catalog(model))
        project.add("dev/source.bpmn", str(options.get("bpmnSource") or ""))

        project.add("tests/__init__.py", "")
        for feature in model.features:
            project.add(f"tests/test_{snake(feature.name)}.py", self._test(model, feature))

        project.add("manage.py", t.MANAGE)
        project.add("conftest.py", t.CONFTEST)
        project.add("pytest.ini", t.PYTEST_INI)
        project.add("requirements.txt", t.REQUIREMENTS)
        project.add(".env.example", t.ENV_EXAMPLE)
        project.add("Dockerfile", t.DOCKERFILE)
        project.add("compose.yaml", t.COMPOSE)
        project.add("Makefile", t.MAKEFILE)
        project.add("README.md", self._readme(model))
        return project

    # -- write side ----------------------------------------------------------

    def _domain(self, aggregates: list[Aggregate]) -> str:
        # One module per bounded context: shared header/imports once, then every
        # aggregate's class (a context with >1 aggregate keeps them all).
        subject = (
            f"`{aggregates[0].name}` aggregate"
            if len(aggregates) == 1
            else f"`{aggregates[0].bounded_context}` context"
        )
        lines = [
            f'"""Write model for the {subject}.',
            "",
            "Decide/evolve are kept apart, exactly as the ESDM model separates them: the",
            "public command methods *decide* (admit against the 0001 state machine, then",
            "trigger), the `@event`-decorated privates *evolve* (mutate state, and re-run on",
            "replay). Never put admit checks in an evolve method — it re-runs on every load.",
            '"""',
            "",
            "from __future__ import annotations",
            "",
            "from eventsourcing.domain import Aggregate, event",
        ]
        err = []
        if any(a.state_machine is not None for a in aggregates):
            err.append("IllegalTransition")
        if any(self._guard_expr(a, c) for a in aggregates for c in a.commands):
            err.append("GuardViolation")
        if err:
            lines += ["", f"from shared.errors import {', '.join(err)}"]
        if any(self._carries_identity(a) for a in aggregates):
            lines += ["", "from uuid import NAMESPACE_URL, UUID, uuid4, uuid5"]

        body: list[str] = []
        for aggregate in aggregates:
            if self._carries_identity(aggregate):
                body += ["", "", self._identity_namespace(aggregate)]
            body += ["", ""] + self._domain_class(aggregate)
        return _file(lines + body)

    def _carries_identity(self, agg: Aggregate) -> bool:
        # A create whose command declares the identity field is caller-supplied (a
        # policy passes through its source aggregate's id, or app code composes one).
        create = agg.create_command()
        return create is not None and create.data.has(agg.identity_field)

    def _identity_namespace(self, agg: Aggregate) -> str:
        const = f"_{upper_const(agg.name)}_NS"
        return f'{const} = uuid5(NAMESPACE_URL, "{agg.bounded_context}/{agg.name}")'

    def _identity_param_name(self, agg: Aggregate) -> str:
        # the reserved carried-identity param must not collide with a model field
        # that also snakes to esdm_id (duplicate ctor argument = SyntaxError)
        taken = {snake(f.name) for f in agg.non_identity_state()}
        create = agg.create_command()
        if create is not None:
            taken |= {snake(f.name) for f in create.data}
        for event in agg.events:
            taken |= {snake(f.name) for f in event.data}
        name = "esdm_id"
        while name in taken:
            name += "_"
        return name

    def _domain_class(self, agg: Aggregate) -> list[str]:
        cls = studly(agg.name)
        sm = agg.state_machine
        parts: list[list[str]] = []
        if sm is not None:
            parts.append([f'    {upper_const(state.name)} = "{state.name}"' for state in sm.states])

        create = agg.create_command()
        for cmd in agg.commands:
            event = agg.event(cmd.primary_event() or "")
            if event is None:
                continue
            if cmd is create:
                parts.append(self._ctor(agg, cmd, event))
            else:
                parts.append(self._command_method(agg, cmd, event))
                parts.append(self._evolve(agg, event))
        if sm is not None:
            parts.append(self._admit(agg))

        body: list[str] = [f"class {cls}(Aggregate):"]
        for index, part in enumerate(parts):
            if index > 0:
                body.append("")
            body += part
        return body

    def _ctor(self, agg: Aggregate, cmd: Command, event: Event) -> list[str]:
        # __init__ is called with the CREATE COMMAND's fields; event-only fields
        # (e.g. a defaulted status) become defaulted keyword params so the stored
        # event still carries them (C4: found via the orders conformance scenario).
        cmd_carried = {f.name for f in cmd.data if not f.is_identity}
        create_fields = sorted(
            (f for f in event.data if not f.is_identity),
            key=lambda f: f.name not in cmd_carried,
        )
        params = "".join(
            f", {snake(f.name)}: {py_type(f)}"
            if f.name in cmd_carried
            else f", {snake(f.name)}: {py_type(f)} = {default_literal(f)}"
            for f in create_fields
        )
        carries_id = self._carries_identity(agg)
        id_param = self._identity_param_name(agg)
        if carries_id:
            params += f', {id_param}: str = ""'
        lines = [f'    @event("{self._event_domain(event, agg)}")', f"    def __init__(self{params}) -> None:"]
        carried = {f.name for f in create_fields}
        body: list[str] = []
        for field in agg.non_identity_state():
            value = snake(field.name) if field.name in carried else default_literal(field)
            body.append(f"        self.{snake(field.name)} = {value}")
        if carries_id:
            body.append(f"        self.{id_param} = {id_param}")
        if agg.state_machine is not None:
            body.append(f"        self.status = self.{upper_const(agg.state_machine.initial)}")
        if not body:
            body.append("        pass")  # identity-only state carries nothing, but @event still records the params
        if carries_id:
            # streams are keyed by originator UUID alone, so the model identity (any string,
            # possibly another aggregate's id) maps to a derived stream id; equal identity ->
            # equal stream -> a redelivered create dedups on save (IntegrityError).
            body += [
                "",
                "    @classmethod",
                f'    def create_id(cls, {id_param}: str = "", **_) -> UUID:',
                f"        return uuid5(_{upper_const(agg.name)}_NS, {id_param}) if {id_param} else uuid4()",
            ]
        return lines + body

    def _command_method(self, agg: Aggregate, cmd: Command, event: Event) -> list[str]:
        method = self._agg_method(cmd, agg)
        params = "".join(f", {snake(f.name)}: {py_type(f)}" for f in cmd.data if not f.is_identity)
        lines = [f"    def {method}(self{params}) -> None:"]
        sm = agg.state_machine
        if sm is not None and sm.admit_for(cmd.name) is not None:
            lines.append(f'        self._admit("{cmd.name}")')
        guard = self._guard_expr(agg, cmd)
        if guard is not None:
            expr, requirement, prelude = guard
            lines += [f"        {line}" for line in prelude]
            lines.append(f"        if not ({expr}):")
            lines.append(f'            raise GuardViolation("{cmd.name}", {json.dumps(requirement)})')
        carried = {f.name for f in cmd.data}
        args, from_state = [], []
        for field in event.data:
            if field.is_identity:
                continue
            if field.name in carried:
                args.append(snake(field.name))
            elif agg.state.has(field.name):
                args.append(f"self.{snake(field.name)}")
                from_state.append(field.name)
            else:
                args.append(default_literal(field))
        if self._carries_identity(agg):
            # mutate events carry the model identity too, so folds key rows by it
            args.append(f"self.{self._identity_param_name(agg)}")
        if from_state:
            lines.append(f"        # carry current {', '.join(from_state)} into the event")
        lines.append(f"        self.{self._evolve_method(event, agg)}({', '.join(args)})")
        return lines

    def _evolve(self, agg: Aggregate, event: Event) -> list[str]:
        params = "".join(f", {snake(f.name)}: {py_type(f)}" for f in event.data if not f.is_identity)
        if self._carries_identity(agg):
            params += f', {self._identity_param_name(agg)}: str = ""'
        lines = [
            f'    @event("{self._event_domain(event, agg)}")',
            f"    def {self._evolve_method(event, agg)}(self{params}) -> None:",
        ]
        body: list[str] = []
        if event.lifecycle.value == "delete":
            body.append("        # soft delete: the projection removes the row, the event stream stays intact")
        else:
            for field in event.data:
                if not field.is_identity and agg.state.has(field.name):
                    body.append(f"        self.{snake(field.name)} = {snake(field.name)}")
        sm = agg.state_machine
        if sm is not None:
            target = sm.transition_target(event.name)
            if target is not None and event.lifecycle.value != "create":
                body.append(f"        self.status = self.{upper_const(target)}")
        if not any(line.strip() and not line.strip().startswith("#") for line in body):
            body.append("        pass")  # a comment alone is not a statement
        return lines + body

    def _admit(self, agg: Aggregate) -> list[str]:
        # Per-command from-states: the union of all admitting states would wrongly
        # admit command A from a state only command B allows (C4: orders scenario).
        entries = []
        for admit in agg.state_machine.admits:
            # class-body scope: the state constants are bare names here, not self.*
            rendered = ", ".join(upper_const(s) for s in admit.from_states)
            trailing = "," if len(admit.from_states) == 1 else ""
            entries.append(f'        "{admit.command}": ({rendered}{trailing}),')
        return [
            "    _ADMITS = {",
            *entries,
            "    }",
            "",
            "    def _admit(self, command: str) -> None:",
            "        if self.status not in self._ADMITS[command]:",
            "            raise IllegalTransition(command, self.status)",
        ]

    # receiver the compiled FEEL guards read state from (`state` in pure-fold targets)
    guard_receiver = "self"

    def _guard_expr(self, agg: Aggregate, cmd: Command) -> tuple[str, str] | None:
        sm = agg.state_machine
        if sm is None:
            return None
        admit = sm.admit_for(cmd.name)
        if admit is None or not admit.when:
            return None
        return feel_guard(admit.when, self.guard_receiver)

    def _application(self, model: Model) -> str:
        app_class = self._app_class(model)
        aggregates = model.aggregates()
        # UUID() appears only in loads of minted-identity streams; carried-identity
        # mutates resolve through create_id instead
        has_mutation = any(
            c is not a.create_command() and not self._carries_identity(a)
            for a in aggregates
            for c in a.commands
        )
        lines = [
            f'"""Application service for the `{model.domain}` domain.',
            "",
            "One method per command: load-or-create the aggregate, invoke the domain method",
            "(which admits + records events), append the events to the store. The read side",
            "is projected separately (see `projections.py`).",
            '"""',
            "",
            "from __future__ import annotations",
            "",
        ]
        if has_mutation:
            lines += ["from uuid import UUID", ""]
        lines.append("from eventsourcing.application import Application")
        lines.append("")
        for aggregate in aggregates:
            lines.append(f"from {aggregate.bounded_context}.domain import {studly(aggregate.name)}")
        lines += ["", "", f"class {app_class}(Application):"]

        blocks: list[list[str]] = []
        for aggregate in aggregates:
            create = aggregate.create_command()
            for cmd in aggregate.commands:
                blocks.append(self._service_method(aggregate, cmd, cmd is create))
        body: list[str] = []
        for index, block in enumerate(blocks):
            if index > 0:
                body.append("")
            body += block
        return _file(lines + body)

    def _service_method(self, agg: Aggregate, cmd: Command, is_create: bool) -> list[str]:
        cls = studly(agg.name)
        var = snake(agg.name)
        method = snake(cmd.name)
        if is_create:
            carries_id = self._carries_identity(agg)
            params = "".join(
                f", {snake(f.name)}: {py_type(f)}"
                for f in cmd.data
                if carries_id or not f.is_identity
            )
            args = ", ".join(snake(f.name) for f in cmd.data if not f.is_identity)
            if carries_id:
                id_attr = snake(agg.identity_field)
                id_kw = self._identity_param_name(agg)
                call = f"{cls}({args}, {id_kw}={id_attr})" if args else f"{cls}({id_kw}={id_attr})"
                return [
                    f"    def {method}(self{params}) -> str:",
                    f"        {var} = {call}",
                    f"        self.save({var})",
                    f"        return {id_attr} or str({var}.id)",
                ]
            return [
                f"    def {method}(self{params}) -> str:",
                f"        {var} = {cls}({args})",
                f"        self.save({var})",
                f"        return str({var}.id)",
            ]
        id_param = self._load_id_param(agg, cmd)
        params = f", {id_param}: str" + "".join(
            f", {snake(f.name)}: {py_type(f)}" for f in cmd.data if not f.is_identity
        )
        args = ", ".join(snake(f.name) for f in cmd.data if not f.is_identity)
        if self._carries_identity(agg):
            # carried-identity streams are keyed by create_id(model id), never the raw
            # UUID form - and the model id may not be a UUID at all (composed ids)
            load = f"{cls}.create_id({self._identity_param_name(agg)}={id_param})"
        else:
            load = f"UUID({id_param})"
        return [
            f"    def {method}(self{params}) -> None:",
            f"        {var}: {cls} = self.repository.get({load})",
            f"        {var}.{self._agg_method(cmd, agg)}({args})",
            f"        self.save({var})",
        ]

    # -- read side -----------------------------------------------------------

    def _models(self, context: BoundedContext) -> str:
        lines = [
            '"""Read-model tables for the `' + context.name + "` context, plus the projector's cursor.",
            "",
            "These are materialized *views*, not the source of truth — the event store is.",
            "Each is rebuildable by replaying the log from notification id 0.",
            '"""',
            "",
            "from __future__ import annotations",
            "",
            "from django.db import models",
        ]
        for rm in context.read_models:
            pk = rm.primary_key()
            lines += ["", "", f"class {self._rm_class(rm)}(models.Model):"]
            for column in rm.columns:
                lines.append(f"    {snake(column.name)} = {model_field(column, column.name == pk)}")
            lines += ["", "    class Meta:", f'        db_table = "{self._rm_table(rm)}"']
        lines += [
            "",
            "",
            "class ProjectionPosition(models.Model):",
            "    name = models.CharField(primary_key=True, max_length=255)",
            "    last_id = models.BigIntegerField(default=0)",
            "",
            "    class Meta:",
            f'        db_table = "{context.name}_projection_position"',
        ]
        return _file(lines)

    def _migration(self, context: BoundedContext) -> str:
        lines = [
            "from django.db import migrations, models",
            "",
            "",
            "class Migration(migrations.Migration):",
            "",
            "    initial = True",
            "",
            "    dependencies = []",
            "",
            "    operations = [",
        ]
        for rm in context.read_models:
            pk = rm.primary_key()
            lines.append("        migrations.CreateModel(")
            lines.append(f'            name="{self._rm_class(rm)}",')
            lines.append("            fields=[")
            for column in rm.columns:
                lines.append(
                    f'                ("{snake(column.name)}", {migration_field(column, column.name == pk)}),'
                )
            lines.append("            ],")
            lines.append(f'            options={{"db_table": "{self._rm_table(rm)}"}},')
            lines.append("        ),")
        lines += [
            "        migrations.CreateModel(",
            '            name="ProjectionPosition",',
            "            fields=[",
            '                ("name", models.CharField(max_length=255, primary_key=True, serialize=False)),',
            '                ("last_id", models.BigIntegerField(default=0)),',
            "            ],",
            f'            options={{"db_table": "{context.name}_projection_position"}},',
            "        ),",
            "    ]",
        ]
        return _file(lines)

    def _projections(self, context: BoundedContext) -> str:
        rm_classes = ", ".join(sorted(self._rm_class(rm) for rm in context.read_models))
        aggregates = context.aggregates
        agg_import = ", ".join(studly(a.name) for a in aggregates)
        id_var = self._id_param(aggregates[0].name) if aggregates else "aggregate_id"
        lines = [
            '"""Read-side projector for the `' + context.name + "` context.",
            "",
            "Pull-based and synchronous: it walks the application's notification log from the",
            "cursor forward and folds each event into the read-model tables, advancing the",
            "cursor in the same transaction. The HTTP edge calls `project(app)` after every",
            "command and before every query, so reads are consistent with the caller's own",
            "writes (the async worker of the Symfony target is not needed here).",
            "",
            "Rebuild from scratch by deleting the row in `projection_position` (and the",
            "read-model tables) and calling `project` again — the log is the source of truth.",
            '"""',
            "",
            "from __future__ import annotations",
            "",
            "from django.db import transaction",
            "",
        ]
        if agg_import:
            lines.append(f"from {context.name}.domain import {agg_import}")
        lines += [
            f"from {context.name}.models import {', '.join(sorted(['ProjectionPosition', *[self._rm_class(rm) for rm in context.read_models]]))}",
            "",
            f'PROJECTION = "{context.name}"',
            "_BATCH = 100",
            "",
            "",
            t.PROJECT_FN,
            "",
            "",
            "def _fold(event) -> None:",
            f"    {id_var} = str(event.originator_id)",
        ]
        branches = self._fold_branches(context, id_var)
        lines += branches
        return _file(lines)

    def _fold_branches(self, context: BoundedContext, id_var: str) -> list[str]:
        # events in aggregate declaration order that at least one read model projects
        events: list[tuple[Aggregate, Event]] = []
        for aggregate in context.aggregates:
            for event in aggregate.events:
                if any(rm.projects_event(event.name) for rm in context.read_models):
                    events.append((aggregate, event))
        lines: list[str] = []
        for index, (aggregate, event) in enumerate(events):
            keyword = "if" if index == 0 else "elif"
            if index == 0:
                lines.append("")
            lines.append(f"    {keyword} isinstance(event, {self._event_ref(event, aggregate)}):")
            if self._carries_identity(aggregate):
                # the row is keyed by the model identity, not the derived stream id;
                # getattr keeps replays of pre-carried-identity history working
                id_param = self._identity_param_name(aggregate)
                lines.append(f'        {id_var} = getattr(event, "{id_param}", "") or {id_var}')
            for rm in context.read_models:
                projection = next((p for p in rm.projections if p.event == event.name), None)
                if projection is None:
                    continue
                lines += self._fold_op(rm, aggregate, event, projection, id_var)
        return lines

    def _fold_op(self, rm: ReadModel, agg: Aggregate, event: Event, projection, id_var: str) -> list[str]:
        cls = self._rm_class(rm)
        pk = rm.primary_key()
        op = (projection.rule or "").strip().split(" ")[0].lower() if projection.rule else event.lifecycle.value
        op = {"insert": "insert", "update": "update", "delete": "delete", "create": "insert", "mutate": "update"}.get(op, "update")
        # the identity never rides as an event attribute (it IS the stream/id_var);
        # a same-named non-pk column maps to id_var, not to a phantom event.<id>
        carried = {f.name for f in event.data if not f.is_identity}

        def column_value(column) -> str:
            if column.name == agg.identity_field:
                return id_var
            if column.name in carried:
                return f"event.{snake(column.name)}"
            return default_literal(column)

        key = id_var if pk == agg.identity_field or pk not in carried else f"event.{snake(pk)}"
        if op == "delete":
            return [f"        {cls}.objects.filter({snake(pk)}={key}).delete()"]
        if op == "insert":
            defaults = [
                f'"{snake(column.name)}": {column_value(column)}'
                for column in rm.columns
                if column.name != pk
            ]
            return [
                f"        {cls}.objects.update_or_create(",
                f"            {snake(pk)}={key}, defaults={{{', '.join(defaults)}}}",
                "        )",
            ]
        assigns = [
            f"{snake(column.name)}={column_value(column)}"
            for column in rm.columns
            if column.name != pk and (column.name in carried or column.name == agg.identity_field)
        ]
        return [f"        {cls}.objects.filter({snake(pk)}={key}).update({', '.join(assigns)})"]

    def _finders(self, context: BoundedContext) -> str:
        used = sorted({self._rm_class(context.read_model(q.read_model)) for q in context.queries if context.read_model(q.read_model)})
        lines = [
            '"""Query helpers over the read-model tables. Row keys are the app\'s own naming',
            '(what `/_dev/catalog` advertises as the read-model columns)."""',
            "",
            "from __future__ import annotations",
            "",
            f"from {context.name}.models import {', '.join(used)}",
        ]
        blocks: list[list[str]] = []
        for query in context.queries:
            rm = context.read_model(query.read_model)
            if rm is None:
                continue
            blocks.append(self._finder(query, rm))
        for block in blocks:
            lines += ["", ""] + block
        return _file(lines)

    def _finder(self, query, rm: ReadModel) -> list[str]:
        cls = self._rm_class(rm)
        pk = rm.primary_key()
        fn = snake(query.name)
        row = "{" + ", ".join(f'"{c.name}": row.{snake(c.name)}' for c in rm.columns) + "}"
        if query.is_get():
            param = self._id_param(rm.name)
            return [
                f"def {fn}({param}: str) -> dict | None:",
                f"    row = {cls}.objects.filter({pk}={param}).first()",
                "    if row is None:",
                "        return None",
                f"    return {row}",
            ]
        return [
            f"def {fn}() -> list[dict]:",
            "    return [",
            f"        {row}",
            f'        for row in {cls}.objects.all().order_by("{pk}")',
            "    ]",
        ]

    # -- HTTP edge -----------------------------------------------------------

    def _views(self, context: BoundedContext, react: bool = False) -> str:
        lines = [
            '"""HTTP edge for the `' + context.name + "` context — the uniform domain surface (0004 §1).",
            "",
            "    POST /" + context.name + "/<command>   JSON body per the command's fields",
            "    GET  /" + context.name + "/<query>     list -> array, get -> one row (params via query string)",
            "",
            'A rejected domain rule (0001 transition / 0002 guard) is 409 with `{ "error": ... }`.',
            "Commands project synchronously so the caller reads its own writes.",
            '"""',
            "",
            "from __future__ import annotations",
            "",
            "import json",
            "",
            "from django.http import HttpResponse, JsonResponse",
            "from django.views.decorators.csrf import csrf_exempt",
            "from django.views.decorators.http import require_http_methods",
            "",
            "from shared.errors import DomainViolation",
            "from shared.runtime import get_app",
            *(["from shared import reactions"] if react else []),
            *([f"from {context.name} import finders, projections"] if context.read_models else []),
            "",
            t.VIEWS_HELPERS % {
                "react": "    reactions.react(app)\n" if react else "",
                "project": "    projections.project(app)\n" if context.read_models else "",
            },
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

    def _command_view(self, cmd: Command, is_create: bool) -> list[str]:
        fn = snake(cmd.name)
        call_args = ", ".join(
            coerce_payload(f, f'data.get("{f.name}", {zero_literal(f)})') for f in cmd.data
        )
        lines = [
            "@csrf_exempt",
            '@require_http_methods(["POST"])',
            f"def {fn}(request):",
            "    app = get_app()",
            "    data = _payload(request)",
            f'    result = _run(app, lambda: app.{fn}({call_args}), "{cmd.name}")',
        ]
        if is_create:
            lines += [
                "    if isinstance(result, HttpResponse):",
                "        return result",
                '    return JsonResponse({"id": result})',
            ]
        else:
            lines.append('    return result if isinstance(result, HttpResponse) else JsonResponse({"id": str(data.get("id", ""))})')
        return lines

    def _query_view(self, query) -> list[str]:
        fn = snake(query.name)
        lines = ['@require_http_methods(["GET"])', f"def {fn}(request):", "    projections.project(get_app())"]
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

    def _urls(self, context: BoundedContext) -> str:
        lines = ["from django.urls import path", "", f"from {context.name} import views", "", "urlpatterns = ["]
        for aggregate in context.aggregates:
            for cmd in aggregate.commands:
                fn = snake(cmd.name)
                lines.append(f'    path("{cmd.name}", views.{fn}, name="{context.name}_{fn}"),')
        for query in context.queries:
            fn = snake(query.name)
            lines.append(f'    path("{query.name}", views.{fn}, name="{context.name}_{fn}"),')
        lines.append("]")
        return _file(lines)

    def _apps(self, context: BoundedContext) -> str:
        return _file([
            "from django.apps import AppConfig",
            "",
            "",
            f"class {studly(context.name)}Config(AppConfig):",
            '    default_auto_field = "django.db.models.BigAutoField"',
            f'    name = "{context.name}"',
        ])

    # -- project-level files -------------------------------------------------

    # -- policies --------------------------------------------------------------

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

    def _reactions(self, model: Model) -> str:
        policies = self._resolvable_policies(model)
        anchor = next((c.name for c in model.bounded_contexts if c.read_models), None)
        if anchor is None:
            # without a cursor table, every react() rescans the log; that is only safe
            # when redelivery dedups - i.e. every emitted create carries its identity
            for policy in policies:
                emit = model.aggregate(policy.emit_context, policy.emit_aggregate)
                if not self._carries_identity(emit):
                    raise ValueError(
                        f"policy '{policy.name}' emits '{policy.emit_command}' whose create does not "
                        f"carry the aggregate identity, and the model has no read models to anchor a "
                        f"reaction cursor - redelivery would duplicate aggregates. Add a read model or "
                        f"declare the identity field in the command data."
                    )
        handle_imports = sorted({(p.handle_context, studly(p.handle_aggregate)) for p in policies})
        lines = [
            f'"""Policy reactor for the `{model.domain}` domain.',
            "",
            "Pull-based and synchronous, like the read-side projector: after every command",
            "the HTTP edge calls `react(app)`, which walks the notification log from its own",
            "cursor and dispatches each policy's follow-up command. At-least-once: a failed",
            "dispatch leaves the cursor BEFORE its event, so the next request retries it;",
            'a redelivered event dedups on the emitted aggregate\'s deterministic id."""',
            "",
            "from __future__ import annotations",
            "",
            "from django.db import transaction",
            "from eventsourcing.persistence import IntegrityError",
            "",
        ]
        if anchor is not None:
            lines.append(f"from {anchor}.models import ProjectionPosition")
        for context, cls in handle_imports:
            lines.append(f"from {context}.domain import {cls}")
        lines += [
            "",
            *(['POSITION = "__reactions__"'] if anchor is not None else []),
            "_BATCH = 100",
            "",
            "",
            t.REACT_FN if anchor is not None else t.REACT_FN_NO_CURSOR,
            "",
            "",
            "def _dispatch(app, event) -> bool:",
            "    ok = True",
        ]
        for policy in policies:
            handle = model.aggregate(policy.handle_context, policy.handle_aggregate)
            handle_event = handle.event(policy.handle_event)
            # plain `if` per policy: two policies may react to the same event
            lines.append(f"    if isinstance(event, {self._event_ref(handle_event, handle)}):")
            lines.append(f"        ok = _{snake(policy.name)}(app, event) and ok")
        lines.append("    return ok")
        for policy in policies:
            lines += ["", ""] + self._reaction_fn(model, policy)
        return _file(lines)

    def _reaction_fn(self, model: Model, policy: Policy) -> list[str]:
        handle = model.aggregate(policy.handle_context, policy.handle_aggregate)
        emit = model.aggregate(policy.emit_context, policy.emit_aggregate)
        command = next(c for c in emit.commands if c.name == policy.emit_command)
        event = handle.event(policy.handle_event)
        is_create = command is emit.create_command()

        # the source id is the handle aggregate's MODEL identity: its carried id when
        # caller-supplied, else its stream id (which is the model id for minted streams)
        if self._carries_identity(handle):
            source_id = f'(getattr(event, "{self._identity_param_name(handle)}", "") or str(event.originator_id))'
        else:
            source_id = "str(event.originator_id)"
        # A declared mapping (proposal 0005) wins per field; the rest falls back to the
        # convention below, which is what that proposal documents as the default.
        mapping = parse_mapping(policy.mapping) if policy.mapping else {}
        # The handled aggregate's identity is not a payload attribute on this target - it is the
        # stream id - so it resolves exactly as the fallback below resolves it.
        resolve = lambda name: (  # noqa: E731
            source_id if name == handle.identity_field else f"event.{snake(name)}"
        )

        args = []
        if not is_create:
            args.append(source_id)
        for field in command.data:
            if field.name in mapping:
                args.append(compile_to_python(mapping[field.name], resolve)[0])
                continue
            if field.is_identity:
                # a policy-minted create inherits the source aggregate's id (usage.id = job.id)
                if is_create:
                    args.append(source_id)
                continue
            if snake(field.name) == snake(f"{policy.handle_aggregate}-id"):
                args.append(source_id)
            elif event.data.has(field.name):
                args.append(f"event.{snake(field.name)}")
            else:
                args.append(default_literal(field))

        return [
            f"def _{snake(policy.name)}(app, event) -> bool:",
            "    try:",
            "        with transaction.atomic():  # savepoint: a dedup rollback must not poison the outer tx",
            f"            app.{snake(command.name)}({', '.join(args)})",
            "    except IntegrityError:",
            "        pass  # deterministic id: this event was already reacted to (at-least-once redelivery)",
            "    except Exception as error:",
            f'        print(f"policy {policy.name} failed to dispatch {command.name} ({{error}})", flush=True)',
            "        return False",
            "    return True",
        ]

    # -- project-level files ---------------------------------------------------

    def _settings(self, model: Model) -> str:
        # `shared` is an app so manage.py finds the eventstore_* commands
        apps = ['"eventsourcing_django"', '"shared"'] + [f'"{c.name}"' for c in model.bounded_contexts]
        installed = "\n".join(f"    {a}," for a in apps)
        return _file([
            '"""Django settings for the generated `' + model.domain + "` app.",
            "",
            "Deliberately lean: no auth/admin/templates — this is an event-sourced CQRS",
            "service, not a page app. The event store is a Django-ORM table (via",
            "`eventsourcing_django`); the read models are the `rm_*` tables.",
            '"""',
            "",
            "from __future__ import annotations",
            "",
            "import os",
            "from pathlib import Path",
            "",
            "BASE_DIR = Path(__file__).resolve().parent.parent",
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
            "DATABASES = {",
            '    "default": {',
            '        "ENGINE": "django.db.backends.postgresql",',
            '        "NAME": os.environ.get("DB_NAME", "app"),',
            '        "USER": os.environ.get("DB_USER", "app"),',
            '        "PASSWORD": os.environ.get("DB_PASSWORD", "app"),',
            '        "HOST": os.environ.get("DB_HOST", "db"),',
            '        "PORT": os.environ.get("DB_PORT", "5432"),',
            "    }",
            "}",
            "",
            'DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"',
            "USE_TZ = True",
        ])

    def _config_urls(self, model: Model) -> str:
        lines = ["from django.urls import include, path", "", "urlpatterns = ["]
        for context in model.bounded_contexts:
            if context.aggregates or context.queries:
                lines.append(f'    path("{context.name}/", include("{context.name}.urls")),')
        lines += ['    path("_dev/", include("dev.urls")),', "]"]
        return _file(lines)

    def _runtime(self, model: Model, primary: BoundedContext | None) -> str:
        app_class = self._app_class(model)
        ctx = primary.name if primary is not None else "app"
        return _file([
            '"""Process-wide singletons that need Django to be configured first.',
            "",
            "The eventsourcing `Application` is built lazily (after `django.setup()`), so its",
            "Django-backed recorder can bind to the configured database. `PERSISTENCE_MODULE`",
            "selects `eventsourcing_django`; the event store lives in Django's own DB.",
            '"""',
            "",
            "from __future__ import annotations",
            "",
            "import threading",
            "",
            "_lock = threading.Lock()",
            "_app = None",
            "",
            "",
            "def get_app():",
            "    global _app",
            "    if _app is None:",
            "        with _lock:",
            "            if _app is None:",
            f"                from {ctx}.application import {app_class}",
            "",
            f'                _app = {app_class}(env={{"PERSISTENCE_MODULE": "eventsourcing_django"}})',
            "    return _app",
        ])

    def _dev_views(self, model: Model) -> str:
        aggregates = model.aggregates()
        lines = [
            '"""Dev-only window onto the app for an external domain console (0004): the model',
            "catalog, the authoring BPMN, and the raw event stream. Not part of the domain",
            'API — gate it out of production."""',
            "",
            "from __future__ import annotations",
            "",
            "import json",
            "from datetime import datetime",
            "from pathlib import Path",
            "from uuid import UUID",
            "",
            "from django.http import HttpResponse, JsonResponse",
            "",
            "from shared.runtime import get_app",
        ]
        for aggregate in aggregates:
            lines.append(f"from {aggregate.bounded_context}.domain import {studly(aggregate.name)}")
        lines += [
            "",
            "_HERE = Path(__file__).resolve().parent",
            "",
            "# domain event class -> (aggregate, ESDM event name, carried-identity attr) rows",
            "_EVENTS = {",
        ]
        for aggregate in aggregates:
            id_attr = (
                f'"{self._identity_param_name(aggregate)}"'
                if self._carries_identity(aggregate)
                else "None"
            )
            for event in aggregate.events:
                lines.append(
                    f'    {self._event_ref(event, aggregate)}: ("{aggregate.name}", "{event.name}", {id_attr}),'
                )
        lines.append("}")
        fallback = _py_literal(aggregates[0].name) if aggregates else '"aggregate"'
        lines.append(t.DEV_VIEWS_TAIL % {"fallback": fallback})
        return _file(lines)

    def _catalog(self, model: Model) -> str:
        contexts = []
        for context in model.bounded_contexts:
            commands = []
            for aggregate in context.aggregates:
                sm = aggregate.state_machine
                for cmd in aggregate.commands:
                    guard = None
                    if cmd.lifecycle.value != "create" and sm is not None:
                        admit = sm.admit_for(cmd.name)
                        if admit is not None:
                            guard = {"from": list(admit.from_states), "when": admit.when}
                    commands.append({
                        "name": cmd.name,
                        "lifecycle": cmd.lifecycle.value,
                        "path": self._path(context.name, cmd.name),
                        "fields": [{"name": f.name, "type": f.json_type, "feel": None} for f in cmd.data],
                        "guard": guard,
                    })
            queries = []
            for query in context.queries:
                queries.append({
                    "name": query.name,
                    "path": self._path(context.name, query.name),
                    "kind": "get" if query.is_get() else "list",
                    "params": [{"name": p.name, "type": p.json_type} for p in query.parameters],
                    "readModel": query.read_model,
                })
            read_models = []
            for rm in context.read_models:
                list_query = next(
                    (q for q in context.queries if q.read_model == rm.name and not q.is_get()), None
                )
                read_models.append({
                    "name": rm.name,
                    "columns": [{"name": c.name, "type": c.json_type, "identity": c.is_identity} for c in rm.columns],
                    "listPath": self._path(context.name, list_query.name) if list_query else None,
                    "stateMachine": None,
                })
            contexts.append({
                "name": context.name,
                "commands": commands,
                "queries": queries,
                "readModels": read_models,
            })
        catalog = {"domain": model.domain, "contexts": contexts}
        return json.dumps(catalog, indent=4) + "\n"

    # -- tests ---------------------------------------------------------------

    def _test(self, model: Model, feature: Feature) -> str:
        aggregate = model.aggregate(feature.bounded_context, feature.aggregate)
        app_class = self._app_class(model)
        has_rejection = any(s.is_rejection() for s in feature.scenarios)
        lines = [
            '"""Write-side lifecycle tests — the GWT scenarios of the `' + feature.name + "` feature",
            "run against an in-memory (POPO) application, so no database is needed. The",
            'codegen emits one test per scenario."""',
            "",
            "from __future__ import annotations",
            "",
            "from uuid import UUID",
            "",
        ]
        if has_rejection:
            lines += ["import pytest", "", "from shared.errors import DomainViolation"]
        # the application service lives in the PRIMARY context (see generate), which
        # is not necessarily the feature's own context in a multi-context model
        primary = next(c for c in model.bounded_contexts if c.aggregates)
        lines.append(f"from {primary.name}.application import {app_class}")
        lines += [
            "",
            "",
            f"def make_app() -> {app_class}:",
            f"    return {app_class}()  # default persistence is in-memory",
        ]
        for scenario in feature.scenarios:
            lines += ["", ""] + self._scenario(aggregate, scenario)
        return _file(lines)

    def _scenario(self, aggregate: Aggregate, scenario) -> list[str]:
        lines = [f"def test_{snake(scenario.name)}():", "    app = make_app()"]
        id_var = self._id_param(aggregate.name)
        # seed given events by replaying the command that publishes each
        for example in scenario.given:
            lines.append(self._replay(aggregate, example, id_var, assign_id=example is scenario.given[0]))
        when_cmd = aggregate.command(scenario.command_name) if hasattr(aggregate, "command") else None
        when_cmd = next((c for c in aggregate.commands if c.name == scenario.command_name), None)
        create = aggregate.create_command()
        call = self._invoke(aggregate, when_cmd, scenario.command_data, id_var, is_create=when_cmd is create)
        if scenario.is_rejection():
            lines.append("    with pytest.raises(DomainViolation):")
            lines.append(f"        {call}")
            return lines
        if when_cmd is create:
            lines.append(f"    {id_var} = {call}")
        else:
            lines.append(f"    {call}")
        lines += self._assertions(aggregate, scenario, id_var)
        return lines

    def _replay(self, aggregate: Aggregate, example, id_var: str, assign_id: bool) -> str:
        cmd = self._command_for_event(aggregate, example.event)
        create = aggregate.create_command()
        call = self._invoke(aggregate, cmd, example.data, id_var, is_create=cmd is create)
        if cmd is create:
            return f"    {id_var} = {call}"
        return f"    {call}"

    def _invoke(self, aggregate: Aggregate, cmd: Command | None, data: dict, id_var: str, is_create: bool) -> str:
        if cmd is None:
            return "pass"
        method = snake(cmd.name)
        args = []
        if not is_create:
            args.append(id_var)
        for field in cmd.data:
            if field.is_identity:
                # a caller-supplied-identity create takes the id in its declared position
                if is_create and self._carries_identity(aggregate):
                    args.append(_py_literal(data.get(field.name, "")))
                continue
            args.append(_py_literal(data.get(field.name, self._zero_value(field))))
        return f"app.{method}({', '.join(args)})"

    def _assertions(self, aggregate: Aggregate, scenario, id_var: str) -> list[str]:
        checks: list[tuple[str, str, str]] = []  # (field, op, literal)
        for example in scenario.then_events:
            event = aggregate.event(example.event)
            if event is None:
                continue
            if event.lifecycle.value == "create":
                # subset-match: only assert state fields the scenario declared, never
                # force an undeclared field to a default/None.
                for field in aggregate.non_identity_state():
                    if field.name not in example.data:
                        continue
                    checks.append((snake(field.name), *self._cmp(field.json_type, example.data[field.name])))
                if aggregate.state_machine is not None:
                    checks.append(("status", "==", _py_literal(aggregate.state_machine.initial)))
            elif event.lifecycle.value == "delete":
                if aggregate.state_machine is not None:
                    target = aggregate.state_machine.transition_target(event.name)
                    if target is not None:
                        checks.append(("status", "==", _py_literal(target)))
            else:
                for field in event.data:
                    if field.is_identity or not aggregate.state.has(field.name):
                        continue
                    if field.name not in example.data:
                        continue
                    checks.append((snake(field.name), *self._cmp(field.json_type, example.data[field.name])))
        getter = f"app.repository.get(UUID({id_var}))"
        if len(checks) > 1:
            lines = [f"    task = {getter}"]
            lines += [f"    assert task.{field} {op} {literal}" for field, op, literal in checks]
            return lines
        if checks:
            field, op, literal = checks[0]
            return [f"    assert {getter}.{field} {op} {literal}"]
        return [f"    assert {getter} is not None"]

    def _cmp(self, json_type: str, value: object) -> tuple[str, str]:
        if json_type == "boolean":
            return "is", _py_literal(bool(value))
        return "==", _py_literal(value)

    def _zero_value(self, field):
        return {"string": "", "boolean": False, "integer": 0, "number": 0.0}.get(field.json_type)

    def _command_for_event(self, aggregate: Aggregate, event_name: str) -> Command | None:
        return next((c for c in aggregate.commands if c.primary_event() == event_name), None)

    # -- README --------------------------------------------------------------

    def _readme(self, model: Model) -> str:
        primary = next((c for c in model.bounded_contexts if c.aggregates), None)
        ctx = primary.name if primary is not None else "app"
        return _file([
            f"# {model.domain} (generated)",
            "",
            "Generated by **esdm-2-python** (`django-eventsourcing-postgres` target) from the",
            f"`{model.domain}` ESDM model. Do not edit by hand — change the model and regenerate.",
            "",
            "## Architecture",
            "",
            f"- **Write side** (`{ctx}/`): HTTP `POST /<context>/<command>` builds a command,",
            "  the application service (`application.py`) loads/creates the **aggregate**",
            "  (`domain.py`), the aggregate admits the command against its 0001 state machine",
            "  and records domain **events**, and the repository appends them to the",
            "  PostgreSQL event store (the `eventsourcing_django` tables).",
            f"- **Read side** (`{ctx}/projections.py`): a pull-based projector folds new events",
            "  from the notification log into the read-model tables (`rm_*`). It runs on the",
            "  request path — after every command and before every query — so reads are",
            "  consistent with the caller's own writes.",
            f"- **Query side** (`{ctx}/views.py` + `finders.py`): HTTP `GET /<context>/<query>`",
            "  reads the read-model tables.",
            "",
            "Event sourcing runtime: [`eventsourcing`](https://github.com/pyeventsourcing/eventsourcing)",
            "with the [`eventsourcing-django`](https://github.com/pyeventsourcing/eventsourcing-django)",
            "persistence module (event store in Django's own database).",
            "",
            "## Tamper evidence",
            "",
            "Every stored event is hash-chained to its predecessor **in-database**: a",
            "`BEFORE INSERT` trigger on `stored_events` (installed on boot by",
            "`manage.py eventstore_hashchain`, idempotent) computes",
            "`hash = sha256(predecessor_hash + immutable columns)`, with appends serialized",
            "by an advisory lock. A mutated, deleted or reordered event breaks every hash",
            "after it. Audit the log any time:",
            "",
            "```sh",
            "docker compose exec api python manage.py eventstore_verify",
            "# -> \"event chain intact (N events)\" and exit 0, or the first broken id and exit 1",
            "```",
            "",
            "## Run",
            "",
            "```sh",
            "docker compose up -d --build      # api migrates on boot, then serves on :8080",
            "curl -s -XPOST localhost:8080/" + ctx + "/add-task -d '{\"title\":\"Buy milk\"}'",
            "curl -s localhost:8080/" + ctx + "/list-tasks",
            "```",
            "",
            "Run the write-side lifecycle tests (in-memory, no database):",
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


def feel_guard(when: str, receiver: str = "self") -> tuple[str, str, list[str]]:
    """Compile a FEEL guard to (python-expr, human requirement, prelude lines).

    Temporal niladics compare as ISO strings — lexicographic order == date order,
    and the model's date fields are ISO strings on every stack (C4)."""
    from ...feel import compile_to_python, parse

    expr, uses_today, uses_now = compile_to_python(parse(when), lambda name: f"{receiver}.{snake(name)}")
    prelude: list[str] = []
    if uses_today or uses_now:
        prelude.append("import datetime as _dt")
    if uses_today:
        prelude.append("today = _dt.date.today().isoformat()")
    if uses_now:
        prelude.append("now = _dt.datetime.now(_dt.timezone.utc).isoformat()")
    return expr, when, prelude
