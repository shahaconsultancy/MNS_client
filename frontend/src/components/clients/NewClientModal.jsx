import { useEffect, useRef, useState } from 'react';
import { X, ArrowRight } from 'lucide-react';
import { createClient } from '../../services/clientService.js';
import './NewClientModal.css';

const TITLES = ['Mr', 'Mrs', 'Ms', 'Dr'];
const EMAIL_REGEX = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

const cleanName = (v) => v.trim().replace(/\s+/g, ' ');
const titleCase = (v) => v.toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase());
const initials = (name) => name.split(' ').slice(0, 2).map((w) => w[0]).join('').toUpperCase();

export default function NewClientModal({ onClose, onCreated }) {
  const [title, setTitle] = useState('Mr');
  const [fullName, setFullName] = useState('');
  const [mobile, setMobile] = useState('');
  const [email, setEmail] = useState('');
  const [errors, setErrors] = useState({});
  const [submitting, setSubmitting] = useState(false);
  const nameRef = useRef(null);

  useEffect(() => {
    nameRef.current?.focus();
    const onKey = (e) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const name = cleanName(fullName);
  const words = name.split(' ').filter(Boolean);
  const planName = words.length ? `${titleCase(words[words.length - 1])} Family Financial Plan` : '';
  const canSubmit = name.length >= 2 && !submitting;

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!canSubmit) return;

    const next = {};
    if (mobile && mobile.length !== 10) next.mobile = 'Enter a valid 10-digit mobile number.';
    if (email && !EMAIL_REGEX.test(email.trim())) next.email = 'Enter a valid email address.';
    setErrors(next);
    if (Object.keys(next).length) return;

    setSubmitting(true);
    try {
      const client = await createClient({
        title,
        full_name: name,
        plan_name: planName,
        mobile,
        email: email.trim(),
      });
      onCreated(client);
    } catch (err) {
      setErrors({ form: err.message });
      setSubmitting(false);
    }
  };

  return (
    <div className="modal" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <form className="modal__panel" onSubmit={handleSubmit} noValidate>
        <header className="modal__head">
          <div>
            <p>NEW CLIENT</p>
            <h2>Create client plan</h2>
            <span>Start a financial plan. You can add income, assets and family members next.</span>
          </div>
          <button type="button" className="modal__close" onClick={onClose} aria-label="Close">
            <X size={18} />
          </button>
        </header>

        <div className="modal__body">
          <div className="mfield">
            <label>Title</label>
            <div className="seg">
              {TITLES.map((t) => (
                <button
                  key={t}
                  type="button"
                  className={`seg__item${title === t ? ' seg__item--active' : ''}`}
                  onClick={() => setTitle(t)}
                >
                  {t}.
                </button>
              ))}
            </div>
          </div>

          <div className="mfield">
            <div className="mfield__row">
              <label htmlFor="nc-name">Client full name</label>
              <span className="mfield__req">*</span>
            </div>
            <input
              id="nc-name"
              ref={nameRef}
              className="minput"
              placeholder="e.g. Rucha Karoshi"
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
              maxLength={150}
            />
            <small>First and last name, as on their documents</small>
          </div>

          <div className="mfield">
            <div className="mfield__row">
              <label>Plan name</label>
              <small>Filled in from the surname</small>
            </div>
            <div className="minput minput--auto">
              <span className={planName ? '' : 'minput__ph'}>{planName || 'Karoshi Family Financial Plan'}</span>
              <em>AUTO</em>
            </div>
          </div>

          <p className="modal__divider"><span>CONTACT · OPTIONAL</span></p>

          <div className="modal__two">
            <div className="mfield">
              <label htmlFor="nc-mobile">Mobile</label>
              <div className="phone">
                <span>+91</span>
                <input
                  id="nc-mobile"
                  inputMode="numeric"
                  placeholder="98765 43210"
                  value={mobile}
                  onChange={(e) => setMobile(e.target.value.replace(/\D/g, '').slice(0, 10))}
                />
              </div>
              {errors.mobile && <small className="merror">{errors.mobile}</small>}
            </div>
            <div className="mfield">
              <label htmlFor="nc-email">Email</label>
              <input
                id="nc-email"
                type="email"
                className="minput"
                placeholder="name@email.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
              {errors.email && <small className="merror">{errors.email}</small>}
            </div>
          </div>

          <div className="preview">
            <span className="preview__avatar">{name ? initials(name) : '?'}</span>
            <div>
              <strong>{name ? `${title.toUpperCase()}. ${name.toUpperCase()}` : 'CLIENT NAME'}</strong>
              <span>{planName || 'Plan name'}</span>
            </div>
            <em>PREVIEW</em>
          </div>

          {errors.form && <p className="merror merror--form" role="alert">{errors.form}</p>}
        </div>

        <footer className="modal__foot">
          <span className="modal__hint"><kbd>Enter</kbd> to create · <kbd>Esc</kbd> to close</span>
          <button type="button" className="mbtn mbtn--ghost" onClick={onClose}>Cancel</button>
          <button type="submit" className="mbtn mbtn--primary" disabled={!canSubmit}>
            {submitting ? 'Creating…' : 'Create & open plan'} <ArrowRight size={15} />
          </button>
        </footer>
      </form>
    </div>
  );
}
