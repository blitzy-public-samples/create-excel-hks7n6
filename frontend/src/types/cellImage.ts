// Alias type for the key that addresses one cell's ephemeral image, shaped '{worksheetId}:{rowIndex}:{colIndex}'
export type CellImageKey = string;

// Interface for a single image dropped onto a cell, held in memory only, timestamped in epoch milliseconds.
//
// pixelWidth, pixelHeight, decodedBytes and frameCount are the result of the pre-commit probe in
// cellImageValidation.ts. They are recorded on the entry for two reasons that are load-bearing rather
// than informational: the store's aggregate budget is accounted from decodedBytes, and an entry can
// only exist if those measurements were taken and passed, so their presence on the shape is what makes
// "nothing is rendered that was not measured first" checkable by reading the type. decodedBytes is the
// cost of ONE full-surface frame at the platform's bytes-per-pixel; animation cost is bounded
// separately by the frame ceiling, because a user agent's decoded-frame cache is not observable from
// script. None of these fields is persisted or transmitted: this shape is deliberately separate from
// the Cell/Worksheet/Workbook shapes in src/schema/workbookTypes.ts that cross the REST boundary.
export interface CellImageEntry {
  objectUrl: string;
  fileName: string;
  mimeType: string;
  sizeBytes: number;
  pixelWidth: number;
  pixelHeight: number;
  decodedBytes: number;
  frameCount: number;
  droppedAt: number;
}

// Alias type for the map of cell keys to their ephemeral image entries
export type CellImageMap = Record<CellImageKey, CellImageEntry>;

// Union type for the reasons a dropped file is rejected. The order below is the order the gates run in,
// which is also the order of increasing cost: metadata is free, container bytes cost a bounded read,
// a decode costs real memory, and the aggregate budget can only be judged once the cost is known.
//   unsupported-type     the MIME type is not on the raster-only allow-list (SVG is excluded by policy)
//   too-large            the encoded file exceeds the per-file byte ceiling
//   format-mismatch      the container bytes do not match the declared type, so the type was a lie
//   undecodable          the bytes are not a readable image of that container, or the decoder refused them
//   dimensions-too-large the declared or decoded surface exceeds a width, height, pixel or decoded-byte ceiling
//   too-many-frames      an animated container declares more frames than the ceiling allows
//   too-many-images      accepting this image would exceed the retained-image count budget
//   budget-exceeded      accepting this image would exceed the aggregate encoded or decoded byte budget
export type CellImageRejectionReason =
  | 'unsupported-type'
  | 'too-large'
  | 'format-mismatch'
  | 'undecodable'
  | 'dimensions-too-large'
  | 'too-many-frames'
  | 'too-many-images'
  | 'budget-exceeded';

// Interface for a rejected image drop, carrying the reason and a human-readable message
export interface CellImageRejection {
  key: CellImageKey;
  reason: CellImageRejectionReason;
  fileName: string;
  message: string;
}
