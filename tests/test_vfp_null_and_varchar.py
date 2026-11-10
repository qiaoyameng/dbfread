"""
Tests for Visual FoxPro nullable fields and Varchar/Varbinary support.

All DBF files used here are generated with struct in tests/vfp_support.py
so the exact on-disk layout is documented next to the bytes themselves.
"""
import datetime
import os

import pytest

from dbfread import DBF, FieldParser, InvalidRecord

from tests.vfp_support import (
    build_dbf,
    field_descriptor,
    file_header,
    varchar_bytes,
    varbinary_bytes,
    fixed_bytes,
    RECORD_NORMAL,
    RECORD_DELETED,
    HEADER_SIZE,
    FIELD_SIZE,
)
from dbfread.dbf import DBFField


# Field layout of the main nullable table.
# V and Q fields consume width + 1 bytes in each record even though the
# descriptor length is the width only.
FIELD_SPECS = [
    ('NAME', 'V', 6),     # varchar, 7 record bytes
    ('DATA', 'Q', 5),     # varbinary, 6 record bytes
    ('NOTE', 'C', 8),     # fixed character, 8 record bytes
    ('BIRTH', 'D', 8),    # date, 8 record bytes
]


def _bitmap(names_null):
    """Build a one-byte nullable bitmap for the four fields above."""
    bits = 0
    for index in names_null:
        bits |= 1 << index
    return bytes([bits])


def _record_bytes(name, data, note, birth, nulls):
    return (
        RECORD_NORMAL,
        varchar_bytes(name, 6)
        + varbinary_bytes(data, 5)
        + fixed_bytes(note, 8)
        + birth,
        _bitmap(nulls),
    )


@pytest.fixture
def vfp_path(tmp_path):
    path = str(tmp_path / 'nullable.dbf')
    # Record 1: a full-width varchar/varbinary and a normal fixed field.
    record1 = _record_bytes(
        'abcdef', b'\x00\x01\x02\x03\x04', 'note one',
        b'19730721', [])
    # Record 2: NULL, empty string and zero-length binary in the same row.
    record2 = _record_bytes(
        '', b'', 'note two', b'19840229', [0])
    # Record 3: two NULLs plus an empty varchar.
    record3 = _record_bytes(
        '', b'\xff\xfe', '', b'        ', [1, 2])
    build_dbf(path, 0x30, FIELD_SPECS, [record1, record2, record3])
    return path


def test_hidden_null_flags_field_is_not_exposed(vfp_path):
    table = DBF(vfp_path)
    assert table.field_names == ['NAME', 'DATA', 'NOTE', 'BIRTH']
    assert '_NullFlags' not in table.field_names
    assert all(field.type != '0' for field in table.fields)
    record = next(iter(table))
    assert '_NullFlags' not in record


def test_hidden_field_is_detected_before_lowernames_renames_it(vfp_path):
    table = DBF(vfp_path, lowernames=True)
    assert '_nullflags' not in table.field_names
    assert table.field_names == ['name', 'data', 'note', 'birth']
    assert table._null_flags_field is not None
    assert list(table)[0]['name'] == 'abcdef'


def test_full_width_values(vfp_path):
    record = list(DBF(vfp_path))[0]
    assert record['NAME'] == 'abcdef'
    assert record['DATA'] == b'\x00\x01\x02\x03\x04'
    assert record['NOTE'] == 'note one'
    assert record['BIRTH'] == datetime.date(1973, 7, 21)


def test_null_empty_string_and_empty_bytes_are_distinguishable(vfp_path):
    records = list(DBF(vfp_path))

    # Record 2: NAME is NULL while DATA is a zero-length byte string.
    assert records[1]['NAME'] is None
    assert records[1]['DATA'] == b''
    assert records[1]['NOTE'] == 'note two'

    # Record 3: an empty (not NULL) varchar next to two NULL fields.
    assert records[2]['NAME'] == ''
    assert records[2]['DATA'] is None
    assert records[2]['NOTE'] is None
    assert records[2]['BIRTH'] is None


def test_nullable_bitmap_layout_uses_field_descriptor_order(vfp_path):
    table = DBF(vfp_path)
    # Four visible fields -> one bitmap byte, bits 0..3 in field order.
    assert table._null_flags_field.length == 1
    assert table._null_bit_field == {
        'NAME': (0, 0),
        'DATA': (0, 1),
        'NOTE': (0, 2),
        'BIRTH': (0, 3),
    }


