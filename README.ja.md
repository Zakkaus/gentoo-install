[English](README.md) | [正體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md) | 日本語 | [한국어](README.ko.md)

# gentoo-install

gentoo-install は、対応する Linux ライブ環境から amd64 Gentoo システムをインストールするインストーラである。インストール内容は対話式メニューまたは TOML 設定ファイルで指定する。インターフェイスは英語、繁体字中国語、簡体字中国語、日本語、韓国語で利用できる。

![インストール内容の各項目を表示するメニュー](screenshot.png)

![簡体字中国語、繁体字中国語、日本語、韓国語を表示する cjktty コンソール](cjk-console.png)

## 機能

**ストレージ。** デバイスグラフは、GPT と MBR、ext2/3/4、subvolume を含む btrfs、xfs、f2fs、vfat、swap、zram、LUKS2、LVM、mdraid に対応する。既存のパーティションテーブルを維持し、パーティションごとに保持、フォーマット、削除を指定できる。

**起動とシステム。** GRUB は UEFI と BIOS に対応し、systemd-boot は UEFI に対応する。systemd または OpenRC、dracut、locale、キーボードレイアウト、タイムゾーン、ホスト名、DNS、静的アドレス、選択したネットワークマネージャを設定できる。

**デスクトップと言語対応。** GNOME、KDE Plasma、Xfce は、gdm、sddm、lightdm と組み合わせて使用できる。グラフィックス設定は AMD、Intel、NVIDIA、仮想マシンに対応する。パッケージカタログには fcitx5、Rime、Anthy、Mozc、Hangul、CJK フォントが含まれる。gentoo-zh のパッチ適用済みカーネルは、Linux テキストコンソールに中国語、日本語、韓国語を表示できる。

**Portage。** profile、`MAKEOPTS`、`USE`、`ACCEPT_KEYWORDS`、`L10N`、ミラー、リポジトリ同期方式を設定できる。gentoo-zh と gig の overlay は明示的に選択した場合のみ有効になる。公式と gentoo-zh のバイナリパッケージ取得元には、それぞれ独立した設定と鍵がある。

**計画と記録。** dry run と実際のインストールは同じ操作計画を使用する。`install.log` はコマンド出力を記録し、`install.jsonl` は操作、パッケージの取得元、バイナリパッケージのフォールバック理由を記録する。メニューは機密情報を除去した設定を `paste.gentoozh.org` に送信し、そのページアドレスをテキストと QR コードで表示できる。

## 検証状況

記録済みの最新エンドツーエンド検証ベースラインは、一部の UEFI と BIOS インストール、systemd、OpenRC、ext4、btrfs、xfs、LUKS2、LVM、mdraid、Plasma、公式 binhost を対象とする。各実行記録はインストーラのリビジョンを明示し、インストール済みシステムの起動を確認して初めて検証の根拠となる。

現在のリビジョンでは、ZFS と ZFSBootMenu、initramfs の SSH リモート解錠、greetd のデスクトップセッション、GNOME 以外での ibus は、このベースラインに含まれない。既定以外の 6 種類のライブメディアからのインストールと、binhost 障害時のフォールバックもベースライン外である。`tests/fixtures/` のファイルは設定モデルを検証するものであり、ファイルの存在だけではエンドツーエンド対応の根拠にならない。

## 要件

実際のインストールには root 権限、amd64 ターゲット、Python 3.11 以降が必要である。設定ファイルの dry run には root 権限を必要としない。Python 標準ライブラリ以外の実行時依存はない。

インストーラの開始時に `packages.gentoo.org` への接続が必要である。ただし、`--missing-commands` と `--config FILE --dry-run` は例外である。カーネルバージョンと `sys-fs/zfs` が対応する最大カーネルバージョンは実行時に取得する。

