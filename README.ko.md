[English](README.md) | [正體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | 한국어

# gentoo-install

<!-- fact: identity -->

gentoo-install은 Linux 라이브 환경에서 실행되어 amd64 아키텍처의 Gentoo 시스템을 설치하는 시스템 설치 도구다. 설치 내용은 대화형 메뉴 또는 TOML 설정 파일로 지정한다. 프로그램 인터페이스는 영어, 번체 중국어, 간체 중국어, 일본어, 한국어를 제공한다.

![설치 과정의 선택 항목을 보여 주는 메뉴](screenshot-ko.png)

## 기능 개요

<!-- fact: capability-summary -->

구현 범위에는 일반 디스크 설치, 스토리지와 부팅 구성, 데스크톱과 언어 profile, 특수 모드가 포함된다.

- **스토리지.** 장치 그래프는 파티션 테이블, 파일 시스템, LUKS2, LVM, mdraid, ZFS를 다룬다.
- **부팅과 시스템.** GRUB, systemd-boot, ZFSBootMenu는 구성에 따라 UEFI 또는 BIOS 부팅을 구성한다.
- **데스크톱과 언어.** GNOME, KDE Plasma, Xfce, CJK 글꼴, 입력기는 구성 선택지다.
- **특수 모드.** 메모리 환경, 인플레이스 변환, 스파스 이미지, `dd`에는 각각의 제약이 있다.

[참조 문서](REFERENCE.md#capabilities)는 모델, 제한, 특수 모드 절차를 정의한다.

## 검증 상태

<!-- fact: verification-scope -->

[`TESTED.md`](TESTED.md)는 실행한 각 경로, 설치 도구 리비전, 실행 환경을 기록한다. 기록한 리비전이 설치 도구와 일치하고, 설치가 `0`으로 끝나며, 설치된 시스템이 부팅되고, 부팅 후 구성 검사가 통과한 실행만 기록에 포함된다.

단위, 계획, fixture coverage는 구현 동작을 설명하지만 설치된 시스템의 부팅을 증명하지 않는다.[`tests/fixtures/`](tests/fixtures/)는 구성 모델을 검증하며 설치된 시스템이 아니다. 기록은 미검증 조합을 표시한다.

<!-- fact: verification-architecture -->

`gentoo_install/model/architecture.py`는 amd64, arm64, x86 행을 가지며 GRUB 대상, `CPU_FLAGS_*` 변수, 바이너리 호스트 하위 디렉터리, EFI 실행 파일 이름은 그 행에서 구성된다. 검증된 것은 amd64뿐이다. [`TESTED.md`](TESTED.md)에 arm64와 x86 기록은 없고 `tests/vm/`는 amd64만 실행한다.

## 요구 사항

<!-- fact: requirements-runtime -->

실제 설치에는 root 권한, amd64 대상, Python 3.11 이상이 필요하다. 설정 파일로 dry run을 실행할 때는 root 권한이 필요하지 않다. 설치 도구에는 타사 Python 런타임 의존성이 없다.

## 안전

<!-- fact: safety-destructive -->

실제 실행은 선택한 디스크에 기록한다. 설정 파일 실행에는 두 번째 삭제 확인이 없다. `wipe = true`, 파티션 삭제, 파일 시스템 생성은 기존 데이터를 파괴할 수 있다.

<!-- fact: safety-review-backup -->

실제 실행 전에 dry-run 출력에서 디스크 선택자와 모든 파괴적 작업을 확인해야 한다. `/dev/sda` 같은 이름보다 안정적인 `/dev/disk/by-id/` 선택자가 더 적합하다. 보존할 데이터에는 이번 실행이 쓰지 않는 디스크에 별도 백업이 필요하다.

## 설치

<!-- fact: install-download -->

다음 명령은 현재 `master` 아카이브를 다운로드하고 메뉴를 연다.

```sh
curl -fsSL https://github.com/Zakkaus/gentoo-install/archive/refs/heads/master.tar.gz | tar xz
cd gentoo-install-master
./bootstrap.sh
```

<!-- fact: install-terminal -->

메뉴에는 80열 24행 이상의 대화형 터미널이 필요하다.

<!-- fact: install-config-workflow -->

메뉴는 응답을 `my-install.toml`로 저장한다. 설정 파일 작업 순서는 저장, dry run, 검사, 설치다.

```sh
./bootstrap.sh --config my-install.toml --dry-run
# 실제 명령을 선택하기 전에 렌더링된 계획을 검사한다.
./bootstrap.sh --config my-install.toml
./bootstrap.sh --config my-install.toml --no-shell   # 같은 실행이며 root 셸 프롬프트를 생략한다
```

<!-- fact: install-root-shell -->

대화형 실행은 마운트를 해제하기 전에 대상 root 셸을 제시한다. `--no-shell`은 해당 프롬프트를 생략한다.

## 설정 파일

<!-- fact: configuration-reference -->

설정 파일은 TOML을 사용하며 `config_version`이 스키마 버전을 선택한다.[설정 참조 문서](REFERENCE.md#configuration-files)는 모든 영속 키와 검증된 예시를 나열한다.[`tests/fixtures/vm-binpkg.toml`](tests/fixtures/vm-binpkg.toml)은 스키마 참조다. 이 가상 머신 디스크 선택자와 테스트 자격 증명을 실제 시스템에 그대로 사용해서는 안 된다.

## 중단된 실행 재개

<!-- fact: resume-limits -->

`--resume`은 동일한 라이브 세션, 동일한 설치 도구 리비전, 동일한 설정 파일에 한정된다. 설치 도구는 일치하지 않는 실행을 거부한다. 기본 저널은 `/run/gentoo-install/install.jsonl`에 있으므로 재부팅 후에는 남지 않는다.

```sh
./bootstrap.sh --config my-install.toml --resume
```

## 바이너리 패키지

<!-- fact: binary-packages -->

바이너리 패키지는 선택 사항이다. 비활성화해도 소스에서 빌드할 수 있다. 공식 binhost와 gentoo-zh binhost에는 별도의 신뢰 구성이 있다. 도달할 수 없는 binhost, 누락된 서명, 신뢰하지 않는 키는 현재 엔드투엔드 증거가 없다. 이 강등 경로는 미검증 상태다.

## 참조 문서

<!-- fact: reference -->

[REFERENCE.md](REFERENCE.md)에는 런타임 요구 사항, 명령줄 옵션, 메모리 환경, 인플레이스 변환, 기능과 검증의 세부 사항, 설정 파일, 바이너리 패키지 신뢰, 종료 코드가 있다.

## 기여

<!-- fact: contributing -->

[CONTRIBUTING.md](CONTRIBUTING.md)는 개발 환경, 아키텍처, 필요한 검사를 설명한다.

## 라이선스

<!-- fact: license -->

gentoo-install은 GNU General Public License 버전 2 또는 수령자가 선택하는 이후 버전에 따라 배포된다. 버전 2 전문은 [LICENSE](LICENSE)에 있으며, 각 소스 파일은 `SPDX-License-Identifier: GPL-2.0-or-later`를 가진다.
