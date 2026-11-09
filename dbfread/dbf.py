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
from .exceptions import DBFNotFound, MissingMemoFile

_py_version = (sys.version_info.major, sys.version_info.minor)
_py_impl = platform.python_implementation()
if _py_version >= (3, 7) or (_py_version == (3, 6) and _py_impl == 'CPython'):
    ORDERED_DICT = dict
else:
    ORDERED_DICT = collections.OrderedDict


# Field types whose content is stored in the memo (FPT/DBT) file.
MEMO_FIELD_TYPES = frozenset('MGPB')


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
                 char_decode_errors='strict',
                 predicate_fields=None,
                 predicate=None,
                 fields=None):

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

        with open(self.filename, mode='rb') as infile:
            self._read_header(infile)
            self._read_field_headers(infile)
            self._check_headers()

            try:
                self.date = datetime.date(expand_year(self.header.year),
                                          self.header.month,
                                          self.header.day)
            except ValueError:
                # Invalid date or '\x00\x00\x00'.
                self.date = None

        self.predicate = predicate
        self._predicate_fields = self._resolve_predicate_fields(
            predicate_fields, predicate)
        self._output_fields = self._resolve_output_fields(fields)

        self.memofilename = self._get_memofilename()

        if load:
            self.load()

    def _resolve_field_names(self, names, option):
        """Resolve caller supplied field names to field objects.

        Names are matched against the table's field names as they
        appear after the current case handling (``lowernames``).
        Unknown and duplicate names are rejected with ``ValueError``.
        """
        by_name = dict((field.name, field) for field in self.fields)
        resolved = []
        seen = set()
        for name in names:
            if name in seen:
                raise ValueError(
                    'duplicate field in {}: {!r}'.format(option, name))
            seen.add(name)
            try:
                resolved.append(by_name[name])
            except KeyError:
                raise ValueError(
                    'unknown field in {}: {!r}'.format(option, name))
        return resolved

    def _resolve_predicate_fields(self, predicate_fields, predicate):
        if predicate is None:
            if predicate_fields is not None:
                raise ValueError(
                    'predicate_fields requires a predicate')
            return []
        if predicate_fields is None:
            raise ValueError(
                'predicate requires predicate_fields')

        resolved = self._resolve_field_names(predicate_fields,
                                             'predicate_fields')
        for field in resolved:
            if field.type in MEMO_FIELD_TYPES:
                message = 'predicate field must not be a memo field: {!r}'
                raise ValueError(message.format(field.name))
        return resolved

    def _resolve_output_fields(self, fields):
        if fields is None:
            return None
        return self._resolve_field_names(fields, 'fields')

    def _memo_fields_needed(self):
        """``True`` if produced records may include memo field values."""
        if self._output_fields is None:
            return True
        return any(field.type in MEMO_FIELD_TYPES
                   for field in self._output_fields)

    @property
    def dbversion(self):
        return get_dbversion_string(self.header.dbversion)

    def _get_memofilename(self):
        # Does the table have a memo field?
        field_types = [field.type for field in self.fields]
        if not set(field_types) & MEMO_FIELD_TYPES:
            # No memo fields.
            return None

        path = find_memofile(self.filename)
        if path is None:
            if self.ignore_missing_memofile or \
                    not self._memo_fields_needed():
                # The memo file is not needed to produce the
                # requested fields.
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
            field.name = self._decode_text(field.name.split(b'\0')[0])
            if self.lowernames:
                field.name = field.name.lower()

            self.field_names.append(field.name)

            self.fields.append(field)

    def _open_memofile(self):
        if self.memofilename and not self.raw \
                and self._memo_fields_needed():
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
                def parse(_, data):
                    return data
            else:
                field_parser = self.parserclass(self, memofile)
                parse = field_parser.parse

            # Shortcuts for speed.
            skip_record = self._skip_record
            read = infile.read

            predicate = self.predicate
            output_fields = self._output_fields

            if predicate is None and output_fields is None:
                for record in self._iter_all_records(
                        record_type, read, parse, skip_record, infile):
                    yield record
            else:
                for record in self._iter_selected_records(
                        record_type, read, parse, skip_record, infile):
                    yield record

    def _iter_all_records(self, record_type, read, parse, skip_record,
                          infile):
        # No filtering or projection: parse every field of every
        # record, like dbfread always has.
        while True:
            sep = read(1)

            if sep == record_type:
                items = [
                    (field.name, parse(field, read(field.length)))
                    for field in self.fields
                ]
                yield self.recfactory(items)

            elif sep in (b'\x1a', b''):
                # End of records.
                break
            else:
                skip_record(infile)

    def _iter_selected_records(self, record_type, read, parse, skip_record,
                               infile):
        # Filtering and/or projection was requested. Only parse
        # predicate fields up front, run the predicate once per
        # candidate record, and parse the remaining output fields
        # only for records that pass. Fields that are not needed at
        # all are seeked past without being read, and memo content is
        # never touched for filtered out records.
        predicate = self.predicate
        predicate_fields = self._predicate_fields
        predicate_names = set(field.name for field in predicate_fields)

        output_fields = self._output_fields
        if output_fields is None:
            output_fields = self.fields
        output_names = set(field.name for field in output_fields)

        seek = infile.seek
        recfactory = self.recfactory

        while True:
            sep = read(1)

            if sep == record_type:
                parsed = {}
                deferred = {}

                for field in self.fields:
                    if field.name in predicate_names:
                        parsed[field.name] = parse(field,
                                                   read(field.length))
                    elif field.name in output_names:
                        # Read the raw bytes now (the file is read
                        # sequentially) but defer parsing until the
                        # record has passed the predicate.
                        deferred[field.name] = (field, read(field.length))
                    else:
                        # Field not needed: skip its bytes entirely.
                        seek(field.length, 1)

                if predicate is not None:
                    predicate_record = ORDERED_DICT(
                        (field.name, parsed[field.name])
                        for field in predicate_fields)
                    if not predicate(predicate_record):
                        # Record filtered out. Output fields are not
                        # parsed and memo content is never read.
                        continue

                items = []
                for field in output_fields:
                    if field.name in parsed:
                        # Already parsed for the predicate: reuse the
                        # value instead of parsing again.
                        value = parsed[field.name]
                    else:
                        deferred_field, data = deferred[field.name]
                        value = parse(deferred_field, data)
                    items.append((field.name, value))
                yield recfactory(items)

            elif sep in (b'\x1a', b''):
                # End of records.
                break
            else:
                skip_record(infile)

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
