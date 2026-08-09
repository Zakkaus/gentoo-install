[English](README.md) | [正體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | 한국어

# gentoo-install

gentoo-install은 지원되는 Linux 라이브 환경에서 amd64 Gentoo 시스템을 설치하는 설치 도구다. 설치 내용은 대화형 메뉴 또는 TOML 설정 파일로 지정한다. 인터페이스는 영어, 번체 중국어, 간체 중국어, 일본어, 한국어로 제공된다.

![설치 과정의 선택 항목을 보여 주는 메뉴](screenshot.png)

![간체 중국어, 번체 중국어, 일본어, 한국어를 표시하는 cjktty 콘솔](cjk-console.png)

## 기능

**저장 장치.** 장치 그래프는 GPT와 MBR, ext2/3/4, subvolume을 포함한 btrfs, xfs, f2fs, vfat, swap, zram, LUKS2, LVM, mdraid를 지원한다. 기존 파티션 테이블을 유지하고 각 파티션에 유지, 포맷, 삭제 중 하나를 지정할 수 있다.

**부팅 및 시스템.** GRUB은 UEFI와 BIOS를 지원하고 systemd-boot는 UEFI를 지원한다. systemd 또는 OpenRC, dracut, locale, 키보드 배열, 시간대, 호스트 이름, DNS, 고정 주소, 선택한 네트워크 관리자를 설정할 수 있다.

**데스크톱 및 언어 지원.** GNOME, KDE Plasma, Xfce를 gdm, sddm, lightdm과 함께 사용할 수 있다. 그래픽 설정은 AMD, Intel, NVIDIA, 가상 머신을 지원한다. 패키지 카탈로그에는 fcitx5, Rime, Anthy, Mozc, Hangul, CJK 글꼴이 포함된다. gentoo-zh의 패치된 커널은 Linux 텍스트 콘솔에 중국어, 일본어, 한국어를 표시할 수 있다.

**Portage.** profile, `MAKEOPTS`, `USE`, `ACCEPT_KEYWORDS`, `L10N`, 미러, 저장소 동기화 방식을 설정할 수 있다. gentoo-zh와 gig overlay는 명시적으로 선택해야 활성화된다. 공식 및 gentoo-zh 바이너리 패키지 소스는 별도의 설정과 키를 사용한다.

**계획 및 기록.** dry run과 실제 설치는 동일한 작업 계획을 사용한다. `install.log`는 명령 출력을 기록하고, `install.jsonl`은 작업, 패키지 소스, 바이너리 패키지에서 소스 빌드로 전환한 사유를 기록한다. 메뉴는 민감한 정보를 제거한 설정을 `paste.gentoozh.org`에 업로드하고, 업로드된 페이지의 주소를 텍스트와 QR 코드로 표시할 수 있다.

## 검증 상태

기록된 최신 엔드투엔드 검증 기준은 일부 UEFI와 BIOS 설치, systemd, OpenRC, ext4, btrfs, xfs, LUKS2, LVM, mdraid, Plasma, 공식 binhost를 포함한다. 유효한 기록은 설치 도구의 리비전을 명시하고 설치된 시스템이 부팅된 후에만 증거로 인정된다.

현재 리비전의 기준에는 ZFS와 ZFSBootMenu, initramfs의 SSH 원격 잠금 해제, greetd 데스크톱 세션, GNOME 이외 환경의 ibus가 포함되지 않는다. 기본 라이브 미디어 이외의 여섯 가지 라이브 미디어에서 수행하는 설치와 binhost 실패 시 소스 빌드로 전환하는 경로도 기준에 포함되지 않는다. `tests/fixtures/`의 파일은 설정 모델을 검증하며, 파일이 존재한다는 사실만으로 엔드투엔드 지원이 입증되지는 않는다.

## 요구 사항

실제 설치에는 root 권한, amd64 대상, Python 3.11 이상이 필요하다. 설정 파일의 dry run에는 root 권한이 필요하지 않다. Python 표준 라이브러리 외의 런타임 의존성은 없다.

설치 도구를 시작할 때 `packages.gentoo.org`에 연결할 수 있어야 한다. 단, `--missing-commands`와 `--config FILE --dry-run`은 예외다. 커널 버전과 `sys-fs/zfs`가 지원하는 최대 커널 버전은 실행 시점에 조회한다.

`bootstrap.sh`는 `/etc/os-release`를 읽고, 누락된 명령을 보고하며, 후보 패키지 관리자 명령을 출력한다. Debian과 Ubuntu, Arch, openSUSE, Fedora, RHEL과 CentOS, Gentoo, Alpine 계열을 인식한다. 출력된 명령은 실행하기 전에 확인해야 한다.

## 안전

실제 설치는 선택한 디스크에 데이터를 기록한다. 설정 파일로 실행하면 디스크 삭제 여부를 추가로 확인하지 않고 작업을 시작한다. `wipe = true`, 파티션 삭제, 파일 시스템 생성은 기존 데이터를 파괴할 수 있다.

실제 설치 전에 dry-run 출력에서 디스크 선택자와 모든 파괴적 작업을 확인해야 한다. `/dev/sda` 같은 이름보다 안정적인 `/dev/disk/by-id/` 선택자가 더 적합하다. 보존해야 하는 데이터는 별도로 백업해야 한다.

## 설치

현재 `master` 아카이브를 다운로드하고 메뉴를 연다.

```sh
curl -fsSL https://github.com/Zakkaus/gentoo-install/archive/refs/heads/master.tar.gz | tar xz
cd gentoo-install-master
./bootstrap.sh
```

메뉴를 사용하려면 화면 크기가 80x24 이상인 대화형 터미널이 필요하다. 시작할 때 인터페이스 언어를 한 번 묻는다. `--lang ko`를 지정하면 인터페이스 언어를 묻지 않고 한국어를 선택한다.

메뉴는 응답을 `my-install.toml`로 저장하고 종료할 수 있다. 아래 설정 파일 절차에서는 실제 설치 전에 전체 계획을 출력한다.

```sh
./bootstrap.sh --config my-install.toml --dry-run
./bootstrap.sh --config my-install.toml
./bootstrap.sh --config my-install.toml --no-shell   # root 셸 확인을 생략함
```

대화형 설치에서는 성공 또는 실패 후 마운트를 해제하기 전에 대상 시스템 안에서 root 셸을 여는 선택지를 제공한다. `--no-shell`은 이 확인을 생략한다.

## 중단된 실행 재개

`--resume`은 저널에서 완료된 것으로 기록된 작업을 건너뛴다.

```sh
./bootstrap.sh --config my-install.toml --resume
```

재개는 동일한 라이브 세션에서만 사용해야 한다. 기본 저널은 `/run/gentoo-install/install.jsonl`에 있으므로 재부팅 후에는 남아 있지 않는다. 저널의 각 항목은 해당 작업의 구현과 자체 값의 다이제스트를 기록하므로, 코드나 값이 변경된 작업은 건너뛰지 않고 다시 수행된다. 설정 전체의 다이제스트는 기록하지 않는다.

## 설정 파일

설정 파일은 TOML 형식이다. 최상위 `config_version` 필드가 스키마 버전을 지정한다. 저장 장치는 장치 그래프로 표현된다. 각 장치에는 `id`가 있으며, 장치는 다른 장치를 `id`로 참조한다. 선택자는 실제 설치 중에만 해석된다.

[`tests/fixtures/vm-binpkg.toml`](tests/fixtures/vm-binpkg.toml)은 UEFI와 ext4 구성을 모두 포함한 완전한 스키마 참조 파일이다. [`tests/fixtures/`](tests/fixtures/)의 다른 파일은 BIOS, LUKS2, LVM, mdraid, ZFS, btrfs subvolume, 데스크톱을 다룬다. 이 설정 파일들에는 가상 머신 디스크 선택자와 테스트용 인증 정보가 포함되어 있으므로, 내용을 수정하지 않은 채 실제 머신 설치에 사용해서는 안 된다.

구문 분석과 계획 단계에서는 저장 장치 하드웨어를 조사하지 않으므로 대상 디스크가 없는 머신에서도 `--dry-run`으로 설정을 확인할 수 있다.

## 바이너리 패키지

바이너리 패키지는 선택 사항이다. 비활성화해도 소스에서 빌드할 수 있다. 공식 binhost와 gentoo-zh binhost는 별도의 선택 사항이며 각각 독립된 신뢰 설정을 사용한다. 현재 엔드투엔드 검증 기준은 binhost에 연결할 수 없거나 서명이 없거나 키를 신뢰할 수 없는 경우를 다루지 않는다. 따라서 binhost 실패 시 소스 빌드로 전환하는 경로는 검증 상태의 미검증 항목으로 남아 있다.

## 종료 코드

`0`은 완료, `1`은 설정 오류, `2`는 preflight 실패, `3`은 무결성 검증 실패, `4`는 외부 명령 실패, `5`는 운영자 중단을 의미한다.

## 기여

개발 환경, 아키텍처, 필수 검사는 [CONTRIBUTING.md](CONTRIBUTING.md)에 설명되어 있다.
