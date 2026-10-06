# AGENTS.md — notees-gtk

## Overview

notees-gtk is the first-class GTK4/libadwaita desktop client for Notees — a
self-hosted, privacy-first, local-first personal information environment whose
only authority is an immutable operation log (envelope **v3**). This repo is
the Python lockstep implementation of the wire protocol, the derived SQLite
appliers, and the sync engine; the TypeScript monorepo
(`miquelrosell99/notees`) is the reference implementation, and the Flutter
client is the third implementation of the same contracts.

The GTK client is offline-first: edits land in a local SQLite outbox and sync
through the relay (`/api/relay/v2`, WS framing v2) when the server is
reachable; the local mirror is a derived, wipe-rebuildable projection of the
log. Headless machines run the full test suite without the `ui` extra — the
`src/notees_gtk/core/` and `src/notees_gtk/data/` trees deliberately import no
GTK.

## Skills (mandatory)

```
AGENTS.md
    │
    ├── notees-gtk-development   (project skill — .agents/skills/notees-gtk-development/)
    │      └── development workflow  → references/development-workflow.md
    └── notees-gtk-operations    (project skill — .agents/skills/notees-gtk-operations/)
           └── releases & packaging → references/releases.md
```

- **Any code, wire, applier, fixture, sync, or UI change → invoke the
  `notees-gtk-development` skill first** and follow its laws (lockstep fixture
  gate, the changelog-as-record rule, docs-part-of-change).
- **Any release/packaging task — tag, CI, PKGBUILD/AUR, GitHub Release →
  invoke the `notees-gtk-operations` skill first.**
- The skills summarize and enforce; the main repo's `docs/developers/`
  runbooks are the canonical protocol/wire reference for all three
  implementations.

## Layout

- `src/notees_gtk/core/protocol/` — wire models: envelopes (envelope v3),
  strict op payload schemas (zod `.strict()` parity), content grammar,
  deterministic date-node ids, HLC clock, feature map. No GTK imports.
- `src/notees_gtk/core/api/` — synchronous REST client (`/api/relay/v2` +
  login/2FA), typed error taxonomy, threaded WebSocket client (WS framing v2).
- `src/notees_gtk/core/sync/` — the two-way sync engine (outbox drain,
  catch-up pull, realtime WS acceleration, op-id dedupe).
- `src/notees_gtk/data/` — the local SQLite store: outbox, dedupe log, seq
  cursor, mirrored node table, and the appliers (row-LWW, OR-Set membership,
  extends closure, property wire semantics). No GTK imports.
- `src/notees_gtk/ui/` — GTK4/libadwaita chrome (window, tree, editor,
  page view, worker); `ast_render.py` is the pure token-stream → view-record
  renderer and stays headless-testable.
- `tests/` — pytest suite; `tests/fixtures/wire/` is the vendored protocol
  fixture corpus (see the lockstep law below — never edit those bytes).
- `PKGBUILD` + `.github/workflows/` — Arch packaging and CI/release
  (operations detail in the operations skill).

## Commands

- Setup: `uv sync` (Python 3.12; the `ui` extra pulls PyGObject and is only
  needed to run the app, not the checks).
- Gate (blocking, mirrors `ci.yml`): `uv run pytest && uv run ruff check &&
  uv run mypy src`.
- Arch package locally: `makepkg -si` from a clone.
- Live end-to-end check: build the monorepo server (`pnpm --filter
  @notees/server build` in a Notees checkout) and point `NOTEES_V2_ROOT` at
  it; `tests/test_live_server_e2e.py` self-skips when the server build or
  node is absent.

## Invariants

- **The fixture corpus is byte-pinned.** `tests/fixtures/wire/` is a vendored
  copy of the main repo's `packages/protocol/fixtures/` (sha256-identical at
  vendor time). Never edit, reformat, or regenerate those bytes; a wire change
  re-vendors the corpus from the main repo.
- **Lockstep law.** A wire/applier change is not done until the three
  implementations converge — the TS reference, this client, and Flutter — each
  with fixtures exercising every affected applier. Recipe:
  `references/development-workflow.md`.
- **Fail loud; no backward compatibility.** Retired wire keys are rejected
  outright by the strict schemas; the stored log is rewritten in place by
  one-time migrations, never read compatibly.
- **Title-is-content.** No `name` field exists on the wire; a node's title IS
  its text content, and pages/classes carry text-only content.
- **The operation log is the only authority.** Every SQLite database this
  client writes (mirror, outbox bookkeeping) is derived state.
- **Fleet-agnostic artifacts.** Never hardcode machine names, IPs, or tailnet
  names in code, templates, or docs; concrete values live only in
  gitignored host-local files.

## Records index (scan, don't embed)

| Record | Home |
|--------|------|
| Shipped work | `CHANGELOG.md` at the repo root (newest first, one entry per slice) |
| In-flight proposals | none today; if the repo grows proposal folders they follow `.plans/YYYY-MM-DD-HHMM-<slug>/` |
| Project skills | `.agents/skills/` (auto-discovered by Kimi Code, Project scope) |

## Working rules (owner)

- **The changelog is the record:** what shipped and why lives in
  `CHANGELOG.md` — one entry per shipped slice, newest first. `AGENTS.md` is
  static guidance: never append history, dates, or work-record entries to it;
  edit it only when the guidance changes. Before implementing, skim
  `CHANGELOG.md` for recent related work. A change without its changelog +
  doc updates is not done.
- **Docs are part of the change:** behavior, wire, or UX changes update the
  relevant docs in the same pass — the main repo's `SCHEMA.md`/`WIRE.md` when
  the wire changes, this file and the skills when they describe changed
  reality.
- **No legacy version names:** narrative v1/v2 history and milestone labels
  (M1–M5) stay out of comments, docs, and test titles. Live version
  identifiers are the only exceptions — envelope v3, `/api/relay/v2`, WS
  framing v2, fixture paths under `tests/fixtures/wire/`, `v2.0.0-mN`-style
  client tags, and DB-schema version numbers (`SCHEMA_VERSION`, web-schema
  v5→v15 parity notes).
