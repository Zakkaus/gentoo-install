[正體中文](README.md) | 简体中文 | [English](README.en.md)

# gentoo-install

从任何一套 Linux live 系统把机器装成可引导的 Gentoo。菜单或配置文件驱动，中文环境默认可用且每一项可关。

## 需求

以 root 运行。Python 3.11 以上，只用标准库。目标架构 amd64。

下列 live 介质逐一实测过。缺少的命令由 `bootstrap.sh` 列出并给该发行版的安装命令：

| 介质 | python3 | 需另装 |
|---|---|---|
| Gentoo minimal 20260712 | 3.14.6 | 无 |
| Arch 2026.08.01 | 3.14.6 | 无 |
| openSUSE Tumbleweed Rescue | 3.13.14 | 无 |
| Fedora Workstation Live 43 | 3.14.0 | `gptfdisk` |
| Debian live 13.6 | 3.13.5 | `dosfstools`、`gdisk` |
| Alpine 3.24.1 | 无 | `python3` 起 |

## 使用

```sh
curl -fsSL https://github.com/Zakkaus/gentoo-install/archive/refs/heads/master.tar.gz | tar xz
cd gentoo-install-master
./bootstrap.sh
```

`bootstrap.sh` 是唯一入口。

```sh
./bootstrap.sh                                       # 菜单
./bootstrap.sh --config my-install.toml              # 无人值守
./bootstrap.sh --dry-run --config my-install.toml    # 只打印操作，不碰磁盘
./bootstrap.sh --config my-install.toml --resume     # 从上次停下的地方继续
```

菜单需要真终端。界面语言开场问一次，`--lang zh-CN` 跳过。

## 配置文件

TOML，第一行声明 `config_version`。磁盘是一张设备图：每个设备有 `id`，彼此以 `id` 引用，路径运行时才解析。

```toml
config_version = 1

[system]
hostname = "gentoo"
locale = "zh_CN.UTF-8"
init = "systemd"
root_password_hash = "$6$..."   # openssl passwd -6 生成，不放明文

[portage]
profile = "default/linux/amd64/23.0/systemd"   # 必须与 init 一致

[bootloader]
kind = "grub"
firmware = "uefi"

[disk]
root = "mnt-root"

[[disk.devices]]
kind = "existing"
id = "disk"
selector = "/dev/disk/by-id/virtio-target0"
wipe = true
```

示例在 `tests/fixtures/`。解析不碰硬件，没有目标磁盘的机器也能用 `--dry-run` 验配置。

## 已实测

下列每一条都装完、关机、拔掉介质再开机，检查挂载、fstab、locale、开机服务与失败单元。

- UEFI 加 `gentoo-kernel-bin`
- 从源码构建内核
- ZFS 加 ZFSBootMenu，未加密与原生加密各一
- BIOS 加 MBR 加 openrc
- systemd-boot
- LUKS2 加 btrfs subvolume
- LVM
- mdraid RAID1
- KDE Plasma 加中文环境

## 中文环境

locale、时区、键盘、镜像、字体、输入法各自独立。选了输入法才装 fcitx5 与 rime 并写入配置；Wayland 下不设 `GTK_IM_MODULE` 与 `QT_IM_MODULE`，设了候选词窗会闪烁。overlay 只在选中时加入。

## 二进制包

可选，源码编译是保证路径。官方与 gentoo-zh 分开，密钥各自管理。任一步失败降级为编译并警告，`install.jsonl` 记下每个包的来源与降级原因。

## 退出码

`0` 完成、`1` 配置错误、`2` preflight 失败、`3` 完整性验证失败、`4` 外部命令失败、`5` 用户中止。

## 参与开发

```sh
python3 -m mypy
python3 -m pytest
```

改动分区、文件系统、chroot、引导程序或 binhost 信任的，另需一次 VM 实测：

```sh
python3 -m tests.vm.run --medium official-minimal --firmware uefi --install fixtures/vm-binpkg.toml
python3 -m tests.vm.run --medium official-minimal --firmware uefi --install fixtures/vm-binpkg.toml --boot-installed
```

需要 `qemu-system-x86_64`、KVM、OVMF 与 `xorriso`。
