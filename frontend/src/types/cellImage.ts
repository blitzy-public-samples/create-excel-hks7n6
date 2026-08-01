// Alias type for the key that addresses one cell's ephemeral image, shaped '{worksheetId}:{rowIndex}:{colIndex}'
export type CellImageKey = string;

// Interface for a single image dropped onto a cell, held in memory only, timestamped in epoch milliseconds.
//
// This shape is deliberately separate from the Cell/Worksheet/Workbook shapes in
// src/schema/workbookTypes.ts that cross the REST boundary and feed the Firestore sync path: an image
// field on those would silently change a persisted contract. Nothing here is persisted or transmitted.
// objectUrl is a blob: URL owned by the store, which is the sole creator and releaser of it; fileName
// doubles as the image's alternative text; mimeType and sizeBytes are the two values the store's
// validation gates are judged against and are recorded so an accepted entry carries the evidence of
// what was accepted.
export interface CellImageEntry {
  objectUrl: string;
  fileName: string;
  mimeType: string;
  sizeBytes: number;
  droppedAt: number;
}

// Alias type for the map of cell keys to their ephemeral image entries
export type CellImageMap = Record<CellImageKey, CellImageEntry>;

// Union type for the reasons a dropped file is rejected. The order below is the order the gates run in:
// the declared type is checked before the encoded length, because the cheaper gate goes first and a
// payload that is not an accepted image is refused whatever its size.
//   unsupported-type  the MIME type is not on the raster-only allow-list (SVG is excluded by policy)
//   too-large         the file exceeds the per-file byte ceiling
export type CellImageRejectionReason = 'unsupported-type' | 'too-large';

// Interface for a rejected image drop, carrying the reason and a human-readable message
export interface CellImageRejection {
  key: CellImageKey;
  reason: CellImageRejectionReason;
  fileName: string;
  message: string;
}
