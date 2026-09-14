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
  git describe --long --tags --abbrev=7 2>/dev/null | sed 's/^v//;s/-g/.g/;s/-/./g' \
    || printf "0.r%s.g%s" "$(git rev-list --count HEAD)" "$(git rev-parse --short=7 HEAD)"
}

build() {
  cd "$pkgname"
  python -m build --wheel --no-isolation
}

package() {
  cd "$pkgname"
  python -m installer --destdir="$pkgdir" dist/*.whl
}
