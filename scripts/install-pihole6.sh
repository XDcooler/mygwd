#!/bin/bash
# Fixed official Pi-hole 6 artifacts; keep the v5 verifier for upgrade integrity.
set -euo pipefail
script_directory=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$script_directory/install-pihole5.sh"
readonly GWD6_CORE_TAG=v6.5 GWD6_CORE_COMMIT=f503a65d3a07cb71fd931389f1d3babf718249aa
readonly GWD6_WEB_TAG=v6.6.1 GWD6_WEB_COMMIT=fb699c4e3fd6965d1c9a5253250d8a9b3c6f7a0e
readonly GWD6_FTL_TAG=v6.8
readonly GWD6_INSTALLER_SHA256=0c67c8f8358fede4b4dd8d51b429114461c9bb90ad56ff00ef05637f664bf3ac

gwd_die() { printf 'Pi-hole 6: %s\n' "$*" >&2; exit 1; }

gwd6_digest() {
    case "$1" in
        pihole-FTL-amd64) printf '%s\n' 1f511e7053a5ddfae1db1f439ebb986dd8025bf7223e1f70fb8087d9c0fd2b0e;;
        pihole-FTL-arm64) printf '%s\n' e728cbcffa43b23e3b06fc10faa2fb0a22399d5931fc9881e4007491ee978e6f;;
        *) gwd_die 'Unsupported FTL architecture.';;
    esac
}

gwd6_verify_ftl() {
    local file=$1 name=$2
    [[ -f $file && ! -L $file ]] || gwd_die 'FTL artifact must be a regular file.'
    [[ $(sha256sum "$file" | cut -d ' ' -f 1) == "$(gwd6_digest "$name")" ]] || gwd_die 'Official FTL SHA-256 mismatch.'
}

gwd6_install_repo() {
    local source=$1 destination=$2 commit=$3 url=$4 staging
    if [[ -e $destination ]]; then
        gwd_verify_repo "$destination" "$commit"
        return
    fi
    mkdir -p "$(dirname "$destination")"
    staging=$(mktemp -d "${destination}.install.XXXXXX")
    git -c advice.detachedHead=false clone --quiet --local --no-hardlinks --no-checkout "$source" "$staging"
    git -c advice.detachedHead=false -C "$staging" checkout --quiet --detach "$commit"
    git -C "$staging" remote set-url origin "$url"
    gwd_verify_repo "$staging" "$commit"
    chown -R root:root "$staging"
    chmod -R a+rX "$staging"
    mv "$staging" "$destination"
}

gwd6_redact() {
    # Upstream v5→v6 migration prints password hashes. Do not propagate them.
    python3 -c 'import re,sys; [print(re.sub(r"(?i)(?<![a-f0-9])[a-f0-9]{64}(?![a-f0-9])", "[redacted]", line),end="",flush=True) for line in sys.stdin]'
}

gwd6_headless_logrotate() {
    # FTL does not create a web log when its HTTP listener is disabled.
    [[ -f /etc/logrotate.d/pihole ]] || return 0
    python3 - <<'PY_LOGROTATE'
from pathlib import Path
import re
path = Path('/etc/logrotate.d/pihole')
def optional_web_log(match):
    body = match[2]
    if not re.search(r'^\s*missingok\s*$', body, re.M):
        body = '\n    missingok' + body
    return match[1] + body + match[3]
path.write_text(re.sub(r'(/var/log/pihole/webserver\.log\s*\{)(.*?)(\n\})',
                       optional_web_log, path.read_text(), flags=re.S))
PY_LOGROTATE
}

