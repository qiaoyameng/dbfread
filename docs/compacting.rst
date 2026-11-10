.. highlight:: python

Compacting memo files
=====================

Memo files (FPT/DBT) tend to grow over time: updated or deleted
records leave behind blocks that are no longer referenced by any
record. ``compact_copy()`` writes a compacted copy of a DBF table and
its memo file to a new directory, keeping only the blocks that are
actually referenced::

    from dbfread import compact_copy

    result = compact_copy('people.dbf', 'compacted/')
    print(result.blocks_before, '->', result.blocks_after)

The original files are never modified. Visual FoxPro FPT and dBase
III DBT memo files are supported.

Behaviour:

* Memo blocks are collected by following the memo pointers in the
  records, then laid out contiguously, and the pointers in the new
  DBF file are rewritten. The record count in the DBF header and the
  next-block pointer in the memo file header are updated; every other
  byte of the records and field headers is preserved, including
  deletion flags and record order.

* Records that share a memo block still share it afterwards. Two
  different pointers are never merged, even if their content is
  identical.

* Pass ``keep_deleted=False`` to drop deleted records (and the memo
  blocks only they reference) from the copy.

* Block size, memo subtypes (text, picture, object) and end markers
  follow the original format.

* All memo references and the file layout are validated before
  anything is written. A bad pointer raises ``InvalidMemoError``
  instead of being treated as an empty memo, and a failed run leaves
  no seemingly complete files in the target directory.

The returned ``CompactResult`` named tuple contains the new file
paths, record counts and the number of allocated memo blocks before
and after compaction.
