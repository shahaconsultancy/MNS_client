import { Flag, Heart, Pencil, Plus, Trash2 } from 'lucide-react';
import { inr, pct, sipFactor } from '../format.js';
import { NumberBox, PlanChart, TabFooter } from '../PlanParts.jsx';

const PRIORITIES = ['High', 'Medium', 'Low'];
const RANK = { High: 3, Medium: 2, Low: 1 };
const NEW_GOAL = { name: 'New goal', cost: '1000000', years: '5', inflation: '6', ret: '10', savings: '', sip: '', priority: 'Medium' };

const num = (v) => Number(v) || 0;

// Everything a goal card shows, worked out from its inputs.
export function goalCalc(g) {
  const years = num(g.years);
  const rate = num(g.ret) / 100;
  const inflation = num(g.inflation) / 100;
  const corpusAt = (t) => num(g.savings) * (1 + rate) ** t + num(g.sip) * sipFactor(rate, t * 12);
  const targetAt = (t) => num(g.cost) * (1 + inflation) ** t;
  const target = Math.round(targetAt(years));
  const corpus = Math.round(corpusAt(years));
  const shortfall = Math.max(0, target - corpus);
  const unfunded = Math.max(0, target - num(g.savings) * (1 + rate) ** years);
  const funded = target ? Math.min((corpus / target) * 100, 100) : 100;
  return {
    years, target, corpus, shortfall, funded, corpusAt, targetAt,
    needSip: Math.round(unfunded / sipFactor(rate, years * 12)),
    sipToClose: Math.ceil(shortfall / sipFactor(rate, years * 12)),
  };
}

function GoalCard({ goal, c, first, onChange, onDelete }) {
  const set = (field) => (value) => onChange({ ...goal, [field]: value });
  const done = c.shortfall <= 0;

  return (
    <article className={`plan-item${first ? ' plan-item--first' : ''}`}>
      <header className="plan-item__head">
        <span className="plan-icon tone-bg--blue"><Heart size={18} /></span>
        <input
          className="plan-item__name" type="text" maxLength={60} aria-label="Goal name"
          value={goal.name} onChange={(e) => set('name')(e.target.value)}
        />
        {first && <span className="goal__first"><Flag size={12} /> Fund first</span>}
        <select className="goal__priority" aria-label="Priority" value={goal.priority} onChange={(e) => set('priority')(e.target.value)}>
          {PRIORITIES.map((p) => <option key={p}>{p}</option>)}
        </select>
        <span className={`ins__badge ${done ? 'ins__badge--ok' : 'ins__badge--bad'}`}>{done ? 'Funded' : 'Behind'}</span>
        <button type="button" className="plan-item__delete" onClick={onDelete} aria-label={`Delete ${goal.name}`}><Trash2 size={16} /></button>
      </header>

      <div className="goal__body">
        <div className="goal__inputs">
          <NumberBox label="Cost today" prefix="₹" value={goal.cost} onChange={set('cost')} />
          <NumberBox label="Years to goal" suffix="yrs" value={goal.years} onChange={set('years')} max={2} />
          <NumberBox label="Inflation" suffix="%" value={goal.inflation} onChange={set('inflation')} max={2} />
          <NumberBox label="Expected return" suffix="%" value={goal.ret} onChange={set('ret')} max={2} />
          <NumberBox label="Current savings" prefix="₹" value={goal.savings} onChange={set('savings')} />
          <NumberBox label="Monthly SIP" prefix="₹" value={goal.sip} onChange={set('sip')} />
        </div>

        <div className="goal__result">
          <div className="goal__funded"><span>Funded</span><strong>{Math.round(c.funded)}%</strong></div>
          <div className="ins__bar"><b className="ins__fill--ok" style={{ width: `${c.funded}%` }} /></div>
          <dl className="goal__rows">
            <div><dt>Future target</dt><dd>{inr(c.target)}</dd></div>
            <div><dt>Projected corpus</dt><dd>{inr(c.corpus)}</dd></div>
            <div className="goal__shortfall">
              <dt>{done ? 'Surplus' : 'Shortfall'}</dt>
              <dd className={done ? 'amount--surplus' : 'amount--deficit'}>{inr(done ? c.corpus - c.target : c.shortfall)}</dd>
            </div>
          </dl>
          <div className="goal__sip">
            <div><span>SIP TO FULLY FUND</span><strong>{inr(c.needSip)}/mo</strong></div>
            <button type="button" className="plan-next" onClick={() => set('sip')(String(c.needSip))}>
              <Pencil size={13} /> Auto-fund
            </button>
          </div>
        </div>

        <div className="goal__plot">
          <div className="plan-legend">
            <span className="plan-key--corpus">Projected corpus</span>
            <span className="plan-key--target">Target</span>
            <span>Years</span>
          </div>
          <PlanChart years={c.years} area lines={[{ at: c.targetAt, cls: 'target' }, { at: c.corpusAt, cls: 'corpus' }]} />
        </div>
      </div>
    </article>
  );
}

