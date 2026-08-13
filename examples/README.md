# Examples

Self-contained example apps for this generator - one directory per model. Each
`examples/<app>/` holds the **source**: `model/*.esdm.yaml` (+ `*.statemachine.yaml`
and FEEL guards), an optional `authoring/*.bpmn`, and an `esdmgen.yaml` targeting
`django-eventsourcing-postgres`. The models are codegen-neutral - keep them in sync with
the equivalent apps in the sibling codegens, which carry the same five.

| App | What it exercises |
|---|---|
| `todo` | the seed model: one aggregate, one read model |
| `orders` | the FEEL probe - a state machine and a guard that refuse a command for different reasons |
| `manufacturing` | a policy across bounded contexts, and what that reaction carries |
| `commerce` | a wider domain, authored as BPMN |
| `factory` | the largest model, several policies |

Generate an app into its own **gitignored** `generated/` subdir - `python/` for the
PostgreSQL target, `python-esdb/` for EventSourcingDB:

```sh
bin/esdmgen generate examples/todo
bin/esdmgen generate examples/manufacturing --target django-eventsourcingdb
```

`scripts/conformance.sh` regenerates every app against every target as a smoke gate, and
runs each emitted app's write-side given-when-then tests. The cross-generator conformance
suite is separate: `scripts/conformance_c4.py <app>` boots a stack and diffs it against
the golden answers in `../esdm-extensions/conformance/`.
