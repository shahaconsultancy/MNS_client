import { NavLink, useNavigate } from 'react-router-dom';
import { LayoutGrid, Calculator, PieChart, LogOut } from 'lucide-react';
import logo from '../../assets/images/logo-small.png';
import { logout, getUserEmail } from '../../services/authService.js';

import './Sidebar.css';

const NAV_ITEMS = [
  { to: '/client-reports', label: 'Client Reports', icon: LayoutGrid },
  { to: '/calculators', label: 'Calculators', icon: Calculator },
  { to: '/fund-analyzer', label: 'Fund Analyzer', icon: PieChart },
];

export default function Sidebar({ open, onNavigate }) {
  const navigate = useNavigate();

  const handleLogout = () => {
    logout();
    navigate('/login', { replace: true });
  };

  return (
    <aside className={`sidebar${open ? ' sidebar--open' : ''}`}>
      <div className="sidebar__brand">
        <img src={logo} alt="MNS" className="sidebar__logo" />
        <div>
          <strong>MNS</strong>
          <span>FINANCIAL SERVICES</span>
        </div>
      </div>

      <nav className="sidebar__nav">
        {NAV_ITEMS.map(({ to, label, icon: Icon }) => (
          <NavLink
            key={to}
            to={to}
            onClick={onNavigate}
            className={({ isActive }) => `sidebar__link${isActive ? ' sidebar__link--active' : ''}`}
          >
            <Icon size={16} /> {label}
          </NavLink>
        ))}
      </nav>

      <div className="sidebar__footer">
        <p className="sidebar__email">{getUserEmail()}</p>
        <button type="button" className="sidebar__signout" onClick={handleLogout}>
          <LogOut size={16} /> Sign Out
        </button>
      </div>
    </aside>
  );
}
