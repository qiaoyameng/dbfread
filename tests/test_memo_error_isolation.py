"""Tests for explicit memo error isolation (``on_memo_error``)."""
import gc
import os

import pytest

from dbfread import DBF
from dbfread import MemoErrorContext
from dbfread.field_parser import FieldParser
from dbfread.exceptions import (MemoError,
                                InvalidMemoPointer,
                                TruncatedMemo,
                                UnsupportedMemoType,
                                DBFRecordTruncated)
from tests.support import (build_dbf,
                           build_fpt,
                           build_dbt,
                           build_db4_dbt,
                           memo_pointer,
                           truncate_file,
                           open_count)


VFP_FIELDS = [('NAME', 'C', 10), ('MEMO', 'M', 4)]


def vfp_record(name, pointer):
    return name.ljust(10, ' ').encode('ascii') + memo_pointer(pointer)


def make_vfp_table(tmp_path, fpt_blocks, records, dbversion=0x30,
                   deleted=(), fpt_size_blocks=None):
    base = str(tmp_path / 'table')
    build_dbf(base + '.dbf', dbversion, VFP_FIELDS, records, deleted)
    build_fpt(base + '.fpt', fpt_blocks, size_blocks=fpt_size_blocks)
    return base + '.dbf'


def make_dbf3_table(tmp_path, dbt_blocks, records):
    base = str(tmp_path / 'table3')
    build_dbf(base + '.dbf', 0x83, VFP_FIELDS, records)
    build_dbt(base + '.dbt', dbt_blocks)
    return base + '.dbf'


def make_dbf4_table(tmp_path, dbt_blocks, records):
    base = str(tmp_path / 'table4')
    # 0x8b is a dBase IV version byte, so open_memofile() picks DB4MemoFile.
    build_dbf(base + '.dbf', 0x8b, VFP_FIELDS, records)
    build_db4_dbt(base + '.dbt', dbt_blocks)
    return base + '.dbf'


def collect(table):
    diagnostics = []

    def handler(context):
        diagnostics.append(context)
        return '<recovered>'

    rows = list(DBF(table, on_memo_error=handler))
    return rows, diagnostics


def test_default_mode_raises_for_out_of_range_pointer(tmp_path):
    table = make_vfp_table(
        tmp_path,
        fpt_blocks={1: (0x1, b'good')},
        records=[vfp_record('Alice', 1),
                 vfp_record('Bob', 50)])  # block 50 does not exist

    with pytest.raises(InvalidMemoPointer) as excinfo:
        list(DBF(table))

    error = excinfo.value
    assert isinstance(error, MemoError)
    assert error.pointer == 50
    assert error.offset == 50 * 512


def test_three_memo_error_kinds_are_isolated(tmp_path):
    table = make_vfp_table(
        tmp_path,
        fpt_blocks={
            1: (0x1, b'good'),
            # Block 2 uses an unknown block type.
            2: (0x9, b'binary-ish'),
            # Block 3 is the last block and declares more data than
            # the file tail holds.
            3: (0x1, b'x' * 600),
        },
        records=[vfp_record('Alice', 1),
                 vfp_record('Bob', 50),     # out of range pointer
                 vfp_record('Cyd', 2),      # unsupported block type
                 vfp_record('Dan', 3)],     # truncated block
        fpt_size_blocks=4)

    rows, diagnostics = collect(table)

    assert [row['NAME'] for row in rows] == ['Alice', 'Bob', 'Cyd', 'Dan']
    assert rows[0]['MEMO'] == 'good'
    assert [row['MEMO'] for row in rows[1:]] == ['<recovered>'] * 3

    assert [context.physical_record_number for context in diagnostics] == \
        [2, 3, 4]
    assert [context.field for context in diagnostics] == ['MEMO'] * 3
    assert [context.pointer for context in diagnostics] == [50, 2, 3]
    assert [context.position for context in diagnostics] == \
        [50 * 512, 2 * 512, 3 * 512]
    assert [type(context.error) for context in diagnostics] == [
        InvalidMemoPointer, UnsupportedMemoType, TruncatedMemo]


def test_truncated_fpt_block_is_isolated_with_next_block_intact(tmp_path):
    # A truncated tail block is isolated per cell and does not stop
    # the following DBF record from being read.
    table = make_vfp_table(
        tmp_path,
        fpt_blocks={
            1: (0x1, b'good'),
            2: (0x1, b'x' * 600),
        },
        records=[vfp_record('Alice', 2),
                 vfp_record('Bob', 1)],
        fpt_size_blocks=3)

    rows, diagnostics = collect(table)
    assert [row['MEMO'] for row in rows] == ['<recovered>', 'good']
    assert len(diagnostics) == 1
    assert isinstance(diagnostics[0].error, TruncatedMemo)
    for context in diagnostics:
        assert isinstance(context, MemoErrorContext)
        assert isinstance(context.error, MemoError)


