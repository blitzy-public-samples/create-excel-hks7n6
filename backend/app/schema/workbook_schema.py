from pydantic import BaseModel
from typing import List, Dict, Optional
from datetime import datetime

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