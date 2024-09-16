import { createSlice, PayloadAction } from '@reduxjs/toolkit';
import { User } from 'frontend/src/schema/workbookTypes';

const userSlice = createSlice({
  name: 'user',
  initialState: {
    currentUser: null as User | null,
    isAuthenticated: false,
    error: null as string | null,
  },
  reducers: {
    setUser: (state, action: PayloadAction<User>) => {
      state.currentUser = action.payload;
      state.isAuthenticated = true;
    },
    clearUser: (state) => {
      state.currentUser = null;
      state.isAuthenticated = false;
    },
    setError: (state, action: PayloadAction<string>) => {
      state.error = action.payload;
    },
  },
});

export const { setUser, clearUser, setError } = userSlice.actions;
export default userSlice.reducer;