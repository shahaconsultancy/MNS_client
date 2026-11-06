import { CirclePlus, CreditCard, GraduationCap, House, Car, User } from 'lucide-react';
import { inr, pct } from '../format.js';
import { AmountInput, ShareCell, TabFooter } from '../PlanParts.jsx';

export const LIABILITIES = [
  { key: 'home', label: 'Home loan', hint: 'Outstanding principal', icon: House, tone: 'red' },
  { key: 'vehicle', label: 'Vehicle loan', hint: 'Outstanding principal', icon: Car, tone: 'blue' },
  { key: 'personal', label: 'Personal loan', hint: 'Outstanding principal', icon: User, tone: 'purple' },
  { key: 'education', label: 'Education loan', hint: 'Outstanding principal', icon: GraduationCap, tone: 'green' },
  { key: 'other', label: 'Other loans', hint: 'Gold loan, loan against securities, etc.', icon: CirclePlus, tone: 'yellow' },
  { key: 'card', label: 'Credit card', hint: 'Outstanding dues', icon: CreditCard, tone: 'gray' },
];

// `data` holds the amount under `key` and the free-text purpose under `${key}Goal`.
export const liabilitiesTotal = (data) =>
  LIABILITIES.reduce((sum, l) => sum + (Number(data[l.key]) || 0), 0);

export default function LiabilitiesTab({
  liabilities, onLiabilitiesChange, assetsTotal, personalAssets, onBack, onNext, prevLabel, nextLabel,
}) {
  const total = liabilitiesTotal(liabilities);
  const share = (n) => (total ? (n / total) * 100 : 0);
  const netWorth = assetsTotal - total;
  const netWorthExcl = assetsTotal - personalAssets - total;
  const set = (field, value) => onLiabilitiesChange({ ...liabilities, [field]: value });

  return (
    <section className="plan-card plan-card--flush">
      <div className="plan-card__pad">
        <h2>Liabilities &amp; net worth</h2>
        <p className="plan-card__sub">Outstanding loans and dues, and what the household is worth after them.</p>
      </div>

      <div className="plan-table">
        <div className="plan-row plan-row--purpose plan-row--head">
          <span>LIABILITY</span><span>OUTSTANDING</span><span>PURPOSE / GOAL</span><span>% OF LIABILITIES</span>
        </div>

        {LIABILITIES.map(({ key, label, hint, icon: Icon, tone }) => (
          <div key={key} className="plan-row plan-row--purpose">
            <div className="plan-source">
              <span className={`plan-icon tone-bg--${tone}`}><Icon size={16} /></span>
              <div><strong>{label}</strong><small>{hint}</small></div>
            </div>
            <AmountInput label={label} value={liabilities[key]} onChange={(v) => set(key, v)} />
            <input
              type="text"
              className="plan-textbox"
              placeholder="Purpose / goal"
              maxLength={100}
              aria-label={`${label} purpose`}
              value={liabilities[`${key}Goal`] || ''}
              onChange={(e) => set(`${key}Goal`, e.target.value)}
            />
            <ShareCell percent={share(Number(liabilities[key]) || 0)} tone={tone} />
          </div>
        ))}

        <div className="plan-row plan-row--purpose plan-row--total">
          <strong>Total liabilities</strong>
          <strong>{inr(total)}</strong>
          <span />
          <strong>100%</strong>
        </div>
      </div>

      <div className="plan-card__pad">
        <h2>Net worth</h2>
        <p className="plan-card__sub">Total assets minus total liabilities.</p>

        <div className="nw__grid">
          <div className="sav__box"><span>TOTAL ASSETS</span><strong>{inr(assetsTotal)}</strong><small>From the Assets tab</small></div>
          <div className="sav__box">
            <span>TOTAL LIABILITIES</span><strong>{inr(total)}</strong>
            <small>Debt-to-assets {pct(assetsTotal ? (total / assetsTotal) * 100 : 0)}</small>
          </div>
          <div className="sav__box sav__box--dark">
            <span>NET WORTH (INCL. PERSONAL)</span><strong>{inr(netWorth)}</strong>
            <small>≈ ₹{(netWorth / 10000000).toFixed(2)} Cr</small>
          </div>
          <div className="sav__box sav__box--gold">
            <span>NET WORTH (EXCL. PERSONAL)</span><strong>{inr(netWorthExcl)}</strong>
            <small>Excludes personal assets ({inr(personalAssets)})</small>
          </div>
        </div>
      </div>

      <TabFooter onBack={onBack} onNext={onNext} prevLabel={prevLabel} nextLabel={nextLabel} />
    </section>
  );
}
