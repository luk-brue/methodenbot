#!/usr/bin/env python3
"""Upload one generated digest through a restricted SSH configuration alias."""

import argparse
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess

from digest_bundle import (DigestBundleError, MAX_MARKDOWN_BYTES, MAX_RIS_BYTES,
                           pack_bundle)

MARKDOWN_NAME = re.compile(r'(\d{4}-\d{2}-\d{2})-methoden-digest\.md')
SSH_TARGET = re.compile(r'[A-Za-z0-9_.@-]{1,255}')


def read_source(path, limit):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as handle:
            metadata = os.fstat(handle.fileno())
            if (not stat.S_ISREG(metadata.st_mode)
                    or not 0 < metadata.st_size <= limit):
                raise RuntimeError('unsafe_digest_source')
            raw = handle.read(limit + 1)
    except RuntimeError:
        raise
    except OSError:
        raise RuntimeError('digest_source_unreadable') from None
    if len(raw) > limit:
        raise RuntimeError('invalid_digest_source')
    return raw


def upload(markdown_path, ris_path, target, *, ssh='/usr/bin/ssh'):
    markdown_source, ris_source = Path(markdown_path), Path(ris_path)
    match = MARKDOWN_NAME.fullmatch(markdown_source.name)
    if match is None or ris_source.name != match.group(1) + '-methoden-artikel.ris':
        raise RuntimeError('invalid_digest_filename')
    if SSH_TARGET.fullmatch(target) is None:
        raise RuntimeError('invalid_ssh_target')
    try:
        raw = pack_bundle(read_source(markdown_source, MAX_MARKDOWN_BYTES),
                          read_source(ris_source, MAX_RIS_BYTES))
    except DigestBundleError as exc:
        raise RuntimeError(str(exc)) from None
    digest = hashlib.sha256(raw).hexdigest()
    command = 'digest-upload-v2 ' + match.group(1) + ' ' + digest
    try:
        result = subprocess.run(
            [ssh, '-T', '-o', 'BatchMode=yes', target, command], input=raw,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=60)
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError('digest_upload_transport_failed') from None
    expected = ('digest_upload_ok ' + match.group(1) + ' ' + digest + '\n').encode('utf-8')
    if result.returncode != 0 or result.stdout != expected:
        raise RuntimeError('digest_upload_not_confirmed')
    return digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True,
                        help='Eng begrenzter SSH-Config-Alias, z. B. methodenbot-digest-upload')
    parser.add_argument('markdown')
    parser.add_argument('ris')
    args = parser.parse_args()
    digest = upload(args.markdown, args.ris, args.target)
    print('upload_verified sha256=' + digest)


if __name__ == '__main__':
    main()
