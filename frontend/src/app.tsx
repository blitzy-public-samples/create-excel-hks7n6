import React from 'react';
import { BrowserRouter, Route, Switch } from 'react-router-dom';
import { Ribbon, Sidebar } from '@/components';
import { Dashboard, Workbook, Settings } from '@/pages';
import { Provider, store } from '@/store';
import { AuthProvider } from '@/services/auth';

const App: React.FC = () => {
  return (
    <Provider store={store}>
      <AuthProvider>
        <BrowserRouter>
          <div className="app-container">
            <Ribbon />
            <div className="main-content">
              <Sidebar />
              <Switch>
                <Route exact path="/" component={Dashboard} />
                <Route path="/workbook/:id?" component={Workbook} />
                <Route path="/settings" component={Settings} />
              </Switch>
            </div>
          </div>
        </BrowserRouter>
      </AuthProvider>
    </Provider>
  );
};

export default App;