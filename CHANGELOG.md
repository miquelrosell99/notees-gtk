# Changelog

The record of shipped work for notees-gtk. One entry per shipped slice,
newest first. This file — not `AGENTS.md`, not the skills — is where history
goes; those stay static guidance. Before implementing a change, skim this
file for recent related work. Anything before 2026-10-06 lives in git
history.

## 2026-11-05

- **feat(e2ee): sealed-snapshot restore contract + the fail-loud locked ruling + the chunked upload transport (main-repo lockstep debt).** Five main-repo slices port into the GTK custody track. (1) `SnapshotMeta` gains the wire's `sealed` declaration and the engine's restore contract keys off it (WIRE.md): keyed clients decrypt and restore ONLY `sealed: true` images; a `sealed: false` blob (a keyless upload or the server's self-heal image — content-empty while claiming `upToSeq` = latest) is SKIPPED with a counted warning (`engine.skipped_snapshots`), the cursor stays put, and seq catch-up replays the log — restoring it would blank the workspace (the empty-store trap); the keyless sealed-blob magic-header skip folds into the same decline marker/counter. (2) Sealed uploads send `sealed=1` (`upload_snapshot(..., sealed=True)`) and, per the server ruling, skip the sanity gate — ciphertext is opaque, accepted never 422-gated (no client expectation of 422 on sealed uploads remains). (3) The optional multipart transport ports per WIRE.md — `upload_snapshot_part` (`PUT /snapshot/data/part`, duplicate index 422, pre-wire validation) and `commit_snapshot` (`POST /snapshot/data/commit`, parts-mismatch 422, unknown upload 404, same `sealed` flag, 201 mirrors the single PUT) — with the single-PUT fallback intact; the engine upload path stays unwired (restore-only client), recorded in `docs/lockstep-debt.md`. (4) The fail-loud ruling (owner decision): a keyed workspace opened without a usable key must not silently open keyless — `_install_workspace_crypto` now returns whether content sync may proceed, sets `SyncEngine.encrypted_locked` on the known-keyed-locked arms (no master key in memory; wrapped record fails to unwrap under the current master key), and the engine refuses push/pull/snapshot-restore with a warning until `set_crypto` installs a key; genuinely keyless workspaces and the undetectable cases (operator/API-key 403 on `/key`, offline starts) stay honest plaintext, loudly logged. The window surfaces the state as an `Adw.Banner` — "This workspace is encrypted — sign in with your password to open it" — with a Sign-in affordance (password re-login re-derives the master key; the post-login workspace load re-opens unlocked), and interval sync skips the locked workspace. `WorkspaceRef.encrypted` parses for selector parity. (5) `docs/lockstep-debt.md` re-baselines: the locked-UI entry moves to PARTIAL (in-app unlock modal without full sign-out and selector lock badges remain debt; local editing during lock stays possible), the upload entry records the ported transport vs the still-unwired engine path. Verification: +13 client tests (sealed meta parse/default, sealed upload param + gate-exemption acceptance, part stage/duplicate/pre-wire validation, commit params/404/422, `WorkspaceRef.encrypted`) and +5 engine tests (keyed plaintext-image skip + counted warning + no re-download + log replay from the durable cursor, meta-declared-sealed restore, fail-loud push/pull/snapshot refusal with outbox preserved, `set_crypto` clearing the lock) — full gate green (`uv run pytest`, `ruff check`, `mypy src`), live-e2e green against the built monorepo server. Lockstep: runtime behavior convergence; the wire and the byte-pinned fixture corpus are untouched; Flutter is the remaining arm.

## 2026-11-04

- **feat(graph): the minimal graph view ships — the v28 edge-index backfill,
  the headless `core/graph/` projection (topology + deterministic layout +
  color groups), and the GTK surface behind a pinned "Graph" sidebar row.**
  The deferred graph slice, built in four gate-green steps on the query
  surface's foundations; the web's `apps/web/src/features/collection/graph/`
  + `packages/store/src/graph.ts` are the reference, reduced per the
  minimal-surface ruling. **(1) The edge index's missing backfill** — the
  derived `edge` + `node_link` index itself shipped at v25 (2026-11-03, the
  `edges.ts` deep port with the applier-time write triggers), but pre-v25
  databases created the tables additively and re-derived a node's rows only
  when a later op rewrote that node, so untouched nodes kept an empty index.
  `SCHEMA_VERSION` 27 → 28: `_migrate_v28` runs `_backfill_edge_index`
  inside the migration's own transaction (the step takes a bare connection,
  the rebuild is a `LocalStore` method — the hook lives in `_migrate` so the
  version bump and the backfill commit or roll back together): every stored
  node's edges re-derive through the applier-time `_rebuild_edges`, stamped
  with the node row's own `updated_at` (order-invariant — content edge ids
  are content-addressed, so the stamp lands in `created_at` only and a
  re-run over a complete index is a no-op). Note: the brief named a
  `class_link` edge vocabulary — the TS contract deliberately keeps class
  assignment / class_link chips OUT of the index (`edges.ts`), and the v25
  port follows it; the graph's class family is membership-derived, not an
  edge-index family. **(2) The headless graph model** (`core/graph/`) —
  `model.py` ports `graph.ts` reduced: the node set is classes + standalone
  pages (aliases never vertices; parentless members of an effectively
  parentlessRole-"block" class excluded, the same extends-aware EXISTS the
  render predicate flattens), five structural edge families — mention /
  property / tag from the derived index via the applier-materialized
  `resolved_target_id`, parent main-node parentage, class OR-Set membership —
  with the block/alias endpoint rollup, parallel merging into weighted
  edges with the earliest contributing stamp, self-loop and out-of-tree
  drops, and per-node degree; NOT ported (each deliberate, documented in the
  module): the semantic/temporal co-occurrence families, the mass/content
  metrics, and the anchor/depth scope. `layout.py` is a deterministic
  Fruchterman–Reingold pass — golden-angle spiral init over sorted ids,
  fixed iterations, linear cooling, zero RNG, so same state → byte-same
  positions and input row order is irrelevant (a summation-order drift the
  determinism pins caught). `viewport.py` carries the world↔screen camera,
  pan/zoom-at-anchor, bounds fitting, and hit-testing. The store grows the
  `graph_topology(workspace_id)` seam (the workspace-scoped SQL) and
  `parse_query_language(workspace_id, text)` (a synchronized parse-only DSL
  seam — the resolvers read the mirror, so the parse holds the lock; the
  color-group edit path needs the AST without running it). **(3) The
  color-group layer** (`core/graph/color_groups.py` + `core/query/ast_v1_map.py`)
  — the `colorGroups.ts` port: the group model ({id, label, color, ast,
  error}, the persisted list order IS the first-match-wins priority), the
  at-load migrations (a legacy text-DSL `query` string parses through the
  slice-2 DSL with mirror-backed name resolution; a stored v1 AST rewrites
  through the pure `map-query-ast-v1-v2.ts` port — total over the v1 grammar,
  fail-loud with the tree path, v2 input must NOT re-map; failures KEEP the
  group, marked erroneous, painted nowhere), evaluation wrapping each group
  in the workspace-scope AST and riding the slice-1 runner (a runner failure
  skips that group, never the rest), and device-local persistence per
  workspace in `config.json` (the prefs.ts device-cache idiom). **(4) The
  GTK chrome** (`ui/graph_view.py`) — a fourth content-stack sibling behind
  the pinned "Graph" sidebar row (the cards-hub pattern, `tree.py` gained
  the parallel row + the cross-unselects): `Gtk.DrawingArea` with nodes as
  labeled circles (group color → own `#RRGGBB` → fallback), family-colored
  edge lines, drag to pan, scroll to zoom at the pointer, and a sub-4px
  drag that releases on a node opens it through the window's navigate seam;
  the color-groups popover lists / adds / edits / removes groups (label +
  color + a query-DSL text, the slice-2 grammar — the condition-builder
  dialog stays the query-token editor; an existing group's empty query
  keeps its AST, a label/color edit), persisting the device cache and
  live-recoloring on every store-change notify. This host has no PyGObject:
  the chrome is verified by py-compile + precedent; ALL logic rides the
  headless layers. No wire change: the fixture corpus stays at 51, both
  fixture-count pins untouched. Verification: +5 test modules (72 tests) —
  the backfill pins (structural convergence with a live-built index, the
  updated_at stamp, the property family's causal stamp surviving, idempotence,
  the end-to-end reopen-at-v27 migration, fresh-stays-empty), the topology
  pins (the node-set law, block/alias rollup, the five families, parallel
  merging with the earliest stamp, self-loop/out-of-set drops, degree, the
  deterministic ordering), the layout determinism pins (same-input identity,
  permutation independence, force sanity, finite output), the viewport
  pins, the v1→v2 mapping battery (every member-spec kind, the folded
  conditions, all ten date types, not/group recursion, the fail-loud paths,
  strict-parseability of the mapped tree), and the color-group pins (legacy
  DSL migration with mirror resolution, v1 AST migration, erroneous rows
  kept, first-match-wins, the runner-failure skip, live recolor on a class
  unassign, the device-cache round trip) — full gate green (`uv sync && uv
  run pytest` 1719 passed / 5 skipped / 2 xfailed, `uv run ruff check` +
  `uv run mypy src` clean). Lockstep: no wire change; the TS reference is
  the ported-from side, Flutter's graph surface is its own phase.

