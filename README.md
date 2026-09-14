# notees-gtk

First-class GTK/Adwaita desktop client for [Notees](https://github.com/miquelrosell99/notees), a self-hosted, privacy-first note-taking application.

Work in progress: the sync protocol core, API client, SQLite sync engine, and GTK UI are being built incrementally.

## Running the UI

The desktop client needs a GTK 4 / libadwaita host (e.g. Arch Linux with `libadwaita` installed) plus the `ui` extra:

```sh
pip install 'notees-gtk[ui]'
notees-gtk
```

On first launch you are asked for the server URL (default `http://localhost:8001`), email, and password; TOTP is prompted when the account has 2FA enabled. The session is stored under `~/.config/notees-gtk/` and the local sync cache under `~/.local/share/notees-gtk/`.

Headless machines can still run the test suite (`uv run pytest`) and the linters (`uv run ruff check`, `uv run mypy src`); the GTK widgets are only importable where PyGObject is installed.

## License

AGPL-3.0 — see [LICENSE](LICENSE).
