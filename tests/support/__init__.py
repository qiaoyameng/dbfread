"""Helpers for building minimal DBF/FPT/DBT files in tests."""
import os
import struct


def _dbf_header(dbversion, numrecords, headerlen, recordlen, codepage=0):
    return struct.pack(
        '<BBBBLHHHBBLLLBBH',
        dbversion,
        126, 11, 10,          # date: 2026-11-10
        numrecords,
        headerlen,
        recordlen,
        0, 0, 0, 0, 0, 0, 0,
        codepage,
        0)


def _field_descriptor(name, field_type, length, address=0):
    encoded = name.encode('ascii')[:11].ljust(11, b'\0')
    return struct.pack('<11scLBBHBBBB7sB',
                       encoded,
                       field_type.encode('ascii'),
                       address,
                       length,
                       0, 0, 0, 0, 0, 0,
                       b'\0' * 7,
                       0)


def build_dbf(path, dbversion, fields, records, deleted=()):
    """Write a minimal DBF file.

    ``fields`` is a list of ``(name, type, length)`` tuples.
    ``records`` and ``deleted`` are lists of already-encoded fixed-width
    record bodies (without the leading status byte).
    """
    descriptors = b''.join(
        _field_descriptor(name, field_type, length)
        for name, field_type, length in fields)
    headerlen = 32 + 32 * len(fields) + 1
    recordlen = 1 + sum(length for _, _, length in fields)
    numrecords = len(records) + len(deleted)
    header = _dbf_header(dbversion, numrecords, headerlen, recordlen)
    body = b''
    for record in records:
        body += b' ' + record
    for record in deleted:
        body += b'*' + record
    with open(path, 'wb') as handle:
        handle.write(header)
        handle.write(descriptors)
        handle.write(b'\r')
        handle.write(body)
        handle.write(b'\x1a')
    return path


def build_fpt(path, blocks, blocksize=512, size_blocks=None):
    """Write an FPT file.

    ``blocks`` maps block number -> ``(type_code, data)``. The header
    claims one more free block than the highest used one.
    """
    highest = max(blocks) if blocks else 1
    header = struct.pack('>LHH504s', highest + 1, 0, blocksize, b'\0' * 504)
    with open(path, 'wb') as handle:
        handle.write(header)
        for block, (type_code, data) in sorted(blocks.items()):
            handle.seek(block * blocksize)
            handle.write(struct.pack('>LL', type_code, len(data)))
            handle.write(data)
        if size_blocks is None:
            size_blocks = highest + 1
        handle.truncate(size_blocks * blocksize)


def build_dbt(path, blocks, block_size=512):
    """Write a dBase III DBT file.

    ``blocks`` maps block number -> memo bytes, each 0x1a terminated and
    padded to whole 512-byte blocks.
    """
    with open(path, 'wb') as handle:
        # Block 0 is the DBT header block.
        handle.write(b'\0' * block_size)
        for block, data in sorted(blocks.items()):
            payload = data + b'\x1a'
            padded = payload + b'\0' * (
                block_size - len(payload) % block_size)
            handle.seek(block * block_size)
            handle.write(padded)


def build_db4_dbt(path, blocks, block_size=512):
    """Write a dBase IV DBT file.

    ``blocks`` maps block number -> memo bytes. Each block has an
    8-byte little-endian header (``reserved`` plus declared length).
    """
    with open(path, 'wb') as handle:
        handle.write(b'\0' * block_size)
        for block, data in sorted(blocks.items()):
            handle.seek(block * block_size)
            handle.write(struct.pack('<LL', 0x0808ffff, len(data)))
            handle.write(data)


def memo_pointer(number):
    return struct.pack('<I', number)


def truncate_file(path, size):
    with open(path, 'r+b') as handle:
        handle.truncate(size)


def write(path, data):
    with open(path, 'wb') as handle:
        handle.write(data)


def open_count():
    """Number of file descriptors currently held open by this process."""
    return len(os.listdir('/proc/self/fd'))
