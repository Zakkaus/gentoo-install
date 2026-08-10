[English](README.md) | [正體中文](README.zh-TW.md) | 简体中文 | [日本語](README.ja.md) | [한국어](README.ko.md)

# gentoo-install

gentoo-install 是一个系统安装程序，支持在兼容的 Linux Live 环境中安装 amd64 架构的 Gentoo 系统。可通过交互式菜单或 TOML 配置文件定制安装内容。程序界面支持英文、繁体中文、简体中文、日文和韩文语言。

![显示各项安装决定的菜单](screenshot.png)

![cjktty 控制台显示简体中文、繁体中文、日文和韩文](cjk-console.png)

## 功能

**存储设备** 设备图支持 GPT 和 MBR 分区表，支持 Ext2/3/4、XFS、F2FS、VFAT 以及包含 子卷 的 Btrfs 等文件系统，支持 swap、zram、LUKS2 加密、LVM 和 mdraid。在进行分区管理时可以保留现有分区表，还可以为每个分区分别指定保留、格式化或删除操作。

**引导与系统** 安装程序支持选择 GRUB、systemd-boot 引导程序，其中 GRUB 支持 UEFI 和 BIOS，systemd-boot 支持 UEFI。还可配置 systemd 或 OpenRC、dracut、locale、键盘布局、时区、主机名、DNS、静态地址和所选的网络管理程序。

**桌面与语言支持** 支持选择 GNOME、KDE Plasma 和 Xfce ，并可搭配 GDM、SDDM 或 LightDM。图形设置方面涵盖 AMD、Intel、NVIDIA 和虚拟机。软件包目录包含 Fcitx 5、Rime、Anthy、Mozc、Hangul 和 CJK 字体。gentoo-zh 提供的补丁内核可在 Linux 文本控制台显示中文、日文和韩文。

**Portage** 配置项包括 profile、`MAKEOPTS`、`USE`、`ACCEPT_KEYWORDS`、`L10N`、镜像和仓库同步方式。gentoo-zh 和 gig overlay 必须明确启用。官方与 gentoo-zh 的二进制软件包来源分别使用独立的设置和密钥。

**计划与记录** dry run 和实际安装使用同一份操作序列。日志`install.log` 将记录命令输出，`install.jsonl` 记录操作、软件包来源和二进制软件包降级原因。菜单可将移除敏感信息的配置上传至 `paste.gentoozh.org`，并以文本和 QR 码显示上传页面的网址。

## 验证状态

最近一份有记录的端到端基准覆盖部分 UEFI 和 BIOS 安装、systemd、OpenRC、Ext4、Btrfs、XFS、LUKS2、LVM、mdraid、Plasma 和官方 binhost。每份有效记录都会标明安装程序修订版，并在安装完成的系统成功引导后才列为证据。

当前修订版的基准尚未覆盖 ZFS 与 ZFSBootMenu、initramfs 的 SSH 远程解锁、greetd 桌面会话，以及 GNOME 以外的 ibus。从六种非默认 live 介质安装和 binhost 失败后的降级路径也未纳入。`tests/fixtures/` 下的文件只验证配置模型；文件存在并不表示对应组合已通过端到端验证。

## 要求

实际安装需要 root 权限、amd64 架构和 Python 3.11 或更高版本。使用配置文件执行 dry run 不需要 root 权限。安装程序没有第三方 Python 运行时依赖项。

安装程序启动时需要连接 `packages.gentoo.org`，但 `--missing-commands` 和 `--config FILE --dry-run` 除外。内核版本和 `sys-fs/zfs` 支持的最高内核版本会在运行时读取。

`bootstrap.sh` 会读取 `/etc/os-release`、报告缺少的命令，并显示候选的软件包管理器命令。它可识别多个发行版系列，包括 Debian 和 Ubuntu、Arch、openSUSE、Fedora、RHEL 和 CentOS、Gentoo，以及 Alpine。显示的命令必须在执行前核对。

## 安全事项

实际安装会写入所选磁盘。请注意，使用配置文件运行时不会再次要求确认擦除磁盘；`wipe = true`、删除分区和创建文件系统都可能破坏现有数据。

实际安装前，必须在 dry-run 输出中核对磁盘选择器和每项破坏性操作。稳定的 `/dev/disk/by-id/` 选择器优于 `/dev/sda` 之类的名称；为避免因误操作导致的数据丢失，请务必备份好需要保留的数据。

## 安装

可使用以下命令下载当前的 `master` 归档文件并打开菜单：

```sh
curl -fsSL https://github.com/Zakkaus/gentoo-install/archive/refs/heads/master.tar.gz | tar xz
cd gentoo-install-master
./bootstrap.sh
```

菜单需要至少 80 列、24 行的交互式终端。安装程序启动时会询问一次界面语言；使用`--lang zh-CN` 可直接选择简体中文。

菜单可将配置保存为 `my-install.toml` 配置文件后退出。以下配置文件操作流程会先显示完整的配置计划，再执行实际安装：

```sh
./bootstrap.sh --config my-install.toml --dry-run
./bootstrap.sh --config my-install.toml
./bootstrap.sh --config my-install.toml --no-shell   # 不询问是否打开 root shell
```

交互式安装无论成功或失败，都会在卸载前提供在目标系统内打开 root shell 的选项。可使用`--no-shell` 跳过这项确认。

## 从中断处继续

`--resume` 会跳过日志中标记为完成的操作：

```sh
./bootstrap.sh --config my-install.toml --resume
```

继续执行仅限同一个 Live 会话。默认日志位于 `/run/gentoo-install/install.jsonl`，重新启动后不会保留。日志的每一条记录该操作实现与自身字段的摘要，因此代码或字段更改过的操作会重新执行而不是跳过。日志不记录整份配置的摘要。

## 配置文件

配置文件使用 TOML。顶层 `config_version` 字段指定结构版本。存储设备以设备图表示：每个设备都有 `id`，设备通过其他设备的 `id` 建立引用，选择器仅在实际安装时解析。

[`tests/fixtures/vm-binpkg.toml`](tests/fixtures/vm-binpkg.toml) 是完整的 UEFI 和 Ext4 结构参考。其他 [`tests/fixtures/`](tests/fixtures/) 文件覆盖 BIOS、LUKS2、LVM、mdraid、ZFS、Btrfs 子卷 和桌面。这些文件使用虚拟机磁盘选择器和测试密码，不得原样用于实际机器的安装。

解析与计划阶段不会探测存储硬件，因此没有目标磁盘的机器也能通过 `--dry-run` 检查配置。

## 二进制软件包

二进制软件包属于可选功能。关闭后仍可从源代码构建。官方 binhost 和 gentoo-zh binhost 是两个独立选项，并使用不同的信任配置。当前端到端基准尚未覆盖 binhost 无法连接、缺少签名或密钥不受信任的情况，因此这些降级路径仍列在验证状态中。

## 退出码

`0` 表示完成，`1` 表示配置错误，`2` 表示 preflight 失败，`3` 表示完整性验证失败，`4` 表示外部命令失败，`5` 表示操作者中止。

## 参与开发

[CONTRIBUTING.md](CONTRIBUTING.md) 介绍开发环境、架构和必要检查。
