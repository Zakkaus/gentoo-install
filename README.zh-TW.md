[English](README.md) | 正體中文 | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | [한국어](README.ko.md)

# gentoo-install

<!-- fact: identity -->

gentoo-install 在 Linux live 環境中執行，用於安裝 amd64 架構的 Gentoo 系統。安裝內容以互動式選單或 TOML 設定檔指定。介面提供英文、正體中文、簡體中文、日文與韓文。

![顯示各項安裝決定的選單](screenshot-zh-TW.png)

## 功能概要

<!-- fact: capability-summary -->

實作範圍包括一般磁碟安裝、儲存與開機設定、桌面與語言 profile，以及特殊模式。

- **儲存。**裝置圖涵蓋分割區表、檔案系統、LUKS2、LVM、mdraid 與 ZFS。
- **開機與系統。**GRUB、systemd-boot 與 ZFSBootMenu 依設定提供 UEFI 或 BIOS 開機。
- **桌面與語言。**GNOME、KDE Plasma、Xfce、CJK 字型與輸入法都是設定選項。
- **特殊模式。**記憶體環境、原地轉換、sparse image 與 `dd` 各有獨立限制。

[參考資料](REFERENCE.md#capabilities)說明模型、限制與特殊模式程序。

## 驗證狀態

<!-- fact: verification-scope -->

[`TESTED.md`](TESTED.md)記錄每條已執行路徑、安裝器修訂版與執行環境。一次執行只有在記錄的修訂版符合安裝器、安裝以 `0` 結束、已安裝系統可開機，且開機後設定檢查通過時才計入。

單元、計畫與 fixture coverage 說明實作行為，不能證明已安裝系統已開機。[`tests/fixtures/`](tests/fixtures/)驗證設定模型，不是已安裝的機器。紀錄會列出尚未驗證的組合。

## 需求

<!-- fact: requirements-runtime -->

實際安裝需要 root 權限、amd64 目標與 Python 3.11 以上版本。使用設定檔執行 dry run 不需要 root 權限。安裝器沒有第三方 Python 執行期相依套件。

## 安全事項

<!-- fact: safety-destructive -->

實際執行會寫入所選磁碟。設定檔執行沒有第二次清除確認；`wipe = true`、刪除分割區與建立檔案系統都可能毀損既有資料。

<!-- fact: safety-review-backup -->

實際執行前，必須在 dry-run 輸出中核對磁碟選擇器與每項破壞性操作。穩定的 `/dev/disk/by-id/` 選擇器優於 `/dev/sda` 之類的名稱；需要保留的資料必須另有備份。

## 安裝

<!-- fact: install-download -->

下列指令會下載目前的 `master` 封存檔並開啟選單：

```sh
curl -fsSL https://github.com/Zakkaus/gentoo-install/archive/refs/heads/master.tar.gz | tar xz
cd gentoo-install-master
./bootstrap.sh
```

<!-- fact: install-terminal -->

選單需要至少 80 欄、24 列的互動式終端機。

<!-- fact: install-config-workflow -->

選單會將答案儲存為 `my-install.toml`。設定檔流程依序為儲存、dry run、檢視、安裝：

```sh
./bootstrap.sh --config my-install.toml --dry-run
# 在選擇實際指令前檢視呈現的計畫。
./bootstrap.sh --config my-install.toml
./bootstrap.sh --config my-install.toml --no-shell   # 同一次執行，不詢問 root shell
```

<!-- fact: install-root-shell -->

互動式執行會在卸載前提供目標 root shell。`--no-shell` 會略過該提示。

## 設定檔

<!-- fact: configuration-reference -->

設定檔使用 TOML，`config_version` 選擇 schema 版本。[設定參考資料](REFERENCE.md#configuration-files)列出所有持久化鍵與通過驗證的範例。[`tests/fixtures/vm-binpkg.toml`](tests/fixtures/vm-binpkg.toml)是 schema reference；其中的虛擬機磁碟選擇器與測試憑證不得原樣用於實機。

## 從中斷處繼續

<!-- fact: resume-limits -->

`--resume` 僅限同一個 live 工作階段、同一安裝器修訂版與同一份設定檔。安裝器會拒絕不相符的情況。預設日誌位於 `/run/gentoo-install/install.jsonl`，所以不會在重新開機後保留。

```sh
./bootstrap.sh --config my-install.toml --resume
```

## 二進位套件

<!-- fact: binary-packages -->

二進位套件是可選項。停用後仍可從原始碼建置。官方 binhost 與 gentoo-zh binhost 有各自的信任設定。無法連線的 binhost、缺少簽章與不受信任的金鑰目前都沒有端對端證據；這些降級路徑仍未驗證。

## 參考資料

<!-- fact: reference -->

[REFERENCE.md](REFERENCE.md)收錄執行期需求、命令列選項、記憶體環境、原地轉換、功能與驗證細節、設定檔、二進位套件信任與退出碼。

## 參與開發

<!-- fact: contributing -->

[CONTRIBUTING.md](CONTRIBUTING.md)說明開發環境、架構與必要檢查。

## 授權

<!-- fact: license -->

gentoo-install 以 GNU General Public License 散布，版本為第 2 版，或由接收者選擇任何較新的版本。第 2 版全文見 [LICENSE](LICENSE)，每份原始碼檔案帶有 `SPDX-License-Identifier: GPL-2.0-or-later`。
