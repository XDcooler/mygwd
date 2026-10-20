#!/usr/bin/env python3
"""Rebuild the client payload used by installWebUI from its tracked source trees."""
from pathlib import Path
import zipfile

root = Path(__file__).resolve().parents[1] / 'resource/client'
destination = root / 'Archive.zip'
temporary = destination.with_suffix('.zip.tmp')
try:
    with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for directory in ['ui-script', 'ui-web']:
            for path in sorted((root / directory).rglob('*')):
                if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc' and path.name != '.DS_Store':
                    entry = zipfile.ZipInfo(path.relative_to(root).as_posix(), (1980, 1, 1, 0, 0, 0))
                    entry.create_system = 3
                    entry.compress_type = zipfile.ZIP_DEFLATED
                    mode = 0o755 if directory == 'ui-script' else 0o644
                    entry.external_attr = (0o100000 | mode) << 16
                    archive.writestr(entry, path.read_bytes())
    temporary.replace(destination)
finally:
    temporary.unlink(missing_ok=True)
print('Rebuilt resource/client/Archive.zip from ui-script and ui-web.')
