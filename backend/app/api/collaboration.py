from fastapi import APIRouter, Depends, HTTPException
from typing import List, Dict
from sqlalchemy.orm import Session
from backend.app.db import get_db
from backend.app.schema import CollaboratorSchema
from backend.app.services import CollaborationService

router = APIRouter()

@router.post('/workbooks/{workbook_id}/share')
def share_workbook(
    workbook_id: str,
    collaborators: List[CollaboratorSchema],
    db: Session = Depends(get_db)
) -> Dict[str, str]:
    # HUMAN ASSISTANCE NEEDED
    # The following code needs review and potential modifications:
    # 1. Error handling for invalid workbook_id or collaborators
    # 2. Proper authentication and authorization checks
    # 3. Validation of collaborator permissions
    # 4. Handling of edge cases (e.g., sharing with existing collaborators)
    
    try:
        collaboration_service = CollaborationService(db)
        result = collaboration_service.share_workbook(workbook_id, collaborators)
        return {"message": "Workbook shared successfully"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))