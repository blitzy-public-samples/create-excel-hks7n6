from fastapi import APIRouter, Depends, HTTPException
from typing import List, Dict
from sqlalchemy.orm import Session
from backend.app.db import get_db
from backend.app.schema import CellSchema
from backend.app.services import CellService
from backend.app.core.security import get_current_user
from backend.app.db.models import User

router = APIRouter()

# SECURITY: require a verified caller for this route.
@router.put('/workbooks/{workbook_id}/worksheets/{worksheet_id}/cells')
def update_cells(
    workbook_id: str,
    worksheet_id: str,
    cells: List[CellSchema],
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
) -> Dict[str, str]:
    try:
        cell_service = CellService(db)
        updated_cells = cell_service.update_cells(workbook_id, worksheet_id, cells)
        return {"message": f"Successfully updated {len(updated_cells)} cells"}
    except Exception as e:
        # KNOWN OPEN DEFECT, tracked as follow-up F7: the line below returns the internal
        # exception text to the caller (CWE-209 information exposure). It is unchanged from the
        # pre-remediation baseline and is NOT fixed here - the handler body is route business
        # logic, which this change set is permitted to edit only to add the authentication
        # dependency above. The marker stays so the defect is visible at the site.
        raise HTTPException(status_code=500, detail=str(e))