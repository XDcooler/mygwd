"""Validate kernel package operations without changing the running machine."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]


class KernelUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mygwd-kernel-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.events = self.root/'events'
        self.events.touch()
        self.os_release = self.root/'os-release'
        self.os_release.write_text('ID=debian\nVERSION_ID=13\n')
        source = (REPO/'scripts/update-kernel.sh').read_text()
        for original, destination in [('/etc/os-release', str(self.os_release)),
                ('/run/mygwd-kernel', str(self.root/'locks')), ('/var/backups', str(self.root/'backups')),
                ('/boot/', str(self.root/'boot')+'/'), ('/lib/modules/', str(self.root/'modules')+'/')]:
            source = source.replace(original, destination)
        self.source = self.root/'kernel.sh'
        self.source.write_text(source)
        self.release = '6.12.999+deb13-cloud-amd64'
        (self.root/'boot').mkdir()
        (self.root/'modules'/self.release).mkdir(parents=True)
        for name in ('vmlinuz-', 'initrd.img-'): (self.root/'boot'/(name+self.release)).write_bytes(b'fixture')

    def shell(self, body, **overrides):
        environment = dict(os.environ, EVENTS=str(self.events), ARCH='amd64',
            RUNNING='6.12.100+deb13-cloud-amd64', CANDIDATE='6.12.999-1', APT_FAIL='',
            CONTAINER='1', IMAGE='linux-image-'+self.release)
        environment.update(overrides)
        script = '''
source "$1"
dpkg() { echo "$ARCH"; }
uname() { echo "$RUNNING"; }
systemd-detect-virt() { return "$CONTAINER"; }
flock() { return 0; }
apt-cache() { printf '  Candidate: %s\\n' "$CANDIDATE"; }
apt-get() { echo "apt-get $*" >>"$EVENTS"; [[ -z $APT_FAIL || " $* " != *" $APT_FAIL "* ]]; }
dpkg-query() {
    case "$*" in
        *binary:Package*) printf 'linux-image-previous\\t1\\tinstall ok installed\\n';;
        *Depends*) echo "$IMAGE (= 6.12.999-1)";;
        *) echo 'install ok installed';;
    esac
}
reboot() { echo reboot >>"$EVENTS"; return 99; }
''' + body
        return subprocess.run(['bash', '-c', script, 'fixture', str(self.source)],
            env=environment, capture_output=True, text=True, timeout=10)

    def test_matching_generic_and_cloud_packages_for_both_architectures(self):
        for architecture in ('amd64', 'arm64'):
            for flavour in ('', 'cloud-'):
                with self.subTest(architecture=architecture, flavour=flavour):
                    result = self.shell('kernelPackage', ARCH=architecture, RUNNING='6.12.100+deb13-'+flavour+architecture)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), 'linux-image-'+flavour+architecture)

    def test_unsupported_os_architecture_and_flavour_stop_before_apt(self):
        result = self.shell('kernelMain --check', ARCH='riscv64')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotEqual(self.shell('kernelMain --check', RUNNING='6.12-custom').returncode, 0)
        self.assertNotEqual(self.shell('kernelMain --check', RUNNING='6.12.100+deb13-rt-amd64').returncode, 0)
        self.os_release.write_text('ID=debian\nVERSION_ID=12\n')
        self.assertNotEqual(self.shell('kernelMain --check').returncode, 0)
        self.assertEqual(self.events.read_text(), '')

    def test_check_is_only_an_apt_simulation(self):
        result = self.shell('kernelMain --check')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events.read_text().splitlines(),
            ['apt-get --simulate --no-remove --no-install-recommends install linux-image-cloud-amd64'])
        self.assertFalse((self.root/'backups').exists())

    def test_missing_candidate_and_simulation_errors_propagate(self):
        self.assertNotEqual(self.shell('kernelMain --check', CANDIDATE='(none)').returncode, 0)
        self.assertEqual(self.events.read_text(), '')
        self.assertNotEqual(self.shell('kernelMain --check', APT_FAIL='--simulate').returncode, 0)

    @unittest.skipUnless(os.geteuid() == 0, 'Root-only package-operation fixture')
    def test_container_install_is_rejected_before_apt(self):
        result = self.shell('kernelMain --install', CONTAINER='0')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.events.read_text(), '')

    @unittest.skipIf(os.geteuid() == 0, 'Non-root rejection runs as an ordinary user')
    def test_non_root_cannot_install(self):
        result = self.shell('kernelMain --install')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('requires root', result.stderr)
        self.assertEqual(self.events.read_text(), '')

    @unittest.skipUnless(os.geteuid() == 0, 'Root-only package-operation fixture')
    def test_failed_package_list_update_never_installs(self):
        result = self.shell('kernelMain --install', APT_FAIL='update')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(' install ', self.events.read_text())
        self.assertNotIn('Installed kernel:', result.stdout)

    @unittest.skipUnless(os.geteuid() == 0, 'Root-only package-operation fixture')
    def test_failed_package_install_does_not_report_success_or_delete_kernels(self):
        result = self.shell('kernelMain --install', APT_FAIL='install')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('Installed kernel:', result.stdout)
        self.assertTrue((self.root/'boot'/('vmlinuz-'+self.release)).exists())
        self.assertNotIn('purge', self.events.read_text())
        self.assertNotIn('reboot', self.events.read_text())

    @unittest.skipUnless(os.geteuid() == 0, 'Root-only package-operation fixture')
    def test_incomplete_boot_artifacts_fail_the_installation_check(self):
        (self.root/'boot'/('initrd.img-'+self.release)).unlink()
        result = self.shell('kernelMain --install')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('incomplete', result.stderr)
        self.assertNotIn('Installed kernel:', result.stdout)

    @unittest.skipUnless(os.geteuid() == 0, 'Root-only package-operation fixture')
    def test_success_keeps_old_kernel_and_records_the_previous_package_state(self):
        old = self.root/'boot/vmlinuz-previous'
        old.write_bytes(b'previous')
        result = self.shell('kernelMain --install')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Installed kernel: '+self.release, result.stdout)
        self.assertEqual(old.read_bytes(), b'previous')
        backup = next((self.root/'backups').iterdir())
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        self.assertEqual((backup/'packages-before.txt').stat().st_mode & 0o777, 0o600)
        self.assertIn('linux-image-previous', (backup/'packages-before.txt').read_text())
        self.assertNotIn('purge', self.events.read_text())
        self.assertNotIn('reboot', self.events.read_text())

    def test_help_and_unknown_flags_do_not_access_apt(self):
        self.os_release.write_text('ID=other\nVERSION_ID=1\n')
        self.assertEqual(self.shell('kernelMain --help').returncode, 0)
        self.assertEqual(self.shell('kernelMain --unknown').returncode, 2)
        self.assertEqual(self.events.read_text(), '')


if __name__ == '__main__': unittest.main(verbosity=2)
