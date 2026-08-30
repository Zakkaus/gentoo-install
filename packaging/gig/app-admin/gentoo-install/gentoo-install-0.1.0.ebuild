# Copyright 2026 Zakk
# Distributed under the terms of the GNU General Public License v2

EAPI=8

PYTHON_COMPAT=( python3_{11..14} )
inherit python-single-r1

DESCRIPTION="Interactive Gentoo installer for the Gig-OS Live ISO"
HOMEPAGE="https://github.com/Zakkaus/gentoo-install"
SRC_URI="https://github.com/Zakkaus/gentoo-install/archive/refs/tags/v${PV}.tar.gz
	-> ${P}.tar.gz"

LICENSE="GPL-2+"
SLOT="0"
KEYWORDS="~amd64"
REQUIRED_USE="${PYTHON_REQUIRED_USE}"

# Standard library only: the installer takes no runtime dependency the medium
# does not already carry.
RDEPEND="${PYTHON_DEPS}"

src_install() {
	python_moduleinto gentoo_install
	python_domodule gentoo_install/.

	exeinto /usr/local/libexec/gentoo-install
	doexe bootstrap.sh

	# The one path `plan/netboot.py` names in its banner, so the command an
	# operator is told about is the command the medium has.
	exeinto /usr/local/sbin
	doexe packaging/launcher/gentoo-install

	# Beside the Calamares entry rather than instead of it.
	insinto /usr/share/applications
	doins packaging/launcher/gentoo-install.desktop

	dodoc README.md TESTED.md
}
