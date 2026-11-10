class DBFNotFound(IOError):
    """Raised if the DBF file was not found."""
    pass


class MissingMemoFile(IOError):
    """Raised if the corresponding memo file was not found."""


class CursorMismatch(ValueError):
    """Raised if a scan cursor cannot be used to resume reading a file.

    This happens when the file generation id does not match, the field
    layout has changed, or the record count has moved backwards (the
    file was truncated or replaced).
    """


__all__ = ['DBFNotFound', 'MissingMemoFile', 'CursorMismatch']
