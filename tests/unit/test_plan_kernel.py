from __future__ import annotations

from pathlib import Path, PurePosixPath

from gentoo_install.exec.config import load
from gentoo_install.plan import bootloader, kernel

from .recorder import Recorder


def test_grub_remote_unlock_keeps_the_system_dracut_path() -> None:
    installation = load(Path("tests/fixtures/vm-unlock.toml"))
    operation = next(
        one
        for one in kernel.build(installation)
        if isinstance(one, kernel.ConfigureRemoteUnlock)
    )
    recorder = Recorder()
    operation.apply(recorder)

    written = recorder.files[PurePosixPath("/etc/dracut.conf.d/crypt-ssh.conf")]
    assert 'dropbear_port="2222"' in written
    assert 'dropbear_rsa_key="SYSTEM"' in written
    assert 'install_items+=" /sbin/cryptsetup "' in written
    assert kernel.dracut_modules(installation)[-2:] == ("crypt-ssh", "network")
    assert not any(
        isinstance(one, bootloader.ConfigureZfsBootMenuRemoteAccess)
        for one in bootloader.build(installation)
    )
