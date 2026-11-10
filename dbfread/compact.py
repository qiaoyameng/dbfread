"""
Compact DBF/memo file pairs by copying only live memo blocks.

The public entry point is ``compact_copy()``. It reads a DBF table
together with its memo file, collects the memo blocks that are actually
referenced by records, and writes a compacted copy of both files to a
new target directory. The original files are never modified.

Supported memo formats:

- Visual FoxPro FPT
- dBase III DBT

Sharing of memo blocks is preserved: two records pointing at the same
block will still point at the same (new) block after compaction. Two
different pointers are never merged, even if their content is
identical.
"""
import os
import struct
from collections import namedtuple

from .dbf import DBF
from .memo import VFPFileHeader, VFPMemoHeader
from .exceptions import InvalidMemoError

VFP_HEADER_SIZE = 512
DB3_BLOCK_SIZE = 512

# Field types that hold a memo block pointer. ('B' is only a memo
# pointer in dBase files; in Visual FoxPro it is a double.)
MEMO_FIELD_TYPES = 'MGP'
VFP_DBVERSIONS = (0x30, 0x31, 0x32)

CompactResult = namedtuple('CompactResult', [
    'dbf_path',        # path of the new DBF file
    'memo_path',       # path of the new memo file
    'records',         # number of live records written
    'deleted_records',  # number of deleted records written
    'memos',           # number of unique memo blocks copied
    'blocks_before',   # allocated blocks in the old memo file
    'blocks_after',    # allocated blocks in the new memo file
])


def _is_memo_field(field, dbversion):
    if field.type in MEMO_FIELD_TYPES:
        return True
    # dBase stores a memo index in 'B' fields, Visual FoxPro a double.
    return field.type == 'B' and dbversion not in VFP_DBVERSIONS


def _parse_memo_pointer(data):
    if len(data) == 4:
        return struct.unpack('<I', data)[0]
    try:
        return int(data)
    except ValueError:
        if data.strip(b' \x00') == b'':
            return 0
        raise InvalidMemoError(
            'memo index is not an integer: {!r}'.format(bytes(data)))


def _encode_memo_pointer(field, pointer):
    if field.length == 4:
        return struct.pack('<I', pointer)
    data = str(pointer).encode('ascii')
    if len(data) > field.length:
        raise ValueError(
            'new memo pointer {} does not fit in {} bytes'.format(
                pointer, field.length))
    return data.rjust(field.length, b' ')


