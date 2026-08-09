from fastapi import APIRouter, Depends, HTTPException, Query
from typing import List
from sqlalchemy.orm import Session
from backend.app.db import get_db
from backend.app.schema import WorkbookSchema
from backend.app.services import WorkbookService
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
@router.get('/workbooks')
def get_workbooks(
    db: Session = Depends(get_db),
    # SECURITY: bound the page window - an unbounded limit served the entire table in one
    # response, and a negative or above-int64 value reached SQL and was answered 500.
    skip: int = Query(DEFAULT_PAGE_OFFSET, ge=0, le=MAX_PAGE_OFFSET),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
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
