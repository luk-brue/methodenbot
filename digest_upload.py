#!/usr/bin/env python3
"""Upload one generated digest through a restricted SSH configuration alias."""

import argparse
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess


MAX_DIGEST_BYTES = 1_000_000
DIGEST_NAME = re.compile(r'\d{4}-\d{2}-\d{2}-methoden-digest\.md')
SSH_TARGET = re.compile(r'[A-Za-z0-9_.@-]{1,255}')


def read_source(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as handle:
            metadata = os.fstat(handle.fileno())
            if (not stat.S_ISREG(metadata.st_mode)
                    or not 0 < metadata.st_size <= MAX_DIGEST_BYTES):
                raise RuntimeError('unsafe_digest_source')
            raw = handle.read(MAX_DIGEST_BYTES + 1)
        text = raw.decode('utf-8')
    except RuntimeError:
        raise
    except (OSError, UnicodeError):
        raise RuntimeError('digest_source_unreadable') from None
    if len(raw) > MAX_DIGEST_BYTES or not text.strip():
        raise RuntimeError('invalid_digest_source')
    return raw


def upload(path, target, *, ssh='/usr/bin/ssh'):
    source = Path(path)
    if DIGEST_NAME.fullmatch(source.name) is None:
        raise RuntimeError('invalid_digest_filename')
    if SSH_TARGET.fullmatch(target) is None:
        raise RuntimeError('invalid_ssh_target')
    raw = read_source(source)
    digest = hashlib.sha256(raw).hexdigest()
    command = 'digest-upload ' + source.name + ' ' + digest
    try:
        result = subprocess.run(
            [ssh, '-T', '-o', 'BatchMode=yes', target, command], input=raw,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=60)
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError('digest_upload_transport_failed') from None
    expected = ('digest_upload_ok ' + source.name + ' ' + digest + '\n').encode('utf-8')
    if result.returncode != 0 or result.stdout != expected:
        raise RuntimeError('digest_upload_not_confirmed')
    return digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True,
                        help='Eng begrenzter SSH-Config-Alias, z. B. methodenbot-digest-upload')
    parser.add_argument('markdown')
    args = parser.parse_args()
    digest = upload(args.markdown, args.target)
    print('upload_verified sha256=' + digest)


if __name__ == '__main__':
    main()
