from fastapi import APIRouter, Depends, HTTPException
from typing import List, Dict
from sqlalchemy.orm import Session
from backend.app.db import get_db
from backend.app.schema import CollaboratorSchema
from backend.app.services import CollaborationService
from backend.app.core.security import get_current_user
from backend.app.db.models import User

router = APIRouter()

# SECURITY: enforce authentication — previously no route required a valid token
@router.post('/workbooks/{workbook_id}/share')
def share_workbook(
    workbook_id: str,
    collaborators: List[CollaboratorSchema],
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
) -> Dict[str, str]:
    # HUMAN ASSISTANCE NEEDED
    # The following code needs review and potential modifications:
    # 1. Error handling for invalid workbook_id or collaborators
    # 2. Add workbook authorization checks
    # 3. Validation of collaborator permissions
    # 4. Handling of edge cases (e.g., sharing with existing collaborators)
    
    try:
        collaboration_service = CollaborationService(db)
        result = collaboration_service.share_workbook(workbook_id, collaborators)
        return {"message": "Workbook shared successfully"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))