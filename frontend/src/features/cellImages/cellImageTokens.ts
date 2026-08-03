// Centralizes feature-local values because Tailwind is installed but not wired.
// Base colors use Tailwind 3 defaults for one-for-one substitution if Tailwind is
// wired later; the active tint derives from blue-500 at 8% alpha.

// Frozen so a component cannot mutate a shared design value; the const assertion
// keeps each member assignable to a React.CSSProperties longhand without a cast.
export const CELL_IMAGE_TOKENS = Object.freeze({
  dropActiveOutlineColor: '#3B82F6', // Tailwind 3 blue-500; closest match to the nominated Fluent accent
  dropActiveBackground: 'rgba(59,130,246,0.08)',
  dropRejectOutlineColor: '#EF4444',
  statusStripBackground: '#1F2937',
  statusStripColor: '#F9FAFB',
  // Geometry of the rejection notice. A notice pinned edge to edge along the viewport bottom lies
  // across whatever grid row happens to be there, and with no padding its text starts on the very
  // edge of the screen; both are avoided by insetting it, bounding how wide it may grow, and giving
  // it interior space. Values are Tailwind 3 defaults, restated in px like every token here.
  statusStripInset: '8px', // Tailwind 3 spacing-2 (0.5rem)
  statusStripPaddingBlock: '6px', // Tailwind 3 spacing-1.5 (0.375rem)
  statusStripPaddingInline: '10px', // Tailwind 3 spacing-2.5 (0.625rem)
  statusStripMaxInlineSize: '320px', // Tailwind 3 w-80 (20rem), narrow enough to leave the grid visible
  statusStripBorderRadius: '6px', // Tailwind 3 rounded-md (0.375rem)
  // Tailwind 3 shadow-md, written with rgba() like the tint above so every renderer parses it.
  // Lifts the notice off the grid it floats over now that it no longer spans the viewport.
  statusStripShadow: '0 4px 6px -1px rgba(0,0,0,0.1), 0 2px 4px -2px rgba(0,0,0,0.1)',
  dropOutlineWidth: '2px',
  dropOutlineStyle: 'dashed', // conventional drop-target affordance
  dismissButtonSize: '14px', // usable without dominating a default-sized cell
  dismissButtonInset: '1px', // keeps the control inside the cell's border box
  // Stand-in for a picture the browser could not decode. Sized to read at a glance in a
  // default-sized cell, and small enough that the corner it is anchored to leaves the cell's
  // own value legible beside it rather than hidden behind it.
  undisplayableIndicatorSize: '24px', // Tailwind 3 spacing-6
  undisplayableGlyphSize: '14px', // Tailwind 3 text-sm, the same step the dismiss control uses
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

// Declared-MIME allow-list intentionally excludes image/svg+xml: SVG can carry active content, and
// GHSA-rcg8-g69v-x23j documents an SVG-upload XSS. Render accepted files only through <img>; no SVG
// sanitizer is present.
export const ACCEPTED_IMAGE_MIME_TYPES = [
  'image/png',
  'image/jpeg',
  'image/gif',
  'image/webp',
  'image/bmp',
] as const;

// Bounds only retained encoded bytes reported by File.size, not the already-delivered File or decoded
// image memory. Validation runs before URL creation, so rejected files add no blob URL to this store.
export const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
