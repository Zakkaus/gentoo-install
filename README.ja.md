[English](README.md) | [正體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md) | 日本語 | [한국어](README.ko.md)

# gentoo-install

<!-- fact: identity -->

gentoo-install は Linux ライブ環境で動作し、amd64 アーキテクチャの Gentoo システムをインストールするシステムインストーラです。インストール内容は、対話式メニューまたは TOML 設定ファイルで指定できます。プログラムのインターフェイスは、英語、繁体字中国語、簡体字中国語、日本語、韓国語に対応しています。

![インストール内容の各項目を表示するメニュー](screenshot-ja.png)

![簡体字中国語、繁体字中国語、日本語、韓国語を表示する cjktty コンソール](cjk-console.png)

## 機能

<!-- fact: capability-scope -->

検証状況により狭い範囲が示されている場合を除き、以下の経路は実装済みであり、自動化された単体テストまたは plan テストで検査されています。

<!-- fact: storage-device-graph -->

**ストレージ** デバイスグラフは、GPT と MBR のパーティションテーブル、ext2、ext3、ext4、xfs、f2fs、vfat、subvolume を含む btrfs、swap、LUKS2 暗号化、LVM、mdraid を扱います。既存のパーティションテーブルを保持し、各パーティションに対して保持、フォーマット、削除のいずれかを個別に指定できます。

<!-- fact: zram-system -->

システム設定では、デバイスグラフおよび swap パーティションとは別に zram を設定できます。

<!-- fact: in-place-conversion -->

**インプレース変換。**`[disk]` テーブルで `mode = "in-place"` を設定すると、インストーラはディスクをパーティション分割する代わりに、稼働中のディストリビューションのユーザ空間を Gentoo で置き換えます。レイアウトはマシンから読み取るため、このテーブルはデバイス一覧を持ちません。`/bin`、`/sbin`、`/etc`、`/lib`、`/lib64`、`/usr`、`/var` が置き換えられ、`/home`、`/root`、`/srv`、`/opt` およびそれ以外のすべてのパスはそのまま残ります。`/etc` は統合ではなく置き換えです。

ステージングされたシステムは稼働中のシステムに触れずに `/gentoo-install.new` 以下で構築され、その後 `rename(2)` でディレクトリごとに交換されます。交換より後に実行されるのは、esp またはブートセクタへの書き込みだけです。ルートが LUKS、LVM、mdraid の下にある場合、ルートファイルシステムがこのインストーラで記述できない種類である場合、ルートファイルシステムの空き容量が 10 GiB 未満の場合、ライブメディア上で実行された場合は、いずれも何も書き込む前に理由を挙げて拒否されます。

<!-- fact: boot-system -->

**起動とシステム** インストーラでは、ブートローダーとして GRUB または systemd-boot を選択できます。GRUB は UEFI と BIOS に対応し、systemd-boot は UEFI に対応しています。また、systemd または OpenRC、dracut、locale、キーボードレイアウト、タイムゾーン、ホスト名、DNS、静的アドレス、選択したネットワークマネージャを設定できます。

<!-- fact: desktop-language -->

**デスクトップと言語対応** GNOME、KDE Plasma、Xfce を選択し、GDM、SDDM、LightDM のいずれかと組み合わせられます。グラフィックス設定は AMD、Intel、NVIDIA、仮想マシンに対応しています。パッケージカタログには Fcitx 5、Rime、Anthy、Mozc、Hangul、CJK フォントが含まれます。カーネルの選択肢には、cjktty パッチを含む `sys-kernel/gentoo-cjk-kernel-bin` と `sys-kernel/gentoo-cjk-kernel` があります。

<!-- fact: portage -->

**Portage** 設定項目には、profile、`MAKEOPTS`、`USE`、`ACCEPT_KEYWORDS`、`L10N`、ミラー、リポジトリ同期方式が含まれます。gentoo-zh と gig の overlay は個別に選択できます。インターフェイス言語として `zh-TW`、`zh-CN`、`ja`、`ko` のいずれかを選択すると、gentoo-zh のパッチ適用済みバイナリカーネルとその overlay も選択されます。`en` を選択した場合は自動的に選択されません。公式と gentoo-zh のバイナリパッケージ取得元は、それぞれ独立した設定と鍵を使用します。