`bootstrap.sh` は `/etc/os-release` を読み、不足しているコマンドを報告し、候補となるパッケージマネージャのコマンドを出力する。Debian と Ubuntu、Arch、openSUSE、Fedora、RHEL と CentOS、Gentoo、Alpine の各系統を識別する。出力されたコマンドは実行前に確認する必要がある。

## 安全上の注意

実際のインストールは選択したディスクに書き込む。設定ファイルからの実行は、消去を再確認せずに開始する。`wipe = true`、パーティションの削除、ファイルシステムの作成は既存データを破壊する可能性がある。

実際のインストール前に、dry-run の出力でディスクセレクタとすべての破壊的操作を確認する必要がある。`/dev/sda` のような名前より、安定した `/dev/disk/by-id/` セレクタが望ましい。必要なデータは別の場所にバックアップする必要がある。

## インストール

現在の `master` アーカイブをダウンロードしてメニューを開く。

```sh
curl -fsSL https://github.com/Zakkaus/gentoo-install/archive/refs/heads/master.tar.gz | tar xz
cd gentoo-install-master
./bootstrap.sh
```

メニューには、画面サイズが 80x24 以上の対話型端末が必要である。開始時にインターフェイス言語の選択を一度求める。`--lang ja` を指定すると、この質問を省略して日本語を選択する。

メニューは回答を `my-install.toml` に保存して終了できる。設定ファイルを使用する次の手順では、実際のインストール前に完全な計画を出力する。

```sh
./bootstrap.sh --config my-install.toml --dry-run
./bootstrap.sh --config my-install.toml
./bootstrap.sh --config my-install.toml --no-shell   # root シェルを開く確認を省略
```

対話型インストールでは、成功時と失敗時のどちらでも、アンマウント前にターゲットシステム内で root シェルを開く選択肢を提示する。`--no-shell` はこの確認を省略する。

## 中断した実行の再開

`--resume` は、ジャーナルで完了済みと記録された操作を省略する。

```sh
./bootstrap.sh --config my-install.toml --resume
```

再開は同じライブセッションに限られる。既定のジャーナルは `/run/gentoo-install/install.jsonl` にあるため、再起動後には残らない。ジャーナルの各項目はその操作の実装と自身の値のダイジェストを記録するため、コードまたは値が変更された操作は省略されずに再度実行される。設定全体のダイジェストは記録されない。

## 設定ファイル

設定ファイルは TOML 形式である。トップレベルの `config_version` フィールドがスキーマバージョンを指定する。ストレージはデバイスグラフで表す。各デバイスは `id` を持ち、デバイスはほかのデバイスを `id` で参照する。セレクタは実際のインストール時にのみ解決される。

[`tests/fixtures/vm-binpkg.toml`](tests/fixtures/vm-binpkg.toml) は、UEFI と ext4 を使用する完全なスキーマ参照例である。ほかの [`tests/fixtures/`](tests/fixtures/) ファイルは BIOS、LUKS2、LVM、mdraid、ZFS、btrfs subvolume、デスクトップを扱う。これらは仮想マシン用のディスクセレクタとテスト用認証情報を含むため、実機にそのまま使用してはならない。

解析と計画ではストレージハードウェアを調査しないため、ターゲットディスクがないマシンでも `--dry-run` で設定を確認できる。

## バイナリパッケージ

バイナリパッケージは任意である。無効にした場合もソースからビルドできる。公式 binhost と gentoo-zh binhost は別々の選択肢であり、それぞれ独立した信頼設定を持つ。現在のエンドツーエンド検証ベースラインは、binhost に接続できない場合、署名がない場合、鍵が信頼されていない場合を対象としていない。これらのフォールバック経路は、検証状況の未検証項目として記載されている。

## 終了コード

`0` は完了、`1` は設定エラー、`2` は preflight の失敗、`3` は完全性検証の失敗、`4` は外部コマンドの失敗、`5` は操作者による中止を意味する。

## 開発への参加

開発環境、アーキテクチャ、必須の検査は [CONTRIBUTING.md](CONTRIBUTING.md) に記載されている。
