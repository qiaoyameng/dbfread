"""
Tests for predicate/projection push-down into record reading.

All fixtures are generated locally: a small Visual FoxPro table with
a deleted record, two records sharing one memo pointer and one record
whose memo pointer is bad. A counting field parser and a tracking
memo file show which parsing work and which memo byte reads are
saved by pushing filtering and projection down into the scan.
"""
import struct
import collections

import pytest

from dbfread import DBF, FieldParser
from dbfread.exceptions import MissingMemoFile
from dbfread.memo import VFPMemoFile
import dbfread.dbf
from dbfread.dbf import MEMO_FIELD_TYPES

BLOCKSIZE = 512

# (name, type, length). AGE is a 4 byte integer so that truncating
# the file inside a record makes the parser raise.
FIELD_SPECS = [('NAME', 'C', 10),
               ('AGE', 'I', 4),
               ('MEMO', 'M', 4)]

# (deleted?, name, age, memo_index). Alice and Bob share memo block 1.
# Dave's memo index 99 points beyond the end of the FPT file, so any
# attempt to read his memo raises an error.
ROWS = [(False, 'Alice', 30, 1),
        (False, 'Bob', 25, 1),
        (True, 'Carol', 40, 2),
        (False, 'Dave', 50, 99)]

MEMOS = {1: b'Alice memo', 2: b'Carol memo'}

# Same table but Dave points at a valid memo block, for tests that
# exercise the default (unfiltered) read path.
ROWS_OK = [(False, 'Alice', 30, 1),
           (False, 'Bob', 25, 1),
           (True, 'Carol', 40, 2),
           (False, 'Dave', 50, 2)]


def pack_field(name, type_, length):
    name_bytes = name.encode('ascii')
    name_bytes += b'\0' * (11 - len(name_bytes))
    return struct.pack('<11scLBBHBBBB7sB',
                       name_bytes, type_.encode('ascii'),
                       0, length, 0, 0, 0, 0, 0, 0, b'\0' * 7, 0)


def encode_value(type_, length, value):
    if type_ == 'C':
        return value.encode('ascii').ljust(length, b' ')
    elif type_ == 'I':
        return struct.pack('<i', value)
    elif type_ == 'M':
        return struct.pack('<I', value)
    else:
        raise AssertionError(type_)


def write_table(dbf_path, rows=ROWS, memos=MEMOS, write_memofile=True,
                declared_records=None):
    fields = [pack_field(*spec) for spec in FIELD_SPECS]
    headerlen = 32 + 32 * len(fields) + 1
    recordlen = 1 + sum(spec[2] for spec in FIELD_SPECS)
    numrecords = (len(rows) if declared_records is None
                  else declared_records)

    header = struct.pack('<BBBBLHHHBBLLLBBH',
                         0x30,        # dbversion: Visual FoxPro
                         126, 11, 9,  # date
                         numrecords,
                         headerlen,
                         recordlen,
                         0, 0, 0, 0, 0, 0, 0, 0, 0)

    data = bytearray(header)
    data += b''.join(fields)
    data += b'\r'  # end of field headers

    for deleted, name, age, memo_index in rows:
        data += b'*' if deleted else b' '
        for (field_name, type_, length), value in zip(
                FIELD_SPECS, (name, age, memo_index)):
            data += encode_value(type_, length, value)
    data += b'\x1a'  # end of file marker
    dbf_path.write_bytes(bytes(data))

    if write_memofile:
        nextblock = max(memos) + 1 if memos else 1
        fpt = bytearray(struct.pack('>LHH504s', nextblock, 0, BLOCKSIZE,
                                    b'\0' * 504))
        for index, content in sorted(memos.items()):
            offset = index * BLOCKSIZE
            if len(fpt) < offset:
                fpt += b'\0' * (offset - len(fpt))
            fpt += struct.pack('>LL', 1, len(content)) + content
        fpt_path = dbf_path.with_suffix('.fpt')
        fpt_path.write_bytes(bytes(fpt))


