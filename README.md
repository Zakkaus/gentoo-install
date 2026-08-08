正體中文 | [简体中文](README.zh-CN.md) | [English](README.en.md)

# gentoo-install

從任何一套 Linux live 系統把一台機器裝成可開機的 Gentoo 的文字安裝器。用選單或設定檔驅動，中文環境預設可用，每一項都是可以關掉的選項。它是 Gig-OS Live ISO 上 Calamares 圖形安裝器的文字對照物：沒有桌面、沒有滑鼠，可以透過序列埠或 SSH 遠端裝機。

## 支援的環境

安裝器在 live 系統上執行，把 Gentoo 裝到目標磁碟。下列六種 live 媒介逐一開機實測過：

| live 系統 | 版本 | python3 | 需要另外安裝的 |
|---|---|---|---|
| Gentoo minimal | 20260712 | 3.14.6 | 無 |
| Arch | 2026.08.01 | 3.14.6 | 無 |
| openSUSE Tumbleweed Rescue | current | 3.13.14 | 無 |
| Fedora Workstation Live | 43 | 3.14.0 | `gptfdisk` |
| Debian live standard | 13.6 | 3.13.5 | `dosfstools`、`gdisk` |
| Alpine standard | 3.24.1 | 沒有 | `python3` 起 |

Python 下限是 3.11，由標準庫的 `tomllib` 決定。安裝器只用標準庫，不引入第三方依賴。目標架構是 amd64。

缺哪些指令由啟動腳本算出來並給出該發行版的安裝指令，不必自己對照。

## 不用先 clone

倉庫是公開的，所以一行就能取得並執行。這條在任何一種 live 系統上都一樣：

```sh
curl -fsSL https://github.com/Zakkaus/gentoo-install/archive/refs/heads/master.tar.gz | tar xz
cd gentoo-install-master && ./bootstrap.sh
```

需要 `curl` 與 `tar`；兩者每一種 live 媒介都有，Alpine 的 busybox tar 也解得開這個壓縮檔（stage3 才需要 GNU tar，那是安裝器自己檢查的）。

## 使用

安裝器要以 root 執行。`bootstrap.sh` 是唯一入口，它檢查 Python 版本、列出缺少的指令，然後執行安裝器。

先看一遍將要執行的操作，這一步不碰磁碟：

```sh
./bootstrap.sh --dry-run --config tests/fixtures/vm-binpkg.toml
```

用選單裝機：

```sh
./bootstrap.sh
```

用設定檔無人值守裝機：

```sh
./bootstrap.sh --config my-install.toml
```

選單需要真終端機；在管線裡執行會報錯並提示改用 `--config`。介面語言從 `LC_ALL`、`LC_MESSAGES`、`LANG` 依序推導，`--lang zh-TW` 可覆蓋。

## 設定檔

設定檔是 TOML，第一行必須宣告 `config_version`。磁碟那一段是一張裝置圖：每個裝置有一個 `id`，彼此以 `id` 相互引用，路徑到執行時才解析，所以中斷重跑時對應關係不變。

```toml
config_version = 1

[system]
hostname = "gentoo"
locale = "zh_TW.UTF-8"
init = "systemd"
# crypt(3) 雜湊，不是明文。用 openssl passwd -6 產生。
root_password_hash = "$6$..."

[portage]
# profile 必須與 init 一致，否則驗證不通過。
profile = "default/linux/amd64/23.0/systemd"

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

完整的例子在 `tests/fixtures/`，每一份對應一條實測過的路徑。

設定檔解析不碰硬體，所以在沒有目標磁碟的機器上也能用 `--dry-run` 驗證一份設定。

## 已實測的安裝路徑

下列十條各自裝完、關機、拔掉安裝媒介之後再開機驗證過，檢查項目包括掛載、fstab、locale、開機服務與有無失敗的單元：

- UEFI 加 `gentoo-kernel-bin`
- 從 sources 套件現地建置核心
- ZFS 根加 ZFSBootMenu，未加密與原生加密各一
- BIOS 加 MBR 加 openrc
- systemd-boot
- LUKS2 加 btrfs subvolume
- LVM
- mdraid RAID1
- KDE Plasma 桌面加中文環境

## 中文環境

locale、時區、鍵盤、鏡像、字型、輸入法是各自獨立的選項，不是綁成一包。選了輸入法才會裝 fcitx5 與 rime，並寫入 `/etc/skel` 與每個使用者家目錄的設定；Wayland 工作階段下不設 `GTK_IM_MODULE` 與 `QT_IM_MODULE`，因為合成器以 text-input 協定直接驅動 fcitx，設了會讓候選字窗閃爍。

overlay 是選進來的，不會背著使用者加。`gentoo-zh` 與 `gig` 各自獨立，選中時一併配置金鑰與 `package.accept_keywords`。

## 二進位套件

`binpkg` 是可選路徑，原始碼編譯永遠是保證路徑。官方與社群 binhost 是兩個獨立開關，各自的金鑰分開管理。任何一步失敗——主機不可達、缺簽章、金鑰不受信任——都降級為原始碼編譯並給出警告，`install.jsonl` 記下每個套件的來源與每次降級的原因。

## 退出碼

| 碼 | 意義 |
|---|---|
| 0 | 完成 |
| 1 | 設定錯誤：解析、驗證、版本不相容 |
| 2 | preflight 硬性檢查失敗 |
| 3 | 完整性驗證失敗：GPG、校驗和、指紋 |
| 4 | 外部指令失敗，或檔案下載不成 |
| 5 | 使用者中止 |

## 參與開發

```sh
python3 -m mypy
python3 -m pytest
```

兩者都必須通過。改動分割、檔案系統、chroot、開機載入器或 binhost 信任的，還要有一次 `tests/vm/run.py` 實測：

```sh
python3 -m tests.vm.run --medium official-minimal --firmware uefi --install fixtures/vm-binpkg.toml
python3 -m tests.vm.run --medium official-minimal --firmware uefi --install fixtures/vm-binpkg.toml --boot-installed
```

VM 測試需要 `qemu-system-x86_64`、KVM、OVMF 與 `xorriso`。ISO 快取在 `lab/vm/`。
