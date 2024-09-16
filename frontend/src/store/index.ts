import { configureStore } from '@reduxjs/toolkit';
import { workbookReducer } from './workbookSlice';
import { userReducer } from './userSlice';

export const setupStore = () => {
  return configureStore({
    reducer: {
      workbook: workbookReducer,
      user: userReducer,
    },
    middleware: (getDefaultMiddleware) => getDefaultMiddleware(),
    devTools: process.env.NODE_ENV !== 'production',
  });
};

export const store = setupStore();

export type RootState = ReturnType<typeof store.getState>;
export type AppDispatch = typeof store.dispatch;