@pytest.fixture
def dbf_file(tmp_path):
    path = tmp_path / 'pushdown.dbf'
    write_table(path)
    return str(path)


@pytest.fixture
def ok_dbf_file(tmp_path):
    path = tmp_path / 'pushdown_ok.dbf'
    write_table(path, rows=ROWS_OK)
    return str(path)


class Stats(object):
    def __init__(self):
        self.parse_counts = collections.Counter()
        self.memo_reads = 0
        self.memo_bytes = 0


def make_counting_parser(stats):
    """FieldParser subclass counting parse() calls and memo reads."""
    class CountingParser(FieldParser):
        def __init__(self, table, memofile=None):
            FieldParser.__init__(self, table, memofile)
            real_get_memo = self.get_memo

            def get_memo(index):
                stats.memo_reads += 1
                memo = real_get_memo(index)
                if memo:
                    stats.memo_bytes += len(memo)
                return memo

            self.get_memo = get_memo

        def parse(self, field, data):
            stats.parse_counts[field.name] += 1
            return FieldParser.parse(self, field, data)

    return CountingParser


def open_table(filename, stats, **kwargs):
    return DBF(filename, parserclass=make_counting_parser(stats), **kwargs)


def test_predicate_and_projection_save_work(dbf_file):
    stats = Stats()
    table = open_table(dbf_file, stats,
                       predicate_fields=['AGE'],
                       predicate=lambda rec: rec['AGE'] < 40,
                       fields=['NAME'])

    assert list(table) == [{'NAME': 'Alice'}, {'NAME': 'Bob'}]

    # 3 candidate (non-deleted) records: AGE parsed once for each.
    assert stats.parse_counts['AGE'] == 3
    # NAME parsed only for the 2 records that passed.
    assert stats.parse_counts['NAME'] == 2
    # MEMO never parsed and no memo bytes read: Dave's bad memo
    # pointer (block 99) was never touched, and the shared block 1
    # was not needed for the projection either.
    assert stats.parse_counts['MEMO'] == 0
    assert stats.memo_reads == 0
    assert stats.memo_bytes == 0


def test_filtered_rows_never_read_memo(dbf_file):
    stats = Stats()
    table = open_table(dbf_file, stats,
                       predicate_fields=['AGE'],
                       predicate=lambda rec: rec['AGE'] < 40,
                       fields=['NAME', 'MEMO'])

    # Dave (AGE 50) is filtered out, so his bad memo pointer is
    # never dereferenced. Alice and Bob share memo block 1.
    assert list(table) == [{'NAME': 'Alice', 'MEMO': 'Alice memo'},
                           {'NAME': 'Bob', 'MEMO': 'Alice memo'}]
    assert stats.memo_reads == 2
    assert stats.memo_bytes == 2 * len(b'Alice memo')


def test_predicate_fields_not_reparsed_for_output(dbf_file):
    stats = Stats()
    table = open_table(dbf_file, stats,
                       predicate_fields=['AGE'],
                       predicate=lambda rec: rec['AGE'] >= 30,
                       fields=['AGE', 'NAME'])

    assert list(table) == [{'AGE': 30, 'NAME': 'Alice'},
                           {'AGE': 50, 'NAME': 'Dave'}]
    # AGE was parsed once per candidate record and reused for the
    # output, not parsed again.
    assert stats.parse_counts['AGE'] == 3
    assert stats.parse_counts['NAME'] == 2


def test_output_field_order_is_caller_defined(dbf_file):
    table = DBF(dbf_file, fields=['MEMO', 'NAME'],
                predicate_fields=['AGE'],
                predicate=lambda rec: rec['AGE'] < 40)
    record = next(iter(table))
    assert list(record.keys()) == ['MEMO', 'NAME']


