[English](README.md) | [正體中文](README.zh-TW.md) | 简体中文 | [日本語](README.ja.md) | [한국어](README.ko.md)

# gentoo-install

<!-- fact: identity -->

gentoo-install 在 Linux live 环境中运行，用于安装 amd64 架构的 Gentoo 系统。安装内容由交互式菜单或 TOML 配置文件指定。程序界面提供英文、繁体中文、简体中文、日文和韩文。

![显示各项安装决定的菜单](screenshot-zh-CN.png)

![cjktty 控制台显示简体中文、繁体中文、日文和韩文](cjk-console.png)

## 功能

<!-- fact: capability-scope -->

除非验证状态另行说明更窄的范围，以下路径均已实现，并有自动化单元测试或 plan 测试。

<!-- fact: storage-device-graph -->

**存储设备** 设备图涵盖 GPT 和 MBR 分区表、ext2、ext3、ext4、xfs、f2fs、vfat、包含 subvolume 的 btrfs、swap、LUKS2 加密、LVM 和 mdraid。现有分区表可以保留，每个分区可以分别指定保留、格式化或删除操作。

<!-- fact: zram-system -->

系统配置可以在设备图和 swap 分区之外单独配置 zram。

<!-- fact: in-place-conversion -->

**就地转换。**在 `[disk]` 表设置 `mode = "in-place"` 时，安装程序替换正在运行的发行版的用户空间，而不是分区磁盘。布局由机器读出，因此该表不带设备列表。`/bin`、`/sbin`、`/etc`、`/lib`、`/lib64`、`/usr` 和 `/var` 会被替换；`/home`、`/root`、`/srv`、`/opt` 和其余所有路径保持原样，且 `/etc` 是替换而非合并。

暂存的系统构建在 `/gentoo-install.new`，正在运行的系统在此期间不受影响；随后以 `rename(2)` 逐个目录交换，只有写入 esp 或引导扇区的操作排在交换之后。根位于 LUKS、LVM 或 mdraid 之下、根文件系统为安装程序无法描述的类型、根文件系统可用空间低于 10 GiB，以及在 live 介质上执行，这四种情况都会在写入任何数据之前逐项指名并拒绝。

<!-- fact: boot-system -->

**引导与系统** 引导程序可选择 GRUB 或 systemd-boot，其中 GRUB 支持 UEFI 和 BIOS，systemd-boot 支持 UEFI。还可配置 systemd 或 OpenRC、dracut、locale、键盘布局、时区、主机名、DNS、静态地址和所选的网络管理程序。

<!-- fact: desktop-language -->

**桌面与语言支持** 桌面可以选择 GNOME、KDE Plasma 和 Xfce，并搭配 GDM、SDDM 或 LightDM。图形设置涵盖 AMD、Intel、NVIDIA 和虚拟机。软件包目录包含 Fcitx 5、Rime、Anthy、Mozc、Hangul 和 CJK 字体。内核选项包括 `sys-kernel/gentoo-cjk-kernel-bin` 和 `sys-kernel/gentoo-cjk-kernel`，两者都包含 cjktty 补丁。

<!-- fact: portage -->

**Portage** 配置项包括 profile、`MAKEOPTS`、`USE`、`ACCEPT_KEYWORDS`、`L10N`、镜像和仓库同步方式。gentoo-zh 和 gig overlay 可以分别选择。界面语言选择 `zh-TW`、`zh-CN`、`ja` 或 `ko` 时，也会选择 gentoo-zh 的补丁二进制内核及其 overlay；选择 `en` 时不会自动选择。官方与 gentoo-zh 的二进制软件包来源分别使用独立的设置和密钥。

<!-- fact: proxy -->

**会话代理** `[proxy]` 配置表接受 `kind`、`host`、`port`、可选的 `username` 和 `password`，以及 `bypass`。`kind` 可以是 `http`、`https` 或 `socks5`；`host` 留空表示直接连接，这是默认值。SOCKS5 派生为 `socks5h://`，所以由代理解析主机名以访问内网。界面主菜单为每个值提供字段，并使用菜单选择代理类型。`bypass` 在界面中是逗号分隔的值，在 TOML 中是列表。