def test_deleted_records_share_the_same_parsing(vfp_path):
    deleted = (
        RECORD_DELETED,
        varchar_bytes('gone', 6)
        + varbinary_bytes(b'', 5)
        + fixed_bytes('bye', 8)
        + b'20010101',
        _bitmap([0]),
    )
    with open(vfp_path, 'r+b') as out:
        # Insert the deleted record before the 0x1a terminator.
        out.seek(-1, os.SEEK_END)
        terminator = out.read(1)
        out.seek(-1, os.SEEK_END)
        out.write(b''.join(deleted))
        out.write(terminator)

    # The header count does not include the appended record, but the
    # deleted iterator does not rely on it.
    deleted_records = list(DBF(vfp_path).deleted)
    assert len(deleted_records) == 1
    record = deleted_records[0]
    assert record['NAME'] is None
    assert record['DATA'] == b''
    assert record['NOTE'] == 'bye'


def test_load_and_unload_give_identical_records(vfp_path):
    loaded = DBF(vfp_path, load=True)
    streamed = DBF(vfp_path)
    assert loaded.loaded
    assert not streamed.loaded
    assert list(loaded) == list(streamed)
    assert list(loaded.deleted) == list(streamed.deleted)

    loaded.unload()
    assert not loaded.loaded
    assert list(loaded) == list(streamed)
    assert list(loaded.deleted) == list(streamed.deleted)


def test_raw_mode_returns_original_field_bytes(vfp_path):
    table = DBF(vfp_path, raw=True)
    record = list(table)[1]
    # Raw mode bypasses both the NULL bitmap semantics and the
    # Varchar/Varbinary length byte interpretation.
    assert record['NAME'] == b''.ljust(6, b'\x00') + b'\x00'
    assert record['DATA'] == b''.ljust(5, b'\x00') + b'\x00'
    assert record['NOTE'] == b'note two'
    assert '_NullFlags' not in record


class UpperCaseParser(FieldParser):
    def parseV(self, field, data):
        return FieldParser.parseV(self, field, data).upper()

    def parseQ(self, field, data):
        return FieldParser.parseQ(self, field, data)


def test_custom_parserclass_is_used_and_nulls_bypass_it(vfp_path):
    records = list(DBF(vfp_path, parserclass=UpperCaseParser))
    assert records[0]['NAME'] == 'ABCDEF'
    assert records[0]['DATA'] == b'\x00\x01\x02\x03\x04'
    # The NULL short-circuit happens before parseV is called.
    assert records[1]['NAME'] is None


def test_encoding_is_used_for_varchar_decoding(tmp_path):
    path = str(tmp_path / 'gbk.dbf')
    specs = [('NAME', 'V', 8)]
    value = '中文'
    encoded = value.encode('gbk')
    record = (
        RECORD_NORMAL,
        encoded.ljust(8, b'\x00') + bytes([len(encoded)]),
        b'\x00',  # one nullable bitmap byte
    )
    # Language driver 0x4d maps to cp936 (GBK).
    build_dbf(path, 0x32, specs, [record], language_driver=0x4d)
    record = next(iter(DBF(path)))
    assert record['NAME'] == value


def test_varbinary_is_not_decoded(vfp_path):
    record = list(DBF(vfp_path))[0]
    assert isinstance(record['DATA'], bytes)


def test_varchar_without_null_flags_still_uses_length_byte(tmp_path):
    # A 0x32 table can contain V fields without a nullable bitmap.
    path = str(tmp_path / 'varchar_only.dbf')
    specs = [('NAME', 'V', 6)]
    build_dbf(path, 0x32, specs,
              [(RECORD_NORMAL, varchar_bytes('hi', 6), None)],
              null_flags=False)
    table = DBF(path)
    assert table._null_flags_field is None
    assert next(iter(table))['NAME'] == 'hi'


def test_bitmap_grows_with_the_number_of_fields(tmp_path):
    path = str(tmp_path / 'wide.dbf')
    specs = [('F{}'.format(i), 'C', 2) for i in range(9)]
    bitmap = bytes([0, 1])  # bit 8 (the ninth field) set
    field_bytes = b''.join(fixed_bytes('x', 2) for _ in specs)
    build_dbf(path, 0x30, specs,
              [(RECORD_NORMAL, field_bytes, bitmap)])
    table = DBF(path)
    assert table._null_flags_field.length == 2
    records = list(table)
    assert records[0]['F0'] == 'x'
    assert records[0]['F8'] is None


def _write_raw(path, header, descriptors, body):
    with open(path, 'wb') as out:
        out.write(header)
        out.write(descriptors)
        out.write(b'\r')
        out.write(body)
        out.write(b'\x1a')


