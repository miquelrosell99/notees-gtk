---
name: notees-gtk-development
description: Develop the notees-gtk repo — the Python/GTK4 lockstep desktop client for Notees (envelope v3 wire models, strict payload parity, derived SQLite appliers, threaded sync engine). Use when writing or changing code, the wire models, appliers, fixtures, sync, or UI in this repo. Covers the three-client lockstep law, the fixture byte-pin, the build/test gate, coding conventions, and the changelog-as-record workflow.
---

# notees-gtk development

notees-gtk is one of three implementations of the Notees sync protocol —
the TypeScript monorepo (`miquelrosell99/notees`) is the reference, Flutter is
the third. This repo ports the wire models (`core/protocol/`), the REST/WS
clients (`core/api/`), the sync engine (`core/sync/`), and the derived SQLite
appliers (`data/`) to Python; the GTK chrome (`ui/`) is deliberately thin over
a headless-testable core.

Canonical protocol references live in the main repo: `packages/protocol/
SCHEMA.md` (the normative model) and `WIRE.md` (the wire spec) — read them
before touching the wire.

## Non-negotiable laws

1. **The fixture corpus is byte-pinned — never edit fixture bytes.**
   `tests/fixtures/wire/` is a vendored copy of the main repo's
   `packages/protocol/fixtures/`, sha256-identical at vendor time. It is the
   cross-implementation convergence anchor shared with Flutter. If a fixture
   file differs from the main repo's, re-vendor from the main repo — do not
   hand-patch. Verify with the sha256 procedure in
   `references/development-workflow.md`.
2. **Lockstep law.** A wire/applier/payload change is NOT done until all three
   implementations converge, each with fixtures exercising every affected
   applier: the TS reference (plus its fixture gate), this client, and
   Flutter. The GTK side of that recipe is in
   `references/development-workflow.md`.
3. **The changelog is the record.** Before implementing, skim `CHANGELOG.md`
   at the repo root for recent related work. After shipping a slice, add one
   entry (what shipped, why, how it was verified) in the same pass. A change
   without its changelog entry is not done.
4. **Docs are part of the change.** Wire changes update the main repo's
   `SCHEMA.md`/`WIRE.md` there; behavior changes update this repo's
   `README.md`/`AGENTS.md`/skills in the same pass when they describe changed
   reality.
5. **Fail loud; no backward compatibility.** Retired wire keys are rejected
   outright by the strict payload schemas and envelope models (`extra=
   "forbid"`), mirroring the zod `.strict()` reference. Never add a compat
   read path.
6. **No legacy version names.** Keep narrative v1/v2 history and milestone
   labels (M1–M5) out of comments, docs, and test titles. Live version
   identifiers stay: envelope v3, `/api/relay/v2`, WS framing v2, the
   `tests/fixtures/wire/` path, `v2.0.0-mN`-style tags, and DB-schema version
   numbers (`SCHEMA_VERSION`, web-schema parity notes).

## Gate before declaring done

```sh
uv sync && uv run pytest && uv run ruff check && uv run mypy src
```

All green is the blocking gate (it mirrors `.github/workflows/ci.yml`). The
live end-to-end module (`tests/test_live_server_e2e.py`) self-skips without a
built monorepo server — run it deliberately when the change touches the wire:

```sh
pnpm --filter @notees/server build   # in a Notees monorepo checkout
NOTEES_V2_ROOT=<path-to-notees-checkout> uv run pytest tests/test_live_server_e2e.py
```

## Read by topic

- **Development workflow (lockstep recipe, fixture byte-pin, coding
  conventions)** → `references/development-workflow.md`
- **Releases & packaging (tags, CI, PKGBUILD/AUR)** → the
  `notees-gtk-operations` skill and its `references/releases.md`
