// Alias type for the key that addresses one cell's ephemeral image, shaped '{worksheetId}:{rowIndex}:{colIndex}'
export type CellImageKey = string;

// Interface for a single image dropped onto a cell, held in memory only, timestamped in epoch milliseconds
export interface CellImageEntry {
  objectUrl: string;
  fileName: string;
  mimeType: string;
  sizeBytes: number;
  droppedAt: number;
}

// Alias type for the map of cell keys to their ephemeral image entries
export type CellImageMap = Record<CellImageKey, CellImageEntry>;

// Union type for the reasons a dropped file is rejected
export type CellImageRejectionReason = 'unsupported-type' | 'too-large';

// Interface for a rejected image drop, carrying the reason and a human-readable message
export interface CellImageRejection {
  key: CellImageKey;
  reason: CellImageRejectionReason;
  fileName: string;
  message: string;
}
