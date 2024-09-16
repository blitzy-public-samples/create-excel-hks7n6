import React, { useCallback, useEffect } from 'react';
import { Cell } from '@/components';
import { useAppSelector, useAppDispatch } from '@/store';
import { selectActiveWorksheet, updateCell } from '@/store/workbookSlice';
import { formatCellValue } from '@/utils/cellFormatting';

// HUMAN ASSISTANCE NEEDED
// The following Grid component implementation may need further refinement and testing for production readiness.
// Additional error handling, performance optimizations, and edge case considerations may be required.

const Grid: React.FC = () => {
  const dispatch = useAppDispatch();
  const activeWorksheet = useAppSelector(selectActiveWorksheet);

  const [selectedCell, setSelectedCell] = React.useState<{ row: number; col: number } | null>(null);

  const handleCellClick = useCallback((row: number, col: number) => {
    setSelectedCell({ row, col });
  }, []);

  const handleCellChange = useCallback((row: number, col: number, value: string) => {
    dispatch(updateCell({ row, col, value }));
  }, [dispatch]);

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (!selectedCell) return;

    const { row, col } = selectedCell;
    let newRow = row;
    let newCol = col;

    switch (e.key) {
      case 'ArrowUp':
        newRow = Math.max(0, row - 1);
        break;
      case 'ArrowDown':
        newRow = Math.min(activeWorksheet.rows.length - 1, row + 1);
        break;
      case 'ArrowLeft':
        newCol = Math.max(0, col - 1);
        break;
      case 'ArrowRight':
        newCol = Math.min(activeWorksheet.rows[0].cells.length - 1, col + 1);
        break;
      default:
        return;
    }

    setSelectedCell({ row: newRow, col: newCol });
    e.preventDefault();
  }, [selectedCell, activeWorksheet]);

  useEffect(() => {
    window.addEventListener('keydown', handleKeyDown as any);
    return () => {
      window.removeEventListener('keydown', handleKeyDown as any);
    };
  }, [handleKeyDown]);

  return (
    <div className="grid" role="grid" tabIndex={0}>
      {activeWorksheet.rows.map((row, rowIndex) => (
        <div key={rowIndex} className="row" role="row">
          {row.cells.map((cell, colIndex) => (
            <Cell
              key={`${rowIndex}-${colIndex}`}
              value={formatCellValue(cell.value, cell.format)}
              isSelected={selectedCell?.row === rowIndex && selectedCell?.col === colIndex}
              onClick={() => handleCellClick(rowIndex, colIndex)}
              onChange={(value) => handleCellChange(rowIndex, colIndex, value)}
            />
          ))}
        </div>
      ))}
    </div>
  );
};

export default Grid;