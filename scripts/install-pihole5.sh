#!/usr/bin/env bash
# Install the last Pi-hole 5 releases required by de_GWD's PHP 7.4 UI and CLI.
# Run --download-only as an ordinary user to prepare the same verified cache.
# A root installation is deliberately limited to Debian 11 with systemd.
set -euo pipefail

readonly GWD_PIHOLE_CORE_TAG=v5.18.4
readonly GWD_PIHOLE_CORE_COMMIT=2cf046d559f2430253fde4e3bf1a89ef4d8f1587
readonly GWD_PIHOLE_WEB_TAG=v5.21
readonly GWD_PIHOLE_WEB_COMMIT=be05b0f61d3fcf796dae4cc3f89f8540b2359325
readonly GWD_PIHOLE_FTL_TAG=v5.25.2
readonly GWD_PIHOLE_INSTALLER_SHA256=2873a66ba68f12e396703501defd922d52f32e2b76d6ccce93bdada150cdaa7d
# These are the unmodified official release .sha1 values, obtained with TLS
# validation. Pin the sidecar too so a writable cache cannot replace both files.
readonly GWD_PIHOLE_FTL_AMD64_SHA1=488a62c785a068179a2c174013870afcec826fd4
readonly GWD_PIHOLE_FTL_ARM64_SHA1=981a5428b7ae5e04a5e92540aba958fd8625485f

gwd_die() { printf 'Pi-hole 5: %s\n' "$*" >&2; exit 1; }

