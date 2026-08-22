[English](README.md) | [正體中文](README.zh-TW.md) | 简体中文 | [日本語](README.ja.md) | [한국어](README.ko.md)

# gentoo-install

<!-- fact: identity -->

gentoo-install 在 Linux live 环境中运行，用于安装 amd64 架构的 Gentoo 系统。安装内容由交互式菜单或 TOML 配置文件指定。程序界面提供英文、繁体中文、简体中文、日文和韩文。

![显示各项安装决定的菜单](screenshot-zh-CN.png)

![cjktty 控制台显示简体中文、繁体中文、日文和韩文](cjk-console.png)

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

| 选项 | 参数与默认值 | 效果 |
| --- | --- | --- |
| `--config` | 文件或 URL；未设置 | 加载该来源，而不是打开菜单。 |
| `--dry-run` | 无；`false` | 显示推导出的操作和摘要，然后在不应用操作的情况下退出。 |
| `--mirror` | stage3 镜像字符串；安装程序默认值 | 为普通安装选择 stage3 来源。内存环境的预约从配置推导其区域。 |
| `--lang` | 语言标签；`""` | 菜单创建配置时覆盖 `LC_ALL`、`LC_MESSAGES` 和 `LANG`。 |
| `--target` | 路径；`/mnt/gentoo` | 选择普通安装的挂载目标。转换和内存环境的预约使用 `/`。 |
| `--work` | 路径；`/run/gentoo-install` | 保存运行状态，包括报告和日志。 |
| `--missing-commands` | 无；`false` | 每行显示一个缺少的主机命令，然后退出。 |
| `--resume` | 无；`false` | 使用现有日志跳过兼容的已完成操作。 |
| `--no-shell` | 无；`false` | 不提供目标 root shell。它还会使内存环境的预约无需值守。 |
| `--skip-preflight` | 无；`false` | 跳过普通安装的预检。 |
| `--ram` | 无；未设置内存模式 | 预约一次进入保存在内存中的 Gentoo CJK ISO 的启动。它与 `--lowram` 冲突。 |
| `--lowram` | 无；未设置内存模式 | 预约一次进入保存在内存中的 Alpine netboot 环境的启动。它与 `--ram` 冲突。 |
| `--ssh-key` | 密钥、文件、HTTP(S) URL 或 `github:`/`gitlab:` 引用；`""` | 设置内存环境的授权公钥。它需要内存模式。 |
| `--ssh-port` | 整数；未设置 | 设置内存环境的 `sshd` 端口；设置内存模式时可用。 |
| `--root-password` | 字符串；`""` | 设置内存环境的 root 密码；仅支持内存模式。 |
| `--bypass` | 无；`false` | 以替换默认引导项的方式预约，而不是建立一次性引导项；只适用于内存模式。 |
| `--disarm` | 无；`false` | 在加载配置前移除先前预约的内存启动及其放置的文件。 |

## 从内存安装

<!-- fact: install-memory -->

`--ram` 与 `--lowram` 预约一次进入内存中活环境的引导，那是一台没有控制台、也没有救援镜像的租用机器覆盖自己磁盘之前所需要的。安装程序、选定的配置与授权密钥都随 initramfs 送达，因此环境起来时运行的正是完成这次预约的那一版：

```sh
./bootstrap.sh --ram --ssh-key github:zakkaus --root-password 'replace this'
reboot
ssh root@the-machine
```

默认引导项不会改动，所以没有进入该环境的机器仍引导回原本的系统；`--disarm` 取消这次预约。`--bypass` 改为取代默认项，供会丢弃一次性引导项的固件使用，那也是唯一一条「环境起不来就连机器都引导不了」的路径。

第一个界面提供安装与救援 shell，没有超时，未回答之前不会擦除任何数据。`--ram` 引导的是带 ZFS 的 Gentoo CJK ISO，约需 2 GiB 内存；`--lowram` 引导的是较小、没有 `zfs.ko` 的 Alpine netboot 压缩包。`--ssh-port` 把服务移离 22 端口。第一页写出稍后启动安装的命令，所以回答 `no`、或在回答之前断线，都不会让重启成为唯一的退路。

`--ram` 连得上 Wi-Fi，`--lowram` 连不上。Gentoo CJK ISO 带着 NetworkManager 与 `linux-firmware`，`nmcli device wifi connect <SSID> password <密码>` 就能接起连接，接好再执行安装；第一页以正体中文、简体中文与英文写出这件事。Alpine netboot 环境没有无线驱动也没有 supplicant，完整模块在一份本身也要下载的 `modloop` 里，因此只有 Wi-Fi 一条连接的机器完全无法使用 `--lowram`。