def test_projection_only(dbf_file):
    stats = Stats()
    table = open_table(dbf_file, stats, fields=['NAME'])
    assert list(table) == [{'NAME': 'Alice'},
                           {'NAME': 'Bob'},
                           {'NAME': 'Dave'}]
    # Only NAME parsed; AGE and MEMO bytes were seeked past.
    assert stats.parse_counts['NAME'] == 3
    assert stats.parse_counts['AGE'] == 0
    assert stats.parse_counts['MEMO'] == 0
    assert stats.memo_reads == 0


def test_predicate_applies_to_deleted_records(dbf_file):
    # Carol is the deleted record (AGE 40).
    table = DBF(dbf_file,
                predicate_fields=['AGE'],
                predicate=lambda rec: rec['AGE'] >= 40,
                fields=['NAME'])
    assert list(table) == [{'NAME': 'Dave'}]
    assert list(table.deleted) == [{'NAME': 'Carol'}]

    table = DBF(dbf_file,
                predicate_fields=['AGE'],
                predicate=lambda rec: rec['AGE'] < 40,
                fields=['NAME'])
    assert list(table) == [{'NAME': 'Alice'}, {'NAME': 'Bob'}]
    assert list(table.deleted) == []


def test_load_and_unload_give_same_content(dbf_file):
    kwargs = dict(predicate_fields=['AGE'],
                  predicate=lambda rec: rec['AGE'] < 40,
                  fields=['NAME', 'MEMO'])

    streamed = DBF(dbf_file, **kwargs)
    loaded = DBF(dbf_file, load=True, **kwargs)

    assert list(streamed) == list(loaded)
    assert list(streamed.deleted) == list(loaded.deleted)

    loaded.unload()
    assert list(loaded) == list(streamed)


def test_raw_mode_predicate_sees_bytes(dbf_file):
    seen = []

    def predicate(rec):
        seen.append(rec)
        return rec['AGE'] == struct.pack('<i', 30)

    table = DBF(dbf_file, raw=True,
                predicate_fields=['AGE'],
                predicate=predicate,
                fields=['NAME'])
    assert list(table) == [{'NAME': b'Alice'.ljust(10, b' ')}]
    # The predicate saw raw field bytes, once per candidate record.
    assert seen == [{'AGE': struct.pack('<i', 30)},
                    {'AGE': struct.pack('<i', 25)},
                    {'AGE': struct.pack('<i', 50)}]


def test_unknown_and_duplicate_fields_rejected(dbf_file):
    with pytest.raises(ValueError):
        DBF(dbf_file, fields=['NAME', 'NOSUCH'])
    with pytest.raises(ValueError):
        DBF(dbf_file, fields=['NAME', 'NAME'])
    with pytest.raises(ValueError):
        DBF(dbf_file, predicate_fields=['NOSUCH'],
            predicate=lambda rec: True)
    with pytest.raises(ValueError):
        DBF(dbf_file, predicate_fields=['AGE', 'AGE'],
            predicate=lambda rec: True)
    # Memo fields are not allowed in the predicate.
    with pytest.raises(ValueError):
        DBF(dbf_file, predicate_fields=['MEMO'],
            predicate=lambda rec: True)
    # predicate_fields without predicate (and vice versa).
    with pytest.raises(ValueError):
        DBF(dbf_file, predicate_fields=['AGE'])
    with pytest.raises(ValueError):
        DBF(dbf_file, predicate=lambda rec: True)


def test_field_names_follow_lowernames(dbf_file):
    table = DBF(dbf_file, lowernames=True,
                predicate_fields=['age'],
                predicate=lambda rec: rec['age'] < 40,
                fields=['name'])
    assert list(table) == [{'name': 'Alice'}, {'name': 'Bob'}]

    # Uppercase names are unknown when lowernames is active.
    with pytest.raises(ValueError):
        DBF(dbf_file, lowernames=True, fields=['NAME'])


