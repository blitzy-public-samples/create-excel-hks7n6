from pydantic import BaseModel, validator
from typing import Any, Dict, List, Optional
from collections.abc import Mapping
from datetime import datetime

# The number of letters in the spreadsheet column alphabet. A column index is rendered in this
# bijective base - 1 is A, 26 is Z, 27 is AA - which is the notation the whole product speaks:
# frontend/src/components/ChartDialog.tsx prompts for a range as "A1:B10".
COLUMN_ALPHABET_SIZE = 26

# First letter of that alphabet, as a code point, so a column index maps onto A..Z arithmetically.
COLUMN_FIRST_LETTER = ord("A")


def cell_reference(row: Any, column: Any) -> str:
    """Return the map key that addresses the cell at ``row`` and ``column``.

    ``CellSchema`` declares no coordinate field, so the key of the ``cells`` map is the only
    place a response carries a cell's position. This renders the A1 form for the one-based
    coordinates a spreadsheet uses, and falls back to an ``R{row}C{column}`` form for anything
    else - a missing, zero, negative or non-integer coordinate - so that every row a database
    can hold produces exactly one key. The two forms cannot collide: an A1 key always begins
    with a letter and the fallback always begins with ``R`` followed by a digit or a sign.

    Args:
        row: The cell's one-based row number, as stored on the mapped row.
        column: The cell's one-based column number, as stored on the mapped row.

    Returns:
        str: ``"A1"`` for row 1 column 1, ``"AA3"`` for row 3 column 27, and
            ``"R0C0"``-style text for a coordinate outside the one-based domain.
    """
    try:
        row_number = int(row)
        column_number = int(column)
    except (TypeError, ValueError):
        return "R{0}C{1}".format(row, column)
    if row_number < 1 or column_number < 1:
        return "R{0}C{1}".format(row_number, column_number)
    letters = ""
    remaining = column_number
    while remaining > 0:
        remaining, index = divmod(remaining - 1, COLUMN_ALPHABET_SIZE)
        letters = chr(COLUMN_FIRST_LETTER + index) + letters
    return "{0}{1}".format(letters, row_number)


class CellSchema(BaseModel):
    value: str
    formula: Optional[str]
    style: Dict[str, str]

class WorksheetSchema(BaseModel):
    name: str
    cells: Dict[str, CellSchema]
    named_ranges: Optional[Dict[str, str]]

    class Config:
        # Required by the from_orm call in backend/app/api/worksheets.py: without it Pydantic
        # refuses to read attributes off a row object at all and the worksheets route raises
        # before it can answer. Field names, types and the serialized shape are unaffected, so
        # the frozen response contract is unchanged.
        orm_mode = True

    @validator("cells", pre=True, allow_reuse=True)
    def _project_mapped_cell_rows(cls, cells: Any) -> Any:
        """Accept the mapped ``Worksheet.cells`` list as well as the declared map.

        ``Worksheet.cells`` is a one-to-many relationship, so a row object presents a LIST of
        ``Cell`` rows carrying ``row``, ``column``, ``value``, ``formula`` and ``style``, while
        this field declares a map keyed by cell reference. ``GET /workbooks/{id}/worksheets``
        answered ``500`` for every worksheet holding at least one cell because of that
        mismatch: an empty relationship coerced, since Pydantic reads ``[]`` as ``{}``, so the
        failure surfaced on exactly the case every real workbook is.

        A mapping is returned untouched, which is what keeps the request contract intact - the
        create body sends the declared map and reaches the field validators unchanged. Only a
        non-mapping sequence is projected.

        The projection is total, because a partial one would move the ``500`` rather than close
        it: ``Cell.value`` and ``Cell.style`` are nullable columns while ``value`` and ``style``
        are required here, so a stored NULL is rendered as the empty string and the empty map
        respectively - the value an empty cell has in a spreadsheet - and any coordinate outside
        the one-based domain still yields a key. Two rows sharing a coordinate collapse onto one
        key, which is inherent to a map-shaped response and not something this can avoid; the
        table declares no uniqueness constraint that would prevent it.

        Args:
            cells: Either the declared map, or the sequence of mapped ``Cell`` rows a row
                object presents.

        Returns:
            The value unchanged when it is already a mapping, otherwise a map from cell
            reference to the field values ``CellSchema`` declares.
        """
        if isinstance(cells, Mapping) or cells is None:
            return cells
        if isinstance(cells, (str, bytes)):
            return cells
        try:
            rows = list(cells)
        except TypeError:
            return cells
        projected: Dict[str, Any] = {}
        for row in rows:
            if isinstance(row, Mapping) or not hasattr(row, "value"):
                # Not a mapped cell row. Hand the original value back so Pydantic reports the
                # type error against what the caller actually sent rather than against a
                # half-built map.
                return cells
            projected[cell_reference(getattr(row, "row", None), getattr(row, "column", None))] = {
                "value": "" if getattr(row, "value", None) is None else row.value,
                "formula": getattr(row, "formula", None),
                "style": getattr(row, "style", None) or {},
            }
        return projected

class WorkbookSchema(BaseModel):
    id: str
    name: str
    owner_id: str
    worksheets: List[WorksheetSchema]
    created_at: datetime
    modified_at: datetime
    settings: Optional[Dict[str, str]]

# HUMAN ASSISTANCE NEEDED
# The WorkbookSchema class has a confidence level of 0.7, which is below the threshold of 0.8.
# Please review and validate the WorkbookSchema class, especially the datetime fields and the
# relationship with WorksheetSchema.

class FormulaSchema(BaseModel):
    expression: str
    dependencies: List[str]

class ChartSchema(BaseModel):
    type: str
    data_range: Dict[str, str]
    options: Dict[str, str]

# HUMAN ASSISTANCE NEEDED
# The ChartSchema class has a confidence level of 0.7, which is below the threshold of 0.8.
# Please review and validate the ChartSchema class, especially the data_range and options fields.
# Consider if additional properties or validations are needed for different chart types.