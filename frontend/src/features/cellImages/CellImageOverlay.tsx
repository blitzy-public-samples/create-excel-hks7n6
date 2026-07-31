// CellImageOverlay renders the ephemeral picture that was dropped onto one
// spreadsheet cell, together with the control that removes it again. It is the only
// place in the feature that renders an image, and it is purely presentational: it
// receives an already validated entry plus a dismiss callback from Cell.tsx, and
// owns no state, no resource and no side effect of its own.
//
// What this file exists to guarantee (Agent Action Plan sections 0.6.2.6 and 0.6.4):
//   R4, I9  Geometry containment. The picture is scaled and clipped inside the
//           existing cell box, so ROW HEIGHT AND COLUMN WIDTH NEVER CHANGE. That
//           invariant is the single most important visual property of this
//           experiment, because it is what reveals how much of a picture a
//           default-sized cell can actually show.
//   I8      Interaction non-interference. The container is transparent to pointer
//           input, so click-to-edit, the drag affordance and grid focus all behave
//           exactly as they did before the feature existed.
//   I10     Removal affordance. A real, labelled button, so the experiment can
//           clear a cell and drop a different picture into it.
//   I13     Accessibility. The alternative text is the dropped file's name, and the
//           removal control carries an accessible label naming that same file.
//   A1      The cell's value and formula are neither read nor mutated here. The
//           overlay only composites over the formatted value, which reappears
//           unchanged the moment the picture is dismissed.
//   A6      Pictures are cell-bound and non-interactive apart from removal: no
//           resizing, no moving, no anchoring, no dragging.
//
// Why the styling is inline and token-driven: the client ships no stylesheet at all
// (its entry point imports one that does not exist) and Tailwind is installed but
// unwired, so a utility class would resolve to nothing. Every colour, size and inset
// below therefore resolves through CELL_IMAGE_TOKENS, and the only literals are
// structural constants that carry no design decision. Inline styles cannot express
// hover or focus variants, so this component deliberately declares none: the drag
// affordance and its transition token belong to the cell root.
//
// Security posture: the picture is rendered through an img element and its src
// attribute, and through nothing else. No inline vector markup, no raw-markup
// injection sink, no embedded-document element and no CSS background built from user
// input appear here, because unsanitized vector images are XML documents that can
// carry active content - GitHub advisory GHSA-rcg8-g69v-x23j records cross-site
// scripting delivered in exactly that way. The upstream allow-list in
// cellImageTokens admits raster types only, and rendering through an img element is
// the second half of that posture.
//
// Resource lifecycle: this component neither mints nor releases the blob URL it
// displays. entry.objectUrl has to stay valid across re-renders, so the store owns
// the whole lifecycle and releases the URL on replacement, on clear, on clear-all
// and on unmount. The dismiss control simply invokes the callback it was handed,
// exactly once per activation, which is what keeps the one-release-per-mint
// invariant true.

import { useCallback } from 'react';
import type { CSSProperties, MouseEvent } from 'react';
import type { CellImageEntry } from '../../types/cellImage';
import { CELL_IMAGE_TOKENS } from './cellImageTokens';

// Declared locally and left unexported: types/cellImage.ts models the ephemeral
// state that crosses module boundaries, not one component's signature. Cell.tsx
// already gates the mount on an entry existing and the cell not being edited, so the
// overlay needs to know nothing about edit mode.
interface CellImageOverlayProps {
  // The validated in-memory picture to display; objectUrl is the blob URL the store minted.
  entry: CellImageEntry;
  // Invoked once per activation of the removal control; Cell.tsx wires it to clearCellImage.
  onDismiss: () => void;
}

