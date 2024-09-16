from fastapi import APIRouter, Depends, HTTPException
from typing import List, Dict
from sqlalchemy.orm import Session
from backend.app.db import get_db
from backend.app.schema import CellSchema
from backend.app.services import CellService

router = APIRouter()

# HUMAN ASSISTANCE NEEDED
# This function may need additional error handling and input validation
@router.put('/workbooks/{workbook_id}/worksheets/{worksheet_id}/cells')
def update_cells(workbook_id: str, worksheet_id: str, cells: List[CellSchema], db: Session = Depends(get_db)) -> Dict[str, str]:
    try:
        cell_service = CellService(db)
        updated_cells = cell_service.update_cells(workbook_id, worksheet_id, cells)
        return {"message": f"Successfully updated {len(updated_cells)} cells"}
    except Exception as e:
        # TODO: Implement proper error handling and logging
        raise HTTPException(status_code=500, detail=str(e))