## 转换运行中的系统

<!-- fact: install-in-place -->

在 `[disk]` 表设 `mode = "in-place"`，安装程序取代运行中发行版的用户空间，而不是分区磁盘。该表不带设备列表，因为布局由机器读出：

```toml
config_version = 1

[system]
hostname = "converted"
timezone = "UTC"
locales = ["en_US.UTF-8"]
locale = "en_US.UTF-8"
init = "systemd"
root_password_hash = "$6$gentooinst$IR3GrdJ862XljQYDqocr4tKniIRDIT.jQNFzIrHE3U75H6B6YSWZoSYoVd5edSHpqaYBdiNfXHCoIPRVgb9lT/"

[portage]
profile = "default/linux/amd64/23.0/systemd"
makeopts = "-j4"

[bootloader]
kind = "grub"
firmware = "uefi"

[disk]
mode = "in-place"
```

上面那个哈希是示例，执行前必须替换。交互式执行会打印这次转换取代哪些目录，并要求输入 `convert` 才会写入任何数据；没有终端的执行不会被询问，因为配置文件里的 `mode = "in-place"` 就是授权，而在那里发问会让串口控制台永远等下去。

**发起这次执行的会话必须保持连接。**`/usr` 与 `/etc` 换成新系统之后，新的 SSH 登录不再成立，而发起执行的会话仍持有它已经映射的可执行文件。

## 从中断处继续

<!-- fact: resume-behavior -->

`--resume` 只会跳过位置和标识均与当前计划匹配，且效果标记为重新启动后仍然存在的已完成操作：

```sh
./bootstrap.sh --config my-install.toml --resume
```

<!-- fact: resume-limits -->

继续执行仅限同一个 live 会话、同一个安装程序与同一份配置文件，而且安装程序会拒绝其他情况，不再只是写在文档里。

- 日志开头记录配置的摘要、机器的 boot id 与安装程序源代码的摘要；`--resume` 三者都比对，任何一项不同就停下并说明原因。内核不提供 boot id 的机器只比对其余两项。
- 默认日志位于 `/run/gentoo-install/install.jsonl`，本来就不会在重新启动后保留。
- 每条操作记录另外包含根据该操作类源代码和字段值生成的标识，标识变化的操作会重新执行而不是跳过。共用辅助函数或常量的更改不在该标识范围内，改由安装程序摘要覆盖。

## 功能

<!-- fact: capability-scope -->

除非验证状态另行说明更窄的范围，以下路径均已实现，并有自动化单元测试或 plan 测试。

<!-- fact: storage-device-graph -->

**存储设备**
- 设备图涵盖 GPT 和 MBR 分区表、ext2、ext3、ext4、xfs、f2fs、vfat、包含 subvolume 的 btrfs、swap、LUKS2 加密、LVM 和 mdraid。
- ZFS 属于同一张设备图：pool 在其 vdev 之上采用 stripe、mirror 或 raidz1、raidz2、raidz3，原生加密是 pool 的属性，每个 dataset 各为一个节点。
- 现有分区表可以保留，每个分区可以分别指定保留、格式化或删除操作。

| 模型节点 | `id` 之外的字段 | 结果 |
| --- | --- | --- |
| `Existing` | `selector`, `wipe` | 选择预先存在的设备。 |
| `PartitionTable` | `disk`, `table`, `create`, `remove` | 创建或编辑分区表。 |
| `Partition` | `table`, `index`, `role`, `size`, `label` | 定义分区；最后一个 `size` 可占用剩余空间。 |
| `Luks` | `backing`, `name`, `passphrase_file` | 定义 LUKS 容器。 |
| `MdRaid` | `members`, `level`, `name`, `metadata` | 定义 mdraid 阵列。 |
| `VolumeGroup` | `members`, `name` | 定义 LVM 卷组。 |
| `LogicalVolume` | `group`, `name`, `size` | 定义 LVM 逻辑卷。 |
| `ZfsPool` | `vdevs`, `name`, `topology`, `encrypted`, `passphrase_file` | 定义 ZFS pool。 |
| `ZfsDataset` | `pool`, `name` | 定义 ZFS dataset。 |
| `Filesystem` | `device`, `kind`, `label`, `create` | 格式化设备；`create = false` 时验证设备。 |
| `Subvolume` | `filesystem`, `name` | 定义 Btrfs subvolume。 |
| `Swap` | `device` | 在引用的设备上定义 swap。 |
| `Mountpoint` | `source`, `path`, `options` | 挂载文件系统、Btrfs subvolume 或 ZFS dataset。 |

