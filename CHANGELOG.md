# Changelog

The record of shipped work for notees-gtk. One entry per shipped slice,
newest first. This file — not `AGENTS.md`, not the skills — is where history
goes; those stay static guidance. Before implementing a change, skim this
file for recent related work. Anything before 2026-10-06 lives in git
history.

## 2026-10-22

- **feat(protocol,store): the classes gallery + the archival `active` bit +
  the retired `class.delete` op (main-repo lockstep; GTK lockstep port).** Four wire/applier changes ported from
  the monorepo's `feat(protocol,store,web)!: classes gallery, archival
  active bit, retired class.delete` slice (packages/protocol/src/op-types.ts,
  packages/domain/src/features.ts, packages/store/src/appliers.ts parity).
  (1) `object.update` gains the archival `active: boolean` — presence
  writes: `false` archives, `true` unarchives, an explicit null is rejected
  outright (`StrictBool` + a not-null validator, zod `z.boolean().optional()`
  parity); the applier projects the bit to `node.is_active` and mirrors it
  to the class-registry `class.active` projection on class rows (no
  HLC/timestamp writes on the registry row). Archival REFUSALS fail loud: a
  LOCKED system class can never be archived (`LockedSystemClassError`, new
  in `data/errors.py`) and a family base refuses a direct archival write
  (`MoveGuardError`, toggle-managed) — unarchiving stays legal on both.
  Archive ≠ trash: no trash row ever lands, and `object.restore` no-ops
  without one (the unarchive path is the archival write itself).
  (2) `WORKSPACE_FEATURES` gains `journal` — a surfaces-only toggle (the
  journal nav view + Today-page management); the new `FamilyFeature` type
  excludes it and `is_family_feature` guards the applier's family
  re-derivation, so journal rides the LWW row with zero class bits.
  (3) `class.delete` is RETIRED from the op registry (22 known op types
  now): no payload schema, no applier — rejected outright like any unknown
  op (the no-wire-compat law).
  (4) `object.delete` on a class node routes four ways, membership RIDING
  every arm (`class_member_set` is never touched by class removal; the
  membership sweep is deleted with the retired op): LOCKED system classes
  (`LOCKED_SYSTEM_CLASSES` in `core/protocol/features.ts` — the date chain +
  table, asset, query, code, template, comment) fail loud; family bases
  apply as `workspace.feature.set {enabled: false}`; other system classes
  archive (`active = false`, no trash row, `permanent` ignored); user
  classes take the plain trash row. The `children()` read joins the
  filtering law (active rows only, `packages/store/src/store.ts`
  `children` parity). The fixture corpus re-vendors byte-identical: +
  `object-update-active.json`, + `object-delete-class-routing.json`, the
  journal pair in `workspace-feature-set.json`, − `class-delete-managed.json`
  (the byte-pin diff against the monorepo is clean). Tests: the fixture
  gate and op-registry parity drop the retired op; new payload classes pin
  the `active` shape (boolean-only, null rejected, create carries none);
  new store suites cover the four routing arms, the archival refusals,
  archive-≠-trash, the journal surfaces-only toggle, and both-orders
  convergence of the routed delete vs a racing re-enable; the
  workspace-feature-set replay asserts the journal off→on pair wins its LWW
  slot. **Verification:** `uv run pytest` 894 passed / 4 skipped; `ruff
  check` + `mypy src` clean; the fixture byte-pin diff against the monorepo
  (`diff -r tests/fixtures/wire <monorepo>/packages/protocol/fixtures`) is
  clean. Flutter convergence is the third leg of the lockstep law.

## 2026-10-22

- **feat(protocol): WS auth moves to the Authorization header — the
  `?token=` credential query param retires (main-repo lockstep, WIRE.md;
  GTK lockstep port).** The relay now rejects `?token=` credential query
  params on the entire API (401): a bearer token in a URL lands in every
  log along the path. The realtime client changes shape accordingly
  (`core/api/ws.py`): `build_ws_url(base_url, workspace_id)` loses its
  `token` parameter entirely and returns a bare
  `/api/relay/v2/ws/{workspaceId}` URL with no query string;
  `RealtimeClient.__init__` keeps its credential-required guard
  (`NoteesClient.ws_token()` — API key, bearer token fallback) but stores
  the value as an upgrade header, `{"Authorization": "Bearer <credential>"}`,
  passed to `websockets.sync.client.connect(..., additional_headers=...)`
  (websockets ≥14 kwarg; the pinned 17.1 in `uv.lock` supports it), so
  every connect — including backoff reconnects, which reuse the stored
  header since the credential is stable per client lifetime — admits auth
  per WIRE.md's native-client path. `NoteesClient.ws_token()`'s docstring
  (`core/api/client.py`) now describes the header, not the query param.
  The REST surface already carried credentials in headers and needed no
  change; a repo-wide sweep confirmed no other query-string credential
  use. Tests (`tests/test_ws_client.py`): the URL-building class asserts
  the bare URL (plus a dedicated no-credential test — no `?`, no `token`
  in the string), and a new `TestAuthHeader` class asserts against the
  in-process `WsRelayStub` that the server sees `Authorization: Bearer
  secret-key` on the initial connect AND on a reconnect after an abnormal
  close. **Verification:** `uv run pytest` 873 passed / 4 skipped; `ruff
  check` + `mypy src` clean; the live end-to-end module against the real
  monorepo server (`NOTEES_V2_ROOT=/root/projects/notees`, dist built at
  `apps/server/dist`) passes 3/3 — its WS acceleration step (Device B
  subscribes, Device A pushes, the ops frame arrives over the live
  socket) now handshakes with the header against the server that rejects
  `?token=`. No fixture/wire/envelope changes (auth transport only), so
  the fixture corpus is untouched; Flutter convergence is the third leg
  of the lockstep law.

## 2026-10-22

- **feat(protocol,store): the system-extends lock — a system class's extends
  mapping is frozen to the seeded set (main-repo lockstep).** `class.setExtends` on a SYSTEM class now applies only when
  the payload's `parentClassIds` set EQUALS the seeded parent set (the
  seed/ensure re-issue convergence path); every deviation — detaching a
  seeded parent, adding an extra parent, attaching parents to a system class
  with none — fails loud with a dedicated `SystemExtendsLockError`
  (`data/errors.py`) and is never applied. The guard sits in
  `_apply_class_set_extends` after the class-node existence check and before
  the parent existence/cycle checks (`packages/store/src/appliers.ts`
  `applyClassSetExtends` parity), set-compared and order-insensitive. The
  lock authority is `SYSTEM_CLASS_EXTENDS_IDS` + `is_system_class_id` in
  `core/protocol/features.py` (`packages/domain/src/seeds.ts`
  `SYSTEM_CLASS_EXTENDS_IDS` / `isSystemClassId` parity) — the static map now
  carries the FULL seeds.ts `SYSTEM_CLASS_UUIDS` block (44 fixed ids; it was
  the family-relevant subset, but the lock needs every fixed id — a system
  class without seeded parents is frozen to the empty set), with
  `SYSTEM_CLASS_EXTENDS` unchanged (already full parity). User classes stay
  freely extensible, including onto system bases. The vendored fixture
  `class-extends-system-lock.json` (sha256-identical to the monorepo,
  verified at vendor time) pins the accepting prefix (user class extends
  `meeting`; the seeded `meeting → [event]` re-issue) and the rejecting
  tail; it joins `REPLAY_EXCLUDED` in the store-fixture suite with a
  dedicated `TestSystemExtendsLockFixture` (prefix rows, per-envelope throw,
  unchanged `class_extends` after the rejects) and the wire list in
  `test_fixtures.py` (36 → 37 fixture files). **Verification:** `uv run
  pytest` 870 passed / 4 skipped; `ruff check` + `mypy src` clean. No
  `SCHEMA_VERSION` bump (write-time guard only — the stored schema is
  unchanged). Flutter convergence remains open (the third leg of the
  lockstep law).

## 2026-10-21

- **feat(ui,protocol): the `external_link` content token retires — links are
  markdown spans in `text` tokens, parsed at render time (main-repo lockstep).** The strict content-grammar validator drops the
  `external_link` branch entirely — the schema no longer lists the type, so a
  retired token now rejects as an unknown type like any other (the payload
  tests parametrize the rejection). The renderer gains the normative render
  grammar port: `splitMarkdownLinks` (`packages/domain/src/markdownLinks.ts`)
  splits every `text` token at `\[([^\]\n]+)\]\(([^()\s]+)\)` spans into runs —
  an http/https/mailto href becomes a `MarkdownLinkRun` (the `ExternalLinkRun`
  look — link color + underline in `ui/page_view.py`), anything else
  (unclosed brackets, other schemes like `javascript:`, relative paths) stays
  a literal `TextRun`, and the token's marks ride the text segments. The
  retired `ExternalLinkRun` dataclass and its render arm are gone:
  `_inline_view` is now the list-valued `_inline_views` (one text token may
  fan out to several runs) and `_page_item` flattens accordingly; quote
  children split the same way. `plaintext_excerpt` needed no change — the
  retired token was always silent there and markdown link text now flows
  verbatim, exactly the derived-state delta the monorepo recorded (link
  labels appear in excerpts/display names post-migration). **Verification:**
  `uv run pytest` 868 passed / 4 skipped; `ruff check` + `mypy src` clean. No
  fixture bytes change (no fixture ever carried the token); the live log
  rewrite (`migrate-retire-external-link.mts`) runs in the monorepo after
  this port per the lockstep gate.

## 2026-10-21

- **feat(protocol,store): the readonly retirement — `readonly` derives from
  the system list, not stored (main-repo lockstep,
  a continuation of the property-definition surfaces batch).** The
  `readonly` render contract RETIRES from `propertySchema.create/update` —
  an enumeration of the seeds finds ZERO system properties that ever carried
  the flag, so it was never user configuration. The strict payload schemas
  reject the retired key like any unknown key (the payload tests parametrize
  the rejection on both schema payloads); `hideWhenEmpty`/`display` stay.
  Readonly-ness now DERIVES from the system property list —
  `is_readonly_property(schema_id)` in `features.py` (fixed seed UUIDs,
  precise) covering the platform-provenance schemas `generatedFrom`
  (…0000-0000-0000-000000000027) + `alias` (…000000000028) — mirroring the
  monorepo's `isReadonlyProperty`. `SCHEMA_VERSION` 18 → 19 (web schema
  v26→v27 parity) — `property_schema` rebuilds WITHOUT the `readonly`
  column (the historical v16 migration view drops it too; no data backfill —
  derived, not stored); the `EffectivePropertySchema`/`EffectiveProperty`
  read models drop the field. **Verification:** `uv run pytest` 854 passed /
  4 skipped; `ruff check` + `mypy src` clean. The live log rewrite
  (`migrate-drop-schema-readonly.mts`) runs in the monorepo after this port
  (the fixture gate law). No fixture bytes change (no fixture ever carried
  the flag).

## 2026-10-21

- **feat(protocol,store): the binding-sequence retirement — class properties
  render as a PURE ALPHABETICAL list everywhere (main-repo lockstep, a continuation of the property-definition surfaces
  batch).** `class.property.set` is now STRICTLY `{classId, propertySchemaId,
  active?}` — `sequence` retires outright: panel order no longer exists to
  configure (class panels render alphabetically by property name, a read-time
  derivation), and the binding keeps only the `active` soft-unbind flag. The
  strict payload schemas reject the retired key like any unknown key (the
  payload tests parametrize the rejection). `SCHEMA_VERSION` 17 → 18 (web
  schema v25→v26 parity) — `class_property` rebuilds WITHOUT the `sequence`
  column (the historical v12/v17 migration views drop it too; no data
  backfill — order metadata is discarded); the `EffectiveProperty` read model
  drops the field and orders PURE ALPHABETICALLY by property name, unbound
  authored rows last, `(idx, element_id)` tiebreak. `applyClassPropertySet`
  is `active`-only; the task-family seed-ensure binds sequence-less (the
  `TaskFamilySeedEntry.sequence` member is gone). Fixtures: the corpus
  re-vendors from the main repo (36 files byte-identical, sha256 verified —
  `class-property-defaults`/`class-property-active`/`class-unassign`/
  `property-schema-scope` re-encoded to the active-only binding).
  **Verification:** `uv run pytest` 854 passed / 4 skipped; `ruff check` +
  `mypy src` clean. The live log rewrite
  (`migrate-drop-binding-sequence.mts`) runs in the monorepo after this port
  (the fixture gate law).

## 2026-10-20

- **feat(protocol,store): the binding-defaults batch — `required` retires,
  `defaultValue` moves to the property schema (main-repo lockstep, the property-definition surfaces batch).**
  `class.property.set` is now STRICTLY `{classId, propertySchemaId,
  sequence?, active?}` — `required` retires outright (its only render
  effect — hiding the icon-button's "None" row — was mild enforcement,
  contradicting the render-contract law: suggestion only, never
  enforcement) and `defaultValue` MOVES to `propertySchema.create`/
  `propertySchema.update` — ONE global default per property, the Logseq
  shape; a per-class default was configuration creep the class page never
  needed. The strict payload schemas reject both retired keys like any
  unknown key (payload tests parametrize the rejection). The moved default
  is nullable-optional with the keep-vs-clear contract on update (absent
  keeps, explicit null clears); PC2 types it per the schema type at APPLY —
  against the payload's type at create, the schema's STORED type at update
  (the update carries no type), schema-less writes (unknown id) unchecked —
  failing loud on a wrong-typed default, never coercing; node-typed schemas
  (datetime/object/asset) accept only JSON null, "a default that links a
  node is meaningless". Store: SCHEMA_VERSION 16→17 (web schema v24→v25
  parity) — `class_property` REBUILDS without `required`/`default_value`
  (the v12/v14 precedent; the moved values arrive from the live log rewrite
  migrate-drop-binding-required.mts + the epoch bump, so the backfill is
  deliberately NULL) and `property_schema` gains `default_value` TEXT
  (JSON-encoded, NULL = no default); the historical v11 migration's
  ephemeral `display` add is now guarded on the pre-v12 row shape so fresh
  chains never trip v12's rebuild into copying retired columns. The
  effective-properties read derives the default from the SCHEMA row (the
  winning ACTIVE binding still supplies boundBy/sequence, never the value);
  the `required` field drops from the effective-row read model. Seeds: the
  task-family binding insert writes sequence only. Live e2e: the properties
  scenario authors the default on the schema create. Fixtures: the corpus
  re-vendors from the main repo (36 files, byte-identical — sha256
  verified; `class-property-defaults.json`, `class-property-active.json`,
  `class-unassign.json` re-encoded to the new shape, the defaults fixture
  gaining the trailing `class.property.unset` for the second class).
  **Verification:** `uv run pytest` 853 passed / 4 skipped / `ruff` clean /
  `mypy src` clean; the live e2e module against the built monorepo server
  (`NOTEES_V2_ROOT`) passes 3/3.

- **feat(protocol,store): the property-scope batch + per-block list styles
  (main-repo lockstep).** `propertySchema.create` RETIRES the display-only `scope`
  enum — the strict payload rejects the key outright, verified explicitly
  in the payload tests — and gains `icon` (nullable MDI name) and
  `scopeClassIds` (the sole locality authority: absent/null = global, a
  non-empty class-id list = local to those classes and their
  extends-ancestors; the empty array is rejected). `propertySchema.update`
  gains the keep-vs-clear nullable `icon` / `scopeClassIds` /
  `targetClassFilter` / `multi` / `validationRules` ({min?, max?, pattern?}
  strict, min <= max parse-time). Appliers: SCHEMA_VERSION 15→16 — the
  `list_style` node column lands (web schema v22→v23 parity; absent/null =
  bullet, present-null clears, the icon/color precedent) and the
  `property_schema` table rebuilds (the v12 precedent; web schema v24
  parity) with `icon` / `scope_class_ids` / `validation_rules` replacing
  the retired `scope` column. Enforcement: `property.set` on an ineligible
  node fails loud (R3, extends-aware class_hierarchy closure; schema-less
  writes stay unchecked), `class.property.set` outside the scope fails loud
  (R5, dead config), the stored `validationRules` validate every value
  before the write (R2 — inclusive min/max after the numeric-string
  normalization, full-string pattern on text/url/email scalars via the
  `^(?:…)$` anchors, carrier refs and schema-less writes unchecked, multi
  validates per element), a non-compilable pattern fails at the rule write
  (never an applier crash), and the effective read tags ineligible authored
  rows `eligible=False` while never deriving a default for an ineligible
  node (R4 — narrowing a scope is never retroactive). Seeds: the task
  family now seeds at GLOBAL scope (the seed-scope audit landed on main —
  the whole family is reusable, Status explicitly so); the GTK client is
  the only repo-side seed emitter, and it matches (NULL
  `scope_class_ids`). Fixtures: the corpus re-vendors from the main repo
  (36 files, byte-identical — `object-list-style.json`,
  `property-schema-scope.json`, `property-schema-update-fields.json`,
  `property-validation-rules.json` added; `property-validation-rules.json`
  joins the replay-excluded set, its reject tail applied deliberately in
  its own test — the store.test.ts parity). **Verification:** `uv run
  pytest` 847 passed / 4 skipped / `ruff` clean / `mypy` clean.

## 2026-10-16

- **feat(model): node links carry their own `isEmbedded` flag — manual,
  default false (main-repo lockstep).** The
  `mention` content token accepts the optional `isEmbedded` boolean
  (strict schema; absent/false = an ordinary inline link). Embedding is a
  per-link USER choice: only a link marked embedded — and sole in its
  block — presents the target's content in place; every pre-existing link
  reads false. The GTK store carries no edge projection table (mentions
  are mined per read), so the port is the token schema: the flag rides the
  stored content verbatim. Also lands the deferred merge of the
  Revision-12 embedding-model slice (this repo's `main` now carries the
  rename + the parent-page guard). Fixtures: the corpus re-vendors from
  the main repo's slice (32 files, sha256-identical, `link-embedded.json`
  added). **Verification:** `uv run pytest` 804 passed / `ruff` clean /
  `mypy` clean.

- **feat(model): the Revision-12 embedding model — `isEmbedded` replaces

- **feat(model): the Revision-12 embedding model — `isEmbedded` replaces
  `presentAsMain` (polarity inverted), the parent-page condition guard, and
  the fixture corpus re-vendor (main-repo lockstep).** The
  node model's three concerns are named separately: classification
  (`is_class`), parent relationship (`parent_id`), and embedding
  (`is_embedded` — whether the node's content belongs to the parent's body;
  true = the Block role and the DEFAULT for parented nodes, false = the
  standalone Page role). Wire: `ObjectCreate`/`ObjectUpdate` carry optional
  `isEmbedded` (the strict schemas reject the retired `presentAsMain` key
  outright, mirroring the zod reference); the applier defaults parentless →
  0, parented → 1; promotion = the false claim and still stringifies the
  content. **The parent-page condition ships with this slice** (the
  2026-10-15 lockstep debt): a standalone claim under a non-page parent fails
  loud (`MoveGuardError`) on create, promotion (update), and move; demotion
  of a parented page is gated on its standalone children being gone; the
  recursive `_is_page_row` chain walk enforces it (a class or embedded-block
  parent makes the claim inert). Store: schema v15 rebuilds the nodes table,
  renaming `present_as_main` → `is_embedded` with polarity inversion and
  converging class rows to 0 (so the ladder and the appliers' canonical
  writes meet); the historical v3/v4 node_type rebuilds emit the new column
  with the Revision-12 polarity directly. Fixtures: the corpus re-vendors
  from the main repo's landed slice (31 files, sha256-identical — the
  `present-as-main-parent-page.json` → `is-embedded-parent-page.json` rename
  and the new `object-is-embedded.json`); the parent-page fixture joins the
  replay-excluded set with its own throw-and-roll-back test. **Verification:**
  `uv run pytest` 804 passed / `ruff check` clean / `mypy src` clean.

## 2026-10-15

- **feat(appliers): the no-divergence law — content-asserted membership,
  the class.unassign content lock, and the class-delete membership sweep
  (GTK lockstep port of the monorepo's 2026-10-15 slice).** A `class_link`
  token in a stored content stream IS a classing of its node: the object
  create/update appliers OR-set-add the referenced classes (any writer —
  web, CLI, GTK, imports — converges; document-chrome content still
  flattens, date-chain nodes keep their fixed class). `class.unassign`
  refuses loud while the stored content references the class (the removal
  gesture strips the links first, strip-then-unassign). Deleting a class
  node (trash or permanent, and trash.empty) sweeps its memberships —
  trash tombstones the pairs, permanent purges them. The vendored wire
  corpus gains `class-link-assertion.json`. **Verification:** `uv run
  pytest` (800 passed), `ruff check`, `mypy src` all green.

## 2026-10-15

- **refactor(protocol)!: the inline class reference is `class_link` — the
  GTK lockstep port of the monorepo's 2026-10-15 rename (retired
  `class_chip`).** The render-only content token that references a class node
  inline (wavy underline, no membership effect) is renamed to say what it
  is — a node link, class variant. `content.py` validates `class_link` /
  its `classId`; `ast_render.py` resolves and renders it unchanged
  (render-only semantics are untouched); the vendored wire corpus gains
  `class-link.json` (re-vendored byte-identical, gate list updated). The
  strict schema rejects the retired `class_chip` key outright — the
  monorepo's stored-log migration rewrites live envelopes before clients
  re-sync. **Verification:** `uv run pytest` (799 passed), `ruff check`,
  `mypy src` all green.

## 2026-10-14

- **feat(protocol): `trash.empty` bulk op — the GTK lockstep port of the
  monorepo's 2026-10-14 slice.** The wire gains one additive op (envelope v3,
  no protocol version bump): empty the trash by permanently deleting every
  node carrying a non-permanent trash row. Payload is a strict empty object —
  any key is rejected outright (zod `z.object({}).strict()` parity); the
  target set is evaluated at APPLY time against the trash table, never an
  author-time id list, so an `object.restore` landing before the op keeps its
  node alive and one landing after is plain log-order LWW against the
  permanent tombstones. Replay is idempotent: an empty trash table no-ops.
  - **Wire models** (`op_types.py`, `payloads.py`): `trash.empty` joins
    `KNOWN_OP_TYPES` (23 op types, docstring count updated); zero-field
    `TrashEmptyPayload` registers in `PAYLOAD_SCHEMAS` (apply-time payload
    validation covers it automatically) plus a `build_trash_empty()` builder.
  - **Applier** (`store.py`): the permanent branch of `object.delete` is
    extracted into `_permanently_delete_subtree` (hard-delete the subtree,
    purge the descendant derived rows, keep the root's `is_permanent = 1`
    retention row with `deleted_at` = the envelope's timestamp) — the shared
    body the new `_apply_trash_empty` runs once per snapshotted root
    (`is_permanent = 0` rows ordered by node_id for determinism). A root
    whose node row is gone — consumed by an earlier iteration's subtree
    purge, or a legacy dangling trash row — is swept, never fatal. The GTK
    purge table set is this repo's own permanent-delete list (tag
    membership, collection membership, and class bindings in addition to the
    TS reference's node/property/asset rows; the TS `node_link`/`edge`/
    `node_stats` projections have no GTK tables — derived-schema parity, not
    a semantic divergence).
  - **Fixture**: `tests/fixtures/wire/trash-empty.json` vendored verbatim
    from the monorepo (sha256 `12b63948…a222da`, byte-identical), registered
    in `test_fixtures.py` and replayed by a new `TestTrashEmptyFixture`
    (every trashed subtree hard-deleted, the restored-before-empty page
    active, exactly two permanent retention rows, a second empty with a
    fresh envelope no-ops).
  - **Tests**: 5 unit specs in `test_store.py` (multiple trashed roots, an
    independently-trashed descendant purged as its own root vs. consumed by
    the parent's earlier purge — both id orderings, restore-before-empty,
    idempotent second apply, stale trash row swept not fatal), payload-shape
    specs in `test_payloads.py`, and the `KNOWN_OP_TYPES` parity set in
    `test_envelope.py`.
  Verification: `uv run pytest` (798 passed, 4 skipped), `uv run ruff
  check`, `uv run mypy src` — all green. The vendored corpus diffs clean
  against the monorepo work branch's `packages/protocol/fixtures/`.

## 2026-10-13

- **feat(store): parent-lock and class-lock date-chain nodes — the GTK port
  of the monorepo's 2026-10-13 slice (TS reference commit a35bb7c5).** A date
  node's parent IS its deterministic chain parent (year at the workspace
  root, month under its year, day under its month) and its class set IS its
  precision's system class — nothing else, never declared a class itself
  (SCHEMA.md "Datetime"). The lockstep law requires all three clients to
  reject alike, so the GTK appliers carry the same five guards, all raising
  `MoveGuardError` (the sync engine's `_apply_guarded` skip-and-continue
  quarantine covers them):
  - **Domain** (`dates.py`): `date_chain_parent_id` / `date_chain_class_id`
    derive the locked parent/class from the date id (year → `None`, month →
    its year node, day → its month node; the one system class per precision
    via `DATE_CHAIN_CLASS_UUIDS` — the seeds.ts year/month/day UUIDs), built
    through the existing generators.
  - **object.move** — any parent other than the chain parent fails loud
    (this also rejects detach-to-root for month/day).
  - **object.create placement** — a date id claiming a non-chain parent
    fails loud; a parentless create stays legal (the standalone shape the
    client chain self-heal adopts).
  - **object.create classIds** — runs at the top of the applier, BEFORE the
    OR-Set membership seeding and the re-create early return: every classId
    on a date id must equal its precision's system class, so the re-issued
    create (the class.assign add carrier, also on an existing id) is covered.
  - **class.unassign** — the precision's system class cannot be removed;
    unassigning a foreign class stays legal (legacy cleanup).
  - **class.create** — a date id can never be declared a class.
  This clears the LOCKSTEP-PENDING marker the main repo's SCHEMA.md carries
  for the GTK appliers (no wire change — the vendored fixture corpus is
  untouched). Verification: `uv run pytest` (6 new lock specs in
  `test_store.py`, 4 new derivation specs in `test_dates.py`), `ruff check`
  + `mypy src` clean.

## 2026-10-09

- **fix(login): the sign-in contract matches the real server — login works
  end-to-end.** The v4.1.1 login attempt surfaced the next drift: the client's
  `AuthResult` parsed an imagined OAuth-ish shape (`access_token` /
  `token_type`) while the server answers `{token, expiresAt, user, kdf}`, so
  validation failed on every login; and the workspace listing called
  `/api/workspaces/` (trailing slash — the Fastify route does not redirect)
  unwrapping a fictional `PaginatedResponse.items`. The whole auth/workspace
  surface now mirrors the server: `AuthResult(token, expires_at, user, kdf)`
  (camelCase aliases, additive-tolerant reads), `GET /api/workspaces` →
  `{workspaces: [{id, name, …}]}`, and the speculative 2FA flow is gone (the
  server has no 2FA endpoint — the TOTP row and `TwoFactorRequired` gate never
  matched reality). The envelope actor is the GTK system actor
  (`config_store.GTK_ACTOR_ID`, the a6 slot of the fixed system-actor block —
  a1 is the web frontend, a2..a5 the server-side migrations) instead of a
  nonexistent `user.uuid` that degraded to `"anonymous"`. The login subtitle
  reads "Connect to a Notees sync server". Verification: `uv run pytest` 772 passed (auth fixtures rewritten
  to the real shapes, the 2FA specs deleted, a new **live** test runs the
  exact user path — `POST /api/setup` → `client.login` → `list_workspaces`
  against the real TypeScript server build — 3 passed), `ruff` + `mypy`
  clean.
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
 in BOTH colour schemes at once (the variables are libadwaita ≥ 1.4
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
  side of the monorepo's #14 follow-up (plain seeds
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
