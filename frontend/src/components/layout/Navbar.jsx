import { Menu } from 'lucide-react';
import logo from '../../assets/images/logo-small.png';
import './Navbar.css';

// Top bar shown on small screens only; on desktop the sidebar handles navigation.
export default function Navbar({ onMenuClick }) {
  return (
    <header className="navbar">
      <button type="button" className="navbar__menu" onClick={onMenuClick} aria-label="Open menu">
        <Menu size={22} />
      </button>
      <img src={logo} alt="MNS" className="navbar__logo" />
      <strong>MNS Financial Services</strong>
    </header>
  );
}
