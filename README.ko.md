[English](README.md) | [正體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | 한국어

# gentoo-install

gentoo-install은 호환되는 Linux 라이브 환경에서 amd64 아키텍처의 Gentoo 시스템을 설치할 수 있는 시스템 설치 도구다. 설치 내용은 대화형 메뉴 또는 TOML 설정 파일로 구성할 수 있다. 프로그램 인터페이스는 영어, 번체 중국어, 간체 중국어, 일본어, 한국어를 지원한다.

![설치 과정의 선택 항목을 보여 주는 메뉴](screenshot.png)

![간체 중국어, 번체 중국어, 일본어, 한국어를 표시하는 cjktty 콘솔](cjk-console.png)

## 기능

**저장 장치** 장치 그래프는 GPT와 MBR 파티션 테이블, Ext2/3/4, XFS, F2FS, VFAT와 subvolume을 포함하는 Btrfs 등의 파일 시스템, swap, zram, LUKS2 암호화, LVM, mdraid를 지원한다. 파티션을 관리할 때 기존 파티션 테이블을 유지할 수 있으며, 각 파티션에 유지, 포맷, 삭제 작업을 각각 지정할 수 있다.

**부팅 및 시스템** 설치 도구에서는 GRUB과 systemd-boot 부트로더를 선택할 수 있다. GRUB은 UEFI와 BIOS를 지원하고 systemd-boot는 UEFI를 지원한다. 설치 도구는 systemd 또는 OpenRC, dracut, locale, 키보드 배열, 시간대, 호스트 이름, DNS, 고정 주소, 선택한 네트워크 관리자를 설정할 수도 있다.

**데스크톱 및 언어 지원** 설치 도구에서는 GNOME, KDE Plasma, Xfce를 선택하고 GDM, SDDM, LightDM 중 하나와 조합할 수 있다. 그래픽 설정은 AMD, Intel, NVIDIA, 가상 머신을 지원한다. 패키지 카탈로그에는 Fcitx 5, Rime, Anthy, Mozc, Hangul, CJK 글꼴이 포함된다. gentoo-zh에서 제공하는 패치된 커널은 Linux 텍스트 콘솔에 중국어, 일본어, 한국어를 표시할 수 있다.

**Portage** 설정 항목에는 profile, `MAKEOPTS`, `USE`, `ACCEPT_KEYWORDS`, `L10N`, 미러, 저장소 동기화 방식이 포함된다. gentoo-zh와 gig overlay는 명시적으로 활성화해야 한다. 공식 바이너리 패키지 소스와 gentoo-zh 바이너리 패키지 소스는 각각 독립된 설정과 키를 사용한다.

**계획 및 기록** dry run과 실제 설치는 동일한 일련의 작업을 사용한다. `install.log`는 명령 출력을 기록하고, `install.jsonl`은 작업, 패키지 소스, 바이너리 패키지에서 소스 빌드로 전환한 사유를 기록한다. 메뉴는 민감한 정보를 제거한 설정을 `paste.gentoozh.org`에 업로드하고, 업로드된 페이지의 주소를 텍스트와 QR 코드로 표시할 수 있다.

## 검증 상태

기록된 최신 엔드투엔드 검증 기준은 일부 UEFI와 BIOS 설치, systemd, OpenRC, Ext4, Btrfs, XFS, LUKS2, LVM, mdraid, Plasma, 공식 binhost를 포함한다. 유효한 각 기록에는 설치 도구의 리비전이 명시되며, 설치된 시스템이 정상적으로 부팅된 후에만 증거로 인정된다.

현재 리비전의 기준에는 ZFS와 ZFSBootMenu, initramfs의 SSH 원격 잠금 해제, greetd 데스크톱 세션, GNOME 이외 환경의 ibus가 포함되지 않는다. 기본값이 아닌 여섯 종류의 라이브 미디어를 사용한 설치와 binhost 실패 시 소스 빌드로 전환하는 경로도 해당 기준에 포함되지 않는다. `tests/fixtures/`의 파일은 설정 모델만 검증하며, 파일이 존재한다고 해서 해당 조합이 엔드투엔드 검증을 통과한 것은 아니다.

## 요구 사항

실제 설치에는 root 권한, amd64 아키텍처, Python 3.11 이상이 필요하다. 설정 파일로 dry run을 수행할 때는 root 권한이 필요하지 않다. 설치 도구에는 타사 Python 런타임 의존성이 없다.

메뉴는 모든 버전을 `packages.gentoo.org`에서 읽으므로 이 사이트에 연결할 수 있어야 한다. 설정 파일로 설치할 때는 대신 그 설정이 지정한 미러에 연결할 수 있어야 하며, `--missing-commands`와 `--config FILE --dry-run`은 둘 다 연결을 요구하지 않는다. 커널 버전과 `sys-fs/zfs`가 지원하는 최대 커널 버전은 실행 시점에 조회한다.

주소 계열은 둘 중 하나만 있으면 충분하다. 해당 계열에 경로가 없어 실패한 요청은 IPv4로 다시 시도한다. 미러 목록은 IPv4로만 응답하는 사이트를 기록하므로, IPv4 주소가 없는 시스템에서는 메뉴가 그런 사이트를 선택하지 못하게 한다.

`bootstrap.sh`는 `/etc/os-release`를 읽고, 누락된 명령을 보고하며, 후보 패키지 관리자 명령을 표시한다. Debian과 Ubuntu, Arch, openSUSE, Fedora, RHEL과 CentOS, Gentoo, Alpine을 비롯한 여러 배포판 계열을 인식한다. 표시된 명령은 실행하기 전에 확인해야 한다.

## 안전

실제 설치는 선택한 디스크에 데이터를 기록한다. 설정 파일로 실행할 때는 디스크 삭제 여부를 다시 확인하지 않는다. `wipe = true`, 파티션 삭제, 파일 시스템 생성은 기존 데이터를 파괴할 수 있다.

실제 설치 전에 dry-run 출력에서 디스크 선택자와 모든 파괴적 작업을 확인해야 한다. `/dev/sda` 같은 이름보다 안정적인 `/dev/disk/by-id/` 선택자가 더 적합하다. 잘못된 조작으로 인한 데이터 손실을 방지하려면 보존해야 하는 데이터를 반드시 백업해야 한다.

## 설치

다음 명령을 사용하여 현재 `master` 아카이브를 다운로드하고 메뉴를 열 수 있다.

```sh
curl -fsSL https://github.com/Zakkaus/gentoo-install/archive/refs/heads/master.tar.gz | tar xz
cd gentoo-install-master
./bootstrap.sh
```

메뉴에는 80열 24행 이상의 대화형 터미널이 필요하다. 설치 도구는 시작할 때 인터페이스 언어를 한 번 묻는다. `--lang ko`를 사용하면 한국어를 바로 선택할 수 있다.

메뉴는 설정을 `my-install.toml`이라는 설정 파일로 저장한 뒤 종료할 수 있다. 다음 설정 파일 작업 절차에서는 전체 설정 계획을 먼저 표시한 다음 실제 설치를 수행한다.

```sh
./bootstrap.sh --config my-install.toml --dry-run
./bootstrap.sh --config my-install.toml
./bootstrap.sh --config my-install.toml --no-shell   # root 셸을 열지 여부를 묻지 않는다
```

대화형 설치에서는 성공하거나 실패한 경우 모두 마운트를 해제하기 전에 대상 시스템 안에서 root 셸을 여는 선택지를 제공한다. `--no-shell`을 사용하면 이 확인을 생략할 수 있다.

## 중단된 실행 재개

`--resume`은 저널에서 완료된 것으로 기록된 작업을 건너뛴다.

```sh
./bootstrap.sh --config my-install.toml --resume
```

재개는 동일한 라이브 세션으로 제한된다. 기본 저널은 `/run/gentoo-install/install.jsonl`에 있으므로 재부팅 후에는 남아 있지 않는다. 저널의 각 항목은 해당 작업의 구현과 해당 작업 자체의 필드에 대한 다이제스트를 기록하므로, 코드나 필드가 변경된 작업은 건너뛰지 않고 다시 수행된다. 저널은 설정 전체의 다이제스트를 기록하지 않는다.

## 설정 파일

설정 파일은 TOML 형식이다. 최상위 `config_version` 필드가 스키마 버전을 지정한다. 저장 장치는 장치 그래프로 표현된다. 각 장치에는 `id`가 있으며, 장치는 다른 장치를 `id`로 참조한다. 선택자는 실제 설치 중에만 해석된다.

[`tests/fixtures/vm-binpkg.toml`](tests/fixtures/vm-binpkg.toml)은 UEFI와 Ext4 구성을 모두 포함한 완전한 스키마 참조 파일이다. [`tests/fixtures/`](tests/fixtures/)의 다른 파일은 BIOS, LUKS2, LVM, mdraid, ZFS, Btrfs subvolume, 데스크톱을 다룬다. 이 설정 파일들에는 가상 머신 디스크 선택자와 테스트용 암호가 포함되어 있으므로, 내용을 수정하지 않은 채 실제 머신 설치에 사용해서는 안 된다.

구문 분석과 계획 단계에서는 저장 장치 하드웨어를 조사하지 않으므로 대상 디스크가 없는 머신에서도 `--dry-run`으로 설정을 확인할 수 있다.

## 바이너리 패키지

바이너리 패키지는 선택 사항이다. 비활성화해도 소스에서 빌드할 수 있다. 공식 binhost와 gentoo-zh binhost는 별도의 선택 사항이며 각각 독립된 신뢰 설정을 사용한다. 현재 엔드투엔드 검증 기준은 binhost에 연결할 수 없거나 서명이 없거나 키를 신뢰할 수 없는 경우를 다루지 않는다. 따라서 binhost 실패 시 소스 빌드로 전환하는 경로는 검증 상태의 미검증 항목으로 남아 있다.

## 종료 코드

`0`은 완료, `1`은 설정 오류, `2`는 preflight 실패, `3`은 무결성 검증 실패, `4`는 외부 명령 실패, `5`는 운영자 중단을 의미한다.

## 기여

개발 환경, 아키텍처, 필수 검사는 [CONTRIBUTING.md](CONTRIBUTING.md)에 설명되어 있다.
