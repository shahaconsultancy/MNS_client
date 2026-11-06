import { Briefcase, Building2, Gift, House, Info, TrendingUp } from 'lucide-react';
import { inr } from '../format.js';
import { AmountInput, ShareCell, TabFooter } from '../PlanParts.jsx';

export const SOURCES = [
  { key: 'salary', label: 'Salary income', hint: 'Husband + wife, net of tax', icon: Briefcase, tone: 'blue' },
  { key: 'bonus', label: 'Annual bonus', hint: 'Husband + wife, monthly equivalent', icon: Gift, tone: 'yellow' },
  { key: 'business', label: 'Business income', hint: 'Profession or business', icon: Building2, tone: 'purple' },
  { key: 'rental', label: 'Rental income', hint: 'Property rentals received', icon: House, tone: 'green' },
  { key: 'investment', label: 'Investment income', hint: 'Interest, dividends, SWP', icon: TrendingUp, tone: 'red' },
];

export const incomeTotal = (income) =>
  SOURCES.reduce((sum, s) => sum + (Number(income[s.key]) || 0), 0);

export default function IncomeTab({ income, onIncomeChange, onBack, onNext, nextLabel, prevLabel }) {
  const amount = (key) => Number(income[key]) || 0;
  const total = incomeTotal(income);
  const share = (key) => (total ? (amount(key) / total) * 100 : 0);

  return (
    <section className="plan-card plan-card--flush">
      <div className="plan-card__pad">
        <div className="income__head">
          <div>
            <h2>Income</h2>
            <p className="plan-card__sub">Monthly take-home income of the household.</p>
          </div>
          <div className="income__totals">
            <div><span>MONTHLY</span><strong>{inr(total)}</strong></div>
            <div><span>ANNUAL</span><strong>{inr(total * 12)}</strong></div>
          </div>
        </div>

        <div className="income__bar" aria-hidden="true">
          {SOURCES.map((s) => (
            <i key={s.key} className={`tone--${s.tone}`} style={{ width: `${share(s.key)}%` }} />
          ))}
        </div>
        <p className="income__bar-note">{total ? 'Share of total monthly income by source' : 'No income entered yet'}</p>
      </div>

      <div className="plan-table">
        <div className="plan-row plan-row--head">
          <span>SOURCE</span><span>MONTHLY</span><span>ANNUAL</span><span>SHARE</span>
        </div>

        {SOURCES.map(({ key, label, hint, icon: Icon, tone }) => (
          <div key={key} className="plan-row">
            <div className="plan-source">
              <span className={`plan-icon tone-bg--${tone}`}><Icon size={16} /></span>
              <div><strong>{label}</strong><small>{hint}</small></div>
            </div>
            <AmountInput label={label} value={income[key]} onChange={(v) => onIncomeChange({ ...income, [key]: v })} />
            <span className="plan-annual">{inr(amount(key) * 12)}</span>
            <ShareCell percent={share(key)} tone={tone} />
          </div>
        ))}

        <div className="plan-row plan-row--total">
          <strong>Total income</strong>
          <strong>{inr(total)}</strong>
          <span>{inr(total * 12)}</span>
          <strong>100%</strong>
        </div>
      </div>

      <p className="plan-note">
        <Info size={14} /> Enter amounts net of income tax. Include both husband's and wife's income where applicable.
      </p>

      <TabFooter onBack={onBack} onNext={onNext} prevLabel={prevLabel} nextLabel={nextLabel} />
    </section>
  );
}
