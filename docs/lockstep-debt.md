# Lockstep debt — E2EE custody

The tracker for deliberately deferred E2EE work in this client. Each entry
names what ships today, what's missing, and the reference design to port.

## Locked-state UI (shipped — residual polish only)

The fail-loud ruling and its UI are GTK-ported (2026-11-05):

- **Refusal**: a workspace with a key row server-side and no usable master
  key sets `SyncEngine.encrypted_locked`; push/pull/snapshot-restore refuse
  with a warning until `set_crypto` installs a key.
- **In-session unlock re-prompt**: the banner's Sign in opens
  `UnlockWorkspaceDialog` (`ui/unlock_dialog.py`) — the web's W2 modal. It
  re-authenticates through `POST /api/auth/unlock` (session-less: `{kdf}`
  only, no session minted; the login lockout applies — 429 surfaces the
  server's retry), derives the master key on the worker thread, and re-opens
  the workspace without a sign-out. Wrong password shows the form error
  (401 / verifier mismatch); success adopts the key, refreshes the badges,
  and re-selects the workspace.
- **Selector lock badges**: keyed workspaces show a lock emblem in the
  sidebar dropdown while no master key is available (web parity via the
  `encrypted` flag on `GET /api/workspaces`) — `Gtk.SignalListItemFactory`
  rows with the `system-lock-screen-symbolic` emblem.
- **Silent-keyless survivors (by design, logged)**: genuinely keyless
  workspaces (no key row) and the undetectable cases — operator/API-key
  configs the server 403s off `/key` (no account email exists for the
  unlock body, so the banner keeps the sign-out path), and offline starts —
  still sync plaintext, each with a loud log line.

Residual (not reference debt, GTK-accepted divergence): editing a locked
workspace stays possible locally — the sync round is refused and the writes
wait in the outbox until unlock.

## Snapshot uploads (engine path shipped)

The engine publishes the local derived image at the end of every pull (the
TS `maybeUploadSnapshot` port): buffered, VACUUMed past the compact
threshold, sealed and declared `sealed=1` when keyed (multipart part+commit
above the 48 MiB comfort threshold, single PUT below — the e7c9c69
transport), raw and undeclared when keyless. Watermarks gate re-uploads
(server coverage + per-session upload seq); a quarantined projection never
uploads (reported once); the never-heals 413/over-limit is reported once
per session; no upload failure ever fails the round. `LocalStore` gained
`snapshot_image` (one `node` table, canonical columns, workspace-filtered —
relay bookkeeping never rides the wire), `compact` (VACUUM), and
`quarantined_outbox_count`.

Nothing recorded here today. Remaining lockstep arms: Flutter for the whole
custody surface.
