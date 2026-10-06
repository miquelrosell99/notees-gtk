# Development workflow — notees-gtk

The longer form of the laws in `SKILL.md`: the three-client lockstep recipe,
the fixture byte-pin procedure, and the coding conventions this repo enforces
through `ruff`/`mypy`/review.

## The lockstep recipe (a wire/applier change, GTK side)

A protocol change lands here in this order; every step is part of the change:

1. **Read the normative docs in the main repo** — `packages/protocol/SCHEMA.md`
   and `WIRE.md` — before writing any Python. The main repo ships the change
   first (op schema, TS appliers, canonical fixtures, its own tests).
2. **Re-vendor the fixture corpus.** Copy the changed/new files from
   `packages/protocol/fixtures/` into `tests/fixtures/wire/` verbatim — never
   retype, reformat, or "fix" a fixture. Then run the byte-pin check below.
3. **Port the wire models** in `src/notees_gtk/core/protocol/`: envelope/
   payload pydantic models are `extra="forbid"` zod-strict parity with
   camelCase alias generators; new payload keys get validators matching the
   TS shape rules (key-presence keep-vs-clear, null semantics, additive
   strip-not-reject on deliberately non-strict records like select options).
4. **Port the applier semantics** in `src/notees_gtk/data/store.py`: row-level
   LWW by `(hlc_physical, hlc_logical, actor_id)`, OR-Set membership with the
   tag-membership tiebreak asymmetry, extends closure, and the
   `SCHEMA_VERSION` migration chain (additive, guarded, idempotent — fresh
   databases run the whole chain). Bump `SCHEMA_VERSION` with a comment block
   naming what changed and the web-schema parity note when one exists.
5. **Write the replay/unit tests.** New fixture files get a replay class in
   `tests/test_store_fixtures.py` asserting the same derived state the main
   repo's store tests assert (LWW winners, convergence under both delivery
   orders where racing envelopes exist); unit-semantics tests live beside the
   applier tests in `tests/test_store.py`; payload-shape tests in
   `tests/test_payloads.py`; model invariants in `tests/test_envelope.py`.
6. **Run the whole gate.** `uv sync && uv run pytest && uv run ruff check &&
   uv run mypy src` — all green. For wire-level changes also run the live
   e2e module against a built monorepo server (see `SKILL.md`).
7. **Record + docs.** Add the `CHANGELOG.md` entry; update `README.md`/
   `AGENTS.md`/skills when behavior they describe changed.

The change is not done until the Flutter client converges too (tracked in the
main repo's changelog/lockstep notes).

## Fixture byte-pin procedure

```sh
# from the repo root, with a Notees monorepo checkout available
diff -r tests/fixtures/wire <notees-checkout>/packages/protocol/fixtures \
  && echo "fixtures in lockstep"
```

Any drift means: re-vendor from the main repo (copy the file(s) verbatim),
never hand-edit the GTK copy. The byte-pin is on file contents, not on the
`wire/` directory name — `git mv` carries history and leaves the bytes
untouched, so the corpus may be renamed freely; the bytes may never be
edited.

## Coding conventions

- **Toolchain**: `uv` + Python 3.12, hatchling build. `ruff` (line-length
  120, pyupgrade, bugbear, comprehensions, simplify) and `mypy`
  (`disallow_untyped_defs`, `warn_return_any`) are blocking. `E501` is left to
  the formatter; `src/notees_gtk/ui/**` carries a per-file `E402` ignore
  because `gi.require_version()` must precede `gi.repository` imports.
- **Style**: Google-convention docstrings on public functions/classes;
  `from __future__ import annotations` in every module; double quotes;
  typed signatures everywhere mypy checks.
- **Module shape**: every module declares `__all__`; comments cite the TS
  source they port (`packages/...` paths) so a reader can diff against the
  reference; PG/PC/PB/F codes and owner-directive dates in comments are the
  accepted citation vocabulary — §-citations, milestone labels, and
  generation names (v1/v2 as history) are retired.
- **Headless-testable core**: nothing under `core/` or `data/` may import
  `gi` or GTK. UI code lives under `ui/`; the pure render/mapping logic
  (`ast_render.py`, `tokens_from_plaintext`, content helpers) stays GTK-free
  and unit-tested without the `ui` extra.
- **Fail loud**: validators raise `ValueError`/typed errors rather than
  coercing silently; unknown snapshot tables, unsafe identifiers, and unknown
  op types all reject rather than guess. The sync engine quarantines
  offending ops instead of retrying them blindly.
- **Convergence discipline**: applier writes carry no wall-clock of their own
  beyond the envelope's HLC; derived reads are pure over stored rows so every
  replica converges byte-identical under any delivery order.
