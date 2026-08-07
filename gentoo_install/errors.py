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
