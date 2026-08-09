"""The bounds every list route applies to its page window.

A list route that accepts any integer for its offset and page size is an amplification
vector and a fault surface at the same time, and both halves were measured against the
running application rather than reasoned about:

* ``GET /workbooks?limit=1000000`` and ``?limit=9223372036854775807`` each answered ``200``
  carrying **every** row in the table - 251 workbooks, 6,809,936 bytes, 13.4 seconds - so a
  single request could make the server serialize the whole dataset. ``GET
  /workbooks/{id}/worksheets`` accepted no page parameter at all, so its response was
  bounded only by how many worksheets a workbook happened to hold.
* A negative value reached SQL as ``OFFSET -1`` / ``LIMIT -1`` and PostgreSQL refused it
  (``InvalidRowCountInResultOffsetClause`` / ``InvalidRowCountInLimitClause``), and a value
  above ``int64`` overflowed the bind parameter (``NumericValueOutOfRange``). Each surfaced
  as an unhandled HTTP 500 where the declared parameter type otherwise produces a 422 -
  ``limit=abc`` and ``limit=' OR 1=1 --`` were both already refused with 422.

Both halves are closed by the same declarative constraint, which is why the numbers live
here rather than being written twice: the two list routes cannot drift apart, and the
"documented maximum" a client is entitled to rely on has one definition. Nothing here is
operator-tunable - these are contract bounds, not thresholds - so they are module constants
rather than :class:`~backend.app.core.config.Settings` fields.

Rationale for the specific values, the alternatives weighed and the residual risk are
recorded in ``documentation/Security Decision Log.md``.
"""

#: Page size a caller gets when it asks for none. Unchanged from the value the route has
#: always defaulted to, so a client that sends no parameter sees exactly what it saw before.
DEFAULT_PAGE_SIZE: int = 100

#: Largest page a caller may ask for. Deliberately equal to :data:`DEFAULT_PAGE_SIZE`: no
#: caller is entitled to a response larger than the one the route produces unasked, and the
#: default page already serializes to roughly 2.7 MB. A client needing more pages through
#: with ``skip``, which costs the server a bounded amount of work per request.
MAX_PAGE_SIZE: int = 100

#: Largest offset a caller may ask for. An offset is not an amplifier - the response is still
#: bounded by :data:`MAX_PAGE_SIZE` - so this exists to keep the value inside ``int64``,
#: where a bind parameter above ``9223372036854775807`` was answered 500. A million rows is
#: far beyond any offset a client of this product reaches while paging.
MAX_PAGE_OFFSET: int = 1_000_000

#: Offset a caller gets when it asks for none.
DEFAULT_PAGE_OFFSET: int = 0