export default function GoalsTab({ goals, onGoalsChange, surplusAmount, onBack, onNext, prevLabel, nextLabel }) {
  const calcs = goals.map(goalCalc);
  const target = calcs.reduce((s, c) => s + c.target, 0);
  const corpus = calcs.reduce((s, c) => s + c.corpus, 0);
  const sip = goals.reduce((s, g) => s + num(g.sip), 0);
  const funded = calcs.filter((c) => c.shortfall <= 0).length;

  // The goal to fund first: highest priority, then the nearest deadline, among those still short.
  const firstIdx = goals
    .map((g, i) => i)
    .filter((i) => calcs[i].shortfall > 0)
    .sort((a, b) => RANK[goals[b].priority] - RANK[goals[a].priority] || calcs[a].years - calcs[b].years)[0];
  const first = firstIdx === undefined ? null : { goal: goals[firstIdx], c: calcs[firstIdx] };

  const stats = [
    { label: 'GOALS', value: goals.length, note: `${funded} fully funded` },
    { label: 'TOTAL TARGET (FUTURE)', value: inr(target), note: 'Inflation-adjusted' },
    { label: 'PROJECTED CORPUS', value: inr(corpus), note: `${pct(target ? (corpus / target) * 100 : 0)} of target`, dark: true },
    { label: 'PLANNED SIP / MONTH', value: inr(sip), note: `Surplus available ${inr(Math.max(0, surplusAmount))}/mo` },
  ];

  const update = (i, goal) => onGoalsChange(goals.map((g, j) => (j === i ? goal : g)));

  return (
    <section className="plan-card plan-card--flush">
      <div className="plan-card__pad plan-top">
        <div>
          <h2>Goals</h2>
          <p className="plan-card__sub">Each life goal against its inflation-adjusted target.</p>
        </div>
        <button type="button" className="plan-add" onClick={() => onGoalsChange([...goals, { ...NEW_GOAL, id: Date.now() }])}>
          <Plus size={15} /> Add goal
        </button>
      </div>

      <div className="plan-stats">
        {stats.map((s) => (
          <div key={s.label} className={`plan__stat${s.dark ? ' plan__stat--dark' : ''}`}>
            <span className="plan__stat-label">{s.label}</span>
            <strong>{s.value}</strong>
            <small>{s.note}</small>
          </div>
        ))}
      </div>

      {first && (
        <p className="plan-note goal__banner">
          <Flag size={14} />
          <span>
            <b>Fund first: {first.goal.name}</b> — {first.goal.priority.toLowerCase()} priority, due in {first.c.years} years,{' '}
            {Math.round(first.c.funded)}% funded. Shortfall of {inr(first.c.shortfall)}; add about <b>{inr(first.c.sipToClose)}/mo</b> to the SIP to close it.
          </span>
        </p>
      )}

      <div className="plan-item__list">
        {goals.length === 0 && <p className="plan-item__empty">No goals yet. Click “Add goal” to plan for one.</p>}
        {goals.map((g, i) => (
          <GoalCard
            key={g.id}
            goal={g}
            c={calcs[i]}
            first={i === firstIdx}
            onChange={(goal) => update(i, goal)}
            onDelete={() => onGoalsChange(goals.filter((_, j) => j !== i))}
          />
        ))}
      </div>

      <TabFooter onBack={onBack} onNext={onNext} prevLabel={prevLabel} nextLabel={nextLabel} />
    </section>
  );
}
