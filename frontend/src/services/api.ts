import axios from 'axios';
import { WorkbookSchema, WorksheetSchema, CellSchema } from 'backend/app/schema/workbook_schema';
import type { InternalAxiosRequestConfig } from 'axios';
import { getAuth } from 'firebase/auth';

const API_BASE_URL = process.env.REACT_APP_API_BASE_URL;

// SECURITY: attach the verified Firebase ID token — requests previously carried no credential
axios.interceptors.request.use(async (config: InternalAxiosRequestConfig) => {
  try {
    const token = await getAuth().currentUser?.getIdToken();
    if (token) {
      config.headers.Authorization = `Bearer ${token}`;
    }
  } catch (error) {
    console.error('Error attaching authentication token:', error);
  }
  return config;
});

export const fetchWorkbooks = async (): Promise<WorkbookSchema[]> => {
  try {
    const response = await axios.get(`${API_BASE_URL}/workbooks`);
    return response.data;
  } catch (error) {
    console.error('Error fetching workbooks:', error);
    throw error;
  }
};

export const createWorkbook = async (workbook: WorkbookSchema): Promise<WorkbookSchema> => {
  try {
    const response = await axios.post(`${API_BASE_URL}/workbooks`, workbook);
    return response.data;
  } catch (error) {
    console.error('Error creating workbook:', error);
    throw error;
  }
};

// HUMAN ASSISTANCE NEEDED
// This function might need additional error handling or data validation
export const updateCell = async (workbookId: string, worksheetId: string, cell: CellSchema): Promise<CellSchema> => {
  try {
    const response = await axios.put(`${API_BASE_URL}/workbooks/${workbookId}/worksheets/${worksheetId}/cells`, cell);
    return response.data;
  } catch (error) {
    console.error('Error updating cell:', error);
    throw error;
  }
};