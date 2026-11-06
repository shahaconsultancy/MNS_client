import { useState } from 'react';
import { CreditCard, House, ShieldCheck, ShoppingBag, Users } from 'lucide-react';
import { inr, pct } from '../format.js';
import { AmountInput, CategoryRow, ShareCell, TabFooter } from '../PlanParts.jsx';

export const CATEGORIES = [
  {
    key: 'household', label: 'Household', icon: House, tone: 'blue',
    items: [
      'House rent',
      'House – society, maintenance, repair',
      'Food, grocery & toiletries',
      'Vehicle – fuel, maintenance / travels',
      'Medicines / doctor / healthcare',
      'Electricity / water / AMCs',
      'Domestic help / car driver',
      'Mobile / telephone / internet',
      'TV cable / newspapers / subscriptions',
      'Others (household)',
    ],
  },
  {
    key: 'lifestyle', label: 'Lifestyle', icon: ShoppingBag, tone: 'yellow',
    items: [
      'Clothes & accessories',
      'Shopping, gifts, electronics, gadgets',
      'Dining / movies / sports',
      'Personal care – salon, spa, gym',
      'Travel & annual vacations',
      'Fitness coach / financial advisor / CA',
      'Hobbies / club memberships',
      'Professional courses / education',
      'Charity / donations',
      'Others (lifestyle)',
    ],
  },
  {
    key: 'dependents', label: 'Dependents', icon: Users, tone: 'purple',
    items: ["Children's schooling / college / tuitions", 'Contribution to parents', 'Others – siblings, relatives'],
  },
  {
    key: 'insurance', label: 'Insurance premiums', icon: ShieldCheck, tone: 'green',
    items: ['Life insurance (term / pure)', 'Health insurance (mediclaim, PA, CI)', 'General insurance (motor & others)'],
  },
  {
    key: 'loans', label: 'Loan EMIs', icon: CreditCard, tone: 'red',
    items: ['Home loan EMIs', 'Vehicle loan EMIs', 'Other loan EMIs'],
  },
];

const itemKey = (cat, i) => `${cat.key}-${i}`;
const sum = (cat, expenses) => cat.items.reduce((s, _, i) => s + (Number(expenses[itemKey(cat, i)]) || 0), 0);

export const expensesTotal = (expenses) => CATEGORIES.reduce((s, c)  => s + sum(c, expenses), 0);
export const expenseBreakdown = (expenses) => CATEGORIES.map((c) => ({ label: c.label, tone: c.tone, value: sum(c, expenses) }));

export default function ExpensesTab({
  expenses, onExpensesChange, incomeTotal, onBack, onNext, prevLabel, nextLabel,
}) {
  const [open, setOpen] = useState({});
  const total = expensesTotal(expenses);
  const ofIncome = (n) => (incomeTotal ? (n / incomeTotal) * 100 : 0);
  const surplus = incomeTotal - total;

  return (
    <section className="plan-card plan-card--flush">
      <div className="plan-card__pad">
        <h2>Expenses</h2>
        <p className="plan-card__sub">Monthly household outflow, grouped by category.</p>
      </div>

      <div className="plan-table">
        <div className="plan-row plan-row--head">
          <span>CATEGORY / ITEM</span><span>MONTHLY</span><span>ANNUAL</span><span>% OF INCOME</span>
        </div>

        {CATEGORIES.map((cat) => {
          const isOpen = Boolean(open[cat.key]);
          const catTotal = sum(cat, expenses);
          const filled = cat.items.filter((_, i) => Number(expenses[itemKey(cat, i)]) > 0).length;
          return (
            <div key={cat.key}>
              <CategoryRow
                icon={cat.icon}
                label={cat.label}
                tone={cat.tone}
                isOpen={isOpen}
                onToggle={() => setOpen({ ...open, [cat.key]: !isOpen })}
                filled={filled}
                count={cat.items.length}
                total={catTotal}
                extra={<span className="plan-annual">{inr(catTotal * 12)}</span>}
                percent={ofIncome(catTotal)}
              />

              {isOpen && cat.items.map((label, i) => {
                const key = itemKey(cat, i);
                const value = Number(expenses[key]) || 0;
                return (
                  <div key={key} className="plan-row exp__item">
                    <div className="plan-source">
                      <span className="exp__index">{i + 1}</span>
                      <strong>{label}</strong>
                    </div>
                    <AmountInput label={label} value={expenses[key]} onChange={(v) => onExpensesChange({ ...expenses, [key]: v })} />
                    <span className="plan-annual">{inr(value * 12)}</span>
                    <ShareCell percent={ofIncome(value)} tone={cat.tone} />
                  </div>
                );
              })}
            </div>
          );
        })}

        <div className="plan-row plan-row--total">
          <strong>Total expenses</strong>
          <strong>{inr(total)}</strong>
          <span>{inr(total * 12)}</span>
          <strong>{pct(ofIncome(total))}</strong>
        </div>

        <div className="plan-row exp__surplus">
          <span>Monthly surplus after expenses (income − expenses)</span>
          <span />
          <span />
          <strong className={surplus < 0 ? 'amount--deficit' : 'amount--surplus'}>
            {surplus < 0 ? '-' : ''}{inr(Math.abs(surplus))}
          </strong>
        </div>
      </div>

      <TabFooter onBack={onBack} onNext={onNext} prevLabel={prevLabel} nextLabel={nextLabel} />
    </section>
  );
}
