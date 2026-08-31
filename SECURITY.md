# Security

## What this program does

`gentoo-install` partitions disks, writes filesystems, installs a bootloader
and configures the system that boots afterwards. Every one of those is
destructive on the wrong machine, and the program is normally run as root from
a live medium.

## Reporting

Report a vulnerability privately to <zakk@gentoozh.org>. Do not open a public
issue for one. A report that names the revision, the configuration and the
command line is one that can be reproduced; one that names a machine's
passphrase is one that has to be discarded, so redact secrets first.

There is no bounty and no service-level agreement. The project is maintained
by one person: a reply comes when there is one to give.

## Where the risk is

- **The configuration is executable.** A TOML file selects disks, mirrors,
  overlays and packages. Treat one you did not write as you would a shell
  script from the same source.
- **Passphrases are staged in a file.** A LUKS or ZFS passphrase is written
  under `/run/gentoo-install/keys/` with mode `0600` for the command that
  reads it, and `/run` is a tmpfs, so it does not survive the reboot into the
  installed system. The installer does not delete it before then. Publishing a
  configuration from the menu replaces `password_hash`, `root_password_hash`
  and `passphrase_file` and drops the proxy username and password, so a
  published configuration carries none of them. Publishing is a menu action and the closing
  offer to send a run's log; it is not a command-line option. The key file's path is replaced as well as the hashes: it is
  not the key, and it still says where key material sits on the machine.
- **What is verified.** The stage3 is checked against its DIGESTS and its
  OpenPGP signature; the repository snapshot is verified by `emerge-webrsync`;
  binary packages are refused unless their host's key is imported and locally
  signed, and a failure there degrades to compiling from source rather than
  installing something unverified.
- **What is not.** Overlays are cloned over HTTPS and are not signed by this
  program. A mirror the operator names is trusted to serve what it claims.
