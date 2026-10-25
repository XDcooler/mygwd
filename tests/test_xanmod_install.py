"""Exercise real kernel preparation logic using private files and fake commands.

No host APT, GRUB, sysctl, kernel installation or reboot is executed, even when
the test process happens to have root privileges.
"""
import contextlib
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('xanmod_install', REPO/'scripts/install-xanmod.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
RELEASE = '6.18.58-x64v1-xanmod1'
VERSION = '6.18.58-xanmod1-0'


class XanModInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mygwd-xanmod-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.events, self.fail = [], None
        self.arch, self.candidate = 'amd64', VERSION
        self.image, self.header = 'linux-image-'+RELEASE, 'linux-headers-'+RELEASE
        self.fingerprint = module.KEY_FINGERPRINT
        self.extra_key = False
        self.bad_status, self.bad_version, self.audit = '', False, ''
        self.omit, self.boot_mode, self.override = '', 'normal', ''
        self.kernel_options = {'CONFIG_TCP_CONG_BBR': 'y', 'CONFIG_NET_SCH_FQ': 'y'}
        self.efi_entry = r'HD(fixture)/File(\EFI\debian\shimx64.efi)'
        self.installer = module.XanModInstaller(self.root, self.run_fake)
        self.put('/etc/os-release', 'ID=debian\nVERSION_ID="13"\n')
        self.put(module.GRUB_CONFIG, 'original grub configuration\n', 0o600)
        self.put(module.GRUB_ENV, 'original environment\n', 0o600)
        self.put('/etc/sysctl.conf', '# retained\nnet.ipv4.ip_forward=1\n'
                 'net.core.default_qdisc = fq_codel\nnet.ipv4.tcp_congestion_control=bbrplus\n')
        self.put('/boot/vmlinuz-previous', 'original kernel')
        self.before = {name: self.installer.path(name).read_bytes() if self.installer.path(name).exists() else None
                       for name in module.MANAGED_FILES}
        stack = contextlib.ExitStack(); self.addCleanup(stack.close)
        stack.enter_context(patch.object(module.os, 'geteuid', return_value=0))
        stack.enter_context(patch.object(module.shutil, 'which', side_effect=lambda name, **kwargs: '/fake/'+name))
        self.container = stack.enter_context(patch.object(self.installer, 'container_status', return_value=1))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))

    def put(self, name, text, mode=0o644):
        path = self.installer.path(name); path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text); path.chmod(mode)
        return path

    def run_fake(self, argv, *, capture=False, timeout=1800):
        self.events.append(argv)
        if self.fail is not None and self.fail(argv):
            raise module.InstallError('Injected command failure')
        name = argv[0]
        if name == 'dpkg': return self.arch if '--print-architecture' in argv else self.audit
        if name == 'uname': return '6.12.99+deb13-amd64\n'
        if name == 'efibootmgr': return 'BootCurrent: 0001\nBoot0001* debian '+self.efi_entry+'\n'
        if name == 'apt-cache': return '  Candidate: '+self.candidate+'\n'
        if name == 'curl': Path(argv[argv.index('-o')+1]).write_text('public key fixture')
        if name == 'gpg':
            if '--show-keys' in argv:
                return 'pub:::::::::\nfpr:::::::::'+self.fingerprint+':\n'+('pub:::::::::\n' if self.extra_key else '')
            Path(argv[argv.index('--output')+1]).write_bytes(b'keyring fixture')
        if name == 'apt-get' and 'install' in argv and any(arg.startswith(module.PACKAGE+'=') for arg in argv) and '--simulate' not in argv:
            self.put(module.GRUB_CONFIG, 'package hook replaced this file\n')
            if self.omit != 'kernel': self.put('/boot/vmlinuz-'+RELEASE, 'kernel fixture')
            self.installer.path('/lib/modules/'+RELEASE).mkdir(parents=True, exist_ok=True)
            if self.omit != 'modules': self.put('/lib/modules/'+RELEASE+'/modules.dep', 'kernel/fixture.ko:\n')
            if self.omit != 'kernel_config':
                self.put('/boot/config-'+RELEASE, ''.join(key+'='+value+'\n' for key, value in self.kernel_options.items()))
            for option, name in (('CONFIG_TCP_CONG_BBR', 'tcp_bbr'), ('CONFIG_NET_SCH_FQ', 'sch_fq')):
                if self.kernel_options.get(option) == 'm' and self.omit != name:
                    self.put('/lib/modules/'+RELEASE+'/kernel/'+name+'.ko.zst', 'module fixture')
        if name == 'dpkg-query':
            if 'linux-image-*' in argv: return 'linux-image-previous\t1\tinstall ok installed\n'
            if '-f=${Depends}' in argv: return self.image+', '+self.header
            if '-f=${Version}' in argv: return '0-bad' if self.bad_version else VERSION
            return 'install ok unpacked' if argv[-1] == self.bad_status else 'install ok installed'
        if name == 'update-initramfs' and self.omit != 'initrd':
            self.put('/boot/initrd.img-'+RELEASE, 'initramfs fixture')
        if name == 'lsinitramfs':
            return 'usr/lib/modules/'+('wrong' if self.omit == 'initrd_modules' else RELEASE)+'/kernel/fixture.ko\n'
        if name == 'update-grub':
            default = 'saved' if self.boot_mode == 'saved' else '0'
            image = 'previous' if self.boot_mode == 'wrong_kernel' else RELEASE
            initrd = 'previous' if self.boot_mode == 'wrong_initrd' else RELEASE
            config = 'set default="'+default+'"\n'
            if self.boot_mode == 'submenu': config += "submenu 'unexpected' {\n}\n"
            config += "menuentry 'Debian GNU/Linux' {\n linux /boot/vmlinuz-"+image+' root=fixture\n'
            config += ' initrd /boot/initrd.img-'+initrd+'\n}\n'
            self.put(module.GRUB_CONFIG, config)
        if name == 'grub-editenv':
            if argv[-1] == 'list': return self.override
            self.put(module.GRUB_ENV, 'cleared next boot override\n')
        return ''

    def assert_restored_without_reboot(self):
        self.assertNotIn(['systemctl', 'reboot'], self.events)
        for name, content in self.before.items():
            path = self.installer.path(name)
            self.assertEqual(path.read_bytes() if path.exists() else None, content, name)
        self.assertEqual(self.installer.path('/boot/vmlinuz-previous').read_text(), 'original kernel')

    def test_success_installs_lts_checks_boot_and_requests_one_reboot(self):
        self.installer.install_and_reboot()
        self.assertEqual(self.events[-2:], [['sync'], ['systemctl', 'reboot']])
        installs = [e for e in self.events if e[0] == 'apt-get' and 'install' in e]
        self.assertTrue(any(module.PACKAGE+'='+VERSION in e for e in installs))
        self.assertTrue(all('--no-remove' in e for e in installs))
        self.assertTrue(all('--no-install-recommends' in e for e in installs))
        self.assertIn('signed-by='+module.KEYRING, self.installer.path(module.SOURCE).read_text())
        self.assertIn('https://deb.xanmod.org trixie main', self.installer.path(module.SOURCE).read_text())
        self.assertIn('GRUB_DEFAULT=0', self.installer.path(module.GRUB_DEFAULTS).read_text())
        self.assertEqual(self.installer.path('/boot/vmlinuz-previous').read_text(), 'original kernel')
        self.assertEqual(self.installer.backup.stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in self.installer.backup.iterdir()))
        self.assertFalse(any(word in ('purge', 'autoremove', 'apt-key') for e in self.events for word in e))

    def test_bbr_is_staged_for_new_kernel_without_touching_other_settings(self):
        self.installer.install_and_reboot()
        self.assertEqual(self.installer.path('/etc/sysctl.conf').read_text(), '# retained\nnet.ipv4.ip_forward=1\n')
        self.assertEqual(self.installer.path(module.BBR_CONFIG).read_text(),
                         'net.core.default_qdisc=fq\nnet.ipv4.tcp_congestion_control=bbr\n')
        self.assertFalse(any(e[0] in ('sysctl', 'modprobe') for e in self.events))

    def test_bbr_configuration_exists_before_the_reboot_request(self):
        def checked(argv, **kwargs):
            if argv == ['systemctl', 'reboot']:
                self.assertIn('net.ipv4.tcp_congestion_control=bbr', self.installer.path(module.BBR_CONFIG).read_text())
                self.assertIn('net.core.default_qdisc=fq', self.installer.path(module.BBR_CONFIG).read_text())
                result = json.loads((self.installer.backup/'result.json').read_text())
                self.assertEqual(result['tcp_congestion_control'], 'bbr')
                self.assertEqual(result['default_qdisc'], 'fq')
            return self.run_fake(argv, **kwargs)
        self.installer.run = checked
        self.installer.install_and_reboot()

    def test_modular_bbr_and_fq_are_loaded_at_boot(self):
        self.kernel_options = {'CONFIG_TCP_CONG_BBR': 'm', 'CONFIG_NET_SCH_FQ': 'm'}
        self.installer.install_and_reboot()
        self.assertEqual(self.installer.path(module.BBR_MODULES).read_text().splitlines()[1:], ['tcp_bbr', 'sch_fq'])
        self.assertFalse(any(e[0] in ('sysctl', 'modprobe') for e in self.events))

    def test_kernel_without_bbr_or_fq_cannot_reboot(self):
        for option in self.kernel_options:
            with self.subTest(option=option), patch.dict(self.kernel_options, {option: 'n'}):
                with self.assertRaises(module.InstallError): self.installer.install_and_reboot()
                self.assert_restored_without_reboot()

    def test_missing_new_kernel_config_cannot_reboot(self):
        self.omit = 'kernel_config'
        with self.assertRaises(module.InstallError): self.installer.install_and_reboot()
        self.assert_restored_without_reboot()

    def test_missing_bbr_module_cannot_reboot(self):
        self.kernel_options['CONFIG_TCP_CONG_BBR'] = 'm'
        self.omit = 'tcp_bbr'
        with self.assertRaises(module.InstallError): self.installer.install_and_reboot()
        self.assert_restored_without_reboot()

    def test_failed_bbr_write_cannot_reboot(self):
        original = self.installer.atomic
        def failed(path, data, mode=0o644):
            if path == self.installer.path(module.BBR_CONFIG): raise OSError('injected BBR write failure')
            return original(path, data, mode)
        with patch.object(self.installer, 'atomic', side_effect=failed), self.assertRaises(OSError):
            self.installer.install_and_reboot()
        self.assert_restored_without_reboot()

    def test_non_root_os_and_arch_are_rejected_before_package_operations(self):
        with patch.object(module.os, 'geteuid', return_value=1000), self.assertRaises(module.InstallError):
            self.installer.install_and_reboot()
        for distro in ('ID=debian\nVERSION_ID=12\n', 'ID=ubuntu\nVERSION_ID=26.04\n'):
            self.put('/etc/os-release', distro)
            with self.assertRaises(module.InstallError): self.installer.install_and_reboot()
        self.put('/etc/os-release', 'ID=debian\nVERSION_ID=13\n')
        self.arch = 'arm64'
        with self.assertRaises(module.InstallError): self.installer.install_and_reboot()
        self.assertFalse(any(e[0] == 'apt-get' for e in self.events))

    def test_containers_and_missing_grub_are_rejected_before_apt(self):
        self.container.return_value = 0
        with self.assertRaises(module.InstallError): self.installer.install_and_reboot()
        self.container.return_value = 1
        self.installer.path(module.GRUB_CONFIG).unlink()
        with self.assertRaises(module.InstallError): self.installer.install_and_reboot()
        self.assertFalse(any(e[0] == 'apt-get' for e in self.events))

    def test_secure_boot_enabled_or_unknown_is_rejected_before_apt(self):
        self.installer.path('/sys/firmware/efi/efivars').mkdir(parents=True)
        with self.assertRaises(module.InstallError): self.installer.install_and_reboot()
        self.installer.path('/sys/firmware/efi/efivars/SecureBoot-fixture').write_bytes(b'\x07\x00\x00\x00\x01')
        with self.assertRaises(module.InstallError): self.installer.install_and_reboot()
        self.assertFalse(any(e[0] == 'apt-get' for e in self.events))

    def test_uefi_with_secure_boot_disabled_can_install(self):
        self.installer.path('/sys/firmware/efi/efivars').mkdir(parents=True)
        self.installer.path('/sys/firmware/efi/efivars/SecureBoot-fixture').write_bytes(b'\x07\x00\x00\x00\x00')
        self.installer.install_and_reboot()
        self.assertEqual(self.events[-1], ['systemctl', 'reboot'])

    def test_uefi_using_another_bootloader_is_rejected_before_apt(self):
        self.installer.path('/sys/firmware/efi/efivars').mkdir(parents=True)
        self.installer.path('/sys/firmware/efi/efivars/SecureBoot-fixture').write_bytes(b'\x07\x00\x00\x00\x00')
        self.efi_entry = r'File(\EFI\systemd\systemd-bootx64.efi)'
        with self.assertRaises(module.InstallError): self.installer.install_and_reboot()
        self.assertFalse(any(e[0] == 'apt-get' for e in self.events))

    def test_signing_key_mismatch_and_multiple_primary_keys_stop_installation(self):
        for fingerprint, extra in (('0'*40, False), (module.KEY_FINGERPRINT, True)):
            self.fingerprint, self.extra_key = fingerprint, extra
            with self.assertRaises(module.InstallError): self.installer.install_and_reboot()
            self.assert_restored_without_reboot()
            self.assertFalse(any(e[0] == 'apt-get' and any(a.startswith(module.PACKAGE+'=') for a in e)
                                 for e in self.events))

    def test_download_prerequisites_simulation_and_install_failures_restore_files(self):
        for failure in (lambda a: a[0] == 'apt-get' and a[-1] == 'update',
                        lambda a: a[0] == 'apt-get' and 'gnupg' in a,
                        lambda a: a[0] == 'curl',
                        lambda a: a[0] == 'apt-get' and '--simulate' in a,
                        lambda a: a[0] == 'apt-get' and 'install' in a and '--simulate' not in a
                                  and any(v.startswith(module.PACKAGE+'=') for v in a)):
            self.events.clear(); self.fail = failure
            with self.assertRaises(module.InstallError): self.installer.install_and_reboot()
            self.assert_restored_without_reboot()

    def test_missing_candidate_is_rejected(self):
        self.candidate = '(none)'
        with self.assertRaises(module.InstallError): self.installer.install_and_reboot()
        self.assert_restored_without_reboot()

    def test_missing_kernel_modules_or_initramfs_does_not_reboot(self):
        for artifact in ('kernel', 'modules', 'initrd', 'initrd_modules'):
            for directory in ('boot', 'lib/modules'):
                path = self.installer.path('/'+directory)
                if directory == 'boot':
                    for file in path.glob('*'+RELEASE): file.unlink()
                elif path.exists(): shutil.rmtree(path)
            self.omit = artifact
            with self.assertRaises(module.InstallError): self.installer.install_and_reboot()
            self.assert_restored_without_reboot()

    def test_mismatched_meta_version_and_unconfigured_headers_are_rejected(self):
        self.bad_version = True
        with self.assertRaises(module.InstallError): self.installer.install_and_reboot()
        self.assert_restored_without_reboot()
        self.bad_version, self.bad_status = False, self.header
        with self.assertRaises(module.InstallError): self.installer.install_and_reboot()
        self.assert_restored_without_reboot()

    def test_missing_or_mismatched_dependencies_do_not_reboot(self):
        for image, header in (('linux-image-other', self.header), (self.image, 'linux-headers-6.18.99-xanmod1')):
            self.image, self.header = image, header
            with self.assertRaises(module.InstallError): self.installer.install_and_reboot()
            self.assert_restored_without_reboot()

    def test_grub_failure_and_wrong_boot_targets_restore_all_settings(self):
        for mode in ('wrong_kernel', 'wrong_initrd', 'saved', 'submenu'):
            self.events.clear(); self.boot_mode = mode
            with self.assertRaises(module.InstallError): self.installer.install_and_reboot()
            self.assert_restored_without_reboot()
        self.boot_mode = 'normal'
        self.fail = lambda a: a[0] == 'grub-script-check'
        with self.assertRaises(module.InstallError): self.installer.install_and_reboot()
        self.assert_restored_without_reboot()

    def test_uncleared_next_boot_override_prevents_reboot(self):
        self.override = 'next_entry=previous\n'
        with self.assertRaises(module.InstallError): self.installer.install_and_reboot()
        self.assert_restored_without_reboot()

    def test_initramfs_validation_failure_prevents_reboot(self):
        self.fail = lambda a: a[0] == 'lsinitramfs'
        with self.assertRaises(module.InstallError): self.installer.install_and_reboot()
        self.assert_restored_without_reboot()

    def test_interrupted_operation_restores_settings(self):
        def interrupted(argv, **kwargs):
            if argv[0] == 'update-grub': raise KeyboardInterrupt()
            return self.run_fake(argv, **kwargs)
        self.installer.run = interrupted
        with self.assertRaises(KeyboardInterrupt): self.installer.install_and_reboot()
        self.assert_restored_without_reboot()

    def test_rejected_reboot_restores_settings_and_reports_failure(self):
        self.fail = lambda a: a == ['systemctl', 'reboot']
        with self.assertRaises(module.InstallError): self.installer.install_and_reboot()
        for name, value in self.before.items():
            path = self.installer.path(name)
            self.assertEqual(path.read_bytes() if path.exists() else None, value)
        result = json.loads((self.installer.backup/'result.json').read_text())
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['configuration_restored'])

    def test_ssh_shutdown_signal_during_reboot_does_not_restore_old_boot_target(self):
        previous = signal.getsignal(signal.SIGHUP)
        def handoff(argv, **kwargs):
            if argv == ['systemctl', 'reboot']:
                signal.raise_signal(signal.SIGHUP)
            return self.run_fake(argv, **kwargs)
        self.installer.run = handoff
        self.installer.install_and_reboot()
        self.assertIn('vmlinuz-'+RELEASE, self.installer.path(module.GRUB_CONFIG).read_text())
        self.assertEqual(signal.getsignal(signal.SIGHUP), previous)

    def test_disconnect_after_reboot_acceptance_keeps_verified_boot_settings(self):
        original = self.installer.request_reboot
        def accepted_then_disconnected():
            original()
            raise KeyboardInterrupt()
        self.installer.request_reboot = accepted_then_disconnected
        self.installer.install_and_reboot()
        self.assertTrue(self.installer.reboot_accepted)
        self.assertIn('vmlinuz-'+RELEASE, self.installer.path(module.GRUB_CONFIG).read_text())

    def test_concurrent_kernel_operation_does_not_start_apt(self):
        path = self.installer.path('/run/mygwd-kernel/update.lock'); path.parent.mkdir(parents=True)
        with path.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(module.InstallError): self.installer.install_and_reboot()
        self.assertFalse(any(e[0] == 'apt-get' for e in self.events))

    def test_pending_package_configuration_prevents_installation(self):
        self.audit = 'unconfigured package\n'
        with self.assertRaises(module.InstallError): self.installer.install_and_reboot()
        self.assertFalse(any(e[0] == 'apt-get' for e in self.events))

    def test_cli_help_and_missing_operation_cannot_install(self):
        with patch.object(module.XanModInstaller, 'install_and_reboot') as install:
            self.assertEqual(module.main([]), 2)
            with self.assertRaises(SystemExit) as exit:
                module.main(['--help'])
            self.assertEqual(exit.exception.code, 0)
            install.assert_not_called()


