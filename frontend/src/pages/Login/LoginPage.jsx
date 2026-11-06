import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowRight, FileText, Lock, Mail, ShieldCheck, TrendingUp, Users } from 'lucide-react';
import logo from '../../assets/images/logo-small.png';
import InputField from '../../components/common/InputField.jsx';
import ForgotPasswordModal from './ForgotPasswordModal.jsx';
import { login } from '../../services/authService.js';
import { validateLogin } from '../../utils/validators.js';
import './LoginPage.css';

const FEATURES = [
  { icon: FileText, title: 'Statement builder', text: 'Assets, liabilities and cash flow in one view' },
  { icon: TrendingUp, title: 'Goal calculators', text: 'Model retirement, education and protection needs' },
  { icon: Users, title: 'Family planner', text: 'Plan for the whole household, not one account' },
];

export default function LoginPage() {
  const navigate = useNavigate();
  const [form, setForm] = useState({ email: '', password: '', remember: false });
  const [errors, setErrors] = useState({});
  const [serverError, setServerError] = useState('');
  const [loading, setLoading] = useState(false);
  const [resetOpen, setResetOpen] = useState(false);

  const handleChange = (e) => {
    const { name, value, type, checked } = e.target;
    setForm((f) => ({ ...f, [name]: type === 'checkbox' ? checked : value }));
    setErrors((er) => ({ ...er, [name]: undefined }));
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    setServerError('');
    const found = validateLogin(form);
    setErrors(found);
    if (Object.keys(found).length) return;

    setLoading(true);
    try {
      await login(form);
      navigate('/client-reports', { replace: true });
    } catch (err) {
      setServerError(err.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <main className="login">
      <section className="login__brand">
        <img src={logo} alt="Money Navigating Solutions" className="login__logo" />

        <div className="login__hero">
          <h1>Financial Statement Builder &amp; Family Planner</h1>
          <p>One secure workspace to build client statements, model goals and deliver polished reports.</p>

          <ul className="login__features">
            {FEATURES.map(({ icon: Icon, title, text }) => (
              <li key={title}>
                <span className="login__feature-icon"><Icon size={18} /></span>
                <div>
                  <strong>{title}</strong>
                  <span>{text}</span>
                </div>
              </li>
            ))}
          </ul>
        </div>

        <small>&copy; {new Date().getFullYear()} Money Navigating Solutions</small>
      </section>

      <section className="login__form-wrap">
        <form className="login__form" onSubmit={handleSubmit} noValidate>
          <span className="login__eyebrow">Advisor sign in</span>
          <h2>Welcome back</h2>
          <p className="login__sub">Sign in to open your client workspace.</p>

          {serverError && <div className="login__alert" role="alert">{serverError}</div>}

          <InputField
            id="email"
            name="email"
            type="email"
            label="Email"
            icon={Mail}
            placeholder="you@example.com"
            autoComplete="email"
            value={form.email}
            onChange={handleChange}
            error={errors.email}
          />

          <InputField
            id="password"
            name="password"
            type="password"
            label="Password"
            icon={Lock}
            placeholder="Your password"
            autoComplete="current-password"
            value={form.password}
            onChange={handleChange}
            error={errors.password}
            labelAside={<button type="button" className="login__forgot" onClick={() => setResetOpen(true)}>Forgot password?</button>}
          />

          <label className="login__remember">
            <input type="checkbox" name="remember" checked={form.remember} onChange={handleChange} />
            Keep me signed in on this device
          </label>

          <button type="submit" className="login__submit" disabled={loading}>
            {loading ? 'Signing in…' : <>Sign in <ArrowRight size={16} /></>}
          </button>

          <p className="login__secure">
            <ShieldCheck size={16} />
            Encrypted session. Access is limited to authorised advisors.
          </p>
        </form>
      </section>

      {resetOpen && <ForgotPasswordModal email={form.email} onClose={() => setResetOpen(false)} />}
    </main>
  );
}
