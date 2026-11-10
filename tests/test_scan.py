"""
Tests for incremental scanning of append-only DBF files.
"""
import struct

import pytest

from dbfread import DBF, ScanCursor
from dbfread.exceptions import CursorMismatch

FIELDS = [('NAME', 'C', 10), ('AGE', 'N', 3)]


def field_descriptor(name, type_, length):
    return struct.pack(
        '<11scLBBHBBBB7sB',
        name.encode('ascii').ljust(11, b'\0'),
        type_.encode('ascii'),
        0,       # address
        length,
        0,       # decimal_count
        0, 0, 0, 0, 0,
        b'\0' * 7,
        0)


def make_record(name, age, deleted=False):
    sep = b'*' if deleted else b' '
    return sep + name.encode('ascii').ljust(10) + str(age).rjust(3).encode()


def build_dbf(rows, fields=FIELDS, numrecords=None, tail=b'\x1a'):
    if numrecords is None:
        numrecords = len(rows)
    headerlen = 32 + 32 * len(fields) + 1
    recordlen = 1 + sum(length for _, _, length in fields)
    data = [struct.pack(
        '<BBBBLHHHBBLLLBBH',
        0x03,   # dbversion
        26, 11, 10,
        numrecords,
        headerlen,
        recordlen,
        0, 0, 0, 0, 0, 0, 0, 0, 0)]
    data += [field_descriptor(*field) for field in fields]
    data.append(b'\r')
    data += rows
    data.append(tail)
    return b''.join(data)


def write_table(path, rows, **kwargs):
    with open(str(path), 'wb') as outfile:
        outfile.write(build_dbf(rows, **kwargs))


def rows_of(*people):
    return [make_record(name, age, deleted=deleted)
            for name, age, deleted in people]


def test_multiple_rounds_match_full_read(tmp_path):
    path = tmp_path / 'scan.dbf'
    generation = 'gen-1'

    # Round 1: three records.
    write_table(path, rows_of(('Alice', 30, False),
                              ('Bob', 40, False),
                              ('Carol', 50, False)))
    table = DBF(str(path))
    scan = table.scan(generation)
    collected = list(scan)
    assert not scan.incomplete
    assert scan.cursor.position == 3

    # The cursor survives a serialization round trip.
    saved = scan.cursor.dumps()
    cursor = ScanCursor.loads(saved)
    assert cursor == scan.cursor

    # Round 2: two records appended. Reuse the same table object; the
    # scan must see the new header-declared record count.
    write_table(path, rows_of(('Alice', 30, False),
                              ('Bob', 40, False),
                              ('Carol', 50, False),
                              ('Dave', 60, False),
                              ('Erin', 70, False)))
    scan = table.scan(generation, cursor=cursor)
    collected += list(scan)
    assert scan.cursor.position == 5

    # Round 3: one more record, this time through a fresh table object.
    write_table(path, rows_of(('Alice', 30, False),
                              ('Bob', 40, False),
                              ('Carol', 50, False),
                              ('Dave', 60, False),
                              ('Erin', 70, False),
                              ('Frank', 80, False)))
    scan = DBF(str(path)).scan(generation, cursor=scan.cursor)
    collected += list(scan)

    # Physical positions and contents match a single full read.
    assert [item.position for item in collected] == [0, 1, 2, 3, 4, 5]
    assert ([item.record for item in collected] ==
            list(DBF(str(path))))
    assert all(not item.deleted for item in collected)


def test_deleted_records_advance_and_are_separable(tmp_path):
    path = tmp_path / 'deleted.dbf'
    write_table(path, rows_of(('Alice', 30, False),
                              ('Ghost', 99, True),
                              ('Bob', 40, False)))

    table = DBF(str(path))
    scan = table.scan('gen-1')
    collected = list(scan)

    # Deleted records advance the physical position like any other.
    assert [item.position for item in collected] == [0, 1, 2]
    assert [item.deleted for item in collected] == [False, True, False]

    # Deleted and live records can be separated and match a full read.
    assert ([item.record for item in collected if not item.deleted] ==
            list(table))
    assert ([item.record for item in collected if item.deleted] ==
            list(table.deleted))


