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
  dropOutlineWidth: '2px',
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
