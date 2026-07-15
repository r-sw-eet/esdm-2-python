"""Command-line entry point: `generate` and `targets`.

`generate <app-dir>` reads `<app-dir>/esdmgen.yaml` (keys: target, model, out,
options), loads the ESDM model, runs the FEEL gate, and writes the chosen
adapter's output to `<out>/<slug>/`. Mirrors the sibling generators' CLI.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from .feel import FeelError, parse, validate
from .model import Model, create_model, load_directory
from .project import AdapterRegistry


def _load_config(app_dir: Path) -> dict:
    config_file = app_dir / "esdmgen.yaml"
    if not config_file.exists():
        return {}
    loaded = yaml.safe_load(config_file.read_text())
    return loaded if isinstance(loaded, dict) else {}


def _validate_feel(model: Model) -> list[str]:
    errors: list[str] = []
    for aggregate in model.aggregates():
        sm = aggregate.state_machine
        if sm is None:
            continue
        allowed = {f.name for f in aggregate.state} | {"status"}
        for admit in sm.admits:
            if not admit.when:
                continue
            try:
                errors.extend(
                    f"{aggregate.name}/{admit.command}: {msg}"
                    for msg in validate(parse(admit.when), allowed)
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
    registry = AdapterRegistry.with_defaults()
    for adapter in registry.all():
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
    tgt.set_defaults(func=_targets)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
