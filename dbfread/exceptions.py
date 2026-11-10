class DBFNotFound(IOError):
    """Raised if the DBF file was not found."""
    pass


class MissingMemoFile(IOError):
    """Raised if the corresponding memo file was not found."""


class InvalidRecord(ValueError):
    """Raised when a record cannot be interpreted.

    Visual FoxPro specific record errors (a truncated nullable bitmap or
    an out-of-range Varchar/Varbinary length byte) are reported with the
    physical record number and the name of the field involved so that the
    offending row can be located in the file.
    """

    def __init__(self, message, record_number=None, field_name=None):
        self.record_number = record_number
        self.field_name = field_name

        if record_number is not None and field_name is not None:
            message = ('record {} field {!r}: {}'.format(
                record_number, field_name, message))
        elif record_number is not None:
            message = 'record {}: {}'.format(record_number, message)

        super(InvalidRecord, self).__init__(message)


__all__ = ['DBFNotFound', 'MissingMemoFile', 'InvalidRecord']
