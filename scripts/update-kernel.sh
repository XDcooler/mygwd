#!/bin/bash
# Debian kernel packages own initramfs and bootloader hooks. Keep old kernels.
set -euo pipefail

kernelError(){ printf 'mygwd kernel: %s\n' "$*" >&2; }

kernelPlatform(){
local distribution version
distribution=$(. /etc/os-release; printf '%s' "${ID:-}")
version=$(. /etc/os-release; printf '%s' "${VERSION_ID:-}")
[[ $distribution == debian && $version == 13 ]] || {
    kernelError 'Debian 13 is required; this command does not upgrade the OS.'; return 1;
}
}

kernelPackage(){
local architecture running
architecture=$(dpkg --print-architecture) || return 1
running=$(uname -r) || return 1
case "$architecture:$running" in
    amd64:*-cloud-amd64|arm64:*-cloud-arm64) printf 'linux-image-cloud-%s\n' "$architecture";;
    amd64:*-amd64|arm64:*-arm64)
        [[ $running != *-rt-* ]] || { kernelError 'Real-time kernels require their own upgrade procedure.'; return 1; }
        printf 'linux-image-%s\n' "$architecture";;
    *) kernelError 'Only standard Debian amd64/arm64 and cloud kernels are supported.'; return 1;;
esac
}

kernelMain(){
local operation=${1:---check} package candidate backup dependencies image release
[[ $# -le 1 ]] || { kernelError 'Use --check or --install.'; return 2; }
case "$operation" in
    --help|-h)
        printf '%s\n' 'Usage: bash scripts/update-kernel.sh [--check|--install]' \
            '--check: show the APT plan without updating lists or installing packages.' \
            '--install: update package lists and install the matching Debian kernel metapackage.' \
            'Old kernels are kept. Reboot manually during a maintenance window.'
        return 0;;
    --check|--install) :;;
    *) kernelError 'Use --check or --install.'; return 2;;
esac
kernelPlatform || return 1
package=$(kernelPackage) || return 1
if [[ $operation == --install ]]; then
    [[ $EUID == 0 ]] || { kernelError 'Installation requires root.'; return 1; }
    command -v systemd-detect-virt >/dev/null || { kernelError 'systemd-detect-virt is required.'; return 1; }
    if systemd-detect-virt --container --quiet; then
        kernelError 'A container uses its host kernel; update the host instead.'; return 1
    fi
    install -d -m0700 /run/mygwd-kernel || return 1
    exec 9>/run/mygwd-kernel/update.lock
    flock -n 9 || { kernelError 'Another kernel update is running.'; return 1; }
    apt-get -o DPkg::Lock::Timeout=120 -o APT::Update::Error-Mode=any update || return 1
fi
candidate=$(LC_ALL=C apt-cache policy "$package" | awk '/Candidate:/ {print $2; exit}') || return 1
[[ -n $candidate && $candidate != '(none)' ]] || {
    kernelError "No candidate for $package. Check the Debian 13 APT sources and package lists."; return 1;
}
printf 'Running kernel: %s\nKernel metapackage: %s\nAPT candidate: %s\n' "$(uname -r)" "$package" "$candidate"
if [[ $operation == --check ]]; then
    apt-get --simulate --no-remove --no-install-recommends install "$package"
    return $?
fi
mkdir -p /var/backups || return 1
backup=$(mktemp -d /var/backups/mygwd-kernel.XXXXXX) || return 1
uname -r >"$backup/running-kernel.txt" || return 1
dpkg-query -W -f='${binary:Package}\t${Version}\t${Status}\n' |
    awk -F '\t' '$1 ~ /^linux-image-/' >"$backup/packages-before.txt" || return 1
chmod 0600 "$backup/"*.txt || return 1
apt-get -o DPkg::Lock::Timeout=120 --yes --no-remove --no-install-recommends install "$package" || return 1
[[ $(dpkg-query -W -f='${Status}' "$package") == 'install ok installed' ]] || return 1
dependencies=$(dpkg-query -W -f='${Depends}' "$package") || return 1
[[ $dependencies =~ (linux-image-[0-9][a-zA-Z0-9.+~_-]+) ]] || {
    kernelError 'Cannot identify the installed kernel image dependency.'; return 1;
}
image=${BASH_REMATCH[1]}
[[ $(dpkg-query -W -f='${Status}' "$image") == 'install ok installed' ]] || return 1
release=${image#linux-image-}
[[ -s /boot/vmlinuz-$release && -s /boot/initrd.img-$release && -d /lib/modules/$release ]] || {
    kernelError "Kernel or initramfs files are incomplete for $release. Check the package installation before rebooting."; return 1;
}
printf 'Installed kernel: %s\nPackage record: %s\n' "$release" "$backup"
printf '%s\n' 'Old kernels are kept. Schedule a manual reboot, then verify uname -r and service health.'
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then kernelMain "$@"; fi
