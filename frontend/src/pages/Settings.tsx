import React, { useState } from 'react';
import { SettingsForm } from '@/components';
import { updateUserSettings as updateUserSettingsAPI } from '@/services/api';
import { useAppSelector, useAppDispatch } from '@/store';
import { selectUser, updateUserSettings } from '@/store/userSlice';

const Settings: React.FC = () => {
  const dispatch = useAppDispatch();
  const user = useAppSelector(selectUser);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (formData: any) => {
    setIsLoading(true);
    setError(null);

    try {
      const updatedUser = await updateUserSettingsAPI(formData);
      dispatch(updateUserSettings(updatedUser));
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