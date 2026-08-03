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

// Apply transitions only with the active/rejection affordance so the idle path can preserve the
// caller's style object by identity.
const affordanceTransition: React.CSSProperties = {
  transitionProperty: 'outline-color, outline-width, background-color',
  transitionDuration: CELL_IMAGE_TOKENS.transitionDuration,
};

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

// An affordance contributes longhands, and React's update path turns a style object that carries both
// a shorthand and one of that shorthand's own longhands into a destructive one: leaving the affordance
// clears the longhands it contributed, while the caller's shorthand is identical across the two renders
// and so is never re-emitted. Everything that shorthand declared is therefore lost for the rest of the
// mount, and React warns that the two were mixed. Each longhand an affordance can contribute is mapped
// here to the shorthand family it belongs to.
const affordanceShorthandFamilies: Readonly<Record<string, string | undefined>> = {
  backgroundColor: 'background',
  outlineWidth: 'outline',
  outlineStyle: 'outline',
  outlineColor: 'outline',
  transitionProperty: 'transition',
  transitionDuration: 'transition',
};

// Withdraws the caller's shorthand for exactly as long as an affordance writes into that family, which
// is React's own remedy for the collision: replace the shorthand with the separate values. The result
// renders identically to the superseded merge, because a longhand already won over the shorthand it
// followed. The caller's object is copied rather than mutated and is returned untouched when no
// affordance is applied, so the idle branch still forwards it by identity.
const withoutSupersededShorthands = (
  base: React.CSSProperties,
  applied: React.CSSProperties | undefined,
): React.CSSProperties => {
  if (applied === undefined) {
    return base;
  }
  const resolved: Record<string, unknown> = { ...base };
  Object.keys(applied).forEach((property: string) => {
    const family = affordanceShorthandFamilies[property];
    if (family !== undefined) {
      delete resolved[family];
    }
  });
  return resolved as React.CSSProperties;
};

const Cell: React.FC<CellProps> = ({ id, value, style, imageKey }) => {
  const dispatch = useAppDispatch();
  const [isEditing, setIsEditing] = useState(false);
  const [editValue, setEditValue] = useState('');
  // Key-scoped snapshots prevent unrelated cells from re-rendering; each provider publish still
  // notifies subscribers to compare their own slice.
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

  // Clearing the ephemeral image leaves the cell's persisted value and formula untouched.
  const handleImageDismiss = () => {
    clearCellImage(imageKey);
  };

  // Editing suppresses the overlay so the editor remains unobstructed. One derived
  // value gates both the containing block and the overlay mount, so the layer can
  // never be pinned to anything but its own cell.
  const visibleImage = isEditing ? undefined : cellImage;
  // A drag in progress outranks a refusal that is still standing, because the active affordance
  // describes what releasing now would do and is what the hook is already advertising through
  // dropEffect. Reversing these two would paint a refusal over a payload this cell will accept; the
  // page-level notice goes on explaining the earlier refusal until its own timer retires it.
  const affordance = isDragActive ? dragActiveOutline : isRejecting ? dragRejectOutline : undefined;

  // An idle cell forwards the caller's own style object by identity, so a cell with
  // no picture and no drag in progress renders exactly as it did before. Any shorthand
  // the affordance would collide with steps aside only while the affordance is applied,
  // so returning to this branch restores the caller's declaration in full.
  const cellStyle: React.CSSProperties =
    visibleImage === undefined && affordance === undefined
      ? style
      : {
          ...withoutSupersededShorthands(style, affordance),
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