选择代理后，配置的代理用于 stage3 及其签名密钥、主仓库与 overlay 版本查询，以及 `gitweb.gentoo.org` 的 ZFS ebuild 查询。它也用于通过 `make.conf` 和 `FETCHCOMMAND`/`RESUMECOMMAND` 的 Portage 下载、`wget`、`curl`、`git`、GnuPG、binhost、overlay 及 paste 上传。时钟、初始连接检查和菜单前的镜像检查都在获得配置前执行，因此不在此设置覆盖范围内。安装程序会将认证信息排除在 dry-run 描述和发布的配置之外；发布的配置与已安装系统都只保留不含认证信息的端点和绕过列表。

<!-- fact: plan-records -->

**计划与记录** dry run 会在不探测存储硬件的情况下显示操作计划。实际安装使用相同的规划器，但会先加入从复用设备探测到的 mdraid 元数据，因此依赖硬件的验证结果可能不同。`install.log` 记录命令输出，`install.jsonl` 记录操作、软件包来源和二进制软件包降级原因。菜单将配置上传至 `paste.gentoozh.org` 前，会把 `password_hash` 和 `root_password_hash` 的值替换为 `removed-before-publishing`，并且完全不写出代理的 `username` 和 `password` 这两个键；其他配置值仍会上传。菜单会以文本和 QR 码显示上传页面的网址。

## 验证状态

<!-- fact: verification-history -->

历史端到端记录使用 amd64 Gentoo minimal ISO，安装程序修订版为 `a71f91b4735469bae8ec76af170201acb967a5fe` 和 `f7257793f95df4b21ebf2ac6a775a343f6205f1b`。这些记录覆盖部分 UEFI 和 BIOS 安装、systemd、OpenRC、ext4、btrfs、xfs、LUKS2、LVM 和 mdraid，也覆盖 Plasma 和官方 binhost。后续安装路径变更使其只能作为历史证据。

<!-- fact: verification-current -->

