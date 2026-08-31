[English](README.md) | [正體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md) | 日本語 | [한국어](README.ko.md)

# gentoo-install

<!-- fact: identity -->

gentoo-install は Linux ライブ環境で動作し、amd64 アーキテクチャの Gentoo システムをインストールするシステムインストーラです。インストール内容は対話式メニューまたは TOML 設定ファイルで指定します。プログラムのインターフェイスは英語、繁体字中国語、簡体字中国語、日本語、韓国語に対応しています。

![インストール内容の各項目を表示するメニュー](screenshot-ja.png)

## 機能の概要

<!-- fact: capability-summary -->

実装範囲には、通常のディスクインストール、ストレージと起動の設定、デスクトップと言語の profile、特殊モードが含まれます。

- **ストレージ**。デバイスグラフはパーティションテーブル、ファイルシステム、LUKS2、LVM、mdraid、ZFS を扱います。
- **起動とシステム**。GRUB、systemd-boot、ZFSBootMenu は設定に応じて UEFI または BIOS の起動を構成します。
- **デスクトップと言語**。GNOME、KDE Plasma、Xfce、CJK フォント、入力メソッドは設定項目です。ディスプレイマネージャーは別の項目であり、デスクトップだけを選んだ機器はテキストログインで起動します。
- **特殊モード**。メモリ環境、インプレース変換、スパースイメージ、`dd` には個別の制約があります。

[リファレンス](REFERENCE.md#capabilities)はモデル、制限、特殊モードの手順を定義します。

## 検証状況

<!-- fact: verification-scope -->

[`TESTED.md`](TESTED.md)は、実行した各経路、インストーラのリビジョン、実行環境を記録します。記録したリビジョンがインストーラと一致し、インストールが `0` で終了し、インストール済みシステムが起動し、起動後の設定検査が通過した実行だけが記録に計上されます。

単体、計画、fixture coverage は実装動作を示しますが、インストール済みシステムの起動を証明しません。[`tests/fixtures/`](tests/fixtures/)は設定モデルを検証するものであり、インストール済みのマシンではありません。記録は未検証の組み合わせを示します。

<!-- fact: verification-architecture -->

`gentoo_install/model/architecture.py` は amd64、arm64、x86 の行を持ち、GRUB のターゲット、`CPU_FLAGS_*` 変数、バイナリホストのサブディレクトリ、EFI 実行ファイル名はその行から組み立てられます。検証済みは amd64 だけです。[`TESTED.md`](TESTED.md)に arm64 と x86 の記録はなく、`tests/vm/` が実行するのは amd64 のみです。

## 要件

<!-- fact: requirements-runtime -->

実際のインストールには root 権限、amd64 ターゲット、Python 3.11 以降が必要です。設定ファイルによる dry run には root 権限が必要ありません。インストーラにはサードパーティーの Python 実行時依存関係がありません。

## 安全上の注意

<!-- fact: safety-destructive -->

実際の実行は選択したディスクに書き込みます。設定ファイルによる実行では、消去確認を二度行いません。`wipe = true`、パーティションの削除、ファイルシステムの作成は既存データを破壊する可能性があります。

<!-- fact: safety-review-backup -->

実際の実行前に、dry-run の出力でディスクセレクタとすべての破壊的操作を確認する必要があります。`/dev/sda` のような名前より、安定した `/dev/disk/by-id/` セレクタが望ましいです。保持するデータには、この実行が書き込まないディスク上のバックアップが必要です。

## インストール

<!-- fact: install-download -->

次のコマンドは現在の `master` アーカイブをダウンロードしてメニューを開きます。

```sh
curl -fsSL https://github.com/Zakkaus/gentoo-install/archive/refs/heads/master.tar.gz | tar xz
cd gentoo-install-master
./bootstrap.sh
```

<!-- fact: install-terminal -->

メニューには 80 列 24 行以上の対話型端末が必要です。

<!-- fact: install-config-workflow -->

メニューは回答を `my-install.toml` に保存します。設定ファイルの手順は、保存、dry run、検査、インストールの順です。

```sh
./bootstrap.sh --config my-install.toml --dry-run
# 実際のコマンドを選ぶ前に、表示された計画を検査します。
./bootstrap.sh --config my-install.toml
./bootstrap.sh --config my-install.toml --no-shell   # 同じ実行で root シェルの確認を省略します
```

<!-- fact: install-root-shell -->

対話型実行はアンマウント前にターゲットの root シェルを提示します。`--no-shell` はそのプロンプトを省略します。

## 設定ファイル

<!-- fact: configuration-reference -->

設定ファイルは TOML を使用し、`config_version` がスキーマバージョンを選択します。[設定リファレンス](REFERENCE.md#configuration-files)はすべての永続化キーと検証済みの例を示します。[`tests/fixtures/vm-binpkg.toml`](tests/fixtures/vm-binpkg.toml)はスキーマリファレンスです。この仮想マシン用ディスクセレクタとテスト用認証情報を実機でそのまま使用してはなりません。

## 中断した実行の再開

<!-- fact: resume-limits -->

`--resume` は同じライブセッション、同じインストーラのリビジョン、同じ設定ファイルに限られます。インストーラは一致しない実行を拒否します。既定のジャーナルは `/run/gentoo-install/install.jsonl` にあるため、再起動後には残りません。

```sh
./bootstrap.sh --config my-install.toml --resume
```

## バイナリパッケージ

<!-- fact: binary-packages -->

バイナリパッケージは任意です。無効にしてもソースからのビルドは可能です。公式 binhost と gentoo-zh binhost には別々の信頼設定があります。到達不能な binhost、署名の欠落、信頼されない鍵については、現在エンドツーエンドの証拠がありません。これらの降格経路は未検証です。

## リファレンス

<!-- fact: reference -->

[REFERENCE.md](REFERENCE.md)には、実行時要件、コマンドラインオプション、メモリ環境、インプレース変換、機能と検証の詳細、設定ファイル、バイナリパッケージの信頼、終了コードがあります。

## 開発への参加

<!-- fact: contributing -->

[CONTRIBUTING.md](CONTRIBUTING.md)は開発環境、アーキテクチャ、必要な検査を説明します。

## ライセンス

<!-- fact: license -->

gentoo-install は GNU General Public License のバージョン 2、または受領者が選ぶそれ以降のバージョンのもとで配布されます。バージョン 2 の全文は [LICENSE](LICENSE) にあり、各ソースファイルは `SPDX-License-Identifier: GPL-2.0-or-later` を持ちます。