| 选项 | 值 |
| --- | --- |
| 分区表 | `gpt`, `mbr` |
| 分区角色 | `esp`, `bios-boot`, `swap`, `raid`, `lvm`, `zfs`, `data` |
| mdraid 级别 | `raid0`, `raid1`, `raid5`, `raid6` |
| mdraid 元数据 | `0.90`, `1.0`, `1.1`, `1.2` |
| 文件系统 | `ext2`, `ext3`, `ext4`, `btrfs`, `xfs`, `f2fs`, `vfat` |
| ZFS 拓扑 | `stripe`, `mirror`, `raidz1`, `raidz2`, `raidz3` |

<!-- fact: zram-system -->

系统配置可以在设备图和 swap 分区之外单独配置 zram。

<!-- fact: in-place-conversion -->

**就地转换。**
- 在 `[disk]` 表设置 `mode = "in-place"` 时，安装程序替换正在运行的发行版的用户空间，而不是分区磁盘。
- 布局由机器读出，因此该表不带设备列表。
- `/bin`、`/sbin`、`/etc`、`/lib`、`/lib64`、`/usr` 和 `/var` 会被替换；`/home`、`/root`、`/srv`、`/opt` 和其余所有路径保持原样，且 `/etc` 是替换而非合并。

- 暂存的系统构建在 `/gentoo-install.new`，正在运行的系统在此期间不受影响；随后以 `rename(2)` 逐个目录交换，只有写入 esp 或引导扇区的操作排在交换之后。
- 根位于 LUKS、LVM 或 mdraid 之下、根文件系统为安装程序无法描述的类型、根文件系统可用空间低于 10 GiB，以及在 live 介质上执行，这四种情况都会在写入任何数据之前逐项指名并拒绝。

<!-- fact: prepared-image -->

**磁盘镜像**
- `mode = "image"` 把系统装进 `disk.image` 指定、`disk.size` 决定大小的稀疏文件，而不是装到磁盘上，产物因此是一份可以复制到别处、之后再写入的文件。
- `mode = "dd"` 不执行安装：它把 `disk.source` 的镜像流式写入 `disk.destination` 这整块磁盘，读取时解开 `raw`、`gz`、`xz`、`zst` 或 `tar`，并保留该镜像原本带的布局与引导程序。
- 两种模式互不接受对方的键，`partition` 模式两组都不接受。

<!-- fact: boot-system -->

**引导与系统**
- 引导程序可选择 GRUB、systemd-boot 或 ZFSBootMenu。GRUB 支持 UEFI 和 BIOS，systemd-boot 支持 UEFI。
- ZFSBootMenu 在 UEFI 上引导 ZFS 根，内核取自 pool 内引导环境自己的 `/boot`。还可配置 systemd 或 OpenRC、dracut、locale、键盘布局、时区、主机名、DNS、静态地址和所选的网络管理程序。

<!-- fact: remote-unlock -->

**以 ssh 解开加密的根**
- `[kernel.remote_unlock]` 在引导路径放进一个 ssh 服务，供无人在旁边回答密语提示的机器使用。
- `enabled` 开启这条路径；`port` 默认 222 而不是 22，避免客户端对运行中系统的 `known_hosts` 记录与 initramfs 的那条相撞；`address`、`gateway` 和 `interface` 给该服务一个静态地址，地址留空则改用 DHCP。
- LUKS 根由系统 initramfs 里的 `sys-kernel/dracut-crypt-ssh` 打开，ZFS 根则由 ZFSBootMenu 构建进自己镜像的 dropbear 打开。
- 授权密钥取自 `system.authorized_keys`：开了这条路径又没有列出任何密钥的配置会被逐项指名并拒绝，因为那描述的是一个没有人登录得进去的服务。

<!-- fact: desktop-language -->

**桌面与语言支持**
- 桌面可以选择 GNOME、KDE Plasma 和 Xfce，并搭配 GDM、SDDM、LightDM，或 greetd 和它的 tuigreet 控制台登录界面。
- 图形设置涵盖 AMD、Intel、NVIDIA 和虚拟机。
- 软件包目录包含 Fcitx 5、Rime、Anthy、Mozc、Hangul 和 CJK 字体。
- 内核选项包括 `sys-kernel/gentoo-cjk-kernel-bin` 和 `sys-kernel/gentoo-cjk-kernel`，两者都包含 cjktty 补丁。

