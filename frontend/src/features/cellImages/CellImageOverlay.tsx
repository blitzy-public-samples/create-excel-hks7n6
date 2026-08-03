// The absolutely positioned overlay relies on Cell.tsx to establish a cell-local containing block,
// keeping the image out of row/column layout. URL ownership remains in the store.

import { useCallback, useRef, useState } from 'react';
import type { CSSProperties, KeyboardEvent, MouseEvent } from 'react';
import type { CellImageEntry } from '../../types/cellImage';
import { CELL_IMAGE_TOKENS } from './cellImageTokens';

interface CellImageOverlayProps {
  entry: CellImageEntry;
  onDismiss: () => void;
}

// A dragged payload can legitimately carry an empty name, which would otherwise reach
// assistive technology as an empty alternative text and an unnamed control. The wording
// matches the fallback the rejection notice uses, so the feature never calls the same
// nameless payload two different things.
const NEUTRAL_FILE_LABEL = 'the dropped file';

// Absolute positioning keeps the image out of grid layout; pointer pass-through
// preserves cell clicks, drag-depth accounting, and grid focus.
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

const imageStyle: CSSProperties = {
  maxWidth: '100%',
  maxHeight: '100%',
  objectFit: 'contain',
  display: 'block',
};

// A file's declared type is all a metadata gate can read, so bytes that do not decode are admitted
// and the browser draws its own broken-picture glyph with the whole file name beside it — inside a
// default-sized cell that is a wall of clipped text over the value underneath. Taking the element out
// of flow removes the glyph, the text and the element's place in the accessibility tree in one move,
// leaving the compact indicator below as the only thing announced or drawn.
const undisplayableImageStyle: CSSProperties = {
  ...imageStyle,
  display: 'none',
};

// Deliberately one element and no wrapper: the layer around it is the only unnamed container this
// overlay contributes, and the indicator carries an image role of its own so it replaces the picture
// in the accessibility tree rather than adding an anonymous box beside it.
//
// Anchored to a corner rather than centred, and to the corner opposite the removal control so the two
// never overlap. Centring it would have put an opaque disc exactly where the cell centres its own
// value, hiding the value behind the very marker that exists to say the picture is not being shown —
// a smaller repeat of the sprawl this replaces. A corner leaves the value legible, which is the whole
// point of not drawing the picture.
const undisplayableIndicatorStyle: CSSProperties = {
  position: 'absolute',
  insetBlockEnd: CELL_IMAGE_TOKENS.dismissButtonInset,
  insetInlineStart: CELL_IMAGE_TOKENS.dismissButtonInset,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  inlineSize: CELL_IMAGE_TOKENS.undisplayableIndicatorSize,
  blockSize: CELL_IMAGE_TOKENS.undisplayableIndicatorSize,
  borderRadius: '100%',
  borderWidth: CELL_IMAGE_TOKENS.dropOutlineWidth,
  borderStyle: 'solid',
  borderColor: CELL_IMAGE_TOKENS.dropRejectOutlineColor,
  color: CELL_IMAGE_TOKENS.dropRejectOutlineColor,
  backgroundColor: CELL_IMAGE_TOKENS.statusStripColor,
  fontSize: CELL_IMAGE_TOKENS.undisplayableGlyphSize,
  lineHeight: CELL_IMAGE_TOKENS.undisplayableGlyphSize,
  overflow: 'hidden',
};

// Every tab stop in this application is one the grid already declares, and this feature adds none of
// its own. Removing the focused control would otherwise drop focus onto the document body, stranding
// a keyboard user outside the grid with no arrow-key context and no way back except tabbing in from
// the top of the document. The selector is written against the rendered attribute, in lower case, and
// it excludes negative values so a node that was removed from the tab order is never chosen.
const FOCUSABLE_ANCESTOR_SELECTOR = '[tabindex]:not([tabindex^="-"])';

// Runs while the control is still mounted: once it unmounts there is no element left to walk up from
// and the browser has already moved focus to the body. Scrolling is suppressed because a dismissal
// should not move the viewport.
const restoreFocusToAncestor = (control: HTMLButtonElement | null): void => {
  if (control === null) {
    return;
  }
  const ancestor = control.closest(FOCUSABLE_ANCESTOR_SELECTOR);
  if (ancestor instanceof HTMLElement) {
    ancestor.focus({ preventScroll: true });
  }
};

// Only the dismiss button opts back into pointer events; token sizing and insets
// keep it inside the cell box.
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
  lineHeight: CELL_IMAGE_TOKENS.dismissButtonSize,
  overflow: 'hidden',
  cursor: 'pointer',
  pointerEvents: 'auto',
  // Replaced by the inset ring below, which the container's clip cannot eat.
  outlineStyle: 'none',
  transitionProperty: 'box-shadow',
  transitionDuration: CELL_IMAGE_TOKENS.transitionDuration,
};

// Build one inset box-shadow: focus takes precedence over hover/press, and the focus ring is listed
// first so it remains visible above the pressed fill.
const dismissButtonBoxShadow = (
  isFocused: boolean,
  isHovered: boolean,
  isPressed: boolean,
): string => {
  const layers: string[] = [];

  if (isFocused) {
    layers.push(
      `inset 0 0 0 ${CELL_IMAGE_TOKENS.dropOutlineWidth} ${CELL_IMAGE_TOKENS.statusStripColor}`,
    );
  } else if (isHovered || isPressed) {
    layers.push(
      `inset 0 0 0 ${CELL_IMAGE_TOKENS.dropOutlineWidth} ${CELL_IMAGE_TOKENS.dropRejectOutlineColor}`,
    );
  }

  if (isPressed) {
    // Spread equal to the control's own size fills it, so a press stays
    // distinguishable from a hover with or without a focus ring over it.
    layers.push(
      `inset 0 0 0 ${CELL_IMAGE_TOKENS.dismissButtonSize} ${CELL_IMAGE_TOKENS.dropActiveBackground}`,
    );
  }

  return layers.length > 0 ? layers.join(', ') : 'none';
};

