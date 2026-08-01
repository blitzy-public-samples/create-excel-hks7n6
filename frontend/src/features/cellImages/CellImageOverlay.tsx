// The absolutely positioned overlay relies on Cell.tsx to establish a cell-local containing block,
// keeping the image out of row/column layout. URL ownership remains in the store.

import { useCallback, useState } from 'react';
import type { CSSProperties, KeyboardEvent, MouseEvent } from 'react';
import type { CellImageEntry } from '../../types/cellImage';
import { CELL_IMAGE_TOKENS } from './cellImageTokens';

interface CellImageOverlayProps {
  entry: CellImageEntry;
  onDismiss: () => void;
}

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
      onDismiss();
    },
    [onDismiss],
  );

  return (
    <div style={containerStyle}>
      <img src={entry.objectUrl} alt={entry.fileName} style={imageStyle} />
      {/* The native button remains in sequential focus order so image removal is keyboard-accessible;
          no explicit tab-order attribute is added. */}
      <button
        type="button"
        style={{
          ...dismissButtonStyle,
          boxShadow: dismissButtonBoxShadow(isFocused, isHovered, isPressed),
        }}
        aria-label={`Remove image ${entry.fileName}`}
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

// Keep both named and default exports for consumer import compatibility.
export default CellImageOverlay;
