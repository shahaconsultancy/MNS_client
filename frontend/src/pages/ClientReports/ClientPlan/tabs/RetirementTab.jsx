import { Pencil } from 'lucide-react';
import { inr, sipFactor } from '../format.js';
import { NumberBox, PlanChart, TabFooter } from '../PlanParts.jsx';

const num = (v) => Number(v) || 0;

// Corpus needed at retirement (expenses grow with inflation, the pot earns `after`), and the SIP to reach it.
export function retirementCalc(r) {
  const yearsTo = Math.max(0, num(r.retireAge) - num(r.age));
  const yearsIn = Math.max(0, num(r.life) - num(r.retireAge));
  const inflation = num(r.inflation) / 100;
  const before = num(r.before) / 100;
  const q = (1 + inflation) / (1 + num(r.after) / 100);
  const annuity = Math.abs(q - 1) < 1e-9 ? yearsIn : (1 - q ** yearsIn) / (1 - q);

  const expenseAtRetirement = num(r.expense) * (1 + inflation) ** yearsTo;
  const required = Math.round(expenseAtRetirement * 12 * annuity);
  const grown = Math.round(num(r.corpus) * (1 + before) ** yearsTo);
  const gap = Math.max(0, required - grown);
  const months = yearsTo * 12;
  const sip = months ? Math.round(gap / sipFactor(before, months)) : 0;
  const corpusAt = (t) => num(r.corpus) * (1 + before) ** t + sip * sipFactor(before, t * 12);

  return { yearsTo, yearsIn, expenseAtRetirement: Math.round(expenseAtRetirement), required, grown, gap, months, sip, corpusAt };
}

export default function RetirementTab({ retirement: r, onRetirementChange, expensesAmount, onBack, onNext, prevLabel, nextLabel }) {
  const c = retirementCalc(r);
  const set = (field) => (value) => onRetirementChange({ ...r, [field]: value });

  const stats = [
    { label: 'MONTHLY EXPENSE AT RETIREMENT', value: c.expenseAtRetirement, note: `Today's ${inr(num(r.expense))} after ${c.yearsTo} yrs at ${num(r.inflation)}% inflation` },
    { label: 'CORPUS REQUIRED', value: c.required, note: `To fund ${c.yearsIn} yrs of retirement (age ${num(r.retireAge)}–${num(r.life)})`, cls: 'plan__stat--dark' },
    { label: 'EXISTING CORPUS (GROWN)', value: c.grown, note: `${inr(num(r.corpus))} growing at ${num(r.before)}% for ${c.yearsTo} yrs` },
    { label: 'REQUIRED MONTHLY SIP', value: c.sip, note: `Closes the ${inr(c.gap)} gap over ${c.months} months`, cls: 'plan__stat--gold' },
  ];

  return (
    <section className="plan-card plan-card--flush">
      <div className="plan-card__pad">
        <h2>Retirement planner</h2>
        <p className="plan-card__sub">The corpus needed to sustain the client's lifestyle after retirement, and the SIP to get there.</p>
      </div>

      <div className="ins__section">
        <h3 className="plan-section"><Pencil size={15} /> INPUTS</h3>
        <div className="plan-grid4">
          <NumberBox
            className="ret__wide" label="Current monthly expense" prefix="₹" value={r.expense} onChange={set('expense')}
            hint={<button type="button" className="ret__link" onClick={() => set('expense')(String(expensesAmount))}>Use plan expenses ({inr(expensesAmount)}/mo)</button>}
          />
          <NumberBox
            className="ret__wide" label="Existing retirement corpus" prefix="₹" value={r.corpus} onChange={set('corpus')}
            hint={<small>Today's value of savings set aside for retirement</small>}
          />
          <NumberBox label="Current age" suffix="yrs" max={2} value={r.age} onChange={set('age')} />
          <NumberBox label="Retirement age" suffix="yrs" max={2} value={r.retireAge} onChange={set('retireAge')} />
          <NumberBox label="Life expectancy" suffix="yrs" max={3} value={r.life} onChange={set('life')} />
          <NumberBox label="Inflation" suffix="%" max={2} value={r.inflation} onChange={set('inflation')} />
          <NumberBox label="Return before retirement" suffix="%" max={2} value={r.before} onChange={set('before')} />
          <NumberBox label="Return after retirement" suffix="%" max={2} value={r.after} onChange={set('after')} />
        </div>
      </div>

      <div className="ins__section">
        <div className="plan-stats">
          {stats.map((s) => (
            <div key={s.label} className={`plan__stat ${s.cls || ''}`}>
              <span className="plan__stat-label">{s.label}</span>
              <strong>{inr(s.value)}</strong>
              <small>{s.note}</small>
            </div>
          ))}
        </div>
      </div>

      <div className="ins__section">
        <div className="ret__chart-head">
          <h3>Corpus build-up to age {num(r.retireAge)}</h3>
          <div className="plan-legend">
            <span className="plan-key--corpus">Projected corpus</span>
            <span className="plan-key--target">Corpus required</span>
          </div>
        </div>
        <div className="ret__chart">
          <PlanChart
            W={1100} H={380} years={c.yearsTo} from={num(r.age)} area xTitle="Age"
            startNote={num(r.corpus) ? `${num(r.corpus).toLocaleString('en-IN')} today` : ''}
            lines={[{ at: () => c.required, cls: 'target' }, { at: c.corpusAt, cls: 'corpus' }]}
          />
        </div>
      </div>

      <TabFooter onBack={onBack} onNext={onNext} prevLabel={prevLabel} nextLabel={nextLabel} />
    </section>
  );
}