<!-- fact: portage -->

**Portage**
- 配置项包括 profile、`MAKEOPTS`、`USE`、`ACCEPT_KEYWORDS`、`L10N`、镜像和仓库同步方式。
- gentoo-zh 和 gig overlay 可以分别选择。
- 界面语言选择 `zh-TW`、`zh-CN`、`ja` 或 `ko` 时，也会选择 gentoo-zh 的补丁二进制内核及其 overlay；选择 `en` 时不会自动选择。
- 官方与 gentoo-zh 的二进制软件包来源分别使用独立的设置和密钥。

<!-- fact: proxy -->

**会话代理**
- `[proxy]` 配置表接受 `kind`、`host`、`port`、可选的 `username` 和 `password`，以及 `bypass`。
- `kind` 可以是 `http`、`https` 或 `socks5`；`host` 留空表示直接连接，这是默认值。
- SOCKS5 派生为 `socks5h://`，所以由代理解析主机名以访问内网。
- 界面主菜单为每个值提供字段，并使用菜单选择代理类型。
- `bypass` 在界面中是逗号分隔的值，在 TOML 中是列表。

- 选择代理后，配置的代理用于 stage3 及其签名密钥、主仓库与 overlay 版本查询，以及 `gitweb.gentoo.org` 的 ZFS ebuild 查询。
- 它也用于通过 `make.conf` 和 `FETCHCOMMAND`/`RESUMECOMMAND` 的 Portage 下载、`wget`、`curl`、`git`、GnuPG、binhost、overlay 及 paste 上传。
- 时钟、初始连接检查和菜单前的镜像检查都在获得配置前执行，因此不在此设置覆盖范围内。安装程序会将认证信息排除在 dry-run 描述和发布的配置之外；发布的配置与已安装系统都只保留不含认证信息的端点和绕过列表。

<!-- fact: memory-environment -->

**内存环境。**
- `--ram` 与 `--lowram` 预约一次开机进入常驻内存的活环境，接着询问是否重新启动；这条路没有界面，因为它针对的机器通常只有一条 SSH 连接而没有控制台。
- `--ram` 用 Gentoo CJK ISO，带有 ZFS，需要约 2 GiB 内存：它的 initramfs 在内存减去 824 MiB 的活镜像后低于 1 GiB 时停在急救 shell。
- `--lowram` 用 Alpine netboot 套件，较小且没有 `zfs.ko`。
- 两者都不写死版本：发布方各自列出当前的镜像与校验和，预约之前先抓取并验证。

- 默认启动项一律不改，所以环境没有起来时机器仍然开得了机。
- `--bypass` 改为替换它，用于会丢弃一次性启动项的固件；这是唯一一条环境没起来就完全开不了机的路径，没有任何路径会自动选它。

<!-- fact: memory-environment-access -->

**用 SSH 观察内存安装。**
- `--ssh-key` 接受公钥本文（`ssh-ed25519`、`ssh-rsa`、`ecdsa-sha2-nistp256`、`-384`、`-521` 以及 `sk-` 变体）、路径、`http` 或 `https` 网址，以及 `github:user` 与 `gitlab:user`；`--ssh-port` 与 `--root-password` 设定其余部分。
- 安装器、选定的配置与密钥都放在 initramfs 内，所以环境运行的是写出该配置的修订，而且在第一次登录之前 `authorized_keys` 已经就位。
- 操作者以 SSH 重新连接观察安装，不必一直开着控制台。
- 回答第一个画面之前不会清除任何数据：该画面提供安装与急救 shell 两项，而且没有超时。

<!-- fact: plan-records -->

**计划与记录**
- dry run 会在不探测存储硬件的情况下显示操作计划。
- 实际安装使用相同的规划器，但会先加入从复用设备探测到的 mdraid 元数据，因此依赖硬件的验证结果可能不同。
- `install.log` 记录命令输出，`install.jsonl` 记录操作、软件包来源和二进制软件包降级原因。
- 菜单将配置上传至 `paste.gentoozh.org` 前，会把 `password_hash` 和 `root_password_hash` 的值替换为 `removed-before-publishing`，并且完全不写出代理的 `username` 和 `password` 这两个键；其他配置值仍会上传。
- 菜单会以文本和 QR 码显示上传页面的网址。