def test_null_and_empty_memos_emit_no_diagnostics(tmp_path):
    records = [vfp_record('Alice', 0),
               vfp_record('Bob', 0)]
    table = make_vfp_table(
        tmp_path,
        fpt_blocks={1: (0x1, b'never used')},
        records=records)

    rows, diagnostics = collect(table)
    assert rows[0]['MEMO'] is None
    assert rows[1]['MEMO'] is None
    assert diagnostics == []


def test_same_bad_block_referenced_by_multiple_cells(tmp_path):
    table = make_vfp_table(
        tmp_path,
        fpt_blocks={1: (0x1, b'good')},
        records=[vfp_record('Alice', 7),
                 vfp_record('Bob', 1),
                 vfp_record('Cyd', 7)])

    rows, diagnostics = collect(table)

    assert [row['MEMO'] for row in rows] == \
        ['<recovered>', 'good', '<recovered>']
    assert len(diagnostics) == 2
    assert [context.physical_record_number for context in diagnostics] == \
        [1, 3]
    assert all(context.pointer == 7 for context in diagnostics)
    assert all(context.position == 7 * 512 for context in diagnostics)


def test_each_memo_field_in_a_row_is_located_separately(tmp_path):
    fields = [('NAME', 'C', 6), ('M1', 'M', 4), ('M2', 'M', 4)]

    def row(name, pointer1, pointer2):
        return (name.ljust(6).encode('ascii')
                + memo_pointer(pointer1)
                + memo_pointer(pointer2))

    base = str(tmp_path / 'multi')
    build_dbf(base + '.dbf', 0x30, fields,
              [row('Alice', 9, 8), row('Bob', 1, 1)])
    build_fpt(base + '.fpt', {1: (0x1, b'good')})

    diagnostics = []
    rows = list(DBF(base + '.dbf',
                    on_memo_error=lambda c: diagnostics.append(c) or None))

    assert [(c.physical_record_number, c.field, c.pointer)
            for c in diagnostics] == [(1, 'M1', 9), (1, 'M2', 8)]
    assert rows[0]['M1'] is None
    assert rows[0]['M2'] is None
    assert rows[1]['M1'] == 'good'
    assert rows[1]['M2'] == 'good'


def test_physical_record_numbers_include_deleted_records(tmp_path):
    table = make_vfp_table(
        tmp_path,
        fpt_blocks={1: (0x1, b'good')},
        records=[vfp_record('Alice', 7)],
        deleted=[vfp_record('Ghost', 1)])

    rows, diagnostics = collect(table)

    assert rows[0]['NAME'] == 'Alice'
    assert diagnostics[0].physical_record_number == 1

    deleted, deleted_diagnostics = [], []

    def handler(context):
        deleted_diagnostics.append(context)
        return None

    deleted_rows = list(DBF(table, on_memo_error=handler).deleted)
    assert deleted_rows[0]['NAME'] == 'Ghost'
    assert deleted_diagnostics == []


def test_load_and_iteration_give_identical_data_and_diagnostics(tmp_path):
    table = make_vfp_table(
        tmp_path,
        fpt_blocks={
            1: (0x1, b'good'),
            2: (0x9, b'weird'),
            3: (0x1, b'x' * 600),
        },
        records=[vfp_record('Alice', 1),
                 vfp_record('Bob', 8),
                 vfp_record('Cyd', 2),
                 vfp_record('Dan', 3)],
        fpt_size_blocks=4)

    def run(load):
        seen = []

        def handler(context):
            seen.append(context)
            return 'REPLACED'

        dbf = DBF(table, load=load, on_memo_error=handler)
        return list(dbf), list(seen)

    streamed_rows, streamed_diag = run(False)
    loaded_rows, loaded_diag = run(True)

    assert streamed_rows == loaded_rows
    assert len(streamed_diag) == len(loaded_diag)
    for streamed, loaded in zip(streamed_diag, loaded_diag):
        assert streamed._replace(error=None) == loaded._replace(error=None)
        assert type(streamed.error) is type(loaded.error)
    assert [d.pointer for d in streamed_diag] == [8, 2, 3]


def test_truncated_main_record_cannot_be_skipped(tmp_path):
    table = make_vfp_table(
        tmp_path,
        fpt_blocks={1: (0x1, b'good')},
        records=[vfp_record('Alice', 1),
                 vfp_record('Bob', 1)])

    full_size = os.path.getsize(table)
    # Keep the header, both records and the 0x1a byte; then cut off the
    # last few bytes of Bob's record body.
    truncate_file(table, full_size - 5)

    def handler(context):
        raise AssertionError('memo handler must not run')

    with pytest.raises(DBFRecordTruncated):
        list(DBF(table, on_memo_error=handler))


