[English](README.md) | 正體中文 | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | [한국어](README.ko.md)

# gentoo-install

gentoo-install 是一套系統安裝器，可在相容的 Linux live 環境中安裝 amd64 架構的 Gentoo 系統。安裝內容以互動式選單或 TOML 設定檔指定。介面提供英文、正體中文、簡體中文、日文與韓文。

![顯示各項安裝決定的選單](screenshot.png)

![cjktty 主控台顯示簡體中文、正體中文、日文與韓文](cjk-console.png)

## 功能

**儲存裝置** 裝置圖支援 GPT 與 MBR 分割表。檔案系統支援 ext2/3/4、xfs、f2fs、vfat 以及含 subvolume 的 btrfs，另有 swap、zram、LUKS2 加密、LVM 與 mdraid。既有分割表可以保留，每個分割區可分別指定保留、格式化或刪除。

**開機與系統** 開機載入器可選 GRUB 或 systemd-boot，其中 GRUB 支援 UEFI 與 BIOS，systemd-boot 支援 UEFI。安裝器另可設定 systemd 或 OpenRC、dracut、locale、鍵盤配置、時區、主機名稱、DNS、靜態位址與所選的網路管理程式。

**桌面與語言支援** 桌面可選 GNOME、KDE Plasma 與 Xfce，並搭配 gdm、sddm 或 lightdm。圖形設定涵蓋 AMD、Intel、NVIDIA 與虛擬機。套件目錄包含 fcitx5、Rime、Anthy、Mozc、Hangul 與 CJK 字型。gentoo-zh 提供的修補核心可在 Linux 文字主控台顯示中文、日文與韓文。

**Portage** 設定項目包含 profile、`MAKEOPTS`、`USE`、`ACCEPT_KEYWORDS`、`L10N`、鏡像與儲存庫同步方式。gentoo-zh 與 gig overlay 必須明確啟用。官方與 gentoo-zh 的二進位套件來源各有獨立設定與金鑰。

**計畫與記錄** dry run 與實際安裝使用同一份操作序列。記錄方面，`install.log` 記錄指令輸出，`install.jsonl` 記錄操作、套件來源與二進位套件降級原因。選單可將移除敏感資料的設定上傳至 `paste.gentoozh.org`，並以文字與 QR 碼顯示上傳頁面的網址。

## 驗證狀態

最近一份有記錄的端到端基準涵蓋部分 UEFI 與 BIOS 安裝、systemd、OpenRC、ext4、btrfs、xfs、LUKS2、LVM、mdraid、Plasma 與官方 binhost。每份有效記錄都會標明安裝器修訂版，並在裝好的系統成功開機後才列為實據。

現行修訂的基準尚未涵蓋 ZFS 與 ZFSBootMenu、initramfs 的 SSH 遠端解鎖、greetd 桌面工作階段，以及 GNOME 以外的 ibus。從六種非預設 live 媒介安裝與 binhost 失敗後的降級路徑也未納入。`tests/fixtures/` 下的檔案只驗證設定模型；檔案存在不代表對應組合已通過端到端驗證。

## 需求

實際安裝需要 root 權限、amd64 目標與 Python 3.11 以上版本。使用設定檔執行 dry run 不需要 root 權限。安裝器沒有第三方 Python 執行期相依套件。

選單從 `packages.gentoo.org` 讀取每個版本，因此需要連線至該站台。以設定檔安裝時需要連線的是該設定指定的鏡像；`--missing-commands` 與 `--config FILE --dry-run` 兩者都不需要。核心版本與 `sys-fs/zfs` 支援的最高核心版本會在執行時讀取。

支援純 IPv4、純 IPv6 與雙堆疊網路；選單會拒絕目前位址家族無法連線的鏡像。

