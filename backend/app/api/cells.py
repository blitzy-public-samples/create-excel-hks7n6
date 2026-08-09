from fastapi import APIRouter, Depends, HTTPException
from typing import List, Dict
import logging
from sqlalchemy.orm import Session
from backend.app.db import get_db
from backend.app.schema import CellSchema
from backend.app.services import CellService
from backend.app.core.security import get_current_user
from backend.app.db.models import User

router = APIRouter()

logger = logging.getLogger(__name__)

#: Answer for a cell update that failed for a reason the caller cannot act on. Fixed text:
#: the cause is recorded server-side instead, because the exception this replaces carried
#: driver messages, constraint names and file paths out to the caller.
UPDATE_FAILED_DETAIL = "The cell update could not be completed"

# CONTRACT, tracked as residual 23 and follow-up F26: ``worksheet_id`` arrives as the
# worksheet's NAME, not as its primary key. ``WorksheetSchema`` in
# backend/app/schema/workbook_schema.py declares no identifier field, so a name is the only
# identifier a response ever gives a client, while ``Worksheet.id`` in
# backend/app/db/models.py is an Integer primary key. An implementation of
# ``CellService.update_cells`` must therefore resolve the worksheet by ``(workbook_id, name)``
# and not by primary key. Both forms are accepted at the transport layer - the parameter is
# annotated ``str`` - so nothing here will surface the mistake.
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
    except HTTPException:
        # A refusal the service raised deliberately already carries the status and the wording
        # it chose. Re-raised unchanged so this handler cannot flatten a 404 into a 500.
        raise
    except Exception:
        # SECURITY: report the failure without its text - returning str(e) disclosed the
        # internal exception to the caller (CWE-209). The cause is recorded server-side only.
        logger.exception(
            "Cell update failed for workbook %s worksheet %s", workbook_id, worksheet_id
        )
        raise HTTPException(status_code=500, detail=UPDATE_FAILED_DETAIL)