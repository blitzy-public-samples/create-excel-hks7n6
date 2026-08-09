from fastapi import APIRouter, Depends, HTTPException
from typing import List, Dict
import logging
from sqlalchemy.orm import Session
from backend.app.db import get_db
from backend.app.schema import CollaboratorSchema
from backend.app.services import CollaborationService
from backend.app.core.security import get_current_user
from backend.app.db.models import User

router = APIRouter()

logger = logging.getLogger(__name__)

#: Answer for a share that failed for a reason the caller cannot act on. Fixed text: the
#: cause is recorded server-side instead, because the exception this replaces carried driver
#: messages, constraint names and file paths out to the caller.
SHARE_FAILED_DETAIL = "The workbook could not be shared"

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
    except HTTPException:
        # A refusal the service raised deliberately already carries the status and the wording
        # it chose. Re-raised unchanged so this handler cannot flatten a 404 into a 400.
        raise
    except Exception:
        # SECURITY: report the failure without its text - returning str(e) disclosed the
        # internal exception to the caller (CWE-209). The cause is recorded server-side only.
        logger.exception("Sharing failed for workbook %s", workbook_id)
        raise HTTPException(status_code=400, detail=SHARE_FAILED_DETAIL)