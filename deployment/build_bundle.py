#!/usr/bin/env python3
"""Build a secret-free, hash-manifested Methodenbot release bundle."""

import argparse
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import shutil
import stat
import tarfile


FILES = (
    '.env.example', 'README.md', 'UEBERGABE_LUKAS.md', 'requirements.txt',
    'main.py', 'configuration.py', 'control_state.py', 'matrixbot.py',
    'matrix_commands.py', 'manual_delivery.py', 'exchangemail.py',
    'digest_bundle.py', 'digest_state.py', 'digest_service.py',
    'digest_upload.py', 'digest_upload_receiver.py',
    'stats_table_manager.py', 'form_table_compat.py', 'ai_summary.py',
    'summary_selection.py', 'ai_service.py',
    'examples/quantitativ.json', 'examples/qualitativ.json', 'examples/unklar.json',
    'tests/test_ai_summary.py', 'tests/test_rate_limits.py',
    'tests/test_integration.py', 'tests/test_final_control.py',
    'tests/test_matrixbot_final.py',
    'tests/test_digest.py',
    'tests/test_deployment.py',
    'tests/test_control_state.py',
    'deployment/build_bundle.py', 'deployment/manage.py',
    'deployment/runtime_preflight.py',
)


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', default='dist')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = (root / args.output_dir).resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    if output == root or root not in output.parents:
        raise SystemExit('Output must be a child of the project directory')

    entries = []
    for relative in FILES:
        source = root / relative
        metadata = source.lstat()
        if not stat.S_ISREG(metadata.st_mode) or source.is_symlink() or metadata.st_size > 2_000_000:
            raise SystemExit('Unsafe release member: ' + relative)
        entries.append((relative, digest(source)))
    content_digest = hashlib.sha256('\n'.join(value + '  ' + name for name, value in entries).encode()).hexdigest()
    release_name = ('methodenbot-final-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
                    + '-' + content_digest[:12])
    release = output / release_name
    if release.exists():
        raise SystemExit('Release already exists: ' + str(release))
    release.mkdir(mode=0o700)
    for relative, _value in entries:
        target = release / relative
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        shutil.copyfile(root / relative, target)
        target.chmod(0o755 if relative.startswith('deployment/') else 0o644)
    manifest = release / 'MANIFEST.sha256'
    manifest.write_text(''.join(value + '  ' + name + '\n' for name, value in entries), encoding='utf-8')
    manifest.chmod(0o644)
    archive = output / (release_name + '.tar.gz')
    with tarfile.open(archive, 'x:gz') as bundle:
        bundle.add(release, arcname=release_name, recursive=True)
    archive.chmod(0o600)
    print(release)
    print(archive)
    print('files=' + str(len(entries) + 1))
    print('content_sha256=' + content_digest)


if __name__ == '__main__':
    main()