gwd_usage() {
    cat <<'USAGE'
Usage: install-pihole5.sh [options]
  --setup-vars FILE             Existing setupVars.conf (default /etc/pihole/setupVars.conf)
  --ipv4 ADDRESS/PREFIX         Override the configured IPv4 address
  --interface IFACE[,IFACE...]  Override the configured interface(s)
  --dns ADDRESS[#PORT]          Upstream IPv4 DNS; repeat for multiple upstreams
  --password-hash-file FILE     Read a previously generated Pi-hole 5 double-SHA256 hash
  --no-web-server              Keep the web UI for nginx; do not install lighttpd
  --backup-existing-repos      Archive incompatible core/admin trees before replacement
  --cache-dir DIR              Verified artifact cache (default /var/cache/de-gwd/pihole5)
  --download-only              Prepare artifacts without installing; needs --cache-dir
  --architecture amd64|arm64   Artifact architecture (default local dpkg architecture)
  --help                      Show this help

Installation requires root, Debian 11, systemd, curl, git, python3, and an existing or
fully supplied configuration. Passwords and hashes are never command arguments
or log output. No shared/default password is supplied. Cache downloads use
official HTTPS endpoints and the official release checksum without alterations.
USAGE
}

gwd_ipv4() {
    local value=$1 part
    local -a parts
    [[ $value =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
    IFS=. read -r -a parts <<< "$value"
    for part in "${parts[@]}"; do
        [[ ${#part} -le 3 && $part != 0[0-9]* ]] || return 1
        ((10#$part <= 255)) || return 1
    done
}

gwd_dns() {
    local value=$1 address=${1%%#*} port
    gwd_ipv4 "$address" || return 1
    if [[ $value == *'#'* ]]; then
        port=${value#*#}
        [[ $port =~ ^[0-9]{1,5}$ ]] || return 1
        ((10#$port >= 1 && 10#$port <= 65535)) || return 1
    fi
}

gwd_config_key() {
    case "$1" in
        PIHOLE_INTERFACE|IPV4_ADDRESS|IPV6_ADDRESS|QUERY_LOGGING|INSTALL_WEB_SERVER|INSTALL_WEB_INTERFACE|LIGHTTPD_ENABLED|BLOCKING_ENABLED|WEBPASSWORD|DNSMASQ_LISTENING|DNS_FQDN_REQUIRED|DNS_BOGUS_PRIV|DNSSEC|CONDITIONAL_FORWARDING|CONDITIONAL_FORWARDING_IP|CONDITIONAL_FORWARDING_DOMAIN|CONDITIONAL_FORWARDING_REVERSE|REV_SERVER|REV_SERVER_CIDR|REV_SERVER_TARGET|REV_SERVER_DOMAIN|CACHE_SIZE|PRIVACY_LEVEL|API_EXCLUDE_DOMAINS|API_EXCLUDE_CLIENTS|API_QUERY_LOG_SHOW|WEBTHEME|TEMPERATUREUNIT|WEBUIBOXEDLAYOUT|DASHBOARDBOXEDLAYOUT|DHCP_ACTIVE|DHCP_START|DHCP_END|DHCP_ROUTER|DHCP_LEASETIME|DHCP_IPv6|DHCP_rapid_commit|PIHOLE_DOMAIN) return 0;;
    esac
    [[ $1 =~ ^PIHOLE_DNS_[1-9][0-9]*$ ]]
}

gwd_read_config() {
    local file=$1 line key value
    [[ -f $file && ! -L $file ]] || gwd_die 'setupVars.conf must be a regular file.'
    while IFS= read -r line || [[ -n $line ]]; do
        [[ -z $line || $line == '#'* ]] && continue
        [[ $line == *=* ]] || gwd_die 'Invalid setupVars.conf assignment.'
        key=${line%%=*}; value=${line#*=}
        gwd_config_key "$key" || gwd_die "Unsupported configuration key: $key"
        # Do not source user input: reject shell expansion, quotes and commands.
        [[ $value =~ ^[a-zA-Z0-9_.,:/#@%+=-]*$ ]] || gwd_die "Unsafe configuration value for $key"
        [[ ! ${gwd_config[$key]+present} ]] || gwd_die "Duplicate configuration key: $key"
        gwd_config[$key]=$value
    done < "$file"
}

gwd_validate_config() {
    local address=${gwd_config[IPV4_ADDRESS]:-} interface=${gwd_config[PIHOLE_INTERFACE]:-}
    local hash=${gwd_config[WEBPASSWORD]:-} key prefix dns_count=0
    [[ $address == */* ]] || gwd_die 'Supply IPv4 address with a network prefix.'
    prefix=${address#*/}
    gwd_ipv4 "${address%/*}" && [[ $prefix =~ ^[0-9]{1,2}$ ]] && ((10#$prefix <= 32)) || gwd_die 'Invalid IPv4 address or prefix.'
    [[ $interface =~ ^[a-zA-Z0-9_.:-]+(,[a-zA-Z0-9_.:-]+)*$ ]] || gwd_die 'Invalid or missing network interface.'
    [[ $hash =~ ^[a-fA-F0-9]{64}$ && $hash != 0000000000000000000000000000000000000000000000000000000000000000 ]] || gwd_die 'Supply a generated Pi-hole 5 password hash through a file or existing configuration.'
    for key in "${!gwd_config[@]}"; do
        if [[ $key == PIHOLE_DNS_* ]]; then
            # The official installer writes an empty PIHOLE_DNS_2 when only
            # one upstream is configured; it is valid on a repeat run.
            [[ -n ${gwd_config[$key]} ]] || continue
            gwd_dns "${gwd_config[$key]}" || gwd_die "Invalid upstream DNS value for $key"
            ((dns_count+=1))
        fi
    done
    ((dns_count > 0)) && [[ -n ${gwd_config[PIHOLE_DNS_1]:-} ]] || gwd_die 'Supply at least PIHOLE_DNS_1.'
    for ((key=1; key<=dns_count; key++)); do
        [[ -n ${gwd_config[PIHOLE_DNS_$key]:-} ]] || gwd_die 'Upstream DNS keys must be consecutive starting at PIHOLE_DNS_1.'
    done
    for key in QUERY_LOGGING INSTALL_WEB_SERVER INSTALL_WEB_INTERFACE LIGHTTPD_ENABLED BLOCKING_ENABLED DNS_FQDN_REQUIRED DNS_BOGUS_PRIV DNSSEC CONDITIONAL_FORWARDING; do
        if [[ ${gwd_config[$key]+present} ]]; then
            [[ ${gwd_config[$key]} == true || ${gwd_config[$key]} == false ]] || gwd_die "Invalid boolean for $key"
        fi
    done
    gwd_config[QUERY_LOGGING]=${gwd_config[QUERY_LOGGING]:-true}
    gwd_config[INSTALL_WEB_SERVER]=${gwd_config[INSTALL_WEB_SERVER]:-false}
    gwd_config[INSTALL_WEB_INTERFACE]=${gwd_config[INSTALL_WEB_INTERFACE]:-true}
    gwd_config[LIGHTTPD_ENABLED]=${gwd_config[LIGHTTPD_ENABLED]:-false}
    gwd_config[BLOCKING_ENABLED]=${gwd_config[BLOCKING_ENABLED]:-true}
}

gwd_download() {
    local url=$1 output=$2
    printf 'Pi-hole 5: downloading %s\n' "$url" >&2
    # -q prevents a curlrc from silently disabling certificate verification.
    curl -q --proto '=https' --proto-redir '=https' --tlsv1.2 \
        --fail --silent --show-error --location --connect-timeout 20 --max-time 600 \
        "$url" --output "$output"
}

gwd_verify_repo() {
    local directory=$1 commit=$2
    [[ -d $directory/.git ]] || gwd_die "Invalid cached repository: $directory"
    [[ $(git --no-replace-objects -c core.fsmonitor=false -C "$directory" rev-parse HEAD) == "$commit" ]] || gwd_die "Repository version mismatch: $directory"
    git --no-replace-objects -c core.fsmonitor=false -C "$directory" fsck --no-reflogs --full > /dev/null || gwd_die "Corrupt repository: $directory"
    # These index flags can conceal changed files from an ordinary git status.
    [[ -z $(git --no-replace-objects -c core.fsmonitor=false -C "$directory" ls-files -v | sed '/^H /d') ]] || gwd_die "Unverifiable repository index flags: $directory"
    # Compare actual bytes to the pinned tree without running Git clean filters,
    # external diff/fsmonitor hooks, or trusting cached timestamps/index entries.
    python3 - "$directory" "$commit" <<'PY_VERIFY_REPO' || gwd_die "Modified repository: $directory"
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys

root = Path(sys.argv[1])
if root.is_symlink() or (root / '.git').is_symlink():
    raise SystemExit('Repository paths must not be symlinks.')
tree = subprocess.check_output(['git', '--no-replace-objects', '-c', 'core.fsmonitor=false',
                                '-C', str(root), 'ls-tree', '-r', '-z', sys.argv[2]])
expected = set()
for record in tree.split(b'\0'):
    if not record:
        continue
    metadata, name = record.split(b'\t', 1)
    mode, kind, wanted = metadata.split()
    relative = os.fsdecode(name)
    path = root / relative
    if kind != b'blob' or path.parent.resolve() != path.parent.absolute():
        raise SystemExit('Unverifiable repository entry.')
    expected.add(relative)
    status = path.lstat()
    if mode == b'120000':
        if not stat.S_ISLNK(status.st_mode):
            raise SystemExit('Changed repository symlink.')
        data = os.fsencode(os.readlink(path))
    else:
        if mode not in (b'100644', b'100755') or not stat.S_ISREG(status.st_mode):
            raise SystemExit('Changed repository file type.')
        if bool(status.st_mode & 0o111) != (mode == b'100755'):
            raise SystemExit('Changed repository executable permissions.')
        data = path.read_bytes()
    actual = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
    if actual != wanted.decode():
        raise SystemExit('Changed repository file contents.')
actual_files = set()
for directory, names, files in os.walk(root, followlinks=False):
    if Path(directory) == root:
        names[:] = [name for name in names if name != '.git']
    for name in files + [name for name in names if (Path(directory) / name).is_symlink()]:
        actual_files.add(str((Path(directory) / name).relative_to(root)))
if actual_files != expected:
    raise SystemExit('Unexpected or missing repository files.')
PY_VERIFY_REPO
}

gwd_prepare_repo() {
    local directory=$1 url=$2 tag=$3 commit=$4 staging
    if [[ ! -e $directory ]]; then
        staging=$(mktemp -d "${directory}.download.XXXXXX")
        if ! GIT_SSL_NO_VERIFY=false git -c http.sslVerify=true -c advice.detachedHead=false clone --quiet --depth 1 --branch "$tag" "$url" "$staging"; then
            rm -rf -- "$staging"
            gwd_die "Official repository download failed: $url ($tag)"
        fi
        if ! gwd_verify_repo "$staging" "$commit"; then
            rm -rf -- "$staging"
            return 1
        fi
        mv -- "$staging" "$directory"
    fi
    gwd_verify_repo "$directory" "$commit"
}

gwd_ftl_sha1() {
    case "$1" in
        pihole-FTL-linux-x86_64) printf '%s\n' "$GWD_PIHOLE_FTL_AMD64_SHA1";;
        pihole-FTL-aarch64-linux-gnu) printf '%s\n' "$GWD_PIHOLE_FTL_ARM64_SHA1";;
        *) gwd_die 'Unknown FTL artifact name.';;
    esac
}

gwd_verify_ftl_checksum() {
    local directory=$1 binary=$2 trusted_digest=$3 digest recorded extra
    [[ -f $directory/$binary && -f $directory/$binary.sha1 && ! -L $directory/$binary && ! -L $directory/$binary.sha1 ]] || gwd_die 'FTL binary or official checksum is missing or symlinked.'
    # Require exactly the checksum for this binary, not a path supplied by a file.
    [[ $(wc -l < "$directory/$binary.sha1") == 1 ]] || gwd_die 'Malformed official FTL checksum.'
    read -r digest recorded extra < "$directory/$binary.sha1"
    [[ $digest =~ ^[a-fA-F0-9]{40}$ && $recorded == "$binary" && -z $extra ]] || gwd_die 'Malformed official FTL checksum.'
    [[ $digest == "$trusted_digest" ]] || gwd_die 'FTL checksum differs from the pinned official release checksum.'
    (cd "$directory" && sha1sum --status --strict --check "$binary.sha1") || gwd_die 'Official FTL checksum verification failed.'
}

gwd_verify_ftl() { gwd_verify_ftl_checksum "$1" "$2" "$(gwd_ftl_sha1 "$2")"; }

gwd_prepare_ftl() {
    local directory=$1 binary=$2 staging
    local url="https://github.com/pi-hole/FTL/releases/download/$GWD_PIHOLE_FTL_TAG"
    if [[ ! -e $directory ]]; then
        staging=$(mktemp -d "${directory}.download.XXXXXX")
        if ! gwd_download "$url/$binary" "$staging/$binary" || \
           ! gwd_download "$url/$binary.sha1" "$staging/$binary.sha1" || \
           ! gwd_verify_ftl "$staging" "$binary"; then
            rm -rf -- "$staging"
            return 1
        fi
        mv -- "$staging" "$directory"
    fi
    gwd_verify_ftl "$directory" "$binary"
}

gwd_install_repo() {
    local source=$1 destination=$2 commit=$3 staging
    if [[ -e $destination ]]; then
        # Never discard an existing deployment or local changes implicitly.
        gwd_verify_repo "$destination" "$commit"
    else
        mkdir -p -- "$(dirname "$destination")"
        staging=$(mktemp -d "${destination}.install.XXXXXX")
        # Materialize verified git objects rather than copying the cache's
        # working tree, mutable index flags, hooks, config, or owner IDs.
        git -c advice.detachedHead=false clone --quiet --local --no-hardlinks --no-checkout "$source" "$staging"
        git --no-replace-objects -c advice.detachedHead=false -C "$staging" checkout --quiet --detach "$commit"
        case "$commit" in
            "$GWD_PIHOLE_CORE_COMMIT") git -C "$staging" remote set-url origin https://github.com/pi-hole/pi-hole.git;;
            "$GWD_PIHOLE_WEB_COMMIT") git -C "$staging" remote set-url origin https://github.com/pi-hole/web.git;;
            *) gwd_die 'Unknown installation repository pin.';;
        esac
        gwd_verify_repo "$staging" "$commit"
        chmod -R a+rX "$staging"
        chown -R root:root "$staging"
        mv -- "$staging" "$destination"
    fi
}

gwd_check_existing_repo() {
    local destination=$1 commit=$2 backup_base=${3:-} backup
    [[ -e $destination ]] || return 0
    if (gwd_verify_repo "$destination" "$commit") > /dev/null 2>&1; then return 0; fi
    if [[ -z $backup_base ]]; then
        gwd_verify_repo "$destination" "$commit"
        return
    fi
    # Replacement is an explicit option. Preserve the complete previous tree,
    # including its customizations; never remove Pi-hole's configuration/DB.
    install -d -m 0700 "$backup_base"
    backup=$(mktemp -d "$backup_base/repositories.XXXXXX")
    mv -- "$destination" "$backup/$(basename "$destination")"
    printf 'Pi-hole 5: previous repository archived in %s\n' "$backup" >&2
}

gwd_ensure_privacy() {
    local file=$1
    # Upstream's grep pipeline expects this key when the file already exists.
    # Give a partial installation the same default used for a missing file.
    if [[ -f $file ]] && ! grep -q '^PRIVACYLEVEL=' "$file"; then
        printf '\nPRIVACYLEVEL=0\n' >> "$file"
    fi
}

gwd_verify_installed_ftl() {
    local binary_file=$1 artifact_name=$2
    [[ $(sha1sum "$binary_file" | cut -d ' ' -f 1) == "$(gwd_ftl_sha1 "$artifact_name")" ]] || gwd_die 'Installed FTL does not match the official checksum.'
}

gwd_main() {
    local gwd_cache=/var/cache/de-gwd/pihole5 gwd_setup=/etc/pihole/setupVars.conf
    local gwd_download_only=false gwd_cache_supplied=false gwd_architecture=
    local gwd_backup_existing=false gwd_backup_base=
    local gwd_ipv4_override= gwd_interface_override= gwd_hash_file= gwd_no_web=false arg key
    local -a gwd_dns_overrides=()
    declare -gA gwd_config=()
    while (($#)); do
        arg=$1; shift
        case "$arg" in
            --setup-vars|--ipv4|--interface|--dns|--password-hash-file|--cache-dir|--architecture)
                (($#)) || gwd_die "Missing argument for $arg"
                case "$arg" in
                    --setup-vars) gwd_setup=$1;;
                    --ipv4) gwd_ipv4_override=$1;;
                    --interface) gwd_interface_override=$1;;
                    --dns) gwd_dns_overrides+=("$1");;
                    --password-hash-file) gwd_hash_file=$1;;
                    --cache-dir) gwd_cache=$1; gwd_cache_supplied=true;;
                    --architecture) gwd_architecture=$1;;
                esac
                shift;;
            --download-only) gwd_download_only=true;;
            --no-web-server) gwd_no_web=true;;
            --backup-existing-repos) gwd_backup_existing=true;;
            --help|-h) gwd_usage; return 0;;
            *) gwd_die "Unknown argument: $arg";;
        esac
    done
    if [[ $gwd_download_only == true ]]; then
        [[ $gwd_cache_supplied == true ]] || gwd_die '--download-only requires --cache-dir.'
    else
        ((EUID == 0)) || gwd_die 'Installation requires root on the target Debian 11 machine.'
        [[ $(sed -n 's/^ID=//p' /etc/os-release | tr -d '"') == debian && $(sed -n 's/^VERSION_ID=//p' /etc/os-release | tr -d '"') == 11 ]] || gwd_die 'This installer supports Debian 11 only.'
        [[ -d /run/systemd/system ]] || gwd_die 'Installation requires a running systemd target.'
        [[ -f $gwd_setup ]] && gwd_read_config "$gwd_setup"
        [[ -n $gwd_ipv4_override ]] && gwd_config[IPV4_ADDRESS]=$gwd_ipv4_override
        [[ -n $gwd_interface_override ]] && gwd_config[PIHOLE_INTERFACE]=$gwd_interface_override
        if ((${#gwd_dns_overrides[@]})); then
            for key in "${!gwd_config[@]}"; do [[ $key != PIHOLE_DNS_* ]] || unset 'gwd_config[$key]'; done
            for key in "${!gwd_dns_overrides[@]}"; do gwd_config[PIHOLE_DNS_$((key+1))]=${gwd_dns_overrides[$key]}; done
        fi
        if [[ -n $gwd_hash_file ]]; then
            [[ -f $gwd_hash_file && ! -L $gwd_hash_file ]] || gwd_die 'Password hash input must be a regular file.'
            gwd_config[WEBPASSWORD]=$(cat -- "$gwd_hash_file")
        fi
        [[ $gwd_no_web == false ]] || gwd_config[INSTALL_WEB_SERVER]=false
        gwd_validate_config
    fi
    for arg in curl git python3 sha256sum sha1sum flock; do command -v "$arg" > /dev/null || gwd_die "Required command is missing: $arg"; done
    [[ -n $gwd_architecture ]] || gwd_architecture=$(dpkg --print-architecture)
    local gwd_binary
    case "$gwd_architecture" in
        amd64) gwd_binary=pihole-FTL-linux-x86_64;;
        arm64) gwd_binary=pihole-FTL-aarch64-linux-gnu;;
        *) gwd_die 'Supported artifact architectures are amd64 and arm64.';;
    esac
    if [[ $gwd_download_only == false ]]; then
        [[ $gwd_architecture == "$(dpkg --print-architecture)" ]] || gwd_die 'FTL architecture must match the target machine.'
        exec 8>/run/lock/de-gwd-pihole5.lock
        flock --nonblock 8 || gwd_die 'Another Pi-hole 5 installation is running.'
    fi
    mkdir -p -- "$gwd_cache"
    gwd_cache=$(cd "$gwd_cache" && pwd -P)
    exec 9>"$gwd_cache/.lock"
    flock --nonblock 9 || gwd_die 'Another process is preparing this artifact cache.'
    gwd_prepare_repo "$gwd_cache/core" https://github.com/pi-hole/pi-hole.git "$GWD_PIHOLE_CORE_TAG" "$GWD_PIHOLE_CORE_COMMIT"
    gwd_prepare_repo "$gwd_cache/web" https://github.com/pi-hole/web.git "$GWD_PIHOLE_WEB_TAG" "$GWD_PIHOLE_WEB_COMMIT"
    local gwd_installer="$gwd_cache/core/automated install/basic-install.sh"
    [[ $(sha256sum "$gwd_installer" | cut -d ' ' -f 1) == "$GWD_PIHOLE_INSTALLER_SHA256" ]] || gwd_die 'Official installer checksum verification failed.'
    gwd_prepare_ftl "$gwd_cache/ftl-$gwd_architecture" "$gwd_binary"
    if [[ $gwd_download_only == true ]]; then
        printf 'Pi-hole 5: verified core %s, web %s, FTL %s (%s).\n' "$GWD_PIHOLE_CORE_TAG" "$GWD_PIHOLE_WEB_TAG" "$GWD_PIHOLE_FTL_TAG" "$gwd_architecture"
        return 0
    fi
    # Check existing deployments before changing configuration or permissions.
    [[ $gwd_backup_existing == false ]] || gwd_backup_base=/var/backups/de-gwd-pihole5
    gwd_check_existing_repo /etc/.pihole "$GWD_PIHOLE_CORE_COMMIT" "$gwd_backup_base"
    if [[ ${gwd_config[INSTALL_WEB_INTERFACE]} == true ]]; then
        gwd_check_existing_repo /var/www/html/admin "$GWD_PIHOLE_WEB_COMMIT" "$gwd_backup_base"
    fi
    install -d -m 0755 /etc/pihole
    getent group pihole > /dev/null || groupadd --system pihole
    local gwd_config_tmp
    gwd_config_tmp=$(mktemp /etc/pihole/setupVars.conf.XXXXXX)
    chown root:pihole "$gwd_config_tmp"
    chmod 0640 "$gwd_config_tmp"
    for key in "${!gwd_config[@]}"; do printf '%s=%s\n' "$key" "${gwd_config[$key]}" >> "$gwd_config_tmp"; done
    mv -- "$gwd_config_tmp" /etc/pihole/setupVars.conf
    if [[ ${gwd_config[INSTALL_WEB_INTERFACE]} == true ]]; then
        usermod -a -G pihole www-data
        if systemctl is-active --quiet php7.4-fpm; then systemctl restart php7.4-fpm; fi
        runuser -u www-data -- test -r /etc/pihole/setupVars.conf || gwd_die 'The PHP user cannot read its password configuration.'
    fi
    gwd_install_repo "$gwd_cache/core" /etc/.pihole "$GWD_PIHOLE_CORE_COMMIT"
    if [[ ${gwd_config[INSTALL_WEB_INTERFACE]} == true ]]; then
        gwd_install_repo "$gwd_cache/web" /var/www/html/admin "$GWD_PIHOLE_WEB_COMMIT"
    fi
    gwd_ensure_privacy /etc/pihole/pihole-FTL.conf

    # The official script's documented source guard preserves all package,
    # service, gravity, sudoers, and PHP setup, while overriding version choice.
    # Its globals intentionally permit unset variables, so disable nounset here.
    set +u
    SKIP_INSTALL=true source "$gwd_installer" --unattended
    clone_or_update_repos() {
        gwd_verify_repo /etc/.pihole "$GWD_PIHOLE_CORE_COMMIT"
        [[ $INSTALL_WEB_INTERFACE != true ]] || gwd_verify_repo /var/www/html/admin "$GWD_PIHOLE_WEB_COMMIT"
    }
    os_check() {
        # The current remote support matrix belongs to Pi-hole 6. The wrapper
        # already checked the exact Debian 11 target supported by these pins.
        printf 'Pi-hole 5: Debian 11 target verified locally.\n'
    }
    FTLcheckUpdate() { return 0; }
    FTLinstall() {
        [[ $1 == "$gwd_binary" ]] || gwd_die 'Official installer selected a different FTL architecture.'
        gwd_verify_ftl "$gwd_cache/ftl-$gwd_architecture" "$gwd_binary"
        stop_service pihole-FTL > /dev/null 2>&1 || true
        install -T -m 0755 "$gwd_cache/ftl-$gwd_architecture/$gwd_binary" /usr/bin/pihole-FTL || gwd_die 'Installing the verified FTL binary failed.'
        gwd_verify_installed_ftl /usr/bin/pihole-FTL "$gwd_binary"
        [[ $(/usr/bin/pihole-FTL tag) == "$GWD_PIHOLE_FTL_TAG" ]] || gwd_die 'Installed FTL version mismatch.'
    }
    main --unattended
    gwd_verify_installed_ftl /usr/bin/pihole-FTL "$gwd_binary"
    [[ $(/usr/bin/pihole-FTL tag) == "$GWD_PIHOLE_FTL_TAG" ]] || gwd_die 'Final FTL version check failed.'
    [[ $(git -C /etc/.pihole rev-parse HEAD) == "$GWD_PIHOLE_CORE_COMMIT" ]] || gwd_die 'Final core version check failed.'
    if [[ $INSTALL_WEB_INTERFACE == true ]]; then
        [[ $(git -C /var/www/html/admin rev-parse HEAD) == "$GWD_PIHOLE_WEB_COMMIT" ]] || gwd_die 'Final web version check failed.'
    fi
    chown root:pihole /etc/pihole/setupVars.conf
    chmod 0640 /etc/pihole/setupVars.conf
    systemctl is-active --quiet pihole-FTL || gwd_die 'Pi-hole FTL did not start.'
    printf 'Pi-hole 5: fixed-version installation and FTL service verified.\n'
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then gwd_main "$@"; fi
