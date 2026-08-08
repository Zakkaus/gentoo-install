[正體中文](README.md) | 简体中文 | [English](README.en.md)

# gentoo-install

从任何一套 Linux live 系统把机器装成可引导 Gentoo 的安装器。菜单或配置文件驱动，中文环境默认开启，每一项都可以关掉。

## 需求

以 root 运行，目标架构 amd64。Python 3.11 以上，只用标准库。

启动时需要能连上 `packages.gentoo.org`。内核版本与 `sys-fs/zfs` 的内核上限都是实时读取的，因此安装器不需要本机有 ebuild 树，也就能在 Alpine、Debian、openSUSE、Fedora 与 Arch 的 live 系统上运行。连不上就停，只有 `--missing-commands` 与「`--config` 加 `--dry-run`」这两种离线答案例外。

`bootstrap.sh` 读 `/etc/os-release` 判断发行版，列出这套布局缺少的命令，并打印该发行版的安装命令。支持 `apt-get`、`pacman`、`zypper`、`dnf`、`emerge` 与 `apk`。

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
./bootstrap.sh --config my-install.toml --no-shell   # 收尾不问，直接卸载
```

菜单需要真终端，画面至少 80x24。界面语言开场问一次，`--lang zh-CN` 跳过这一问。

安装结束或中途失败时，卸载之前会问一次要不要在目标系统里开一个 root shell。失败时同样会问：机器还救不救得回来由操作者判断，而卸载之后要再进去得把整个布局手动挂回来。`--no-shell` 关掉这一问。

## 配置文件

TOML，第一行声明 `config_version`。磁盘是一张设备图：每个设备有自己的 `id`，彼此以 `id` 引用，设备路径到运行时才解析。

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

`tests/fixtures/` 下有十三份可用的样例，涵盖 UEFI、BIOS、LUKS2、LVM、mdraid、ZFS 与桌面。解析不碰硬件，所以没有目标磁盘的机器也能用 `--dry-run` 验一份配置文件。

## 支持的布局

分区表 GPT 与 MBR。文件系统 ext2/3/4、btrfs（含 subvolume）、xfs、f2fs、vfat、swap 与 zram。堆叠 LUKS2、LVM、mdraid，以及 ZFS pool 与 dataset，含原生加密。引导程序 GRUB、systemd-boot，ZFS 根走 ZFSBootMenu。既有分区可以沿用：整张分区表不重写，逐个分区决定挂在哪里、要不要格式化。

## 中文环境

locale、时区、键盘、镜像、字体与输入法是六个各自独立的选项。选了输入法才安装 fcitx5 与 rime 并写入配置；Wayland 下不设 `GTK_IM_MODULE` 与 `QT_IM_MODULE`，设了候选字窗会闪烁。rime 的方案一个一组：`luna_pinyin` 随引擎一起来，`bopomofo`、`cangjie5`、`wubi86`、`jyut6ping3` 各自勾选。overlay 只在选中时加入。

控制台要显示 CJK 需要 `sys-kernel/gentoo-cjk-kernel`，它带 cjktty 补丁，在 gentoo-zh 里。选了那个内核才能选 16x32 的控制台字体。

## 二进制软件包

可选，源码编译是保证路径。官方 binhost 与 gentoo-zh 分开，密钥各自管理。取得密钥、验签或下载任何一步失败都降级为编译并打印警告，`install.jsonl` 记下每个软件包的来源与每一次降级的原因。

## 退出码

`0` 完成、`1` 配置错误、`2` preflight 失败、`3` 完整性验证失败、`4` 外部命令失败、`5` 用户中止。

## 参与开发

```sh
python3 -m mypy
python3 -m pytest
```

改动分区、文件系统、chroot、引导程序或 binhost 信任的，另外需要一次 VM 实测：

```sh
python3 -m tests.vm.run --medium official-minimal --firmware uefi --install fixtures/vm-binpkg.toml
python3 -m tests.vm.run --medium official-minimal --firmware uefi --install fixtures/vm-binpkg.toml --boot-installed
```

需要 `qemu-system-x86_64`、KVM、OVMF 与 `xorriso`。
