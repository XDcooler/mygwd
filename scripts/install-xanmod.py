#!/usr/bin/env python3
"""Install official XanMod LTS, enable BBR for the new kernel, verify GRUB and reboot.

Only the explicit --install-and-reboot operation mutates a machine. Old kernels
are retained; package installation is not rolled back if boot preparation fails.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile

PACKAGE = 'linux-xanmod-lts-x64v1'
KEY_URL = 'https://dl.xanmod.org/archive.key'
KEY_FINGERPRINT = 'D38D7D1DA1349567ADED882D86F7D09EE734E623'
KEYRING = '/etc/apt/keyrings/mygwd-xanmod.gpg'
SOURCE = '/etc/apt/sources.list.d/mygwd-xanmod.list'
PIN = '/etc/apt/preferences.d/mygwd-xanmod'
GRUB_DEFAULTS = '/etc/default/grub.d/99-mygwd-xanmod.cfg'
GRUB_CONFIG = '/boot/grub/grub.cfg'
GRUB_ENV = '/boot/grub/grubenv'
BBR_CONFIG = '/etc/sysctl.d/90-de-gwd-bbr.conf'
BBR_MODULES = '/etc/modules-load.d/mygwd-xanmod-bbr.conf'
MANAGED_FILES = (KEYRING, SOURCE, PIN, GRUB_DEFAULTS, GRUB_CONFIG, GRUB_ENV,
                 BBR_CONFIG, BBR_MODULES, '/etc/sysctl.conf')
ENV = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
       'HOME': '/root', 'LANG': 'C.UTF-8', 'LC_ALL': 'C', 'DEBIAN_FRONTEND': 'noninteractive'}


class InstallError(RuntimeError):
    pass


def command(argv, *, capture=False, timeout=1800):
    result = subprocess.run(argv, env=ENV, check=False, text=True, timeout=timeout,
                            stdout=subprocess.PIPE if capture else None)
    if result.returncode:
        raise InstallError('Command failed (exit '+str(result.returncode)+'): '+' '.join(argv))
    return result.stdout or ''


class XanModInstaller:
    def __init__(self, root=Path('/'), run=command):
        self.root, self.run = root, run
        self.backup = None
        self.originals = {}
        self.reboot_accepted = False

    def path(self, name):
        return self.root/name.lstrip('/')

    def container_status(self):
        return subprocess.run(['systemd-detect-virt', '--container', '--quiet'], env=ENV).returncode

    @staticmethod
    def atomic(path, data, mode=0o644):
        if path.is_symlink():
            raise InstallError('Refusing symbolic-link destination: '+str(path))
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        fd, temporary = tempfile.mkstemp(prefix='.'+path.name+'.', dir=path.parent)
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, 'wb') as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def preflight(self):
        if os.geteuid() != 0:
            raise InstallError('Installation requires root.')
        values = dict(re.findall(r'^([A-Z_]+)=[\x22\x27]?([^\n\x22\x27]+)',
                                 self.path('/etc/os-release').read_text(), re.M))
        if values.get('ID') != 'debian' or values.get('VERSION_ID') != '13':
            raise InstallError('Debian 13 is required; this command does not upgrade the OS.')
        if self.run(['dpkg', '--print-architecture'], capture=True).strip() != 'amd64':
            raise InstallError('Official XanMod packages require amd64; ARM64 is not supported.')
        for name in ('systemd-detect-virt', 'systemctl', 'update-grub', 'grub-script-check',
                     'grub-editenv', 'apt-get', 'apt-cache', 'dpkg-query', 'python3'):
            if shutil.which(name, path=ENV['PATH']) is None:
                raise InstallError('Required command is missing: '+name)
        if self.container_status() != 1:
            raise InstallError('A VM or physical machine is required; containers cannot replace their kernel.')
        for name in MANAGED_FILES:
            if self.path(name).is_symlink():
                raise InstallError('Refusing symbolic-link destination: '+name)
        if not self.path(GRUB_CONFIG).is_file() or not self.path(GRUB_ENV).is_file():
            raise InstallError('A standard Debian GRUB installation with grub.cfg and grubenv is required.')
        efi = self.path('/sys/firmware/efi')
        if efi.is_dir():
            variables = list((efi/'efivars').glob('SecureBoot-*'))
            if len(variables) != 1 or len(variables[0].read_bytes()) != 5:
                raise InstallError('Cannot verify UEFI Secure Boot state; no kernel installation was started.')
            if variables[0].read_bytes()[4] != 0:
                raise InstallError('Secure Boot is enabled. This installer does not enroll or disable signing keys.')
            if shutil.which('efibootmgr', path=ENV['PATH']) is None:
                raise InstallError('efibootmgr is required to verify the active UEFI GRUB boot entry.')
            entries = self.run(['efibootmgr', '-v'], capture=True)
            current = re.search(r'^BootCurrent:\s*([0-9A-Fa-f]{4})\s*$', entries, re.M)
            active = re.search(r'^Boot'+current[1]+r'\*?\s+(.+)$', entries, re.M | re.I) if current else None
            if not active or not re.search(r'\\EFI\\debian\\(?:shimx64|grubx64)\.efi\b', active[1], re.I):
                raise InstallError('The active UEFI boot entry is not a verified Debian GRUB entry.')
        if self.run(['dpkg', '--audit'], capture=True).strip():
            raise InstallError('Finish pending package configuration before replacing the kernel.')

    def snapshot(self):
        directory = self.path('/var/backups')
        directory.mkdir(parents=True, exist_ok=True)
        self.backup = Path(tempfile.mkdtemp(prefix='mygwd-xanmod.', dir=directory))
        self.backup.chmod(0o700)
        for index, name in enumerate(MANAGED_FILES):
            path = self.path(name)
            if path.exists():
                mode = stat.S_IMODE(path.stat().st_mode)
                data = path.read_bytes()
                self.originals[name] = (data, mode)
                self.atomic(self.backup/str(index), data, 0o600)
            else:
                self.originals[name] = None
        self.atomic(self.backup/'running-kernel.txt',
                    self.run(['uname', '-r'], capture=True).encode(), 0o600)
        self.atomic(self.backup/'packages-before.txt', self.run(
            ['dpkg-query', '-W', '-f=${binary:Package}\t${Version}\t${Status}\n', 'linux-image-*'],
            capture=True).encode(), 0o600)
        self.atomic(self.backup/'manifest.json', json.dumps({name: None if value is None else value[1]
                    for name, value in self.originals.items()}, indent=2).encode(), 0o600)

    def restore(self):
        errors = []
        for name, original in self.originals.items():
            try:
                if original is None:
                    self.path(name).unlink(missing_ok=True)
                else:
                    self.atomic(self.path(name), *original)
            except (OSError, InstallError) as error:
                errors.append(name+': '+str(error))
        if errors:
            raise InstallError('Configuration recovery incomplete; see '+str(self.backup)+': '+'; '.join(errors))

    def add_source(self, staging):
        self.run(['apt-get', '-o', 'DPkg::Lock::Timeout=120', '-o', 'APT::Update::Error-Mode=any', 'update'])
        self.run(['apt-get', '-o', 'DPkg::Lock::Timeout=120', '--yes', '--no-remove',
                  '--no-install-recommends', 'install', 'ca-certificates', 'curl', 'gnupg', 'initramfs-tools'])
        key = staging/'archive.key'
        self.run(['curl', '--fail', '--location', '--proto', '=https', '--proto-redir', '=https',
                  '--connect-timeout', '15', '--max-time', '120', KEY_URL, '-o', str(key)])
        home = staging/'gnupg'; home.mkdir(mode=0o700)
        output = self.run(['gpg', '--batch', '--homedir', str(home), '--with-colons',
                           '--show-keys', str(key)], capture=True)
        primary, fingerprints, awaiting = 0, [], False
        for line in output.splitlines():
            fields = line.split(':')
            if fields[0] == 'pub': primary += 1; awaiting = True
            elif fields[0] == 'fpr' and awaiting:
                fingerprints.append(fields[9]); awaiting = False
        if primary != 1 or fingerprints != [KEY_FINGERPRINT]:
            raise InstallError('The XanMod signing key fingerprint does not match the pinned official key.')
        keyring = staging/'archive.gpg'
        self.run(['gpg', '--batch', '--homedir', str(home), '--dearmor', '--output', str(keyring), str(key)])
        self.atomic(self.path(KEYRING), keyring.read_bytes())
        self.atomic(self.path(SOURCE), ('deb [arch=amd64 signed-by='+KEYRING+
                    '] https://deb.xanmod.org trixie main\n').encode())
        self.atomic(self.path(PIN), b'Package: linux-*xanmod*\nPin: origin "deb.xanmod.org"\nPin-Priority: 999\n')
        self.run(['apt-get', '-o', 'DPkg::Lock::Timeout=120', '-o', 'APT::Update::Error-Mode=any', 'update'])

    @staticmethod
    def dependencies(text):
        # dpkg-query supplies the installed metapackage's concrete dependencies.
        images = re.findall(r'\b(linux-image-[0-9][A-Za-z0-9.+~_-]*xanmod[A-Za-z0-9.+~_-]*)\b', text)
        headers = re.findall(r'\b(linux-headers-[0-9][A-Za-z0-9.+~_-]*xanmod[A-Za-z0-9.+~_-]*)\b', text)
        if len(set(images)) != 1 or len(set(headers)) != 1:
            raise InstallError('Cannot identify exactly one XanMod kernel and headers dependency.')
        image, header = images[0], headers[0]
        if header.removeprefix('linux-headers-') != image.removeprefix('linux-image-'):
            raise InstallError('XanMod image and headers releases do not match.')
        return image, header

    def install_kernel(self):
        policy = self.run(['apt-cache', 'policy', PACKAGE], capture=True)
        match = re.search(r'^\s*Candidate:\s*(\S+)', policy, re.M)
        if not match or not re.fullmatch(r'[0-9][A-Za-z0-9.+:~_-]*', match[1]):
            raise InstallError('No usable official XanMod LTS package candidate was found.')
        candidate = match[1]
        package = PACKAGE+'='+candidate
        self.run(['apt-get', '--simulate', '--no-remove', '--no-install-recommends', 'install', package])
        self.run(['apt-get', '-o', 'DPkg::Lock::Timeout=120', '--yes', '--no-remove',
                  '--no-install-recommends', 'install', package])
        if self.run(['dpkg-query', '-W', '-f=${Version}', PACKAGE], capture=True).strip() != candidate:
            raise InstallError('The installed XanMod metapackage version does not match the selected candidate.')
        dependencies = self.run(['dpkg-query', '-W', '-f=${Depends}', PACKAGE], capture=True)
        image, header = self.dependencies(dependencies)
        for name in (PACKAGE, image, header):
            if self.run(['dpkg-query', '-W', '-f=${Status}', name], capture=True).strip() != 'install ok installed':
                raise InstallError('Kernel package configuration is incomplete: '+name)
        release = image.removeprefix('linux-image-')
        if not self.path('/boot/vmlinuz-'+release).is_file() or not self.path('/lib/modules/'+release).is_dir():
            raise InstallError('Installed XanMod kernel or modules are missing.')
        if not self.path('/boot/vmlinuz-'+release).stat().st_size:
            raise InstallError('Installed XanMod kernel is empty.')
        modules = self.path('/lib/modules/'+release+'/modules.dep')
        if not modules.is_file() or not modules.stat().st_size:
            raise InstallError('The new kernel module dependency index is missing or empty.')
        initrd = self.path('/boot/initrd.img-'+release)
        self.run(['update-initramfs', '-u' if initrd.exists() else '-c', '-k', release])
        if not initrd.is_file() or not initrd.stat().st_size:
            raise InstallError('The new kernel initramfs is missing or empty.')
        contents = self.run(['lsinitramfs', str(initrd)], capture=True)
        if not re.search(r'^(?:usr/)?lib/modules/'+re.escape(release)+r'(?:/|$)', contents, re.M):
            raise InstallError('The initramfs does not contain modules for the installed XanMod release.')
        if self.run(['dpkg', '--audit'], capture=True).strip():
            raise InstallError('Package configuration is incomplete after installing XanMod.')
        return release

    def stage_bbr(self, release):
        # Apply on the new kernel at boot; do not change the running kernel here.
        kernel_config = self.path('/boot/config-'+release)
        if not kernel_config.is_file():
            raise InstallError('Cannot verify BBR support in the new kernel configuration.')
        options = dict(re.findall(r'^(CONFIG_[A-Z0-9_]+)=(y|m)$', kernel_config.read_text(), re.M))
        modules = []
        for option, name in (('CONFIG_TCP_CONG_BBR', 'tcp_bbr'), ('CONFIG_NET_SCH_FQ', 'sch_fq')):
            mode = options.get(option)
            if mode not in ('y', 'm'):
                raise InstallError('The new kernel does not support the required BBR setting: '+option)
            if mode == 'm':
                available = [path for path in self.path('/lib/modules/'+release).rglob(name+'.ko*')
                             if path.is_file() and path.stat().st_size]
                if not available:
                    raise InstallError('The new kernel is missing required module: '+name)
                modules.append(name)
        old = self.path('/etc/sysctl.conf')
        if old.exists():
            data = old.read_bytes()
            clean = b''.join(line for line in data.splitlines(keepends=True) if not re.match(
                rb'^\s*-?net\.(?:core\.default_qdisc|ipv4\.tcp_congestion_control)\s*=', line))
            if clean != data:
                self.atomic(old, clean, stat.S_IMODE(old.stat().st_mode))
        self.atomic(self.path(BBR_CONFIG), b'net.core.default_qdisc=fq\nnet.ipv4.tcp_congestion_control=bbr\n')
        if modules:
            self.atomic(self.path(BBR_MODULES), ('# Load before sysctl applies BBR on the new kernel.\n'+
                        '\n'.join(modules)+'\n').encode())
        else:
            self.path(BBR_MODULES).unlink(missing_ok=True)

    def prepare_boot(self, release):
        self.atomic(self.path(GRUB_DEFAULTS), b'# Managed by mygwd XanMod installation.\n'
                    b'GRUB_DEFAULT=0\nGRUB_SAVEDEFAULT=false\nGRUB_TIMEOUT_STYLE=menu\nGRUB_TIMEOUT=5\n')
        self.run(['update-grub'])
        self.run(['grub-script-check', str(self.path(GRUB_CONFIG))])
        config = self.path(GRUB_CONFIG).read_text()
        defaults = re.findall(r'^\s*set default=[\x22\x27]?([^\s\x22\x27]+)', config, re.M)
        if '0' not in defaults or any(value not in ('0', '${next_entry}') for value in defaults):
            raise InstallError('GRUB did not apply the requested default entry; reboot was stopped.')
        first = re.search(r'^(menuentry|submenu)\s', config, re.M)
        if not first or first[1] != 'menuentry':
            raise InstallError('Cannot verify the first GRUB boot entry.')
        entry = config[first.start():].split('\n}', 1)[0]
        kernels = re.findall(r'^\s*linux(?:efi|16)?\s+(\S+)', entry, re.M)
        initrds = re.findall(r'^\s*initrd(?:efi|16)?\s+(.+)', entry, re.M)
        if len(kernels) != 1 or Path(kernels[0]).name != 'vmlinuz-'+release or not any(
                Path(part).name == 'initrd.img-'+release for line in initrds for part in line.split()):
            raise InstallError('The default GRUB entry does not boot the installed XanMod kernel and initramfs.')
        self.run(['grub-editenv', str(self.path(GRUB_ENV)), 'unset', 'next_entry', 'prev_saved_entry'])
        environment = self.run(['grub-editenv', str(self.path(GRUB_ENV)), 'list'], capture=True)
        if re.search(r'^(?:next_entry|prev_saved_entry)=.+', environment, re.M):
            raise InstallError('A previous GRUB boot override could not be cleared.')

    def request_reboot(self):
        # Shutdown can terminate SSH immediately after systemd accepts the
        # request. Do not restore the old boot target during that handoff.
        previous = {signum: signal.getsignal(signum) for signum in
                    (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)}
        try:
            for signum in previous:
                signal.signal(signum, signal.SIG_IGN)
            self.run(['systemctl', 'reboot'], timeout=30)
            self.reboot_accepted = True
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)

    def install_and_reboot(self):
        self.preflight()
        locks = self.path('/run/mygwd-kernel'); locks.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (locks/'update.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise InstallError('Another kernel operation is running.') from None
            self.snapshot()
            try:
                temporary = self.path('/var/tmp'); temporary.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix='mygwd-xanmod.', dir=temporary) as directory:
                    self.add_source(Path(directory))
                    release = self.install_kernel()
                    self.stage_bbr(release)
                    self.prepare_boot(release)
                record = {'kernel': release, 'package': PACKAGE, 'status': 'ready_to_reboot',
                          'tcp_congestion_control': 'bbr', 'default_qdisc': 'fq'}
                self.atomic(self.backup/'result.json', json.dumps(record, indent=2).encode(), 0o600)
                print('XanMod kernel and default boot entry verified: '+release, flush=True)
                print('BBR + fq configured to activate on the new kernel after reboot.', flush=True)
                print('Old kernels retained. Backup: '+str(self.backup), flush=True)
                self.run(['sync'])
                self.request_reboot()
            except (Exception, KeyboardInterrupt) as error:
                if self.reboot_accepted:
                    print('Reboot was accepted; verified boot settings retained.', flush=True)
                    return
                recovery_error = None
                try:
                    self.restore()
                except (InstallError, OSError) as failure:
                    recovery_error = failure
                try:
                    self.atomic(self.backup/'result.json', json.dumps({
                        'status': 'failed', 'error': str(error),
                        'configuration_restored': recovery_error is None}, indent=2).encode(), 0o600)
                except OSError:
                    print('Could not write failure status in '+str(self.backup), file=sys.stderr)
                if recovery_error is not None:
                    raise InstallError(str(recovery_error)) from error
                print('Boot/source settings restored. Installed packages are retained. Backup: '+str(self.backup),
                      file=sys.stderr)
                raise
            print('Reboot requested. Reconnect and check uname -r.', flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install-and-reboot', action='store_true',
                        help='Install official XanMod LTS, enable BBR at boot, verify boot files and reboot.')
    args = parser.parse_args(argv)
    if not args.install_and_reboot:
        parser.print_help()
        return 2
    os.umask(0o077)
    def interrupted(signum, frame):
        raise InstallError('Kernel operation interrupted by signal '+str(signum))
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    try:
        XanModInstaller().install_and_reboot()
        return 0
    except (InstallError, OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        print('mygwd XanMod: '+str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
