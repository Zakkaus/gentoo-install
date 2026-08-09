#!/bin/sh
# Start the installer from whatever live system you happen to have booted.
#
# POSIX sh on purpose: this runs before anything is installed, and the live
# media of the distributions below do not agree on which shell is /bin/sh.
set -eu

PYTHON_MINIMUM_MINOR=11
# Overridable so the test can hand it a file from another distribution.
OS_RELEASE=${OS_RELEASE:-/etc/os-release}
# Parameter expansion, not dirname: a live system with a broken PATH should
# still reach the message that says what is wrong.
case "$0" in
*/*) HERE=${0%/*} ;;
*) HERE=. ;;
esac

say() { printf '%s\n' "$*" >&2; }
die() { say "error: $*"; exit 1; }

distribution() {
	# ID_LIKE first: derivatives name their parent there, so Mint reports
	# ubuntu, Manjaro reports arch, and the package manager below matches.
	if [ -r "$OS_RELEASE" ]; then
		# shellcheck disable=SC1091
		. "$OS_RELEASE"
		for candidate in ${ID_LIKE:-} ${ID:-}; do
			case "$candidate" in
			debian | ubuntu | arch | suse | opensuse* | fedora | rhel | centos | gentoo | alpine)
				printf '%s\n' "$candidate"
				return 0
				;;
			esac
		done
		printf '%s\n' "${ID:-unknown}"
		return 0
	fi
	printf 'unknown\n'
}

install_command() {
	# How to install a package, per family. Nothing is installed without the
	# operator seeing the exact command first.
	case "$1" in
	debian | ubuntu) printf 'apt-get install -y' ;;
	arch) printf 'pacman -Sy --needed --noconfirm' ;;
	suse | opensuse | opensuse-leap | opensuse-tumbleweed) printf 'zypper --non-interactive install' ;;
	fedora | rhel | centos) printf 'dnf install -y' ;;
	gentoo) printf 'emerge --noreplace' ;;
	alpine) printf 'apk add' ;;
	*) return 1 ;;
	esac
}

# Which package ships each command, where the name differs by distribution.
package_for() {
	command=$1
	family=$2
	case "$command:$family" in
	sgdisk:debian | sgdisk:ubuntu | sgdisk:fedora | sgdisk:rhel | sgdisk:centos) printf 'gdisk' ;;
	sgdisk:alpine) printf 'sgdisk' ;;
	sgdisk:*) printf 'gptfdisk' ;;
	partprobe:* | parted:*) printf 'parted' ;;
	mkfs.vfat:* | mkfs.fat:*) printf 'dosfstools' ;;
	mkfs.btrfs:* | btrfs:*) printf 'btrfs-progs' ;;
	cryptsetup:*) printf 'cryptsetup' ;;
	mdadm:*) printf 'mdadm' ;;
	# The multicall name and the three binaries the operations invoke: all of
	# them come from lvm2, and the three reached the fallback and were printed
	# as package names no distribution has.
	lvm:* | pvcreate:* | vgcreate:* | lvcreate:*) printf 'lvm2' ;;
	gpg:*) printf 'gnupg' ;;
	zpool:debian | zfs:debian | zpool:ubuntu | zfs:ubuntu) printf 'zfsutils-linux' ;;
	# Nothing official provides these on Arch: `zfs-utils` is in archzfs, a
	# third-party repository the medium has not configured, and `pacman -S
	# zfs-utils` on a stock image answers `target not found`. Empty, so the
	# launcher says the medium cannot supply them instead of printing a
	# command that fails.
	zpool:arch | zfs:arch) ;;
	udevadm:alpine) printf 'eudev' ;;
	udevadm:debian | udevadm:ubuntu) printf 'udev' ;;
	udevadm:fedora | udevadm:rhel | udevadm:centos) printf 'systemd-udev' ;;
	udevadm:*) printf 'systemd' ;;
	tar:alpine) printf 'tar' ;;
	# Alpine splits util-linux into one package per tool, and the `util-linux`
	# package itself is an empty placeholder that installs nothing. The busybox
	# applets answer `which` and then reject the flags, so each real one is
	# named on its own.
	mount:alpine | umount:alpine | lsblk:alpine | blkid:alpine | findmnt:alpine \
		| wipefs:alpine) printf '%s' "$command" ;;
	# Debian keeps mount and umount in a package of their own; everywhere
	# else they come from util-linux and the fallback printed `mount`.
	mount:debian | umount:debian | mount:ubuntu | umount:ubuntu) printf 'mount' ;;
	mount:* | umount:*) printf 'util-linux' ;;
	blockdev:alpine | swapon:alpine | swapoff:alpine | mkswap:alpine)
		printf 'util-linux-misc'
		;;
	# Debian and Ubuntu keep swapoff in the mount package, and Fedora keeps
	# both swap tools in util-linux-core. Neither split is guessable, and the
	# generic util-linux row below is wrong for both.
	swapoff:debian | swapoff:ubuntu) printf 'mount' ;;
	mkswap:fedora | swapoff:fedora) printf 'util-linux-core' ;;
	openssl:*) printf 'openssl' ;;
	zpool:* | zfs:*) printf 'zfs' ;;
	mkfs.xfs:*) printf 'xfsprogs' ;;
	mkfs.f2fs:*) printf 'f2fs-tools' ;;
	mkfs.ext4:* | mkfs.ext2:* | mkfs.ext3:*) printf 'e2fsprogs' ;;
	tar:*) printf 'tar' ;;
	wipefs:* | blkid:* | lsblk:* | findmnt:* | blockdev:* | swapon:* | swapoff:* \
		| mkswap:*) printf 'util-linux' ;;
	# No distribution ships a package named after either of these.
	chroot:* | hostid:*) printf 'coreutils' ;;
	*) printf '%s' "$command" ;;
	esac
}

python_binary() {
	# The newest python3 on PATH, because a live system can carry several and
	# `python3` is not always the newest one.
	for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
		binary=$(command -v "$candidate" 2>/dev/null) || continue
		minor=$("$binary" -c 'import sys; print(sys.version_info[1])' 2>/dev/null) || continue
		major=$("$binary" -c 'import sys; print(sys.version_info[0])' 2>/dev/null) || continue
		[ "$major" = 3 ] || continue
		[ "$minor" -ge "$PYTHON_MINIMUM_MINOR" ] || continue
		printf '%s\n' "$binary"
		return 0
	done
	return 1
}

family=$(distribution)
say "live system: $family"

if ! python=$(python_binary); then
	found=$(command -v python3 >/dev/null 2>&1 && python3 --version 2>&1 || printf 'none')
	say "this installer needs python 3.$PYTHON_MINIMUM_MINOR or newer; found: $found"
	if manager=$(install_command "$family"); then
		# A versioned name for the dnf families: their bare `python3` is the
		# platform 3.9, which fails this same check.
		case "$family" in
		fedora | rhel | centos) say "install one with: $manager python3.12" ;;
		*) say "install one with: $manager python3" ;;
		esac
	fi
	exit 1
fi
say "python: $python ($("$python" --version 2>&1))"

# The installer's own preflight lists what the chosen layout needs and names
# every missing command at once, so the operator fixes them in one pass. Not
# for a dry run: it performs nothing, and refusing it on a machine without the
# tools takes away the one way to check a file before reaching the target.
case " $* " in
*" --dry-run "*) missing="" ;;
*)
	# PYTHONPATH rather than a cd: `--config` names a path the operator typed,
	# which has to resolve against their directory and not against this script's.
	if ! missing=$(PYTHONPATH=$HERE "$python" -m gentoo_install --missing-commands "$@" 2>&1); then
		# Reported rather than read as an empty list: an unreadable tree and a
		# machine with every tool present are not the same answer.
		say "the installer could not list what is missing: $missing"
		exit 1
	fi
	;;
esac
if [ -n "$missing" ]; then
	packages=""
	unavailable=""
	for command in $missing; do
		package=$(package_for "$command" "$family")
		if [ -z "$package" ]; then
			unavailable="$unavailable $command"
			continue
		fi
		# Two commands often come from one package; naming it twice reads as
		# though the operator has to install it twice.
		case " $packages " in
		*" $package "*) ;;
		*) packages="$packages $package" ;;
		esac
	done
	say "missing commands:$(printf ' %s' $missing)"
	if [ -n "$unavailable" ]; then
		say "this system has no package for:$unavailable"
	fi
	if [ -n "$packages" ] && manager=$(install_command "$family"); then
		say "run: $manager$packages"
	else
		say "install what provides them, then run this again"
	fi
	exit 1
fi

# Last, so the diagnostics above still run for an ordinary user: what needs
# root is the install itself, which stages keys under /run and writes disks.
case " $* " in
*" --dry-run "*) ;;
*) [ "$(id -u)" = 0 ] || die "run as root: sudo $0 $*" ;;
esac

cd "$HERE"
exec "$python" -m gentoo_install "$@"
