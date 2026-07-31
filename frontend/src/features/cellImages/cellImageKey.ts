// Ephemeral cell-image key derivation for the drag-and-drop image experiment.
//
// Dependency-free on purpose: zero imports, so this module can never contribute a
// module-resolution diagnostic, and it never reads a workbook, worksheet, or cell
// object. The repository's cell-addressing model is internally inconsistent:
// Worksheet declares 'cells: Record<string, Cell>' (schema/workbookTypes.ts:L27),
// the workbook reducer calls the array method 'cells.findIndex(...)' against that
// record (store/workbookSlice.ts:L22), and the grid iterates 'rows[].cells[]'
// instead (components/Grid.tsx:L62-L72). The key therefore derives only from the
// three primitive values that demonstrably exist at the render site. It mirrors
// the '${rowIndex}-${colIndex}' React key already used there, with colons because
// a worksheet id segment is carried alongside.
//
// 'worksheetId' tolerates undefined so the Grid call site needs no cast and no
// narrowing: 'activeWorksheet' is already implicitly 'any' there because its
// selector is unresolvable, and accepting 'string | undefined' therefore adds no
// strictNullChecks diagnostic. Nullish coalescing is used rather than '||' so a
// legitimately empty worksheet id is preserved instead of being replaced.
//
// Keys produced here address a map held in React context only: never in Redux,
// never in the Cell/Worksheet/Workbook shapes that cross the REST boundary, never
// in web storage. A page refresh therefore discards every entry, as intended.

// Returns the stable ephemeral cell-image key for one cell: '{worksheetId}:{rowIndex}:{colIndex}'.
export function cellImageKey(
  worksheetId: string | undefined,
  rowIndex: number,
  colIndex: number,
): string {
  return `${worksheetId ?? 'ws'}:${rowIndex}:${colIndex}`;
}
