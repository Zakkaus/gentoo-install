正體中文 | [简体中文](README.zh-CN.md) | [English](README.en.md)

# gentoo-install

從任何一套 Linux live 系統把機器裝成可開機的 Gentoo 的安裝器。選單或設定檔驅動，中文環境預設開啟，每一項都可以關掉。

## 需求

以 root 執行，目標架構 amd64。Python 3.11 以上，只用標準庫。

啟動時需要能連上 `packages.gentoo.org`。核心版本與 `sys-fs/zfs` 的核心上限都是即時讀取的，因此安裝器不需要本機有 ebuild 樹，也就能在 Alpine、Debian、openSUSE、Fedora 與 Arch 的 live 系統上執行。連不上就停，只有 `--missing-commands` 與「`--config` 加 `--dry-run`」這兩種離線答案例外。

`bootstrap.sh` 讀 `/etc/os-release` 判斷發行版，列出這套版面缺少的指令，並印出該發行版的安裝指令。支援 `apt-get`、`pacman`、`zypper`、`dnf`、`emerge` 與 `apk`。

## 使用

```sh
curl -fsSL https://github.com/Zakkaus/gentoo-install/archive/refs/heads/master.tar.gz | tar xz
cd gentoo-install-master
./bootstrap.sh
```

```sh
./bootstrap.sh                                       # 選單
./bootstrap.sh --config my-install.toml              # 無人值守
./bootstrap.sh --dry-run --config my-install.toml    # 只印操作，不碰磁碟
./bootstrap.sh --config my-install.toml --resume     # 從上次停下的地方繼續
./bootstrap.sh --config my-install.toml --no-shell   # 收尾不問，直接卸載
```

選單需要真終端機，畫面至少 80x24。介面語言開場問一次，`--lang zh-TW` 跳過這一問。

安裝結束或中途失敗時，卸載之前會問一次要不要在目標系統裡開一個 root shell。失敗時同樣會問：機器還救不救得回來由操作者判斷，而卸載之後要再進去得把整個版面手動掛回來。`--no-shell` 關掉這一問。

## 設定檔

TOML，第一行宣告 `config_version`。磁碟是一張裝置圖：每個裝置有自己的 `id`，彼此以 `id` 引用，裝置路徑到執行時才解析。

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

`tests/fixtures/` 下有十三份可用的範例，涵蓋 UEFI、BIOS、LUKS2、LVM、mdraid、ZFS 與桌面。解析不碰硬體，所以沒有目標磁碟的機器也能用 `--dry-run` 驗一份設定檔。

## 支援的版面

分割表 GPT 與 MBR。檔案系統 ext2/3/4、btrfs（含 subvolume）、xfs、f2fs、vfat、swap 與 zram。堆疊 LUKS2、LVM、mdraid，以及 ZFS pool 與 dataset，含原生加密。開機載入器 GRUB、systemd-boot，ZFS 根走 ZFSBootMenu。既有分割區可以沿用：整張分割表不重寫，逐個分割區決定掛在哪裡、要不要格式化。

## 中文環境

locale、時區、鍵盤、鏡像、字型與輸入法是六個各自獨立的選項。選了輸入法才安裝 fcitx5 與 rime 並寫入設定；Wayland 下不設 `GTK_IM_MODULE` 與 `QT_IM_MODULE`，設了候選字窗會閃爍。rime 的方案一個一組：`luna_pinyin` 隨引擎一起來，`bopomofo`、`cangjie5`、`wubi86`、`jyut6ping3` 各自勾選。overlay 只在選中時加入。

主控台要顯示 CJK 需要 `sys-kernel/gentoo-cjk-kernel`，它帶 cjktty 補丁，在 gentoo-zh 裡。選了那個核心才能選 16x32 的主控台字型。

## 二進位套件

可選，原始碼編譯是保證路徑。官方 binhost 與 gentoo-zh 分開，金鑰各自管理。取得金鑰、驗簽或下載任何一步失敗都降級為編譯並印出警告，`install.jsonl` 記下每個套件的來源與每一次降級的原因。

## 退出碼

`0` 完成、`1` 設定錯誤、`2` preflight 失敗、`3` 完整性驗證失敗、`4` 外部指令失敗、`5` 使用者中止。

## 參與開發

```sh
python3 -m mypy
python3 -m pytest
```

改動分割、檔案系統、chroot、開機載入器或 binhost 信任的，另外需要一次 VM 實測：

```sh
python3 -m tests.vm.run --medium official-minimal --firmware uefi --install fixtures/vm-binpkg.toml
python3 -m tests.vm.run --medium official-minimal --firmware uefi --install fixtures/vm-binpkg.toml --boot-installed
```

需要 `qemu-system-x86_64`、KVM、OVMF 與 `xorriso`。
