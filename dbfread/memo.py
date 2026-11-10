"""
Reads data from FPT (memo) files.

FPT files are used to varying lenght text or binary data which is too
large to fit in a DBF field.

VFP == Visual FoxPro
DB3 == dBase III
DB4 == dBase IV
"""
from .ifiles import ifind
from .struct_parser import StructParser
from .exceptions import (InvalidMemoPointer,
                         TruncatedMemo,
                         UnsupportedMemoType)


VFPFileHeader = StructParser(
    'FPTHeader',
    '>LHH504s',
    ['nextblock',
     'reserved1',
     'blocksize',
     'reserved2'])

VFPMemoHeader = StructParser(
    'FoxProMemoHeader',
    '>LL',
    ['type',
     'length'])

DB4MemoHeader = StructParser(
    'DBase4MemoHeader',
    '<LL',
    ['reserved',  # Always 0xff 0xff 0x08 0x08.
     'length'])


class VFPMemo(bytes):
    pass


# Used for Visual FoxPro memos to distinguish binary from text memos.
class BinaryMemo(VFPMemo):
    pass


class PictureMemo(BinaryMemo):
    pass


class ObjectMemo(BinaryMemo):
    pass


class TextMemo(VFPMemo):
    pass


VFP_TYPE_MAP = {
    0x0: PictureMemo,
    0x1: TextMemo,
    0x2: ObjectMemo,
}


class MemoFile(object):
    def __init__(self, filename):
        self.filename = filename
        self._open()
        self._init()

    def _init(self):
        pass

    def _open(self):
        self.file = open(self.filename, 'rb')
        # Shortcuts for speed.
        self._read = self.file.read
        self._seek = self.file.seek

    def _close(self):
        self.file.close()

    def _file_size(self):
        current = self.file.tell()
        self.file.seek(0, 2)
        size = self.file.tell()
        self.file.seek(current)
        return size

    def __getitem__(self, index):
        raise NotImplementedError

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self._close()
        return False


class FakeMemoFile(MemoFile):
    def __getitem__(self, i):
        return None

    def _open(self):
        pass

    _init = _close = _open


class VFPMemoFile(MemoFile):
    def _init(self):
        self.header = VFPFileHeader.read(self.file)
        self._size = self._file_size()

    def __getitem__(self, index):
        """Get a memo from the file."""
        if index <= 0:
            return None

        offset = index * self.header.blocksize
        if offset >= self._size:
            raise InvalidMemoPointer(
                'memo pointer {} is outside the memo file'.format(index),
                pointer=index, offset=offset)

        self._seek(offset)
        header_bytes = self.file.read(VFPMemoHeader.size)
        if len(header_bytes) != VFPMemoHeader.size:
            raise TruncatedMemo(
                'EOF reached while reading memo header at block '
                '{}'.format(index),
                pointer=index, offset=offset)
        memo_header = VFPMemoHeader.unpack(header_bytes)

        data = self._read(memo_header.length)
        if len(data) != memo_header.length:
            raise TruncatedMemo(
                'EOF reached while reading memo at block {}'.format(index),
                pointer=index, offset=offset)

        try:
            memo_type = VFP_TYPE_MAP[memo_header.type]
        except KeyError:
            raise UnsupportedMemoType(
                'unsupported memo block type 0x{:x} at block {}'.format(
                    memo_header.type, index),
                pointer=index, offset=offset)
        return memo_type(data)


class DB3MemoFile(MemoFile):
    """dBase III memo file."""
    # Code from dbf.py
    def _init(self):
        self._size = self._file_size()

    def __getitem__(self, index):
        if index <= 0:
            return None

        block_size = 512
        offset = index * block_size
        if offset >= self._size:
            raise InvalidMemoPointer(
                'memo pointer {} is outside the memo file'.format(index),
                pointer=index, offset=offset)

        self._seek(offset)
        data = b''
        while True:
            newdata = self._read(block_size)
            if not newdata:
                return data
            data += newdata

            # Todo: some files (help.dbt) has only one field separator.
            # Is this enough for all file though?
            end_of_memo = data.find(b'\x1a')
            if end_of_memo != -1:
                return data[:end_of_memo]

            # Alternative end of memo markers:
            # \x1a\x1a
            # \x0d\x0a

        # TODO: this is never reached. Whey is it here?
        # return data[:eom]


class DB4MemoFile(MemoFile):
    """dBase IV memo file"""
    def _init(self):
        self._size = self._file_size()

    def __getitem__(self, index):
        if index <= 0:
            return None

        # Todo: read this from the file header.
        block_size = 512

        offset = index * block_size
        if offset >= self._size:
            raise InvalidMemoPointer(
                'memo pointer {} is outside the memo file'.format(index),
                pointer=index, offset=offset)

        self._seek(offset)
        header_bytes = self.file.read(DB4MemoHeader.size)
        if len(header_bytes) != DB4MemoHeader.size:
            raise TruncatedMemo(
                'EOF reached while reading memo header at block '
                '{}'.format(index),
                pointer=index, offset=offset)
        memo_header = DB4MemoHeader.unpack(header_bytes)
        data = self._read(memo_header.length)
        if len(data) != memo_header.length:
            raise TruncatedMemo(
                'EOF reached while reading memo at block {}'.format(index),
                pointer=index, offset=offset)
        # Todo: fields are terminated in different ways.
        # \x1a is one of them
        # \x1f seems to be another (dbase_8b.dbt)
        return data.split(b'\x1f', 1)[0]


def find_memofile(dbf_filename):
    for ext in ['.fpt', '.dbt']:
        name = ifind(dbf_filename, ext=ext)
        if name:
            return name
    else:
        return None


def open_memofile(filename, dbversion):
    if filename.lower().endswith('.fpt'):
        return VFPMemoFile(filename)
    else:
        # print('######', dbversion)
        if dbversion == 0x83:
            return DB3MemoFile(filename)
        else:
            return DB4MemoFile(filename)
