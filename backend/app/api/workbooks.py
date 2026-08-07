from fastapi import APIRouter, Depends, HTTPException
from typing import List
from sqlalchemy.orm import Session
from backend.app.db import get_db
from backend.app.schema import WorkbookSchema
from backend.app.services import WorkbookService
from backend.app.core.security import get_current_user
from backend.app.db.models import User

router = APIRouter()

# SECURITY: enforce authentication — previously no route required a valid token
@router.get('/workbooks')
def get_workbooks(db: Session = Depends(get_db), skip: int = 0, limit: int = 100, current_user: User = Depends(get_current_user)) -> List[WorkbookSchema]:
    workbook_service = WorkbookService(db)
    workbooks = workbook_service.get_workbooks(skip=skip, limit=limit)
    return workbooks

# HUMAN ASSISTANCE NEEDED
# The following function has a confidence level below 0.8 and may need review
# SECURITY: enforce authentication — previously no route required a valid token
@router.post('/workbooks')
def create_workbook(workbook: WorkbookSchema, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)) -> WorkbookSchema:
    workbook_service = WorkbookService(db)
    created_workbook = workbook_service.create_workbook(workbook)
    return created_workbook

# Additional comments:
# - Error handling might need to be added to both functions
# - Workbook ownership/authorization checks should still be implemented
# - Input validation for the create_workbook function may be necessary
# - The WorkbookService methods (get_workbooks and create_workbook) need to be implemented in the corresponding service file