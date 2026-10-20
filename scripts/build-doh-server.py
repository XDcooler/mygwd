#!/usr/bin/env python3
"""Build the official DoH server for both supported Linux architectures."""
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
GO_VERSION = 'go1.27.2'
GO_SHA256 = {
    'amd64': 'ecbadb99091a3f46e31f5f934b068b1864eafa7995211b39eaddf76996045fe5',
    'arm64': '94f3e30b8e374bc285e7dadc11e0865726b9bc6e85b841ccceaabc0214c6b7c8',
}
COMMIT = 'f13dea391fe17a76eddb879d120abc9ed7b6e014'
TAG = 'v2.3.10'


def run(*args, **kwargs):
    subprocess.run(args, check=True, **kwargs)


def main():
    if platform.system() != 'Linux':
        raise SystemExit('Run this build in the local Linux VM.')
    arch = {'aarch64': 'arm64', 'x86_64': 'amd64'}[platform.machine()]
    manifest = {'repository': 'https://github.com/m13253/dns-over-https',
                'tag': TAG, 'commit': COMMIT, 'go': GO_VERSION, 'artifacts': {}}
    with tempfile.TemporaryDirectory(prefix='de-gwd-doh-build-') as temporary:
        work = Path(temporary)
        filename = GO_VERSION + '.linux-' + arch + '.tar.gz'
        archive = work/filename
        urllib.request.urlretrieve('https://go.dev/dl/' + filename, archive)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != GO_SHA256[arch]:
            raise SystemExit('Official Go archive checksum mismatch.')
        with tarfile.open(archive) as package:
            package.extractall(work)
        repository = work/'doh'
        run('git', 'clone', '--quiet', '--depth', '1', '--branch', TAG,
            'https://github.com/m13253/dns-over-https.git', str(repository))
        actual = subprocess.check_output(['git', '-C', str(repository), 'rev-parse', 'HEAD'], text=True).strip()
        if actual != COMMIT:
            raise SystemExit('Official DoH source commit mismatch.')
        shutil.copyfile(repository/'LICENSE', ROOT/'resource/doh-server-LICENSE')
        go = str(work/'go/bin/go')
        environment = dict(os.environ, GOTOOLCHAIN='local', CGO_ENABLED='0',
                           GOOS='linux', GOFLAGS='-mod=readonly')
        run(go, 'mod', 'download', cwd=repository, env=environment)
        run(go, 'mod', 'verify', cwd=repository, env=environment)
        for target in ['amd64', 'arm64']:
            output = work/('doh-server_' + target)
            run(go, 'build', '-trimpath', '-ldflags=-s -w', '-o', str(output),
                './doh-server', cwd=repository, env=dict(environment, GOARCH=target))
            data = output.read_bytes()
            destination = ROOT/'resource'/output.name
            destination.write_bytes(data)
            destination.chmod(0o755)
            manifest['artifacts'][target] = {'file': 'resource/' + output.name,
                                           'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        (ROOT/'resource/doh-server-build.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