export const CellImageOverlay = ({ entry, onDismiss }: CellImageOverlayProps) => {
  // Held in React because this feature adds no stylesheet and an inline style cannot
  // carry a pseudo-class. Presentation only: nothing here reads or writes a cell.
  const [isHovered, setIsHovered] = useState<boolean>(false);
  const [isPressed, setIsPressed] = useState<boolean>(false);
  const [isFocused, setIsFocused] = useState<boolean>(false);

  // The control has to be reachable at the moment it is being removed, which a ref gives without
  // adding a second source of truth for anything.
  const dismissControlRef = useRef<HTMLButtonElement | null>(null);

  // Recorded as the URL that failed rather than as a flag, so a replacement picture starts from a
  // clean slate without any explicit reset: a different URL simply stops matching.
  const [undisplayableUrl, setUndisplayableUrl] = useState<string | null>(null);
  const isUndisplayable = undisplayableUrl === entry.objectUrl;

  // The error event is the browser telling us synchronously that it has finished trying. Nothing is
  // read from the file and no decode is attempted here, so admission stays exactly as immediate as it
  // was.
  const handleImageError = useCallback((): void => {
    setUndisplayableUrl(entry.objectUrl);
  }, [entry.objectUrl]);

  const handlePointerEnter = useCallback((): void => {
    setIsHovered(true);
  }, []);

  // A pointer that leaves mid-press must clear both flags, or the control stays stuck
  // in its pressed treatment with no event left to release it.
  const handlePointerLeave = useCallback((): void => {
    setIsHovered(false);
    setIsPressed(false);
  }, []);

  const handlePressStart = useCallback((): void => {
    setIsPressed(true);
  }, []);

  const handlePressEnd = useCallback((): void => {
    setIsPressed(false);
  }, []);

  const handleFocus = useCallback((): void => {
    setIsFocused(true);
  }, []);

  const handleBlur = useCallback((): void => {
    setIsFocused(false);
    setIsPressed(false);
  }, []);

  // Enter and Space natively activate a button, so they get the same pressed
  // treatment a pointer press does. Neither default action is touched.
  const handleKeyDown = useCallback((event: KeyboardEvent<HTMLButtonElement>): void => {
    if (event.key === 'Enter' || event.key === ' ') {
      setIsPressed(true);
    }
  }, []);

  const handleKeyUp = useCallback((event: KeyboardEvent<HTMLButtonElement>): void => {
    if (event.key === 'Enter' || event.key === ' ') {
      setIsPressed(false);
    }
  }, []);

  // Stop the button click before it reaches the cell root, so dismissing an image does not open the
  // inline editor.
  const handleDismissClick = useCallback(
    (event: MouseEvent<HTMLButtonElement>) => {
      event.stopPropagation();
      // Both a pointer press and a keyboard activation arrive here, so one call covers both. It has
      // to precede the callback: the callback is what unmounts this control.
      restoreFocusToAncestor(dismissControlRef.current);
      onDismiss();
    },
    [onDismiss],
  );

  // One blank-name check feeds both exposures, so the alternative text and the control's
  // accessible name can never disagree. The control keeps the "Remove image <name>" form
  // whenever a real name exists and drops the redundant noun otherwise, which keeps the
  // announcement grammatical in both cases.
  const hasFileName = entry.fileName.length > 0;
  const fileLabel = hasFileName ? entry.fileName : NEUTRAL_FILE_LABEL;
  const dismissLabel = hasFileName ? `Remove image ${fileLabel}` : `Remove ${fileLabel}`;
  // Says what happened rather than naming an error, and reuses the same label so a nameless payload
  // reads as a phrase here too. The removal control keeps working, so the reading is actionable.
  const undisplayableLabel = `${fileLabel} could not be displayed`;

  return (
    <div style={containerStyle}>
      <img
        src={entry.objectUrl}
        alt={fileLabel}
        style={isUndisplayable ? undisplayableImageStyle : imageStyle}
        onError={handleImageError}
      />
      {isUndisplayable ? (
        // The glyph is decorative: an image role with a label of its own is what assistive
        // technology reads, so no extra node is needed to hide the character.
        <span role="img" aria-label={undisplayableLabel} style={undisplayableIndicatorStyle}>
          !
        </span>
      ) : null}
      {/* The native button remains in sequential focus order so image removal is keyboard-accessible;
          no explicit tab-order attribute is added. */}
      <button
        type="button"
        ref={dismissControlRef}
        style={{
          ...dismissButtonStyle,
          boxShadow: dismissButtonBoxShadow(isFocused, isHovered, isPressed),
        }}
        aria-label={dismissLabel}
        onClick={handleDismissClick}
        onMouseEnter={handlePointerEnter}
        onMouseLeave={handlePointerLeave}
        onMouseDown={handlePressStart}
        onMouseUp={handlePressEnd}
        onFocus={handleFocus}
        onBlur={handleBlur}
        onKeyDown={handleKeyDown}
        onKeyUp={handleKeyUp}
      >
        {/* Decorative glyph; the accessible name comes from the label above it. */}
        <span aria-hidden="true">×</span>
      </button>
    </div>
  );
};
