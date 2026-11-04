# Plan — the deferred graph slice (edge backfill + graph model + color groups + GTK chrome)

The minimal graph view, built in four gate-green steps. Reference: the web's
`apps/web/src/features/collection/graph/` + `packages/store/src/graph.ts` +
`colorGroups.ts`; the TS edge derivation is `packages/store/src/edges.ts`.

## 1. The edge index — the missing piece is the backfill, not the index

The derived `edge` + `node_link` index already shipped (schema v25,
2026-11-03 — the `edges.ts` deep port: mention/typed_link/property/tag
families, write triggers, wipe path). What it never got is the v27-style
**backfill**: pre-v25 databases created the tables additively and derive
rows only when a node is rewritten. So:

- `SCHEMA_VERSION` 27 → 28; `_migrate_v28` + a `_backfill_edge_index` pass
  that reuses the applier-time `_rebuild_edges` per stored node (stamp: the
  node row's `updated_at` — deterministic; edge ids are content-addressed).
- Purely derived — no wire change, the fixture corpus stays at 51.

Deviation note: the parent's brief named a `class_link` edge vocabulary; the
TS contract deliberately keeps class assignment / class_link chips OUT of the
index (edges.ts) — the v25 port follows it.

## 2. The graph model (headless — `core/graph/`)

- `model.py` — the `graph.ts` topology port, reduced per the brief: node set
  = classes + standalone pages (aliases never vertices; parentlessRole
  "block" members excluded), five structural edge families (mention /
  property / tag from the edge index via `resolved_target_id`, parent
  main-node parentage, class OR-Set membership), endpoint rollup through
  blocks + aliases, weighted merge with earliest-stamp, per-node degree.
  NOT ported (documented): semantic/temporal co-occurrence, mass/content
  metrics, anchor/depth scope.
- `layout.py` — a deterministic Fruchterman–Reingold pass: golden-angle
  spiral init (sorted ids), fixed iterations, linear cooling, zero RNG.
- Store seam: `LocalStore.graph_topology(workspace_id)`.

## 3. Color groups (headless)

- `core/query/ast_v1_map.py` — the pure `map-query-ast-v1-v2.ts` port.
- `core/graph/color_groups.py` — the `colorGroups.ts` port: group model
  ({id, label, color, ast, error}), at-load migration (legacy DSL string →
  the slice-2 DSL parser with mirror-backed resolvers; v1 AST → the pure
  mapping; failures keep the group, marked erroneous), first-match-wins
  evaluation over the slice-1 query runner (list order IS the priority).
- Persistence: the device-local idiom (`ui/config_store.py`, config.json),
  keyed per workspace.
- Store seam: `LocalStore.parse_query_language(workspace_id, text)` (a
  synchronized parse-only DSL seam — locking correctness for UI callers).

## 4. The GTK chrome (`ui/graph_view.py`)

- A content-stack sibling of view/edit/cards (a pinned "Graph" sidebar row,
  the cards-hub pattern): `Gtk.DrawingArea`, nodes = labeled circles, edges
  = lines colored by family, drag to pan, scroll to zoom, click a node →
  `_open_node`. Live refresh via the store's subscribe seam.
- Color-groups popover: list / add / edit / remove (label + color + DSL
  text), live recolor. All logic (viewport transform, hit-testing,
  click-vs-drag) in the headless `core/graph/viewport.py`.
- This host has no PyGObject: the chrome is verified by py-compile +
  precedent; all logic rides the headless layers' tests.

Gate: `uv run pytest && uv run ruff check && uv run mypy src`. Changelog
entry in the same pass. No push — commit and report the hash.
