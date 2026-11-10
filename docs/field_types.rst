Field Types
===========

Supported Field Types
---------------------

==  =============  ========================================================
:   Field type     Converted to
==  =============  ========================================================
\+  autoincrement  int
@   time           datetime.datetime
0   flags          byte string (int before 2.0)
B   double         float (Visual FoxPro)
B   binary memo    byte string (other versions)
C   text           unicode string
D   date           datetime.date or None
F   float          float
G   OLE object     byte string
I   integer        int
L   logical        True, False or None
M   memo           unicode string (memo), byte string (picture or object)
                   or None
N   numeric        int, float or None
O   double         float (floats are doubles in Python)
P   picture        byte string
T   time           datetime.datetime
V   varchar        unicode string
Y   currency       decimal.Decimal
Q   varbinary      byte string
==  =============  ========================================================

Text values ('C') can be up to 65535 bytes long. DBF was originally
limited to 255 bytes but some vendors have reused the
``decimal_count`` field to get another byte for field length.

The 'B' field type is used to store double precision (64 bit) floats
in Visual FoxPro databases and binary memos in other
versions. ``dbfread`` will look at the database version to parse and
return the correct data type.

Visual FoxPro nullable fields
-----------------------------

When a table contains nullable fields Visual FoxPro appends a hidden
system field named ``_NullFlags`` (type ``'0'``). It holds a bitmap with
one bit per visible field, in field-descriptor order; a set bit means
the value of that field is ``NULL``. dbfread reads this bitmap
automatically, returns ``None`` for nullable fields whose bit is set,
and never exposes ``_NullFlags`` itself as a record field.

A ``NULL`` value is distinct from an empty value. For a Varchar field
an empty string (``''``) and for a Varbinary field a zero-length byte
string (``b''``) are returned as such when the NULL bit is clear, and
as ``None`` only when the bit is set.

Varchar (``'V'``) and Varbinary (``'Q'``) fields are variable length.
Each value occupies the declared width plus one extra byte: the data,
left-justified and padded with ``0x00``, followed by a one-byte length
of the actual data. A length of zero is the empty value, while a length
equal to the width means the value fills the whole field. Varchar is
decoded using the table encoding while Varbinary is returned as
``bytes``.

In ``raw`` mode the original field bytes are returned unchanged: the
NULL bitmap is not applied and Varchar/Varbinary values keep their
padding and trailing length byte.


Adding Custom Field Types
-------------------------

You can add new field types by subclassing
:py:class:`FieldParser`. For example:

.. literalinclude:: ../examples/custom_field_type.py

The ``FieldParser`` object has the following attributes:

self.table
  A reference to the ``DBF`` objects. This can be used to get the headers
  to find ``dbversion`` and other things.

self.encoding
  The character encoding. (A a shortcut for ``self.table.encoding`` to
  speed things up a bit.)

self.char_decode_errors
  Error handling scheme to use while decoding. (A shortcut for ``self.table.char_decode_errors``.)

self.dbversion
  The database version as an integer. (A shortcut for
  ``self.table.header.dbversion``.)

self.get_memo(index)
  Returns a memo from the memo file using the index stored in the field data.

  This returns a byte string (``bytes``) which you can then
  decode.

  For Visual FoxPro (``.FPT``) files it will return ``TextMemo``,
  ``PictureMemo`` and ``ObjectMemo`` objects depending on the type of
  memo. These are all subclasses of ``bytes`` so the type is only used
  to annotate the memo type without breaking code elsewhere. The full
  class tree::

      bytes
        VFPMemo
          TextMemo
          BinaryMemo
            PictureMemo
            ObjectMemo

  These are all found in ``dbfread.memo``.

self.decode_text(text)

  This will decode the text using the correct encoding and the user
  supplied ``char_decode_errors`` option.


Special Characters in Field Type Names
--------------------------------------

For a field type like '+' (autoincrement) the method would be named
``parse+()``.  Since this is not allowed in Python you can instead use
its ASCII value in hexadecimal. For example, the '+' parser is called
``parse3F()``.

You can name your method with::

    >>> 'parse' + format(ord('?'), 'x').upper()
    'parse3F'

Just replace ``'?'`` with your field type.


InvalidValue
------------

The field parser will normally raise ``ValueError`` when invalid
values are encountered. If instead you want them returned as raw data
you can do this:

.. literalinclude:: ../examples/print_invalid_values.py

``InvalidValue`` is a subclass of ``bytes``, and allows you to tell
invalid data apart from valid data that happens to be byte
strings. You can test for this with::

    isinstance(value, InvalidData)

You can also tell from the ``repr()`` string::

    >>> value
    InvalidData(b'not a number')
