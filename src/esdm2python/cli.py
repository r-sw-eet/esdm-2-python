"""Command-line entry point: `generate` and `targets`.

`generate <app-dir>` reads `<app-dir>/esdmgen.yaml` (keys: target, model, out,
options), loads the ESDM model, runs the FEEL gate, and writes the chosen
adapter's output to `<out>/<slug>/`. Mirrors the sibling generators' CLI.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from .feel import FeelError, parse, validate
from .mapping import parse as parse_mapping
from .mapping import validate as validate_mapping
from .model import Model, create_model, load_directory
from .project import AdapterRegistry


def _load_config(app_dir: Path) -> dict:
    config_file = app_dir / "esdmgen.yaml"
    if not config_file.exists():
        return {}
    loaded = yaml.safe_load(config_file.read_text())
    return loaded if isinstance(loaded, dict) else {}



def _validate_reaction_mappings(model: Model) -> list[str]:
    """Proposal 0005: a mapping may assign only fields the emitted command declares, must produce
    every required one, and its expressions bind against the handled event's payload."""
    errors: list[str] = []
    for policy in model.policies:
        if not policy.mapping:
            continue
        handled = model.aggregate(policy.handle_context, policy.handle_aggregate)
        emitting = model.aggregate(policy.emit_context, policy.emit_aggregate)
        if handled is None or emitting is None:
            continue
        event = handled.event(policy.handle_event)
        command = next((c for c in emitting.commands if c.name == policy.emit_command), None)
        if event is None or command is None:
            continue

        try:
            mapping = parse_mapping(policy.mapping)
        except FeelError as error:
            errors.append(f"{policy.name}: {error}")
            continue

        declared = [f.name for f in command.data]
        for key in mapping:
            if key not in declared and key != emitting.identity_field:
                shown = ", ".join(declared) or "nothing"
                errors.append(
                    f'{policy.name}: "{key}" is not a field of command "{command.name}" (declared: {shown})'
                )
        for field in command.data:
            if field.required and field.name not in mapping:
                errors.append(
                    f'{policy.name}: required field "{field.name}" of command "{command.name}" '
                    "is not assigned by the mapping"
                )
        errors.extend(
            f"{policy.name}: {error}"
            for error in validate_mapping(mapping, {f.name for f in event.data})
        )

    return errors


def _validate_feel(model: Model) -> list[str]:
    errors: list[str] = []
    for aggregate in model.aggregates():
        sm = aggregate.state_machine
        if sm is None:
            continue
        allowed = {f.name for f in aggregate.state} | {"status"}
        # The arithmetic gate needs the declared types, which the binder never had.
        types = {f.name: f.json_type for f in aggregate.state}
        for admit in sm.admits:
            if not admit.when:
                continue
            try:
                errors.extend(
                    f"{aggregate.name}/{admit.command}: {msg}"
                    for msg in validate(parse(admit.when), allowed, types)
                )
            except FeelError as exc:
                errors.append(f"{aggregate.name}/{admit.command}: {exc}")
    return errors


def _generate(args: argparse.Namespace) -> int:
    app_dir = Path(args.app_dir)
    config = _load_config(app_dir)
    registry = AdapterRegistry.with_defaults()

    target = args.target or config.get("target") or registry.all()[0].name()
    model_dir = app_dir / (args.model or config.get("model", "model"))
    out_dir = Path(args.out) if args.out else app_dir / config.get("out", "generated")

    adapter = registry.get(target)

    documents = load_directory(model_dir)
    model = create_model(documents)

    feel_errors = _validate_feel(model)
    if feel_errors:
        print("FEEL guard errors:", file=sys.stderr)
        for error in feel_errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    mapping_errors = _validate_reaction_mappings(model)
    if mapping_errors:
        print("Reaction mapping errors:", file=sys.stderr)
        for error in mapping_errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    options = dict(config.get("options") or {})
    authoring = sorted(app_dir.glob("authoring/*.bpmn"))
    if authoring:
        options["bpmnSource"] = authoring[0].read_text()

    project = adapter.generate(model, options)
    destination = out_dir / adapter.slug()
    written = project.write_to(destination)
    for path in sorted(written):
        print(path)
    print(f"{len(written)} files -> {destination}")
    return 0


def _targets(args: argparse.Namespace) -> int:
    adapters = AdapterRegistry.with_defaults().all()
    if args.json:
        print(json.dumps([
            {"name": a.name(), "description": a.description(), "slug": a.slug()} for a in adapters
        ]))
        return 0
    for adapter in adapters:
        print(f"{adapter.name()}\t{adapter.description()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="esdm2python", description="ESDM → Django code generator.")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="generate an app from an ESDM model")
    gen.add_argument("app_dir", help="app directory containing esdmgen.yaml and model/")
    gen.add_argument("-t", "--target", help="adapter target id")
    gen.add_argument("-m", "--model", help="model subdirectory (default: model)")
    gen.add_argument("-o", "--out", help="output directory (default: <app-dir>/generated)")
    gen.set_defaults(func=_generate)

    tgt = sub.add_parser("targets", help="list registered adapter targets")
    tgt.add_argument("--json", action="store_true", help="output as JSON (name, description, slug)")
    tgt.set_defaults(func=_targets)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
