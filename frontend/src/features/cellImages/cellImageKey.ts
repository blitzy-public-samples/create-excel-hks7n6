// Derive keys from render-site primitives because the persisted cells record
// diverges from Grid's rows[].cells[] model.
// Accept undefined to keep callers cast-free; ?? preserves an intentional empty
// id and falls back to ws only for nullish input.

// Returns the stable ephemeral cell-image key for one cell: '{worksheetId}:{rowIndex}:{colIndex}'.
export function cellImageKey(
  worksheetId: string | undefined,
  rowIndex: number,
  colIndex: number,
): string {
  return `${worksheetId ?? 'ws'}:${rowIndex}:${colIndex}`;
}
