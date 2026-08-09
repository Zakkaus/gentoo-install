[English](README.md) | 正體中文 | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | [한국어](README.ko.md)

# gentoo-install

從任何一套執行中的 Linux live 系統，把機器裝成可開機 Gentoo 的安裝器。以選單或設定檔驅動。介面提供英文、正體中文、简体中文、日本語與한국어。

![選單，列出安裝器所做的每一項決定](screenshot.png)

![cjktty 主控台渲染簡體中文、正體中文、日文與韓文](cjk-console.png)

## 功能

**磁碟。** GPT 與 MBR。ext2/3/4、帶 subvolume 的 btrfs、xfs、f2fs、vfat、swap 與 zram。LUKS2、LVM，以及 raid0、raid1、raid5、raid6 的 mdraid。ZFS pool 與 dataset，含原生加密、mirror 與 raidz。既有分割表可以沿用：逐個分割區指定掛載點，並各自決定是否格式化。

**開機。** UEFI 與 BIOS 上的 GRUB、systemd-boot，ZFS 根則用 ZFSBootMenu。initramfs 由 dracut 產生，模組清單從裝置圖推導而非手動列出。根檔案系統可在 initramfs 階段以 SSH 解開。

**核心。** `::gentoo` 的 `sys-kernel/gentoo-kernel-bin` 與 `sys-kernel/gentoo-kernel`，以及 gentoo-zh overlay 的 `sys-kernel/gentoo-cjk-kernel-bin` 與 `sys-kernel/gentoo-cjk-kernel`。gentoo-zh 那一對帶 [cjktty-patches](https://github.com/gentoo-zh/cjktty-patches)，能在文字主控台顯示中文、日文與韓文，原版核心在同一位置畫的是空白。上面第二張圖是套用該補丁的 `7.1.7-gentoo-dist`。

**系統。** systemd 或 OpenRC。NetworkManager 搭配 wpa_supplicant 或 iwd、systemd-networkd，或不設網路。靜態位址、DNS、主機名稱與時區。首次開機執行一次的指令或腳本，可指定網址取得。

**桌面。** GNOME、KDE Plasma 與 Xfce，登入管理程式為 gdm、sddm、lightdm 或 greetd。顯示卡涵蓋 amdgpu、intel、nvidia、nouveau、radeon 與虛擬機：`VIDEO_CARDS`、該驅動需要的 USE 旗標與核心參數一併設定，不留給操作者自行補齊。

**輸入法。** fcitx5 與 ibus。Rime 提供拼音、注音、倉頡、五筆與粵拼方案；日文為 Anthy 與 Mozc；韓文為 Hangul。字型與 locale 是兩個各自獨立的選項。

**Portage。** profile、`MAKEOPTS`、`USE`、`ACCEPT_KEYWORDS`、`L10N`、鏡像區域與倉庫同步方式。gentoo-zh 與 gig overlay 為選用，選中時一併寫入其金鑰與 `package.accept_keywords`。二進位套件來自官方主機與 gentoo-zh，金鑰各自管理。

**每一項功能都有 dry run。** `--dry-run` 依同一份計畫印出實際執行會套用的操作清單，因此只印不做的路徑無法與真正執行的路徑分歧。中斷的安裝可依日誌續裝。`install.jsonl` 記錄每個套件的來源與每一次退回原始碼編譯的原因。設定檔可匯出到 pastebin 或主控台上的 QR code，密碼雜湊在匯出前移除。

## 需求

以 root 執行，目標架構 amd64，Python 3.11 以上，只用標準庫。

啟動時需要能連上 `packages.gentoo.org`。核心版本與 `sys-fs/zfs` 的核心上限都是即時讀取，因此本機不需要 ebuild 樹，安裝器也就能在 Alpine、Debian、openSUSE、Fedora、Arch 與 Gentoo 的 live 系統上執行。連不上就停止，只有 `--missing-commands` 以及 `--config` 加 `--dry-run` 兩種離線答案例外。

`bootstrap.sh` 讀 `/etc/os-release`，列出所選版面需要而本機缺少的指令，並印出該發行版的安裝指令。已知 `apt-get`、`pacman`、`zypper`、`dnf`、`emerge` 與 `apk`。

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
./bootstrap.sh --config my-install.toml --no-shell   # 收尾不詢問，直接卸載
```

選單需要真終端機，畫面至少 80x24。介面語言在開場詢問一次，`--lang zh-TW` 跳過這一問。

卸載之前，無論安裝完成或失敗，安裝器都會提供在新系統裡開啟 root shell 的選項。失敗時同樣提供：機器是否還能救回由操作者判斷，而卸載之後要再進去就得把整個版面手動掛回來。`--no-shell` 移除這一問。

## 設定檔

TOML，第一行宣告 `config_version`。磁碟是一張裝置圖：每個裝置帶有 `id`，裝置之間以 `id` 互相引用，裝置路徑到執行時才解析。

```toml
config_version = 1

[system]
hostname = "gentoo"
locale = "zh_TW.UTF-8"
init = "systemd"
root_password_hash = "$6$..."   # 由 openssl passwd -6 產生，不放明文

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

`tests/fixtures/` 下有可用的範例，涵蓋 UEFI、BIOS、LUKS2、LVM、mdraid、ZFS、btrfs subvolume 與桌面。解析不碰硬體，因此沒有目標磁碟的機器也能以 `--dry-run` 檢查一份設定檔。

## 二進位套件

選用，而且從不是唯一路徑。原始碼編譯才是保證路徑。官方 binhost 與 gentoo-zh binhost 是兩個各自獨立的選項，金鑰分開管理。主機不可達、缺少簽章或金鑰不受信任，都退回編譯並印出警告，`install.jsonl` 記下原因。

## 退出碼

`0` 完成、`1` 設定錯誤、`2` preflight 失敗、`3` 完整性驗證失敗、`4` 外部指令失敗、`5` 操作者中止。

## 參與開發

見 [CONTRIBUTING.md](CONTRIBUTING.md)。
