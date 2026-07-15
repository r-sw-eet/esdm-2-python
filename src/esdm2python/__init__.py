"""Python codegen: an ESDM model in, a Django + `eventsourcing` app out.

The pipeline mirrors the sibling generators (`esdm-2-symfony`, `esdm-2-nimbus`):
load the ESDM YAML, build a stack-neutral typed model, gate it (FEEL/lint), then
let one adapter emit an in-memory file tree. Only the adapter knows the target
stack; everything upstream is framework-agnostic.
"""

from __future__ import annotations

__version__ = "0.1.0"