gwd6_main() {
    local cache=/var/cache/de-gwd/pihole6 setup=/etc/pihole/setupVars.conf
    local architecture= download_only=false backup=false dns_port=53 headless=false argument
    while (($#)); do
        argument=$1; shift
        case "$argument" in
            --cache-dir|--setup-vars|--architecture|--dns-port)
                (($#)) || gwd_die "Missing argument for $argument"
                case "$argument" in
                    --cache-dir) cache=$1;; --setup-vars) setup=$1;;
                    --architecture) architecture=$1;; --dns-port) dns_port=$1;;
                esac
                shift;;
            --download-only) download_only=true;;
            --backup-existing-repos) backup=true;;
            --no-web-server) :;; # Built-in admin always binds loopback, behind nginx.
            --headless) headless=true;;
            --help|-h) printf '%s\n' 'Pi-hole 6 on Debian 13: --setup-vars FILE --cache-dir DIR --dns-port PORT --backup-existing-repos --headless --download-only --architecture amd64|arm64'; return 0;;
            *) gwd_die "Unknown argument: $argument";;
        esac
    done
    [[ $dns_port =~ ^[0-9]{1,5}$ ]] && ((10#$dns_port > 0 && 10#$dns_port <= 65535)) || gwd_die 'Invalid DNS port.'
    [[ -n $architecture ]] || architecture=$(dpkg --print-architecture)
    [[ $architecture == amd64 || $architecture == arm64 ]] || gwd_die 'Only amd64 and arm64 are supported.'
    if [[ $download_only == false ]]; then
        ((EUID == 0)) || gwd_die 'Installation requires root.'
        [[ $(. /etc/os-release; printf '%s:%s' "$ID" "$VERSION_ID") == debian:13 ]] || gwd_die 'Debian 13 is required.'
        [[ -d /run/systemd/system ]] || gwd_die 'A running systemd target is required.'
        [[ $architecture == "$(dpkg --print-architecture)" ]] || gwd_die 'Target architecture mismatch.'
        declare -gA gwd_config=()
        gwd_read_config "$setup"
        gwd_validate_config
        exec 8>/run/lock/de-gwd-pihole6.lock
        flock -n 8 || gwd_die 'Another Pi-hole migration is running.'
    fi
    mkdir -p "$cache"
    cache=$(cd "$cache" && pwd -P)
    exec 9>"$cache/.lock"
    flock -n 9 || gwd_die 'Artifact preparation is already running.'
    gwd_prepare_repo "$cache/core" https://github.com/pi-hole/pi-hole.git "$GWD6_CORE_TAG" "$GWD6_CORE_COMMIT"
    gwd_prepare_repo "$cache/web" https://github.com/pi-hole/web.git "$GWD6_WEB_TAG" "$GWD6_WEB_COMMIT"
    local installer="$cache/core/automated install/basic-install.sh" binary=pihole-FTL-$architecture
    [[ $(sha256sum "$installer" | cut -d ' ' -f 1) == "$GWD6_INSTALLER_SHA256" ]] || gwd_die 'Official installer SHA-256 mismatch.'
    if [[ ! -e $cache/$binary ]]; then
        local artifact
        artifact=$(mktemp "$cache/.ftl.XXXXXX")
        gwd_download "https://github.com/pi-hole/FTL/releases/download/$GWD6_FTL_TAG/$binary" "$artifact"
        gwd6_verify_ftl "$artifact" "$binary"
        mv "$artifact" "$cache/$binary"
    fi
    gwd6_verify_ftl "$cache/$binary" "$binary"
    if [[ $download_only == true ]]; then
        printf 'Official Pi-hole 6 core, web and FTL artifacts verified (%s).\n' "$architecture"
        return 0
    fi
    local backup_directory=
    [[ $backup == false ]] || backup_directory=/var/backups/de-gwd-pihole6
    gwd_check_existing_repo /etc/.pihole "$GWD6_CORE_COMMIT" "$backup_directory"
    gwd_check_existing_repo /var/www/html/admin "$GWD6_WEB_COMMIT" "$backup_directory"
    install -d -m 0755 /var/www /var/www/html
    gwd6_install_repo "$cache/core" /etc/.pihole "$GWD6_CORE_COMMIT" https://github.com/pi-hole/pi-hole.git
    gwd6_install_repo "$cache/web" /var/www/html/admin "$GWD6_WEB_COMMIT" https://github.com/pi-hole/web.git
    if [[ -f /etc/pihole/pihole.toml ]]; then
        # Never run the v5 migration over an already configured v6 installation.
        install -d -m 0700 /var/backups/de-gwd-pihole6
        if [[ -f /etc/pihole/setupVars.conf ]]; then
            mv /etc/pihole/setupVars.conf "/var/backups/de-gwd-pihole6/setupVars.$(date -u +%Y%m%dT%H%M%SZ).conf"
        fi
    else
        # Upstream reads this exact path, even when our input is elsewhere.
        local seed key
        install -d -m 0755 /etc/pihole
        seed=$(mktemp /etc/pihole/.setupVars.XXXXXX)
        for key in "${!gwd_config[@]}"; do printf '%s=%s\n' "$key" "${gwd_config[$key]}"; done >"$seed"
        chmod 0600 "$seed"
        mv "$seed" /etc/pihole/setupVars.conf
    fi
    set +u
    # Vendor scripts create runtime directories with the current umask.
    # Keep private inputs at 0600, but let the pihole service read DNS snippets.
    umask 022
    install -d -m 0755 /etc/dnsmasq.d
    rm -f /etc/dnsmasq.d/99-de-gwd-bootstrap-port.conf
    SKIP_INSTALL=true source "$installer" --unattended
    clone_or_reset_repos() {
        gwd_verify_repo /etc/.pihole "$GWD6_CORE_COMMIT"
        gwd_verify_repo /var/www/html/admin "$GWD6_WEB_COMMIT"
        mark_repos_safe
    }
    os_check() { printf 'Debian 13 target verified locally against the supported platform.\n'; }
    FTLcheckUpdate() { return 0; }
    FTLinstall() {
        [[ $1 == "$binary" ]] || gwd_die 'Unexpected official FTL artifact selection.'
        gwd6_verify_ftl "$cache/$binary" "$binary"
        stop_service pihole-FTL >/dev/null 2>&1 || true
        install -m 0755 "$cache/$binary" /usr/bin/pihole-FTL
    }
    eval "$(declare -f migrate_dnsmasq_configs | sed '1s/migrate_dnsmasq_configs/gwd6_official_migration/')"
    migrate_dnsmasq_configs() {
        gwd6_official_migration | gwd6_redact
        pihole-FTL --config dns.port "$dns_port" >/dev/null
        pihole-FTL --config dns.interface "${gwd_config[PIHOLE_INTERFACE]%%,*}" >/dev/null
        pihole-FTL --config dns.listeningMode LOCAL >/dev/null
        pihole-FTL --config misc.etc_dnsmasq_d true >/dev/null
        pihole-FTL --config ntp.ipv4.active false >/dev/null
        pihole-FTL --config ntp.ipv6.active false >/dev/null
        pihole-FTL --config ntp.sync.active false >/dev/null
        if [[ $headless == true ]]; then
            pihole-FTL --config webserver.port '' >/dev/null
        else
            pihole-FTL --config webserver.port '127.0.0.1:8086' >/dev/null
        fi
    }
    main --unattended
    if [[ $headless == true ]]; then gwd6_headless_logrotate; fi
    gwd6_verify_ftl /usr/bin/pihole-FTL "$binary"
    [[ $(pihole-FTL tag) == "$GWD6_FTL_TAG" ]] || gwd_die 'Installed FTL does not match the fixed release.'
    systemctl is-active --quiet pihole-FTL || gwd_die 'FTL did not start.'
    dig +time=2 +tries=1 @127.0.0.1 -p "$dns_port" pi.hole >/dev/null || gwd_die 'FTL is running but its DNS listener did not respond.'
    printf 'Pi-hole 6 fixed release and service verified.\n'
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then gwd6_main "$@"; fi
