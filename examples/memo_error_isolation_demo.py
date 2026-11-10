"""Demonstrate explicit memo error isolation with temporary files.

Scenario:
  1. A DBF table references a memo block whose pointer is out of range.
     With ``on_memo_error`` the bad cell is replaced and the next row is
     still read; a structured diagnostic is emitted.
  2. The main DBF file is then truncated in the middle of a record.
     Truncation is a structural error and is raised immediately even
     though the memo isolation callback is active, so no leftover bytes
     can be glued together into a fake "recovered" record.

Run with: python examples/memo_error_isolation_demo.py
"""
import os
import struct
import tempfile

from dbfread import DBF
from dbfread.exceptions import (InvalidMemoPointer, DBFRecordTruncated)


def write_dbf(path, records, fields):
    num_fields = len(fields)
    headerlen = 32 + 32 * num_fields + 1
    recordlen = 1 + sum(length for _, _, length in fields)
    header = struct.pack(
        '<BBBBLHHHBBLLLBBH',
        0x30, 126, 11, 10, len(records), headerlen, recordlen,
        0, 0, 0, 0, 0, 0, 0, 0, 0)
    descriptors = b''
    for name, field_type, length in fields:
        descriptors += struct.pack(
            '<11scLBBHBBBB7sB',
            name.encode('ascii').ljust(11, b'\0'),
            field_type.encode('ascii'), 0, length,
            0, 0, 0, 0, 0, 0, b'\0' * 7, 0)
    with open(path, 'wb') as handle:
        handle.write(header)
        handle.write(descriptors)
        handle.write(b'\r')
        for record in records:
            handle.write(b' ' + record)
        handle.write(b'\x1a')


def write_fpt(path):
    blocksize = 512
    with open(path, 'wb') as handle:
        handle.write(struct.pack('>LHH504s', 3, 0, blocksize, b'\0' * 504))
        # Block 1 is a healthy text memo. Block 2 never exists.
        handle.seek(blocksize)
        handle.write(struct.pack('>LL', 0x1, 3) + b'ok!')
        handle.truncate(2 * blocksize)


def write_dbf3(path, records, fields):
    # Version 0x83 selects the dBase III DBT memo reader.
    num_fields = len(fields)
    headerlen = 32 + 32 * num_fields + 1
    recordlen = 1 + sum(length for _, _, length in fields)
    header = struct.pack(
        '<BBBBLHHHBBLLLBBH',
        0x83, 126, 11, 10, len(records), headerlen, recordlen,
        0, 0, 0, 0, 0, 0, 0, 0, 0)
    descriptors = b''
    for name, field_type, length in fields:
        descriptors += struct.pack(
            '<11scLBBHBBBB7sB',
            name.encode('ascii').ljust(11, b'\0'),
            field_type.encode('ascii'), 0, length,
            0, 0, 0, 0, 0, 0, b'\0' * 7, 0)
    with open(path, 'wb') as handle:
        handle.write(header)
        handle.write(descriptors)
        handle.write(b'\r')
        for record in records:
            handle.write(b' ' + record)
        handle.write(b'\x1a')


def write_dbt(path):
    blocksize = 512
    with open(path, 'wb') as handle:
        handle.write(b'\0' * blocksize)  # block 0 header
        handle.seek(blocksize)
        payload = b'dbt memo\x1a'
        handle.write(payload + b'\0' * (blocksize - len(payload)))


def main():
    directory = tempfile.mkdtemp(prefix='dbfread-memo-demo-')
    base = os.path.join(directory, 'demo')
    dbf_path = base + '.dbf'
    fpt_path = base + '.fpt'

    fields = [('NAME', 'C', 10), ('MEMO', 'M', 4)]

    def row(name, pointer):
        return name.ljust(10).encode('ascii') + struct.pack('<I', pointer)

    write_fpt(fpt_path)
    write_dbf(dbf_path,
              [row('Alice', 99),   # out-of-range memo pointer
               row('Bob', 1)],     # healthy memo in the next row
              fields)

    diagnostics = []

    def handler(context):
        diagnostics.append(context)
        return '<memo unavailable>'

    print('--- recoverable memo failure, reading continues ---')
    table = DBF(dbf_path, on_memo_error=handler)
    for record in table:
        print(record)

    context = diagnostics[0]
    print('diagnostic:', context)
    assert isinstance(context.error, InvalidMemoPointer)
    assert context.physical_record_number == 1
    assert context.field == 'MEMO'
    assert context.pointer == 99
    assert context.position == 99 * 512

    print()
    print('--- main DBF truncated mid-record: isolation does not apply ---')
    size_before = os.path.getsize(dbf_path)
    with open(dbf_path, 'r+b') as handle:
        handle.truncate(size_before - 6)

    try:
        list(DBF(dbf_path, on_memo_error=handler))
    except DBFRecordTruncated as error:
        print('raised:', type(error).__name__, '-', error)
    else:
        raise AssertionError('truncated main record must raise')

    print()
    print('--- same isolation with a dBase III DBT memo file ---')
    dbt_base = os.path.join(directory, 'demo3')
    write_dbf3(dbt_base + '.dbf',
               [row('Cyd', 1),     # healthy DBT block
                row('Dan', 40)],   # pointer past end of the DBT
               fields)
    write_dbt(dbt_base + '.dbt')

    dbt_diagnostics = []
    dbt_rows = list(DBF(
        dbt_base + '.dbf',
        on_memo_error=lambda c: dbt_diagnostics.append(c) or None))
    for record in dbt_rows:
        print(record)
    assert dbt_rows[0]['MEMO'] == 'dbt memo'
    assert dbt_rows[1]['MEMO'] is None
    assert dbt_diagnostics[0].pointer == 40
    assert isinstance(dbt_diagnostics[0].error, InvalidMemoPointer)
    print('diagnostic:', dbt_diagnostics[0])


if __name__ == '__main__':
    main()
