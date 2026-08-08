from dataclasses import replace
import pytest
from .layouts import config
from gentoo_install.model.config import KernelConfig, KernelSource
from gentoo_install.model.validate import validate
from gentoo_install.errors import ValidationFailed
from gentoo_install.plan.kernel import kernel_operations

def test_atoms():
    for atom in ['sys-kernel/gentoo-sources', 'sys-kernel/gentoo-sources:6.12.16',
                 '=sys-kernel/gentoo-sources-6.12.16', 'sys-kernel/gentoo-sources-6.12.16',
                 'sys-kernel/vanilla-sources:6.12']:
        c = replace(config(), kernel=KernelConfig(source=KernelSource.DIST_SOURCE, package=atom))
        try:
            validate(c)
            ops = kernel_operations(c)
            emerges = [o.describe() for o in ops if 'kernel' in o.describe().lower() or 'install' in o.describe().lower()]
            print('PASSES:', atom, '->', [o for o in emerges if 'kernel' in o])
        except ValidationFailed:
            print('REJECTED:', atom)
