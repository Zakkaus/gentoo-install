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


class ValidationFailed(ConfigError):
    """The configuration parses but does not describe an installable system; the
    message carries every problem found, not the first one."""


class DeviceNotFound(GentooInstallError):
    """A device the configuration names is absent from the running system."""
