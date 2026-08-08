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
	lvm:*) printf 'lvm2' ;;
	gpg:*) printf 'gnupg' ;;
	zpool:debian | zfs:debian | zpool:ubuntu | zfs:ubuntu) printf 'zfsutils-linux' ;;
	zpool:arch | zfs:arch) printf 'zfs-utils' ;;
	udevadm:alpine) printf 'eudev' ;;
	chroot:alpine | tar:alpine) printf '%s' "$command" ;;
	# The busybox applets answer `which` and then reject the flags the chroot
	# and the stage3 need, so Alpine installs the real ones.
	mount:alpine | umount:alpine) printf 'util-linux' ;;
	openssl:*) printf 'openssl' ;;
	zpool:* | zfs:*) printf 'zfs' ;;
	mkfs.xfs:*) printf 'xfsprogs' ;;
	mkfs.f2fs:*) printf 'f2fs-tools' ;;
	mkfs.ext4:* | mkfs.ext2:* | mkfs.ext3:*) printf 'e2fsprogs' ;;
	tar:*) printf 'tar' ;;
	udevadm:debian | udevadm:ubuntu) printf 'systemd' ;;
	wipefs:* | blkid:* | lsblk:* | findmnt:* | blockdev:* | swapon:*) printf 'util-linux' ;;
	chroot:debian | chroot:ubuntu) printf 'coreutils' ;;
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
		say "install one with: $manager python3"
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
*) missing=$("$python" -m gentoo_install --missing-commands "$@" 2>/dev/null) || missing="" ;;
esac
if [ -n "$missing" ]; then
	packages=""
	for command in $missing; do
		package=$(package_for "$command" "$family")
		# Two commands often come from one package; naming it twice reads as
		# though the operator has to install it twice.
		case " $packages " in
		*" $package "*) ;;
		*) packages="$packages $package" ;;
		esac
	done
	say "missing commands:$(printf ' %s' $missing)"
	if manager=$(install_command "$family"); then
		say "run: $manager$packages"
	else
		say "install the packages providing them, then run this again"
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
