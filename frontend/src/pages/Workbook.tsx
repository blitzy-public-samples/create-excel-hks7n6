import React, { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { Grid, Ribbon, FormulaBar, Sidebar } from '@/components';
import { fetchWorkbooks, updateCell as putCell } from '@/services/api';
import { useAppSelector, useAppDispatch } from '@/store';
import type { Cell, Workbook as WorkbookModel, WorkbookState } from '@/schema/workbookTypes';
import { cellToSchema, defaultCellStyle, workbookFromSchema } from '@/schema/workbookTypes';
import { setCurrentWorkbook, updateCell as updateCellInStore } from '@/store/workbookSlice';

// HUMAN ASSISTANCE NEEDED
// The confidence level is below 0.8, indicating that this component might need additional review or improvements for production readiness.

const Workbook: React.FC = () => {
  const { id } = useParams<{ id: string }>();
  const dispatch = useAppDispatch();
  // The selector names the slice it reads, so the workbook it returns is typed here.
  const currentWorkbook: WorkbookModel | null = useAppSelector(
    (state: { workbook: WorkbookState }) => state.workbook.currentWorkbook
  );
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // CONTRACT: the API exposes no workbook-by-id route. The collection route is the
    // supported way to reach one workbook, so it is read and this workbook selected from it.
    const loadWorkbook = async () => {
      try {
        setLoading(true);
        const workbooks = await fetchWorkbooks();
        const workbookData = workbooks.find((workbook) => workbook.id === id);
        if (workbookData === undefined) {
          setError('Workbook not found');
        } else {
          dispatch(setCurrentWorkbook(workbookFromSchema(workbookData)));
        }
        setLoading(false);
      } catch (err) {
        setError('Failed to load workbook');
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
    } catch (err) {
      setError('Failed to update cell');
    }
  };

  if (loading) {
    return <div>Loading...</div>;
  }

  if (error) {
    return <div>Error: {error}</div>;
  }

  return (
    <div className="workbook-container">
      <Ribbon />
      <FormulaBar />
      <div className="workbook-content">
        <Grid workbook={currentWorkbook} onCellUpdate={handleCellUpdate} />
        <Sidebar />
      </div>
    </div>
  );
};

export default Workbook;