"""
Incremental scanning of append-only DBF files.

This module implements a serializable scan cursor for collectors that
repeatedly scan a DBF file to which a producer only appends records.
The cursor tracks the *physical* record position (counting deleted
records as well), so resuming never depends on how many records pass a
filter.

Contract with the producer:

* Records are only ever appended. In-place modification of old records
  is *not* detected.
* The memo file may grow by appending new blocks, but blocks that were
  referenced during a scan are guaranteed by the producer to be left
  unmodified.
* When a scan is opened the record count declared in the header is
  frozen as the upper bound. Records appended while the scan runs are
  left for the next round.
* If the file ends with less than one full record (a partially written
  append), the scan reports itself as incomplete and the cursor is not
  advanced past the partial record.
* If the source raises an error, ``scan.cursor`` is left exactly on the
  last completed record boundary.
"""
import json
import collections

from .exceptions import CursorMismatch

ScannedRecord = collections.namedtuple(
    'ScannedRecord', ['position', 'deleted', 'record'])
ScannedRecord.__doc__ = """
A record read during a scan.

``position`` is the physical record number (0-based, counting deleted
records), ``deleted`` is True if the record is flagged as deleted, and
``record`` is the parsed record.
"""


class ScanCursor(object):
    """Serializable position in an append-only DBF file.

    Attributes:

    ``generation``
        Caller-provided file generation id. The caller is responsible
        for bumping this when the file is replaced with a new table.
        Resuming with a different generation is refused.

    ``field_digest``
        Digest of the field descriptors in the file header. Filled in
        when the cursor is created or validated by ``DBF.scan()``.

    ``position``
        Physical number of the next record to read.
    """
    __slots__ = ('generation', 'field_digest', 'position')

    def __init__(self, generation, field_digest=None, position=0):
        self.generation = generation
        self.field_digest = field_digest
        self.position = position

    def to_dict(self):
        """Return a JSON-serializable dict representation."""
        return {
            'generation': self.generation,
            'field_digest': self.field_digest,
            'position': self.position,
        }

    @classmethod
    def from_dict(cls, data):
        """Rebuild a cursor from a dict produced by ``to_dict()``."""
        return cls(generation=data['generation'],
                   field_digest=data['field_digest'],
                   position=data['position'])

    def dumps(self):
        """Serialize the cursor to a JSON string."""
        return json.dumps(self.to_dict())

    @classmethod
    def loads(cls, text):
        """Rebuild a cursor from a JSON string produced by ``dumps()``."""
        return cls.from_dict(json.loads(text))

    def __eq__(self, other):
        return (isinstance(other, ScanCursor) and
                self.generation == other.generation and
                self.field_digest == other.field_digest and
                self.position == other.position)

    def __ne__(self, other):
        return not self == other

    def __repr__(self):
        return ('ScanCursor(generation={!r}, field_digest={!r}, '
                'position={!r})').format(self.generation,
                                         self.field_digest,
                                         self.position)


class Scan(object):
    """One pass over an append-only DBF file.

    Created by ``DBF.scan()``. Iterating yields ``ScannedRecord``
    objects up to the record count that was frozen when the scan was
    opened. ``cursor`` is updated as records complete and can be
    serialized and used to resume the next round. ``incomplete`` is
    True if the file ended with a partially written record; the cursor
    then points at that record so it is retried next round.
    """
    def __init__(self, table, cursor, ceiling):
        self.table = table
        self.cursor = cursor
        self.ceiling = ceiling
        self.incomplete = False

    def __repr__(self):
        return '<Scan position={} ceiling={} incomplete={}>'.format(
            self.cursor.position, self.ceiling, self.incomplete)

    def __iter__(self):
        table = self.table
        cursor = self.cursor

        with open(table.filename, 'rb') as infile, \
             table._open_memofile() as memofile:

            if table.raw:
                def parse(_, data):
                    return data
            else:
                field_parser = table.parserclass(table, memofile)
                parse = field_parser.parse

            headerlen = table.header.headerlen
            recordlen = table.header.recordlen
            fields = table.fields
            recfactory = table.recfactory
            read = infile.read
            seek = infile.seek

            for position in range(cursor.position, self.ceiling):
                seek(headerlen + position * recordlen, 0)
                data = read(recordlen)

                if len(data) < recordlen:
                    # Partially written record at the end of the file.
                    # Report it and leave the cursor on this record so
                    # it is retried in the next round.
                    self.incomplete = True
                    return

                offset = 1
                items = []
                for field in fields:
                    items.append(
                        (field.name,
                         parse(field, data[offset:offset + field.length])))
                    offset += field.length

                # The record is fully read and parsed. Advance the
                # cursor before yielding so it always rests on a
                # complete record boundary, even if the consumer
                # aborts or the source raises on the next record.
                cursor.position = position + 1

                yield ScannedRecord(position=position,
                                    deleted=data[0:1] == b'*',
                                    record=recfactory(items))


def validate_cursor(generation, cursor, numrecords, field_digest):
    """Check a resume cursor against the current state of the file.

    Raises CursorMismatch if the generation id differs, the field
    layout has changed, or the record count has moved backwards.
    """
    if cursor.generation != generation:
        raise CursorMismatch(
            'file generation mismatch: cursor has {!r}, expected {!r}'.format(
                cursor.generation, generation))

    if cursor.field_digest != field_digest:
        raise CursorMismatch(
            'field layout has changed since the cursor was created')

    if cursor.position > numrecords:
        raise CursorMismatch(
            'record count moved backwards: cursor is at physical record '
            '{} but the header now declares {} records'.format(
                cursor.position, numrecords))