class _VFPMemoLayout(object):
    """Reads and rewrites Visual FoxPro FPT memo files."""

    padding = b'\x00'
    header_size = VFP_HEADER_SIZE

    def __init__(self, filename):
        self.filename = filename
        self.filesize = os.path.getsize(filename)
        self._file = open(filename, 'rb')
        try:
            header_data = self._file.read(VFP_HEADER_SIZE)
            if len(header_data) != VFP_HEADER_SIZE:
                raise InvalidMemoError(
                    'FPT header is incomplete: {!r}'.format(filename))
            self._header_data = bytearray(header_data)
            header = VFPFileHeader.unpack(bytes(header_data))
            self.blocksize = header.blocksize or 512
            # ceil(header size / block size)
            self.first_block = -(-VFP_HEADER_SIZE // self.blocksize)
            if self.first_block * self.blocksize > self.filesize:
                raise InvalidMemoError(
                    'FPT block size {} is inconsistent with file size '
                    '{}'.format(self.blocksize, self.filesize))
            self.blocks_before = header.nextblock
        except Exception:
            self._file.close()
            raise

    def close(self):
        self._file.close()

    def read_memo(self, pointer):
        if pointer < self.first_block:
            raise InvalidMemoError(
                'memo pointer {} points into the FPT header'.format(
                    pointer))
        offset = pointer * self.blocksize
        if offset + VFPMemoHeader.size > self.filesize:
            raise InvalidMemoError(
                'memo pointer {} is past end of file'.format(pointer))
        self._file.seek(offset)
        memo_header = VFPMemoHeader.read(self._file)
        data = self._file.read(memo_header.length)
        if len(data) != memo_header.length:
            raise InvalidMemoError(
                'memo at block {} is truncated'.format(pointer))
        return struct.pack('>LL', memo_header.type,
                           memo_header.length) + data

    def make_header(self, next_block):
        header = bytearray(self._header_data)
        header[0:4] = struct.pack('>L', next_block)
        return bytes(header)


class _DB3MemoLayout(object):
    """Reads and rewrites dBase III DBT memo files."""

    padding = b'\x1a'
    blocksize = DB3_BLOCK_SIZE
    header_size = DB3_BLOCK_SIZE
    first_block = 1

    def __init__(self, filename):
        self.filename = filename
        self.filesize = os.path.getsize(filename)
        if self.filesize < DB3_BLOCK_SIZE:
            raise InvalidMemoError(
                'DBT header is incomplete: {!r}'.format(filename))
        self._file = open(filename, 'rb')
        try:
            self._header_data = bytearray(self._file.read(DB3_BLOCK_SIZE))
            self.blocks_before = -(-self.filesize // DB3_BLOCK_SIZE)
        except Exception:
            self._file.close()
            raise

    def close(self):
        self._file.close()

    def read_memo(self, pointer):
        if pointer < self.first_block:
            raise InvalidMemoError(
                'memo pointer {} points into the DBT header'.format(
                    pointer))
        offset = pointer * self.blocksize
        if offset >= self.filesize:
            raise InvalidMemoError(
                'memo pointer {} is past end of file'.format(pointer))
        self._file.seek(offset)
        data = self._file.read()
        end = data.find(b'\x1a')
        if end == -1:
            raise InvalidMemoError(
                'memo at block {} has no end marker'.format(pointer))
        return data[:end + 1]

    def make_header(self, next_block):
        header = bytearray(self._header_data)
        header[0:4] = struct.pack('>L', next_block)
        return bytes(header)


def _open_memo_layout(memofilename, dbversion):
    lower = memofilename.lower()
    if lower.endswith('.fpt'):
        return _VFPMemoLayout(memofilename)
    elif lower.endswith('.dbt'):
        if dbversion == 0x83:
            return _DB3MemoLayout(memofilename)
        raise NotImplementedError(
            'only dBase III DBT files are supported, '
            'got dbversion {:#04x}'.format(dbversion))
    else:
        raise ValueError(
            'unknown memo file type: {!r}'.format(memofilename))


def _read_raw_records(table):
    """Read records as (separator, data) pairs, preserving raw bytes."""
    records = []
    with open(table.filename, 'rb') as infile:
        infile.seek(table.header.headerlen)
        while True:
            sep = infile.read(1)
            if sep in (b' ', b'*'):
                data = infile.read(table.header.recordlen - 1)
                if len(data) != table.header.recordlen - 1:
                    raise ValueError(
                        'unexpected end of file inside a record')
                records.append((sep, bytearray(data)))
            elif sep in (b'\x1a', b''):
                break
            else:
                raise ValueError(
                    'unknown record separator: {!r}'.format(sep))
    return records


def compact_copy(dbf_filename, target_dir, keep_deleted=True,
                 ignorecase=True):
    """Write a compacted copy of a DBF table and its memo file.

    Only memo blocks referenced by records are copied, and the blocks
    are laid out contiguously. Memo pointers in the records and the
    record count in the DBF header are updated; every other byte of
    the records and the field headers is preserved.

    ``keep_deleted`` controls whether deleted records (and the memo
    blocks only they reference) are carried over.

    All memo references are validated before anything is written, and
    the new files appear in ``target_dir`` only when both are
    complete. Returns a ``CompactResult``.
    """
    table = DBF(dbf_filename, ignorecase=ignorecase)
    if table.memofilename is None:
        raise ValueError('table has no memo fields: {!r}'.format(
            table.filename))

    recordlen = 1 + sum(field.length for field in table.fields)
    if recordlen != table.header.recordlen:
        raise ValueError(
            'record length in header ({}) does not match field '
            'lengths ({})'.format(table.header.recordlen, recordlen))

    # Offsets are relative to the record data, which does not
    # include the deletion flag.
    memo_fields = []
    offset = 0
    for field in table.fields:
        if _is_memo_field(field, table.header.dbversion):
            memo_fields.append((offset, field))
        offset += field.length

    layout = _open_memo_layout(table.memofilename,
                               table.header.dbversion)
    try:
        records = _read_raw_records(table)
        kept_records = [
            (sep, data) for sep, data in records
            if sep == b' ' or keep_deleted
        ]

        # Collect and validate every referenced memo block before
        # anything is written. A bad pointer raises InvalidMemoError
        # here instead of being treated as an empty memo.
        mapping = {}       # old pointer -> new pointer
        entries = []       # raw memo bytes in write order
        new_pointers = []
        next_block = layout.first_block
        for sep, data in kept_records:
            for field_offset, field in memo_fields:
                raw = data[field_offset:field_offset + field.length]
                pointer = _parse_memo_pointer(raw)
                if pointer == 0 or pointer in mapping:
                    continue
                content = layout.read_memo(pointer)
                mapping[pointer] = next_block
                new_pointers.append(next_block)
                entries.append(content)
                next_block += -(-len(content) // layout.blocksize)

        blocks_before = layout.blocks_before
        header = layout.make_header(next_block)
    finally:
        layout.close()

    with open(table.filename, 'rb') as infile:
        dbf_header = infile.read(table.header.headerlen)

    if not os.path.isdir(target_dir):
        os.makedirs(target_dir)
    dest_dbf = os.path.join(target_dir, os.path.basename(table.filename))
    dest_memo = os.path.join(target_dir,
                             os.path.basename(table.memofilename))

    for dest, source in ((dest_dbf, table.filename),
                         (dest_memo, table.memofilename)):
        if os.path.abspath(dest) == os.path.abspath(source):
            raise ValueError(
                'target directory must not be the source directory')
        if os.path.exists(dest):
            raise IOError('target file already exists: {!r}'.format(dest))

    tmp_dbf = '{}.compact-{}.tmp'.format(dest_dbf, os.getpid())
    tmp_memo = '{}.compact-{}.tmp'.format(dest_memo, os.getpid())
    try:
        _write_memo_file(tmp_memo, layout, header, new_pointers, entries)
        _write_dbf(tmp_dbf, dbf_header, kept_records, memo_fields,
                   mapping)
        # The pair becomes visible only when both files are complete.
        os.rename(tmp_memo, dest_memo)
        try:
            os.rename(tmp_dbf, dest_dbf)
        except Exception:
            os.remove(dest_memo)
            raise
    except Exception:
        for path in (tmp_memo, tmp_dbf):
            if os.path.exists(path):
                os.remove(path)
        raise

    return CompactResult(
        dbf_path=dest_dbf,
        memo_path=dest_memo,
        records=sum(1 for sep, _ in kept_records if sep == b' '),
        deleted_records=sum(1 for sep, _ in kept_records if sep == b'*'),
        memos=len(entries),
        blocks_before=blocks_before,
        blocks_after=next_block,
    )


def _write_memo_file(path, layout, header, new_pointers, entries):
    blocksize = layout.blocksize
    with open(path, 'wb') as outfile:
        outfile.write(header)
        offset = len(header)
        for pointer, content in zip(new_pointers, entries):
            start = pointer * blocksize
            if start < offset:
                raise AssertionError('memo blocks overlap')
            outfile.write(layout.padding * (start - offset))
            outfile.write(content)
            offset = start + len(content)
            remainder = offset % blocksize
            if remainder:
                pad = blocksize - remainder
                outfile.write(layout.padding * pad)
                offset += pad
        outfile.flush()
        os.fsync(outfile.fileno())


def _write_dbf(path, dbf_header, records, memo_fields, mapping):
    header = bytearray(dbf_header)
    # Record count is the only header value that may change.
    struct.pack_into('<L', header, 4, len(records))
    with open(path, 'wb') as outfile:
        outfile.write(bytes(header))
        for sep, data in records:
            outfile.write(sep)
            for field_offset, field in memo_fields:
                raw = data[field_offset:field_offset + field.length]
                pointer = _parse_memo_pointer(raw)
                if pointer:
                    data[field_offset:field_offset + field.length] = \
                        _encode_memo_pointer(field, mapping[pointer])
            outfile.write(bytes(data))
        outfile.write(b'\x1a')
        outfile.flush()
        os.fsync(outfile.fileno())
