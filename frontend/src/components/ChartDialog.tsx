import React, { useState } from 'react';
import { Modal } from '@/components';
import { useAppSelector, useAppDispatch } from '@/store';
import { selectActiveWorksheet, addChart } from '@/store/workbookSlice';
import { Chart } from 'chart.js';

// HUMAN ASSISTANCE NEEDED
// The following component needs further refinement and implementation details.
// The confidence level is below 0.8, indicating that additional work is required
// to make this production-ready.

const ChartDialog: React.FC<{ isOpen: boolean; onClose: () => void }> = ({ isOpen, onClose }) => {
  const dispatch = useAppDispatch();
  const activeWorksheet = useAppSelector(selectActiveWorksheet);
  const [chartType, setChartType] = useState<string>('');
  const [dataRange, setDataRange] = useState<string>('');
  const [previewChart, setPreviewChart] = useState<Chart | null>(null);

  const handleChartTypeChange = (e: React.ChangeEvent<HTMLSelectElement>) => {
    setChartType(e.target.value);
  };

  const handleDataRangeChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setDataRange(e.target.value);
  };

  const handlePreviewChart = () => {
    // TODO: Implement chart preview using Chart.js
    // This will require parsing the data range and creating a Chart instance
  };

  const handleCreateChart = () => {
    if (activeWorksheet && chartType && dataRange) {
      dispatch(addChart({ worksheetId: activeWorksheet.id, chartType, dataRange }));
      onClose();
    }
  };

  return (
    <Modal isOpen={isOpen} onClose={onClose}>
      <h2>Create Chart</h2>
      <div>
        <label htmlFor="chartType">Chart Type:</label>
        <select id="chartType" value={chartType} onChange={handleChartTypeChange}>
          <option value="">Select a chart type</option>
          <option value="bar">Bar Chart</option>
          <option value="line">Line Chart</option>
          <option value="pie">Pie Chart</option>
          {/* Add more chart types as needed */}
        </select>
      </div>
      <div>
        <label htmlFor="dataRange">Data Range:</label>
        <input
          type="text"
          id="dataRange"
          value={dataRange}
          onChange={handleDataRangeChange}
          placeholder="e.g., A1:B10"
        />
      </div>
      <button onClick={handlePreviewChart}>Preview Chart</button>
      {previewChart && <div id="chartPreview"></div>}
      <button onClick={handleCreateChart}>Create Chart</button>
    </Modal>
  );
};

export default ChartDialog;