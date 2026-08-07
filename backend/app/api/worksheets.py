from fastapi import APIRouter, Depends, HTTPException
from typing import List
from sqlalchemy.orm import Session
from backend.app.db import get_db
from backend.app.schema import WorksheetSchema
from backend.app.services import WorksheetService
from backend.app.core.security import get_current_user
from backend.app.db.models import User

router = APIRouter()

# SECURITY: enforce authentication — previously no route required a valid token
@router.get('/workbooks/{workbook_id}/worksheets')
def get_worksheets(workbook_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)) -> List[WorksheetSchema]:
    worksheet_service = WorksheetService(db)
    worksheets = worksheet_service.get_worksheets(workbook_id)
    
    if not worksheets:
        raise HTTPException(status_code=404, detail="No worksheets found for the given workbook")
    
    return [WorksheetSchema.from_orm(worksheet) for worksheet in worksheets]

# HUMAN ASSISTANCE NEEDED
# The following improvements might be necessary:
# 1. Add error handling for invalid workbook_id
# 2. Implement pagination for large numbers of worksheets
# 3. Add workbook authorization checks
# 4. Implement caching mechanism for frequently accessed worksheets
# 5. Add logging for monitoring and debugging purposes