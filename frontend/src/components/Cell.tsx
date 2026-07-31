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
  // Optional so this cell keeps rendering exactly as before when no key is supplied,
  // and so any existing caller compiles untouched. Identifies which ephemeral image
  // belongs to this cell; the grid derives it from the worksheet id plus the row and
  // column indices. An absent key makes the drop handlers inert.
  imageKey?: string;
}

// Token-driven drop affordance, merged over the cell's own style only while a drag is in progress
const dragActiveOutline: React.CSSProperties = {
  outlineWidth: CELL_IMAGE_TOKENS.dropOutlineWidth,
  outlineStyle: CELL_IMAGE_TOKENS.dropOutlineStyle,
  outlineColor: CELL_IMAGE_TOKENS.dropActiveOutlineColor,
  backgroundColor: CELL_IMAGE_TOKENS.dropActiveBackground,
};

// Token-driven rejection affordance, shown transiently when a dropped file fails validation
const dragRejectOutline: React.CSSProperties = {
  outlineWidth: CELL_IMAGE_TOKENS.dropOutlineWidth,
  outlineStyle: CELL_IMAGE_TOKENS.dropOutlineStyle,
  outlineColor: CELL_IMAGE_TOKENS.dropRejectOutlineColor,
};

const Cell: React.FC<CellProps> = ({ id, value, style, imageKey }) => {
  const dispatch = useAppDispatch();
  const [isEditing, setIsEditing] = useState(false);
  const [editValue, setEditValue] = useState('');

  // Ephemeral, page-lifetime image state. It is read from a React context that is
  // deliberately not part of the Redux workbook, so a dropped picture never reaches
  // the persisted document model and this cell's value and formula stay untouched.
  // The context ships a working inert default, so the two reads below behave
  // identically whether or not a provider is mounted above this cell.
  const { getCellImage, clearCellImage } = useCellImages();
  const { dragHandlers, isDragActive, isRejecting } = useCellImageDrop(imageKey);
  const cellImage = getCellImage(imageKey);

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

  // Removes this cell's picture. Delegating to the store is what keeps the blob URL
  // paired: the store releases the one URL it minted for this key, exactly once. The
  // cell's own value and formula are not touched, so dismissing reveals the original
  // value unchanged, and edit mode is not entered.
  const handleImageDismiss = () => {
    clearCellImage(imageKey);
  };

  return (
    <div
      className="cell"
      style={isDragActive || isRejecting ? { ...style, ...(isRejecting ? dragRejectOutline : dragActiveOutline) } : style}
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
      {/* Sibling of the editing/display ternary, never a replacement: the picture is a
          layer over the value area, so the value stays in the store and reappears the
          moment the picture is dismissed. Suppressed while editing so it can never
          obstruct the auto-focused input above. */}
      {!isEditing && cellImage ? (
        <CellImageOverlay entry={cellImage} onDismiss={handleImageDismiss} />
      ) : null}
    </div>
  );
};

export default Cell;