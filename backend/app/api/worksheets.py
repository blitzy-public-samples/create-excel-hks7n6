from fastapi import APIRouter, Depends, HTTPException, Query
from typing import List
from sqlalchemy.orm import Session
from backend.app.db import get_db
from backend.app.schema import WorksheetSchema
from backend.app.services import WorksheetService
from backend.app.core.pagination import (
    DEFAULT_PAGE_OFFSET,
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_OFFSET,
    MAX_PAGE_SIZE,
)
from backend.app.core.security import get_current_user
from backend.app.db.models import User

router = APIRouter()

# SECURITY: require a verified caller for this route.
@router.get('/workbooks/{workbook_id}/worksheets')
def get_worksheets(
    workbook_id: str,
    db: Session = Depends(get_db),
    # SECURITY: bound the page window - this route accepted no page parameter, so its
    # response grew with the workbook and was bounded only by how many worksheets it held.
    skip: int = Query(DEFAULT_PAGE_OFFSET, ge=0, le=MAX_PAGE_OFFSET),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    current_user: User = Depends(get_current_user)
) -> List[WorksheetSchema]:
    worksheet_service = WorksheetService(db)
    worksheets = worksheet_service.get_worksheets(workbook_id, skip=skip, limit=limit)
    
    if not worksheets:
        raise HTTPException(status_code=404, detail="No worksheets found for the given workbook")
    
    return [WorksheetSchema.from_orm(worksheet) for worksheet in worksheets]