### 兼容性规则

验证拒绝下列组合：

| 被拒绝的组合 |
| --- |
| 没有可使用密码认证的用户或可用 SSH 密钥登录时，空的、锁定的或格式错误的 root hash。 |
| ZFS 根与 GRUB。 |
| ZFS 上的 `/boot` 与 GRUB。 |
| ZFS 根与 BIOS 引导。 |
| ZFS 根与 LUKS 根链。 |
| ZFSBootMenu 与非 ZFS 根。 |
| 未挂载 ESP 的 UEFI 引导。 |
| 使用加密 ESP 的 UEFI 引导。 |
| systemd-boot 与 BIOS 引导。 |
| systemd-boot 与因 `/boot` 已加密或不是 vfat 而无法从 ESP 访问的 kernel 或 initramfs。 |
| 带有元数据 `1.1` 或 `1.2` 的 mdraid 上的 ESP。 |
| 没有 `bios-boot` 分区的 GPT 磁盘上的 BIOS 引导。 |
| 使用缺少 cjktty 的 kernel 进行 CJK 控制台渲染。 |
| 没有授权 SSH 密钥的远程解锁。 |
| 没有加密根容器或 pool 的远程解锁。 |
| 使用必须在 initramfs SSH 启动前解锁 `/boot` 的 GRUB 的远程解锁。 |
| 由 GRUB 或 systemd-boot 系统 initramfs 解锁的原生 ZFS 加密远程解锁。 |
| 没有 `gentoo-zh` overlay 的 CJK kernel。 |
| 使用非 `8x16` 控制台字体的 CJK 控制台渲染。 |
| 没有 `gentoo-zh` overlay 的 ZFSBootMenu。 |
| 没有 `gentoo-zh` overlay 的 gentoo-zh 社区 binhost。 |

## 验证状态

<!-- fact: verification-scope -->

[`TESTED.md`](TESTED.md) 是验证记录：每一条走过的路径各一行，写明它运行时的安装器修订版与运行的地点。一次运行要记录的修订版与安装器相符、安装退出码为 `0`、装出的系统能引导、且引导后的配置检查全部通过，才算数。

| 路径 | 记录 |
| --- | --- |
| 装到磁盘上 | ext4、ext2、ext3、xfs、btrfs、f2fs、ZFS、LVM、mdraid 与 LUKS2，两种固件与两种 init 系统皆有 |
| ZFS pool | stripe、mirror、raidz、加密 pool，以及一个由 ZFSBootMenu 引导的 pool |
| 通过 ssh 解锁 | 由系统 initramfs 打开的 LUKS 根，以及由 ZFSBootMenu 自己镜像打开的 ZFS pool |
| 静态地址与 greetd | 各自有集群记录 |
| 就地转换运行中的系统 | 四条 QEMU 记录：两条 BIOS，一条保留 `/home` 的 UEFI，一条根文件系统为 btrfs、`/home` 与 `/var` 是 subvolume 的 UEFI |
| 转换这个安装程序装出来的机器 | 六次走到重新启动的集群转换里，五次启动成功，一次因为缺少模块停在 GRUB 的救援 shell，而转换本身的退出码是 `0`，所以这条路径还不可靠 |
| `--ram` 与 `--lowram` | 一台 Debian 12 机器预约一次启动、默认启动项未变，重新启动后带着交付给它的配置进入送达的环境；在那里回答 `install` 会装出 Gentoo 并引导它写出的磁盘 |
| `--bypass` | 替换默认启动项挺过了一次电源循环：两次启动都进入送达的环境 |
| 预约的启动没有起来 | 一台被移除预约项 initramfs 的机器，没有出现送达的画面，接下来两次启动都进入原本的云系统 |
| `dd` | 一条记录：从 live 介质把准备好的镜像写入整块磁盘并逐字节读回，原始和 gzip 两种格式皆是 |
| 装进文件 | 一条记录：镜像以 `losetup -Pf` 挂上，读回的是它布局声明的那两个文件系统，而没有任何机器从那份文件引导过 |
| 菜单 | 在 80x24 的串行控制台上逐行打开过，覆盖英文、繁体中文、简体中文、日文与韩文，没有一行宽过终端 |

源代码构建的内核和二进制软件包降级只有 runner 层级的测试，而 runner 层级的测试不是端到端记录。`tests/fixtures/` 下的文件验证的是配置模型，它们存在并不代表任何一台装出来的机器。

