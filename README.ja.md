[English](README.md) | [正體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md) | 日本語 | [한국어](README.ko.md)

# gentoo-install

<!-- fact: identity -->

gentoo-install は Linux ライブ環境で動作し、amd64 アーキテクチャの Gentoo システムをインストールするシステムインストーラです。インストール内容は、対話式メニューまたは TOML 設定ファイルで指定できます。プログラムのインターフェイスは、英語、繁体字中国語、簡体字中国語、日本語、韓国語に対応しています。

![インストール内容の各項目を表示するメニュー](screenshot.png)

![簡体字中国語、繁体字中国語、日本語、韓国語を表示する cjktty コンソール](cjk-console.png)

## 機能

<!-- fact: capability-scope -->

検証状況により狭い範囲が示されている場合を除き、以下の経路は実装済みであり、自動化された単体テストまたは plan テストで検査されています。

<!-- fact: storage-device-graph -->

**ストレージ** デバイスグラフは、GPT と MBR のパーティションテーブル、ext2、ext3、ext4、xfs、f2fs、vfat、subvolume を含む btrfs、swap、LUKS2 暗号化、LVM、mdraid を扱います。既存のパーティションテーブルを保持し、各パーティションに対して保持、フォーマット、削除のいずれかを個別に指定できます。

<!-- fact: zram-system -->

システム設定では、デバイスグラフおよび swap パーティションとは別に zram を設定できます。

<!-- fact: boot-system -->

**起動とシステム** インストーラでは、ブートローダーとして GRUB または systemd-boot を選択できます。GRUB は UEFI と BIOS に対応し、systemd-boot は UEFI に対応しています。また、systemd または OpenRC、dracut、locale、キーボードレイアウト、タイムゾーン、ホスト名、DNS、静的アドレス、選択したネットワークマネージャを設定できます。

<!-- fact: desktop-language -->

**デスクトップと言語対応** GNOME、KDE Plasma、Xfce を選択し、GDM、SDDM、LightDM のいずれかと組み合わせられます。グラフィックス設定は AMD、Intel、NVIDIA、仮想マシンに対応しています。パッケージカタログには Fcitx 5、Rime、Anthy、Mozc、Hangul、CJK フォントが含まれます。カーネルの選択肢には、cjktty パッチを含む `sys-kernel/gentoo-cjk-kernel-bin` と `sys-kernel/gentoo-cjk-kernel` があります。

<!-- fact: portage -->

**Portage** 設定項目には、profile、`MAKEOPTS`、`USE`、`ACCEPT_KEYWORDS`、`L10N`、ミラー、リポジトリ同期方式が含まれます。gentoo-zh と gig の overlay は個別に選択できます。インターフェイス言語として `zh-TW`、`zh-CN`、`ja`、`ko` のいずれかを選択すると、gentoo-zh のパッチ適用済みバイナリカーネルとその overlay も選択されます。`en` を選択した場合は自動的に選択されません。公式と gentoo-zh のバイナリパッケージ取得元は、それぞれ独立した設定と鍵を使用します。

<!-- fact: plan-records -->

**計画と記録** dry run はストレージハードウェアを調査せずに操作計画を表示します。実際のインストールでは、再利用するデバイスから調査した mdraid メタデータを追加したうえで同じ planner を使用するため、ハードウェアに依存する検証結果が変わる場合があります。`install.log` はコマンド出力を記録し、`install.jsonl` は操作、パッケージの取得元、バイナリパッケージのフォールバック理由を記録します。メニューは設定を `paste.gentoozh.org` にアップロードする前に、`password_hash` と `root_password_hash` の値だけを `removed-before-publishing` に置き換えます。その他の設定値はアップロードに残ります。メニューはアップロード先ページのアドレスをテキストと QR コードで表示します。

## 検証状況

<!-- fact: verification-history -->

過去のエンドツーエンド記録では、amd64 Gentoo minimal ISO と、インストーラリビジョン `a71f91b4735469bae8ec76af170201acb967a5fe` および `f7257793f95df4b21ebf2ac6a775a343f6205f1b` が使用されました。これらの記録は、一部の UEFI と BIOS インストール、systemd、OpenRC、ext4、btrfs、xfs、LUKS2、LVM、mdraid、Plasma、公式 binhost を対象としていましたが、その後のインストール経路の変更により、現在は過去の根拠としてのみ扱われます。

