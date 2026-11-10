class DBFNotFound(IOError):
    """Raised if the DBF file was not found."""
    pass


class MissingMemoFile(IOError):
    """Raised if the corresponding memo file was missing."""


class MemoError(IOError):
    """Base class for errors raised while reading a memo block.

    These are the only errors that can be isolated with the
    ``on_memo_error`` callback. Each error carries the memo ``pointer``
    and the byte ``offset`` of the block in the memo file so that an
    isolated failure can be located without parsing the message.
    """

    def __init__(self, message, pointer, offset):
        super(MemoError, self).__init__(message)
        self.pointer = pointer
        self.offset = offset


class InvalidMemoPointer(MemoError):
    """Raised when a memo pointer points outside the memo file."""


class TruncatedMemo(MemoError):
    """Raised when a memo block ends before its declared data."""


class UnsupportedMemoType(MemoError):
    """Raised when a memo block has an unknown block type."""


class DBFRecordTruncated(IOError):
    """Raised when a DBF record ends before all fields are read."""


__all__ = ['DBFNotFound',
           'MissingMemoFile',
           'MemoError',
           'InvalidMemoPointer',
           'TruncatedMemo',
           'UnsupportedMemoType',
           'DBFRecordTruncated']