## 配置文件

<!-- fact: config-model -->

配置文件使用 TOML。顶层 `config_version` 字段指定结构版本。存储设备以设备图表示：每个设备都有 `id`，设备通过其他设备的 `id` 建立引用，选择器仅在实际安装时解析。

<!-- fact: config-fixtures -->

[`tests/fixtures/vm-binpkg.toml`](tests/fixtures/vm-binpkg.toml) 是完整的 UEFI 和 ext4 结构参考。其他 [`tests/fixtures/`](tests/fixtures/) 文件覆盖 BIOS、LUKS2、LVM、mdraid、ZFS、btrfs subvolume 和桌面。这些文件使用虚拟机磁盘选择器和测试密码，不得原样用于实际机器的安装。

<!-- fact: config-dry-run -->

解析与计划阶段不会探测存储硬件，因此没有目标磁盘的机器也能通过 `--dry-run` 检查配置。

以下参考列出每个持久化键。表路径采用 TOML 表记法，`[[disk.devices]]` 包含图节点。

| 键 | 含义和默认值或选项 |
| --- | --- |
| `config_version` | 持久化的结构版本；`1`。 |
| `proxy.kind` | 代理协议：`http`、`https` 或 `socks5`；`http`。 |
| `proxy.host` | 代理主机；`""` 禁用代理。 |
| `proxy.port` | 代理端口；`0`。 |
| `proxy.username` | 可选代理用户名；`""`。 |
| `proxy.password` | 可选代理密码；`""`。 |
| `proxy.bypass` | 绕过代理的主机；`[]`。 |

| `system` 键 | 含义和默认值或选项 |
| --- | --- |
| `hostname` | 目标主机名；`gentoo`。 |
| `timezone` | 目标时区；`Asia/Shanghai`。 |
| `locales` | 生成的 locale；`en_US.UTF-8`、`zh_CN.UTF-8`、`zh_TW.UTF-8`。 |
| `locale` | 选定的 locale；`zh_CN.UTF-8`。 |
| `keymap` | 已安装系统的键盘映射；`us`。 |
| `keymap_initramfs` | initramfs 键盘映射；`""` 表示跟随 `keymap`。 |
| `interface` | 网络接口模式；`""` 匹配 `en*` 和 `eth*`。 |
| `addresses` | 静态 CIDR 地址；`[]` 选择 DHCP 或路由器通告。 |
| `gateways` | 网关，每个地址族最多一个；`[]`。 |
| `dns` | 解析器地址；`[]`。 |
| `authorized_keys` | root 和 sudo 用户的密钥；`[]`。 |
| `console_cjk` | 要求 CJK 控制台渲染；`false`；需要 cjktty。 |
| `console_font` | 控制台单元格大小：`8x8`、`8x16` 或 `16x32`；`8x16`。 |
| `init` | init 系统：`openrc` 或 `systemd`；`systemd`。 |
| `zram` | 压缩 RAM swap 大小；未设置则禁用。 |
| `hardware_clock_utc` | RTC 存储 UTC；`true`。 |
| `users` | 用户记录；`[]`。 |
| `root_password_hash` | root `crypt(3)` 哈希；`""` 锁定 root。 |
| `logger` | 日志程序：`none`、`sysklogd`、`syslog-ng` 或 `metalog`；`sysklogd`。 |
| `cron` | 安装 `sys-process/cronie`；`true`。 |
| `sshd` | 安装并配置 SSH 守护进程支持；`false`。 |
| `sshd_password_login` | SSH 守护进程接受密码；`false`。 |
| `sshd_root_login` | root 可通过 SSH 登录；`false`。 |
| `networking` | 链路管理：`builtin`、`networkmanager-wpa`、`networkmanager-iwd` 或 `none`；`builtin`。 |
| `firewall` | 数据包过滤软件包：`none`、`nftables` 或 `iptables`；`none`。 |
| `first_boot` | 首次启动记录；默认为空记录。 |

| `system.users` 项 | 含义和默认值 |
| --- | --- |
| `name` | 用户名；必填。 |
| `groups` | 补充组；`[]`。 |
| `shell` | 登录 shell；`/bin/bash`。 |
| `sudo` | 使该用户成为 sudo 用户；`false`。 |
| `password_hash` | 用户 `crypt(3)` 哈希；`""` 锁定账户。 |

