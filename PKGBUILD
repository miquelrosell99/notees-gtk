# Maintainer: Miquel Rosell <miquelrosell99 at gmail dot com>

pkgname=notees-gtk-git
pkgver=0.1.0
pkgrel=1
pkgdesc="First-class GTK4/libadwaita desktop client for Notees (self-hosted, offline-first notes)"
arch=(any)
url="https://github.com/miquelrosell99/notees-gtk"
license=('AGPL-3.0-only')
depends=(python python-httpx python-pydantic python-gobject gtk4 libadwaita)
makedepends=(git python-build python-installer python-wheel python-hatchling)
provides=(notees-gtk)
conflicts=(notees-gtk)
options=('!debug')
source=("$pkgname::git+https://github.com/miquelrosell99/notees-gtk.git")
sha256sums=('SKIP')

pkgver() {
  cd "$pkgname"
  desc="$(git describe --long --tags --abbrev=7 2>/dev/null)" || true
  if [ -n "$desc" ]; then
    printf '%s\n' "$desc" | sed 's/^v//;s/-g/.g/;s/-/./g'
  else
    printf "0.r%s.g%s" "$(git rev-list --count HEAD)" "$(git rev-parse --short=7 HEAD)"
  fi
}

build() {
  cd "$pkgname"
  python -m build --wheel --no-isolation
}

package() {
  cd "$pkgname"
  python -m installer --destdir="$pkgdir" dist/*.whl

  # Desktop entry — the launcher's apps-menu source. Icon name matches the
  # window's set_icon_name so the running window associates with this entry.
  install -Dm644 data/dev.notees.Gtk.desktop \
    "$pkgdir/usr/share/applications/dev.notees.Gtk.desktop"

  # Margin Green app icon (vendored from the brand/ submodule, v1.0.0);
  # resolves through the hicolor theme as dev.notees.Gtk (the window's
  # set_icon_name). glib2's pacman hook refreshes the icon cache.
  install -Dm644 data/icons/hicolor/scalable/apps/dev.notees.Gtk.svg \
    "$pkgdir/usr/share/icons/hicolor/scalable/apps/dev.notees.Gtk.svg"
  install -Dm644 data/icons/hicolor/512x512/apps/dev.notees.Gtk.png \
    "$pkgdir/usr/share/icons/hicolor/512x512/apps/dev.notees.Gtk.png"
}
