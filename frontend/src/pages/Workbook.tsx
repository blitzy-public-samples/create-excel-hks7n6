import React, { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { Grid, Ribbon, FormulaBar, Sidebar } from '@/components';
import { fetchWorkbook, updateCell } from '@/services/api';
import { useAppSelector, useAppDispatch } from '@/store';
import { setCurrentWorkbook, updateCellValue } from '@/store/workbookSlice';

// HUMAN ASSISTANCE NEEDED
// The confidence level is below 0.8, indicating that this component might need additional review or improvements for production readiness.

const Workbook: React.FC = () => {
  const { id } = useParams<{ id: string }>();
  const dispatch = useAppDispatch();
  const currentWorkbook = useAppSelector((state) => state.workbook.currentWorkbook);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const loadWorkbook = async () => {
      try {
        setLoading(true);
        const workbookData = await fetchWorkbook(id);
        dispatch(setCurrentWorkbook(workbookData));
        setLoading(false);
      } catch (err) {
        setError('Failed to load workbook');
        setLoading(false);
      }
    };

    loadWorkbook();
  }, [id, dispatch]);

  const handleCellUpdate = async (cellId: string, value: string) => {
    try {
      await updateCell(id, cellId, value);
      dispatch(updateCellValue({ cellId, value }));
    } catch (err) {
      setError('Failed to update cell');
    }
  };

  if (loading) {
    return <div>Loading...</div>;
  }

  if (error) {
    return <div>Error: {error}</div>;
  }

  return (
    <div className="workbook-container">
      <Ribbon />
      <FormulaBar />
      <div className="workbook-content">
        <Grid workbook={currentWorkbook} onCellUpdate={handleCellUpdate} />
        <Sidebar />
      </div>
    </div>
  );
};

export default Workbook;