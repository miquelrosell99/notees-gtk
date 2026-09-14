# notees-gtk

First-class GTK4/libadwaita desktop client for [Notees](https://github.com/miquelrosell99/notees), a self-hosted, privacy-first note-taking application with an operation-log sync protocol.

<!-- Screenshot: add an app window capture here once the UI stabilizes. -->

## Features

- **Offline-first sync** — edits go to a local SQLite outbox and are pushed when the server is reachable; catch-up pulls and snapshots restore the local mirror.
- **2FA support** — TOTP second factor prompted at login when the account requires it.
- **Workspaces** — switch between server workspaces from the sidebar.
- **Plain-text editing** — a fast split view (page tree + editor) rendered from the note AST.
- **Local session** — credentials and sync state stay on your machine under `~/.config/notees-gtk/` and `~/.local/share/notees-gtk/`.

## Requirements

- Python 3.12 or later.
- For the desktop UI: GTK 4 and libadwaita system libraries, plus PyGObject — installed automatically with the `ui` extra (`pip install 'notees-gtk[ui]'`). On Arch these are `gtk4` and `libadwaita`; on Fedora `gtk4` and `libadwaita`; on Debian/Ubuntu `gir1.2-gtk-4.0` and `gir1.2-adw-1`.
- For production use, serve Notees over TLS or a private overlay network such as Tailscale (per the relay SPEC): the sync protocol encrypts nothing in transit by itself, and the client's default `http://localhost:8001` is for local development only.

Headless machines can run the test suite and linters without the `ui` extra; the GTK widgets are only importable where PyGObject is installed.

## Install

### Arch Linux

**Prebuilt package (recommended).** GitHub CI builds an Arch package on every push to `main`. Download the `archpkg` artifact from the latest green run and install it:

```sh
gh run list --workflow release.yml --repo miquelrosell99/notees-gtk --branch main --limit 1
gh run download <run-id> --repo miquelrosell99/notees-gtk --name archpkg
sudo pacman -U notees-gtk-*.pkg.tar.zst
```

**Build from source.** Clone the repository and use `makepkg` (CI-independent):

```sh
git clone https://github.com/miquelrosell99/notees-gtk.git
cd notees-gtk
makepkg -si
```

### Other distributions

Install with pipx (recommended) or into a virtual environment; both pull the `ui` extra:

```sh
pipx install 'git+https://github.com/miquelrosell99/notees-gtk.git#egg=notees-gtk[ui]'
```

```sh
python -m venv .venv && . .venv/bin/activate
pip install 'git+https://github.com/miquelrosell99/notees-gtk.git#egg=notees-gtk[ui]'
notees-gtk
```

On first launch you are asked for the server URL (default `http://localhost:8001`), email, and password; TOTP is prompted when the account has 2FA enabled.

Not yet on Flathub — distribution as a Flatpak is planned once the client reaches its first stable tag.

## Development

Dev setup (uses [uv](https://docs.astral.sh/uv/)):

```sh
uv python install 3.12
uv sync
```

Run a Notees dev server from the [notees](https://github.com/miquelrosell99/notees) checkout and point the client at it:

```sh
docker compose -f compose.dev.yaml up
uv run notees-gtk
```

Checks:

```sh
uv run pytest
uv run ruff check
uv run mypy src
```

## Building

- Python sdist/wheel and the Arch package are built by GitHub Actions — see the [Actions page](https://github.com/miquelrosell99/notees-gtk/actions). Pushes to `main` and `v*` tags produce downloadable artifacts; tags additionally publish a GitHub Release with both attached.
- To build the Arch package locally, run `makepkg -si` from a clone (see [Install](#arch-linux)).

## License

[AGPL-3.0-only](LICENSE) — GNU Affero General Public License v3.0.
