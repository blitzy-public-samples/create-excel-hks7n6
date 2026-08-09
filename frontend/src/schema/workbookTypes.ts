// Two type families live here, and the separation is the point.
//
// 1. The API DTOs - CellSchema, WorksheetSchema, WorkbookSchema - mirror the frozen Pydantic
//    models in backend/app/schema/workbook_schema.py field for field. They are the wire
//    contract and nothing else may be added to them.
// 2. The domain models - Cell, Worksheet, Workbook, WorkbookState, User - are what the Redux
//    store holds. Their shape is unchanged.
//
// The mapping functions at the end of this file are the only crossing between the two.

// ===========================================================================
// API DTOs - exact mirrors of backend/app/schema/workbook_schema.py
// ===========================================================================
// CONTRACT: every field below matches that module's declaration. `Optional[X]` in Pydantic v1
// admits null, so it is `X | null` here; `Dict[str, str]` is `Record<string, string>`; and a
// `datetime` is serialised by FastAPI as an ISO 8601 string, so it is `string`.

/** Mirrors `CellSchema`: `value: str`, `formula: Optional[str]`, `style: Dict[str, str]`. */
export interface CellSchema {
  value: string;
  formula: string | null;
  style: Record<string, string>;
}

/**
 * Mirrors `WorksheetSchema`: `name: str`, `cells: Dict[str, CellSchema]`,
 * `named_ranges: Optional[Dict[str, str]]`.
 *
 * The frozen model carries no identifier field. `name` is therefore the only identifier a
 * response provides, and it is what `worksheetIdOf` addresses the cells route with.
 */
export interface WorksheetSchema {
  name: string;
  cells: Record<string, CellSchema>;
  named_ranges: Record<string, string> | null;
}

/**
 * Mirrors `WorkbookSchema`: `id: str`, `name: str`, `owner_id: str`,
 * `worksheets: List[WorksheetSchema]`, `created_at`/`modified_at` datetimes and
 * `settings: Optional[Dict[str, str]]`.
 */
export interface WorkbookSchema {
  id: string;
  name: string;
  owner_id: string;
  worksheets: WorksheetSchema[];
  created_at: string;
  modified_at: string;
  settings: Record<string, string> | null;
}

// ===========================================================================
// Domain models - the shape the Redux store holds
// ===========================================================================

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

/**
 * The signed-in user held in the store.
 *
 * The backend exposes no user DTO - `backend/app/schema/workbook_schema.py` declares none -
 * so this is a domain model, not a wire contract. The fields are the ones the Firebase
 * `User` supplies and the API's `owner_id` is resolved from.
 */
export interface User {
  uid: string;
  email: string | null;
  displayName: string | null;
}

// ===========================================================================
// DTO <-> domain mapping - the only crossing between the two families
// ===========================================================================

/** Default styling applied to a cell no style is supplied for. */
const DEFAULT_CELL_STYLE: CellStyle = {
  fontFamily: 'Calibri',
  fontSize: '11pt',
  fontWeight: 'normal',
  textAlign: 'left',
  color: '#000000',
  backgroundColor: '#ffffff',
};

/** Return a fresh copy of the default cell styling. */
export function defaultCellStyle(): CellStyle {
  return { ...DEFAULT_CELL_STYLE };
}

/**
 * Return the identifier the cells route addresses `worksheet` with.
 *
 * CONTRACT: `WorksheetSchema` carries no identifier field, so the worksheet's name is the
 * only one a response provides.
 */
export function worksheetIdOf(worksheet: WorksheetSchema): string {
  return worksheet.name;
}

/** Convert one DTO style map into the domain `CellStyle`, filling absent properties. */
function cellStyleFromSchema(style: Record<string, string>): CellStyle {
  return {
    fontFamily: style.fontFamily ?? DEFAULT_CELL_STYLE.fontFamily,
    fontSize: style.fontSize ?? DEFAULT_CELL_STYLE.fontSize,
    fontWeight: style.fontWeight ?? DEFAULT_CELL_STYLE.fontWeight,
    textAlign: style.textAlign ?? DEFAULT_CELL_STYLE.textAlign,
    color: style.color ?? DEFAULT_CELL_STYLE.color,
    backgroundColor: style.backgroundColor ?? DEFAULT_CELL_STYLE.backgroundColor,
  };
}

/** Convert one cell DTO into the domain `Cell`. */
export function cellFromSchema(cell: CellSchema): Cell {
  return {
    value: cell.value,
    formula: cell.formula ?? '',
    style: cellStyleFromSchema(cell.style ?? {}),
  };
}

/**
 * Convert one domain `Cell` into the DTO the cells route accepts.
 *
 * CONTRACT: the route takes `List[CellSchema]`, so every field the model declares is
 * populated. `value` is `str` on the wire, so a numeric or boolean domain value is rendered
 * as a string and a null one as the empty string. `style` is `Dict[str, str]`.
 */
export function cellToSchema(cell: Cell): CellSchema {
  return {
    value: cell.value === null ? '' : String(cell.value),
    formula: cell.formula === '' ? null : cell.formula,
    style: { ...cell.style },
  };
}

/** Convert one worksheet DTO into the domain `Worksheet`. */
export function worksheetFromSchema(worksheet: WorksheetSchema): Worksheet {
  const cells: Record<string, Cell> = {};
  Object.keys(worksheet.cells ?? {}).forEach((reference) => {
    cells[reference] = cellFromSchema(worksheet.cells[reference]);
  });
  return { id: worksheetIdOf(worksheet), name: worksheet.name, cells };
}

/**
 * Convert one workbook DTO into the domain `Workbook` the store holds.
 *
 * The domain model carries an `activeWorksheetId` the DTO does not, so the first worksheet
 * is selected, and the empty string when the workbook has none.
 */
export function workbookFromSchema(workbook: WorkbookSchema): Workbook {
  const worksheets = (workbook.worksheets ?? []).map(worksheetFromSchema);
  return {
    id: workbook.id,
    name: workbook.name,
    worksheets,
    activeWorksheetId: worksheets.length > 0 ? worksheets[0].id : '',
  };
}
