import React, { useState, useEffect } from 'react';
import { useAppSelector, useAppDispatch } from '@/store';
import { selectActiveCell, updateCell } from '@/store/workbookSlice';
import { parseFormula } from '@/utils/formulaParser';

// HUMAN ASSISTANCE NEEDED
// This component may need additional error handling and UI improvements for production readiness

const FormulaBar: React.FC = () => {
  const dispatch = useAppDispatch();
  const activeCell = useAppSelector(selectActiveCell);
  const [formulaInput, setFormulaInput] = useState('');

  useEffect(() => {
    if (activeCell) {
      setFormulaInput(activeCell.formula || activeCell.value || '');
    }
  }, [activeCell]);

  const handleFormulaChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setFormulaInput(e.target.value);
  };

  const handleFormulaSubmit = () => {
    if (activeCell) {
      try {
        const parsedFormula = parseFormula(formulaInput);
        dispatch(updateCell({
          id: activeCell.id,
          changes: {
            formula: formulaInput,
            value: parsedFormula
          }
        }));
      } catch (error) {
        console.error('Formula parsing error:', error);
        // TODO: Implement proper error handling and user feedback
      }
    }
  };

  return (
    <div className="formula-bar">
      <input
        type="text"
        value={formulaInput}
        onChange={handleFormulaChange}
        onBlur={handleFormulaSubmit}
        onKeyPress={(e) => {
          if (e.key === 'Enter') {
            handleFormulaSubmit();
          }
        }}
        placeholder="Enter formula or value"
      />
    </div>
  );
};

export default FormulaBar;