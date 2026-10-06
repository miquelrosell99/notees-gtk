# Changelog

The record of shipped work for notees-gtk. One entry per shipped slice,
newest first. This file — not `AGENTS.md`, not the skills — is where history
goes; those stay static guidance. Before implementing a change, skim this
file for recent related work. Anything before 2026-10-06 lives in git
history.

## 2026-10-06

- **chore(sync): re-vendored the wire fixture corpus from the main repo —
  lockstep convergence.** `tests/fixtures/wire/` is byte-identical to the main
  repo's `packages/protocol/fixtures/` again: all 21 fixtures copied verbatim
  (`cp -a`, bytes untouched), sha256sum pairwise against the main repo reports
  zero mismatches, zero extras, zero missing. The corpus gains
  `object-restore.json` (trash-restore probe: create → delete → restore) and
  the drifted `class-property-defaults.json` is overwritten (gains the trailing
  number-format `propertySchema.create`; the GTK models and appliers already
  cover both). The exact-list assertion in
  `test_every_fixture_file_is_covered` now maps all 21 files — that update is
  the intended convergence signal, mirroring the TS protocol gate's
  exact-list assertion. Gate after the re-vendor: `uv run pytest` 680 passed /
  3 skipped, `uv run ruff check` clean, `uv run mypy src` clean.
- **chore(docs): record-keeping retirement sweep — plan-era citations and
  legacy version names scrubbed; AGENTS.md + project skills + this changelog
  created.** Same treatment the main repo gave itself on 2026-10-06: ~180
  plan §-citations stripped from code comments, docstrings, and test titles
  (the citation token goes, the sentence stays; parentheticals holding only
  a citation go entirely); both `implementation-plan` references removed;
  narrative v1/v2 history reworded out ("the v1 scheme" → "the scheme",
  `v2/packages/...` monorepo paths → `packages/...`, "v1-migrated" →
  "migrated", "pre-v2 cache" → "pre-reshape cache"); the 8 M1/M3 milestone
  labels dropped ("the M1 op registry" → "the op registry", "reserved for M3
  E2EE" → "reserved for E2EE"). Deliberate keeps: the vendored fixture corpus
  (byte-pinned lockstep corpus, untouched — renamed to `tests/fixtures/wire/`
  later that day, next bullet); live version identifiers (envelope v3,
  `/api/relay/v2`, WS framing v2, "relay protocol v2", `action-gh-release@v2`,
  DB-schema version numbers, the `NOTEES_V2_ROOT` e2e env var); code symbols
  carrying the v2 label (renamed later that day, next bullet); the external
  RFC 9562 §5.2 citation; quoted historical
  commit titles in the seed-parity test; the RFC-style `(Fork 3)`/`(Fork 4)`
  decision names that SCHEMA.md still defines. One test title renamed
  (`test_v1_shape_...` → `test_legacy_shape_...`, matching the main repo's
  `it("...v1 shape")` → `it("...pinned shape")`). No logic, symbol, or
  assertion changes — comments/docs/test-titles only. Gate after the scrub:
  `uv run pytest` 679 passed / 3 skipped, `uv run ruff check` clean, `uv run
  mypy src` clean.
- **chore(repo): legacy `v2` labels purged from the vendored fixture corpus
  and its harness.** The fixture corpus directory and its test-side symbols
  carried a legacy-version label in their paths and names — the one exception
  the no-legacy-version-names rule still allowed; the exception is gone.
  Renamed `tests/fixtures/{v2 → wire}/` with `git mv` (history follows); all
  20 JSON files sha256-identical before/after, so the byte-pin law held — the
  bytes were never touched. Path strings and symbols now say `wire`/`WIRE_`
  (`WIRE_SINGLE_ENVELOPE_FIXTURES`, `WIRE_ENVELOPE_LIST_FIXTURES`,
  `WIRE_TABLES`, `_NODES_DDL`, `_AUX_DDL`, the `test_wire_…` titles); AGENTS.md
  and the development skill + workflow updated in the same pass. Deliberate
  keeps (live protocol versions, not labels): `/api/relay/v2`, WS framing v2,
  envelope v3, `_migrate_v2` (DB-schema version), `NOTEES_V2_ROOT`,
  `action-gh-release@v2`, the v2-envelope rejection tests (wire-version
  semantics), and `"v2"` as node text in a sync-fixture payload. Gate after:
  `uv run pytest` 679 passed / 3 skipped, `uv run ruff check` clean, `uv run
  mypy src` clean.
- **chore(docs): AGENTS.md created** — static guidance (overview, layout,
  commands, invariants incl. the fixture byte-pin and the three-client
  lockstep law, fleet-agnostic rule) with a records index pointing here.
- **chore(docs): project skill pair added** under `.agents/skills/` —
  `notees-gtk-development` (lockstep law, gate commands, changelog-as-record,
  fixture byte-pin procedure, coding conventions) and
  `notees-gtk-operations` (tag-triggered release pipeline, PKGBUILD/AUR,
  never-re-tag law), each with a `references/` detail file.
- **chore(docs): CHANGELOG.md created** as the shipped-work record, this
  entry its first row.

### Known follow-up (not part of this slice)

`tests/fixtures/wire/` predates the main repo's current fixture set: the main
repo has since added `object-restore.json` and a number-format probe to
`class-property-defaults.json`. The corpus here was deliberately left
untouched (byte-pin law); a re-vendor from `packages/protocol/fixtures/` is
owed as its own lockstep slice.