<!-- fact: proxy -->

**セッションプロキシ** `[proxy]` テーブルは `kind`、`host`、`port`、任意の `username` と `password`、`bypass` を受け付けます。`kind` は `http`、`https`、`socks5` のいずれかです。`host` が空の場合は直接接続となり、これが既定値です。SOCKS5 は `socks5h://` を導出し、内部ホスト名をプロキシで解決します。メインメニューには値ごとのフィールドとプロキシ種類のメニューがあります。`bypass` はインターフェイスではコンマ区切りの値、TOML ではリストです。

プロキシを選択した後、設定したプロキシは stage3 と署名鍵、メインツリーおよび overlay のバージョン取得、`gitweb.gentoo.org` の ZFS ebuild 取得、`make.conf` と `FETCHCOMMAND`/`RESUMECOMMAND` を通る Portage のダウンロード、`wget`、`curl`、`git`、GnuPG、binhost、overlay、paste のアップロードに使用されます。時計、初期接続検査、メニュー前のミラー検査は設定を取得する前に実行されるため、この設定の対象外です。インストーラは dry-run の説明と公開設定から認証情報を除外します。公開設定とインストール済みシステムには、認証情報を含まないエンドポイントとバイパスリストだけが残ります。

<!-- fact: plan-records -->

**計画と記録** dry run はストレージハードウェアを調査せずに操作計画を表示します。実際のインストールでは、再利用するデバイスから調査した mdraid メタデータを追加したうえで同じ planner を使用するため、ハードウェアに依存する検証結果が変わる場合があります。`install.log` はコマンド出力を記録し、`install.jsonl` は操作、パッケージの取得元、バイナリパッケージのフォールバック理由を記録します。メニューは設定を `paste.gentoozh.org` にアップロードする前に、`password_hash` と `root_password_hash` の値を `removed-before-publishing` に置き換え、プロキシの `username` と `password` は鍵ごと出力しません。その他の設定値はアップロードに残ります。メニューはアップロード先ページのアドレスをテキストと QR コードで表示します。

## 検証状況

<!-- fact: verification-history -->

過去のエンドツーエンド記録では、amd64 Gentoo minimal ISO と、インストーラリビジョン `a71f91b4735469bae8ec76af170201acb967a5fe` および `f7257793f95df4b21ebf2ac6a775a343f6205f1b` が使用されました。これらの記録は、一部の UEFI と BIOS インストール、systemd、OpenRC、ext4、btrfs、xfs、LUKS2、LVM、mdraid、Plasma、公式 binhost を対象としていましたが、その後のインストール経路の変更により、現在は過去の根拠としてのみ扱われます。

<!-- fact: verification-current -->