def test_truncated_main_record_does_not_manufacture_records(tmp_path):
    # First demonstrate a recoverable memo failure, then truncate the
    # main file and make sure no garbage is parsed as another row.
    table = make_vfp_table(
        tmp_path,
        fpt_blocks={1: (0x1, b'good')},
        records=[vfp_record('Alice', 99),
                 vfp_record('Bob', 1),
                 vfp_record('Cyd', 1)])

    rows, diagnostics = collect(table)
    assert len(rows) == 3
    assert rows[0]['MEMO'] == '<recovered>'
    assert rows[1]['MEMO'] == 'good'
    assert len(diagnostics) == 1

    full_size = os.path.getsize(table)
    truncate_file(table, full_size - 8)

    with pytest.raises(DBFRecordTruncated) as excinfo:
        list(DBF(table, on_memo_error=lambda context: '<x>'))

    # The truncated third record is reported by its physical position,
    # not silently glued together with leftover bytes.
    assert excinfo.value.args[0].startswith('physical record 3 ')


def test_custom_parser_business_exception_is_not_isolated(tmp_path):
    table = make_vfp_table(
        tmp_path,
        fpt_blocks={1: (0x1, b'good')},
        records=[vfp_record('Alice', 1)])

    class BusinessError(Exception):
        pass

    class ExplodingParser(FieldParser):
        def __init__(self, table, memofile=None):
            super(ExplodingParser, self).__init__(table, memofile)

        def parse(self, field, data):
            raise BusinessError('custom parser failure')

    with pytest.raises(BusinessError):
        list(DBF(table, parserclass=ExplodingParser,
                 on_memo_error=lambda context: None))


def test_handler_exception_keeps_chain_and_closes_files(tmp_path):
    table = make_vfp_table(
        tmp_path,
        fpt_blocks={1: (0x1, b'good')},
        records=[vfp_record('Alice', 42)])

    def handler(context):
        raise RuntimeError('handler broke')

    gc.collect()
    before = open_count()
    with pytest.raises(RuntimeError) as excinfo:
        list(DBF(table, on_memo_error=handler))
    gc.collect()
    after = open_count()

    assert isinstance(excinfo.value.__context__, InvalidMemoPointer)
    assert before == after


def test_handler_receives_original_exception_instance(tmp_path):
    table = make_vfp_table(
        tmp_path,
        fpt_blocks={2: (0x9, b'weird')},
        records=[vfp_record('Alice', 2)])

    seen = []
    list(DBF(table, on_memo_error=lambda c: seen.append(c) or None))
    context = seen[0]
    assert isinstance(context.error, UnsupportedMemoType)
    assert context.pointer == 2
    assert context.position == 2 * 512


def test_dbt_out_of_range_pointer_is_isolated(tmp_path):
    table = make_dbf3_table(
        tmp_path,
        dbt_blocks={1: b'first memo'},
        records=[vfp_record('Alice', 1),
                 vfp_record('Bob', 30)])

    rows, diagnostics = collect(table)

    assert rows[0]['MEMO'] == 'first memo'
    assert rows[1]['MEMO'] == '<recovered>'
    assert diagnostics[0].physical_record_number == 2
    assert diagnostics[0].pointer == 30
    assert diagnostics[0].position == 30 * 512
    assert isinstance(diagnostics[0].error, InvalidMemoPointer)


def test_db4_dbt_truncated_block_is_isolated(tmp_path):
    table = make_dbf4_table(
        tmp_path,
        dbt_blocks={1: b'first memo'},
        records=[vfp_record('Alice', 1)])
    # Remove the tail of the memo file while leaving the pointer valid.
    truncate_file(table.replace('.dbf', '.dbt'), 512 + 4)

    rows, diagnostics = collect(table)
    assert rows[0]['MEMO'] == '<recovered>'
    assert isinstance(diagnostics[0].error, TruncatedMemo)
    assert diagnostics[0].position == 512


def test_db4_dbt_out_of_range_pointer_is_isolated(tmp_path):
    table = make_dbf4_table(
        tmp_path,
        dbt_blocks={1: b'first memo'},
        records=[vfp_record('Alice', 1),
                 vfp_record('Bob', 12)])

    rows, diagnostics = collect(table)
    assert rows[0]['MEMO'] == 'first memo'
    assert rows[1]['MEMO'] == '<recovered>'
    assert isinstance(diagnostics[0].error, InvalidMemoPointer)
    assert diagnostics[0].pointer == 12
    assert diagnostics[0].position == 12 * 512


def test_memo_errors_accumulate_on_table(tmp_path):
    table = make_vfp_table(
        tmp_path,
        fpt_blocks={1: (0x1, b'good')},
        records=[vfp_record('Alice', 5),
                 vfp_record('Bob', 6)])

    dbf = DBF(table, on_memo_error=lambda context: None)
    list(dbf)
    list(dbf)

    assert len(dbf.memo_errors) == 4
    assert [c.pointer for c in dbf.memo_errors] == [5, 6, 5, 6]
