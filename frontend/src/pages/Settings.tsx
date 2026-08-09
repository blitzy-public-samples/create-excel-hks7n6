import React, { useState } from 'react';
import { SettingsForm } from '@/components';
import type { User } from '@/schema/workbookTypes';
import { useAppSelector, useAppDispatch } from '@/store';
import { selectUser, setUser } from '@/store/userSlice';

const Settings: React.FC = () => {
  const dispatch = useAppDispatch();
  const user = useAppSelector(selectUser);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // CONTRACT: the API exposes no user-settings route, so the submitted values are applied to
  // the signed-in user held in the store and are not persisted server-side.
  const handleSubmit = async (formData: Partial<User>) => {
    setIsLoading(true);
    setError(null);

    try {
      if (user === null) {
        setError('Sign in before changing your settings.');
      } else {
        dispatch(setUser({ ...user, ...formData }));
      }
    } catch (err) {
      setError('Failed to update settings. Please try again.');
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="settings-page">
      <h1>User Settings</h1>
      {error && <div className="error-message">{error}</div>}
      <SettingsForm
        initialData={user}
        onSubmit={handleSubmit}
        isLoading={isLoading}
      />
    </div>
  );
};

export default Settings;