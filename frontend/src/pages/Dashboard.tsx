import React, { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { useDispatch } from 'react-redux';
import { WorkbookList, RecentActivity } from '@/components';
import { apiFailure, apiFailureMessage, fetchWorkbooks } from '@/services/api';
import type { WorkbookSchema } from '@/schema/workbookTypes';
import { useAppSelector } from '@/store';
import { clearUser, selectUser } from '@/store/userSlice';

const Dashboard: React.FC = () => {
  // Typed as the response DTO, which is what fetchWorkbooks resolves to.
  const [workbooks, setWorkbooks] = useState<WorkbookSchema[]>([]);
  const [error, setError] = useState<string | null>(null);
  const user = useAppSelector(selectUser);
  const dispatch = useDispatch();

  useEffect(() => {
    const loadWorkbooks = async () => {
      try {
        const fetchedWorkbooks = await fetchWorkbooks();
        setWorkbooks(fetchedWorkbooks);
        setError(null);
      } catch (err) {
        // SECURITY: a credential the API refused stops counting as a signed-in session here
        // too, so the interface cannot go on presenting one the server will not serve.
        if (apiFailure(err)?.reauthenticate === true) {
          dispatch(clearUser());
        }
        // CONTRACT: the message is the classification api.ts computed from the response STATUS,
        // never text from the response body. An empty list and a refused request are different
        // things and must not look the same.
        setError(
          apiFailureMessage(err, 'Your workbooks could not be loaded. Try again in a moment.')
        );
      }
    };

    loadWorkbooks();
  }, [dispatch]);

  return (
    <div className="dashboard">
      <h1>Welcome, {user.name}!</h1>

      {error === null ? null : (
        <div className="dashboard-error" role="alert">
          <span>{error}</span>
          <button type="button" onClick={() => setError(null)}>
            Dismiss
          </button>
        </div>
      )}

      <section className="workbooks">
        <h2>Your Workbooks</h2>
        <WorkbookList workbooks={workbooks} />
        <Link to="/workbook/new" className="btn btn-primary">
          Create New Workbook
        </Link>
      </section>

      <section className="recent-activity">
        <h2>Recent Activity</h2>
        <RecentActivity />
      </section>
    </div>
  );
};

export default Dashboard;