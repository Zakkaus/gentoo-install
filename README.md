正體中文 | [简体中文](README.zh-CN.md) | [English](README.en.md)

# gentoo-install

從任何一套 Linux live 系統把機器裝成可開機的 Gentoo。選單或設定檔驅動，中文環境預設可用且每一項可關。

## 需求

以 root 執行。Python 3.11 以上，只用標準庫。目標架構 amd64。

下列 live 媒介逐一實測過。缺少的指令由 `bootstrap.sh` 列出並給該發行版的安裝指令：

| 媒介 | python3 | 需另裝 |
|---|---|---|
| Gentoo minimal 20260712 | 3.14.6 | 無 |
| Arch 2026.08.01 | 3.14.6 | 無 |
| openSUSE Tumbleweed Rescue | 3.13.14 | 無 |
| Fedora Workstation Live 43 | 3.14.0 | `gptfdisk` |
| Debian live 13.6 | 3.13.5 | `dosfstools`、`gdisk` |
| Alpine 3.24.1 | 無 | `python3` 起 |

## 使用

```sh
curl -fsSL https://github.com/Zakkaus/gentoo-install/archive/refs/heads/master.tar.gz | tar xz
cd gentoo-install-master
./bootstrap.sh
```

`bootstrap.sh` 是唯一入口。

```sh
./bootstrap.sh                                       # 選單
./bootstrap.sh --config my-install.toml              # 無人值守
./bootstrap.sh --dry-run --config my-install.toml    # 只印操作，不碰磁碟
./bootstrap.sh --config my-install.toml --resume     # 從上次停下的地方繼續
```

選單需要真終端機。介面語言開場問一次，`--lang zh-TW` 跳過。

## 設定檔

TOML，第一行宣告 `config_version`。磁碟是一張裝置圖：每個裝置有 `id`，彼此以 `id` 引用，路徑執行時才解析。

```toml
config_version = 1

[system]
hostname = "gentoo"
locale = "zh_TW.UTF-8"
init = "systemd"
root_password_hash = "$6$..."   # openssl passwd -6 產生，不放明文

[portage]
profile = "default/linux/amd64/23.0/systemd"   # 必須與 init 一致

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

範例在 `tests/fixtures/`。解析不碰硬體，沒有目標磁碟的機器也能用 `--dry-run` 驗設定。

## 已實測

下列每一條都裝完、關機、拔掉媒介再開機，檢查掛載、fstab、locale、開機服務與失敗單元。

- UEFI 加 `gentoo-kernel-bin`
- 從 sources 建置核心
- ZFS 加 ZFSBootMenu，未加密與原生加密各一
- BIOS 加 MBR 加 openrc
- systemd-boot
- LUKS2 加 btrfs subvolume
- LVM
- mdraid RAID1
- KDE Plasma 加中文環境

## 中文環境

locale、時區、鍵盤、鏡像、字型、輸入法各自獨立。選了輸入法才裝 fcitx5 與 rime 並寫入設定；Wayland 下不設 `GTK_IM_MODULE` 與 `QT_IM_MODULE`，設了候選字窗會閃爍。overlay 只在選中時加入。

## 二進位套件

可選，原始碼編譯是保證路徑。官方與 gentoo-zh 分開，金鑰各自管理。任一步失敗降級為編譯並警告，`install.jsonl` 記下每個套件的來源與降級原因。

## 退出碼

`0` 完成、`1` 設定錯誤、`2` preflight 失敗、`3` 完整性驗證失敗、`4` 外部指令失敗、`5` 使用者中止。

## 參與開發

```sh
python3 -m mypy
python3 -m pytest
```

改動分割、檔案系統、chroot、開機載入器或 binhost 信任的，另需一次 VM 實測：

```sh
python3 -m tests.vm.run --medium official-minimal --firmware uefi --install fixtures/vm-binpkg.toml
python3 -m tests.vm.run --medium official-minimal --firmware uefi --install fixtures/vm-binpkg.toml --boot-installed
```

需要 `qemu-system-x86_64`、KVM、OVMF 與 `xorriso`。