2026 年 8 月 11 日付のリビジョン付きエンドツーエンド記録は、Arch Linux、openSUSE、Debian、Fedora、自前でビルドした gentoo-cjk minimal ISO からのインストールと起動をそれぞれ 1 回ずつ対象としています。これらの記録は、インストーラリビジョン [`b931ef46fc15ed50385f70467f2bfb0a8d1fd154`](https://github.com/Zakkaus/gentoo-install/commit/b931ef46fc15ed50385f70467f2bfb0a8d1fd154) を対象としています。gentoo-cjk の記録は ZFS と ZFSBootMenu を使用し、ほかの 4 件は ext4 を使用しています。記録されたリビジョンがインストーラと一致し、インストールの終了コードが `0` で、インストール済みシステムが起動し、起動後の設定検査に合格した場合に限り、その実行を現在の根拠として扱います。

2026 年 8 月 16 日付のクラスタ記録は、インストーラリビジョン [`a8bf2f3837b6`](https://github.com/Zakkaus/gentoo-install/commit/a8bf2f3837b6) を対象とし、amd64 Gentoo minimal ISO 上で `vm-luks`、`vm-mdraid`、`vm-xfs`、`vm-btrfs`、`vm-f2fs` の 5 件がそれぞれインストールと起動を完了し、起動後の設定チェックをすべて通過しています。`vm-lvm` はリビジョン `073997aa74d2` で同じチェックを通過しています。

2026-08-17 のクラスタ記録は次を追加で対象とします。`openrc-sdboot` はリビジョン `40ea3d90f1cc`、`vm-binpkg`、`vm-btrfs`、`vm-desktop`、`vm-gnome` は `6ba5530fd3c8`、`vm-xfs` は `304dffa41602`、`vm-f2fs`、`vm-mdraid`、`vm-proxy-dead`、`vm-xfs`、`vm-zram` は `7ac43a1d5050` です。`d2bed50eed48` の記録は `vm-lvm`、`vm-sdboot`、`vm-unlock` を追加します。`vm-unlock` は initramfs の SSH ロック解除に関する最初のクラスタ記録です。

同じ日付で、クラスタではなく単一マシン上の QEMU による記録もあり、クラスタでは駆動できない経路を対象とします。クラスタの BIOS ゲストはカーネル起動までシリアルポートへ何も出力せず、root 以外の API トークンではスクリーンショットのエンドポイントもファームウェア引数の受け渡しも利用できません。したがって `vm-bios`、`vm-bios-luks`、`ext4-bios`、`mbr-edit` は `304dffa41602` で QEMU から記録しています。`zfs-zbm`、`vm-proxy`、`vm-proxy-http` は `15d45598637a` で同様です。プロキシ用の fixture は QEMU のユーザーモードネットワーク経由でホスト上のプロキシに接続しますが、ブリッジ接続のクラスタゲストにはそのアドレスがありません。

インプレース変換にはエンドツーエンドの記録が 2 件あり、いずれもクラスタではなく単一マシンの QEMU によるものです。リビジョン [`bcc090fab621`](https://github.com/Zakkaus/gentoo-install/commit/bcc090fab621) はパーティション上に ext4 ルートを持つ Debian 12 genericcloud イメージ、リビジョン [`71e751cf14a1`](https://github.com/Zakkaus/gentoo-install/commit/71e751cf14a1) は btrfs ルートを持つ Arch Linux クラウドイメージで、後者では `/swap/swapfile` の行が新しい fstab に引き継がれました。いずれも `uname -r` は `6.18.43-gentoo-dist-bin` を報告し、`emerge` が存在し、元のディストリビューションのパッケージマネージャは存在せず、ルートデバイスは変わらず、実行の記録は `/var/log/gentoo-install` に保存されました。2 件とも BIOS です。UEFI、btrfs サブボリューム上のルート、`vm-convert` クラスタフィクスチャは未検証です。

その他の実装済みの組み合わせは、エンドツーエンド未検証です。現在の根拠は、greetd のデスクトップセッション、GNOME 以外での ibus を対象としていません。公式 Gentoo minimal ISO、Alpine または Gig-OS のライブメディア、binhost 障害時のフォールバックも対象としていません。

プロキシ経路には、SOCKS5 の DNS モード、dry-run 出力と公開設定での認証情報の除去、インストール済みシステムに残る認証情報なしのエンドポイントを対象とする単体テストと plan テストがあります。リビジョンを記録したクラスタ実行が逆方向を裏づけます。`vm-proxy-dead` フィクスチャは待ち受けのないポートをプロキシに指定し、インストールは stage3 のダウンロードで `Connection refused` により停止します。ミラーに到達する実行はプロキシが迂回されたことを示します。

リビジョン `4d8512a496d` の二回の実行が順方向を裏づけます。`vm-proxy` はパスワードを要求する SOCKS5 プロキシを通してインストールを完了し、`vm-proxy-http` は HTTP プロキシを通して完了します。いずれも 57 個の操作を書き込み、93 個のパッケージはバイナリホストから、14 個はソースから構築されます。パスワードを要求する HTTP または HTTPS プロキシでは、メインツリーのスナップショットの署名を検証できません。`emerge-webrsync` が gemato に渡すのは認証情報を含まないエンドポイントだけだからです。dirmngr は SOCKS をまったく扱えないため、SOCKS5 では鍵の更新に keyserver への直接経路が必要です。

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

次の完全な設定は、認証情報と 2 つのバイパスホストを持つプロキシを示します。認証情報は例であり、実行前に置き換える必要があります。

```toml
config_version = 1

[proxy]
kind = "socks5"
host = "proxy.example"
port = 1080
username = "operator"
password = "secret"
bypass = ["localhost", "intranet.example"]

[system]
hostname = "proxy-target"
timezone = "UTC"
locales = ["en_US.UTF-8"]
locale = "en_US.UTF-8"
init = "openrc"
root_password_hash = "$6$gentooinst$IR3GrdJ862XljQYDqocr4tKniIRDIT.jQNFzIrHE3U75H6B6YSWZoSYoVd5edSHpqaYBdiNfXHCoIPRVgb9lT/"

[portage]
profile = "default/linux/amd64/23.0"
makeopts = "-j4"

[portage.binhost]
official = false

[bootloader]
firmware = "bios"

[disk]
root = "mnt-root"

[[disk.devices]]
kind = "existing"
id = "disk"
selector = "/dev/disk/by-id/virtio-target0"
wipe = true

[[disk.devices]]
kind = "table"
id = "table"
disk = "disk"
table = "mbr"

[[disk.devices]]
kind = "partition"
id = "rootpart"
table = "table"
index = 1
role = "data"

[[disk.devices]]
kind = "filesystem"
id = "rootfs"
device = "rootpart"
type = "ext4"

[[disk.devices]]
kind = "mountpoint"
id = "mnt-root"
source = "rootfs"
path = "/"
```

## バイナリパッケージ

<!-- fact: binary-packages -->

バイナリパッケージは任意の機能です。無効にしてもソースからビルドできます。公式 binhost と gentoo-zh binhost は別々の選択肢であり、それぞれ異なる信頼設定を使用します。binhost に接続できない場合、署名がない場合、鍵が信頼されていない場合を対象とする現在のエンドツーエンド根拠はなく、これらのフォールバック経路は未検証です。

## 終了コード

<!-- fact: exit-codes -->

`gentoo-install` では、`0` は正常終了、`1` は設定エラーを意味します。`2` は `argparse` の使用方法エラーまたは preflight の失敗、`3` は完全性検証の失敗を意味します。`4` はダウンロード、外部コマンド、OS、未分類のインストーラの失敗、`5` は操作者による中止を意味します。Python CLI の起動前に Python、必須コマンド、root 権限の検査が失敗した場合、`bootstrap.sh` も `1` で終了することがあります。

## よくある質問

<!-- fact: faq-customisation -->

**この種のインストーラーは Gentoo の自由な構成を損なうのか。**

損なわない。基礎的なインストールだけを行って終わる。パーティション、stage3、Portage の設定、カーネル、ブートローダー、および任意のデスクトップである。その後の判断はすべて運用者のものであり、できあがるのは本プロジェクトの構成要素が何も残らない通常の Gentoo である。取り除かれるのは最初の一時間の手間であり、それが Gentoo の導入と、多数の機材や VPS への展開を難しくしている。

## 開発への参加

<!-- fact: contributing -->

[CONTRIBUTING.md](CONTRIBUTING.md) では、開発環境、アーキテクチャ、必要な検査について説明しています。

## ライセンス

<!-- fact: license -->

本プロジェクトは GNU General Public License のバージョン 2、または受領者が選ぶそれ以降のバージョンのもとで配布される。バージョン 2 の全文は [LICENSE](LICENSE) にあり、各ソースファイルは `SPDX-License-Identifier: GPL-2.0-or-later` を持つ。