def test_bad_bitmap_length_in_descriptor_is_rejected(tmp_path):
    path = str(tmp_path / 'bad_bitmap.dbf')
    specs = FIELD_SPECS
    descriptors = b''.join(
        field_descriptor(name, ftype, width)
        for name, ftype, width in specs)
    # Four fields need one bitmap byte; claim two instead.
    descriptors += field_descriptor('_NullFlags', '0', 2, system=True)
    record_len = 1 + sum(
        width + (1 if ftype in ('V', 'Q') else 0)
        for _, ftype, width in specs) + 2
    header = file_header(0x30, 0, len(specs) + 1, record_len)
    _write_raw(path, header, descriptors, b'')

    with pytest.raises(ValueError) as excinfo:
        DBF(path)
    assert '_NullFlags' in str(excinfo.value)


def test_truncated_record_reports_number_and_field(tmp_path):
    path = str(tmp_path / 'truncated.dbf')
    specs = [('A', 'C', 4), ('B', 'V', 4)]
    good = (
        RECORD_NORMAL,
        fixed_bytes('aaaa', 4) + varchar_bytes('ok', 4),
        b'\x00',
    )
    # Field B has its trailing length byte corrupted to 255, which is
    # outside the declared width of 4.
    bad = (
        RECORD_NORMAL,
        fixed_bytes('cccc', 4) + b'dddd' + bytes([255]),
        b'\x00',
    )
    build_dbf(path, 0x30, specs, [good, bad])

    table = DBF(path)
    iterator = iter(table)
    next(iterator)  # first record still reads
    with pytest.raises(InvalidRecord) as excinfo:
        next(iterator)
    error = excinfo.value
    assert error.record_number == 2
    assert error.field_name == 'B'
    assert 'record 2' in str(error)
    assert 'B' in str(error)


def test_short_record_bitmap_reports_number_and_field(tmp_path):
    path = str(tmp_path / 'short.dbf')
    specs = [('A', 'C', 4)]
    good = (RECORD_NORMAL, fixed_bytes('aaaa', 4), b'\x00')
    build_dbf(path, 0x30, specs, [good, good])
    # Drop the terminator and the bitmap byte of the second record so
    # the file ends after field A but before the nullable bitmap.
    with open(path, 'r+b') as out:
        out.seek(-2, os.SEEK_END)
        out.truncate()

    table = DBF(path)
    iterator = iter(table)
    next(iterator)
    with pytest.raises(InvalidRecord) as excinfo:
        next(iterator)
    error = excinfo.value
    assert error.record_number == 2
    assert error.field_name == '_NullFlags'


def test_out_of_range_varchar_length_reports_number_and_field(tmp_path):
    path = str(tmp_path / 'bad_length.dbf')
    specs = [('NAME', 'V', 6)]
    bad_varchar = b'abcdef' + bytes([7])  # length exceeds width 6
    build_dbf(path, 0x32, specs,
              [(RECORD_NORMAL, bad_varchar, b'\x00')])
    table = DBF(path)
    with pytest.raises(InvalidRecord) as excinfo:
        list(table)
    error = excinfo.value
    assert error.record_number == 1
    assert error.field_name == 'NAME'
    assert 'length byte 7' in str(error)


def test_parser_unit_tests_for_v_and_q():
    parser = FieldParser(_MockTable())
    vfield = _MockField('V', name='NAME', length=6)
    qfield = _MockField('Q', name='DATA', length=5)

    assert parser.parse(vfield, varchar_bytes('hi', 6)) == 'hi'
    assert parser.parse(vfield, varchar_bytes('', 6)) == ''
    assert parser.parse(
        vfield, varchar_bytes('abcdef', 6)) == 'abcdef'
    assert parser.parse(qfield, varbinary_bytes(b'\x00\xff', 5)) == (
        b'\x00\xff')
    assert parser.parse(qfield, varbinary_bytes(b'', 5)) == b''

    with pytest.raises(InvalidRecord):
        parser.parse(vfield, b'abcdef' + bytes([7]))


class _MockHeader(object):
    dbversion = 0x30


class _MockTable(object):
    header = _MockHeader()
    encoding = 'cp1252'
    char_decode_errors = 'strict'


class _MockField(object):
    def __init__(self, field_type, name, length):
        self.type = field_type
        self.name = name
        self.length = length


def test_struct_sizes_match_dbfread_layout():
    # The helper must reproduce the exact on-disk struct sizes.
    assert HEADER_SIZE == 32
    assert FIELD_SIZE == 32
    assert DBFField.size == 32
