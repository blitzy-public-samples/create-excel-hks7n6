import React, { useState, useEffect } from 'react';
import { useAppSelector, useAppDispatch } from '@/store';
import { updateCell } from '@/store/workbookSlice';
import { formatCellValue } from '@/utils/cellFormatting';

// HUMAN ASSISTANCE NEEDED
// The following component may need additional refinement for production readiness.
// Please review and enhance the implementation, particularly for edge cases and performance optimization.

interface CellProps {
  id: string;
  value: any;
  style: React.CSSProperties;
}

const Cell: React.FC<CellProps> = ({ id, value, style }) => {
  const dispatch = useAppDispatch();
  const [isEditing, setIsEditing] = useState(false);
  const [editValue, setEditValue] = useState('');

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

  return (
    <div
      className="cell"
      style={style}
      onClick={handleCellClick}
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
    </div>
  );
};

export default Cell;