| `system.first_boot` 键 | 含义和默认值 |
| --- | --- |
| `commands` | 在获取的脚本之后按顺序执行的 shell 行；`[]`。 |
| `url` | 脚本 URL；`""` 表示不获取脚本。 |

| `portage` 键 | 含义和默认值或选项 |
| --- | --- |
| `profile` | Portage profile；`default/linux/amd64/23.0/systemd`。 |
| `keywords` | 全局关键词通道：`stable` 或 `testing`；`stable`。 |
| `sync` | 持续同步：`git`、`webrsync` 或 `rsync`；`git`。首次同步使用 webrsync。 |
| `testing_packages` | 系统保持稳定时允许使用 testing 的 atom；`[]`。 |
| `makeopts` | `MAKEOPTS`；`""`。 |
| `common_flags` | 通用编译器标志；`-O2 -pipe`。 |
| `use` | `USE` 标志；`[]`。 |
| `video_cards` | `VIDEO_CARDS` 值；`[]`。 |
| `l10n` | `L10N`；`[]` 从生成的 locale 推导值。 |
| `input_devices` | `INPUT_DEVICES`；`["libinput"]`。 |
| `accept_license` | 接受的许可证；`["@FREE"]`。 |
| `cpu_flags` | CPU 标志；`[]` 保留 profile 值。 |
| `build_in_ram` | `/var/tmp/portage` tmpfs 大小；未设置则在磁盘上构建。 |
| `mirrors` | 镜像记录；默认值见下文。 |
| `binhost` | 二进制主机记录；默认值见下文。 |
| `overlays` | Overlay 记录；`[]`。 |

| `portage.mirrors` 键 | 含义和默认值或选项 |
| --- | --- |
| `region` | Gentoo 镜像区域：`cn` 或 `global`；`global`。 |
| `speed_test` | 对提供的镜像执行速度测试；`false`。 |
| `distfiles` | 自定义 distfile 基地址；非空列表会替换内置列表。 |
| `repo_sync_uri` | 显式仓库同步 URI；`""`。 |
| `site` | `region` 中的站点键；`""` 选择该区域的第一个站点。 |
| `gentoo_distfiles` | 写入 `GENTOO_MIRRORS`；`true`。 |
| `gentoo_zh` | gentoo-zh 镜像：`upstream`、`cernet`、`nju`、`nyist` 或 `ha`；`upstream`。 |
| `gentoo_zh_distfiles` | 将 gentoo-zh distfile 追加到 `GENTOO_MIRRORS`；`true`。 |

| 镜像区域 | 站点键 |
| --- | --- |
| `cn` | `ustc`, `nju`, `bfsu`, `tuna`, `zju`, `sdu`, `hust`, `sustech`, `hit`, `lzu`, `aliyun`, `netease`, `cernet`, `cicku-hk`, `planetunix-hk`, `xtom-hk`, `rackspace-hk`, `aditsu-hk`, `nchc-tw`, `cicku-tw`, `freedif-sg`, `cicku-sg`, `planetunix-sg` |
| `global` | `gentoo`, `osuosl` |

| `portage.binhost` 键 | 含义和默认值或选项 |
| --- | --- |
| `official` | 启用官方二进制主机；`true`。 |
| `subarch` | 官方二进制主机子架构；`x86-64`。 |
| `community` | gentoo-zh 通道：`off`、`stable` 或 `unstable`；`off`。 |

| `portage.overlays` 项 | 含义 |
| --- | --- |
| `name` | Overlay 名称；必填。 |
| `sync_uri` | Overlay 同步 URI；必填。 |

| `kernel` 键 | 含义和默认值或选项 |
| --- | --- |
| `source` | kernel 选择：`dist-bin`、`dist-source`、`cjk-bin` 或 `cjk`；`dist-bin`。 |
| `package` | 覆盖 `source` 隐含的软件包；`""`。 |
| `version` | 版本固定；`""` 让 Portage 选择允许的最新关键词版本。 |
| `dracut_modules` | 添加磁盘布局所需的 dracut 模块；`[]`。 |
| `remote_unlock` | initramfs SSH 解锁记录；默认为空记录。 |

| `kernel.source` | 软件包和 CJK 状态 |
| --- | --- |
| `dist-bin` | `sys-kernel/gentoo-kernel-bin`；不是 CJK kernel。 |
| `dist-source` | `sys-kernel/gentoo-kernel`；不属于 CJK kernel。 |
| `cjk-bin` | `sys-kernel/gentoo-cjk-kernel-bin`；CJK kernel。 |
| `cjk` | `sys-kernel/gentoo-cjk-kernel`；CJK kernel。 |

