import { CirclePlus, FileText, TrendingUp } from 'lucide-react';
import { inr } from '../format.js';
import { AmountInput, ShareCell, TabFooter } from '../PlanParts.jsx';

export const INVESTMENTS = [
  { key: 'sip', label: 'Mutual fund SIPs', hint: 'Equity, debt & hybrid SIPs', icon: TrendingUp, tone: 'blue' },
  { key: 'policies', label: 'Policies & RDs', hint: 'Endowment / ULIP premiums, recurring deposits', icon: FileText, tone: 'yellow' },
  { key: 'other', label: 'Other investments', hint: 'PPF, NPS, stocks, gold, etc.', icon: CirclePlus, tone: 'green' },
];

export const savingsTotal = (savings) =>
  INVESTMENTS.reduce((sum, i) => sum + (Number(savings[i.key]) || 0), 0);

export default function SavingsTab({
  savings, onSavingsChange, incomeTotal, expensesAmount, onBack, onNext, prevLabel, nextLabel,
}) {
  const monthlySavings = incomeTotal - expensesAmount;
  const invested = savingsTotal(savings);
  const surplus = monthlySavings - invested;
  const ofIncome = (n) => (incomeTotal ? (n / incomeTotal) * 100 : 0);

  return (
    <section className="plan-card plan-card--flush">
      <div className="plan-card__pad">
        <h2>Savings &amp; investments</h2>
        <p className="plan-card__sub">What's left each month, and how much of it is already committed.</p>

        <div className="sav__calc">
          <div className="sav__box"><span>INCOME</span><strong>{inr(incomeTotal)}</strong></div>
          <i aria-hidden="true">−</i>
          <div className="sav__box"><span>EXPENSES</span><strong>{inr(expensesAmount)}</strong></div>
          <i aria-hidden="true">=</i>
          <div className="sav__box sav__box--dark"><span>MONTHLY SAVINGS</span><strong>{inr(monthlySavings)}</strong></div>
        </div>
      </div>

      <div className="plan-table">
        <div className="plan-row plan-row--head">
          <span>ONGOING INVESTMENTS</span><span>MONTHLY</span><span>ANNUAL</span><span>% OF INCOME</span>
        </div>

        {INVESTMENTS.map(({ key, label, hint, icon: Icon, tone }) => {
          const value = Number(savings[key]) || 0;
          return (
            <div key={key} className="plan-row">
              <div className="plan-source">
                <span className={`plan-icon tone-bg--${tone}`}><Icon size={16} /></span>
                <div><strong>{label}</strong><small>{hint}</small></div>
              </div>
              <AmountInput label={label} value={savings[key]} onChange={(v) => onSavingsChange({ ...savings, [key]: v })} />
              <span className="plan-annual">{inr(value * 12)}</span>
              <ShareCell percent={ofIncome(value)} tone={tone} />
            </div>
          );
        })}

        <div className="plan-row plan-row--total">
          <strong>Total ongoing investments</strong>
          <strong>{inr(invested)}</strong>
          <span>{inr(invested * 12)}</span>
          <strong>{ofIncome(invested).toFixed(1)}%</strong>
        </div>
      </div>

      <div className={`sav__surplus${surplus < 0 ? ' sav__surplus--deficit' : ''}`}>
        <div>
          <h3>Surplus for new investments</h3>
          <p>Available each month for new investments or goals</p>
        </div>
        <strong>{inr(surplus)} <small>/mo</small></strong>
      </div>

      <TabFooter onBack={onBack} onNext={onNext} prevLabel={prevLabel} nextLabel={nextLabel} />
    </section>
  );
}
