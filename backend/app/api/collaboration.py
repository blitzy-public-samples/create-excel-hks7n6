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
        # KNOWN OPEN DEFECT, tracked as follow-up F7: the line below returns the internal
        # exception text to the caller (CWE-209 information exposure). It is unchanged from the
        # pre-remediation baseline and is NOT fixed here - the handler body is route business
        # logic, which this change set is permitted to edit only to add the authentication
        # dependency above. The marker stays so the defect is visible at the site.
        raise HTTPException(status_code=400, detail=str(e))