def test_generation_mismatch_refused(tmp_path):
    path = tmp_path / 'gen.dbf'
    write_table(path, rows_of(('Alice', 30, False)))
    table = DBF(str(path))
    cursor = table.scan('gen-1').cursor

    # Same file, but the caller knows the table was replaced.
    with pytest.raises(CursorMismatch):
        table.scan('gen-2', cursor=cursor)


def test_field_layout_change_refused(tmp_path):
    path = tmp_path / 'layout.dbf'
    write_table(path, rows_of(('Alice', 30, False)))
    table = DBF(str(path))
    scan = table.scan('gen-1')
    list(scan)

    # Field layout changes (NAME widened), record count only grows.
    new_fields = [('NAME', 'C', 20), ('AGE', 'N', 3)]
    rows = [b' ' + b'Alice'.ljust(20) + b' 30',
            b' ' + b'Bob'.ljust(20) + b' 40']
    write_table(path, rows, fields=new_fields)

    with pytest.raises(CursorMismatch):
        DBF(str(path)).scan('gen-1', cursor=scan.cursor)


def test_record_count_rollback_refused(tmp_path):
    path = tmp_path / 'rollback.dbf'
    write_table(path, rows_of(('Alice', 30, False),
                              ('Bob', 40, False),
                              ('Carol', 50, False)))
    table = DBF(str(path))
    scan = table.scan('gen-1')
    list(scan)
    assert scan.cursor.position == 3

    # File replaced by a shorter table with the same layout and
    # generation id (simulating a truncated or rolled-back file).
    write_table(path, rows_of(('Alice', 30, False)))
    with pytest.raises(CursorMismatch):
        DBF(str(path)).scan('gen-1', cursor=scan.cursor)


def test_incomplete_tail_not_advanced_past(tmp_path):
    path = tmp_path / 'partial.dbf'
    full = rows_of(('Alice', 30, False),
                   ('Bob', 40, False),
                   ('Carol', 50, False))
    # Header declares 4 records but the 4th is only partially written.
    write_table(path, full + [b' Dave'],
                numrecords=4, tail=b'')

    table = DBF(str(path))
    scan = table.scan('gen-1')
    collected = list(scan)

    assert scan.incomplete
    assert [item.position for item in collected] == [0, 1, 2]
    # The cursor rests on the partial record, not beyond it.
    assert scan.cursor.position == 3

    # The producer finishes the append; the next round picks it up.
    write_table(path, full + [make_record('Dave', 60)], numrecords=4)
    scan = DBF(str(path)).scan('gen-1', cursor=scan.cursor)
    collected = list(scan)
    assert not scan.incomplete
    assert [item.position for item in collected] == [3]
    assert collected[0].record['NAME'] == 'Dave'


def test_record_count_frozen_when_scan_opened(tmp_path):
    path = tmp_path / 'frozen.dbf'
    write_table(path, rows_of(('Alice', 30, False),
                              ('Bob', 40, False)))
    table = DBF(str(path))
    scan = table.scan('gen-1')

    # Records appended after the scan was opened are left for the
    # next round.
    write_table(path, rows_of(('Alice', 30, False),
                              ('Bob', 40, False),
                              ('Carol', 50, False)))
    collected = list(scan)
    assert [item.position for item in collected] == [0, 1]
    assert scan.cursor.position == 2

    scan = table.scan('gen-1', cursor=scan.cursor)
    collected = list(scan)
    assert [item.position for item in collected] == [2]
    assert collected[0].record['NAME'] == 'Carol'


def test_error_leaves_cursor_on_record_boundary(tmp_path):
    path = tmp_path / 'error.dbf'
    rows = rows_of(('Alice', 30, False))
    # Undecodable byte in the second record makes the parser fail.
    rows.append(b' ' + b'Bad\xffName'.ljust(10) + b' 40')
    rows += rows_of(('Carol', 50, False))
    write_table(path, rows)

    table = DBF(str(path), encoding='ascii')
    scan = table.scan('gen-1')
    with pytest.raises(UnicodeDecodeError):
        list(scan)

    # The last successful cursor is exactly on the boundary before
    # the record that failed.
    assert scan.cursor.position == 1


def test_cursor_serialization_round_trip():
    cursor = ScanCursor(generation='gen-1', field_digest='abc', position=7)
    assert ScanCursor.loads(cursor.dumps()) == cursor
    assert ScanCursor.from_dict(cursor.to_dict()) == cursor
    assert cursor != ScanCursor(generation='gen-2', field_digest='abc',
                                position=7)
