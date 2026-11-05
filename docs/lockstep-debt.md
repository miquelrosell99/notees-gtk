# Lockstep debt — E2EE custody

The tracker for deliberately deferred E2EE work in this client. Each entry
names what ships today, what's missing, and the reference design to port.

## Locked-state UI (partial — fail-loud ruling shipped 2026-11-05)

The fail-loud ruling (owner decision, 2026-11-04 — a keyed workspace must
never silently open keyless) is GTK-ported:

- **Refusal**: `_install_workspace_crypto` (`ui/window.py`) fetch-or-ensures
  the workspace key before the first sync round; a workspace with a key row
  server-side and no usable master key (no master in memory, or the wrapped
  record fails to unwrap under the current password's master key) sets
  `SyncEngine.encrypted_locked`, and push/pull/snapshot-restore **refuse with
  a warning** — nothing plaintext moves. `set_crypto` clears the lock.
- **Loud state**: an `Adw.Banner` on the main page — "This workspace is
  encrypted — sign in with your password to open it" — with a Sign-in
  affordance that returns to the login page; a password sign-in re-derives
  the master key and the post-login workspace load re-opens unlocked.
  Interval sync skips the locked workspace.
- **Silent-keyless survivors (by design, logged)**: a genuinely keyless
  workspace (no key row — plaintext is its contract) and the undetectable
  cases — operator/API-key configs the server 403s off `/key`, and offline
  starts that cannot ask — still sync plaintext, each with a loud log line.

Still missing vs the web reference (`feat(web,server): keyed workspaces fail
loud`, 2026-11-04):

- An **in-app unlock re-prompt** (the web's `UnlockWorkspaceModal` asks for
  the account password and re-opens without a full sign-out). GTK's path is
  the banner → login page → re-login, which costs the session reload the
  modal avoids.
- **Lock badges in the workspace selector** — `WorkspaceRef.encrypted` is
  parsed (wire parity) but the sidebar renders no badge.
- Editing a locked workspace stays possible locally (the engine refuses the
  sync round; the writes sit in the outbox until unlock).

## Snapshot uploads (transport ported, engine path unwired)

The wire transport now carries the full upload surface:

- `upload_snapshot(..., sealed=True)` sends `sealed=1` — sealed images skip
  the server sanity gate (ciphertext is opaque), accepted never 422-gated.
- Optional multipart methods per WIRE.md: `upload_snapshot_part`
  (`PUT /snapshot/data/part`, one 64 MiB chunk, duplicate index 422) and
  `commit_snapshot` (`POST /snapshot/data/commit`, parts must match the
  staged indexes, same sanity gate + `sealed` flag, 201 mirrors the single
  PUT). The single-PUT fallback stays the default path.

The **engine upload path itself is still unwired** — restore-only, as
before. When it lands (server-directed snapshot publishing), it seals with
`WorkspaceKeyCrypto.encrypt_bytes`, declares `sealed=1`, chunks above the
48 MiB comfort threshold, and mirrors the TS reference's
`maybeUploadSnapshot` crypto branch (skip when the local projection carries
quarantined rows; report-once on 413).
