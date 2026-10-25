"""Check project sources and migration without downloads or host configuration changes."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
import zipfile

REPO = Path(__file__).resolve().parents[1]
HELPERS = REPO/'resource/client/ui-script'
OFFICIAL = 'bash <(curl -fsSL https://raw.githubusercontent.com/XDcooler/mygwd/main/client)'
OLD_URL = re.compile(rb'https?://[a-z0-9.-]+(?::[0-9]+)?/(?:[^\s\x22\x27<>]*/)?de[_-]gwd(?:[/.?#@:)]|$)', re.I)


class UpdateSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mygwd-source-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.gateway = self.root/'gateway'; self.gateway.mkdir()
        self.private = self.root/'panel'
        self.config = self.gateway/'0conf'
        # Only these paths are changed. The actual broker writer/lock and Bash
        # source selection functions run against private files.
        broker = (HELPERS/'ui-panel').read_text().replace(
            "Path('/opt/de_GWD')", 'Path('+repr(str(self.gateway))+')').replace(
            "Path('/var/lib/de-gwd-panel')", 'Path('+repr(str(self.private))+')')
        (self.gateway/'ui-panel').write_text(broker)

    def shell(self, role, body, **environment):
        source = (REPO/role).read_text().rsplit('\ncase "${1:-}" in\n', 1)[0]
        source = source.replace('/opt/de_GWD/ui-panel', str(self.gateway/'ui-panel'))
        entry = self.root/role; entry.write_text(source)
        env = {key: value for key, value in os.environ.items() if not key.startswith('DE_GWD_RULES_')}
        env.update(environment)
        script = '''
clear() { :; }
dpkg() { printf 'amd64\\n'; }
systemd-detect-virt() { printf 'kvm\\n'; }
source "$1"
''' + body
        return subprocess.run(['bash', '-c', script, 'fixture', str(entry)], env=env,
                              capture_output=True, text=True, timeout=15)

    def download(self, role, repository=None, branch=None):
        log = self.root/'curl.log'
        env = {'CURL_LOG': str(log), 'DESTINATION': str(self.root/'asset')}
        if repository is not None: env['DE_GWD_RULES_REPOSITORY'] = repository
        if branch is not None: env['DE_GWD_RULES_BRANCH'] = branch
        result = self.shell(role, '''
sourceDirectory=/nonexistent-mygwd-source-fixture
curl() {
    printf '%s\\n' "$@" >>"$CURL_LOG"
    while [[ $# -gt 0 ]]; do
        if [[ $1 == -o ]]; then printf 'fixture' >"$2"; return 0; fi
        shift
    done
    return 1
}
copyOrDownloadAsset resource/client/Archive.zip "$DESTINATION" || exit 1
[[ ${installCMD:-} == '' ]] || printf 'COMMAND=%s\\n' "$installCMD"
''', **env)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertEqual((self.root/'asset').read_text(), 'fixture')
        return [line for line in log.read_text().splitlines() if line.startswith('https://')], result

    def test_both_roles_default_to_mygwd_assets(self):
        for role in ('client', 'server'):
            (self.root/'curl.log').unlink(missing_ok=True)
            urls, _ = self.download(role)
            self.assertEqual(urls, ['https://raw.githubusercontent.com/XDcooler/mygwd/main/resource/client/Archive.zip'])

    def test_known_old_environment_sources_migrate_before_downloading(self):
        for role in ('client', 'server'):
            for repository in ('XDcooler/de_GWD', 'jacyl4/de_GWD', 'XDCOOLER/DE_GWD'):
                with self.subTest(role=role, repository=repository):
                    (self.root/'curl.log').unlink(missing_ok=True)
                    urls, result = self.download(role, repository)
                    self.assertEqual(urls, ['https://raw.githubusercontent.com/XDcooler/mygwd/main/resource/client/Archive.zip'])
                    self.assertIn('Legacy project source replaced', result.stdout)

    def test_cli_pin_is_preserved_but_panel_command_stays_official(self):
        sha = 'a'*40
        urls, result = self.download('client', 'XDcooler/mygwd', sha)
        self.assertEqual(urls, ['https://raw.githubusercontent.com/XDcooler/mygwd/'+sha+'/resource/client/Archive.zip'])
        self.assertIn('COMMAND='+OFFICIAL, result.stdout)

    def test_explicit_nonlegacy_fork_and_branch_remain_available_to_cli(self):
        for role in ('client', 'server'):
            (self.root/'curl.log').unlink(missing_ok=True)
            urls, _ = self.download(role, 'example/mygwd-fork', 'review')
            self.assertEqual(urls, ['https://raw.githubusercontent.com/example/mygwd-fork/review/resource/client/Archive.zip'])

    def test_installer_migrates_only_metadata_without_executing_it(self):
        marker = self.root/'must-not-exist'
        for command in ('bash <(curl -fsSL https://raw.githubusercontent.com/XDcooler/de_GWD/main/client)',
                        'touch '+str(marker), None):
            config = {'update': {'updateCMD': command, 'v2node': {'domain': 'node.example'}},
                      'dns': {'DOH': ['node.example/dq']}, 'address': {'PWD': 'fixture'}}
            self.config.write_text(json.dumps(config)); self.config.chmod(0o600)
            backup = self.gateway/'0conf_bak'; backup.write_bytes(self.config.read_bytes())
            before_backup = backup.read_bytes()
            result = self.shell('client', 'migrateUpdateSource')
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            expected = dict(config, update=dict(config['update'], updateCMD=OFFICIAL))
            self.assertEqual(json.loads(self.config.read_text()), expected)
            self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
            self.assertEqual(backup.read_bytes(), before_backup)
            self.assertFalse(marker.exists())
            self.assertTrue((self.private/'management.lock').is_file())

    def test_current_metadata_is_idempotent_and_invalid_config_is_untouched(self):
        self.config.write_text(json.dumps({'update': {'updateCMD': OFFICIAL}}, separators=(',', ':')))
        before = self.config.read_bytes()
        stamp = self.config.stat().st_mtime_ns
        self.assertEqual(self.shell('client', 'migrateUpdateSource').returncode, 0)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.config.stat().st_mtime_ns, stamp)
        for invalid in ('{invalid', '[]', '{"update":null}'):
            self.config.write_text(invalid)
            result = self.shell('client', 'migrateUpdateSource')
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self.config.read_text(), invalid)

    def test_executable_sources_have_no_literal_old_repository_urls(self):
        paths = [REPO/'client', REPO/'server']
        for directory in ('scripts', '.github', 'resource/client/ui-script', 'resource/client/ui-web'):
            paths.extend(p for p in (REPO/directory).rglob('*') if p.is_file()
                         and '__pycache__' not in p.parts and p.suffix != '.pyc')
        for path in paths:
            with self.subTest(path=path.relative_to(REPO)):
                self.assertIsNone(OLD_URL.search(path.read_bytes()), 'Old repository download URL')

    def test_client_archive_matches_sources_and_has_no_old_download_urls(self):
        root = REPO/'resource/client'
        expected = {path.relative_to(root).as_posix(): path for directory in ('ui-script', 'ui-web')
                    for path in (root/directory).rglob('*') if path.is_file()
                    and '__pycache__' not in path.parts and path.suffix != '.pyc' and path.name != '.DS_Store'}
        with zipfile.ZipFile(root/'Archive.zip') as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(set(archive.namelist()), set(expected))
            for name, path in expected.items():
                with self.subTest(member=name):
                    data = archive.read(name)
                    self.assertEqual(data, path.read_bytes())
                    self.assertIsNone(OLD_URL.search(data))


if __name__ == '__main__':
    unittest.main(verbosity=2)
