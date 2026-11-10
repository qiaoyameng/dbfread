import os
import struct
import hashlib

import pytest

from dbfread import DBF, compact_copy
from dbfread.exceptions import InvalidMemoError

VFP_BLOCKSIZE = 64
DB3_BLOCKSIZE = 512

MEMO_A = b'Shared memo text.'
MEMO_B = b'B' * 70  # Spans two 64-byte blocks with its header.
MEMO_C = b'Deleted guy memo.'
MEMO_D = MEMO_A  # Same content as A, but referenced by its own pointer.


def _vfp_memo(data, memo_type=1):
    return struct.pack('>LL', memo_type, len(data)) + data


def _write_dbf(path, dbversion, fields, records):
    """Write a minimal DBF file.

    fields: [(name, type, length)], records: [(deleted, [raw values])].
    """
    headerlen = 32 + 32 * len(fields) + 1
    recordlen = 1 + sum(length for _, _, length in fields)
    header = bytearray(32)
    header[0] = dbversion
    header[1:4] = bytes([26, 11, 10])
    struct.pack_into('<L', header, 4, len(records))
    struct.pack_into('<H', header, 8, headerlen)
    struct.pack_into('<H', header, 10, recordlen)
    out = bytearray(header)
    for name, field_type, length in fields:
        desc = bytearray(32)
        desc[0:len(name)] = name.encode('ascii')
        desc[11] = ord(field_type)
        desc[16] = length
        out += desc
    out += b'\r'
    for deleted, values in records:
        out += b'*' if deleted else b' '
        for value, (_, _, length) in zip(values, fields):
            assert len(value) == length
            out += value
    out += b'\x1a'
    with open(str(path), 'wb') as outfile:
        outfile.write(bytes(out))


def _name(value):
    return value.encode('ascii').ljust(10, b' ')


def _raw_records(path):
    """Return raw records (separator + data) from a DBF file."""
    with open(str(path), 'rb') as infile:
        data = infile.read()
    headerlen, recordlen = struct.unpack_from('<HH', data, 8)
    records = []
    pos = headerlen
    while data[pos:pos + 1] in (b' ', b'*'):
        records.append(data[pos:pos + recordlen])
        pos += recordlen
    return records


def _sha1(path):
    with open(str(path), 'rb') as infile:
        return hashlib.sha1(infile.read()).hexdigest()


def make_vfp_pair(dirpath, extra_records=()):
    """Visual FoxPro DBF/FPT pair with shared pointers, an empty memo,
    unreferenced hole blocks and a deleted record."""
    dbf_path = os.path.join(str(dirpath), 'vfp_table.dbf')
    fpt_path = os.path.join(str(dirpath), 'vfp_table.fpt')

    def ptr(pointer):
        return struct.pack('<I', pointer)

    fields = [('NAME', 'C', 10), ('MEMO', 'M', 4)]
    records = [
        (False, [_name('Alice'), ptr(8)]),
        (False, [_name('Bob'), ptr(8)]),    # shared with Alice
        (False, [_name('Carol'), ptr(0)]),  # empty memo
        (False, [_name('Dave'), ptr(10)]),
        (True, [_name('Eve'), ptr(13)]),    # deleted record
        (False, [_name('Frank'), ptr(14)]),
    ] + list(extra_records)
    _write_dbf(dbf_path, 0x30, fields, records)

    next_block = 15
    header = bytearray(512)
    struct.pack_into('>L', header, 0, next_block)
    struct.pack_into('>H', header, 6, VFP_BLOCKSIZE)
    body = bytearray((next_block - 8) * VFP_BLOCKSIZE)

    def put(block, content):
        start = (block - 8) * VFP_BLOCKSIZE
        body[start:start + len(content)] = content

    put(8, _vfp_memo(MEMO_A))
    put(9, b'X' * VFP_BLOCKSIZE)            # unreferenced hole
    put(10, _vfp_memo(MEMO_B))              # spans blocks 10-11
    put(12, b'Y' * VFP_BLOCKSIZE)           # unreferenced hole
    put(13, _vfp_memo(MEMO_C))
    put(14, _vfp_memo(MEMO_D))
    with open(fpt_path, 'wb') as outfile:
        outfile.write(bytes(header) + bytes(body))
    return dbf_path


