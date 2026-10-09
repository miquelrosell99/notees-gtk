# Changelog

The record of shipped work for notees-gtk. One entry per shipped slice,
newest first. This file — not `AGENTS.md`, not the skills — is where history
goes; those stay static guidance. Before implementing a change, skim this
file for recent related work. Anything before 2026-10-06 lives in git
history.

## 2026-10-09

- **fix(login): the brand lockup renders at its real size, and the login body
  is strict `{email, password}` again — the two bugs that broke the sign-in
  screen.** (1) The mark: the vendored `full-color(-dark).svg` viewBox carries
  the brand clear-space padding around the ink (the ink is ~74% × ~39% of the
  viewBox), so the raw at-scale render shrank the lockup to an illegible
  ~47×10px squiggle inside its 64px budget. The loader now renders
  oversampled (512px), crops to the opaque bounding box (new pure, gi-free
  `brand.ink_bbox` — headless-testable), and downscales to a 96px-wide mark:
  the wordmark letterforms land ~21px high and read cleanly. (2) The login
  call: `client.login` dropped its `remember_me` field — the server schema is
  strict `{email, password}` and rejected the extra key with 422
  ("unrecognized key"), so every login failed; session lifetime is
  server-owned (30-day sliding sessions), the same contract the Flutter
  client documents. Verification: `uv run pytest` 774 passed (the remember_me
  pin rewritten as a strict-body assertion, 4 new `ink_bbox` specs), `ruff
  check` + `mypy src` clean.
- **feat(packaging): ship a `.desktop` entry — the app appears in launcher
  apps menus.** The package installed the binary + icons but no desktop
  entry, so launcher menus had nothing to list (found on the fleet
  workstation). `data/dev.notees.Gtk.desktop` — Name=Notees,
  Exec=/usr/bin/notees-gtk, Icon=dev.notees.Gtk (the window's
  `set_icon_name`), StartupWMClass for window↔entry association — is now
  installed to `/usr/share/applications/` by the PKGBUILD. Verification:
  rebuilt the package (`makepkg -s` from the updated repo), reinstalled
  with `pacman -U`, `desktop-file-validate` clean.
