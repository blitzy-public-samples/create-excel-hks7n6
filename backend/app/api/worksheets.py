from fastapi import APIRouter, Depends, HTTPException
from typing import List
from sqlalchemy.orm import Session
from backend.app.db import get_db
from backend.app.schema import WorksheetSchema
from backend.app.services import WorksheetService

router = APIRouter()

@router.get('/workbooks/{workbook_id}/worksheets')
def get_worksheets(workbook_id: str, db: Session = Depends(get_db)) -> List[WorksheetSchema]:
    worksheet_service = WorksheetService(db)
    worksheets = worksheet_service.get_worksheets(workbook_id)
    
    if not worksheets:
        raise HTTPException(status_code=404, detail="No worksheets found for the given workbook")
    
    return [WorksheetSchema.from_orm(worksheet) for worksheet in worksheets]

# HUMAN ASSISTANCE NEEDED
# The following improvements might be necessary:
# 1. Add error handling for invalid workbook_id
# 2. Implement pagination for large numbers of worksheets
# 3. Add authentication and authorization checks
# 4. Implement caching mechanism for frequently accessed worksheets
# 5. Add logging for monitoring and debugging purposes