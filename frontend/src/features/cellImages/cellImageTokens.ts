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
