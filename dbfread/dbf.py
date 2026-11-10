"""
Class to read DBF files.
"""
import os
import sys
import datetime
import platform
import collections

from .ifiles import ifind
from .struct_parser import StructParser
from .field_parser import FieldParser
from .memo import find_memofile, open_memofile, FakeMemoFile
from .codepages import guess_encoding
from .dbversions import get_dbversion_string
from .exceptions import DBFNotFound, MissingMemoFile, InvalidRecord

_py_version = (sys.version_info.major, sys.version_info.minor)
_py_impl = platform.python_implementation()
if _py_version >= (3, 7) or (_py_version == (3, 6) and _py_impl == 'CPython'):
    ORDERED_DICT = dict
else:
    ORDERED_DICT = collections.OrderedDict


DBFHeader = StructParser(
    'DBFHeader',
    '<BBBBLHHHBBLLLBBH',
    ['dbversion',
     'year',
     'month',
     'day',
     'numrecords',
     'headerlen',
     'recordlen',
     'reserved1',
     'incomplete_transaction',
     'encryption_flag',
     'free_record_thread',
     'reserved2',
     'reserved3',
     'mdx_flag',
     'language_driver',
     'reserved4',
     ])

DBFField = StructParser(
    'DBFField',
    '<11scLBBHBBBB7sB',
    ['name',
     'type',
     'address',
     'length',
     'decimal_count',
     'reserved1',
     'workarea_id',
     'reserved2',
     'reserved3',
     'set_fields_flag',
     'reserved4',
     'index_field_flag',
     ])


def expand_year(year):
    """Convert 2-digit year to 4-digit year."""

    if year < 80:
        return 2000 + year
    else:
        return 1900 + year


class RecordIterator(object):
    def __init__(self, table, record_type):
        self._record_type = record_type
        self._table = table

    def __iter__(self):
        return self._table._iter_records(self._record_type)

    def __len__(self):
        return self._table._count_records(self._record_type)