| `kernel.remote_unlock` 键 | 含义和默认值 |
| --- | --- |
| `enabled` | 启用 initramfs SSH 解锁；`false`。 |
| `port` | initramfs SSH 端口；`222`。 |
| `address` | 静态 CIDR 地址；`""` 使用 DHCP。 |
| `gateway` | 静态地址网关；`""`。 |
| `interface` | initramfs 网络接口；`""`。 |

| `bootloader` 键 | 含义和默认值或选项 |
| --- | --- |
| `kind` | 引导程序：`grub`、`systemd-boot` 或 `zfsbootmenu`；`grub`。 |
| `firmware` | 固件：`uefi` 或 `bios`；`uefi`。 |
| `kernel_params` | 额外 kernel 命令行参数；`[]`。 |

| `packages` 键 | 含义和默认值 |
| --- | --- |
| `desktop` | 桌面 profile 名称；`""` 表示不选择桌面。 |
| `applications` | 软件包组名称；`[]`。 |
| `graphics` | 图形驱动程序组名称；`[]`；多个组适用于混合硬件。 |
| `display_manager` | 显示管理器组；`""` 选择控制台登录。 |
| `extra` | 在其他选择之后合并的软件包 atom；`[]`。 |

| `disk` 键 | 含义和默认值或选项 |
| --- | --- |
| `graph` | 由 `[[disk.devices]]` 表示的设备图；必填。 |
| `root` | 根图节点标识符；转换之外必填；`""` 在转换时使用运行中布局。 |
| `mode` | `partition`、`in-place`、`image` 或 `dd`；`partition`。 |
| `image` | image 模式下的稀疏镜像路径；`""`。 |
| `size` | image 模式下的稀疏镜像大小；未设置。 |
| `wipe` | 磁盘级擦除设置；`false`。 |
| `source` | `dd` 模式下的准备镜像来源；`""`。 |
| `source_format` | 来源编码：`raw`、`gz`、`xz`、`zst` 或 `tar`；`raw`。 |
| `destination` | 整盘 `dd` 目标；`""`。 |

| 模板输入 | 含义和默认值或选项 |
| --- | --- |
| `disk` | 整盘选择器；必填。 |
| `layout` | `whole-disk`、`whole-disk-btrfs`、`whole-disk-zfs` 或 `reuse`；`whole-disk`。 |
| `firmware` | 模板固件；`uefi`。 |
| `table` | 分区表覆盖；未设置时 UEFI 推导为 GPT，BIOS 推导为 MBR。 |
| `filesystem` | 非 Btrfs、非 ZFS 整盘布局的根文件系统；`xfs`。 |
| `swap` | swap 分区大小；未设置。 |
| `passphrase_file` | 安装系统的 passphrase 文件路径；`""` 表示布局不加密。 |
| `pool` | ZFS pool 名称；`rpool`。 |

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

| 退出码 | `gentoo-install` |
| --- | --- |
| `0` | 成功完成 |
| `1` | 配置错误 |
| `2` | `argparse` 用法错误或 preflight 失败 |
| `3` | 完整性验证失败 |
| `4` | 下载、外部命令、操作系统或未分类的安装程序失败 |
| `5` | 操作者中止 |

Python CLI 启动前，如果 Python、必要命令或 root 权限检查失败，`bootstrap.sh` 也可能以 `1` 退出。

## 常见问题

<!-- fact: faq-customisation -->

**这种安装器会不会让 Gentoo 失去可定制性？**

不会。它只执行基础安装：分区、stage3、Portage 配置、内核、bootloader，以及可选的桌面。之后的每个决定仍然属于操作者，而那台机器是一套普通的 Gentoo，上面不留本项目的任何组件。它省掉的是第一个小时的成本，而那正是 Gentoo 难以入门、也难以在大量机器或 VPS 上部署的原因。

## 参与开发

<!-- fact: contributing -->

[CONTRIBUTING.md](CONTRIBUTING.md) 介绍开发环境、架构和必要检查。

## 许可

<!-- fact: license -->

本项目以 GNU General Public License 发布，版本为第 2 版，或由接收者选择任何更新的版本。第 2 版全文见 [LICENSE](LICENSE)，每份源代码文件带有 `SPDX-License-Identifier: GPL-2.0-or-later`。
