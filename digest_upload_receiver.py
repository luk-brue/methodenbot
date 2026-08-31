#!/usr/bin/env python3
"""Restricted-SSH receiver for one validated Methoden-Digest Markdown file."""

import hashlib
import os
from pathlib import Path
import re
import stat
import sys
import uuid


MAX_DIGEST_BYTES = 1_000_000
COMMAND = re.compile(
    r'digest-upload (\d{4}-\d{2}-\d{2}-methoden-digest\.md) ([0-9a-f]{64})')


def fail(code):
    print(code, file=sys.stderr)
    return 1


def main():
    match = COMMAND.fullmatch(os.environ.get('SSH_ORIGINAL_COMMAND', ''))
    if match is None:
        return fail('invalid_digest_upload_command')
    target_directory = Path(os.environ.get(
        'METHODENBOT_DIGEST_INBOX', '/var/lib/methodenbot/digest/inbox'))
    try:
        metadata = os.lstat(target_directory)
    except OSError:
        return fail('digest_inbox_unavailable')
    if (not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700
            or metadata.st_uid != os.geteuid()):
        return fail('unsafe_digest_inbox')
    raw = sys.stdin.buffer.read(MAX_DIGEST_BYTES + 1)
    if not 0 < len(raw) <= MAX_DIGEST_BYTES:
        return fail('invalid_digest_upload_size')
    try:
        text = raw.decode('utf-8')
    except UnicodeError:
        return fail('digest_not_utf8')
    if not text.strip() or hashlib.sha256(raw).hexdigest() != match.group(2):
        return fail('digest_upload_hash_mismatch')
    target = target_directory / match.group(1)
    temporary = target_directory / ('.upload.' + uuid.uuid4().hex)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        directory_fd = os.open(target_directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        return fail('digest_upload_write_failed')
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    print('digest_upload_ok ' + match.group(1) + ' ' + match.group(2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
