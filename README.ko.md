[English](README.md) | [正體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | 한국어

# gentoo-install

<!-- fact: identity -->

gentoo-install은 Linux 라이브 환경에서 실행되어 amd64 아키텍처의 Gentoo 시스템을 설치하는 시스템 설치 도구다. 설치 내용은 대화형 메뉴 또는 TOML 설정 파일로 지정할 수 있다. 프로그램 인터페이스는 영어, 번체 중국어, 간체 중국어, 일본어, 한국어를 제공한다.

![설치 과정의 선택 항목을 보여 주는 메뉴](screenshot.png)

![간체 중국어, 번체 중국어, 일본어, 한국어를 표시하는 cjktty 콘솔](cjk-console.png)

## 기능

<!-- fact: capability-scope -->

검증 상태에서 더 좁은 범위를 밝힌 경우를 제외하면 아래 경로는 구현되어 있으며 자동화된 단위 테스트 또는 plan 테스트로 검사된다.

<!-- fact: storage-device-graph -->

**저장 장치** 장치 그래프는 GPT와 MBR 파티션 테이블, ext2, ext3, ext4, xfs, f2fs, vfat, subvolume을 포함하는 btrfs, swap, LUKS2 암호화, LVM, mdraid를 다룬다. 기존 파티션 테이블을 유지할 수 있으며 각 파티션에 유지, 포맷, 삭제 작업을 각각 지정할 수 있다.

<!-- fact: zram-system -->

시스템 설정은 장치 그래프와 swap 파티션과 별도로 zram을 구성할 수 있다.

<!-- fact: boot-system -->

**부팅 및 시스템** 설치 도구에서는 GRUB과 systemd-boot 부트로더를 선택할 수 있다. GRUB은 UEFI와 BIOS를 지원하고 systemd-boot는 UEFI를 지원한다. 설치 도구는 systemd 또는 OpenRC, dracut, locale, 키보드 배열, 시간대, 호스트 이름, DNS, 고정 주소, 선택한 네트워크 관리자를 설정할 수도 있다.

<!-- fact: desktop-language -->

**데스크톱 및 언어 지원** 설치 도구에서는 GNOME, KDE Plasma, Xfce를 선택하고 GDM, SDDM, LightDM 중 하나와 조합할 수 있다. 그래픽 설정은 AMD, Intel, NVIDIA, 가상 머신을 지원한다. 패키지 카탈로그에는 Fcitx 5, Rime, Anthy, Mozc, Hangul, CJK 글꼴이 포함된다. 커널 선택 항목에는 cjktty 패치가 적용된 `sys-kernel/gentoo-cjk-kernel-bin`과 `sys-kernel/gentoo-cjk-kernel`이 포함된다.

<!-- fact: portage -->

**Portage** 설정 항목에는 profile, `MAKEOPTS`, `USE`, `ACCEPT_KEYWORDS`, `L10N`, 미러, 저장소 동기화 방식이 포함된다. gentoo-zh와 gig overlay는 각각 선택할 수 있다. 인터페이스 언어로 `zh-TW`, `zh-CN`, `ja`, `ko` 중 하나를 선택하면 gentoo-zh의 패치된 바이너리 커널과 해당 overlay도 선택된다. `en`을 선택하면 자동으로 선택되지 않는다. 공식 바이너리 패키지 소스와 gentoo-zh 바이너리 패키지 소스는 각각 독립된 설정과 키를 사용한다.

<!-- fact: proxy -->

**세션 프록시** `[proxy]` 테이블은 `url`과 `bypass`를 받는다. URL에는 `http://`, `https://`, `socks5://`, `socks5h://`를 사용할 수 있고 인증 정보도 포함할 수 있다. URL이 비어 있으면 직접 연결하며 이것이 기본값이다. `socks5`는 호스트 이름을 로컬에서 확인하고 `socks5h`는 프록시에서 확인한다. 따라서 라이브 환경이 내부 호스트 이름을 확인하지 못하면 `socks5h`가 필요하다. 주 메뉴에는 URL과 우회 호스트 두 필드가 있는 `Proxy` 행이 있다. URL 필드는 secret으로 처리되어 비밀번호를 표시하지 않는다. `bypass`는 인터페이스에서 쉼표로 구분한 값이고 TOML에서는 목록이다.

프록시를 선택한 뒤 설정한 프록시는 stage3와 서명 키, 메인 트리 및 overlay 버전 조회, `gitweb.gentoo.org`의 ZFS ebuild 조회, `make.conf`와 `FETCHCOMMAND`/`RESUMECOMMAND`를 통한 Portage 다운로드, `wget`, `curl`, `git`, GnuPG, binhost, overlay, paste 업로드에 사용된다. 시계, 초기 연결 검사, 메뉴 전에 실행되는 미러 검사는 설정을 읽기 전에 실행되므로 이 설정의 대상이 아니다. 설치 도구는 dry-run 설명과 공개 설정에서 인증 정보를 제외한다. 공개 설정과 설치된 시스템에는 인증 정보가 없는 엔드포인트와 우회 목록만 남는다.

<!-- fact: plan-records -->

**계획 및 기록** dry run은 저장 장치 하드웨어를 조사하지 않고 작업 계획을 표시한다. 실제 설치는 재사용 장치에서 조사한 mdraid 메타데이터를 추가한 뒤 같은 planner를 사용하므로 하드웨어에 의존하는 검증 결과가 달라질 수 있다. `install.log`는 명령 출력을 기록하고, `install.jsonl`은 작업, 패키지 소스, 바이너리 패키지에서 소스 빌드로 전환한 사유를 기록한다. 메뉴는 설정을 `paste.gentoozh.org`에 업로드하기 전에 `password_hash`와 `root_password_hash` 값만 `removed-before-publishing`으로 바꾼다. 다른 설정값은 업로드에 남는다. 메뉴는 업로드된 페이지의 주소를 텍스트와 QR 코드로 표시한다.

## 검증 상태

<!-- fact: verification-history -->

과거 엔드투엔드 기록은 amd64 Gentoo minimal ISO와 설치 도구 리비전 `a71f91b4735469bae8ec76af170201acb967a5fe` 및 `f7257793f95df4b21ebf2ac6a775a343f6205f1b`를 사용했다. 해당 기록은 일부 UEFI와 BIOS 설치, systemd, OpenRC, ext4, btrfs, xfs, LUKS2, LVM, mdraid, Plasma, 공식 binhost를 다뤘지만 이후 설치 경로가 변경되어 현재는 과거 증거로만 인정된다.

<!-- fact: verification-current -->

2026년 8월 11일 자 리비전 표기 엔드투엔드 기록은 Arch Linux, openSUSE, Debian, Fedora, 자체 빌드한 gentoo-cjk minimal ISO에서 각각 한 번 설치하고 부팅한 결과를 다룬다. 이 기록은 설치 도구 리비전 [`b931ef46fc15ed50385f70467f2bfb0a8d1fd154`](https://github.com/Zakkaus/gentoo-install/commit/b931ef46fc15ed50385f70467f2bfb0a8d1fd154)을 대상으로 한다. gentoo-cjk 기록은 ZFS와 ZFSBootMenu를 사용하며, 나머지 네 건은 ext4를 사용한다. 기록된 리비전이 설치 도구와 일치하고 설치 종료 코드가 `0`이며 설치한 시스템이 부팅되고 부팅 후 설정 검사를 통과한 실행만 현재 증거로 인정된다.

그 밖의 구현된 조합은 엔드투엔드 검증을 거치지 않았다. 현재 증거는 initramfs SSH 잠금 해제, greetd 데스크톱 세션, GNOME 외부의 ibus를 다루지 않는다. 공식 Gentoo minimal ISO, Alpine 또는 Gig-OS 라이브 미디어, binhost 장애 시 전환도 다루지 않는다.

프록시 경로에는 SOCKS5 DNS 모드, dry-run 출력과 공개 설정의 인증 정보 제거, 설치된 시스템에 남는 인증 정보 없는 엔드포인트를 다루는 단위 테스트와 plan 테스트가 있다. 리비전이 기록된 클러스터 실행이 역방향을 뒷받침한다. `vm-proxy-dead` 픽스처는 수신 대기가 없는 포트를 프록시로 지정하며, 설치는 stage3 내려받기 단계에서 `Connection refused`로 중단된다. 미러에 도달하는 실행은 프록시가 우회되었음을 뜻한다. 동작하는 프록시를 통해 설치를 마친 실행은 아직 없으므로 순방향은 검증되지 않았다.

CJK 텍스트 콘솔 표시에도 현재 검증 증거가 없다. ext2와 ext3에는 해당 설정을 다루는 자동화 테스트도 없다. `tests/fixtures/`의 파일은 설정 모델만 검증하며 해당 조합의 설치와 부팅을 입증하지 않는다.

<!-- fact: verification-network -->

IPv4 전용, IPv6 전용, 듀얼 스택 VM 검사는 디스크에 접근하기 전에 끝난다. 이 검사는 주소 계열 감지, `bootstrap.sh --missing-commands`, stage3 pointer 가져오기만 확인한다. stage3 다운로드, 저장소 동기화, binhost 접근, 패키지 설치, 대상 시스템 부팅은 검증하지 않는다.

## 요구 사항

<!-- fact: requirements-runtime -->

실제 설치에는 root 권한, amd64 아키텍처, Python 3.11 이상이 필요하다. 설정 파일로 dry run을 수행할 때는 root 권한이 필요하지 않다. 설치 도구에는 타사 Python 런타임 의존성이 없다.

<!-- fact: requirements-version-sources -->

메뉴는 Gentoo 메인 트리 패키지 버전을 `packages.gentoo.org`에서 읽고 gentoo-zh 패치 커널 버전을 `api.github.com/repos/gentoo-zh/overlay/contents`에서 읽는다. `sys-fs/zfs`가 허용하는 최대 커널 버전은 `gitweb.gentoo.org`에서 읽는다. 설정 파일로 설치할 때는 해당 설정이 지정한 미러에 연결해야 한다. `--missing-commands`와 `--config FILE --dry-run`은 이 버전 제공 지점에 연결하지 않는다.

<!-- fact: requirements-network-filter -->

라이브 환경에 IPv6가 있고 IPv4가 없으면 메뉴는 기록상 IPv4 전용인 Gentoo 미러를 비활성화한다.

<!-- fact: requirements-bootstrap -->

`bootstrap.sh`는 `/etc/os-release`를 읽고, 누락된 명령을 보고하며, 후보 패키지 관리자 명령을 표시한다. 인식하는 배포판 계열은 Debian과 Ubuntu, Arch, openSUSE, Fedora, RHEL과 CentOS, Gentoo, Alpine이다. 표시된 명령은 실행하기 전에 확인해야 한다.

## 안전

<!-- fact: safety-destructive -->

실제 설치는 선택한 디스크에 데이터를 기록한다. 설정 파일로 실행할 때는 디스크 삭제 여부를 다시 확인하지 않는다. `wipe = true`, 파티션 삭제, 파일 시스템 생성은 기존 데이터를 파괴할 수 있다.

<!-- fact: safety-review-backup -->

실제 설치 전에 dry-run 출력에서 디스크 선택자와 모든 파괴적 작업을 확인해야 한다. `/dev/sda` 같은 이름보다 안정적인 `/dev/disk/by-id/` 선택자가 더 적합하다. 보존해야 하는 데이터는 선택한 디스크와 분리된 위치에 별도 백업이 있어야 한다.

## 설치

<!-- fact: install-download -->

다음 명령을 사용하여 현재 `master` 아카이브를 다운로드하고 메뉴를 열 수 있다.

```sh
curl -fsSL https://github.com/Zakkaus/gentoo-install/archive/refs/heads/master.tar.gz | tar xz
cd gentoo-install-master
./bootstrap.sh
```

<!-- fact: install-terminal -->

메뉴에는 80열 24행 이상의 대화형 터미널이 필요하다. 설치 도구는 시작할 때 인터페이스 언어를 한 번 묻는다. `--lang ko`를 사용하면 한국어를 바로 선택할 수 있다.

<!-- fact: install-config-workflow -->

메뉴는 설정을 `my-install.toml`이라는 설정 파일로 저장한 뒤 종료할 수 있다. 다음 설정 파일 작업 절차에서는 전체 설정 계획을 먼저 표시한 다음 실제 설치를 수행한다.

```sh
./bootstrap.sh --config my-install.toml --dry-run
# 이어서 아래 두 줄 가운데 하나만 실행한다. 둘 다 선택한 디스크에 쓴다.
./bootstrap.sh --config my-install.toml
./bootstrap.sh --config my-install.toml --no-shell   # 같은 실행이며 root 셸을 묻지 않는다
```

<!-- fact: install-root-shell -->

대화형 설치에서는 성공하거나 실패한 경우 모두 마운트를 해제하기 전에 대상 시스템 안에서 root 셸을 여는 선택지를 제공한다. `--no-shell`을 사용하면 이 확인을 생략할 수 있다.

## 중단된 실행 재개

<!-- fact: resume-behavior -->

`--resume`은 저널의 위치와 식별자가 현재 계획과 일치하고 재부팅 후에도 효과가 유지된다고 표시된 완료 작업만 건너뛴다.

```sh
./bootstrap.sh --config my-install.toml --resume
```

<!-- fact: resume-limits -->

재개는 동일한 라이브 세션, 동일한 설치 프로그램 리비전, 동일한 설정 파일로 제한된다. 기본 저널은 `/run/gentoo-install/install.jsonl`에 있으므로 재부팅 후에는 남아 있지 않는다. 각 작업 기록에는 해당 작업 클래스의 소스와 필드 값에서 만든 식별자가 포함된다. 식별자가 바뀐 작업은 건너뛰지 않고 다시 수행된다. 공용 헬퍼나 상수의 변경은 그 식별자의 범위 밖이며 저널은 설정 전체의 다이제스트도 기록하지 않으므로, 다른 리비전이나 설정 파일은 문서화된 재개 범위에 포함되지 않는다.

## 설정 파일

<!-- fact: config-model -->

설정 파일은 TOML 형식이다. 최상위 `config_version` 필드가 스키마 버전을 지정한다. 저장 장치는 장치 그래프로 표현된다. 각 장치에는 `id`가 있으며, 장치는 다른 장치를 `id`로 참조한다. 선택자는 실제 설치 중에만 해석된다.

<!-- fact: config-fixtures -->

[`tests/fixtures/vm-binpkg.toml`](tests/fixtures/vm-binpkg.toml)은 UEFI와 Ext4 구성을 모두 포함한 완전한 스키마 참조 파일이다. [`tests/fixtures/`](tests/fixtures/)의 다른 파일은 BIOS, LUKS2, LVM, mdraid, ZFS, Btrfs subvolume, 데스크톱을 다룬다. 이 설정 파일들에는 가상 머신 디스크 선택자와 테스트용 암호가 포함되어 있으므로, 내용을 수정하지 않은 채 실제 머신 설치에 사용해서는 안 된다.

<!-- fact: config-dry-run -->

구문 분석과 계획 단계에서는 저장 장치 하드웨어를 조사하지 않으므로 대상 디스크가 없는 머신에서도 `--dry-run`으로 설정을 확인할 수 있다.

다음 완전한 설정은 인증 정보와 우회 호스트 두 개가 있는 프록시를 보여 준다. 인증 정보는 예시이므로 실행하기 전에 바꿔야 한다.

```toml
config_version = 1

[proxy]
url = "socks5h://operator:secret@proxy.example:1080"
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

## 바이너리 패키지

<!-- fact: binary-packages -->

바이너리 패키지는 선택 사항이다. 비활성화해도 소스에서 빌드할 수 있다. 공식 binhost와 gentoo-zh binhost는 별도의 선택 사항이며 각각 독립된 신뢰 설정을 사용한다. binhost에 연결할 수 없거나 서명이 없거나 키를 신뢰할 수 없는 경우를 다루는 현재 엔드투엔드 증거는 없으며, 이 전환 경로는 검증되지 않았다.

## 종료 코드

<!-- fact: exit-codes -->

`gentoo-install`에서 `0`은 정상 완료, `1`은 설정 오류를 의미한다. `2`는 `argparse` 사용법 오류 또는 preflight 실패, `3`은 무결성 검증 실패를 의미한다. `4`는 다운로드, 외부 명령, OS 또는 분류되지 않은 설치 도구 실패, `5`는 운영자 중단을 의미한다. Python CLI가 시작되기 전에 Python, 필수 명령 또는 root 권한 검사가 실패하면 `bootstrap.sh`도 `1`로 종료될 수 있다.

## 기여

<!-- fact: contributing -->

개발 환경, 아키텍처, 필수 검사는 [CONTRIBUTING.md](CONTRIBUTING.md)에 설명되어 있다.
