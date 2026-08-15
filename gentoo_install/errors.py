# SPDX-License-Identifier: GPL-2.0-or-later
"""Every exception the installer raises. `cli.py` is the only module that maps
these to exit codes; the table lives in docs/design.md."""

from __future__ import annotations


class GentooInstallError(Exception):
    """Base of every error the installer raises on purpose."""


class ConfigError(GentooInstallError):
    """The configuration cannot be parsed, or it is internally inconsistent."""


class InvalidSize(ConfigError):
    """A size literal is malformed, negative, or uses an unknown unit."""


class UnalignedSize(ConfigError):
    """A size or offset does not sit on the alignment the device requires."""


class DuplicateDeviceId(ConfigError):
    """Two nodes in the device graph claim the same id."""


class UnknownDeviceId(ConfigError):
    """A node references an id that no node defines."""


class DeviceCycle(ConfigError):
    """The device graph contains a cycle, so no build order exists."""


class InvalidLayout(ConfigError):
    """A device references another of a kind it cannot be built from."""


class ValidationFailed(ConfigError):
    """The configuration parses but does not describe an installable system; the
    message carries every problem found, not the first one."""


class LocaleMissing(GentooInstallError):
    """A locale the target needs is absent after generating it."""


class DeviceNotFound(GentooInstallError):
    """A device the configuration names is absent from the running system."""


class PreflightFailed(GentooInstallError):
    """A condition the install needs does not hold on this machine."""


class IntegrityError(GentooInstallError):
    """A signature, checksum or key fingerprint did not match. Never retried
    against another mirror: untrusted data stays untrusted."""


class NothingToBoot(GentooInstallError):
    """The bootloader was written but has no kernel to offer."""


class DownloadFailed(GentooInstallError):
    """A file the install needs could not be fetched. Distinct from an integrity
    failure: the data never arrived rather than arriving untrustworthy."""


class UploadFailed(GentooInstallError):
    """A paste could not be created. Never fatal: it is offered after the run
    has already ended, and the log is still on the machine."""


class CommandFailed(GentooInstallError):
    """An external command exited non-zero. The operation did not finish, which
    is a different thing from data that cannot be trusted."""
