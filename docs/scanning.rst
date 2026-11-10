Incremental Scanning
====================

Collectors that repeatedly scan an append-only DBF file can use
``DBF.scan()`` to read only the records added since the last round.
The scan returns a serializable cursor that tracks the *physical*
record position (deleted records included), never a count of filtered
rows::

    table = DBF('events.dbf')

    # First round: scan from the beginning.
    scan = table.scan(generation='january-table')
    for item in scan:
        print(item.position, item.deleted, item.record)
    save(scan.cursor.dumps())          # persist somewhere

    # Next round: resume where we left off.
    cursor = ScanCursor.loads(load())
    scan = table.scan('january-table', cursor=cursor)
    for item in scan:
        ...

Each item is a ``ScannedRecord`` with the physical record number
(``position``), a ``deleted`` flag so deleted records can be separated
out, and the parsed ``record``.

The record count declared in the header is frozen when the scan is
opened, so records appended while a scan runs are left for the next
round. If the file ends with a partially written record, the scan sets
``scan.incomplete`` to ``True`` and leaves the cursor on that record
so it is retried later. If the source raises an error, ``scan.cursor``
is always left exactly on the last completed record boundary.

Resuming is refused with ``CursorMismatch`` when:

* the caller-provided ``generation`` id differs from the cursor's
  (use a new generation id when the file is replaced by a new table),
* the field layout in the header has changed, or
* the record count has moved backwards (truncated or replaced file).

Only append-only producers are supported. In-place modification of old
records is not detected. Memo files may grow by appending new blocks,
but blocks referenced during a scan must be left unmodified by the
producer.
