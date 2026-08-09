import React, { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { Grid, Ribbon, FormulaBar, Sidebar } from '@/components';
import { apiFailure, apiFailureMessage, fetchWorkbooks, updateCell as putCell } from '@/services/api';
import { useAppSelector, useAppDispatch } from '@/store';
import type { Cell, Workbook as WorkbookModel, WorkbookState } from '@/schema/workbookTypes';
import { cellToSchema, defaultCellStyle, workbookFromSchema } from '@/schema/workbookTypes';
import { clearUser } from '@/store/userSlice';
import { setCurrentWorkbook, updateCell as updateCellInStore } from '@/store/workbookSlice';

// HUMAN ASSISTANCE NEEDED
// The confidence level is below 0.8, indicating that this component might need additional review or improvements for production readiness.

// The page size the collection route serves when no limit is sent: `get_workbooks` in
// backend/app/api/workbooks.py declares `limit: int = 100`. Kept as a named value because the
// message below distinguishes "absent from this page" from "does not exist" by comparing
// against it.
const WORKBOOK_PAGE_SIZE = 100;

const Workbook: React.FC = () => {
  const { id } = useParams<{ id: string }>();
  const dispatch = useAppDispatch();
  // The selector names the slice it reads, so the workbook it returns is typed here.
  const currentWorkbook: WorkbookModel | null = useAppSelector(
    (state: { workbook: WorkbookState }) => state.workbook.currentWorkbook
  );
  const [loading, setLoading] = useState(true);
  // Two failure states, because they cost different things to show. `error` means there is no
  // workbook to render, so it replaces the view; `saveError` means one write did not land, and
  // must NOT take the grid away from the person who was editing it.
  const [error, setError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);

  useEffect(() => {
    // CONTRACT: the API exposes no workbook-by-id route. The collection route is the
    // supported way to reach one workbook, so it is read and this workbook selected from it.
    const loadWorkbook = async () => {
      try {
        setLoading(true);
        const workbooks = await fetchWorkbooks();
        const workbookData = workbooks.find((workbook) => workbook.id === id);
        if (workbookData === undefined) {
          // CONTRACT: the collection route serves ONE page - the backend's default limit is 100
          // and this caller sends no skip - so a workbook missing from a full page has not been
          // shown not to exist. Reporting both cases as "not found" told a user their workbook
          // was gone when it was merely past the end of the page.
          setError(
            workbooks.length >= WORKBOOK_PAGE_SIZE
              ? 'This workbook was not in the first ' +
                  `${WORKBOOK_PAGE_SIZE} of your workbooks, so it could not be opened from here. ` +
                  'It may still exist.'
              : 'Workbook not found'
          );
        } else {
          dispatch(setCurrentWorkbook(workbookFromSchema(workbookData)));
        }
        setLoading(false);
      } catch (err) {
        // SECURITY: a credential the API refused stops counting as a signed-in session here too.
        if (apiFailure(err)?.reauthenticate === true) {
          dispatch(clearUser());
        }
        setError(apiFailureMessage(err, 'Failed to load workbook'));
        setLoading(false);
      }
    };

    loadWorkbook();
  }, [id, dispatch]);

  // CONTRACT: the cells route is addressed by workbook AND worksheet, and its body is a list
  // of complete cells, so the active worksheet's id and a whole cell are what it receives.
  const handleCellUpdate = async (cellId: string, value: string) => {
    if (currentWorkbook === null) {
      return;
    }
    const worksheetId = currentWorkbook.activeWorksheetId;
    const worksheet = currentWorkbook.worksheets.find((sheet) => sheet.id === worksheetId);
    const existing = worksheet?.cells[cellId];
    const updates: Cell = {
      value,
      formula: existing?.formula ?? '',
      style: existing?.style ?? defaultCellStyle(),
    };
    try {
      await putCell(currentWorkbook.id, worksheetId, cellToSchema(updates));
      dispatch(updateCellInStore({ worksheetId, cellId, updates }));
      setSaveError(null);
    } catch (err) {
      // SECURITY: a credential the API refused stops counting as a signed-in session here too.
      if (apiFailure(err)?.reauthenticate === true) {
        dispatch(clearUser());
      }
      // CONTRACT: a failed write is reported WITHOUT unmounting the grid, and names the cell it
      // concerns - the store was not updated, so the edit is not saved and the person editing
      // has to know which one. The message is api.ts's status-derived classification, which for
      // a 429 already carries the Retry-After the server sent.
      setSaveError(
        `${cellId}: ${apiFailureMessage(err, 'This change could not be saved.')}`
      );
    }
  };

  if (loading) {
    return <div>Loading...</div>;
  }

  // Blocking, because a load failure leaves no workbook to render - unlike a failed write,
  // which is reported beside the grid below.
  if (error) {
    return (
      <div className="workbook-load-error" role="alert">
        Error: {error}
      </div>
    );
  }

  return (
    <div className="workbook-container">
      <Ribbon />
      <FormulaBar />
      {saveError === null ? null : (
        <div className="workbook-save-error" role="alert">
          <span>{saveError}</span>
          <button type="button" onClick={() => setSaveError(null)}>
            Dismiss
          </button>
        </div>
      )}
      <div className="workbook-content">
        <Grid workbook={currentWorkbook} onCellUpdate={handleCellUpdate} />
        <Sidebar />
      </div>
    </div>
  );
};

export default Workbook;