2026 年 8 月 11 日的端到端记录均标有安装程序修订版，覆盖从 Arch Linux、openSUSE、Debian、Fedora 和自行构建的 gentoo-cjk minimal ISO 各安装并引导一次。这些记录覆盖安装程序修订版 [`b931ef46fc15ed50385f70467f2bfb0a8d1fd154`](https://github.com/Zakkaus/gentoo-install/commit/b931ef46fc15ed50385f70467f2bfb0a8d1fd154)。gentoo-cjk 记录使用 ZFS 和 ZFSBootMenu，其余四条记录使用 ext4。只有在记录的修订版与安装程序匹配、安装退出码为 `0`、已安装系统成功引导，且引导后配置检查通过时，该次运行才算当前证据。

2026 年 8 月 16 日的集群记录标有安装程序修订版 [`a8bf2f3837b6`](https://github.com/Zakkaus/gentoo-install/commit/a8bf2f3837b6)，在 amd64 Gentoo minimal ISO 上覆盖 `vm-luks`、`vm-mdraid`、`vm-xfs`、`vm-btrfs` 和 `vm-f2fs`，五者各自完成安装、引导并通过全部引导后配置检查。`vm-lvm` 在修订版 `073997aa74d2` 完成同一组检查。

就地转换具备单元与计划层测试，尚无端到端记录。其操作顺序、各项拒绝条件与交换本身均有测试覆盖，但尚未有任何一次执行完成转换并引导验证结果。此路径应视为未验证。

其他已实现组合仍未完成端到端验证。当前证据未覆盖 initramfs SSH 解锁、greetd 桌面会话或 GNOME 以外的 ibus。当前证据也未覆盖官方 Gentoo minimal ISO、Alpine 或 Gig-OS live 介质，以及 binhost 失败时的降级。

代理路径已有聚焦单元测试和 plan 测试，覆盖 SOCKS5 DNS 模式、dry-run 输出与发布配置中的认证信息移除，以及已安装系统保留不含认证信息的端点。带版本标记的集群执行已覆盖反向：`vm-proxy-dead` fixture 把代理指向没有进程监听的端口，安装在 stage3 下载阶段以 `Connection refused` 停止，因此执行到达镜像就表示代理被绕过。

版本 `4d8512a496d` 的两次执行覆盖正向：`vm-proxy` 通过要求密码的 SOCKS5 代理完成安装，`vm-proxy-http` 通过 HTTP 代理完成安装，两者都写入 57 个操作，其中 93 个二进制软件包来自二进制主机、14 个由源代码编译。要求密码的 HTTP 或 HTTPS 代理无法检查主树快照的签名，因为 `emerge-webrsync` 只把不含认证信息的端点交给 gemato。dirmngr 完全不支持 SOCKS，因此 SOCKS5 下密钥更新需要直连 keyserver。

CJK 文本控制台显示目前没有验证证据。ext2 和 ext3 也没有针对其配置的自动化测试。`tests/fixtures/` 下的文件只验证配置模型；文件存在并不表示对应组合已完成端到端安装和引导验证。

<!-- fact: verification-network -->

纯 IPv4、纯 IPv6 和双栈的 VM 检查会在访问磁盘前结束。该检查只验证地址族检测、`bootstrap.sh --missing-commands` 和 stage3 pointer 读取，不验证 stage3 下载、仓库同步、binhost 访问、软件包安装或目标系统引导。

## 要求

<!-- fact: requirements-runtime -->

实际安装需要 root 权限、amd64 架构和 Python 3.11 或更高版本。使用配置文件执行 dry run 不需要 root 权限。安装程序没有第三方 Python 运行时依赖项。

<!-- fact: requirements-version-sources -->

菜单从 `packages.gentoo.org` 读取 Gentoo 主仓库的软件包版本，并从 `api.github.com/repos/gentoo-zh/overlay/contents` 读取 gentoo-zh 补丁内核版本。`sys-fs/zfs` 接受的最高内核版本从 `gitweb.gentoo.org` 读取。使用配置文件安装时需要连接的是该配置指定的镜像；`--missing-commands` 和 `--config FILE --dry-run` 都不需要这些版本端点。

<!-- fact: requirements-network-filter -->

live 环境有 IPv6 但没有 IPv4 时，菜单会停用记录中标为仅可通过 IPv4 访问的 Gentoo 镜像。

<!-- fact: requirements-bootstrap -->

`bootstrap.sh` 会读取 `/etc/os-release`、报告缺少的命令，并显示候选的软件包管理器命令。它可识别以下发行版系列：Debian 和 Ubuntu、Arch、openSUSE、Fedora、RHEL 和 CentOS、Gentoo、Alpine。显示的命令必须在执行前核对。

## 安全事项

<!-- fact: safety-destructive -->

实际安装会写入所选磁盘。使用配置文件运行时不会再次要求确认擦除磁盘；`wipe = true`、删除分区和创建文件系统都可能破坏现有数据。

<!-- fact: safety-review-backup -->

实际安装前，必须在 dry-run 输出中核对磁盘选择器和每项破坏性操作。稳定的 `/dev/disk/by-id/` 选择器优于 `/dev/sda` 之类的名称；需要保留的数据必须另有一份独立于所选磁盘的备份。

## 安装

<!-- fact: install-download -->

以下命令下载当前的 `master` 归档文件并打开菜单：

```sh
curl -fsSL https://github.com/Zakkaus/gentoo-install/archive/refs/heads/master.tar.gz | tar xz
cd gentoo-install-master
./bootstrap.sh
```

<!-- fact: install-terminal -->

菜单需要至少 80 列、24 行的交互式终端。安装程序启动时会询问一次界面语言；`--lang zh-CN` 直接选择简体中文。

<!-- fact: install-config-workflow -->

菜单可将配置保存为 `my-install.toml` 后退出。以下流程会先显示完整的配置计划，再执行实际安装：

```sh
./bootstrap.sh --config my-install.toml --dry-run
# 接着择一执行下列其中一行。两者都会写入所选磁盘。
./bootstrap.sh --config my-install.toml
./bootstrap.sh --config my-install.toml --no-shell   # 同一次安装，不询问是否打开 root shell
```

<!-- fact: install-root-shell -->

交互式安装无论成功或失败，都会在卸载前提供在目标系统内打开 root shell 的选项。`--no-shell` 跳过这项确认。

## 从中断处继续

<!-- fact: resume-behavior -->

`--resume` 只会跳过位置和标识均与当前计划匹配，且效果标记为重新启动后仍然存在的已完成操作：

```sh
./bootstrap.sh --config my-install.toml --resume
```

<!-- fact: resume-limits -->

继续执行仅限同一个 live 会话、同一个安装程序修订版与同一份配置文件。默认日志位于 `/run/gentoo-install/install.jsonl`，重新启动后不会保留。每条操作记录都包含根据该操作类源代码和字段值生成的标识；标识变化的操作会重新执行而不是跳过。共用辅助函数或常量的更改不在该标识覆盖范围内，日志也不记录整份配置的摘要，因此不同修订版或配置文件不在续装的文档约定范围内。

## 配置文件

<!-- fact: config-model -->

配置文件使用 TOML。顶层 `config_version` 字段指定结构版本。存储设备以设备图表示：每个设备都有 `id`，设备通过其他设备的 `id` 建立引用，选择器仅在实际安装时解析。

<!-- fact: config-fixtures -->

[`tests/fixtures/vm-binpkg.toml`](tests/fixtures/vm-binpkg.toml) 是完整的 UEFI 和 ext4 结构参考。其他 [`tests/fixtures/`](tests/fixtures/) 文件覆盖 BIOS、LUKS2、LVM、mdraid、ZFS、btrfs subvolume 和桌面。这些文件使用虚拟机磁盘选择器和测试密码，不得原样用于实际机器的安装。

<!-- fact: config-dry-run -->

解析与计划阶段不会探测存储硬件，因此没有目标磁盘的机器也能通过 `--dry-run` 检查配置。

以下完整配置演示包含认证信息和两个绕过主机。认证信息只是示例，执行前必须替换：

```toml
config_version = 1

[proxy]
kind = "socks5"
host = "proxy.example"
port = 1080
username = "operator"
password = "secret"
bypass = ["localhost", "intranet.example"]

[system]
hostname = "proxy-target"
timezone = "UTC"
locales = ["en_US.UTF-8"]
locale = "en_US.UTF-8"
init = "openrc"
root_password_hash = "$6$gentooinst$IR3GrdJ862XljQYDqocr4tKniIRDIT.jQNFzIrHE3U75H6B6YSWZoSYoVd5edSHpqaYBdiNfXHCoIPRVgb9lT/"

[portage]
profile = "default/linux/amd64/23.0"
makeopts = "-j4"

[portage.binhost]
official = false

[bootloader]
firmware = "bios"

[disk]
root = "mnt-root"

[[disk.devices]]
kind = "existing"
id = "disk"
selector = "/dev/disk/by-id/virtio-target0"
wipe = true

[[disk.devices]]
kind = "table"
id = "table"
disk = "disk"
table = "mbr"

[[disk.devices]]
kind = "partition"
id = "rootpart"
table = "table"
index = 1
role = "data"

[[disk.devices]]
kind = "filesystem"
id = "rootfs"
device = "rootpart"
type = "ext4"

[[disk.devices]]
kind = "mountpoint"
id = "mnt-root"
source = "rootfs"
path = "/"
```

## 二进制软件包

<!-- fact: binary-packages -->

二进制软件包属于可选功能。关闭后仍可从源代码构建。官方 binhost 和 gentoo-zh binhost 是两个独立选项，并使用不同的信任配置。目前没有端到端证据覆盖 binhost 无法连接、缺少签名或密钥不受信任的情况；这些降级路径仍未验证。

## 退出码

<!-- fact: exit-codes -->

对于 `gentoo-install`，`0` 表示成功完成，`1` 表示配置错误。`2` 表示 `argparse` 用法错误或 preflight 失败，`3` 表示完整性验证失败。`4` 表示下载、外部命令、操作系统或未分类的安装程序失败，`5` 表示操作者中止。Python CLI 启动前，如果 Python、必要命令或 root 权限检查失败，`bootstrap.sh` 也可能以 `1` 退出。

## 常见问题

<!-- fact: faq-customisation -->

**这种安装器会不会让 Gentoo 失去可定制性？**

不会。它只做基础安装就停手：分区、stage3、Portage 配置、内核、bootloader，以及可选的桌面。之后的每个决定仍然属于操作者，而那台机器是一套普通的 Gentoo，上面不留本项目的任何组件。它省掉的是第一个小时的成本，而那正是 Gentoo 难以入门、也难以在大量机器或 VPS 上部署的原因。

## 参与开发

<!-- fact: contributing -->

[CONTRIBUTING.md](CONTRIBUTING.md) 介绍开发环境、架构和必要检查。

## 许可

<!-- fact: license -->

本项目以 GNU General Public License 发布，版本为第 2 版，或由接收者选择任何更新的版本。第 2 版全文见 [LICENSE](LICENSE)，每份源代码文件带有 `SPDX-License-Identifier: GPL-2.0-or-later`。