- **fix(protocol): the mention validator and the plaintext excerpt align on the wire contract — `customLabel` is the only text a mention stores (the retired `text`/`displayText` keys reject outright), and the live-e2e route drift after plan-07 is fixed.** Two findings from the search-text parity handoff, one slice. **(1) The strict content validator's mention branch** (`core/protocol/content.py`) still REQUIRED the retired captured `text` key and accepted `displayText` — the wire schema and the corpus (`mention-custom-label.json`) carry `customLabel` only: the validator now accepts exactly `content-mark.ts` `mentionTokenSchema` — `{type, targetNodeId, customLabel? (1..512), linkId?, isEmbedded?}`, strict keys, the retired captured-text keys rejected as unknown keys like any `.strict()` extra (the fail-loud / no-backward-compatibility law). `plaintext_excerpt` (the `plainTextExcerpt` port) had the same drift — it read `displayText ?? text`; it now reads `customLabel ?? the resolver's current target name`, gaining the TS-parallel optional `resolve_mention` parameter (a bare mention contributes nothing without a resolver — the store's mention-resolving search path already did this). `ui/ast_render.py`'s mention pill had the third drifted reader (`displayText`); it reads `customLabel` now. The strict-xfail pin in `test_store_fixtures.py` flips to a live pin and gains the fixture-bytes arm — the corpus's actual `contentAst` bytes validate through the strict grammar — plus the rejection pins for `text`/`displayText` mentions. The test suites that built mentions with the retired shape (store flatten/promotion arms, edges, node stats, query DSL/execute fixtures, the renderer battery, the repo's own catch-up fixture) move to the wire shape. **(2) The live end-to-end route drift** (`tests/test_live_server_e2e.py`): two of three tests 404'd against the current monorepo server — plan-07 retired the object/search/query content routes (`/api/objects*`, `/api/search`, `/api/query`), and with them the e2e's read probes and its REST property-value write. The rewrite keeps the file's intent on the surviving surface (mirroring `verify-min.mjs`'s post-plan-07 posture): the read probe is now `GET /api/resolve?name=` (name→id against the server's derived FTS state), the class catalog (`GET /api/classes` / `/api/classes/:id` — names, extends edges, member counts), and the operation feed (`GET /api/operations` — the log is the authority; the block's create envelope is audited there with the mention token intact); the server-stamped write surface is `POST /api/property-schemas` + `POST /api/classes/:id/properties` (schema-level default + the `class.property.set` binding), with the property VALUE now authored through the engine's own relay path (values ride the log — the one write plan-07 kept); the seeded mention rides `customLabel`. The self-skip posture is unchanged (no built server / no node → skip). No wire change: the fixture corpus stays byte-pinned, both fixture-count pins untouched. Verification: the previously-xfailed contract pin now passes + the new fixture-bytes validator pin; full gate green (`uv run pytest` 1647 passed / 5 skipped / 2 xfailed, `ruff check` + `mypy src` clean); all three live-e2e tests verified against the built monorepo server (`NOTEES_V2_ROOT=<checkout> uv run pytest tests/test_live_server_e2e.py` — 3 passed: relay ingest/catch-up/WS realtime/replay determinism + the surviving read surface, the server-stamped schema/binding/value convergence + effective defaults, and the login/workspace listing). Lockstep: the validator now matches the TS reference exactly; Flutter convergence on the mention grammar is its own phase (the fixture already lives in the shared corpus).

- **feat(store): search-text parity — the `content` condition's plaintext source gains asset names, property values, mention resolution, and diacritic folding (the slice-1 R1 follow-up).** GTK search text was the `plaintext_excerpt` only; the TS contract (`packages/store/src/content.ts` `extractSearchPlaintext`, folded at index time by `search.ts` `foldSearchText`, mirrored client-side by the web's `sectionViewResolve.ts` `searchPlaintextOf`) is the mention-resolved content plaintext + ASSET ORIGINAL NAMES (`asset_ref` tokens, first name per asset, quote/heading children walked) + text-ish PROPERTY VALUES (authored visible rows only; a text carrier's content one level deep; select/multi_select option labels with the raw-id fallback; numbers in JS string form; date/object refs structural and out) + NFD mark-stripping at derive time. The mirror gains the derived `nodes.search_plain` column — the TS `search_index` row's counterpart — while `content_plain` stays the display excerpt (content-only, unfolded; the UI tree/search-label/name-resolution consumers untouched). The derivation lives in module-level pure-over-`conn` helpers (`_derive_search_plain` and its parts, the `content.ts` port) so the v27 migration backfill (guarded ALTER + a per-row derive, the `reindexAllSearch` precedent) and the snapshot-restore path share it; `SCHEMA_VERSION` 26 → 27 (purely additive). The appliers refresh the column at the TS `reindexNode` trigger sites within GTK's op coverage — `object.create`, `object.update` content writes (incl. the promotion flatten), `class.create` (unconditional, the conversion path adopts the content), `class.update` with content, `property.set`/`property.unset` — and deliberately NOT `asset.attach`/`detach`: a late attach heals only on the node's next reindex, the documented online-index staleness (a rename never fans out to mentioners either; both staleness arms are pinned). The compiler's content conditions flip one place (`TEXT_BACKEND = "search_plain"`): contains/starts/ends/eq/neq/exists and the GLOB token-prefix `fts` arms now read the folded superset row; the needle stays VERBATIM at compare time (the one mirror pair — `peru` matches "Perú" while `perú` matches nothing folded, exactly the TS SQL semantics; `eq` stays the BINARY case-sensitive full-row equality). No wire change: the fixture corpus stays at 51, both fixture-count pins untouched. Verification: +1 test module (37 tests) — byte pins on the derived row (diacritic fold, asset first-name-per-asset, the late-attach staleness, bare/custom-label/ghost/cyclic mention resolution, the rename no-fan-out arm, text/select-label/raw-id/number/email/object/carrier property arms, the option-removal staleness + the re-write reindex), the web-spec contains arms (needle in title / body / asset original name / text property value / mention target / nowhere), the compare-op arms (eq case-sensitivity, the property-carried exists row, the fts arms over the folded row with verbatim diacritic terms), the `property.unset` refresh, and the migration/snapshot-restore backfill pins — plus the slice-1 golden SQL pins flipped to the new column; scoped + full gate green (`uv sync && uv run pytest` 1644 passed / 5 skipped / 3 xfailed, `ruff check` + `mypy src` clean). Lockstep: no wire change; the TS reference is the ported-from side, Flutter's query surface is its own phase.

- **feat(query): the condition-builder UI ships — the register-driven builder model over the v2 AST, the GTK builder dialog, and authoring that writes back through the content-update op path (P2 slice 4).** The `query` token's section is editable now: "Edit query…" (or "Set a query…" on the unspecified AST — the affordance IS the empty state) opens a modal condition builder and the commit rewrites the token's `queryAst` through the normal `object.update` content path, the same seam the editor's save uses. **The model** (`core/query/builder.py`, headless — the `apps/web/src/features/collection/filterBlockModel.ts` + `filterQuery.ts` prune/create port): the 19-family taxonomy + the group constructors ride the add-menu register verbatim (order + the config gating), `create_condition` seeds the per-family half-typed defaults, `block_view` unwraps the NOT-sugar rows (NOT-around-a-group stays a group wrapper), `family_of` maps the v2 wire families onto the register (name joins content; date field picks created/edited; the nodeField fields pick type/placement/cover/banner/alias), `operator_options`/`operator_of`/`apply_operator` carry the per-family Odoo registers — the membership labels flip ONLY the wire op (in ⇄ not-in, target verbatim across ids/query/self), the set bits re-materialize exists/not-exists, the content/property/date negatives and the wire-field "is not set" sugar as NOT-wraps, the boolean rows (type/placement) ride the owner's single-selector ruling (NO operator column — is/is-not is redundant with a non-nullable boolean; negatives via group Exclude), boolean properties fold the truth value into the value column (is-true ≡ set + true), and the property operator list adapts to the selected schema's type; `apply_target_mode` rebuilds ONLY the target (operator survives; set rows re-materialize preserving polarity; Manual carries the picked ids) — one deliberate deviation: a Query→Query switch keeps the nested root verbatim (the web's rebuild-from-empty is unreachable through its dropdown, which only fires on change; the GTK model makes the unreachable case safe instead of discarding a typed nested query); `filter_query_to_group` prunes half-typed drafts exactly like the web (blank content values, empty ids picks, empty schemaIds, blank timestamps, missing range bounds drop; the set bits, the self target, date-within, the node-field predicates survive; nested groups recurse, emptied ones drop, a not drops with its pruned child); `draft_or_factory_ast` commits the pruned draft or the factory match-all (the emptied builder is the unspecified query, never an absent AST). Draft edits ride the frozen AST dataclasses — the immutable-tree verbs (`replace_child_at`/`insert_child_at`/`remove_child_at`) are the chrome's edit primitives. **The serializer** (`dump_query_ast` in `core/protocol/query_ast.py`): the strict parse's exact inverse (`parse → dump` byte-round-trips; absent option fields stay absent — the zod `exclude_none` parity); draft shapes the parser rejects (empty ids, blank values) stay dumpable — the prune is the draft → wire boundary, the parse rejection the producer-side gate. **The write path** (`ui/query_tokens.py`, headless — the `queryTokens.ts` port): the token address space (`query_token_indexes`), the AST splice at the token's index (`update_query_token_ast`) and the append (`append_query_token`), fail loud on a wrong-kind/out-of-range index, and the `object.update` envelope in the editor-save shape; `QuerySectionView` gains the token's content-array `token_index` (the write address — `ast_render` enumerates the tokens), the hosting page id staying the `self` anchor. **The chrome** (`ui/query_builder.py`, the `FilterBlockBuilder.tsx` model adaptation — NOT a pixel port): one `Adw.Dialog` with `[Exclude] [Type] [Operator] [Value] [Mode] [×]` rows (the property schema pick rides the type slot like the web's PropertySchemaPick), the AND/OR group heads + nesting (nested groups indent; NOT-around-a-group renders as an excludable group box), the Manual/Query/Self mode switch (Query mode embeds the recursive group editor; Self is offered only with a hosting page or on a row already riding it — the web's host-less rule), the Manual picker reusing the shared `node_picker` popover with per-family candidate sets (class nodes / pages for tags / non-class nodes otherwise), and the "Aggregation (JSON)" expander — the documented slice-4 escape hatch: pre-filled from the token's AST, strict-validated through the wire parser on Apply (an invalid doc blocks the commit with the error inline), empty = no aggregation; the grouped grid itself already renders through the slice-3 aggregate arm. Chrome adaptations (each deliberate, documented in the module): text value edits write the draft silently instead of per-keystroke rebuilds (typing keeps focus; the web is controlled per keystroke), reorder chevrons are scoped out (delete + re-add covers ordering), the Raw-AST exchange mode is not ported (web-optional). **The window wiring**: the section's edit seam threads `PageViewWidget.show_page` → `QuerySectionWidget` (the unspecified AST's "Set a query…" IS the empty state), and the commit builds the envelope (actor/device/clock from the standing config seams), enqueues + applies optimistically (op-id dedupe makes the server echo harmless), syncs on the worker, and re-renders the page. The store grows `property_schema_summaries` (id/name/type + select options — the `BuilderFacts.boundProperties` read). Verification: +2 test modules (78 tests) — the `filter-block-model.test.ts` matrix ported (the representative battery renders an offered operator at both polarities with the boolean rows' empty register, the full orthogonality audit per family/op/target-mode, the mode-switch preservation, the reported childPath-subquery scenario, both coupling directions, the boolean single-selector ruling incl. the reachable/stability walk, the not-wrap arms) + the `filter-query.test.ts` prune/create battery (createCondition defaults per family, the add-menu register order + config gating, every prune case incl. the nested-query-target and self-target pins) + the draft-tree verbs' fail-loud pins + the dump round-trip corpus (every scope shape, absent-field omission, half-typed-draft dumpability) + the token helpers (address space, splice/append, fail-loud, the envelope shape) and the live round-trip over an envelopes-built world (the write lands in the mirror through the real applier, the store notify fires, the render plan carries the new AST + token index + host) — full gate green (`uv sync && uv run pytest` 1607 passed / 5 skipped / 3 xfailed, `ruff check` + `mypy src` clean). Lockstep: no wire change; the fixture corpus stays at 51, both fixture-count pins untouched; the TS reference is the ported-from side, Flutter's query surface is its own phase.

- **feat(query): live `query` content-token sections — the page view runs the token's own AST with coalesced re-runs on store changes (P2 slice 3).** The `query` block-scale token is a live section now, not a placeholder: `ui/ast_render.py` renders it as a `QuerySectionView` run request — the token's OWN `queryAst` verbatim (the default-tab vs hosted-view distinction is web-specific; GTK renders what the token carries) + the hosting page's node id as the `self` anchor (the window's shown-node id, threaded as `ast_to_view(..., self_node_id=...)`; the compiler's fail-loud-without-host contract makes the host part of the run identity, so the chrome always supplies it). `ui/query_run.py` (headless, GTK-free — the `apps/web/src/ui/queryRun.ts` port) owns the run contract: the `(AST key, version, selfNodeId)` identity skips already-current runs; a store notify mid-flight marks the run dirty and schedules exactly ONE trailing run (a burst costs at most two executions); an invalid AST surfaces as the cached error state (the "invalid query" line), never a crash; the empty match-all is unspecified (`is_unspecified_query_ast` — the `isUnspecifiedQueryAst` port) and renders "Set a query to see results." instead of loading the graph; an `aggregation` AST takes the aggregate arm (`dimension_count` rides the state). `open_query_result` ports the render-cascade navigation (document-chrome rows open directly — classes excepted by the predicate's first arm; an inline block resolves to its nearest document-chrome ancestor with the parent-page condition + the seen-set walk; the parentlessRole "block" demote rides an injected resolver), and `aggregate_column_label`/`format_aggregate_cell` port the grid labels (class names resolved from the mirror, property names via the new tiny `LocalStore.property_schema_names` seam, short-id fallbacks). The store grows a change-notification seam: `LocalStore.subscribe(listener)` (returns the unsubscribe) fires AFTER a mutating `apply_remote` commits — plus `restore_snapshot` and `wipe` — while dedupe hits and LWW drops stay silent; a failing listener is logged, never propagated (the sync engine applies on a worker). `ui/query_section.py` is the chrome: worker-thread notifies marshal onto the main loop through a pending-flag idle callback (the React-tick batching mirror — an apply burst collapses into one refresh), rows replace in place (deletions/renames converge with no manual refresh), the result cap mirrors `QUERY_RESULT_CAP` (200) with an honest "…and N more" line where the web's load-more will go, and the section unsubscribes when the page view re-renders (`PageViewWidget` stops its sections on every `show_page`; the destroy signal is the fallback). Verification: +1 test module (44 tests) — the render-plan pins (query token → run request with the right `self_node_id`; self-hosted plan runs through the store seam returning the host's children; the without-host fail-loud pin), the controller matrix (initial pass, self in the run identity, notify re-runs + result replacement, unchanged-triple skip, the mid-flight burst = two runs pin, per-notify semantics when idle, the error state, the aggregation arm, unspecified-clears-stale, disabled holds, stop unsubscribes), the open-navigation pins (direct page, embedded block → containing page, the inert-middle walk, the parentless-block fallback, JSON/list class_ids reads), the label/cell pins, the store-notify pins (mutating apply notifies post-commit, dedupe/LWW silent, unsubscribe, wipe, failing listener, the committed-state re-read, `property_schema_names`) and the live-seam end-to-end pins over an envelopes-built world (self-hosted children run, deletion/rename converge on the apply's own notify, the aggregate grid) — plus the query-token render-plan pins in `test_ast_render.py` — full gate green (`uv sync && uv run pytest` 1525 passed / 4 skipped / 3 xfailed, `ruff check` + `mypy src` clean). Lockstep: no wire change; the TS reference is the ported-from side, Flutter's query surface is its own phase.
- **feat(query): the text query DSL ships — `packages/query/src/dsl.ts` ported to `core/query/dsl.py` and wired into the global search entry (P2 slice 2).** GTK search understands the query language now: `parse_query_language` ports the full grammar (scanner + recursive-descent parser, fail loud with positions) — the field keywords `class:`/`isClass:`/`isEmbedded:`/`text:`/`name:`/`linked:`/`coverAsset:`/`bannerAsset:`/`aliasedNode:`/`prop:<schema><op>val` plus the bare `<schema>:<op>val` shorthand, the twelve operator tokens (`:`/`:=`/`=`/`!=`/`:>`/`>`/`:>=`/`>=`/`:<`/`<`/`:<=`/`<=`), implicit/explicit AND, OR, NOT, parenthesized groups, quoted phrases, number coercion (int/float — the TS `Number()` parity), token-vs-slice value reads (URLs/DOIs/ISO dates survive whole), and the `QueryLanguageResolvers` injection contract (class/schema/node NAME resolution is the caller's job, the web `resolversFor` policy). The emitted shapes ride the slice-1 compiler verbatim: `class:`/`linked:` → `member class/links in ids[...]`, `isClass:`/`isEmbedded:` → `nodeField` eq "true"/"false", the wire node fields → `nodeField` eq/neq/exists (uuid passthrough, node-name resolution otherwise), `name:` → title contains/eq/neq/exists, `prop:`/bare-schema → property conditions over all eight ops. `looks_like_query_language` ports the search-box heuristic (field prefixes minus URL schemes, standalone AND/OR/NOT, quoted phrases). The wiring: the window's search entry (`ui/window.py`) detects DSL input and calls the new `LocalStore.run_query_language` seam — resolvers close over the mirror INSIDE the lock (case-insensitive name → id over the active `class`/`property_schema` rows and `nodes.content_plain`; the web's `resolveNodeByName` additionally consults name equivalents, which the GTK mirror does not read — the documented divergence), `QueryLanguageError` surfaces the parser message as the toast, and the plain-text path is byte-identical to slice 1. Verification: +1 test module (48 tests) — the `dsl.test.ts` case list ported as parity pins (every field kind, the 11-entry operator matrix, composition, quoting, coercion, the exact fail-loud messages incl. the known-fields listing and the position pin, the emitted-AST round-trip through the strict parser, the heuristic pins) plus six store-seam pins over an envelopes-built world (class/schema/node name resolution, case-insensitivity, `linked:` backlinks, OR composition, the fail-loud seam errors) — full gate green (`uv sync && uv run pytest` 1479 passed / 4 skipped / 3 xfailed, `ruff check` + `mypy src` clean). Lockstep: no wire change; the TS reference is the ported-from side, Flutter's query surface is its own phase.
- **feat(query): the query surface ships — strict query-AST v2 parse, the full `packages/query` compiler + executor port over the mirror, and a global search entry (P2 slice 1).** GTK moves from carrying `queryAst` opaquely (the P1 arm) to a live query surface, all headless until the chrome: `core/protocol/query_ast.py` strict-parses the v2 grammar (frozen dataclasses + hand-rolled validators — pydantic's lax coercion would break the zod `.strict()` fail-loud contract; `version: 1` and every unknown condition/family/op/target-mode/extra-key rejects outright), and `core/query/` ports `packages/query` method-for-method: `placeholders.py` (the `{today}`/`{this_week}`/`{this_month}`/`{this_year}` tokens, injected `now`, local-day → UTC `Z` boundaries — the TS `toISOString` shape), `compiler.py` (the whole grammar: all 4 scopes, all 6 condition types, all 10 member families × in/not-in/exists/not-exists/exact × ids/query/self target modes, sort with the NULLS-last + id tiebreak, aggregation with the deterministic `#n` column-label collision suffix, positional-`?` params pushed in SQL text order), and `execute.py` (`run_query`/`run_aggregate`/`count_query`/`matches` over a stdlib `sqlite3.Connection`). Schema deltas are isolated in the compiler's constants block per the plan: **D1** — the mirror's `nodes` (plural, composite PK) + a mandatory `workspace_id` compile option threading the predicate into every `nodes`/`edge` scan (one helper generates every table reference); **D2** — no FTS tables, so content conditions read `nodes.content_plain` (contains/eq/neq/starts/ends/exists as LIKE/equality arms; `fts` as ASCII-case-folded GLOB token-prefix arms — the unicode61 prefix-AND semantics — until the FTS backend lands; `build_match_expression` ports verbatim for that follow-up), with the R1 divergence documented (GTK search text is the plaintext excerpt only: no asset names / property values / diacritic folding); **D6** (found in bring-up, beyond the plan's A–E) — the fresh dynamic-target scopes alias `m`, not the TS's reused `n`: on system SQLite the shadowed name hijacks the correlated CTE anchors in the containing-page/root walks, so every condition fragment now takes the row alias as a parameter. `LocalStore` gains the `run_query_ast`/`run_aggregate_ast` seams (`@_synchronized`, parse + compile + execute on the private connection). The UI slice: a `Gtk.SearchEntry` in the previously empty sidebar header (`ui/window.py`) running `{content contains <text>}` over the mirror on every keystroke, a results popover (Enter/click opens the node), the pure `ast_for_search_text` builder + `search_wire` serializer in `core/query/search.py`, and the node-picker substring filter deliberately untouched. Verification: +5 new test modules (~215 tests) — the strict-parse matrix ported from `query-ast-v2.test.ts`, placeholder unit + execution pins ported from `placeholders.test.ts`, golden SQL + full param-vector pins per family/op/target-mode (including the R3 text-order pins that caught a real bind-order bug in the page/root dynamic arms), and the France/Paris result-set parity suite ported from `query.test.ts` (scope isolation incl. a multi-workspace no-leak pin, class closure, title-is-content name conditions, the property effective read model with tombstone/option-membership/PC4 arms, backlinksWithRollup, the hierarchy families, dynamic + self targets, tags-as-links, contains-vs-fts, date windows, soft deletes, case-folding sort, aggregation grids, date-node point/range refs, nodeField predicates, archived-class resolution, the parentlessRole demote) — plus the fixture gate's new convergence arm: `query-ast-v2.json` strict-parses AND compiles + executes against the mirror (self hosted), with the self-without-host and referencePath/root-exists fail-loud pins. No wire change: the corpus stays at 51 fixtures, both fixture-count pins untouched. Full gate green (`uv sync && uv run pytest` 1431 passed / 4 skipped / 3 xfailed, `ruff check` + `mypy src` clean). Lockstep: no fixture/wire change; the TS reference is the ported-from side, Flutter's query surface is its own phase.

