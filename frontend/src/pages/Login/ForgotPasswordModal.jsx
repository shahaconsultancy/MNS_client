import { useEffect, useState } from 'react';
import { CheckCircle2, KeyRound, Lock, X } from 'lucide-react';
import InputField from '../../components/common/InputField.jsx';
import { resetPassword } from '../../services/authService.js';
import { validateReset } from '../../utils/validators.js';

const EMPTY = { key: '', newPassword: '', confirmPassword: '' };

// Pop-up opened by "Forgot password?". `email` is the one typed in the sign-in form: its password is the one reset.
export default function ForgotPasswordModal({ email, onClose }) {
  const [form, setForm] = useState(EMPTY);
  const [errors, setErrors] = useState({});
  const [serverError, setServerError] = useState('');
  const [loading, setLoading] = useState(false);
  const [done, setDone] = useState(false);

  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  const handleChange = (e) => {
    setForm((f) => ({ ...f, [e.target.name]: e.target.value }));
    setErrors((er) => ({ ...er, [e.target.name]: undefined, form: undefined }));
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    setServerError('');
    const found = validateReset({ email, ...form });
    setErrors(found);
    if (Object.keys(found).length) return;

    setLoading(true);
    try {
      await resetPassword({ email, ...form });
      setForm(EMPTY);
      setDone(true);
    } catch (err) {
      setServerError(err.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="login-modal" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="login-modal__box" role="dialog" aria-modal="true" aria-labelledby="reset-title">
        <button type="button" className="login-modal__close" onClick={onClose} aria-label="Close"><X size={18} /></button>

        {done ? (
          <div className="login-modal__done">
            <CheckCircle2 size={44} />
            <h2 id="reset-title">Password updated</h2>
            <p>Sign in with your new password.</p>
            <button type="button" className="login__submit" onClick={onClose}>Back to sign in</button>
          </div>
        ) : (
          <form onSubmit={handleSubmit} noValidate>
            <h2 id="reset-title">Reset password</h2>
            <p className="login__sub">
              {email.trim() ? <>Account: <b>{email.trim().toLowerCase()}</b></> : 'Enter your email in the sign-in form first.'}
            </p>

            {(errors.form || serverError) && <div className="login__alert" role="alert">{errors.form || serverError}</div>}

            <InputField
              id="reset-key" name="key" type="password" label="Secret key" icon={KeyRound}
              placeholder="Enter the secret key" autoComplete="off"
              value={form.key} onChange={handleChange} error={errors.key}
            />
            <InputField
              id="reset-new" name="newPassword" type="password" label="New password" icon={Lock}
              placeholder="At least 8 characters" autoComplete="new-password"
              value={form.newPassword} onChange={handleChange} error={errors.newPassword}
            />
            <InputField
              id="reset-confirm" name="confirmPassword" type="password" label="Confirm password" icon={Lock}
              placeholder="Repeat the new password" autoComplete="new-password"
              value={form.confirmPassword} onChange={handleChange} error={errors.confirmPassword}
            />

            <button type="submit" className="login__submit" disabled={loading}>
              {loading ? 'Updating…' : 'Update password'}
            </button>
          </form>
        )}
      </div>
    </div>
  );
}
