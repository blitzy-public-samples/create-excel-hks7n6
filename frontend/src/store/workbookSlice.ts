import { createSlice, PayloadAction } from '@reduxjs/toolkit';
import { WorkbookState, Workbook, Worksheet, Cell } from 'frontend/src/schema/workbookTypes';

const initialState: WorkbookState = {
  currentWorkbook: null,
};

const workbookSlice = createSlice({
  name: 'workbook',
  initialState,
  reducers: {
    setCurrentWorkbook: (state, action: PayloadAction<Workbook>) => {
      state.currentWorkbook = action.payload;
    },

    // HUMAN ASSISTANCE NEEDED
    // This reducer needs more complex logic for cell dependencies and recalculation
    updateCell: (state, action: PayloadAction<{worksheetId: string, cellId: string, updates: Partial<Cell>}>) => {
      if (state.currentWorkbook) {
        const worksheet = state.currentWorkbook.worksheets.find(ws => ws.id === action.payload.worksheetId);
        if (worksheet) {
          const cellIndex = worksheet.cells.findIndex(cell => cell.id === action.payload.cellId);
          if (cellIndex !== -1) {
            worksheet.cells[cellIndex] = { ...worksheet.cells[cellIndex], ...action.payload.updates };
            // TODO: Implement logic to trigger recalculation of dependent cells
          }
        }
      }
    },

    addWorksheet: (state, action: PayloadAction<Worksheet>) => {
      if (state.currentWorkbook) {
        state.currentWorkbook.worksheets.push(action.payload);
      }
    },
  },
});

export const { setCurrentWorkbook, updateCell, addWorksheet } = workbookSlice.actions;
export default workbookSlice.reducer;