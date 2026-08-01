import React, { useCallback, useEffect } from 'react';
import { Cell } from '@/components';
import { useAppSelector, useAppDispatch } from '@/store';
import { selectActiveWorksheet, updateCell } from '@/store/workbookSlice';
import { formatCellValue } from '@/utils/cellFormatting';
// Relative specifier on purpose: the '@/…' prefix used above resolves under no alias
// this project declares, so a new module imported that way would not resolve.
import { cellImageKey } from '../features/cellImages/cellImageKey';

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
              // Addresses this cell's ephemeral image on the same worksheet, row and
              // column basis as the React key above. Derived at the render site because
              // the persisted worksheet's cells record diverges from the rows[].cells[]
              // model iterated here, so no key taken from that collection would be stable.
              imageKey={cellImageKey(activeWorksheet.id, rowIndex, colIndex)}
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