def make_db3_pair(dirpath, extra_records=()):
    """dBase III DBF/DBT pair with shared pointers, an empty memo,
    an unreferenced hole block and a deleted record."""
    dbf_path = os.path.join(str(dirpath), 'db3_table.dbf')
    dbt_path = os.path.join(str(dirpath), 'db3_table.dbt')

    def ptr(pointer):
        return str(pointer).encode('ascii').rjust(10, b' ')

    fields = [('NAME', 'C', 10), ('MEMO', 'M', 10)]
    records = [
        (False, [_name('Alice'), ptr(1)]),
        (False, [_name('Bob'), ptr(1)]),      # shared with Alice
        (False, [_name('Carol'), b' ' * 10]),  # empty memo
        (True, [_name('Eve'), ptr(3)]),       # deleted record
        (False, [_name('Frank'), ptr(4)]),
    ] + list(extra_records)
    _write_dbf(dbf_path, 0x83, fields, records)

    next_block = 5
    header = bytearray(DB3_BLOCKSIZE)
    struct.pack_into('>L', header, 0, next_block)
    body = bytearray((next_block - 1) * DB3_BLOCKSIZE)

    def put(block, content):
        start = (block - 1) * DB3_BLOCKSIZE
        body[start:start + len(content)] = content

    put(1, b'Shared dbt memo.\x1a')
    put(2, b'Z' * DB3_BLOCKSIZE)  # unreferenced hole
    put(3, b'Deleted dbt memo.\x1a')
    put(4, b'Shared dbt memo.\x1a')  # same content as block 1
    with open(dbt_path, 'wb') as outfile:
        outfile.write(bytes(header) + bytes(body))
    return dbf_path


def test_compact_vfp_keep_deleted(tmp_path):
    dbf_path = make_vfp_pair(tmp_path)
    old_hashes = {_sha1(dbf_path), _sha1(dbf_path[:-4] + '.fpt')}
    old_records = list(DBF(dbf_path))
    old_deleted = list(DBF(dbf_path).deleted)

    target = str(tmp_path / 'out')
    result = compact_copy(dbf_path, target)

    # Both files appear together in the new directory.
    assert sorted(os.listdir(target)) == ['vfp_table.dbf',
                                          'vfp_table.fpt']
    # Holes are gone: 15 allocated blocks become 8 header blocks plus
    # 1 + 2 + 1 + 1 memo blocks.
    assert result.blocks_before == 15
    assert result.blocks_after == 13
    assert result.memos == 4
    assert os.path.getsize(result.memo_path) == 13 * VFP_BLOCKSIZE

    # Parsed values are unchanged, including deleted records.
    assert list(DBF(result.dbf_path)) == old_records
    assert list(DBF(result.dbf_path).deleted) == old_deleted

    # Raw records: only the memo pointer bytes may differ.
    old_raw = _raw_records(dbf_path)
    new_raw = _raw_records(result.dbf_path)
    assert len(old_raw) == len(new_raw)
    for old, new in zip(old_raw, new_raw):
        assert old[0] == new[0]            # deletion flag preserved
        assert old[1:11] == new[1:11]      # other fields preserved

    # Shared pointer stays shared.
    alice_ptr = struct.unpack('<I', new_raw[0][11:15])[0]
    bob_ptr = struct.unpack('<I', new_raw[1][11:15])[0]
    assert alice_ptr == bob_ptr
    # Same content under a different old pointer is not merged.
    frank_ptr = struct.unpack('<I', new_raw[5][11:15])[0]
    assert frank_ptr != alice_ptr
    # Empty memo is still empty.
    assert new_raw[2][11:15] == b'\x00' * 4
    # New pointers are densely packed from the first data block.
    pointers = sorted(
        struct.unpack('<I', record[11:15])[0]
        for record in new_raw
        if record[11:15] != b'\x00' * 4
    )
    assert pointers == [8, 8, 9, 11, 12]

    # The original files are untouched.
    assert _sha1(dbf_path) in old_hashes
    assert _sha1(dbf_path[:-4] + '.fpt') in old_hashes


