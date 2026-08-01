// Derive keys from worksheet/row/column render-site values because the persisted cells record does not
// match Grid's rows[].cells[] collection; nullish worksheet ids use 'ws'.
export function cellImageKey(
  worksheetId: string | undefined,
  rowIndex: number,
  colIndex: number,
): string {
  return `${worksheetId ?? 'ws'}:${rowIndex}:${colIndex}`;
}
