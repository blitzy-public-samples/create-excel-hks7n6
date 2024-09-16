import React, { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { WorkbookList, RecentActivity } from '@/components';
import { fetchWorkbooks } from '@/services/api';
import { useAppSelector, useAppDispatch } from '@/store';
import { selectUser } from '@/store/userSlice';

const Dashboard: React.FC = () => {
  const [workbooks, setWorkbooks] = useState([]);
  const user = useAppSelector(selectUser);
  const dispatch = useAppDispatch();

  useEffect(() => {
    const loadWorkbooks = async () => {
      try {
        const fetchedWorkbooks = await fetchWorkbooks();
        setWorkbooks(fetchedWorkbooks);
      } catch (error) {
        console.error('Failed to fetch workbooks:', error);
        // HUMAN ASSISTANCE NEEDED
        // TODO: Implement proper error handling and user feedback
      }
    };

    loadWorkbooks();
  }, []);

  return (
    <div className="dashboard">
      <h1>Welcome, {user.name}!</h1>
      
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