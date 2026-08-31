[English](README.md) | [正體中文](README.zh-TW.md) | 简体中文 | [日本語](README.ja.md) | [한국어](README.ko.md)

# gentoo-install

<!-- fact: identity -->

gentoo-install 在 Linux live 环境中运行，用于安装 amd64 架构的 Gentoo 系统。安装内容由交互式菜单或 TOML 配置文件指定。程序界面提供英文、繁体中文、简体中文、日文和韩文。

![显示各项安装决定的菜单](screenshot-zh-CN.png)

## 功能概要

<!-- fact: capability-summary -->

实现范围包括常规磁盘安装、存储与引导配置、桌面与语言 profile，以及特殊模式。

- **存储**。设备图涵盖分区表、文件系统、LUKS2、LVM、mdraid 和 ZFS。
- **引导与系统**。GRUB、systemd-boot 和 ZFSBootMenu 按配置提供 UEFI 或 BIOS 引导。
- **桌面与语言**。GNOME、KDE Plasma、Xfce、CJK 字体和输入法都是配置选项。
- **特殊模式**。内存环境、原地转换、稀疏镜像和 `dd` 各有独立限制。

[参考资料](REFERENCE.md#capabilities)说明模型、限制和特殊模式流程。

## 验证状态

<!-- fact: verification-scope -->

[`TESTED.md`](TESTED.md)记录每条已执行路径、安装程序修订版和执行环境。一次运行只有在记录的修订版符合安装程序、安装以 `0` 结束、已安装系统可引导，且引导后配置检查通过时才计入。

单元、计划和 fixture coverage 说明实现行为，不能证明已安装系统已经引导。[`tests/fixtures/`](tests/fixtures/)验证配置模型，不是已安装的机器。记录会列出尚未验证的组合。

<!-- fact: verification-architecture -->

`gentoo_install/model/architecture.py` 带有 amd64、arm64 和 x86 三列，GRUB 目标、`CPU_FLAGS_*` 变量、二进制软件包主机的子目录和 EFI 可执行文件名称都由那一列组成。已验证的只有 amd64：[`TESTED.md`](TESTED.md)没有任何 arm64 或 x86 的记录，`tests/vm/` 也只运行 amd64。

## 要求

<!-- fact: requirements-runtime -->

实际安装需要 root 权限、amd64 目标和 Python 3.11 或更高版本。使用配置文件执行 dry run 不需要 root 权限。安装程序没有第三方 Python 运行时依赖项。

## 安全事项

<!-- fact: safety-destructive -->

实际运行会写入所选磁盘。配置文件运行没有第二次擦除确认；`wipe = true`、删除分区和创建文件系统都可能破坏现有数据。

<!-- fact: safety-review-backup -->

实际运行前，必须在 dry-run 输出中核对磁盘选择器和每项破坏性操作。稳定的 `/dev/disk/by-id/` 选择器优于 `/dev/sda` 之类的名称；需要保留的数据必须在这次运行不会写入的磁盘上另有备份。

## 安装

<!-- fact: install-download -->

以下命令下载当前的 `master` 归档文件并打开菜单：

```sh
curl -fsSL https://github.com/Zakkaus/gentoo-install/archive/refs/heads/master.tar.gz | tar xz
cd gentoo-install-master
./bootstrap.sh
```

<!-- fact: install-terminal -->

菜单需要至少 80 列、24 行的交互式终端。

<!-- fact: install-config-workflow -->

菜单会将配置保存为 `my-install.toml`。配置文件流程依次为保存、dry run、检查、安装：

```sh
./bootstrap.sh --config my-install.toml --dry-run
# 选择实际命令前检查呈现的计划。
./bootstrap.sh --config my-install.toml
./bootstrap.sh --config my-install.toml --no-shell   # 同一次运行，不询问 root shell
```

<!-- fact: install-root-shell -->

交互式运行会在卸载前提供目标 root shell。`--no-shell` 会跳过该提示。

## 配置文件

<!-- fact: configuration-reference -->

配置文件使用 TOML，`config_version` 选择 schema 版本。[配置参考资料](REFERENCE.md#configuration-files)列出所有持久化键和通过验证的示例。[`tests/fixtures/vm-binpkg.toml`](tests/fixtures/vm-binpkg.toml)是 schema reference；其中的虚拟机磁盘选择器和测试凭证不得原样用于实际机器。

## 从中断处继续

<!-- fact: resume-limits -->

`--resume` 仅限同一个 live 会话、同一安装程序修订版和同一份配置文件。安装程序会拒绝不匹配的情况。默认日志位于 `/run/gentoo-install/install.jsonl`，因此不会在重新启动后保留。

```sh
./bootstrap.sh --config my-install.toml --resume
```

## 二进制软件包

<!-- fact: binary-packages -->

二进制软件包是可选项。停用后仍可从源代码构建。官方 binhost 和 gentoo-zh binhost 有各自的信任配置。无法连接的 binhost、缺少签名和不受信任的密钥目前都没有端到端证据；这些降级路径仍未验证。

## 参考资料

<!-- fact: reference -->

[REFERENCE.md](REFERENCE.md)收录运行时要求、命令行选项、内存环境、原地转换、功能和验证细节、配置文件、二进制软件包信任和退出码。

## 参与开发

<!-- fact: contributing -->

[CONTRIBUTING.md](CONTRIBUTING.md)说明开发环境、架构和必要检查。

## 许可

<!-- fact: license -->

gentoo-install 以 GNU General Public License 发布，版本为第 2 版，或由接收者选择任何更新的版本。第 2 版全文见 [LICENSE](LICENSE)，每份源代码文件带有 `SPDX-License-Identifier: GPL-2.0-or-later`。
