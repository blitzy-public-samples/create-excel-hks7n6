import { createSlice, PayloadAction } from '@reduxjs/toolkit';

// Union type for possible cell values
export type CellValue = string | number | boolean | null;

// Interface for cell styling properties
export interface CellStyle {
  fontFamily: string;
  fontSize: string;
  fontWeight: string;
  textAlign: string;
  color: string;
  backgroundColor: string;
}

// Interface for a single cell in a worksheet
export interface Cell {
  value: CellValue;
  formula: string;
  style: CellStyle;
}

// Interface for a worksheet in a workbook
export interface Worksheet {
  id: string;
  name: string;
  cells: Record<string, Cell>;
}

// Interface for an Excel workbook
export interface Workbook {
  id: string;
  name: string;
  worksheets: Worksheet[];
  activeWorksheetId: string;
}

// Interface for the workbook state in Redux store
export interface WorkbookState {
  currentWorkbook: Workbook | null;
  isLoading: boolean;
  error: string | null;
}