## 2026-11-03
- **feat(e2ee): CAS asset encryption — the plan-05 GTK track (K1–K3): asset-byte helpers in the protocol crypto module, the `/api/assets` client channel with declared encrypted uploads, and the keyed seal/open boundary.** No wire/protocol change — the IV is server metadata (the `asset.iv` column), never op-log state, so the fixture corpus stays untouched. `core/protocol/crypto.py` gains the asset half of the reference contract (the `encryptAssetBytes`/`decryptAssetBytes` ports): `encrypt_asset_bytes` seals with a random 12-byte IV kept EXTERNAL to the blob (it rides the upload multipart + the server's `asset.iv` metadata as b64u) and a `tag(16) ‖ ct` blob — tag first, the same order as the payload slot's `ct`, the cross-client byte contract TS/Flutter must match for asset sharing; `decrypt_asset_bytes` opens it, failing loud (ValueError) on wrong key or tamper. The keyed boundaries port `sealAssetIfKeyed`/`openAssetIfSealed`: `seal_asset_if_keyed` — keyless sessions are legal, plaintext passthrough with iv None, exactly today's upload; `open_asset_if_sealed` — plaintext passthrough, sealed-without-key returns None (the caller's honest-fallback surface, NEVER a throw), sealed-with-key raises the honest error on tamper, never a corrupted render. `NoteesClient` gains the asset channel it never had (`post_asset`/`get_asset`/`get_asset_info`, `AssetInfo`/`AssetUploadResult` models): encrypted uploads declare themselves — `encrypted`/`mimeType`/`iv` multipart fields, sniffing skipped server-side, declared type stored — while plaintext uploads keep today's file-part-only shape (a plaintext client cannot smuggle a declared type; `iv` without `mimeType` fails client-side before any request); the upload response and `GET /assets/:id/info` carry `iv`; downloads return the raw stored bytes, openness staying the crypto boundary's job. GTK asset UI note: the chrome renders block-scale asset tokens as dimmed placeholders only (no upload/download surface exists yet — `ui/page_view.py`), so the keyless rules live in the boundary for that future UI to call, same as the web's AssetView chip precedent. Verification: +9 crypto tests (round-trip, tag-first layout pin, external-random IV, wrong key, tamper, malformed iv/blob, keyed/keyless passthrough both directions, sealed-without-key ⇒ None, tamper-with-key raises) and +7 client tests (multipart field presence/absence, declared mime + iv in the body, pre-request mime guard, objectId/workspace-header pass-through, raw-byte download, info iv, and the sealed upload→info→download→open round trip through the boundary) — full gate green (`uv sync && uv run pytest` / `ruff check` / `mypy src`). Lockstep: no wire change, no fixtures; the TS reference (plan-05 M1/M2) ports the same helpers and fields in parallel, Flutter is the remaining arm (plan 05 D-track).

- **feat(protocol): query-AST v2 fixture lockstep — `query-ast-v2.json` joins the vendored corpus (50 → 51 fixtures).** The monorepo's query-AST v2 grammar slice ships a 51st fixture: an `object.create` envelope whose content carries a query token with a v2 `queryAst` (every folded condition shape — the member family across target modes, content, name, property, date, and the nodeField predicates). Clients carry query ASTs opaquely, so this arm is corpus re-vendor + count pins only: the bytes are copied verbatim (sha256-identical to `packages/protocol/fixtures/query-ast-v2.json`) and the fixture joins `WIRE_ENVELOPE_LIST_FIXTURES` in `test_fixtures.py` — the strict content grammar accepts the query token as a loose record and the replay/idempotence/convergence suites absorb it through the existing auto-glob (REPLAY_EXCLUDED unchanged). No wire-model or applier changes. Verification: full gate green (`uv run pytest` 1200 passed / 4 skipped / 3 xfailed, `ruff check` + `mypy src` clean). Lockstep: TS reference + GTK converge on the fixture; Flutter is the remaining arm.

