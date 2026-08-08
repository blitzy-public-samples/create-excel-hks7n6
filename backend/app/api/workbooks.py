from fastapi import APIRouter, Depends, HTTPException
from typing import List
from sqlalchemy.orm import Session
from backend.app.db import get_db
from backend.app.schema import WorkbookSchema
from backend.app.services import WorkbookService
from backend.app.core.security import get_current_user
from backend.app.db.models import User

router = APIRouter()

# SECURITY: require a verified caller for this route.
@router.get('/workbooks')
def get_workbooks(
    db: Session = Depends(get_db),
    skip: int = 0,
    limit: int = 100,
    current_user: User = Depends(get_current_user)
) -> List[WorkbookSchema]:
    workbook_service = WorkbookService(db)
    workbooks = workbook_service.get_workbooks(skip=skip, limit=limit)
    return workbooks

# SECURITY: require a verified caller for this route.
@router.post('/workbooks')
def create_workbook(
    workbook: WorkbookSchema,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
) -> WorkbookSchema:
    workbook_service = WorkbookService(db)
    created_workbook = workbook_service.create_workbook(workbook)
    return created_workbook