<!-- fact: verification-current -->

2026 年 8 月 11 日付のリビジョン付きエンドツーエンド記録は、Arch Linux、openSUSE、Debian、Fedora、自前でビルドした gentoo-cjk minimal ISO からのインストールと起動をそれぞれ 1 回ずつ対象としています。これらの記録は、インストーラリビジョン [`b931ef46fc15ed50385f70467f2bfb0a8d1fd154`](https://github.com/Zakkaus/gentoo-install/commit/b931ef46fc15ed50385f70467f2bfb0a8d1fd154) を対象としています。gentoo-cjk の記録は ZFS と ZFSBootMenu を使用し、ほかの 4 件は ext4 を使用しています。記録されたリビジョンがインストーラと一致し、インストールの終了コードが `0` で、インストール済みシステムが起動し、起動後の設定検査に合格した場合に限り、その実行を現在の根拠として扱います。

その他の実装済みの組み合わせは、エンドツーエンド未検証です。現在の根拠は、initramfs の SSH ロック解除、greetd のデスクトップセッション、GNOME 以外での ibus を対象としていません。公式 Gentoo minimal ISO、Alpine または Gig-OS のライブメディア、binhost 障害時のフォールバックも対象としていません。

CJK のテキストコンソール表示にも現在の検証根拠はありません。ext2 と ext3 には、各設定に対する自動テストもありません。`tests/fixtures/` のファイルは設定モデルのみを検証し、対応する組み合わせのインストールと起動を証明するものではありません。

<!-- fact: verification-network -->

IPv4 のみ、IPv6 のみ、デュアルスタックの VM 検査は、ディスクへアクセスする前に終了します。この検査は、アドレスファミリの検出、`bootstrap.sh --missing-commands`、stage3 pointer の取得だけを確認します。stage3 のダウンロード、リポジトリ同期、binhost へのアクセス、パッケージのインストール、ターゲットシステムの起動は検証しません。

## 要件

<!-- fact: requirements-runtime -->

実際のインストールには root 権限、amd64 アーキテクチャ、Python 3.11 以降が必要です。設定ファイルを使用する dry run には root 権限が必要ありません。インストーラには Python 標準ライブラリ以外の実行時依存関係がありません。

<!-- fact: requirements-version-sources -->

メニューは、Gentoo メインツリーのパッケージバージョンを `packages.gentoo.org` から、gentoo-zh のパッチ適用済みカーネルのバージョンを `api.github.com/repos/gentoo-zh/overlay/contents` から読み取ります。`sys-fs/zfs` が受け入れる最大カーネルバージョンは `gitweb.gentoo.org` から読み取ります。設定ファイルからのインストールでは、その設定が指定するミラーへの接続が必要です。`--missing-commands` と `--config FILE --dry-run` は、これらのバージョン取得先への接続を必要としません。

<!-- fact: requirements-network-filter -->

ライブ環境に IPv6 があり IPv4 がない場合、メニューは記録上 IPv4 専用の Gentoo ミラーを無効にします。

<!-- fact: requirements-bootstrap -->

`bootstrap.sh` は `/etc/os-release` を読み、不足しているコマンドを報告し、候補となるパッケージマネージャのコマンドを表示します。識別するディストリビューション系統は、Debian と Ubuntu、Arch、openSUSE、Fedora、RHEL と CentOS、Gentoo、Alpine です。表示されたコマンドは実行前に確認する必要があります。

## 安全上の注意

<!-- fact: safety-destructive -->

実際のインストールでは、選択したディスクに書き込みます。設定ファイルを使用して実行する場合、ディスク消去の確認は再度行われません。`wipe = true`、パーティションの削除、ファイルシステムの作成は既存データを破壊する可能性があります。

<!-- fact: safety-review-backup -->

実際のインストール前に、dry-run の出力でディスクセレクタとすべての破壊的操作を確認する必要があります。`/dev/sda` のような名前より、安定した `/dev/disk/by-id/` セレクタが望ましいです。保持する必要があるデータには、選択したディスクとは別のバックアップが必要です。

## インストール

<!-- fact: install-download -->

次のコマンドを使用すると、現在の `master` アーカイブをダウンロードしてメニューを開けます。

```sh
curl -fsSL https://github.com/Zakkaus/gentoo-install/archive/refs/heads/master.tar.gz | tar xz
cd gentoo-install-master
./bootstrap.sh
```

<!-- fact: install-terminal -->

メニューには、80 列 24 行以上の対話型端末が必要です。インストーラは起動時にインターフェイス言語の選択を一度求めます。`--lang ja` を使用すると、日本語を直接選択できます。

<!-- fact: install-config-workflow -->

メニューでは、設定を `my-install.toml` という設定ファイルに保存して終了できます。次の設定ファイルによる操作手順では、最初に完全な設定計画を表示し、その後に実際のインストールを実行します。

```sh
./bootstrap.sh --config my-install.toml --dry-run
# 続いて次のいずれか一方を実行します。どちらも選択したディスクに書き込みます。
./bootstrap.sh --config my-install.toml
./bootstrap.sh --config my-install.toml --no-shell   # 同じ実行で、root シェルの確認をしません
```

<!-- fact: install-root-shell -->

対話型インストールでは、成功時と失敗時のどちらでも、アンマウント前にターゲットシステム内で root シェルを開く選択肢が提示されます。`--no-shell` を使用すると、この確認を省略できます。

## 中断した実行の再開

<!-- fact: resume-behavior -->

`--resume` は、ジャーナル上の位置と識別子が現在の計画に一致し、再起動後も効果が残ると指定された完了済み操作だけを省略します。

```sh
./bootstrap.sh --config my-install.toml --resume
```

<!-- fact: resume-limits -->

再開は同じライブセッション、同じインストーラーリビジョン、同じ設定ファイルに限られます。既定のジャーナルは `/run/gentoo-install/install.jsonl` にあるため、再起動後には残りません。各操作記録には、その操作のクラスのソースとフィールド値から生成された識別子が含まれます。識別子が変わった操作は、省略されずに再度実行されます。共有のヘルパーや定数の変更はその識別子の範囲外であり、設定全体のダイジェストも記録されないため、異なるリビジョンや設定ファイルは文書化された再開範囲に含まれません。

## 設定ファイル

<!-- fact: config-model -->

設定ファイルは TOML 形式です。トップレベルの `config_version` フィールドがスキーマバージョンを指定します。ストレージはデバイスグラフで表します。各デバイスは `id` を持ち、ほかのデバイスを `id` で参照します。セレクタは実際のインストール時にのみ解決されます。

<!-- fact: config-fixtures -->

[`tests/fixtures/vm-binpkg.toml`](tests/fixtures/vm-binpkg.toml) は、UEFI と Ext4 を使用する完全なスキーマ参照です。ほかの [`tests/fixtures/`](tests/fixtures/) ファイルは BIOS、LUKS2、LVM、mdraid、ZFS、Btrfs subvolume、デスクトップを扱います。これらのファイルには仮想マシン用のディスクセレクタとテスト用パスワードが含まれているため、内容を変更せずに実機のインストールに使用してはなりません。

<!-- fact: config-dry-run -->

解析と計画ではストレージハードウェアを調査しないため、ターゲットディスクがないマシンでも `--dry-run` で設定を確認できます。

## バイナリパッケージ

<!-- fact: binary-packages -->

バイナリパッケージは任意の機能です。無効にしてもソースからビルドできます。公式 binhost と gentoo-zh binhost は別々の選択肢であり、それぞれ異なる信頼設定を使用します。binhost に接続できない場合、署名がない場合、鍵が信頼されていない場合を対象とする現在のエンドツーエンド根拠はなく、これらのフォールバック経路は未検証です。

## 終了コード

<!-- fact: exit-codes -->

`gentoo-install` では、`0` は正常終了、`1` は設定エラーを意味します。`2` は `argparse` の使用方法エラーまたは preflight の失敗、`3` は完全性検証の失敗を意味します。`4` はダウンロード、外部コマンド、OS、未分類のインストーラの失敗、`5` は操作者による中止を意味します。Python CLI の起動前に Python、必須コマンド、root 権限の検査が失敗した場合、`bootstrap.sh` も `1` で終了することがあります。

## 開発への参加

<!-- fact: contributing -->

[CONTRIBUTING.md](CONTRIBUTING.md) では、開発環境、アーキテクチャ、必要な検査について説明しています。
