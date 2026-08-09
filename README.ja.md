[English](README.md) | [正體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md) | 日本語 | [한국어](README.ko.md)

# gentoo-install

稼働中の任意の Linux live システムから、起動可能な Gentoo を構築するインストーラ。メニューまたは設定ファイルで駆動する。インターフェイスは英語、繁体字中国語、簡体字中国語、日本語、韓国語に対応する。

![インストーラが決定する項目を一覧するメニュー](screenshot.png)

![簡体字中国語・繁体字中国語・日本語・韓国語を描画する cjktty コンソール](cjk-console.png)

## 機能

**ディスク。** GPT および MBR。ext2/3/4、subvolume を含む btrfs、xfs、f2fs、vfat、swap、zram。LUKS2、LVM、および raid0・raid1・raid5・raid6 の mdraid。ZFS の pool と dataset は、ネイティブ暗号化、mirror、raidz に対応する。既存のパーティションテーブルは維持できる。各パーティションに対してマウントポイントと再フォーマットの可否を個別に指定する。

**起動。** UEFI と BIOS の GRUB、systemd-boot、ZFS ルートには ZFSBootMenu。initramfs は dracut が生成し、そのモジュール一覧は手書きではなくデバイスグラフから導出される。ルートファイルシステムは initramfs の段階で SSH 経由により解錠できる。

**カーネル。** `::gentoo` の `sys-kernel/gentoo-kernel-bin` と `sys-kernel/gentoo-kernel`、および gentoo-zh overlay の `sys-kernel/gentoo-cjk-kernel-bin` と `sys-kernel/gentoo-cjk-kernel`。gentoo-zh の二つは [cjktty-patches](https://github.com/gentoo-zh/cjktty-patches) を適用しており、テキストコンソールに中国語・日本語・韓国語を表示する。標準のカーネルは同じ位置に空白を描画する。上の二枚目は当該パッチを適用した `7.1.7-gentoo-dist` である。

**システム。** systemd または OpenRC。wpa_supplicant もしくは iwd を伴う NetworkManager、systemd-networkd、またはネットワーク設定なし。静的アドレス、DNS、ホスト名、タイムゾーン。初回起動時に一度だけ実行するコマンドまたはスクリプトを指定でき、URL からの取得にも対応する。

**デスクトップ。** GNOME、KDE Plasma、Xfce。ディスプレイマネージャは gdm、sddm、lightdm、greetd。グラフィックスは amdgpu、intel、nvidia、nouveau、radeon、仮想マシンに対応し、`VIDEO_CARDS`、当該ドライバが必要とする USE フラグ、カーネルパラメータをまとめて設定する。操作者による補完を前提としない。

**入力メソッド。** fcitx5 と ibus。Rime は拼音、注音、倉頡、五筆、粤拼の方式を提供する。日本語は Anthy と Mozc、韓国語は Hangul。フォントは locale とは独立した選択肢である。

**Portage。** profile、`MAKEOPTS`、`USE`、`ACCEPT_KEYWORDS`、`L10N`、ミラー地域、リポジトリの同期方式。gentoo-zh と gig の overlay は任意選択であり、選択した場合はその鍵と `package.accept_keywords` も併せて書き込まれる。バイナリパッケージは公式ホストと gentoo-zh から取得し、鍵は別々に管理する。

**すべての機能に dry run がある。** `--dry-run` は同一の計画に基づき、実際の実行が適用する操作一覧を出力する。したがって出力のみの経路が実行経路と乖離することはない。中断したインストールはジャーナルから再開できる。`install.jsonl` は各パッケージの取得元と、ソースビルドへ切り替えた理由をすべて記録する。設定はパスワードハッシュを除去したうえで、pastebin またはコンソール上の QR コードへ出力できる。

## 要件

root 権限、amd64 のターゲット、Python 3.11 以上。標準ライブラリのみを使用する。

起動時に `packages.gentoo.org` への接続を必要とする。カーネルの版と `sys-fs/zfs` が対応するカーネルの上限は実行時に取得するため、ローカルの ebuild ツリーは不要であり、Alpine、Debian、openSUSE、Fedora、Arch、Gentoo の live システム上で動作する。接続できない場合は停止する。例外は `--missing-commands` と、`--config` に `--dry-run` を伴う場合の二つである。

`bootstrap.sh` は `/etc/os-release` を読み、選択された構成が必要とし当該マシンに存在しないコマンドを列挙して、そのディストリビューションのインストールコマンドを出力する。`apt-get`、`pacman`、`zypper`、`dnf`、`emerge`、`apk` に対応する。

## 使用方法

```sh
curl -fsSL https://github.com/Zakkaus/gentoo-install/archive/refs/heads/master.tar.gz | tar xz
cd gentoo-install-master
./bootstrap.sh
```

```sh
./bootstrap.sh                                       # メニュー
./bootstrap.sh --config my-install.toml              # 無人実行
./bootstrap.sh --dry-run --config my-install.toml    # 操作を出力し、ディスクには触れない
./bootstrap.sh --config my-install.toml --resume     # 前回停止した位置から継続する
./bootstrap.sh --config my-install.toml --no-shell   # 確認せずにアンマウントして終了する
```

メニューは実端末を必要とし、画面は 80x24 以上であること。インターフェイス言語は開始時に一度尋ねられる。`--lang ja` はこの質問を省略する。

アンマウントの前に、インストールの成否にかかわらず、新しいシステム内で root シェルを開く選択肢が提示される。失敗時にも提示される。機器が復旧可能かどうかは操作者の判断であり、アンマウント後に再度入るには構成全体を手動でマウントし直す必要があるためである。`--no-shell` はこの質問を取り除く。

## 設定ファイル

TOML。先頭行で `config_version` を宣言する。ディスクはデバイスグラフである。各デバイスは `id` を持ち、デバイスどうしは `id` で参照し合い、デバイスパスは実行時に解決される。

```toml
config_version = 1

[system]
hostname = "gentoo"
locale = "ja_JP.UTF-8"
init = "systemd"
root_password_hash = "$6$..."   # openssl passwd -6 で生成する。平文は置かない

[portage]
profile = "default/linux/amd64/23.0/systemd"   # init と一致していること

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

`tests/fixtures/` に動作する例があり、UEFI、BIOS、LUKS2、LVM、mdraid、ZFS、btrfs subvolume、デスクトップを網羅する。解析はハードウェアに触れないため、ターゲットディスクを持たないマシンでも `--dry-run` で設定を検証できる。

## バイナリパッケージ

任意であり、唯一の経路となることはない。保証された経路はソースからのビルドである。公式 binhost と gentoo-zh の binhost は別個の選択肢であり、鍵も別々に管理する。ホストへ到達できない、署名が欠けている、鍵が信頼されていない、のいずれの場合も警告を出してビルドへ切り替え、`install.jsonl` に理由を記録する。

## 終了コード

`0` 完了、`1` 設定の誤り、`2` preflight の失敗、`3` 完全性検証の失敗、`4` 外部コマンドの失敗、`5` 操作者による中止。

## 開発への参加

[CONTRIBUTING.md](CONTRIBUTING.md) を参照。
