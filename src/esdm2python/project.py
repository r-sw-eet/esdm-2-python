"""The adapter seam: an in-memory file tree, the Adapter protocol, a registry.

An adapter turns a `Model` into a `GeneratedProject` (a `path -> contents` map);
the CLI writes it under `<out>/<slug>/`. Mirrors the sibling generators'
`GeneratedProject` / `Adapter` / `AdapterRegistry` trio.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from .model import Model


class GeneratedProject:
    def __init__(self) -> None:
        self._files: dict[str, str] = {}

    def add(self, relative_path: str, contents: str) -> None:
        self._files[relative_path.lstrip("/")] = contents

    def files(self) -> dict[str, str]:
        return dict(self._files)

    def write_to(self, directory: str | Path) -> list[str]:
        root = Path(directory)
        written: list[str] = []
        for relative_path, contents in self._files.items():
            target = root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents)
            written.append(str(target))
        return written


@runtime_checkable
class Adapter(Protocol):
    def name(self) -> str: ...
    def description(self) -> str: ...
    def slug(self) -> str: ...
    def generate(self, model: Model, options: dict) -> GeneratedProject: ...


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, Adapter] = {}

    def register(self, adapter: Adapter) -> "AdapterRegistry":
        self._adapters[adapter.name()] = adapter
        return self

    def get(self, name: str) -> Adapter:
        if name not in self._adapters:
            known = ", ".join(sorted(self._adapters)) or "(none)"
            raise KeyError(f"unknown target {name!r}; registered: {known}")
        return self._adapters[name]

    def all(self) -> list[Adapter]:
        return list(self._adapters.values())

    @staticmethod
    def with_defaults() -> "AdapterRegistry":
        from .adapters.django import DjangoEventSourcingAdapter
        from .adapters.django_esdb import DjangoEventSourcingDbAdapter

        return (
            AdapterRegistry()
            .register(DjangoEventSourcingAdapter())
            .register(DjangoEventSourcingDbAdapter())
        )