- **fix(e2ee): locked snapshot skip + counted warning (TS parity), cursor-skip hardening, debt register.** A locked engine (no provider) that probes a SEALED snapshot now declines it BEFORE the download with an explicit SQLite-magic check and a counted once-per-snapshot warning (`workspace key unavailable: skipped a sealed snapshot …; seq catch-up covers the content`) — previously the opaque blob re-downloaded every round and failed silently inside `restore_snapshot`. A per-snapshot marker declines re-downloads while locked and clears on `set_crypto` (a later lock cycle re-warns). Key-install failure at workspace open now degrades through one broad `except` (any exception ⇒ keyless locked sync, never a failed round), and the two specific arms also clear a stale provider. Removes a duplicate module-level logger. New `docs/lockstep-debt.md` registers the two known gaps against the web reference design: locked-state UI (log-only here; recovery via re-login — the web's `EncryptedLockedBanner` + `UnlockWorkspaceModal` are the port target) and snapshot-upload sealing (unwired — no upload path yet; seal-at-upload lands with that path). Verification: +1 engine test (locked + sealed blob ⇒ skip, warning counted once, no re-download, marker reset on key install), full `test_sync_engine.py` 42 green, `ruff`/`mypy src` clean.

- **feat(e2ee): full custody at sync — the scrypt KDF record, master/workspace key wrap, envelope + snapshot encryption, key routes, and the engine crypto boundary (plan 04, GTK track G1–G5).** The GTK client is now a full custody citizen. `core/protocol/crypto.py` gains the custody half of the reference `packages/protocol/src/crypto.ts`: `b64u_encode`, the scrypt KDF record (`create_kdf_record`/`unwrap_master_key` — verifier HMAC-SHA256 over the DERIVED key, constant-time, fail-fast on a wrong password), workspace-key wrap/unwrap (`iv ‖ tag ‖ ct` b64u — the documented OPPOSITE order of the payload slot's tag-first `ct`), `encrypt_payload`/`encrypt_envelope`/`encrypt_bytes`/`decrypt_bytes`, and `WorkspaceKeyCrypto` (the TS `EnvelopeCrypto` port, closed over raw key bytes — this port has no CryptoKey equivalent). Interop is pinned by the plan-01 golden vector ported with EXACT-string equality: fixed salt/iv/master bytes, expected wrapped record + verifier produced by node:crypto with the server's auth.ts formulas (`tests/test_crypto.py::TestGoldenVector` — verified against the TS vector by hand, plus round-trips, wrong-password verifier failure, and the iv‖tag‖ct vs tag-first layout pin). `NoteesClient` gains `fetch_workspace_key`/`store_workspace_key` (GET → `{wrappedKey}`|404-as-None; PUT → first-write-wins — a 409 re-GETs and ADOPTS the winner's record, never a split-key blind upsert; operator/API-key principals 403 loud). `SyncEngine` takes `crypto=` + `set_crypto`: push seals a COPY of each chunk before `submit_batch` (outbox rows + the mirror's self-apply stay plaintext), the single apply chokepoint `_apply_guarded` decrypts before `store.apply_remote`, snapshot blobs decrypt at download, and the LOCKED semantic lands — no provider + `$e` envelope ⇒ skip with a counted warning (`engine.skipped_encrypted`) BEFORE store validation, cursor advances, sync stays green; plaintext still applies; a decryption failure WITH a key raises (real corruption, fail loud). Login (`ui/login.py`) derives the master key on the worker thread from the login password against the server's `kdf` record — a wrong password fails at the verifier and surfaces as a login-page error, never a silently unencrypted session — and hands the raw key to the window's session-scoped in-memory holder (NEVER `ClientConfig`/config.json: keys are memory-only, no keyring, same threat model as the web). `_on_workspace_selected` fetch-or-ensures the workspace key on the worker before the first sync round (generate + wrap + PUT on 404; adopt on 409). Key-only (API-key) configs keep no master key — the server 403s operators on `/key` — so those workspaces stay keyless by design (locked sync, plaintext writes). Docs: the module docstrings carry the boundary contract. Verification: +29 crypto tests, +8 key-route tests, +9 engine boundary tests (wire-ciphertext/plaintext-mirror, locked mixed batch green with cursor advance, locked skip never tripping the store's `$e` raise, tamper-with-key fails loud, provider swap mid-session, sealed snapshot restore keyed/keyless, two-device convergence) — full gate green (`uv run pytest` 1198 passed / 4 skipped / 3 xfailed, `ruff check` + `mypy src` clean) plus a deliberate live-e2e run against the built monorepo server. Lockstep: runtime behavior convergence, the wire and fixtures untouched; the TS reference carries the same `EnvelopeCrypto` seam, Flutter is the remaining arm (plan 04 F-track).
- **feat(protocol): E2EE fixture lockstep — decrypt port for the `$e` slot, the encrypted fixture joins the gate and the replay corpus (49 fixtures).** The corpus re-vendors byte-identical from the monorepo (48 → 49 files — `envelope-encrypted.json` plus six drifted files caught by the byte-pin check). New `core/protocol/crypto.py` ports the decrypt half of the reference `packages/protocol/src/crypto.ts`: unpadded base64url, AES-256-GCM with the tag-first `ct` layout (`tag(16) ‖ ct`, NOT WebCrypto's `ct ‖ tag` — a misread fails the fixture) and the `{id}.{workspaceId}` AAD; plaintext envelopes pass through. The fixture gate (`test_fixtures.py`) gains the file in the coverage list and a decrypt-and-validate test — the fixture key opens both envelopes and each OPENED payload validates against the strict per-opType schema; `test_payloads.py` pins that a raw `$e` payload bypasses strict validation (producer gate) while opened payloads must validate. The replay harness (`test_store_fixtures.py`) decrypts crypto-carrying fixtures before replay — the auto-glob idempotence/convergence suites now replay the encrypted fixture DECRYPTED (REPLAY_EXCLUDED unchanged: the apply-time `$e` rejection stays — runtime sync still refuses encrypted ops; sibling custody is a later phase). The list round-trip dump takes `exclude_none` — the encrypted fixture is the first without a `client` claim, and the wire omits absent claims. `cryptography` joins the deps. Verification: +7 crypto vector tests (fixture deep-equal, wrong-AAD, tamper, tag-misread, wrong-key, passthrough, b64u), +1 decrypt-and-validate gate test, +1 producer-gate pin, full gate green (`uv run pytest` 1161 passed / 4 skipped / 3 xfailed, `ruff check` + `mypy src` clean on the touched files — one pre-existing F841 lives in another slice's untracked `tests/test_node_stats.py`, not this change). Lockstep: TS reference + GTK converge on `envelope-encrypted.json`; Flutter is the remaining arm (plan 03).


- **feat(store): the materialized node counts land — `node_stats` at schema
  v26 (the `packages/store/src/stats.ts` port) — and the strict content
  validator gains the `heading` token (the `content-mark.ts`
  `headingTokenSchema` parity, closing the gap the edge-index slice
  documented).**
  - **`node_stats`** (`data/store.py`, `_NODE_STATS_DDL` + `_migrate_v26`,
    purely additive CREATE IF NOT EXISTS, the v25 precedent): one derived
    row per node — child_count from node_child_order, backlink_count =
    COUNT(DISTINCT source_id) of incoming edges, reference_count = COUNT(*)
    of outgoing edges, descendant_count via the recursive node_child_order
    walk, updated_at mirroring the node row's own (the LWW-winning op — the
    order-invariance law: same semantic state → same rows). GTK's
    nodes/edge tables carry workspace_id, so every query threads it (the
    `_rebuild_edges` idiom). `_rebuild_node_stats(workspace_id, ids)`
    expands the seeds to their self-inclusive ANCESTOR CLOSURE (recursive
    CTE, ids without a live node row skipped by the node join) and
    DELETE+re-INSERTs the closure. Wired at the TS `rebuildNodeStats`
    trigger points: `_rebuild_edges` now consumes its own return value (the
    live tail + the gone arm's orphaned targets), the object.create fresh
    tail (counts LAST, after the child_order insert — the stats.ts
    create-tail order), object.delete arm-3 archival + soft-trash
    (node + ancestors), `_permanently_delete_subtree` (ancestors ∪
    lost-backlink targets — collected from the purged sources' edge rows
    BEFORE the delete, the TS `lostBacklinkTargets` port — plus the purge of
    the subtree's own stats rows), object.restore, object.move (the seeds
    are the node + the PRE-move ancestor chain, captured before the
    reparenting — the closure walks current parents, so both chains
    recompute), `_trash_text_carrier_if_orphaned` (target + ancestors, AND
    the edge reresolve the TS fires on the carrier liveness flip — closing
    the last gap the edge port documented), and
    `wipe()` (the rows die with the nodes). Since `_rebuild_edges` rebuilds
    stats internally, every edge-deriving surface (content writes, property
    set/unset, tag assign/unassign, the create re-issue carrier) keeps
    counts in the same transaction for free. No GTK reader consumes the
    table yet — this is derived-state parity that unblocks the
    backlink-badge/section chrome.
  - **`heading` in the strict grammar** (`core/protocol/content.py`):
    `validate_content_token` gains the `heading` branch — the same INLINE
    union as `quote` (block-scale children like `embed_ref`/`code_block`/
    `hr` fail loud, children recursively validated, strict keys) — and
    `plaintext_excerpt` recurses heading children like quote (the
    `plainTextExcerpt` parity); `stringify_content_ast` flattens headings
    through the excerpt automatically (NOT a promotion survivor, the TS
    stringify contract).
  - **Verification:** new `tests/test_node_stats.py` (8 tests: the four
    counts after a tree build + mention/tag, untag recomputing both sides,
    soft-trash row stability, permanent-delete shrinking ancestors +
    recomputing lost-backlink targets, the updated_at mirror, the move's
    old/new chains, gone-node rows skipped by the node join, wipe) +
    `tests/test_content.py` heading coverage (excerpt recursion, validator
    accept/reject, the stringify flatten) + the full gate (`uv run pytest`
    1161 passed / 4 skipped / 3 xfailed, `uv run mypy src` clean).

## 2026-11-03

- **feat(store): the derived reference-edge index — the `edge` + `node_link`
  tables land at schema v25 (the `packages/store/src/edges.ts` deep port).**
  Edge is the single derived reference index, never authored: the appliers
  rebuild a source node's rows from its stored content (mention + typed_link
  tokens — top level AND inside `quote`/`heading` children), its PG5
  visible-set property values (node-typed refs, verb = propertySchemaId;
  datetime refs fan out to the deterministic chain ancestors — a day ref
  also yields the month + year edges, a month ref the year edge; `{start,
  end}` ranges project each present side), and its tag membership (the
  2026-11-03 "tags are links" ruling — one `type='tag'` edge per
  `nodes.tag_ids` entry, derived from the surviving row, never the tag
  node's liveness). Edge ids are deterministic (sha256 over
  source/type/target/verb/metadata/occurrence, the per-key multiset counter
  in desired order), so wipe -> replay -> identical rows; `resolved_target_id`
  materializes the alias-terminal at write time via the LIVE-AWARE chain
  walk (a trashed/deleted/missing row is its own terminal; cycle -> the
  starting id; depth cap 32) and `reresolve_edge_targets` re-materializes it
  after alias writes and liveness flips through the pointer-based reverse
  closure. The `node_link` assertion table syncs mention tokens carrying
  `linkId` (upsert by linkId, stale sweep). PB1 holds: the permanent-delete
  purge drops the subtree's OUTGOING rows only — incoming edges from
  surviving sources keep theirs. `created_at`: the applying envelope
  timestamp, except property edges which stamp their causal value row (ms
  epoch -> ISO); the conflict path keeps the EARLIEST stamp
  (`COALESCE(MIN(...))`, the TS upsert parity). Triggers (each inside the
  applier's own transaction, the TS 2026-11-03 call-site parity):
  `object.create` (fresh tail + the tagIds re-issue branch), `object.update`
  (`contentAst` -> rebuild; `aliasedNodeId`/`active` -> reresolve),
  `property.set`/`property.unset` tails, `tag.unassign`, `object.delete`
  (arm-3 archival + soft-trash reresolve), `_permanently_delete_subtree`
  (source purge + reresolve), `object.restore` (reactivation reresolve),
  and `wipe()` (workspace-scoped delete).
  - **Schema v25** (`data/store.py`): `_EDGE_DDL` (edge + node_link +
    indexes, `packages/store/src/schema.ts` web-schema-parity columns)
    joins `_CANONICAL_AUX_DDL`; `_migrate_v25` is the purely additive
    CREATE IF NOT EXISTS (the v23 precedent) — a no-op for fresh databases.
  - **Deferred:** `node_stats` — the TS rebuild recomputes stats for the
    source and every affected target, but GTK has no stats table or
    readers, so the rebuild's return value (the affected set) is currently
    unused by callers. No `property.empty` trigger (the op does not exist
    in GTK yet).
  - **Known follow-up:** the GTK strict content validator does not know
    the `heading` token yet — the edge walker recurses heading children
    regardless (shape-tolerant, the TS walker's contract), so heading-carried
    mentions derive; validator parity is a separate slice.
  - **Verification:** new `tests/test_edges.py` (14 tests: mention derive
    on create/update, the occurrence counter's distinct ids, quote + heading
    recursion, typed_link string/bound verbs with merged metadata, the
    node_link assertion lifecycle, node-typed + datetime fan-out + range
    property edges with the causal stamp, tag assign/unassign/re-add, alias
    reresolution, the trashed-alias terminal, permanent-delete PB1, and
    edge-id determinism across two fresh stores) + the scoped regression
    suites (`tests/test_store.py` 236 passed, `tests/test_store_fixtures.py`
    71 passed / 3 xfailed) + the full gate (`uv run pytest` 1140 passed /
    4 skipped / 3 xfailed, `uv run ruff check`, `uv run mypy src`).

## 2026-11-02

- **feat(seed): the task Status schema seeds `defaultValue` = the Pending
  option id — the GTK side of the monorepo's task-status-default slice (the
  `seeds.ts` `TASK_FAMILY_SEED` parity, no lockstep gate — `defaultValue` is
  an existing `propertySchema.create/update` payload key every port
  applies).** The task class now supplies "Pending" as a DERIVED read (the
  schema-level default, never materialized into property values), so
  removing the class drops an unchanged status automatically and no client
  authors the value on creation:
  - **The manifest** (`core/protocol/features.py`): `TaskFamilySeedEntry`
    gains `default_value: str | None = None` (the seeds.ts `defaultValue`
    parity — the option id for select types); the Status entry carries the
    Pending option id (`TASK_STATUS_OPTION_UUIDS["pending"]`,
    …0004-…000000000009), every other entry stays absent.
  - **The ensure** (`data/store.py` `_ensure_task_family_rows`): the
    `property_schema` INSERT gains the `default_value` column, written
    JSON-encoded (`'"<uuid>"'` — the column stores JSON like `options`, the
    TS `ensureTaskFamilyRows` parity comment).
  - **The effective read** (`data/store.py` `get_effective_properties`):
    read-side option membership (the `effective.ts` 2026-11-02 rule) — a
    select/multi_select default whose value is no longer among the schema's
    stored options (option deleted after the default was written) yields no
    default rather than a dangling option id; write-time validation stays
    type-only so drifted workspaces never fail loud.
  - **No GTK code authored a Pending status write** (grepped the tree:
    only the seed option definition carries the Pending id) — nothing to
    remove, unlike the web cycle's assign arm. The monorepo's live-workspace
    convergence (`migrate-task-status-default.mts`, appended
    `propertySchema.update` + `property.unset` envelopes) flows to this
    client through ordinary catch-up.
  - **Verification:** the scoped gate (`uv run pytest tests/test_features.py
    tests/test_store.py tests/test_store_fixtures.py` + ruff + mypy) and the
    full `uv run pytest` suite green; new tests pin the manifest default,
    the ensure's JSON encoding, the derived "Pending" read (never
    materialized, authored statuses shadow it), and the dangling-option
    read-side drop.

## 2026-11-02

- **docs(agents): gate economics + host memory discipline in the development
  skill.** The two-tier gate is written law (mirroring the notees monorepo's
  2026-10-30 ruling): scoped runs (pytest on the touched files + ruff + mypy)
  are the per-task loop; the full gate (`uv sync && uv run pytest && uv run
  ruff check && uv run mypy src`) is blocking at pre-landing, after a rebase
  onto a moved main, and for wire/applier/sync changes. Same pass records the
  host discipline: one suite at a time (never parallel full gates — two OOM
  the dev host), `pytest -n 2` under pressure, and the load-symptom rule (a
  red that vanishes unloaded is memory-timing, root-cause before retrying).
  Files: `.agents/skills/notees-gtk-development/SKILL.md`,
  `.agents/skills/notees-gtk-development/references/development-workflow.md`.
  Docs-only; no code surface.

## 2026-10-30

- **feat(seed): the workflows slice 2 pair — the everyday `question`/`plan`
  classes ported to the static map; the seed-parity suite pins the manifest
  so drift fails the gate.** The GTK side of the monorepo's workflows slice
  2 seed follow-up (main `b3558430f`): the everyday `question`/`plan`
  classes the research workflow reuses, deploy-catalog plain seeds per the
  definition/idea/place/project/trip precedent (zero wire cost, seed
  convergence only), ported from `packages/domain/src/seeds.ts` +
  `features.ts`:
  - **The static map** (`core/protocol/features.py`) gains the two fixed
    class UUIDs (…0066/…0067, the two slots right after the source-library
    roster's …0065) and their MDI icons + display titles (the
    `SYSTEM_CLASS_ICONS` / `SYSTEM_CLASS_DISPLAY_NAMES` subset —
    `mdiHelpCircleOutline`/"Question", `mdiClipboardTextOutline`/"Plan").
    No extends edge (neither IS anything else), no parentlessRole flag,
    not archived by default — the pair stays unmanaged: no gating, not
    always-on (features.ts parity, exactly like the four plain #14 seeds).
  - **The seed-parity test** (`tests/test_seed_parity.py`) grows the
    `TestWorkflowsEverydayPair` class: the ids pinned (block prefix,
    uniqueness, contiguous …0066/…0067 suffixes, the withdrawn slots
    untouched), the static-map coverage guard, the seed-op replay
    (`class.create` with the display title as content + the icon) through
    the store appliers, and the gating/family semantics mirroring
    `features.ts`.
  - **The feature tests** extend the plain-seed pins: the six plain
    deploy-catalog seeds gate on nothing, extend nothing, and stay out of
    the always-on set.
  - **Fixtures untouched** — the seed change emits server-side seed
    envelopes; the corpus stays byte-identical.
- **feat(fixtures): the `propertySchema.delete` canonical fixture lands in
  the vendored wire corpus (lockstep with the TS reference's meetingDate
  retirement).** The TS monorepo withdraws the `meetingDate` property (a
  meeting IS-A event and carries the inherited `eventDate`; the class-local
  date duplicate rendered twice) and ships
  `scripts/migrate-meeting-date-retirement.mts`, whose cleanup appends
  `propertySchema.delete` — the schema's soft delete (`active = 0`), an op
  this port has applied since the v2 promotion but which never had a
  canonical fixture. The corpus gains `property-schema-delete.json`
  (byte-identical, sha256
  `90fc9502bfaffc59cd99f3888ccfee3f1d18de742e5e9e7bbeac2f3fdc202d3c`),
  added to the wire round-trip list: a bound text schema + authored value,
  the soft delete, a post-delete `property.set` (writes against the retained
  row still validate — the deliberate no-active-filter rule), and a
  delete of an unknown id (a lenient no-op). The whole suite replays it
  clean (pytest green). Sibling seed-manifest check (the retirement's
  lockstep law): this port carries no `meetingDate` constant — the only
  touch is the `CardFamilySeedEntry` docstring's pin-roster sentence,
  amended to the post-retirement roster (source family, meeting family, the
  task heads). The Flutter port receives the same fixture vendor (its
  verification is pending there — no toolchain on the reference host).
  The GTK applier needed no code change — the op already soft-deletes
  exactly like the fixture pins.

## 2026-10-29

- **feat(store): class-level `displayTemplate` — the members' derived-title
  format string (lockstep with the TS reference, web schema v35→v36
  parity).** `class.create`/`class.update` now carry the optional nullable
  `displayTemplate` payload key (strict-schema acceptance with
  `z.string().max(1024).nullish()` parity; pre-change clients reject the
  key, which gates mixed writers). Tri-state, exactly like `color`: absent =
  inherit (no write — a re-issued create's `COALESCE` preserves the stored
  value), a non-empty string = the template, `""` = an explicit opt-out
  (round-trips as `''`, distinct from NULL, and STOPS extends-inheritance for
  the class and its subclasses), present null = clear back to NULL/inherit.
  The applier maps the key to a new `class.display_template` TEXT column
  (schema v23→v24, the `class` table ONLY — unlike `parentless_role` the
  node row is NOT a carrier); display reads go through the new store helpers
  `get_class_display_template` / `resolve_effective_display_template`
  (``packages/store/src/store.ts`` parity — BFS over `class_extends`
  nearest-first, within-level ties by smallest class id, an own or ancestor
  `''` stops the walk with None). The vendored fixture corpus gains
  `class-display-template.json` (byte-identical, sha256
  `6d654cb70463bb27d90c6bf05006e262868fdebad2ec5c06df508195763a7d8d`),
  added to the wire round-trip list, with a new replay class asserting the
  create-with-template, inheritance, `""` opt-out stop, present-null clear,
  the node row carrying no column, and the COALESCE preserve on a re-issued
  create. Verified: the full gate (`uv run pytest && uv run ruff check &&
  uv run mypy src`) green.
- **feat(store): reject `object.delete` on date-chain nodes — the date-chain
  delete lock (lockstep with the TS reference).** A date node is permanent
  journal scaffolding under the permanent-address model — it realizes on
  first write and can never leave the graph. `object.delete` on a date-chain
  node id now raises ``MoveGuardError`` in BOTH arms — soft delete (trash)
  and `permanent: true` (purge) — placed after the "node must exist" check
  and before the class delete routing and any trash/purge write, so the
  trash-bypass arm throws before any purge. The condition is
  prefix-strict over the deterministic date-node id parser — it never
  matches class ids, so the locked-system-class delete arm (the
  year/month/day system CLASSES) is untouched (``packages/store/src/appliers.ts``
  ``applyObjectDelete`` parity). The vendored fixture corpus gains
  `object-delete-date-chain.json` (byte-identical, sha256
  `35b80f12f634abe9bf13971ee024b6140e073fd069c13fcdbeefc157799ab9bd`),
  added to the replay-exclusion set and the wire round-trip list, with a new
  replay class applying the 2024-07-26 chain prefix cleanly and asserting
  both delete envelopes throw and the nodes stay live with no trash row.
  Verified: the full gate (`uv run pytest && uv run ruff check && uv run
  mypy src`) green.
- **ci: pin release.yml runners to ubuntu-22.04, add workflow-level
  concurrency, and give ci.yml a git-ref dispatch input.** The `guard`,
  `python-dist`, `archpkg`, and `release` jobs all ran on
  `ubuntu-latest` while ci.yml pinned `ubuntu-22.04` — every runner is now
  pinned to the same fleet image (the `archpkg` job's `archlinux` container
  is unaffected; its runner is pinned for consistency). `release.yml` gains
  `concurrency: {group: release-${{ github.ref }}, cancel-in-progress:
  false}` at workflow level — a tag push queues behind any in-flight run
  instead of being cancelled mid-publish. ci.yml's `workflow_dispatch` gains
  a `git-ref` input (default `main`) and its checkout honors
  `ref: ${{ inputs.git-ref || github.ref }}` (schedules and PRs still
  resolve to `github.ref`). YAML validated with `yaml.safe_load`; the pytest
  + ruff + mypy gate is green.
- **ci(release): git-ref dispatch input + fail-loud dispatch guard on
  release.yml.** Manual dispatches can now build the `python-dist` and
  `archpkg` artifacts from an explicit `git-ref` input (default `main`) —
  both checkouts take `ref: ${{ inputs.git-ref || github.ref }}`. A new
  `guard` job (a `needs` of both artifact jobs) makes the dispatch semantics
  explicit: a branch/SHA dispatch is legitimate and prints an
  artifacts-only `::warning` (GitHub Releases still come only from pushing
  a `v*` tag — the `release` job stays tag-gated), while a `git-ref` that
  looks like a release tag (`v*` or `refs/tags/v*`) fails the run loudly —
  a dispatch has `ref_type=branch` and can never publish what the operator
  expected; the tag itself must be pushed instead. Header comment
  documents the contract. YAML validated with `yaml.safe_load`; the
  pytest + ruff + mypy gate is green.
- **feat(store): reject `isEmbedded` writes on date-chain nodes — the
  date-chain embedding lock (lockstep with the TS reference).** A date
  node's journal role is platform-fixed (document chrome); an
  `object.update` carrying `isEmbedded` — either direction, demote or
  promote — now raises ``MoveGuardError`` at the top of the applier's
  embedding branch, before the parent-page checks. The parent lock lived in
  `object.move`, the class lock in the `object.create` / class guards —
  this closes the last carrier (``packages/store/src/appliers.ts``
  ``applyObjectUpdate`` parity). The vendored fixture corpus gains
  `is-embedded-date-chain.json` (byte-identical, sha256
  `3192678b3d5d328e2528c1d50ce1960b70e0e024a1c75aad31b639412e9f384b`),
  added to the replay-exclusion sets and the wire round-trip list, with a
  new replay class applying the 2024-07-26 chain prefix cleanly and
  asserting both violating envelopes throw and roll back. Verified: the
  full gate (`uv run pytest && uv run ruff check && uv run mypy src`) green.

- **ci: restructure the GitHub Actions workflows on the fleet RosellRamos
  pattern — PR/nightly/dispatch CI, tag/dispatch-only release pipeline,
  SHA-pinned actions; no AUR publication.**
  Mechanism: `ci.yml` drops the `push: [main]` trigger (the recurring
  push-time failures were the pain point; the nightly cron at `23 3 * * *`
  covers `main`'s head) and gains `pull_request:`, `workflow_dispatch:`, a
  per-ref `concurrency` group with `cancel-in-progress`, pinned
  `ubuntu-22.04` runners, and SHA pins for every action (checkout v7.0.0,
  setup-uv v10.3.3 — the same pin set as the Flutter client).
  `release.yml` now triggers on `v*` tag pushes and `workflow_dispatch`
  only — `python-dist` and `archpkg` no longer build on every main push; the
  `release` job stays tag-gated, so a dispatch produces artifacts only.
  `upload-artifact` (v7.0.1), `download-artifact` (v7.0.0), and
  `softprops/action-gh-release` (v3.0.3) are SHA-pinned. Docs agree with the
  new reality: the operations skill and its `references/releases.md` now
  record **no AUR publication** (the PKGBUILD is local/CI-only — nothing is
  pushed to the AUR; the prebuilt install is the `archpkg` CI artifact), and
  the README install instructions point at tag/dispatch runs instead of main
  pushes.

## 2026-10-28

- **fix(tests): the realtime CI flakes — wait for the stub's close-code
  record instead of asserting right after stop.** Mechanism:
  `test_sync_engine.py::TestRealtime::test_start_realtime_applies_remote_ops_frame_end_to_end`,
  `test_ws_client.py::TestLifecycle::test_hello_dispatches_and_updates_state`,
  and `test_ws_client.py::TestLifecycle::test_stop_closes_cleanly_and_never_reconnects`
  asserted the stub relay's recorded close code immediately after
  `stop_realtime()` / `client.stop()`; the stub records the code on its own
  handler thread, so on a slow scheduler the assertion ran before the record
  landed (`assert [] == [1000]`). All three assertions now poll through the
  suite's existing `wait_until` helper (deadline-based, 5 s default) —
  `test_stop_closes_cleanly_and_never_reconnects` was the same latent race,
  fixed the same way before it could flake. No production code touched.
  Verified: each fixed test looped 25× green (was 4/20 and 1/20 failing
  before the fix), the containing files and the full suite green.
- **feat(ui,core): the memorization-cards hub + review session — the GTK
  cards surface at web parity.** Follow-up of the `card.review` lockstep
  slice; no wire, applier, schema, or fixture change anywhere (the fixture
  corpus stays byte-pinned and untouched). What shipped: (1)
  ``core/srs.py`` — the pure SM-2 / FSRS-5 scheduler ported
  function-for-function from the monorepo's ``packages/domain/src/srs.ts``
  (``next_sm2_state`` / ``next_fsrs_state`` / ``next_card_state`` /
  ``is_card_due`` / ``retrievability`` / UTC day math), the mid-deck
  algorithm-switch staleness contract preserved; the FSRS recurrence-arm
  test vectors were produced BY RUNNING THE TYPESCRIPT REFERENCE ITSELF and
  are asserted with exact ``==`` — bit-for-bit parity, no ULP drift. (2)
  ``core/cards.py`` — the pure reads the chrome renders:
  ``partition_cards_into_buckets`` + ``build_review_queue``
  (``CardBuckets.tsx`` / ``reviewQueue.ts`` ports — due + fresh only,
  future-due never enters the queue), the ``scheduledIsoOf`` due-day
  grammar, the journal-derived ``card_srs_state`` (latest ``card.review``
  ``stateAfter``), and the extends-aware ``card_members``. (3) The
  **Cards hub**: a pinned "Memorization cards" sidebar row above the node
  list (owner ruling 1), the bucket strip ("Review N due · M new" + fresh /
  later counts), the plain member list with due-day labels, and the
  sm2/fsrs **algorithm selector** over the new per-user prefs seam (owner
  ruling 2 — the selector AND its ``PUT /api/me/prefs`` write path ship in
  this slice); a node selection switches back to view/edit (the hub is a
  sibling surface, not a node). (4) The **prefs read/write seam**:
  ``NoteesClient.get_prefs()`` / ``patch_prefs()`` with the prefs.ts
  ``normalizeUserPrefs`` port (fail loud on a non-object body, anything but
  ``"sm2"`` → ``"fsrs"``), a device-local cache scalar beside the actor id,
  and a once-per-login worker-thread fetch; offline or unreachable answers
  **cached, else ``"fsrs"``** — the honest offline fallback. (5) The
  **review session**: a modal ``Adw.Dialog`` (owner ruling 5) over the hub —
  the front is the card's own content read-only through ``ast_to_view`` +
  ``PageViewWidget``, the reveal adds the embedded children (the condensed
  back), a front-only card shows "No answer on this card" and still grades
  (Review Focus 1), the four-grade bar (Again destructive) answers to keys
  **1–4 active only while the back is up** (the web's editor-interaction
  rule) and Enter/Space flips the reveal from the front (a GTK convenience),
  the header shows i/N, and the end summary counts reviewed + lapses (a
  lapse = ``again`` on a card with reps > 0, the web semantics). Each grade
  computes ``state_after`` on the pure scheduler, then enqueues the Task-4
  envelope set — the due-day **date-chain ensure** (create at the
  deterministic chain parent / restore when trashed — every normal read
  filters active rows, so a trashed day page must come back before the
  cardDue write anchors to it), the **property batch at the row's existing
  idx** (cardDue point + reps/interval numbers + the ACTIVE algorithm's
  fields only — the staleness contract; the first GTK ``property.set`` UI
  writes), and the ``card.review`` history op last — with the optimistic
  mirror apply and **one ``engine.sync`` per grade** (owner ruling 3); GTK
  ships without the web's per-grade undo batch (owner ruling 7 — a mis-grade
  needs a re-grade). The session's position/counters live in the pure
  ``ReviewSessionState`` (headless-tested); a card that vanishes mid-session
  is skipped, never graded. **Verification:** ``uv run pytest && uv run
  ruff check && uv run mypy src`` — all green (1085 → 1092 tests through
  the slice; the final gate runs 1092 passed, 4 skipped — the live-server
  e2e self-skip — 3 xfailed).

- **feat(protocol,store): the memorization-cards slice — `card` class rename
  seeds, the card-family property manifest, and the `card.review` op.**
  Lockstep with the monorepo's `memorization-cards` branch: (1) the `card`
  system class (uuid …0011 unchanged) is renamed — display name
  "Memorization card", icon ``mdiCardsPlayingOutline`` — carried in the GTK
  seed-knowledge maps (``SYSTEM_CLASS_DISPLAY_NAMES`` / ``SYSTEM_CLASS_ICONS``);
  (2) the six SRS schedule schemas join the seeds at the fixed …0004-
  property block — ``CARD_FAMILY_SEED`` / ``card_property_uuid()``
  (seeds.ts ``CARD_FAMILY_SEED`` / ``CARD_PROPERTY_UUIDS`` parity):
  cardDue (datetime) + cardReps/cardInterval/cardEase/cardDifficulty/
  cardStability (number), class-bound to `card`, all UNPINNED (the
  2026-10-27 pinned roster is frozen; seed knowledge only — the GTK client
  authors no seed envelopes); (3) the new wire op ``card.review``
  (op-types.ts ``cardReviewPayload`` strict parity — cardId/grade
  [again|hard|good|easy]/algorithm [sm2|fsrs]/stateBefore/stateAfter strict
  ``CardSrsState`` records/reviewedAt/sessionId) lands HISTORY-ONLY in the
  derived ``card_review`` table: one append-only row per graded review, NO
  LWW gate, never writes property values, membership-only validation (the
  node must exist and carry the card system class — ``NotFoundError`` /
  ``MoveGuardError``, fail loud, whole apply rolled back). Read seam:
  ``LocalStore.list_card_reviews`` (HLC order; the latest row's stateAfter
  is the card's derived SRS state). Schema: GTK ``SCHEMA_VERSION`` 22 → 23
  (web schema v34→v35 parity) — the canonical aux DDL gains the table + the
  (card_id, hlc) index; the migration step is a no-op for fresh databases.
  Fixture: ``card-review.json`` re-vendored BYTE-IDENTICAL from the branch
  (sha256 verified) and replayed green. Tests: ``TestCardReviewFixture``
  (full replay → two history rows + the fixture's end state, verbatim-wire-JSON
  state snapshots, both-delivery-orders convergence, fail-loud arms,
  re-delivery dedupe), ``TestCardReviewPayloads`` (registry strictness, enum
  and shape rejections), ``TestCardFamilySeed`` (the rename decorations, the
  six fixed ids, the datetime/number split); ``card_review`` joins the
  both-orders convergence dump. **Verification:** `uv run pytest &&
  uv run ruff check && uv run mypy src` — all green.

- **feat(seed): the `project` property seed — the task→project membership
  relation joins the task-family manifest as the seventh authored schema;
  the store ensure authors it at the full spec shape.** The GTK side of the
  monorepo's `project` seed (plain seed per the weblink-extends precedent —
  zero wire cost, seed convergence only), ported from
  `packages/domain/src/seeds.ts` (`SYSTEM_PROPERTY_UUIDS.project` …0012 +
  the `SYSTEM_PROPERTY_SPECS` entry):
  - **The constant** (`core/protocol/features.py` ``_TASK_PROPERTY_UUIDS``)
    gains ``project`` = …0003-000000000012 — the next genuinely-free slot in
    the workflow-properties block (…001–…011 used), resolved through
    ``task_property_uuid`` like the family.
  - **The manifest** (``TASK_FAMILY_SEED``) grows the seventh entry:
    object-typed, MULTI, ``target_class_filter ("project",)`` (PG6
    extends-aware validation), ``hide_when_empty`` — the schema-level
    hide-when-empty contract, the first seed to carry it — at GLOBAL scope
    (the seed-scope audit: any node may participate in a project; only the
    class-local binding row on `task` is task-specific, the url→source
    precedent). ``TaskFamilySeedEntry`` gains the ``multi`` /
    ``target_class_filter`` / ``hide_when_empty`` fields (default-off, the
    six family rows untouched). The binding seeds UNPINNED — the pin roster
    declares no set carrying it, and the `class_property` table stores no
    pin flag (a plain presence row like the family's).
  - **The store ensure** (`data/store.py` ``_ensure_task_family_rows``) now
    authors the schema row with ``multi`` / ``target_class_filter`` /
    ``hide_when_empty`` per entry (the ``project`` row: 1 /
    ``["project"]`` / 1) — INSERT OR IGNORE everywhere, so a server-seeded
    workspace converges untouched (first writer wins).
  - **The tests** pin the shape so the drift cannot recur:
    ``test_features.py`` pins the seven-name roster, the …0012 id, and the
    full spec shape (multi/filter/hide-when-empty on `project`; the six
    family rows default-off); ``test_store.py`` pins the authored row
    (object/Multi/NULL scope/``["project"]``/hide-when-empty + the plain
    binding) and the 7-schema/7-binding counts; the racing-toggle fixture
    replay (``test_store_fixtures.py``) expects the object-typed seventh
    schema and the seven bindings.
  - **Fixtures untouched** — the seed change emits server-side seed
    envelopes; the corpus stays byte-identical. **Verification:** `uv run
    pytest && uv run ruff check && uv run mypy src` — 1001 passed (+2), 4
    skipped, 3 xfailed (pre-existing lockstep-pending xfails); ruff/mypy
    clean.
- **feat(protocol)!: accept the retired `url` property type (url →
  text).** Lockstep with the monorepo's `url-type-retirement` branch: the
  dedicated `url` property type leaves the wire enum — the strict
  `propertySchema.create`/`update` payload schemas reject it outright (the
  `date`/`date_range` precedent; no backward compat) — while the seeded
  `url` property (…0024, source family) survives retyped to `text` (legacy
  url values are plain strings, legal text values; live logs converge by
  the monorepo's `migrate-url-type-to-text.mts`). The applier's url arms
  are gone: scalar typing (``email``/``select`` string check), the
  default-value type check, and validationRules applicability/pattern
  enforcement now read ``text``/``email`` (the error message says
  "text/email schemas only"). The seed manifest needed no change — the GTK
  client authors no url seed (seeding is server-side; the task-family
  manifest is the only local one). Fixtures re-vendored verbatim from the
  branch: ``property-assign.json`` and ``property-validation-rules.json``
  (their schema-create envelopes carry ``type: "text"`` now — the old bytes
  would fail the strict gate mid-replay) + ``property-schema-update-fields.json``
  (comment-only retirement wording). Tests: the PG6 suite's "link" schema
  rides text (url-shaped string legal, non-string shape fails loud), a new
  pin asserts the retired type's create is rejected at the envelope gate
  with the row never landing (property-validation.test.ts PG6 parity), the
  retired-types parametrize gains ``url`` (test_payloads.py), the pattern
  applicability match string follows the new message. **Verification:**
  `uv run pytest && uv run ruff check && uv run mypy src` — 999 passed,
  4 skipped, 3 xfailed (pre-existing lockstep-pending xfails); ruff/mypy
  clean. Remaining corpus drift vs the branch (``class-property-active``,
  ``class-unassign``, ``is-embedded-parent-page``, ``link-embedded``,
  ``object-delete-class-routing``, ``typed-link-mark``) belongs to other
  in-flight monorepo slices — the byte-pin re-vendor completes when those
  land.

## 2026-10-27

- **chore(protocol,test): re-vendored 4 protocol fixtures from the main
  monorepo (tags-api @ dc96188) + the GTK half of their expectation
  coverage.** The corpus grows 40 → 44 files, byte-identical (sha256
  verified): ``tag-membership.json`` (NEW — the tags OR-Set gate: tag pages,
  the re-issued ``object.create`` assign carrier, the ``tag.unassign``
  tombstone, the newer-HLC add-wins resurrection), ``class-property-pinned.json``,
  ``mention-custom-label.json``, ``property-assign.json`` (three lagging).
  Bookkeeping: all four join the round-trip list in ``tests/test_fixtures.py``;
  ``class-property-pinned.json`` and ``property-assign.json`` join
  ``REPLAY_EXCLUDED`` in ``tests/test_store_fixtures.py`` (GTK-main
  lockstep-pending additions the TS list does not carry — removed when the
  wire lands). New per-fixture suites in ``tests/test_store_fixtures.py``:
  ``TestTagMembershipFixture`` (green — full replay with intermediate
  tag_ids states, mirrors the monorepo store test), ``TestMentionCustomLabelFixture``
  (green replay — the label write then the label clear land verbatim),
  ``TestClassPropertyPinnedFixture`` and ``TestPropertyAssignFixture``
  (fixture-shape pins green; the derived-state replay methods xfail strict —
  the wire is lockstep-pending on main: the strict ``class.property.set``
  schema rejects the ``pinned`` key outright, and ``property.assign`` /
  ``property.unassign`` / ``property.empty`` are not in the GTK op registry
  (skipped unrecorded, so the fixture's assigned-and-valueless end state —
  the emptied value, the surviving declaration — does not derive). The
  mention customLabel reshape (grammar acceptance, retired ``text``/
  ``displayText`` rejection, the flatten resolving the target name) is
  likewise unported; its grammar test xfails strict. The xfails flip hard
  (strict) when the lockstep applier work lands — the pinned half already
  rides the ``lockstep/class-property-pinned`` branch. **Verification:**
  `uv sync && uv run pytest && uv run ruff check && uv run mypy src` — all
  green.

- **feat(ui): first-class tag management in the page view — the Tags row
  (pills + add picker + per-pill remove) at web parity.** Tags are
  first-class and node-scoped: any page can be assigned as a tag to pages
  AND blocks (the applier side — the tag OR-Set, the re-issued
  ``object.create`` carrier, ``tag.unassign`` — already shipped; this slice
  is the GTK chrome on top, no applier or wire change, the fixture corpus
  untouched). The web reference (``apps/web/src/features/metadata/
  TagsRow.tsx``, ``client-writes.ts assignTag/unassignTag``,
  ``client-reads-refs.ts effectiveNodeColor``, ``shared.ts contrastFor``)
  was mirrored: (1) a Tags row under the page title on every opened node's
  view — the GTK view renders one node's own content (no child-block rows),
  so the web's two surfaces (the page-header row and the dedicated block
  row) collapse into this single row; pills render alphabetically by tag
  title with the tag's EFFECTIVE color as the surface (own color, else the
  first assigned class's extends-chain color, resolved through the web
  ``PRESET_HEX`` table for the ten preset tokens) and a luminance-threshold
  contrast foreground (Pango spans, the inline class-chip mechanism);
  (2) assign: an Add pill pops the page picker (pages only — the pure
  ``rendersWithDocumentChrome`` port matching the store's ``_is_page_row``
  rule, parentlessRole-"block" demote and extends-aware — minus the node
  itself and its already-assigned tags) and writes the OR-Set add carrier, a
  re-issued ``object.create`` over the node's own id carrying the single tag
  (``parentId``/``contentAst`` ride for replicas that missed the original
  create); (3) remove: the per-pill × writes the ``tag.unassign``
  tombstone. The write path mirrors the alias chrome's exactly:
  ``store.enqueue`` → optimistic ``apply_remote`` (op-id dedupe makes the
  server echo harmless) → outbox push on a worker thread; applier guard
  failures toast. No undo machinery exists in the GTK UI (the undo journal
  is a web-client concern), so there is no undo to mirror. Architecture: the
  pure logic lives in the new headless ``ui/tag_ops.py`` (envelopes, pill
  ordering, color/contrast resolution, the page filter — importable without
  the ``ui`` extra, the ``alias_ops.py`` precedent); the GTK row widget is
  the new ``ui/tags.py``; the search-over-rows picker is extracted from the
  alias chrome into the shared ``ui/node_picker.py`` (``aliases.py``
  refactored onto it — one picker implementation); the page view renders
  the window-built row between the title and the alias chrome; the store
  gains one derived read, ``class_extends_edges()`` (the tag resolvers'
  chain input, ``client-reads-refs.ts`` parity — no applier change).
  **Verification:** `uv sync && uv run pytest && uv run ruff check &&
  uv run mypy src` — all green (988 passed, 4 skipped: the live e2e module
  and the GTK-import smoke, both self-skipping on this headless box; 27 new
  tests: ``tests/test_tag_ops.py`` + ``TestClassExtendsEdges`` in
  ``tests/test_store.py``).

## 2026-10-26

- **fix(store): `object.move` keeps a parentless main node a page — the
  first parenting acquires the standalone claim (treat-as-main; the GTK port of the monorepo's fix, reference commit
  867c25134, completing the three-implementation lockstep).** The
  monorepo's TS reference shipped the treat-as-main semantics on
  2026-10-26: `object.move` never wrote `is_embedded`, so a node left
  parentless with the bit still 1 (an extracted-to-page block, or a block
  un-parented via "Remove parent") rendered as a page while parentless but
  silently demoted to an embedded block the moment a parent was assigned.
  GTK port: in `_apply_object_move`, after the existing placement guards,
  the first parenting of such a node now (1) computes `claims_main` —
  parentless → parented, non-class, `is_embedded = 1`, and NOT a member of
  an effectively parentlessRole "block" class (extends-aware, the same
  check `_is_page_row`'s parentless arm uses); (2) fails loud with
  MoveGuardError when the new parent does not render with document chrome
  (a class or an embedded block) — a page cannot be parented under either;
  (3) on the winning move, sets `is_embedded = 0` AND stringifies the
  content in the same UPDATE — the same lossy flatten the object.update
  promotion (1→0) performs (content_plain projection included); the
  acquisition rides the row-level LWW gate, so a stale move still drops
  whole. Parented→parented moves still never touch the flag; unparenting
  is unchanged. Applier comments updated (the "moves never write the flag"
  wording now carries the treat-as-main exception, mirroring the TS
  comment). No wire change — the fixture corpus is untouched. Tests: four
  new applier unit tests mirroring the monorepo's (`tests/test_store.py`
  `TestObjectMove`): the orphan's first parenting acquires the claim; the
  acquisition stringifies rich content (text + mention) to a single
  text-only token; parenting under an embedded block or a class raises
  MoveGuardError and leaves the flag untouched; a parentless
  parentlessRole-"block" class member keeps `is_embedded = 1`.
  **Verification:** `uv sync && uv run pytest && uv run ruff check &&
  uv run mypy src` — all green.

## 2026-10-24

- **feat(protocol,store): class-level `parentlessRole` — the parentless
  branch of the render cascade bends to class config (main-repo lockstep
  port).** The monorepo shipped the class-level
  parentlessRole render contract (SCHEMA.md "Node structure"; TS store
  schema v31→v32; `packages/domain` `SYSTEM_CLASS_PARENTLESS_ROLE_BLOCK`):
  `class.create`/`class.update` gain the OPTIONAL NULLABLE `parentlessRole`
 `"page" | "block"`; absent/null = "page", the zero-migration default.
  Presence writes, present-null clears, exactly like `color`. GTK port:
  (1) the strict `ClassCreatePayload`/`ClassUpdatePayload` accept the key
  (zod `z.enum(["page", "block"]).nullish()` parity — the lockstep payload
  acceptance; pre-change clients reject it, gating mixed writers), and the
  builders follow the web `WorkspaceClient` (`createClass` opts,
  `updateClass` keep-vs-clear with explicit-null clear); (2) the derived
  schema gains `parentless_role` TEXT on BOTH the node table and the class
  registry (meaningful on class rows only — the icon/color two-table
  precedent), fresh databases via the canonical DDL, existing databases via
  the guarded `SCHEMA_VERSION` 21→22 migration (web schema v31→v32
  parity); the class.create applier maps presence/present-null to both
  tables (create-time value rides the node-row INSERT; the registry
  conflict arm COALESCEs, so a re-issued flag-less class.create — the
  seed/ensure re-run — preserves the stored flag); `class.update` keys off
  payload presence like `color`; (3) the parent-page guard is flag-aware:
  `_is_page_row`'s parentless branch now demotes a parentless node whose
  classes EFFECTIVELY carry "block" (own flag OR any extends-ancestor's via
  the class_hierarchy closure — one-way demote, no per-level unflag), so a
  standalone (is_embedded = 0) claim under a block-role parentless node
  fails loud (MoveGuardError — same guard, corrected predicate, no new
  error type); the embedding flag stays read only for parented rows (the
  symmetry); (4) `NodeRow` exposes `parentless_role` (TS `StoreNodeRow`
  parity) and the snapshot verbatim columns ride it when present; (5) the
  seed-manifest roster `SYSTEM_CLASS_PARENTLESS_ROLE_BLOCK` lands in
  features.py (seeds.ts parity — the 14 flagged names; meeting/birthday
  deliberately absent, inheriting the demote through event); the GTK
  client authors no seed envelopes, so no emission path changes. The
  `class-parentless-role.json` fixture re-vendored byte-identical (sha256
  `11779e2ce5a896b34942201cb3234655f46e6068a557c49f0d33d27685cab494`).
  Tests: fixture replay (flag set + present-null clear on both tables,
  Errand-extends-Task effective inheritance derives at read); applier unit
  tests (create-with-flag maps to both tables, absent = NULL, update
  set/clear, re-issued flag-less create preserves, the flag-aware guard
  rejects a standalone claim under a block-role parentless node and a
  promotion of its embedded child, explicit "page" never demotes);
  payload-shape tests (enum/null/garbage/object-op rejection, builder
  emission rules).
  **Verification:** `uv sync && uv run pytest && uv run ruff check &&
  uv run mypy src` — all green (see the session report for counts).

## 2026-10-23

- **feat(protocol,store): the workspace.feature.set settings payload — the
  optional per-feature configuration record (main-repo source-library
  lockstep port).** The monorepo extended the
  ``workspace.feature.set`` wire payload with an optional ``settings`` JSON
  object (``settings: z.record(z.unknown()).optional()`` — record-shaped,
  feature-scoped vocabulary; the protocol pins only "a JSON object", the
  Sources library's citekey config et al. ride it). GTK port: (1) the strict
  ``WorkspaceFeatureSetPayload`` gains the optional record — explicit null
  rejected (zod ``optional()`` parity, the elementId precedent), non-records
  fail loud; (2) the LWW row writer persists the winning payload's record as
  JSON text (compact separators, the ``JSON.stringify`` byte parity) — the
  settings ride the SAME whole-row (workspace, feature) LWW comparator, and a
  winning old-shape payload (no settings — including the ``object.delete``
  arm-2 routed family disable) stores NULL, distinct from the empty record;
  (3) the derived schema gains the ``workspace_feature.settings`` column —
  fresh databases via the canonical aux DDL, existing databases via the
  guarded ``SCHEMA_VERSION`` 20→21 migration (web schema v29→v30 parity);
  (4) ``feature_rows`` surfaces the parsed record (``None`` = the winning
  payload carried no settings). No GTK code emits settings yet —
  convergence-only alignment; the builder is unchanged. The
  ``workspace-feature-set.json`` fixture re-vendored verbatim (sha256
  f8989a15…a94431 — the three-envelope settings tail). Tests: the
  fixture-replay test asserts both delivery orders converge on the final
  sources winner's record (and the intermediate old-shape re-enable cleared
  it to NULL); a unit test ports the main repo's settings-semantics test
  (persist, old-shape-wins → NULL, ``{}`` round-trips as a real value, stale
  settings op drops whole); payload-shape tests pin the record/null rules.
  **Verification:** `uv run pytest` 925 passed / 4 skipped; `uv run ruff
  check` clean; `uv run mypy src` clean.

- **feat(protocol,store): the source-library seed roster — renames, 30 new
  source type-classes, the authorship model, and the archival-is-not-absence
  hierarchy rebuild (main-repo source-library lockstep port).** The monorepo's source-library slice reshaped the system-seed
  manifest (packages/domain/src/seeds.ts + source-class-bindings.ts); this is
  the GTK static-seed-knowledge port plus the one applier correction the new
  seed state forces. (1) Renames, keys only — the fixed UUIDs stay:
  ``article``/``weblink``/``movie`` become ``journal_article``/``web_page``/
  ``film`` (…0026/…0034/…0035); ``paper`` (…0025) is WITHDRAWN (Zotero has no
  "paper" — nodes migrate to journal_article), the id a dead slot never
  reused. (2) 30 NEW source type-classes (…0048–…0065 hex, alphabetical by
  key), ALL extends source — the sources family the toggle archives/revives
  grows from 11 to 40 (base + 39 children), and their seeds.ts mdi icons +
  display titles join the static decorations map. (3)
  ``SYSTEM_ARCHIVED_BY_DEFAULT_CLASSES`` parity: the 18-name archived-by-
  default long tail (seed knowledge — GTK seeds nothing; the server authors
  the archival flips from it). (4) The authorship model: ``authors`` RETYPED
  object→text (multi, still …0012, local to source, no targetClassFilter);
  ``linkedAuthors`` REINSTATED as the node-typed experiment at its NEW pool
  UUID …0006-…0001 (object, multi, targetClassFilter [agent] — the 2026-09-27
  …0025 withdrawal stays burned); ``editors`` new at …0006-…0002 (text,
  multi). (5) The applier correction (appliers.ts ``rebuildClassHierarchy``
  parity): the class_hierarchy closure rebuild was
  restricted to ``active = 1`` class rows, so ANY archived class (feature
  toggle-off or the new archived-by-default seeds) silently lost its entire
  extends closure on the next setExtends — archival is VISIBILITY state, not
  an absence (an archived-by-default type still IS-A source for queries,
  scope eligibility, and the effective-properties reach), so the rebuild now
  walks EVERY class row and the default reads keep filtering the bit. Not
  ported (structural): the 85-property bibliographic pool and
  SYSTEM_SOURCE_CLASS_BINDINGS — the GTK client has no seed manifest and no
  binding rows (seeding is server-side; binding/config rows arrive as ops the
  generic appliers already apply); the class-side knowledge above is what the
  GTK feature map and lock authority resolve through. No wire change, so no
  fixture re-vendor (the corpus diff against the monorepo is empty for this
  slice). Tests: test_seed_parity grows the source-library roster pins (fixed
  ids contiguous …0048–…0065, withdrawn-slot disjointness, static-map
  icons/titles drift guard, the archived-by-default set) and replays the
  server seed op stream end to end (39 creates + extends edges + the 18
  archival flips asserting the derived active bits, then a later unrelated
  setExtends rebuild asserting the archived classes KEEP their closure — the
  regression the applier fix closes); the authorship trio replays through
  propertySchema.create with the retyped authors / reinstated linkedAuthors /
  new editors rows asserted; test_features pins the 40-strong sources family
  and the 18-name archived set. **Verification:** `uv run pytest` 919 passed
  / 4 skipped; `uv run ruff check` clean; `uv run mypy src` clean.

- **feat(protocol,store): the external_ref.* op family — the importer/
  sync-plugin external-id mapping substrate (main-repo lockstep; GTK lockstep port).** Two new wire ops ported
  from the monorepo's external-id mappings slice (packages/protocol/src/
  op-types.ts, packages/store/src/appliers.ts + schema.ts parity):
  `external_ref.set {ref, nodeId}` and `external_ref.delete {ref}` — the
  durable per-workspace `externalId -> nodeId` map (a Google Calendar event
  UID, a Readwise highlight id) so importer/sync re-runs update the mapped
  node instead of duplicating it. (1) The op registry gains both ops (24
  known op types — the op-types.ts `OP_PAYLOAD_SCHEMAS` parity); the strict
  payload schemas cap the opaque caller-namespaced `ref` key at 1..512
  chars (`EXTERNAL_REF_MAX_LENGTH` parity) and require a uuid `nodeId`
  with no referential integrity (the `property_value` precedent) — extra
  keys fail loud like any retired key; the builders
  `build_external_ref_set`/`build_external_ref_delete` ride along. (2) The
  store gains the derived tables at SCHEMA_VERSION 20 (web schema v30
  parity, purely additive — the v8 migration precedent): `external_ref`
  (PK (workspace_id, ref), envelope causality columns, index on node_id) +
  `external_ref_tombstone` (the `property_value_tombstone` precedent). The
  appliers converge on the TS reference exactly: one LIVE mapping per ref,
  LWW by the full envelope (hlc, actor) against the single standing
  live-or-tombstone record — a re-set MOVES the mapping, a stale write
  drops untouched, a winning set supersedes the tombstone, a winning
  delete (UNMAPPED refs included) lands the tombstone that kills stale
  concurrent sets under any delivery order. A node's hard delete
  (`object.delete {permanent: true}` and `trash.empty`) purges its live
  rows (the subtreeIds pattern); soft-trash keeps them; the ref tombstones
  survive the purge (keyed by ref). Reads: `resolve_external_ref`
  (absent/tombstoned -> None) and `external_refs_of` (sorted — the
  `externalRefsOf` reverse scan). (3) The canonical fixture
  `external-ref.json` re-vendors byte-identical (sha256
  9cf46b1b630be8db109cf999f451242ca880398eb43c2decc79068994145a47e, the
  convergence-gate corpus). Tests: the fixture replay suite replays the
  12-envelope scenario (racing sets, the move, both deletes, the permanent
  purge) asserting the same final derived state under forward and reversed
  mapping-op delivery plus idempotent replay, with both new tables in the
  byte-deterministic convergence dump; new store suites cover the LWW
  move/stale-drop/actor-tiebreak, the tombstone-kills-stale-set and
  delete-of-unmapped-ref semantics, the hard-delete purge vs soft-trash
  retention, the tombstones-survive-purge invariant, the unsynced-node
  (no-referential-integrity) read, and apply-time payload rejections; new
  payload suites pin the ref bounds, the uuid shape, and the strict
  extra-key rejection; the envelope round-trip and op-registry parity
  suites cover the new ops. **Verification:** `uv run pytest` 912 passed /
  4 skipped; `ruff check` + `mypy src` clean; the vendored
  `external-ref.json` is sha256-identical to the monorepo's. Note: the
  full-corpus `diff -r` against the monorepo also shows pre-existing drift
  from OTHER in-flight monorepo slices (a longer
  `object-delete-class-routing.json` + the new `property-assign.json`) —
  that re-vendor belongs to those slices' GTK ports, not this one. Flutter
  convergence is the remaining leg of the lockstep law.

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
