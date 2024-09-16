import React from 'react';
import { TabGroup, Tab } from '@/components';
import { useAppSelector, useAppDispatch } from '@/store';
import { selectActiveWorkbook, setActiveTab } from '@/store/workbookSlice';

const Ribbon: React.FC = () => {
  const activeWorkbook = useAppSelector(selectActiveWorkbook);
  const dispatch = useAppDispatch();

  const handleTabChange = (tabName: string) => {
    dispatch(setActiveTab(tabName));
  };

  return (
    <TabGroup>
      <Tab label="Home" onClick={() => handleTabChange('Home')}>
        {/* HUMAN ASSISTANCE NEEDED */}
        {/* Implement Home tab content */}
      </Tab>
      <Tab label="Insert" onClick={() => handleTabChange('Insert')}>
        {/* HUMAN ASSISTANCE NEEDED */}
        {/* Implement Insert tab content */}
      </Tab>
      <Tab label="Page Layout" onClick={() => handleTabChange('Page Layout')}>
        {/* HUMAN ASSISTANCE NEEDED */}
        {/* Implement Page Layout tab content */}
      </Tab>
      <Tab label="Formulas" onClick={() => handleTabChange('Formulas')}>
        {/* HUMAN ASSISTANCE NEEDED */}
        {/* Implement Formulas tab content */}
      </Tab>
      <Tab label="Data" onClick={() => handleTabChange('Data')}>
        {/* HUMAN ASSISTANCE NEEDED */}
        {/* Implement Data tab content */}
      </Tab>
      <Tab label="Review" onClick={() => handleTabChange('Review')}>
        {/* HUMAN ASSISTANCE NEEDED */}
        {/* Implement Review tab content */}
      </Tab>
      <Tab label="View" onClick={() => handleTabChange('View')}>
        {/* HUMAN ASSISTANCE NEEDED */}
        {/* Implement View tab content */}
      </Tab>
    </TabGroup>
  );
};

export default Ribbon;