`bootstrap.sh` 會讀取 `/etc/os-release`、回報缺少的指令，並印出候選的套件管理器指令。它可辨識多個發行版系列，包括 Debian 與 Ubuntu、Arch、openSUSE、Fedora、RHEL 與 CentOS、Gentoo，以及 Alpine。印出的指令必須在執行前核對。

## 安全事項

實際安裝會寫入所選磁碟。使用設定檔執行時不會再次要求確認清除磁碟；`wipe = true`、刪除分割區與建立檔案系統都可能毀損既有資料。

實際安裝前，必須在 dry-run 輸出中核對磁碟選擇器與每項破壞性操作。穩定的 `/dev/disk/by-id/` 選擇器優於 `/dev/sda` 之類的名稱；需要保留的資料必須另有備份。

## 安裝

下列指令會下載目前的 `master` 封存檔並開啟選單：

```sh
curl -fsSL https://github.com/Zakkaus/gentoo-install/archive/refs/heads/master.tar.gz | tar xz
cd gentoo-install-master
./bootstrap.sh
```

選單需要至少 80 欄、24 列的互動式終端機。安裝器啟動時會詢問一次介面語言；`--lang zh-TW` 可直接選用正體中文。

選單可將答案儲存為 `my-install.toml` 後離開。下列設定檔操作流程會先印出完整計畫，再執行實際安裝：

```sh
./bootstrap.sh --config my-install.toml --dry-run
# 接著擇一執行下列其中一行。兩者都會寫入所選磁碟。
./bootstrap.sh --config my-install.toml
./bootstrap.sh --config my-install.toml --no-shell   # 同一次安裝，不詢問是否開啟 root shell
```

互動式安裝無論成功或失敗，都會在卸載前提供於目標系統內開啟 root shell 的選項。`--no-shell` 可略過這項確認。

## 從中斷處繼續

`--resume` 會略過日誌中記為完成的操作：

```sh
./bootstrap.sh --config my-install.toml --resume
```

繼續執行僅限同一個 live 工作階段、同一個安裝器修訂版與同一份設定檔。預設日誌位於 `/run/gentoo-install/install.jsonl`，重新開機後不會保留。日誌的每一筆記錄該操作類別原始碼與該操作自身欄位的摘要，因此類別或欄位變更過的操作會重新執行而不是略過。共用輔助函式或常數的變更不在該摘要涵蓋範圍內，日誌也不記錄整份設定的摘要，因此跨修訂版或跨設定檔的續裝不受支援。

## 設定檔

設定檔使用 TOML。頂層的 `config_version` 欄位指定結構版本。儲存裝置以裝置圖表示：每個裝置都有 `id`，裝置以其他裝置的 `id` 建立引用，選擇器只在實際安裝時解析。

[`tests/fixtures/vm-binpkg.toml`](tests/fixtures/vm-binpkg.toml) 是完整的 UEFI 與 ext4 結構參考。其他 [`tests/fixtures/`](tests/fixtures/) 檔案涵蓋 BIOS、LUKS2、LVM、mdraid、ZFS、btrfs subvolume 與桌面。這些檔案使用虛擬機磁碟選擇器與測試密碼，不得原樣用於實機安裝。

解析與計畫階段不會探測儲存硬體，因此沒有目標磁碟的機器也能以 `--dry-run` 檢查設定。

## 二進位套件

二進位套件為選用功能。關閉後仍可從原始碼建置。官方 binhost 與 gentoo-zh binhost 是兩個獨立選項，並使用不同的信任設定。現行端到端基準未涵蓋 binhost 無法連線、缺少簽章或金鑰不受信任的情況，因此這些降級路徑仍列在驗證狀態中。

## 退出碼

`0` 表示完成，`1` 表示設定錯誤，`2` 表示 preflight 失敗，`3` 表示完整性驗證失敗，`4` 表示外部指令失敗，`5` 表示操作者中止。

## 參與開發

[CONTRIBUTING.md](CONTRIBUTING.md) 說明開發環境、架構與必要檢查。
