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
  // Duration of every affordance transition this feature draws: the cell's
  // drag-active and rejection outlines, and the dismiss control's own ring.
  // Short enough to feel immediate while a drag is in progress.
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

// Per-file 10 MiB ceiling on the ENCODED length reported by File.size, which is the
// only cost this prototype claims to bound. It is checked before an object URL is
// created, so a refused payload allocates nothing and the map cannot retain
// arbitrarily large dropped blobs. Decoded surface is deliberately not modelled:
// script cannot observe a user agent's decoded-frame cache, so any figure derived
// from a declared canvas would be an estimate presented as a bound.
export const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
