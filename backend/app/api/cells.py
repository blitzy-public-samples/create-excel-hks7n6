from fastapi import APIRouter, Depends, HTTPException
from typing import List, Dict
from sqlalchemy.orm import Session
from backend.app.db import get_db
from backend.app.schema import CellSchema
from backend.app.services import CellService
from backend.app.core.security import get_current_user
from backend.app.db.models import User

router = APIRouter()

# HUMAN ASSISTANCE NEEDED
# This function may need additional error handling and input validation
# SECURITY: enforce authentication — previously no route required a valid token
@router.put('/workbooks/{workbook_id}/worksheets/{worksheet_id}/cells')
def update_cells(workbook_id: str, worksheet_id: str, cells: List[CellSchema], db: Session = Depends(get_db), current_user: User = Depends(get_current_user)) -> Dict[str, str]:
    try:
        cell_service = CellService(db)
        updated_cells = cell_service.update_cells(workbook_id, worksheet_id, cells)
        return {"message": f"Successfully updated {len(updated_cells)} cells"}
    except Exception as e:
        # TODO: Implement proper error handling and logging
        raise HTTPException(status_code=500, detail=str(e))