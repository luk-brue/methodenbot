"""Validated binary envelope for one Markdown digest and its Zotero RIS file."""

import struct


MAGIC = b'METHODENBOT_DIGEST_V2\n'
MAX_MARKDOWN_BYTES = 1_000_000
MAX_RIS_BYTES = 1_000_000
HEADER = struct.Struct('>II')
MAX_BUNDLE_BYTES = len(MAGIC) + HEADER.size + MAX_MARKDOWN_BYTES + MAX_RIS_BYTES


class DigestBundleError(RuntimeError):
    pass


def _text(raw, limit, error):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= limit:
        raise DigestBundleError(error)
    try:
        value = raw.decode('utf-8')
    except UnicodeError:
        raise DigestBundleError(error) from None
    if not value.strip() or '\x00' in value:
        raise DigestBundleError(error)
    return value


def validate_markdown(raw):
    return _text(raw, MAX_MARKDOWN_BYTES, 'invalid_digest_markdown')


def validate_ris(raw):
    text = _text(raw, MAX_RIS_BYTES, 'invalid_digest_ris')
    lines = text.splitlines()
    starts = sum(line == 'TY  - JOUR' for line in lines)
    ends = sum(line == 'ER  -' for line in lines)
    if starts == 0 or starts != ends:
        raise DigestBundleError('invalid_digest_ris')
    return text


def pack_bundle(markdown, ris):
    validate_markdown(markdown)
    validate_ris(ris)
    return MAGIC + HEADER.pack(len(markdown), len(ris)) + markdown + ris


def unpack_bundle(raw):
    if (not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BUNDLE_BYTES
            or not raw.startswith(MAGIC)
            or len(raw) < len(MAGIC) + HEADER.size):
        raise DigestBundleError('invalid_digest_bundle')
    offset = len(MAGIC)
    markdown_size, ris_size = HEADER.unpack(raw[offset:offset + HEADER.size])
    offset += HEADER.size
    if (not 0 < markdown_size <= MAX_MARKDOWN_BYTES
            or not 0 < ris_size <= MAX_RIS_BYTES
            or len(raw) != offset + markdown_size + ris_size):
        raise DigestBundleError('invalid_digest_bundle')
    markdown = raw[offset:offset + markdown_size]
    ris = raw[offset + markdown_size:]
    validate_markdown(markdown)
    validate_ris(ris)
    return markdown, ris
