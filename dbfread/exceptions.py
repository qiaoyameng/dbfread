class DBFNotFound(IOError):
    """Raised if the DBF file was not found."""
    pass


class MissingMemoFile(IOError):
    """Raised if the corresponding memo file was not found."""


class InvalidMemoError(ValueError):
    """Raised if a memo pointer or memo file layout is invalid."""


__all__ = ['DBFNotFound', 'MissingMemoFile', 'InvalidMemoError']
