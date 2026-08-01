import React, { useState, useEffect } from 'react';
import { useAppSelector, useAppDispatch } from '@/store';
import { updateCell } from '@/store/workbookSlice';
import { formatCellValue } from '@/utils/cellFormatting';
import { useCellImages } from '../features/cellImages/cellImageStore';
import { useCellImageDrop } from '../features/cellImages/useCellImageDrop';
import { CellImageOverlay } from '../features/cellImages/CellImageOverlay';
import { CELL_IMAGE_TOKENS } from '../features/cellImages/cellImageTokens';

// HUMAN ASSISTANCE NEEDED
// The following component may need additional refinement for production readiness.
// Please review and enhance the implementation, particularly for edge cases and performance optimization.

interface CellProps {
  id: string;
  value: any;
  style: React.CSSProperties;
  imageKey?: string;
}

// The overlay pins itself to all four edges, so the cell has to be its positioned
// containing block for exactly as long as a picture is mounted; without one those
// edges resolve against a higher ancestor and the picture leaves its cell. The
// declaration is structural only: it offsets nothing and grows no row or column.
const cellImageContainingBlock: React.CSSProperties = { position: 'relative' };

// The token-defined motion for the drop affordance, which is the purpose that token
// exists for. Declaring it in the same style change that brings an affordance in is
// exactly when a transition is honoured — CSS resolves transition-property from the
// after-change style — so the outline grows and the tint fades in over the token
// duration instead of snapping. It is deliberately absent from the idle path: an idle
// cell must forward the caller's own style object by identity, so the affordance
// animates in and then clears at once when the drag leaves. Property names are
// structural, not design decisions; the only value here is the token.
const affordanceTransition: React.CSSProperties = {
  transitionProperty: 'outline-color, outline-width, background-color',
  transitionDuration: CELL_IMAGE_TOKENS.transitionDuration,
};

// CSSProperties preserves the token's literal outline style without a cast.
const dragActiveOutline: React.CSSProperties = {
  ...affordanceTransition,
  outlineWidth: CELL_IMAGE_TOKENS.dropOutlineWidth,
  outlineStyle: CELL_IMAGE_TOKENS.dropOutlineStyle,
  outlineColor: CELL_IMAGE_TOKENS.dropActiveOutlineColor,
  backgroundColor: CELL_IMAGE_TOKENS.dropActiveBackground,
};

const dragRejectOutline: React.CSSProperties = {
  ...affordanceTransition,
  outlineWidth: CELL_IMAGE_TOKENS.dropOutlineWidth,
  outlineStyle: CELL_IMAGE_TOKENS.dropOutlineStyle,
  outlineColor: CELL_IMAGE_TOKENS.dropRejectOutlineColor,
};

const Cell: React.FC<CellProps> = ({ id, value, style, imageKey }) => {
  const dispatch = useAppDispatch();
  const [isEditing, setIsEditing] = useState(false);
  const [editValue, setEditValue] = useState('');
  // Subscribed to this one cell's key, so a picture dropped on another cell — or a
  // refusal reported for one — cannot re-render this cell. A grid renders a cell per
  // visible position, so that scoping is what keeps a drop O(1) rather than O(cells).
  const { image: cellImage, clearCellImage } = useCellImages(imageKey);
  const { dragHandlers, isDragActive, isRejecting } = useCellImageDrop(imageKey);

  const formattedValue = formatCellValue(value);

  const handleCellClick = () => {
    setIsEditing(true);
    setEditValue(String(value));
  };

  const handleBlur = () => {
    setIsEditing(false);
    if (editValue !== String(value)) {
      dispatch(updateCell({ id, value: editValue }));
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      handleBlur();
    }
  };

  // Release path (b) of the store's object-URL invariant: the explicit clear. Invoked exactly once
  // per dismissal, and it neither reads nor writes the cell's value or formula, so whatever the cell
  // displayed before the image arrived is revealed again unchanged.
  const handleImageDismiss = () => {
    clearCellImage(imageKey);
  };

  // Editing suppresses the overlay so the editor remains unobstructed. One derived
  // value gates both the containing block and the overlay mount, so the layer can
  // never be pinned to anything but its own cell.
  const visibleImage = isEditing ? undefined : cellImage;
  const affordance = isRejecting ? dragRejectOutline : isDragActive ? dragActiveOutline : undefined;

  // An idle cell forwards the caller's own style object by identity, so a cell with
  // no picture and no drag in progress renders exactly as it did before.
  const cellStyle: React.CSSProperties =
    visibleImage === undefined && affordance === undefined
      ? style
      : {
          ...style,
          ...(visibleImage !== undefined ? cellImageContainingBlock : undefined),
          ...affordance,
        };

  return (
    <div
      className="cell"
      style={cellStyle}
      onClick={handleCellClick}
      {...dragHandlers}
    >
      {isEditing ? (
        <input
          type="text"
          value={editValue}
          onChange={(e) => setEditValue(e.target.value)}
          onBlur={handleBlur}
          onKeyDown={handleKeyDown}
          autoFocus
        />
      ) : (
        <span>{formattedValue}</span>
      )}
      {visibleImage !== undefined ? (
        <CellImageOverlay entry={visibleImage} onDismiss={handleImageDismiss} />
      ) : null}
    </div>
  );
};

export default Cell;