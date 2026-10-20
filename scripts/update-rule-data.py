#!/usr/bin/env python3
"""Refresh the repository snapshot using the same updater shipped to client machines."""
from pathlib import Path
import subprocess
import sys
import zipfile

repo = Path(__file__).resolve().parents[1]
rules = repo/'resource/rules'
subprocess.run(['bash', str(repo/'resource/client/ui-script/ui-updateRules'),
                '--directory', str(rules), '--data-only'], check=True)
for architecture in ['amd64', 'arm64']:
    path = repo/'resource'/('vtrui_' + architecture + '.zip')
    temporary = path.with_suffix('.zip.tmp')
    try:
        with zipfile.ZipFile(path) as old, zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as new:
            for entry in old.infolist():
                content = (rules/entry.filename).read_bytes() if entry.filename in ['geoip.dat', 'geosite.dat'] else old.read(entry)
                new.writestr(entry, content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
subprocess.run([sys.executable, str(repo/'scripts/build-client-archive.py')], check=True)
print('Updated rule snapshot and both architecture packages; core binaries were preserved.')