class DBF(object):
    """DBF table."""
    def __init__(self, filename, encoding=None, ignorecase=True,
                 lowernames=False,
                 parserclass=FieldParser,
                 recfactory=ORDERED_DICT,
                 load=False,
                 raw=False,
                 ignore_missing_memofile=False,
                 char_decode_errors='strict'):

        self.encoding = encoding
        self.ignorecase = ignorecase
        self.lowernames = lowernames
        self.parserclass = parserclass
        self.raw = raw
        self.ignore_missing_memofile = ignore_missing_memofile
        self.char_decode_errors = char_decode_errors

        if recfactory is None:
            self.recfactory = lambda items: items
        else:
            self.recfactory = recfactory

        # Name part before .dbf is the table name
        self.name = os.path.basename(filename)
        self.name = os.path.splitext(self.name)[0].lower()
        self._records = None
        self._deleted = None

        if ignorecase:
            self.filename = ifind(filename)
            if not self.filename:
                raise DBFNotFound('could not find file {!r}'.format(filename))
        else:
            self.filename = filename

        # Filled in by self._read_headers()
        self.memofilename = None
        self.header = None
        self.fields = []       # namedtuples
        self.field_names = []  # strings
        # Visual FoxPro nullable support. ``_null_flags_field`` is the
        # hidden system field that holds the NULL bitmap; the lists map
        # every visible field to its byte offset in the record and (for
        # nullable fields) to its bit in the bitmap.
        self._null_flags_field = None
        self._field_offsets = []
        self._null_bit_field = {}

        with open(self.filename, mode='rb') as infile:
            self._read_header(infile)
            self._read_field_headers(infile)
            self._setup_vfp_fields()
            self._check_headers()

            try:
                self.date = datetime.date(expand_year(self.header.year),
                                          self.header.month,
                                          self.header.day)
            except ValueError:
                # Invalid date or '\x00\x00\x00'.
                self.date = None

        self.memofilename = self._get_memofilename()

        if load:
            self.load()

    @property
    def dbversion(self):
        return get_dbversion_string(self.header.dbversion)

    def _get_memofilename(self):
        # Does the table have a memo field?
        field_types = [field.type for field in self.fields]
        if not set(field_types) & set('MGPB'):
            # No memo fields.
            return None

        path = find_memofile(self.filename)
        if path is None:
            if self.ignore_missing_memofile:
                return None
            else:
                raise MissingMemoFile('missing memo file for {}'.format(
                    self.filename))
        else:
            return path

    @property
    def loaded(self):
        """``True`` if records are loaded into memory."""
        return self._records is not None

    def load(self):
        """Load records into memory.

        This loads both records and deleted records. The ``records``
        and ``deleted`` attributes will now be lists of records.

        """
        if not self.loaded:
            self._records = list(self._iter_records(b' '))
            self._deleted = list(self._iter_records(b'*'))

    def unload(self):
        """Unload records from memory.

        The records and deleted attributes will now be instances of
        ``RecordIterator``, which streams records from disk.
        """
        self._records = None
        self._deleted = None

    @property
    def records(self):
        """Records (not included deleted ones). When loaded a list of records,
        when not loaded a new ``RecordIterator`` object.
        """
        if self.loaded:
            return self._records
        else:
            return RecordIterator(self, b' ')

    @property
    def deleted(self):
        """Deleted records. When loaded a list of records, when not loaded a
        new ``RecordIterator`` object.
        """
        if self.loaded:
            return self._deleted
        else:
            return RecordIterator(self, b'*')

    def _read_header(self, infile):
        # Todo: more checks?
        self.header = DBFHeader.read(infile)

        if self.encoding is None:
            try:
                self.encoding = guess_encoding(self.header.language_driver)
            except LookupError:
                self.encoding = 'ascii'

    def _decode_text(self, data):
        return data.decode(self.encoding, errors=self.char_decode_errors)

    def _read_field_headers(self, infile):
        while True:
            sep = infile.read(1)
            if sep in (b'\r', b'\n', b''):
                # End of field headers
                break

            field = DBFField.unpack(sep + infile.read(DBFField.size - 1))

            field.type = chr(ord(field.type))

            # For character fields > 255 bytes the high byte
            # is stored in decimal_count.
            if field.type in 'C':
                field.length |= field.decimal_count << 8
                field.decimal_count = 0

            # Field name is b'\0' terminated.
            field_name = self._decode_text(field.name.split(b'\0')[0])

            if (field.type == '0' and field_name == '_NullFlags'
                    and self._null_flags_field is None):
                # Visual FoxPro appends this hidden system field to every
                # table that has nullable fields. It must not show up as a
                # normal record field. This is checked before lowernames
                # rewrites the field name.
                field.name = field_name
                self._null_flags_field = field
                continue

            if self.lowernames:
                field_name = field_name.lower()

            field.name = field_name

            self.field_names.append(field.name)

            self.fields.append(field)

    def _setup_vfp_fields(self):
        """Compute record offsets and the Visual FoxPro NULL bitmap layout.

        The nullable bitmap is stored at the end of each record in the
        hidden ``_NullFlags`` field. Bit 0 corresponds to the first
        visible field, bit 1 to the second, and so on; a set bit marks
        the field value as NULL for that record.
        """
        offset = 0
        for field in self.fields:
            self._field_offsets.append(offset)
            if field.type in ('V', 'Q'):
                # The descriptor width excludes the trailing length byte.
                offset += field.length + 1
            else:
                offset += field.length

        if self._null_flags_field is None:
            return

        bitmap_len = self._null_flags_field.length
        for bit_index, field in enumerate(self.fields):
            self._null_bit_field[field.name] = (
                bit_index // 8, bit_index % 8)

        # Structural checks. These describe the whole table rather than
        # a single record.
        expected_bitmap_len = (len(self.fields) + 7) // 8
        if bitmap_len != expected_bitmap_len:
            raise ValueError(
                "field '_NullFlags' has length {} but the table has {} "
                "nullable fields, which requires {} bitmap byte(s)".format(
                    bitmap_len, len(self.fields), expected_bitmap_len))

        expected_record_len = 1 + offset + bitmap_len
        if expected_record_len > self.header.recordlen:
            raise ValueError(
                "record length {} is too small for the field data and "
                "the '_NullFlags' bitmap ({} bytes)".format(
                    self.header.recordlen, expected_record_len))

    def _open_memofile(self):
        if self.memofilename and not self.raw:
            return open_memofile(self.memofilename, self.header.dbversion)
        else:
            return FakeMemoFile(self.memofilename)

    def _check_headers(self):
        field_parser = self.parserclass(self)

        """Check headers for possible format errors."""
        for field in self.fields:

            if field.type == 'I' and field.length != 4:
                message = 'Field type I must have length 4 (was {})'
                raise ValueError(message.format(field.length))

            elif field.type == 'L' and field.length != 1:
                message = 'Field type L must have length 1 (was {})'
                raise ValueError(message.format(field.length))

            elif not field_parser.field_type_supported(field.type):
                # Todo: return as byte string?
                raise ValueError('Unknown field type: {!r}'.format(field.type))

    def _skip_record(self, infile):
        # -1 for the record separator which was already read.
        infile.seek(self.header.recordlen - 1, 1)

    def _count_records(self, record_type=b' '):
        count = 0

        with open(self.filename, 'rb') as infile:
            # Skip to first record.
            infile.seek(self.header.headerlen, 0)

            while True:
                sep = infile.read(1)
                if sep == record_type:
                    count += 1
                    self._skip_record(infile)
                elif sep in (b'\x1a', b''):
                    # End of records.
                    break
                else:
                    self._skip_record(infile)

        return count

    def _iter_records(self, record_type=b' '):
        with open(self.filename, 'rb') as infile, \
             self._open_memofile() as memofile:

            # Skip to first record.
            infile.seek(self.header.headerlen, 0)

            if self.raw:
                field_parser = None
            else:
                field_parser = self.parserclass(self, memofile)

            # Shortcuts for speed.
            read = infile.read
            record_len = self.header.recordlen
            record_number = 0

            while True:
                sep = read(1)

                if sep == record_type:
                    record_number += 1
                    record = read(record_len - 1)
                    yield self._build_record(
                        record, field_parser, record_number)

                elif sep in (b'\x1a', b''):
                    # End of records.
                    break
                else:
                    record_number += 1
                    infile.seek(record_len - 1, 1)

    def _read_null_bitmap(self, record, record_number):
        """Read the ``_NullFlags`` bitmap from the end of a record."""
        field = self._null_flags_field
        start = self.header.recordlen - 1 - field.length
        bitmap = record[start:]
        if len(bitmap) != field.length:
            raise InvalidRecord(
                'nullable bitmap is {} byte(s) but field {!r} requires '
                '{} byte(s)'.format(
                    len(bitmap), field.name, field.length),
                record_number=record_number,
                field_name=field.name)
        return bitmap

    def _is_null(self, field, bitmap):
        byte_index, bit_index = self._null_bit_field[field.name]
        return bool(bitmap[byte_index] & (1 << bit_index))

    def _build_record(self, record, field_parser, record_number):
        """Turn the raw bytes of one record into a recfactory object."""
        bitmap = None
        if self._null_flags_field is not None:
            bitmap = self._read_null_bitmap(record, record_number)

        items = []
        for index, field in enumerate(self.fields):
            start = self._field_offsets[index]
            # Varchar/Varbinary fields have an extra trailing length
            # byte that is not counted in the descriptor length.
            stored_length = (field.length + 1
                             if field.type in ('V', 'Q')
                             else field.length)
            end = start + stored_length
            data = record[start:end]
            if len(data) != stored_length:
                raise InvalidRecord(
                    'record is truncated: expected {} byte(s) for field '
                    '{!r} but got {} byte(s)'.format(
                        stored_length, field.name, len(data)),
                    record_number=record_number,
                    field_name=field.name)

            if field_parser is None:
                # Raw mode: hand back the original field bytes without
                # applying any of the semantic conversions.
                value = data
            elif bitmap is not None and self._is_null(field, bitmap):
                # The NULL bitmap overrides whatever the field bytes hold.
                value = None
            else:
                try:
                    value = field_parser.parse(field, data)
                except InvalidRecord as error:
                    if error.record_number is None:
                        raise InvalidRecord(
                            error.args[0],
                            record_number=record_number,
                            field_name=field.name)
                    raise
                except ValueError as error:
                    raise InvalidRecord(
                        str(error),
                        record_number=record_number,
                        field_name=field.name)

            items.append((field.name, value))

        return self.recfactory(items)

    def __iter__(self):
        if self.loaded:
            return list.__iter__(self._records)
        else:
            return self._iter_records()

    def __len__(self):
        return len(self.records)

    def __repr__(self):
        if self.loaded:
            status = 'loaded'
        else:
            status = 'unloaded'
        return '<{} DBF table {!r}>'.format(status, self.filename)

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.unload()
        return False
