[English](README.md) | [正體中文](README.zh-TW.md) | 简体中文 | [日本語](README.ja.md) | [한국어](README.ko.md)

# gentoo-install

从任何一套运行中的 Linux live 系统，把机器装成可引导 Gentoo 的安装器。以菜单或配置文件驱动。界面提供英文、繁体中文、简体中文、日本語与한국어。

![菜单，列出安装器所做的每一项决定](screenshot.png)

![cjktty 控制台渲染简体中文、繁体中文、日文与韩文](cjk-console.png)

## 功能

**磁盘。** GPT 与 MBR。ext2/3/4、带 subvolume 的 btrfs、xfs、f2fs、vfat、swap 与 zram。LUKS2、LVM，以及 raid0、raid1、raid5、raid6 的 mdraid。ZFS pool 与 dataset，含原生加密、mirror 与 raidz。既有分区表可以沿用：逐个分区指定挂载点，并各自决定是否格式化。

**引导。** UEFI 与 BIOS 上的 GRUB、systemd-boot，ZFS 根则用 ZFSBootMenu。initramfs 由 dracut 生成，模块清单从设备图推导而非手动列出。根文件系统可在 initramfs 阶段以 SSH 解锁。

**内核。** `::gentoo` 的 `sys-kernel/gentoo-kernel-bin` 与 `sys-kernel/gentoo-kernel`，以及 gentoo-zh overlay 的 `sys-kernel/gentoo-cjk-kernel-bin` 与 `sys-kernel/gentoo-cjk-kernel`。gentoo-zh 那一对带 [cjktty-patches](https://github.com/gentoo-zh/cjktty-patches)，能在文字控制台显示中文、日文与韩文，原版内核在同一位置画的是空白。上面第二张图是应用该补丁的 `7.1.7-gentoo-dist`。

**系统。** systemd 或 OpenRC。NetworkManager 搭配 wpa_supplicant 或 iwd、systemd-networkd，或不设网络。静态地址、DNS、主机名与时区。首次开机执行一次的命令或脚本，可指定网址获取。

**桌面。** GNOME、KDE Plasma 与 Xfce，登录管理程序为 gdm、sddm、lightdm 或 greetd。显卡涵盖 amdgpu、intel、nvidia、nouveau、radeon 与虚拟机：`VIDEO_CARDS`、该驱动需要的 USE 标记与内核参数一并设置，不留给操作者自行补齐。

**输入法。** fcitx5 与 ibus。Rime 提供拼音、注音、仓颉、五笔与粤拼方案；日文为 Anthy 与 Mozc；韩文为 Hangul。字体与 locale 是两个各自独立的选项。

**Portage。** profile、`MAKEOPTS`、`USE`、`ACCEPT_KEYWORDS`、`L10N`、镜像区域与仓库同步方式。gentoo-zh 与 gig overlay 为可选，选中时一并写入其密钥与 `package.accept_keywords`。二进制包来自官方主机与 gentoo-zh，密钥各自管理。

**每一项功能都有 dry run。** `--dry-run` 依同一份计划打印实际执行会应用的操作清单，因此只打印不执行的路径无法与真正执行的路径分歧。中断的安装可依日志续装。`install.jsonl` 记录每个包的来源与每一次退回源码编译的原因。配置文件可导出到 pastebin 或控制台上的 QR code，密码哈希在导出前移除。

## 需求

以 root 运行，目标架构 amd64，Python 3.11 以上，只用标准库。

启动时需要能连上 `packages.gentoo.org`。内核版本与 `sys-fs/zfs` 的内核上限都是实时读取，因此本机不需要 ebuild 树，安装器也就能在 Alpine、Debian、openSUSE、Fedora、Arch 与 Gentoo 的 live 系统上运行。连不上就停止，只有 `--missing-commands` 以及 `--config` 加 `--dry-run` 两种离线答案例外。

`bootstrap.sh` 读 `/etc/os-release`，列出所选布局需要而本机缺少的命令，并打印该发行版的安装命令。已知 `apt-get`、`pacman`、`zypper`、`dnf`、`emerge` 与 `apk`。

## 使用

```sh
curl -fsSL https://github.com/Zakkaus/gentoo-install/archive/refs/heads/master.tar.gz | tar xz
cd gentoo-install-master
./bootstrap.sh
```

```sh
./bootstrap.sh                                       # 菜单
./bootstrap.sh --config my-install.toml              # 无人值守
./bootstrap.sh --dry-run --config my-install.toml    # 只打印操作，不碰磁盘
./bootstrap.sh --config my-install.toml --resume     # 从上次停下的地方继续
./bootstrap.sh --config my-install.toml --no-shell   # 收尾不询问，直接卸载
```

菜单需要真终端，画面至少 80x24。界面语言在开场询问一次，`--lang zh-CN` 跳过这一问。

卸载之前，无论安装完成或失败，安装器都会提供在新系统里打开 root shell 的选项。失败时同样提供：机器是否还能救回由操作者判断，而卸载之后要再进去就得把整个布局手动挂回来。`--no-shell` 移除这一问。

## 配置文件

TOML，第一行声明 `config_version`。磁盘是一张设备图：每个设备带有 `id`，设备之间以 `id` 互相引用，设备路径到运行时才解析。

```toml
config_version = 1

[system]
hostname = "gentoo"
locale = "zh_CN.UTF-8"
init = "systemd"
root_password_hash = "$6$..."   # 由 openssl passwd -6 生成，不放明文

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

`tests/fixtures/` 下有可用的示例，涵盖 UEFI、BIOS、LUKS2、LVM、mdraid、ZFS、btrfs subvolume 与桌面。解析不碰硬件，因此没有目标磁盘的机器也能以 `--dry-run` 检查一份配置文件。

## 二进制包

可选，而且从不是唯一路径。源码编译才是保证路径。官方 binhost 与 gentoo-zh binhost 是两个各自独立的选项，密钥分开管理。主机不可达、缺少签名或密钥不受信任，都退回编译并打印警告，`install.jsonl` 记下原因。

## 退出码

`0` 完成、`1` 配置错误、`2` preflight 失败、`3` 完整性验证失败、`4` 外部命令失败、`5` 操作者中止。

## 参与开发

见 [CONTRIBUTING.md](CONTRIBUTING.md)。