// Covers the cell box without participating in layout, so no row and no column can
// be grown by a picture. Being out of flow and transparent to pointer input is what
// lets a click on the picture still reach the cell root, keeps the drag depth count
// free of spurious enter and leave pairs, and leaves keyboard focus with the grid.
// The annotation is load-bearing: without it the string literals widen to string and
// no longer satisfy the closed unions the style prop expects.
const containerStyle: CSSProperties = {
  position: 'absolute',
  top: 0,
  right: 0,
  bottom: 0,
  left: 0,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  overflow: 'hidden',
  zIndex: CELL_IMAGE_TOKENS.overlayZIndex,
  pointerEvents: 'none',
};

// The upper bounds cap the picture at the cell box and object-fit preserves its
// aspect ratio, so landscape and portrait pictures are both letterboxed rather than
// distorted; whatever still falls outside is clipped by the container. No fixed
// size, no lower bound and no ratio property appears anywhere in this file, which is
// what makes the never-grow-the-grid guarantee structural rather than incidental.
const imageStyle: CSSProperties = {
  maxWidth: '100%',
  maxHeight: '100%',
  objectFit: 'contain',
  display: 'block',
};

// The only interactive element in the overlay, and therefore the only one that opts
// back into pointer input. It is sized from the dismiss token and offset by the
// inset token, which keeps it inside the cell's border box, and it is placed with
// logical properties so it follows the writing direction. Zero padding and no border
// stop a user agent's default button chrome from inflating it past the token size,
// and the glyph is sized from that same token so it can never overflow the control.
//
// BLITZY [A11Y]: the dismiss token resolves to a 14-pixel square control, smaller
// than the 44-by-44 minimum touch target WCAG 2.1 AA recommends. The Agent Action
// Plan specifies that token deliberately - the control must not dominate a
// default-sized cell in an experiment about how pictures fit inside cells - so it is
// implemented exactly as specified and flagged here for designer review instead of
// being silently enlarged. It remains a natively focusable, keyboard-operable button
// with an accessible name, and its foreground and background tokens resolve to a
// contrast ratio far above the AA threshold.
const dismissButtonStyle: CSSProperties = {
  position: 'absolute',
  insetBlockStart: CELL_IMAGE_TOKENS.dismissButtonInset,
  insetInlineEnd: CELL_IMAGE_TOKENS.dismissButtonInset,
  inlineSize: CELL_IMAGE_TOKENS.dismissButtonSize,
  blockSize: CELL_IMAGE_TOKENS.dismissButtonSize,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  padding: 0,
  border: 'none',
  borderRadius: '100%',
  backgroundColor: CELL_IMAGE_TOKENS.statusStripBackground,
  color: CELL_IMAGE_TOKENS.statusStripColor,
  fontSize: CELL_IMAGE_TOKENS.dismissButtonSize,
  lineHeight: 1,
  overflow: 'hidden',
  cursor: 'pointer',
  pointerEvents: 'auto',
};

// Renders one cell's dropped picture plus the control that clears it again.
export const CellImageOverlay = ({ entry, onDismiss }: CellImageOverlayProps) => {
  // The activation is stopped from bubbling before the callback runs, so removing a
  // picture can never reach the cell root and open the inline editor. The callback
  // runs exactly once, which is what lets the store release exactly one blob URL.
  const handleDismissClick = useCallback(
    (event: MouseEvent<HTMLButtonElement>) => {
      event.stopPropagation();
      onDismiss();
    },
    [onDismiss],
  );

  return (
    <div style={containerStyle}>
      <img src={entry.objectUrl} alt={entry.fileName} style={imageStyle} />
      <button
        type="button"
        style={dismissButtonStyle}
        aria-label={`Remove image ${entry.fileName}`}
        onClick={handleDismissClick}
      >
        {/* Decorative glyph; the accessible name comes from the label above it. */}
        <span aria-hidden="true">×</span>
      </button>
    </div>
  );
};

// Both export forms are deliberate. Consumers inside this feature import the named
// binding, while every component in the existing components folder is imported as a
// default, and Cell.tsx's edit is authored separately. Offering both means either
// specifier resolves, so no consumer can gain a diagnostic from this file.
export default CellImageOverlay;
