# Lockstep debt — E2EE custody

The tracker for deliberately deferred E2EE work in this client. Each entry
names what ships today, what's missing, and the reference design to port.

## Locked-state UI (missing)

Locked sync — a keyed workspace opened without the master key — is **log-only**
in the GTK client: remote `$e` envelopes and sealed snapshots are skipped with
counted warnings (`core/sync/engine.py`), local writes stay plaintext, and
recovery is a full **re-login** (the login flow re-derives the master key; the
next workspace open re-installs the engine provider). There is no on-screen
locked indicator and no in-app unlock re-prompt.

Reference design (the main repo's web client, shipped 2026-11-03):

- `EncryptedLockedBanner` (sticky warning bar: states the consequence —
  plaintext writes, sealed remote changes skipped — with an Unlock
  affordance), rendered from the connect outcome's locked state.
- `UnlockWorkspaceModal` — asks for the account password, re-authenticates
  (fresh kdf → master-key derivation), and re-opens the workspace without a
  sign-out; the server lockout (429) is surfaced distinctly.
- Locked-state plumbing: the key-resolution seam returns `{ key, locked }`
  and the banner renders off that.

## Snapshot-upload sealing (unwired)

The engine's snapshot path today only **restores** (download → decrypt when
keyed → locked-skip when not). There is no client snapshot-upload path in this
client at all — so sealing-at-upload has nothing to attach to yet. When the
upload path lands (server-directed snapshot publishing), the sealed upload is
part of that slice: `WorkspaceKeyCrypto.encrypt_bytes` over the serialized
image, mirroring the TS reference's `maybeUploadSnapshot` crypto branch.
