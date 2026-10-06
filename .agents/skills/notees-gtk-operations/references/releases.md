# Releases — notees-gtk

The full release/packaging picture for this repo. Source of truth for job
definitions is `.github/workflows/`; this file explains how the pieces fit.

## Pipelines

### CI — `.github/workflows/ci.yml`

Runs on every push to `main` and every PR targeting `main`, on
`ubuntu-latest`:

1. `actions/checkout@v4`, `astral-sh/setup-uv@v5`, `uv python install 3.12`
2. `uv sync`
3. `uv run pytest` — the full suite (e2e self-skips headless)
4. `uv run ruff check`
5. `uv run mypy src`

### Release — `.github/workflows/release.yml`

Runs on every push to `main` AND on `v*` tags.

- **`python-dist` job** (ubuntu-latest): `uv run python -m build` → sdist +
  wheel uploaded as the `python-dist` artifact (`dist/`).
- **`archpkg` job** (`archlinux:latest` container): installs the build chain
  (`git base-devel python-build python-installer python-wheel python-hatchling
  python-httpx python-pydantic python-gobject gtk4 libadwaita`), creates a
  non-root `builder` user, runs `su builder -c makepkg`, uploads
  `notees-gtk-*.pkg.tar.zst` as the `archpkg` artifact.
- **`release` job** (only when `github.ref` starts with `refs/tags/v`): needs
  both build jobs, downloads and merges the artifacts, publishes them with
  `softprops/action-gh-release@v2`. Tags are therefore the only source of
  GitHub Releases; main-branch pushes produce artifacts only.

## Packaging

### PKGBUILD (`notees-gtk-git`)

- `pkgname=notees-gtk-git`, `provides=(notees-gtk)`, `conflicts=(notees-gtk)`.
- Sources the git repo itself (`git+https://...`); `pkgver()` runs `git
  describe --long --tags --abbrev=7` and mangles the result (`v` prefix
  stripped, `-g` → `.g`, `-` → `.`), falling back to
  `0.r<commits>.g<sha>` when no tag exists. **Tags drive the package version.**
- `build()` = `python -m build --wheel --no-isolation`; `package()` =
  `python -m installer --destdir="$pkgdir" dist/*.whl`.
- Runtime deps: `python python-httpx python-pydantic python-gobject gtk4
  libadwaita` (PyGObject + GTK4/libadwaita provide the `ui` extra on Arch).
- The AUR package tracks this PKGBUILD; update it when the PKGBUILD changes
  materially (deps, build steps).

### End-user install paths (from README)

- Arch: download the `archpkg` artifact from the latest green `release.yml`
  run on `main` (`gh run list/download`) and `sudo pacman -U`, or `makepkg
  -si` from a clone.
- Other distros: `pipx install 'git+https://...#egg=notees-gtk[ui]'` or a venv
  install; both pull the `ui` extra.

## Cutting a release

1. Ensure `main` is green (CI) and `pyproject.toml`'s `project.version`
   matches the tag you are about to push.
2. `git tag vX.Y.Z && git push origin vX.Y.Z`.
3. Watch `release.yml`: all three jobs green, Release published with the
   sdist, wheel, and `.pkg.tar.zst` attached.
4. Add the `CHANGELOG.md` entry for the release slice.

## Rolling back a bad release

- **Never delete or re-point the tag.** Mark the Release as a pre-release
  (or delete the Release assets if it was never announced), fix on `main`,
  and cut the next patch tag.
- A bad `main` build only affects CI/archpkg artifacts; they are replaced by
  the next green run and are not user-facing install sources.

## Consuming side

The client talks to a Notees server deployment (relay + object API, see the
main repo's operations docs). Client data stays local under
`~/.config/notees-gtk/` and `~/.local/share/notees-gtk/`; server connection
(URL, API key / session) is configured at first launch.
