"""Safety checks for the fixed-version installer, without root/system changes."""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]
INSTALLER = REPO / "scripts/install-pihole5.sh"


class PiHoleInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pihole5-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def shell(self, body, *arguments):
        return subprocess.run(
            ["bash", "-c", 'source "$1"; shift; ' + body, "test", str(INSTALLER),
             *map(str, arguments)], text=True, capture_output=True)

    def config(self, **overrides):
        settings = {
            "PIHOLE_INTERFACE": "eth0,wg0", "IPV4_ADDRESS": "192.0.2.3/24",
            "PIHOLE_DNS_1": "127.0.0.1#5350", "WEBPASSWORD": "a" * 64,
            "INSTALL_WEB_SERVER": "false", "INSTALL_WEB_INTERFACE": "true",
        }
        settings.update(overrides)
        path = self.root / "setupVars.conf"
        path.write_text("".join(f"{key}={value}\n" for key, value in settings.items()))
        return path

    def validate(self, path):
        return self.shell('declare -gA gwd_config=(); gwd_read_config "$1"; '
                          'gwd_validate_config', path)

    def test_existing_de_gwd_configuration_is_accepted_without_hash_output(self):
        result = self.validate(self.config(PIHOLE_DNS_2=""))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("a" * 64, result.stdout + result.stderr)

    def test_shell_expansion_is_rejected_without_execution(self):
        marker = self.root / "executed"
        path = self.config(PIHOLE_INTERFACE=f"$(touch {marker})")
        result = self.validate(path)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists())
        self.assertIn("Unsafe configuration value", result.stderr)

    def test_source_can_not_override_shell_or_install_paths(self):
        for key in ("PATH", "BASH_ENV", "PI_HOLE_LOCAL_REPO", "GWD_PIHOLE_CORE_TAG"):
            with self.subTest(key=key):
                result = self.validate(self.config(**{key: "/tmp/unsafe"}))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Unsupported configuration key", result.stderr)

    def test_duplicate_keys_are_rejected(self):
        path = self.config()
        with path.open("a") as handle:
            handle.write("INSTALL_WEB_INTERFACE=false\n")
        result = self.validate(path)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Duplicate configuration key", result.stderr)

    def test_shared_password_hash_and_missing_password_are_rejected(self):
        for password in ("0" * 64, "", "plaintext-password", "f" * 63):
            with self.subTest(password_length=len(password)):
                result = self.validate(self.config(WEBPASSWORD=password))
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(password or "unprintable", result.stdout + result.stderr)

    def test_invalid_address_upstream_and_boolean_fail_closed(self):
        for overrides in (
            {"IPV4_ADDRESS": "999.0.2.3/24"}, {"IPV4_ADDRESS": "192.0.2.3/33"},
            {"IPV4_ADDRESS": "192.0.2.3/24/extra"},
            {"PIHOLE_DNS_1": "127.0.0.1#65536"}, {"PIHOLE_DNS_1": "127.0.0.1#0"},
            {"PIHOLE_DNS_1": "127.0.0.1#53#53"}, {"PIHOLE_DNS_1": "1.1.1.999"},
            {"INSTALL_WEB_INTERFACE": "yes"},
        ):
            with self.subTest(overrides=overrides):
                self.assertNotEqual(self.validate(self.config(**overrides)).returncode, 0)

    def binary(self):
        directory = self.root / "ftl"
        directory.mkdir(exist_ok=True)
        binary = "pihole-FTL-linux-x86_64"
        content = b"FTL fixture for integrity verification\n"
        (directory / binary).write_bytes(content)
        (directory / f"{binary}.sha1").write_text(
            f"{hashlib.sha1(content).hexdigest()}  {binary}\n")
        return directory, binary

    def test_official_checksum_format_verifies_and_rejects_changed_binary(self):
        directory, binary = self.binary()
        digest = hashlib.sha1((directory / binary).read_bytes()).hexdigest()
        result = self.shell('gwd_verify_ftl_checksum "$1" "$2" "$3"', directory, binary, digest)
        self.assertEqual(result.returncode, 0, result.stderr)
        (directory / binary).write_bytes(b"corrupted download")
        result = self.shell('gwd_verify_ftl_checksum "$1" "$2" "$3"', directory, binary, digest)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checksum verification failed", result.stderr)

    def test_checksum_for_another_path_or_multiple_files_is_rejected(self):
        directory, binary = self.binary()
        for checksum in (
            "a" * 40 + "  ../outside\n", "a" * 40 + "  " + binary + "\n\n",
            "not-a-checksum  " + binary + "\n", "a" * 40 + "\n",
        ):
            with self.subTest(checksum=checksum):
                (directory / f"{binary}.sha1").write_text(checksum)
                result = self.shell('gwd_verify_ftl "$1" "$2"', directory, binary)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Malformed official FTL checksum", result.stderr)

    def test_replacing_both_cache_binary_and_checksum_is_rejected(self):
        directory, binary = self.binary()
        result = self.shell('gwd_verify_ftl "$1" "$2"', directory, binary)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pinned official release checksum", result.stderr)

    def test_partial_ftl_config_gets_privacy_default_without_changing_existing_value(self):
        config = self.root / "pihole-FTL.conf"
        config.write_text("# Official default template\n")
        result = self.shell('gwd_ensure_privacy "$1"', config)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PRIVACYLEVEL=0\n", config.read_text())
        config.write_text("PRIVACYLEVEL=3\n")
        result = self.shell('gwd_ensure_privacy "$1"', config)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(config.read_text(), "PRIVACYLEVEL=3\n")

    def test_download_failure_never_promotes_partial_artifact_cache(self):
        destination = self.root / "prepared-ftl"
        result = self.shell(
            'gwd_download() { printf broken > "$2"; return 22; }; '
            'gwd_prepare_ftl "$1" pihole-FTL-linux-x86_64', destination)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.root.glob("prepared-ftl.download.*")), [])

    def repository(self):
        directory = self.root / "core"
        directory.mkdir()
        commands = (
            ["init", "-q"], ["config", "user.email", "fixture@example.invalid"],
            ["config", "user.name", "Fixture"],
        )
        for arguments in commands:
            subprocess.run(["git", "-C", str(directory), *arguments], check=True)
        (directory / "file").write_text("verified content\n")
        subprocess.run(["git", "-C", str(directory), "add", "file"], check=True)
        subprocess.run(["git", "-C", str(directory), "commit", "-qm", "fixture"], check=True)
        commit = subprocess.check_output(
            ["git", "-C", str(directory), "rev-parse", "HEAD"], text=True).strip()
        return directory, commit

    def test_wrong_repo_version_and_modified_repo_are_rejected(self):
        directory, commit = self.repository()
        result = self.shell('gwd_verify_repo "$1" "$2"', directory, commit)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.shell('gwd_verify_repo "$1" "$2"', directory, "0" * 40)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Repository version mismatch", result.stderr)
        (directory / "file").write_text("tampered content\n")
        result = self.shell('gwd_verify_repo "$1" "$2"', directory, commit)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Modified repository", result.stderr)

    def test_git_index_flags_cannot_conceal_modified_files(self):
        directory, commit = self.repository()
        for flag in ("--skip-worktree", "--assume-unchanged"):
            with self.subTest(flag=flag):
                subprocess.run(["git", "-C", str(directory), "update-index", flag, "file"], check=True)
                (directory / "file").write_text("concealed modification\n")
                result = self.shell('gwd_verify_repo "$1" "$2"', directory, commit)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Unverifiable repository index flags", result.stderr)
                for clear_flag in ("--no-skip-worktree", "--no-assume-unchanged"):
                    subprocess.run(["git", "-C", str(directory), "update-index", clear_flag, "file"], check=True)
                subprocess.run(["git", "-C", str(directory), "checkout", "--", "file"], check=True)

    def test_git_clean_filter_cannot_execute_or_conceal_changed_files(self):
        directory, commit = self.repository()
        marker = self.root / "filter-executed"
        cleaner = self.root / "clean-filter"
        cleaner.write_text(f"#!/bin/sh\ntouch '{marker}'\nprintf 'verified content\\n'\n")
        cleaner.chmod(0o755)
        (directory / ".git/info/attributes").write_text("file filter=fixture\n")
        subprocess.run(["git", "-C", str(directory), "config", "filter.fixture.clean", str(cleaner)], check=True)
        (directory / "file").write_text("changed content\n")
        result = self.shell('gwd_verify_repo "$1" "$2"', directory, commit)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists())
        self.assertIn("Modified repository", result.stderr)

    def test_existing_deployment_is_never_overwritten(self):
        source, commit = self.repository()
        destination = self.root / "existing-deployment"
        destination.mkdir()
        marker = destination / "user-file"
        marker.write_text("preserve me")
        result = self.shell('gwd_install_repo "$1" "$2" "$3"', source, destination, commit)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(marker.read_text(), "preserve me")
        self.assertFalse((destination / "file").exists())

    def test_explicit_replacement_archives_complete_old_tree(self):
        destination = self.root / "legacy-admin"
        destination.mkdir()
        (destination / "custom-file").write_text("keep this customization")
        backup_base = self.root / "backups"
        result = self.shell('gwd_check_existing_repo "$1" "$2" "$3"',
                            destination, "0" * 40, backup_base)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(destination.exists())
        backups = list(backup_base.glob("repositories.*/legacy-admin/custom-file"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "keep this customization")
        self.assertEqual(backup_base.stat().st_mode & 0o777, 0o700)

    def test_dns_sequence_gaps_are_rejected_and_real_ui_layout_is_preserved(self):
        result = self.validate(self.config(PIHOLE_DNS_2="", PIHOLE_DNS_3="1.1.1.1"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("consecutive", result.stderr)
        result = self.validate(self.config(WEBUIBOXEDLAYOUT="boxed"))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_nonroot_installation_fails_before_system_changes(self):
        if os.geteuid() == 0:
            self.skipTest("This check exercises an ordinary user's root refusal.")
        result = subprocess.run(["bash", str(INSTALLER)], text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Installation requires root", result.stderr)


if __name__ == "__main__":
    unittest.main()
