[正體中文](README.md) | 简体中文 | [English](README.en.md)

# gentoo-install

从任意 Linux live 系统把一台机器装成可开机的 Gentoo 的文本安装器。用菜单或配置文件驱动，中文环境默认可用，每一项都是可关的选项。它是 Gig-OS Live ISO 上 Calamares 图形安装器的文本对照物：没有桌面、没有鼠标、可以通过串口或 SSH 远程装机。

## 支持的环境

安装器在 live 系统上运行，把 Gentoo 装到目标磁盘。以下六种 live 媒介逐一开机实测过：

| live 系统 | 版本 | python3 | 需要另外安装的 |
|---|---|---|---|
| Gentoo minimal | 20260712 | 3.14.6 | 无 |
| Arch | 2026.08.01 | 3.14.6 | 无 |
| openSUSE Tumbleweed Rescue | current | 3.13.14 | 无 |
| Fedora Workstation Live | 43 | 3.14.0 | `gptfdisk` |
| Debian live standard | 13.6 | 3.13.5 | `dosfstools`、`gdisk` |
| Alpine standard | 3.24.1 | 无 | `python3` 起 |

Python 下限是 3.11，由标准库的 `tomllib` 决定。安装器只用标准库，不引入第三方依赖。目标架构是 amd64。

缺哪些命令由启动脚本算出来并给出该发行版的安装命令，不必自己对照。

## 不用先 clone

仓库是公开的，所以一行就能取得并执行。这条在任何一种 live 系统上都一样：

```sh
curl -fsSL https://github.com/Zakkaus/gentoo-install/archive/refs/heads/master.tar.gz | tar xz
cd gentoo-install-master && ./bootstrap.sh
```

需要 `curl` 与 `tar`；两者每一种 live 媒介都有，Alpine 的 busybox tar 也解得开这个压缩档（stage3 才需要 GNU tar，那是安装器自己检查的）。

## 使用

安装器要以 root 运行。`bootstrap.sh` 是唯一入口，它检查 Python 版本、列出缺少的命令，然后执行安装器。

先看一遍将要执行的操作，这一步不碰磁盘：

```sh
./bootstrap.sh --dry-run --config tests/fixtures/vm-binpkg.toml
```

用菜单装机：

```sh
./bootstrap.sh
```

用配置文件无人值守装机：

```sh
./bootstrap.sh --config my-install.toml
```

菜单需要真终端；在管道里运行会报错并提示改用 `--config`。界面语言从 `LC_ALL`、`LC_MESSAGES`、`LANG` 依次推导，`--lang zh-CN` 可覆盖。

## 配置文件

配置文件是 TOML，第一行必须声明 `config_version`。磁盘部分是一张设备图：每个设备有一个 `id`，彼此用 `id` 引用，路径在执行时才解析，所以中断重跑对应关系不变。

```toml
config_version = 1

[system]
hostname = "gentoo"
locale = "zh_CN.UTF-8"
init = "systemd"
# crypt(3) 哈希，不是明文。用 openssl passwd -6 生成。
root_password_hash = "$6$..."

[portage]
# profile 必须与 init 一致，否则验证不通过。
profile = "default/linux/amd64/23.0/systemd"

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

完整的例子在 `tests/fixtures/`，每份对应一条实测过的路径。

配置文件解析不碰硬件，所以在没有目标磁盘的机器上也能用 `--dry-run` 验证一份配置。

## 已实测的安装路径

下列十条各自装完、关机、拔掉安装媒介后再开机验证过，检查项包括挂载、fstab、locale、开机服务与有无失败单元：

- UEFI 加 `gentoo-kernel-bin`
- 从 sources 包现地编译内核
- ZFS 根加 ZFSBootMenu，未加密与原生加密各一
- BIOS 加 MBR 加 openrc
- systemd-boot
- LUKS2 加 btrfs subvolume
- LVM
- mdraid RAID1
- KDE Plasma 桌面加中文环境

## 中文环境

locale、时区、键盘、镜像、字体、输入法是各自独立的选项，不是一个绑死的包。选了输入法才会装 fcitx5 与 rime，并写入 `/etc/skel` 与每个用户家目录的配置；Wayland 会话下不设 `GTK_IM_MODULE` 与 `QT_IM_MODULE`，因为合成器用 text-input 协议直接驱动 fcitx，设了会让候选词窗口闪烁。

overlay 是选进来的，不会背着用户加。`gentoo-zh` 与 `gig` 各自独立，选中时一并配置密钥与 `package.accept_keywords`。

## 二进制包

`binpkg` 是可选路径，源码编译永远是保证路径。官方与社区 binhost 是两个独立开关，各自的密钥分开管理。任何一步失败——主机不可达、缺签名、密钥不受信任——都降级为源码编译并给出警告，`install.jsonl` 记下每个包的来源与每次降级的原因。

## 退出码

| 码 | 意义 |
|---|---|
| 0 | 完成 |
| 1 | 配置错误：解析、验证、版本不兼容 |
| 2 | preflight 硬性检查失败 |
| 3 | 完整性验证失败：GPG、校验和、指纹 |
| 4 | 外部命令失败，或文件下载不成 |
| 5 | 用户中止 |

## 参与开发

```sh
python3 -m mypy
python3 -m pytest
```

两者都必须通过。改动分区、文件系统、chroot、引导程序或 binhost 信任的，还要有一次 `tests/vm/run.py` 实测：

```sh
python3 -m tests.vm.run --medium official-minimal --firmware uefi --install fixtures/vm-binpkg.toml
python3 -m tests.vm.run --medium official-minimal --firmware uefi --install fixtures/vm-binpkg.toml --boot-installed
```

VM 测试需要 `qemu-system-x86_64`、KVM、OVMF 与 `xorriso`。ISO 缓存在 `lab/vm/`。
