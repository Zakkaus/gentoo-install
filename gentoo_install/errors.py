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
    """A signature, checksum or key fingerprint did not match.

    Never retried against another mirror: untrusted data stays untrusted. The
    one exception is `ArchiveDigestMismatch`, which says something about that
    mirror's copy rather than about the release.
    """


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


class ArchiveDigestMismatch(IntegrityError):
    """A downloaded archive does not match the digest its signed DIGESTS names.

    Separate from the rest of `IntegrityError` because it says something about
    one mirror rather than about the release: the signature on the DIGESTS was
    good, so the metadata is authentic and the copy of the file is not.
    """


class TargetEscape(GentooInstallError):
    """A path inside the target resolved to something outside it.

    Under `GentooInstallError` because it is raised from inside an operation:
    `apply` records only the failures it can catch, and `cli` maps only those
    to an exit code, so a plain `Exception` here left the one refusal that
    stops the installer writing to the live system unrecorded and untyped.
    """


class ResumeRefused(GentooInstallError):
    """A `--resume` whose journal was written by a different run."""


class WorkDirectoryBusy(GentooInstallError):
    """Another invocation holds this work directory."""


class ConversionUnsupported(GentooInstallError):
    """The running layout is one the conversion cannot rebuild.

    Names the layer that stops it. A root below LUKS, LVM or mdraid needs the
    whole stack described, not one `Existing` node, and stopping here leaves
    the machine untouched.
    """


class ConversionFailed(GentooInstallError):
    """The in-place swap could not be completed.

    Carries what was put back and what was not: this is the one step with no
    second attempt, and an operator reading it is deciding between a reboot
    and a rescue medium.
    """
