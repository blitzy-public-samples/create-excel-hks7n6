from fastapi import APIRouter, Depends, HTTPException
from typing import List, Dict
from sqlalchemy.orm import Session
from backend.app.db import get_db
from backend.app.schema import CollaboratorSchema
from backend.app.services import CollaborationService
from backend.app.core.security import get_current_user
from backend.app.db.models import User

router = APIRouter()

# SECURITY: require a verified caller for this route.
@router.post('/workbooks/{workbook_id}/share')
def share_workbook(
    workbook_id: str,
    collaborators: List[CollaboratorSchema],
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
) -> Dict[str, str]:
    try:
        collaboration_service = CollaborationService(db)
        result = collaboration_service.share_workbook(workbook_id, collaborators)
        return {"message": "Workbook shared successfully"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))