- **feat(protocol,store): the unified Datetime property type — `date`/`date_range` retire into one `datetime` type (LOCKSTEP with the monorepo's unified-datetime batch, gate 24→25).** The monorepo's wire batch (TS reference shipped 2026-10-09) retires the `date`/`date_range` property types into ONE `datetime` type: the strict `propertySchema.create` type enum rejects the retired values outright and adds `datetime`; a value is a point `{nodeId, time?}` or a range `{start: slot|null, end: slot|null}` (slot = `{nodeId, time?}`) anchored to the year/month/day node chain — full-day is the absence of `time`, legacy bare-uuid strings normalize to `{nodeId}`, and every legacy shape is a legal member of the new union (live values ride untouched). The GTK lockstep side:
  - **Wire model.** `payloads.py` `_PROPERTY_TYPE` retires `date`/`date_range` and adds `datetime` (strict `Literal` — the zod enum parity; retired values rejected outright). `dates.py` gains the shared value vocabulary (`TIME_OF_DAY_PATTERN`, `is_valid_time_of_day` — the `packages/domain/src/dates.ts` port): 24h `HH:MM`, minute precision, no timezone.
  - **Store validation (the `property-values.ts` port).** The PB2 shape gate unifies the old `date`/`date_range` arms into one `datetime` union: a point `{nodeId, time?}` (legacy bare-uuid string normalizes) or a range of slots with either side open (both-open legal, a missing side key fails loud); a value carrying BOTH `nodeId` and `start`/`end` keys is rejected outright; a `time` must match `HH:MM` and ride a DAY-precision date-node ref AND a day-precision schema ceiling (the ceiling check runs in the PG6 ref-target pass, the datePrecision rank parity). Per-slot existence checks mirror the old date_range arms — each non-null range slot ref must resolve to a node row, open sides skip. PC2 typed defaults: `datetime` joins the node-typed family (JSON null only). No store schema change — values ride ordinary property JSON (`SCHEMA_VERSION` stays 14, the TS parity).
  - **Seed vocabulary.** `features.py` `TASK_FAMILY_SEED` retypes the task family's three date bindings (Scheduled/Deadline/Closed) to `datetime` (the seeds.ts retype parity; the other three date-typed system properties are server-side seeds the client receives as ops — no GTK manifest carries them).
  - **Fixture corpus.** `tests/fixtures/wire/property-datetime.json` vendored from the main repo (gate 24→25); sha256-match the TS reference 25/25. The exact-list gate (`test_fixtures.py`) and the store replay (`TestPropertyDatetimeFixture` — schema precisions, the value-union LWW chain, the landed date chain) cover the new fixture.
  - **Tests.** New `TestDatetimeValueUnion` (the monorepo dates.test.ts suite port: timed point/range round-trips, both-open range, mixed-shape rejection, malformed `time`, day-precision on ref and ceiling, per-slot existence), the PB2/PG6 suites retyped to `datetime`, payload strict-enum tests (retired values rejected), and the vocabulary tests in `test_dates.py`.
  - **Verified.** `uv run pytest` (770 passed + 3 skipped, was 741 + 3), `uv run ruff check`, `uv run mypy src` — all green. The live end-to-end module not run (no live sync per the batch constraints). Flutter port pending — the batch stays LOCKSTEP-PENDING until it lands.
- **feat(protocol,store): the page-subtitle wire node field — `description` on `object.update` (web schema v18 parity).** The monorepo's wire gained the optional nullable `description` on the `object.update` payload — the page subtitle in the core page chrome (the Capacities header precedent; the icon/color/coverAssetId convention): plain text, max 512 chars, presence writes / present-null clears / absence preserves. `object.create` rejects the key outright (the strict schema, no wire compat). The GTK lockstep side:
  - **Wire model.** `ObjectUpdatePayload` gains `description: str | None` (max 512 chars — the zod `z.string().max(512).nullish()` grammar); `build_object_update` carries it with the `_UNSET` absence-vs-clear convention like the other wire node fields.
  - **Derived store (schema v13 → v14, web v17→v18 parity).** The node table gains a nullable `description` column — in `_NODES_DDL` for fresh creates and via the idempotent column-guarded `_migrate_v14` for on-disk databases; the `object.update` applier maps presence-writes / present-null-clears onto it (the color precedent). `NodeRow`, the `node()`/`children()` reads, and the snapshot verbatim-column map surface it; `_NULLISH_OBJECT_UPDATE_FIELDS` (the outbox writable-field guard) accepts a null-only description update as a real clear.
  - **Fixture corpus.** `tests/fixtures/wire/object-wire-fields.json` re-vendored from the main repo (sha256-identical) — two new envelopes: a `description` set ("Subtitle text") and a clear (`null`).
  - **Verified.** `uv run pytest` (all green), `uv run ruff check`, `uv run mypy src`.

## 2026-10-08

- **feat(ui): align the GTK client to the Margin Green brand — icon, theme
  tokens, colours.** The Notees identity (MARGIN — the page is the canvas,
  the margin is where thought accumulates) lands as the `brand/` submodule
  (`notees-brand`, pinned `v1.0.0`: tokens, logo masters, guidelines).
  Brand alignment only — no wire/sync/model change.
  - **App icon.** `data/icons/hicolor/scalable/apps/dev.notees.Gtk.svg` +
    `data/icons/hicolor/512x512/apps/dev.notees.Gtk.png`, vendored from
    `brand/assets/logo/app-icon-512.{svg,png}` (the repo shipped no icon
    before this slice). `PKGBUILD` installs them into the hicolor theme
    (the glib2 pacman hook refreshes the cache) and the window resolves
    them via `set_icon_name("dev.notees.Gtk")` — a no-op on hosts whose
    theme lacks the icon, exactly as before.
  - **Accent chrome.** New `ui/theme.css`, installed by `NoteesApp` through
    a `Gtk.CssProvider` and shipped inside the wheel as a package asset,
    maps libadwaita's public `--accent-*` variables to Advance Green
    `#2e5e46` — suggested-action buttons, selections, switches, focus rings
    — in BOTH colour schemes at once (the variables are libadwaita ≥ 1.4
    public CSS API; a static override is the only dark-safe way).
  - **Content colours.** New pure module `ui/brand.py` mirrors
    `brand/assets/tokens/tokens.css`; the hardcoded Pango hexes in
    `ui/page_view.py` (mention `#3584E4`, external link `#1B5FBF`, class
    chip `#D3D3D3` on black) become the brand roles resolved per scheme:
    link `#3b7357`/`#6da789` (the tokens.css `--bi-link` role, the Advance
    Green family), chip surface-alt `#edeae2` on iron ink `#1c1a16` /
    `#312a22` on `#f4f3f1`. The user highlight mark stays yellow — a
    content semantic, not chrome.
  - **Wordmark.** The login form carries the Margin Green symbol above the
    "Sign in to Notees" group: `ui/assets/full-color{,-dark}.svg` (the
    scheme-correct renderable masters — the `master-*.svg` files are
    semantic pipeline sources without fills) at 64 px, skipped gracefully
    when the host lacks the SVG pixbuf loader.
  - **Grounds & type.** Window/surface chrome stays Adwaita: libadwaita
    variables are static across schemes and Adwaita's dark palette already
    carries the `#161412` night ground, so paper `#f7f4ec` owns the content
    tokens instead of fighting the toolkit. The reading surface
    (`.page-content`) takes Newsreader with a system-serif fallback; the
    client bundles no fonts (as before this slice). Recommendation per
    `brand/guidelines/fonts.md`: install Instrument Sans (chrome), Newsreader
    (reading) and JetBrains Mono (data) system-wide — all OFL via Google
    Fonts; GTK font-name wiring beyond the Newsreader fallback is left to
    user/system preference.
  - **Verified.** `uv run pytest` (734 passed + 3 brand-token tests, 3
    skipped), `uv run ruff check`, `uv run mypy src` — all green; a wheel
    build confirmed `theme.css` and both symbol SVGs ship in the package.

## 2026-10-07

- **feat(ui,store): the alias chrome — the Aliases section on the aliased
  node's view + the Aliased-node row on the alias's own view (the
  `aliasedNodeId` field's GTK UI).** The GTK side of the monorepo's alias
  read-path slice, the minimal honest set on the wire field ported from
  `apps/web/src/ui/components/{AliasesButton,AliasedNodeRow}.tsx`:
  - **The store reverse read.** `LocalStore.alias_nodes_of` — the
    `Store.aliasNodesOf` port: the recursive reverse-walk over
    `aliased_node_id`, self excluded, LIVE rows only, id order, scoped to
    the workspace (the GTK store is multi-workspace on one connection — the
    one deliberate addition over the TS SQL). `TestAliasNodesOf` pins the
    semantics the monorepo suite pins (direct + chain, self-excluded,
    trashed skipped, empty-when-none) plus the workspace scoping.
  - **The Aliases section** (`ui/aliases.py::build_aliases_section`) on the
    aliased node's view: one row per live alias (chains included, labels
    via `node_display_name`), each with an Open button that navigates to
    the ALIAS node's own view — the one deliberate bypass of the
    navigation redirect, as on the web. Rendered only when aliases exist;
    the main-side ADD backward write is not part of this chrome.
  - **The Aliased-node row** (`ui/aliases.py::build_aliased_node_row`) on
    the alias's own view: names the main (Open navigates to it), Change…
    re-points the carrier's OWN `aliasedNodeId` through a searchable
    popover picker (the pure `alias_repoint_candidates` filter — the alias
    itself and every already-aliased node excluded, the web picker's
    `canAdd` rule), Clear writes the present-null. Both ride
    `object.update` envelopes (`ui/alias_ops.py::alias_update_envelope`,
    the editor-save authoring shape) through enqueue + the optimistic
    mirror apply, then the sync round; a `CycleError` surfaces as a toast,
    never a crash.
  - **Navigation.** `NodeTreeSidebar.select_node` selects a node, expanding
    collapsed ancestors (cycle-guarded like `_visible_rows`); the window's
    `_open_node` seam covers the already-selected case where no selection
    signal fires.
  - **The pure/impure split.** All decision logic is GTK-free
    (`ui/alias_ops.py`, headless-tested by `tests/test_alias_ops.py` — the
    envelope payload shape for re-point vs clear, the candidate filter);
    the widget module only turns records into widgets. No wire change — the
    fixture corpus stays byte-identical (`diff -r` clean).
  Verified: `uv run pytest` 734 passed / 3 skipped, `uv run ruff check`
  clean, `uv run mypy src` clean — and the live end-to-end module against
  the monorepo's built server (`NOTEES_V2_ROOT=<checkout> uv run pytest
  tests/test_live_server_e2e.py`: 2 passed).
- **feat(seed): the #14 follow-up five — definition/idea/place/project/trip
  seeds ported to the static map; the events family gains the trip cascade;
  the seed-parity suite pins the manifest so drift fails the gate.** The GTK
  side of the monorepo's #14 follow-up (owner list, 2026-10-06 — plain seeds
  per the meeting-system precedent, zero wire cost), ported from
  `packages/domain/src/seeds.ts` + `features.ts`:
  - **The static map** (`core/protocol/features.py`) gains the five fixed
    class UUIDs (…0043-…0047), their MDI icons + display titles (the
    `SYSTEM_CLASS_ICONS` / `SYSTEM_CLASS_DISPLAY_NAMES` subset), and the
    `trip → event` extends edge — so the events-family cascade (family set,
    gating walk, the applier's archival re-derivation) now reaches trip
    exactly like meeting/birthday, while the four plain seeds stay
    unmanaged: no gating, not always-on (features.ts parity — they are
    absent from the TS `ALWAYS_ON_SYSTEM_CLASSES` too).
  - **The seed-parity test** (`tests/test_seed_parity.py`) grows the
    `TestDeployCatalogFive` class: the five ids pinned (block prefix,
    uniqueness, the withdrawn …0001/…0042 slots untouched), the static-map
    coverage guard, the seed-op replay (`class.create` with the display
    title as content + the icon, `class.setExtends` for trip→event) through
    the store appliers, and the gating/family semantics mirroring
    `features.ts`.
  - **The feature/store tests** pin the four-strong events family
    (`family_class_names("events") == ("event", "birthday", "meeting",
    "trip")`) and the toggle cascade with trip seeded alongside the
    calendar family.
  - **Fixtures untouched** — the seed change emits server-side seed
    envelopes; the corpus stays byte-identical (`diff -r` clean).
- **feat(protocol+store): the M27/M12/M47/M38 alignment — wire node fields,
  the title-flatten ruling, class conversion, alias cycle validation, the
  asset property type, seed convergence; the three new fixtures re-vendored
  (lockstep).** The GTK side of the monorepo's 2026-10-07 batches, ported
  faithfully from `packages/protocol/src/op-types.ts`,
  `packages/store/src/{appliers,property-values,schema,store}.ts`, and
  `packages/domain/src/{seeds,features}.ts`:
  - **Wire node fields (M27).** `object.update` gains the optional nullable
    `coverAssetId` / `bannerAssetId` / `aliasedNodeId` node fields (strict
    payload schema, uuid format-checked, `object.create` rejects them
    outright) mapped presence-write / present-null-clear onto three new
    derived node columns — store schema **v13** (web schema v15→v16 parity;
    guarded additive migration, the v10 precedent; fresh databases run the
    whole chain). `NodeRow` surfaces the fields; server snapshots carrying
    the columns restore through them.
  - **Title applier.** The `object.update` content path no longer flattens
    rich tokens to text-only for present-as-main nodes — class rows stay
    text-only; create-as-main and promotion remain the lossy boundaries;
    display-name derivation still flattens. The registered lockstep debt
    from the web batch is paid.
  - **Class conversion (M47).** `class.create` on an EXISTING node DECLARES
    it a class: `is_class` flips, a parented node is cut to a root (parent
    edge + child-order row drop), the render bit clears, the registry adopts
    the node's existing title (COALESCE/NULLIF preserve on re-declaration —
    absent icon/color/name never wipe), and the hierarchy self-row lands at
    create (the TS `applyClassCreate` parity).
  - **Alias write-time validation (M12).** `object.update {aliasedNodeId: T}`
    walks the would-be chain; a revisit (self-alias included) raises
    `CycleError` and the write is never applied; clearing skips the check;
    a stale-HLC write drops by row LWW before the check. `resolve_alias` is
    the read helper — chain to terminal, cycle-safe (a revisit yields the
    starting id unchanged), depth cap 32.
  - **The asset property type (M38).** The propertySchema type enum gains
    `asset`: values validate as node references, the target MUST carry the
    asset class (the filter is implicit in the type — an explicit
    targetClassFilter is ignored on asset schemas); node-typed defaults
    stay unsupported.
  - **Seeds.** The seeded `class` meta class (…0001) is retired — out of
    the always-on manifest, its UUID never reused; `weblink` (…0034) extends
    `source` — it left the always-on list and now rides the SOURCES family
    set and gating; the `asset` class id joins the static map (the implicit
    filter resolves through it).
  - **Outbox guard.** The enqueue writable-field check is now nullish-aware:
    a null on a NULLISH field (color + the three wire node fields) is a real
    CLEAR the server's presence-based refine accepts, so it must not be
    swallowed; nulls on optional fields (icon/contentAst) still skip (they
    would 422).
  - **Fixtures.** `tests/fixtures/wire/` re-vendored from the main repo —
  `class-convert.json`, `object-wire-fields.json`, `property-asset-type.json`
  copied verbatim (`cp -a`), the corpus now 24 files, `diff -r` clean and
  sha256-identical file-by-file; the three join the round-trip list in
  `test_fixtures.py`, and new replay classes in `test_store_fixtures.py`
  assert the same derived state the monorepo store tests assert.
  Verified: `uv run pytest` 720 passed / 3 skipped, `uv run ruff check`
  clean, `uv run mypy src` clean — and the live end-to-end module against a
  fresh `pnpm --filter @notees/server build` of the monorepo HEAD
  (`NOTEES_V2_ROOT=<checkout> uv run pytest tests/test_live_server_e2e.py`:
  2 passed). Flutter convergence and the migration-script runs stay gated
  on the main repo's lockstep tracking.

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