def test_compact_vfp_drop_deleted(tmp_path):
    dbf_path = make_vfp_pair(tmp_path)
    old_records = list(DBF(dbf_path))

    result = compact_copy(dbf_path, str(tmp_path / 'out'),
                          keep_deleted=False)

    assert result.records == 5
    assert result.deleted_records == 0
    # The deleted record's memo block is dropped too.
    assert result.memos == 3
    assert result.blocks_after == 12

    new_table = DBF(result.dbf_path)
    assert list(new_table) == old_records
    assert list(new_table.deleted) == []
    assert new_table.header.numrecords == 5
    assert all(record[0:1] == b' ' for record in
               _raw_records(result.dbf_path))


def test_compact_db3_keep_deleted(tmp_path):
    dbf_path = make_db3_pair(tmp_path)
    old_records = list(DBF(dbf_path))
    old_deleted = list(DBF(dbf_path).deleted)

    target = str(tmp_path / 'out')
    result = compact_copy(dbf_path, target)

    assert sorted(os.listdir(target)) == ['db3_table.dbf',
                                          'db3_table.dbt']
    # The hole block 2 is gone: 5 blocks become 4.
    assert result.blocks_before == 5
    assert result.blocks_after == 4
    assert result.memos == 3
    assert os.path.getsize(result.memo_path) == 4 * DB3_BLOCKSIZE

    assert list(DBF(result.dbf_path)) == old_records
    assert list(DBF(result.dbf_path).deleted) == old_deleted

    old_raw = _raw_records(dbf_path)
    new_raw = _raw_records(result.dbf_path)
    for old, new in zip(old_raw, new_raw):
        assert old[0] == new[0]
        assert old[1:11] == new[1:11]

    # Shared pointer stays shared, equal content is not merged.
    assert new_raw[0][11:21] == new_raw[1][11:21]
    assert new_raw[4][11:21] != new_raw[0][11:21]
    # Empty memo is still empty.
    assert new_raw[2][11:21] == b' ' * 10
    # Pointers are rewritten to the dense layout 1, 2, 3.
    pointers = sorted(
        int(record[11:21]) for record in new_raw
        if record[11:21].strip()
    )
    assert pointers == [1, 1, 2, 3]


def test_compact_db3_drop_deleted(tmp_path):
    dbf_path = make_db3_pair(tmp_path)
    result = compact_copy(dbf_path, str(tmp_path / 'out'),
                          keep_deleted=False)

    assert result.records == 4
    assert result.deleted_records == 0
    assert result.memos == 2
    assert result.blocks_after == 3
    assert list(DBF(result.dbf_path).deleted) == []


def test_compact_vfp_bad_pointer(tmp_path):
    dbf_path = make_vfp_pair(tmp_path, extra_records=[
        (False, [_name('Mallory'), struct.pack('<I', 99)]),
    ])
    target = tmp_path / 'out'

    with pytest.raises(InvalidMemoError):
        compact_copy(dbf_path, str(target))

    # A bad pointer must not become an empty memo, and no
    # seemingly complete target is left behind.
    assert not os.path.exists(str(target)) or os.listdir(str(target)) == []


def test_compact_db3_bad_pointer(tmp_path):
    dbf_path = make_db3_pair(tmp_path, extra_records=[
        (False, [_name('Mallory'), str(99).encode('ascii').rjust(10)]),
    ])
    target = tmp_path / 'out'

    with pytest.raises(InvalidMemoError):
        compact_copy(dbf_path, str(target))

    assert not os.path.exists(str(target)) or os.listdir(str(target)) == []


def test_compact_db3_unterminated_memo(tmp_path):
    # A referenced block without an end marker is a layout error,
    # not an empty memo.
    dbf_path = make_db3_pair(tmp_path, extra_records=[
        (False, [_name('Mallory'), str(5).encode('ascii').rjust(10)]),
    ])
    # Block 5 is the last block and has no end marker before EOF.
    with open(dbf_path[:-4] + '.dbt', 'ab') as outfile:
        outfile.write(b'Z' * DB3_BLOCKSIZE)
    with pytest.raises(InvalidMemoError):
        compact_copy(dbf_path, str(tmp_path / 'out'))


def test_compact_refuses_source_directory(tmp_path):
    dbf_path = make_vfp_pair(tmp_path)
    with pytest.raises(ValueError):
        compact_copy(dbf_path, str(tmp_path))


def test_compact_refuses_existing_target(tmp_path):
    dbf_path = make_vfp_pair(tmp_path)
    target = str(tmp_path / 'out')
    compact_copy(dbf_path, target)
    with pytest.raises(IOError):
        compact_copy(dbf_path, target)