def test_missing_memofile_with_projection(tmp_path):
    path = tmp_path / 'nomemo.dbf'
    write_table(path, write_memofile=False)

    # Default behaviour is unchanged: missing memo file is an error.
    with pytest.raises(MissingMemoFile):
        DBF(str(path))

    # Projecting only non-memo fields works without the memo file.
    table = DBF(str(path), fields=['NAME'])
    assert list(table) == [{'NAME': 'Alice'},
                           {'NAME': 'Bob'},
                           {'NAME': 'Dave'}]

    # Selecting a memo field still follows the missing-file policy.
    with pytest.raises(MissingMemoFile):
        DBF(str(path), fields=['NAME', 'MEMO'])

    table = DBF(str(path), fields=['NAME', 'MEMO'],
                ignore_missing_memofile=True,
                predicate_fields=['AGE'],
                predicate=lambda rec: rec['AGE'] < 40)
    assert list(table) == [{'NAME': 'Alice', 'MEMO': None},
                           {'NAME': 'Bob', 'MEMO': None}]


def test_memo_file_not_opened_when_not_projected(ok_dbf_file, monkeypatch):
    opened = []
    real_open = dbfread.dbf.open_memofile

    def tracking_open(filename, dbversion):
        opened.append(filename)
        return real_open(filename, dbversion)

    monkeypatch.setattr(dbfread.dbf, 'open_memofile', tracking_open)

    list(DBF(ok_dbf_file, fields=['NAME']))
    assert opened == []

    # Default behaviour still opens the memo file.
    list(DBF(ok_dbf_file))
    assert len(opened) == 1


class TrackingVFPMemoFile(VFPMemoFile):
    def __init__(self, filename):
        self.closed = False
        VFPMemoFile.__init__(self, filename)

    def _close(self):
        self.closed = True
        VFPMemoFile._close(self)


@pytest.fixture
def track_memofile(monkeypatch):
    instances = []

    def tracking_open(filename, dbversion):
        memo = TrackingVFPMemoFile(filename)
        instances.append(memo)
        return memo

    monkeypatch.setattr(dbfread.dbf, 'open_memofile', tracking_open)
    return instances


def test_predicate_error_propagates_and_closes_files(dbf_file,
                                                     track_memofile):
    calls = []

    def bad_predicate(rec):
        calls.append(rec['AGE'])
        if len(calls) == 2:
            raise RuntimeError('predicate failed')
        return True

    table = DBF(dbf_file,
                predicate_fields=['AGE'],
                predicate=bad_predicate,
                fields=['NAME', 'MEMO'])

    with pytest.raises(RuntimeError):
        list(table)

    # The error was not swallowed as a filter result, and the memo
    # file opened for the scan was closed.
    assert len(track_memofile) == 1
    assert track_memofile[0].closed


def test_truncated_file_propagates_error_and_closes_files(
        tmp_path, track_memofile):
    path = tmp_path / 'truncated.dbf'
    write_table(path)

    # Truncate inside the last record so that Dave's AGE field
    # (a 4 byte integer) is cut short.
    data = path.read_bytes()
    path.write_bytes(data[:-6])

    table = DBF(str(path),
                predicate_fields=['AGE'],
                predicate=lambda rec: True,
                fields=['NAME', 'MEMO'])

    # AGE is a 4 byte integer: parsing the truncated row raises
    # struct.error instead of silently dropping the record.
    with pytest.raises(struct.error):
        list(table)

    assert len(track_memofile) == 1
    assert track_memofile[0].closed


def test_default_behaviour_unchanged(ok_dbf_file):
    stats = Stats()
    table = open_table(ok_dbf_file, stats)
    records = list(table)
    assert [rec['NAME'] for rec in records] == ['Alice', 'Bob', 'Dave']
    # Every field of every candidate record was parsed.
    assert stats.parse_counts['NAME'] == 3
    assert stats.parse_counts['AGE'] == 3
    assert stats.parse_counts['MEMO'] == 3
    assert stats.memo_reads == 3


def test_memo_field_types_constant():
    assert MEMO_FIELD_TYPES == frozenset('MGPB')
