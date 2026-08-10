[English](README.md) | [正體中文](README.zh-TW.md) | 简体中文 | [日本語](README.ja.md) | [한국어](README.ko.md)

# gentoo-install

<!-- fact: identity -->

gentoo-install 是一个系统安装程序，用于在能识别的 Linux live 环境中安装 amd64 架构的 Gentoo 系统。安装内容由交互式菜单或 TOML 配置文件指定。程序界面提供英文、繁体中文、简体中文、日文和韩文。

![显示各项安装决定的菜单](screenshot.png)

![cjktty 控制台显示简体中文、繁体中文、日文和韩文](cjk-console.png)

## 功能

<!-- fact: capability-scope -->

除非验证状态另行说明更窄的范围，以下路径均已实现，并有自动化单元测试或 plan 测试。

<!-- fact: storage-device-graph -->

**存储设备** 设备图涵盖 GPT 和 MBR 分区表、ext2、ext3、ext4、xfs、f2fs、vfat、包含 subvolume 的 btrfs、swap、LUKS2 加密、LVM 和 mdraid。现有分区表可以保留，每个分区可以分别指定保留、格式化或删除操作。

<!-- fact: zram-system -->

系统配置可以在设备图和 swap 分区之外单独配置 zram。

<!-- fact: boot-system -->

**引导与系统** 安装程序支持选择 GRUB、systemd-boot 引导程序，其中 GRUB 支持 UEFI 和 BIOS，systemd-boot 支持 UEFI。还可配置 systemd 或 OpenRC、dracut、locale、键盘布局、时区、主机名、DNS、静态地址和所选的网络管理程序。

<!-- fact: desktop-language -->

**桌面与语言支持** 桌面可以选择 GNOME、KDE Plasma 和 Xfce，并搭配 GDM、SDDM 或 LightDM。图形设置涵盖 AMD、Intel、NVIDIA 和虚拟机。软件包目录包含 Fcitx 5、Rime、Anthy、Mozc、Hangul 和 CJK 字体。gentoo-zh 提供的补丁内核可在 Linux 文本控制台显示中文、日文和韩文。

<!-- fact: portage -->

**Portage** 配置项包括 profile、`MAKEOPTS`、`USE`、`ACCEPT_KEYWORDS`、`L10N`、镜像和仓库同步方式。gentoo-zh 和 gig overlay 可以分别选择。界面语言选择 `zh-TW`、`zh-CN`、`ja` 或 `ko` 时，也会选择 gentoo-zh 的补丁二进制内核及其 overlay；选择 `en` 时不会自动选择。官方与 gentoo-zh 的二进制软件包来源分别使用独立的设置和密钥。

<!-- fact: plan-records -->

**计划与记录** dry run 和实际安装使用同一份操作序列。日志 `install.log` 记录命令输出，`install.jsonl` 记录操作、软件包来源和二进制软件包降级原因。菜单可将移除敏感信息的配置上传至 `paste.gentoozh.org`，并以文本和 QR 码显示上传页面的网址。

## 验证状态

<!-- fact: verification-history -->

历史端到端记录使用 amd64 Gentoo minimal ISO，安装程序修订版为 `a71f91b4735469bae8ec76af170201acb967a5fe` 和 `f7257793f95df4b21ebf2ac6a775a343f6205f1b`。这些记录覆盖部分 UEFI 和 BIOS 安装、systemd、OpenRC、ext4、btrfs、xfs、LUKS2、LVM 和 mdraid，也覆盖 Plasma 和官方 binhost。后续安装路径变更使其只能作为历史证据。

<!-- fact: verification-current -->

目前没有带修订版标记的端到端运行可以验证当前安装程序修订版。因此，上述所有存储、引导、桌面、live 介质和 binhost 组合目前都尚未完成端到端验证。ext2 和 ext3 也没有针对其配置的自动化测试。`tests/fixtures/` 下的文件只验证配置模型；文件存在并不表示对应组合已完成端到端安装和引导验证。

<!-- fact: verification-network -->

纯 IPv4、纯 IPv6 和双栈的 VM 检查会在访问磁盘前结束。该检查只验证地址族检测、`bootstrap.sh --missing-commands` 和 stage3 pointer 读取，不验证 stage3 下载、仓库同步、binhost 访问、软件包安装或目标系统引导。

## 要求

<!-- fact: requirements-runtime -->

实际安装需要 root 权限、amd64 架构和 Python 3.11 或更高版本。使用配置文件执行 dry run 不需要 root 权限。安装程序没有第三方 Python 运行时依赖项。

<!-- fact: requirements-version-sources -->

菜单从 `packages.gentoo.org` 读取 Gentoo 主仓库的软件包版本，并从 `api.github.com/repos/gentoo-zh/overlay/contents` 读取 gentoo-zh 补丁内核版本。`sys-fs/zfs` 接受的最高内核版本从 `gitweb.gentoo.org` 读取。使用配置文件安装时需要连接的是该配置指定的镜像；`--missing-commands` 和 `--config FILE --dry-run` 都不需要这些版本端点。

