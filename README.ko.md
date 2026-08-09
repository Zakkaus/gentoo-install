[English](README.md) | [正體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | 한국어

# gentoo-install

실행 중인 임의의 Linux live 시스템에서 부팅 가능한 Gentoo를 구성하는 설치 도구. 메뉴 또는 설정 파일로 구동한다. 인터페이스는 영어, 번체 중국어, 간체 중국어, 일본어, 한국어를 제공한다.

![설치 도구가 결정하는 항목을 나열한 메뉴](screenshot.png)

![간체 중국어·번체 중국어·일본어·한국어를 그리는 cjktty 콘솔](cjk-console.png)

## 기능

**디스크.** GPT 및 MBR. ext2/3/4, subvolume을 포함한 btrfs, xfs, f2fs, vfat, swap, zram. LUKS2, LVM, 그리고 raid0·raid1·raid5·raid6의 mdraid. ZFS pool과 dataset은 네이티브 암호화, mirror, raidz를 지원한다. 기존 파티션 테이블은 유지할 수 있다. 각 파티션마다 마운트 지점과 포맷 여부를 개별적으로 지정한다.

**부팅.** UEFI와 BIOS의 GRUB, systemd-boot, ZFS 루트에는 ZFSBootMenu. initramfs는 dracut이 생성하며, 모듈 목록은 직접 나열하지 않고 장치 그래프에서 도출한다. 루트 파일 시스템은 initramfs 단계에서 SSH로 잠금을 해제할 수 있다.

**커널.** `::gentoo`의 `sys-kernel/gentoo-kernel-bin`과 `sys-kernel/gentoo-kernel`, 그리고 gentoo-zh overlay의 `sys-kernel/gentoo-cjk-kernel-bin`과 `sys-kernel/gentoo-cjk-kernel`. gentoo-zh의 두 패키지는 [cjktty-patches](https://github.com/gentoo-zh/cjktty-patches)를 적용하여 텍스트 콘솔에 중국어·일본어·한국어를 표시한다. 기본 커널은 같은 위치에 빈칸을 그린다. 위의 두 번째 그림은 해당 패치를 적용한 `7.1.7-gentoo-dist`다.

**시스템.** systemd 또는 OpenRC. wpa_supplicant 또는 iwd를 사용하는 NetworkManager, systemd-networkd, 또는 네트워크 설정 없음. 고정 주소, DNS, 호스트 이름, 시간대. 최초 부팅 시 한 번 실행할 명령이나 스크립트를 지정할 수 있으며, URL에서 가져오는 방식도 지원한다.

**데스크톱.** GNOME, KDE Plasma, Xfce. 디스플레이 관리자는 gdm, sddm, lightdm, greetd. 그래픽은 amdgpu, intel, nvidia, nouveau, radeon, 가상 머신을 지원하며 `VIDEO_CARDS`, 해당 드라이버가 요구하는 USE 플래그, 커널 파라미터를 함께 설정한다. 운영자가 직접 보완하도록 남기지 않는다.

**입력기.** fcitx5와 ibus. Rime은 병음, 주음, 창힐, 오필, 광둥어 방식을 제공한다. 일본어는 Anthy와 Mozc, 한국어는 Hangul. 글꼴은 locale과 별개의 선택 항목이다.

**Portage.** profile, `MAKEOPTS`, `USE`, `ACCEPT_KEYWORDS`, `L10N`, 미러 지역, 저장소 동기화 방식. gentoo-zh와 gig overlay는 선택 사항이며, 선택하면 해당 키와 `package.accept_keywords`도 함께 기록한다. 바이너리 패키지는 공식 호스트와 gentoo-zh에서 가져오며 키는 각각 관리한다.

**모든 기능에 dry run이 있다.** `--dry-run`은 동일한 계획을 바탕으로 실제 실행이 적용할 작업 목록을 출력한다. 따라서 출력 전용 경로가 실행 경로와 어긋날 수 없다. 중단된 설치는 저널을 기준으로 이어서 진행한다. `install.jsonl`은 각 패키지의 출처와 소스 빌드로 전환한 모든 사유를 기록한다. 설정은 비밀번호 해시를 제거한 뒤 pastebin이나 콘솔의 QR 코드로 내보낼 수 있다.

## 요구 사항

root 권한, amd64 대상, Python 3.11 이상. 표준 라이브러리만 사용한다.

시작 시 `packages.gentoo.org`에 대한 연결이 필요하다. 커널 판과 `sys-fs/zfs`가 지원하는 커널 상한은 실행 중에 읽으므로 로컬 ebuild 트리가 필요 없으며, Alpine, Debian, openSUSE, Fedora, Arch, Gentoo의 live 시스템에서 동작한다. 연결할 수 없으면 중단한다. 예외는 `--missing-commands`와 `--config`에 `--dry-run`을 함께 지정한 경우 두 가지다.

`bootstrap.sh`는 `/etc/os-release`를 읽고, 선택한 구성에 필요하지만 해당 머신에 없는 명령을 나열한 뒤 그 배포판의 설치 명령을 출력한다. `apt-get`, `pacman`, `zypper`, `dnf`, `emerge`, `apk`를 지원한다.

## 사용법

```sh
curl -fsSL https://github.com/Zakkaus/gentoo-install/archive/refs/heads/master.tar.gz | tar xz
cd gentoo-install-master
./bootstrap.sh
```

```sh
./bootstrap.sh                                       # 메뉴
./bootstrap.sh --config my-install.toml              # 무인 실행
./bootstrap.sh --dry-run --config my-install.toml    # 작업만 출력하고 디스크는 건드리지 않는다
./bootstrap.sh --config my-install.toml --resume     # 이전에 멈춘 지점부터 이어서 진행한다
./bootstrap.sh --config my-install.toml --no-shell   # 확인 없이 마운트를 해제하고 종료한다
```

메뉴는 실제 터미널을 요구하며 화면은 80x24 이상이어야 한다. 인터페이스 언어는 시작할 때 한 번 묻는다. `--lang ko`는 이 질문을 건너뛴다.

마운트를 해제하기 전에, 설치의 성공 여부와 관계없이 새 시스템 안에서 root 셸을 여는 선택지를 제시한다. 실패한 경우에도 제시한다. 기기를 복구할 수 있는지는 운영자의 판단이며, 마운트를 해제한 뒤 다시 들어가려면 전체 구성을 직접 마운트해야 하기 때문이다. `--no-shell`은 이 질문을 없앤다.

## 설정 파일

TOML. 첫 줄에서 `config_version`을 선언한다. 디스크는 장치 그래프다. 각 장치는 `id`를 가지고, 장치끼리는 `id`로 서로를 참조하며, 장치 경로는 실행 시점에 해석한다.

```toml
config_version = 1

[system]
hostname = "gentoo"
locale = "ko_KR.UTF-8"
init = "systemd"
root_password_hash = "$6$..."   # openssl passwd -6으로 생성한다. 평문은 넣지 않는다

[portage]
profile = "default/linux/amd64/23.0/systemd"   # init과 일치해야 한다

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

`tests/fixtures/`에 동작하는 예제가 있으며 UEFI, BIOS, LUKS2, LVM, mdraid, ZFS, btrfs subvolume, 데스크톱을 포함한다. 구문 해석은 하드웨어를 건드리지 않으므로 대상 디스크가 없는 머신에서도 `--dry-run`으로 설정을 검증할 수 있다.

## 바이너리 패키지

선택 사항이며 유일한 경로가 되는 일은 없다. 보장된 경로는 소스 빌드다. 공식 binhost와 gentoo-zh binhost는 별개의 선택지이며 키도 따로 관리한다. 호스트에 도달할 수 없거나, 서명이 없거나, 키를 신뢰할 수 없는 경우에는 경고를 출력하고 빌드로 전환하며 `install.jsonl`에 사유를 기록한다.

## 종료 코드

`0` 완료, `1` 설정 오류, `2` preflight 실패, `3` 무결성 검증 실패, `4` 외부 명령 실패, `5` 운영자 중단.

## 개발 참여

[CONTRIBUTING.md](CONTRIBUTING.md) 참조.