class GatewayMenuTests(unittest.TestCase):
    def check_menu(self, role):
        source = (REPO/role).read_text().rsplit('\ncase "${1:-}" in\n', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/role; path.write_text(source)
            for choice, expected in (('2', 'enable-bbr'), ('3', 'install-xanmod')):
                script = '''
clear() { :; }
dpkg() { echo amd64; }
systemd-detect-virt() { echo kvm; }
systemctl() { echo active; }
sysctl() { echo 'key = value'; }
source "$1"
requireRoot() { return 0; }
requireDebian13() { return 0; }
installBBR() { echo 'ACTION=enable-bbr'; start_menu() { :; }; }
installXanMod() { echo 'ACTION=install-xanmod'; start_menu() { :; }; }
runKernelUpdate() { echo 'UNEXPECTED-Debian-kernel-update'; }
start_menu
'''
                result = subprocess.run(['bash', '-c', script, 'fixture', str(path)],
                                        input=choice+'\n', text=True, capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('ACTION='+expected, result.stdout)
                self.assertEqual(result.stdout.count('Enable Debian kernel BBR'), 1)
                self.assertIn('3. Install XanMod LTS kernel, enable BBR and reboot (amd64)', result.stdout)
                self.assertNotIn('UNEXPECTED-Debian-kernel-update', result.stdout)

    def test_client_menu_two_enables_bbr_and_three_installs_xanmod(self):
        self.check_menu('client')

    def test_server_menu_two_enables_bbr_and_three_installs_xanmod(self):
        self.check_menu('server')

    def test_server_container_menu_refuses_both_kernel_choices(self):
        source = (REPO/'server').read_text().rsplit('\ncase "${1:-}" in\n', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'server'; path.write_text(source)
            for choice in ('2', '3'):
                result = subprocess.run(['bash', '-c', '''
dpkg() { echo amd64; }
systemd-detect-virt() { echo docker; }
systemctl() { echo active; }
source "$1"
installBBR() { echo UNEXPECTED-BBR; }
installXanMod() { echo UNEXPECTED-XanMod; }
runKernelUpdate() { echo UNEXPECTED-kernel; }
start_menu
''', 'fixture', str(path)], input=choice+'\n', text=True, capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('Container kernels and BBR must be configured on the host.', result.stdout)
                self.assertNotIn('UNEXPECTED-', result.stdout)

    def test_server_loader_uses_shared_installer_and_propagates_failure(self):
        source = (REPO/'server').read_text().rsplit('\ncase "${1:-}" in\n', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root/'server'; path.write_text(source)
            for status, download in ((0, 0), (73, 0), (0, 22)):
                with self.subTest(status=status, download=download):
                    result = subprocess.run(['bash', '-c', '''
dpkg() { echo amd64; }
systemd-detect-virt() { echo kvm; }
source "$1"
requireRoot() { return 0; }
requireDebian13() { return 0; }
mktemp() { command mktemp "$FIXTURE_ROOT/helper.XXXXXX"; }
copyOrDownloadAsset() {
  [[ $1 == scripts/install-xanmod.py ]] || return 99
  [[ $DOWNLOAD_STATUS == 0 ]] || return "$DOWNLOAD_STATUS"
  printf '# harmless loader fixture\\n' >"$2"
}
python3() {
  [[ -f $1 && $2 == --install-and-reboot ]] || return 98
  echo SHARED-XANMOD-INSTALLER
  return "$HELPER_STATUS"
}
installXanMod
''', 'fixture', str(path)], text=True, capture_output=True, timeout=5,
                        env=dict(os.environ, FIXTURE_ROOT=directory,
                                 HELPER_STATUS=str(status), DOWNLOAD_STATUS=str(download)))
                    self.assertEqual(result.returncode, status if download == 0 else 1, result.stderr)
                    self.assertEqual('SHARED-XANMOD-INSTALLER' in result.stdout, download == 0)
                    self.assertFalse(list(root.glob('helper.*')))

    def test_server_loader_rejects_arm64_and_container_before_download(self):
        source = (REPO/'server').read_text().rsplit('\ncase "${1:-}" in\n', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'server'; path.write_text(source)
            for arch, virt in (('arm64', 'vm'), ('amd64', 'container')):
                result = subprocess.run(['bash', '-c', '''
dpkg() { echo amd64; }
systemd-detect-virt() { echo kvm; }
source "$1"
requireRoot() { return 0; }
requireDebian13() { return 0; }
architecture=$2
virtVC=$3
copyOrDownloadAsset() { echo UNEXPECTED-download; return 99; }
installXanMod
''', 'fixture', str(path), arch, virt], text=True, capture_output=True, timeout=5)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('UNEXPECTED-download', result.stdout)


if __name__ == '__main__':
    unittest.main(verbosity=2)