<!-- fact: requirements-network-filter -->

检测到的地址族与镜像声明的 IPv4 或 IPv6 可用性均不匹配时，菜单会拒绝该镜像。

<!-- fact: requirements-bootstrap -->

`bootstrap.sh` 会读取 `/etc/os-release`、报告缺少的命令，并显示候选的软件包管理器命令。它可识别以下发行版系列：Debian 和 Ubuntu、Arch、openSUSE、Fedora、RHEL 和 CentOS、Gentoo、Alpine。显示的命令必须在执行前核对。

## 安全事项

<!-- fact: safety-destructive -->

实际安装会写入所选磁盘。使用配置文件运行时不会再次要求确认擦除磁盘；`wipe = true`、删除分区和创建文件系统都可能破坏现有数据。

<!-- fact: safety-review-backup -->

实际安装前，必须在 dry-run 输出中核对磁盘选择器和每项破坏性操作。稳定的 `/dev/disk/by-id/` 选择器优于 `/dev/sda` 之类的名称；需要保留的数据必须另有一份独立于所选磁盘的备份。

## 安装

<!-- fact: install-download -->

可使用以下命令下载当前的 `master` 归档文件并打开菜单：

```sh
curl -fsSL https://github.com/Zakkaus/gentoo-install/archive/refs/heads/master.tar.gz | tar xz
cd gentoo-install-master
./bootstrap.sh
```

<!-- fact: install-terminal -->

菜单需要至少 80 列、24 行的交互式终端。安装程序启动时会询问一次界面语言；使用 `--lang zh-CN` 可直接选择简体中文。

<!-- fact: install-config-workflow -->

菜单可将配置保存为 `my-install.toml` 后退出。以下配置文件操作流程会先显示完整的配置计划，再执行实际安装：

```sh
./bootstrap.sh --config my-install.toml --dry-run
# 接着择一执行下列其中一行。两者都会写入所选磁盘。
./bootstrap.sh --config my-install.toml
./bootstrap.sh --config my-install.toml --no-shell   # 同一次安装，不询问是否打开 root shell
```

<!-- fact: install-root-shell -->

交互式安装无论成功或失败，都会在卸载前提供在目标系统内打开 root shell 的选项。使用 `--no-shell` 可以跳过这项确认。

## 从中断处继续

<!-- fact: resume-behavior -->

`--resume` 会跳过日志中标记为完成的操作：

```sh
./bootstrap.sh --config my-install.toml --resume
```

<!-- fact: resume-limits -->

继续执行仅限同一个 live 会话、同一个安装程序修订版与同一份配置文件。默认日志位于 `/run/gentoo-install/install.jsonl`，重新启动后不会保留。日志的每一条记录包含该操作类的源代码与该操作自身字段的摘要，因此类或字段更改过的操作会重新执行而不是跳过。共用辅助函数或常量的更改不在该摘要覆盖范围内，日志也不记录整份配置的摘要，因此跨修订版或跨配置文件的续装不受支持。

## 配置文件

<!-- fact: config-model -->

配置文件使用 TOML。顶层 `config_version` 字段指定结构版本。存储设备以设备图表示：每个设备都有 `id`，设备通过其他设备的 `id` 建立引用，选择器仅在实际安装时解析。

<!-- fact: config-fixtures -->

[`tests/fixtures/vm-binpkg.toml`](tests/fixtures/vm-binpkg.toml) 是完整的 UEFI 和 ext4 结构参考。其他 [`tests/fixtures/`](tests/fixtures/) 文件覆盖 BIOS、LUKS2、LVM、mdraid、ZFS、btrfs subvolume 和桌面。这些文件使用虚拟机磁盘选择器和测试密码，不得原样用于实际机器的安装。

<!-- fact: config-dry-run -->

解析与计划阶段不会探测存储硬件，因此没有目标磁盘的机器也能通过 `--dry-run` 检查配置。

## 二进制软件包

<!-- fact: binary-packages -->

二进制软件包属于可选功能。关闭后仍可从源代码构建。官方 binhost 和 gentoo-zh binhost 是两个独立选项，并使用不同的信任配置。目前没有端到端证据覆盖 binhost 无法连接、缺少签名或密钥不受信任的情况，因此这些降级路径仍列在验证状态中。

## 退出码

<!-- fact: exit-codes -->

Python CLI 成功解析参数后，以下退出码对照才适用。`0` 表示完成，`1` 表示配置错误，`2` 表示 preflight 失败。`3` 表示完整性验证失败，`4` 表示外部命令失败，`5` 表示操作者中止。在此之前，`argparse` 以 `2` 表示无效参数。`bootstrap.sh` 则以 `1` 表示 Python 版本不足、缺少命令或权限不足等启动程序失败。

## 参与开发

<!-- fact: contributing -->

[CONTRIBUTING.md](CONTRIBUTING.md) 介绍开发环境、架构和必要检查。
