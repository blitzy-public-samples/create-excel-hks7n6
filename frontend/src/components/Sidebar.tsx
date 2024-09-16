import React from 'react';
import { FormattingOption } from '@/components';
import { useAppSelector, useAppDispatch } from '@/store';
import { selectActiveCell, updateCellStyle } from '@/store/workbookSlice';

const Sidebar: React.FC = () => {
  const dispatch = useAppDispatch();
  const activeCell = useAppSelector(selectActiveCell);

  const handleStyleChange = (property: string, value: string) => {
    if (activeCell) {
      dispatch(updateCellStyle({ cellId: activeCell.id, style: { [property]: value } }));
    }
  };

  return (
    <div className="sidebar">
      <h2>Formatting</h2>
      <FormattingOption
        label="Font"
        options={['Arial', 'Helvetica', 'Times New Roman', 'Courier']}
        value={activeCell?.style?.fontFamily || 'Arial'}
        onChange={(value) => handleStyleChange('fontFamily', value)}
      />
      <FormattingOption
        label="Size"
        options={['10', '12', '14', '16', '18', '20', '24']}
        value={activeCell?.style?.fontSize || '12'}
        onChange={(value) => handleStyleChange('fontSize', `${value}px`)}
      />
      <FormattingOption
        label="Color"
        options={['black', 'red', 'blue', 'green']}
        value={activeCell?.style?.color || 'black'}
        onChange={(value) => handleStyleChange('color', value)}
      />
      <FormattingOption
        label="Alignment"
        options={['left', 'center', 'right']}
        value={activeCell?.style?.textAlign || 'left'}
        onChange={(value) => handleStyleChange('textAlign', value)}
      />
    </div>
  );
};

export default Sidebar;