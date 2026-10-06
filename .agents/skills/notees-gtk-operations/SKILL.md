---
name: notees-gtk-operations
description: Operate the notees-gtk release pipeline — git tags as the release mechanism, GitHub Actions CI (pytest/ruff/mypy) and release.yml (sdist/wheel + Arch package + GitHub Release), PKGBUILD/AUR packaging, and makepkg local builds. Use when cutting or verifying a release, tagging, publishing packages, or changing CI/packaging in this repo.
---

# notees-gtk operations

Release truth for THIS repo only. The client consumes a Notees server
deployment (sync relay + object API); it is distributed to end machines as a
Python package and an Arch Linux package. Git tags are the release mechanism.

## Release mechanics

- **Tag-triggered releases.** Pushing a `v*` tag runs `.github/workflows/
  release.yml`, which builds the sdist/wheel (`python-dist` job) and the Arch
  package (`archpkg` job, `makepkg` inside an `archlinux` container), then
  publishes both as a GitHub Release (`release` job, gated on `refs/tags/v*`).
- **Every push to `main`** also runs `release.yml`'s build jobs, producing
  downloadable `python-dist` and `archpkg` artifacts — the Arch artifact is
  the recommended prebuilt install (see README); only tags produce a Release.
- **`.github/workflows/ci.yml`** runs on push/PR to `main`: `uv sync`, `uv run
  pytest`, `uv run ruff check`, `uv run mypy src`. Green CI is the merge gate.
- **PKGBUILD** (`notees-gtk-git`) builds from a git clone; `pkgver()` derives
  the version from `git describe --long --tags`, so tags feed the package
  version automatically. `makepkg -si` from a clone is the CI-independent
  local build. The AUR package is maintained from this PKGBUILD.
- **Version sources of truth**: the git tag (release identity + PKGBUILD
  version), and `pyproject.toml`'s `project.version` (the built wheel's
  version — bump it as part of the release commit). Client provenance on the
  wire is the `client: "gtk"` envelope claim, not the version string.

## Non-negotiable laws

1. **Never re-tag.** A broken release gets the next patch tag; re-pointing an
   existing `v*` tag breaks the PKGBUILD version derivation and any Release
   assets already attached.
2. **Tag from a green `main`.** CI must be green on the commit being tagged;
   the release jobs build exactly the tagged tree.
3. **Do not cut releases from worktrees or uncommitted trees** — `git
   describe` and the sdist both read the checkout state.
4. **Fleet-agnostic artifacts.** No machine names, IPs, or tailnet names in
   CI, PKGBUILD, or docs; install targets are described generically and
   configured per-machine.

Detail (job inventory, artifact names, verification, rollback of a bad
release): `references/releases.md`.
