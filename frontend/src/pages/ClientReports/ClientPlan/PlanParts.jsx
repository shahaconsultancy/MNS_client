import { ArrowLeft, ArrowRight, ChevronDown, ChevronRight } from 'lucide-react';
import { inr, lakhs, pct } from './format.js';

// Small pieces shared by every table-style tab (Income, Expenses, Savings, ...).

// ₹ amount box. `value` is the raw digit string kept in the page state.
export function AmountInput({ label, value, onChange }) {
  return (
    <label className="plan-amount">
      <em>₹</em>
      <input
        type="text"
        inputMode="numeric"
        placeholder="0"
        aria-label={`${label} per month`}
        value={value || ''}
        onChange={(e) => onChange(e.target.value.replace(/\D/g, '').slice(0, 12))}
      />
    </label>
  );
}

// Small progress bar + percentage. `percent` is 0-100.
export function ShareCell({ percent, tone, strong = false }) {
  const Label = strong ? 'strong' : 'span';
  return (
    <div className="plan-share">
      <i><b className={`tone--${tone}`} style={{ width: `${Math.min(percent, 100)}%` }} /></i>
      <Label>{pct(percent)}</Label>
    </div>
  );
}

// "← Previous" / "Next: X →" buttons at the bottom of a tab.
export function TabFooter({ onBack, onNext, prevLabel, nextLabel }) {
  return (
    <footer className="plan-footer plan-footer--split">
      <button type="button" className="plan-next" onClick={onBack}>
        <ArrowLeft size={14} /> {prevLabel}
      </button>
      <button type="button" className="plan-next" onClick={onNext}>
        Next: {nextLabel} <ArrowRight size={14} />
      </button>
    </footer>
  );
}

// Expandable group header row (Expenses categories, Asset classes, ...).
// `extra` fills the third column (annual value, or an empty cell).
export function CategoryRow({
  icon: Icon, label, tone, isOpen, onToggle, filled, count, total, extra, percent, className = '',
}) {
  return (
    <button type="button" className={`plan-row exp__cat ${className}`} aria-expanded={isOpen} onClick={onToggle}>
      <span className="plan-source">
        <span className="exp__chevron">{isOpen ? <ChevronDown size={16} /> : <ChevronRight size={16} />}</span>
        <span className={`plan-icon tone-bg--${tone}`}><Icon size={16} /></span>
        <span><strong>{label}</strong><small>{filled} of {count} items filled</small></span>
      </span>
      <strong className="exp__num">{inr(total)}</strong>
      {extra}
      <ShareCell percent={percent} tone={tone} strong />
    </button>
  );
}

// Labelled digits-only box with a ₹ prefix or a unit suffix; `hint` goes under it.
export function NumberBox({ label, value, onChange, prefix, suffix, max = 12, hint, className = '' }) {
  return (
    <label className={`plan-field ${className}`}>
      <span>{label}</span>
      <div className="plan-input">
        {prefix && <em>{prefix}</em>}
        <input
          type="text"
          inputMode="numeric"
          placeholder="0"
          value={prefix && value ? Number(value).toLocaleString('en-IN') : value}
          onChange={(e) => onChange(e.target.value.replace(/\D/g, '').slice(0, max))}
        />
        {suffix && <em>{suffix}</em>}
      </div>
      {hint}
    </label>
  );
}

// Line chart over `years` steps starting at x = `from`. `lines` are { at(t), cls: 'corpus' | 'target' };
// the 'corpus' line gets a filled area when `area` is set. `startNote` is a bold note beside the first tick.
export function PlanChart({ years, from = 0, W = 360, H = 250, lines, area, startNote, xTitle }) {
  const L = 44, R = 14, T = 12, B = xTitle ? 46 : 30;
  const ts = Array.from({ length: years + 1 }, (_, t) => t);
  const max = Math.max(...ts.flatMap((t) => lines.map((l) => l.at(t))), 1);
  const x = (t) => L + (years ? (t / years) * (W - L - R) : 0);
  const y = (v) => T + (1 - v / max) * (H - T - B);
  const path = (fn) => ts.map((t) => `${x(t)},${y(fn(t))}`).join(' ');
  const step = Math.ceil(years / 9) || 1;
  const ticks = ts.filter((t) => t === 0 || t === years || (t % step === 0 && years - t >= step));

  return (
    <svg className="plan-chart" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Projected corpus against target">
      {[0, 1, 2, 3, 4].map((i) => (
        <g key={i}>
          <line x1={L} x2={W - R} y1={y((max / 4) * i)} y2={y((max / 4) * i)} className="plan-chart__grid" />
          <text x={L - 8} y={y((max / 4) * i) + 4} textAnchor="end">{lakhs((max / 4) * i)}</text>
        </g>
      ))}
      {ticks.map((t) => <text key={t} x={x(t)} y={H - B + 18} textAnchor="middle">{from + t}</text>)}
      {startNote && <text x={x(0) + 18} y={H - B + 18} className="plan-chart__note">{startNote}</text>}
      {xTitle && <text x={(L + W - R) / 2} y={H - 6} textAnchor="middle">{xTitle}</text>}
      {area && (
        <polygon
          points={`${x(0)},${y(0)} ${path(lines.find((l) => l.cls === 'corpus').at)} ${x(years)},${y(0)}`}
          className="plan-chart__area"
        />
      )}
      {lines.map((l) => <polyline key={l.cls} points={path(l.at)} className={`plan-chart__line plan-chart__line--${l.cls}`} />)}
      {lines.map((l) => (
        <circle key={l.cls} cx={x(years)} cy={y(l.at(years))} r="4" className={`plan-chart__dot plan-chart__dot--${l.cls}`} />
      ))}
    </svg>
  );
}
