export type CellImageKey = string;

// Kept separate from persisted workbook schemas so blob URLs never enter REST or Firestore payloads;
// the store owns each object URL's lifecycle.
export interface CellImageEntry {
  objectUrl: string;
  fileName: string;
  mimeType: string;
  sizeBytes: number;
  droppedAt: number;
}

export type CellImageMap = Record<CellImageKey, CellImageEntry>;

// Machine-readable rejection categories; zero-byte files also use 'unsupported-type' with a specific
// user-facing detail.
export type CellImageRejectionReason = 'unsupported-type' | 'too-large';

export interface CellImageRejection {
  key: CellImageKey;
  reason: CellImageRejectionReason;
  fileName: string;
  message: string;
}
