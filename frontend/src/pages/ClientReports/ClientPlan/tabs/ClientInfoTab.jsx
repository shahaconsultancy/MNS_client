import { ArrowRight, CalendarDays, Mail, User } from 'lucide-react';

const NOTES_MAX = 1000;

export default function ClientInfoTab({ info, onChange, onNext, nextLabel }) {
  const set = (field) => (e) => onChange({ ...info, [field]: e.target.value });

  return (
    <section className="plan-card">
      <h2>Client information</h2>
      <p className="plan-card__sub">Basic details used across the plan, reports and exports.</p>

      <h3 className="plan-section"><User size={15} /> PERSONAL &amp; CONTACT</h3>
      <div className="plan-grid">
        <label className="plan-field">
          <span>Client name</span>
          <input type="text" value={info.clientName} onChange={set('clientName')} />
        </label>
        <label className="plan-field">
          <span>Spouse name</span>
          <input type="text" value={info.spouseName} onChange={set('spouseName')} />
        </label>
        <label className="plan-field">
          <span>Phone</span>
          <div className="plan-input">
            <em>+91</em>
            <input
              type="tel"
              inputMode="numeric"
              maxLength={10}
              value={info.phone}
              onChange={(e) => onChange({ ...info, phone: e.target.value.replace(/\D/g, '') })}
            />
          </div>
          <small>10-digit mobile number</small>
        </label>
        <label className="plan-field">
          <span>Email</span>
          <div className="plan-input">
            <Mail size={15} />
            <input type="email" value={info.email} onChange={set('email')} />
          </div>
          <small>Reports are sent to this address</small>
        </label>
        <label className="plan-field plan-field--wide">
          <span>Address</span>
          <textarea rows={3} value={info.address} onChange={set('address')} />
        </label>
      </div>

      <hr className="plan-divider" />

      <h3 className="plan-section"><CalendarDays size={15} /> PLAN DETAILS</h3>
      <div className="plan-grid">
        <label className="plan-field">
          <span>Plan date</span>
          <input type="date" value={info.planDate} onChange={set('planDate')} />
        </label>
        <label className="plan-field plan-field--wide">
          <span>Advisor notes</span>
          <textarea
            rows={4}
            maxLength={NOTES_MAX}
            placeholder="Key observations, client priorities, follow-ups for the next meeting…"
            value={info.advisorNotes}
            onChange={set('advisorNotes')}
          />
          <small className="plan-field__row">
            <span>Internal only — not included in client reports</span>
            <span>{info.advisorNotes.length} / {NOTES_MAX}</span>
          </small>
        </label>
      </div>

      <footer className="plan-footer">
        <button type="button" className="plan-next" onClick={onNext}>
          Next: {nextLabel} <ArrowRight size={14} />
        </button>
      </footer>
    </section>
  );
}
