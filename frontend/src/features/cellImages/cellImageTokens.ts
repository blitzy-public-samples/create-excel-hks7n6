// Feature-local design tokens and validation constants for the ephemeral
// cell-image drop experiment.
//
// This module is the single source of every design value the cellImages feature
// renders, and the single source of the two constants that gate what may be
// ingested. It exists because the client ships no stylesheet whatsoever: there
// is no .css file in the repository, no tailwind.config.js and no PostCSS entry,
// so the installed Tailwind resolves nothing and utility strings would be inert.
// Introducing a global stylesheet would restyle every existing component, which
// the experiment's brief forbids, so the values below are consumed exclusively
// through inline style objects on the feature's own elements.
//
// Every colour is a Tailwind 3 default palette value, so a future decision to
// actually wire tailwind.config.js substitutes each one for its named palette
// entry one-for-one rather than re-deriving a palette. The hues are also
// compatible with the Fluent Design System that the project's specifications
// nominate as their visual language.
//
// Because every value lives here, the feature's guarantee that its JSX contains
// no hardcoded design literals is verifiable by reading this one file. A
// consumer may inline only structural constants that carry no design decision
// (0, none, auto, inherit, currentColor, transparent, 100%, hidden, contain,
// block, absolute, relative). If a consumer appears to need a thirteenth
// presentation value, it is one of those structural constants, not a new token.
//
// Consumed by cellImageStore.tsx, useCellImageDrop.ts, CellImageOverlay.tsx and
// the additive drag affordance in components/Cell.tsx, all through the relative
// specifier './cellImageTokens' (or '../features/cellImages/cellImageTokens').

// Frozen presentation values for the cell-image drop affordance and overlay.
// The object is frozen deliberately: an accidental write from a component then
// fails loudly at runtime instead of silently restyling the whole feature. The
// trailing literal types are preserved so a consumer can assign these members
// straight into a React.CSSProperties object with no cast, which matters because
// several CSS longhand properties are typed as closed literal unions.
export const CELL_IMAGE_TOKENS = Object.freeze({
  // Affordance shown on the hovered cell while an acceptable payload is over it.
  dropActiveOutlineColor: '#3B82F6', // Tailwind 3 blue-500; closest match to the nominated Fluent accent
  dropActiveBackground: 'rgba(59,130,246,0.08)', // the same blue-500 primitive at low alpha
  // Affordance shown when a payload is refused for its type or its size.
  dropRejectOutlineColor: '#EF4444', // Tailwind 3 red-500
  // Surface of the single application-level rejection notice. The pairing below
  // measures 14.05:1 in Chrome, well above the WCAG 2.1 AA minimum of 4.5:1 for
  // normal text, so the notice needs no contrast exception.
  statusStripBackground: '#1F2937', // Tailwind 3 gray-800
  statusStripColor: '#F9FAFB', // Tailwind 3 gray-50
  // Outline geometry shared by both affordance states.
  dropOutlineWidth: '2px', // Tailwind border-2 equivalent
  dropOutlineStyle: 'dashed', // conventional drop-target affordance
  // Control that clears an image from its cell.
  // BLITZY [A11Y]: the design source (AAP 0.5.3) fixes this control at 14px so it
  // stays usable without dominating a default-sized cell, which yields a 14x14
  // target. That sits below WCAG 2.2 AA 2.5.8 Target Size (Minimum) of 24x24 and
  // below WCAG 2.1 AAA 2.5.5 of 44x44. The project's stated goal is WCAG 2.1 AA,
  // which carries no target-size criterion, so this is not a conformance failure
  // today. The specified value is implemented exactly and raised here for designer
  // review rather than silently enlarged. Measured in Chrome at 14.00x14.00 CSS
  // pixels on a real <button type="button">, keyboard-reachable with a visible
  // focus ring, and unchanged when the root font size is doubled because the value
  // is px rather than rem, so the target does not grow under text scaling either.
  dismissButtonSize: '14px', // usable without dominating a default-sized cell
  dismissButtonInset: '1px', // keeps the control inside the cell's border box
  // Lowest stacking value that composites the overlay above the cell's value
  // text without entering a global stacking contest.
  overlayZIndex: 1,
  // Affordance transition, short enough to read as immediate during a drag. Note
  // for consumers: these values are applied as inline styles, which a CSS media
  // rule cannot override, so honouring prefers-reduced-motion means reading
  // window.matchMedia('(prefers-reduced-motion: reduce)') where the style object
  // is built and substituting '0s' for the duration below.
  transitionDuration: '120ms',
  // Lifetime of the rejection notice, consumed as a setTimeout delay: long
  // enough to read, short enough not to obstruct the experiment.
  rejectionNoticeMs: 2500,
} as const);

// Raster-only MIME allow-list. image/svg+xml is deliberately absent.
//
// SVG is an XML document parsed by the same engine as HTML, so it can carry
// script elements, event-handler attributes, embedded HTML and references to
// external resources. GitHub Security Advisory GHSA-rcg8-g69v-x23j against
// makeplane/plane, where an SVG profile-image upload yielded cross-site
// scripting, is concrete precedent that the risk is real on image-preview
// surfaces. Rendering through an <img> element is materially safer than inlining
// untrusted SVG markup, but the recommended posture on a preview surface with no
// sanitizer is not to accept SVG at all, so it is omitted below at no cost to
// the visual assessment this experiment performs.
//
// A consumer membership-tests with ACCEPTED_IMAGE_MIME_TYPES.some(...) compared
// against File.type. Passing File.type to .includes(...) does not typecheck,
// because this tuple's element type is the union of its five literals while
// File.type is a plain string. Immutability here is compile-time only: the tuple
// is readonly and fixed-length to the compiler, while the runtime freeze is
// reserved for the token object above, which is the value a component could
// otherwise write through.
export const ACCEPTED_IMAGE_MIME_TYPES = [
  'image/png',
  'image/jpeg',
  'image/gif',
  'image/webp',
  'image/bmp',
] as const;

// Per-file byte ceiling of 10 MiB, checked against File.size before an object
// URL is minted: a blob of unbounded size retained in the ephemeral map for the
// lifetime of the page is a client-side memory-exhaustion vector, and validating
// first keeps a refused drop from allocating at all.
export const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
