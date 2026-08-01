// Centralizes feature-local values because Tailwind is installed but not wired.
// Base colors use Tailwind 3 defaults for one-for-one substitution if Tailwind is
// wired later; the active tint derives from blue-500 at 8% alpha.

// Frozen so a component cannot mutate a shared design value; the const assertion
// keeps each member assignable to a React.CSSProperties longhand without a cast.
export const CELL_IMAGE_TOKENS = Object.freeze({
  dropActiveOutlineColor: '#3B82F6', // Tailwind 3 blue-500; closest match to the nominated Fluent accent
  dropActiveBackground: 'rgba(59,130,246,0.08)', // the same blue-500 primitive at low alpha
  dropRejectOutlineColor: '#EF4444', // Tailwind 3 red-500
  statusStripBackground: '#1F2937', // Tailwind 3 gray-800
  statusStripColor: '#F9FAFB', // Tailwind 3 gray-50
  dropOutlineWidth: '2px', // Tailwind border-2 equivalent
  dropOutlineStyle: 'dashed', // conventional drop-target affordance
  dismissButtonSize: '14px', // usable without dominating a default-sized cell
  dismissButtonInset: '1px', // keeps the control inside the cell's border box
  // Lowest stacking value that composites the overlay above the cell's value
  // text without entering a global stacking contest.
  overlayZIndex: 1,
  // Short transition keeps drag feedback immediate.
  transitionDuration: '120ms',
  // Lifetime of the rejection notice, consumed as a setTimeout delay: long
  // enough to read, short enough not to obstruct the experiment.
  rejectionNoticeMs: 2500,
} as const);

// Raster-only MIME allow-list. image/svg+xml is deliberately absent: unsanitized
// SVG is XML that can carry active content, and GHSA-rcg8-g69v-x23j documents XSS
// via uploaded SVG. An <img> element is safer than inline markup, but a raster-only
// allow-list avoids that attack surface without a sanitizer.
// Match File.type with .some(...); .includes(...) accepts only this readonly
// tuple's five literal types.
export const ACCEPTED_IMAGE_MIME_TYPES = [
  'image/png',
  'image/jpeg',
  'image/gif',
  'image/webp',
  'image/bmp',
] as const;

// Per-file 10 MiB ceiling: consumers must check File.size against it before
// creating an object URL, so the map cannot retain arbitrarily large dropped blobs.
export const MAX_IMAGE_BYTES = 10 * 1024 * 1024;

// RESOURCE POLICY. The constants below bound what the feature will retain and what it will decode.
// They live here, beside the allow-list and the per-file ceiling, because policy and design values are
// both "decisions that must not be written inline at a usage site": a limit buried in a component
// cannot be reviewed, and two limits written twice cannot be kept in agreement.
//
// Why a per-file byte ceiling alone is not sufficient, and each of these is therefore required:
//   * A compressed byte count says nothing about the surface a decoder will allocate. Image formats
//     store a declared canvas size, so a few dozen bytes can legitimately ask for tens of mebibytes of
//     RGBA memory. Bounding width, height, pixels and decoded bytes is what closes that gap.
//   * A per-image bound says nothing about the total. Every distinct cell key can retain another
//     accepted image for the document's lifetime, so the aggregate needs its own count and byte budgets.
// Both are enforced before an object URL is minted, so a refused payload allocates nothing.

// Largest surface this feature will accept, expressed three ways because a single measure is escapable:
// a wide-and-short image can satisfy a pixel budget while breaking a layout, and a square image can
// satisfy width and height ceilings while costing far more memory than either implies.
export const MAX_IMAGE_WIDTH = 4096;
export const MAX_IMAGE_HEIGHT = 4096;
// 4 megapixels. Generous for a picture displayed inside a spreadsheet cell, and small enough that the
// widely-demonstrated 4096 x 4096 expansion (16.7 megapixels) is refused before anything decodes it.
export const MAX_IMAGE_PIXELS = 4 * 1024 * 1024;

// Bytes a decoder is assumed to allocate per pixel: four channels at one byte each, which is what
// canvas-backed RGBA surfaces cost in every mainstream engine. Used to turn a pixel count into a
// memory figure, since decoded memory is the resource actually being budgeted.
export const DECODED_BYTES_PER_PIXEL = 4;

// 16 MiB of decoded surface per image, derived from the pixel ceiling so the two can never disagree.
export const MAX_DECODED_IMAGE_BYTES = MAX_IMAGE_PIXELS * DECODED_BYTES_PER_PIXEL;

// Frame ceiling for animated containers. An animation multiplies decode work by its frame count while
// its declared canvas size stays modest, so frames are bounded in their own right. Sixty-four comfortably
// covers a short looping animation without admitting a thousand-frame one.
export const MAX_IMAGE_FRAMES = 64;

// Aggregate budgets across every retained image. The count budget bounds the number of live blobs; the
// encoded budget bounds their compressed footprint; the decoded budget bounds the surface they can ask a
// decoder for. All three are measured against what is currently retained, with the entry being replaced
// credited back, so replacing an image never consumes budget twice.
export const MAX_RETAINED_IMAGES = 24;
export const MAX_TOTAL_IMAGE_BYTES = 32 * 1024 * 1024;
export const MAX_TOTAL_DECODED_BYTES = 64 * 1024 * 1024;

// How much of a file is read to verify its container signature and declared dimensions when the format
// cannot animate. 64 KiB reaches past the metadata blocks that precede a JPEG's frame header in
// practice, while keeping the transient read small; animated containers are read in full because a
// frame count cannot be known from a prefix.
export const IMAGE_HEADER_PROBE_BYTES = 64 * 1024;

// How long the decode probe waits for a user agent to report an image's natural size before treating
// the payload as undecodable. Only the fallback probe path can stall; the timeout exists so a file that
// never resolves cannot leave a drop pending, and so its temporary object URL is always released.
export const DECODE_PROBE_TIMEOUT_MS = 5000;
